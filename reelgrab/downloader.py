"""In-process media download via yt-dlp (+ ffmpeg convert / probe / thumbnail)."""

from __future__ import annotations

import asyncio
import json
import logging
import mimetypes
import shutil
import subprocess
import urllib.error
import urllib.request
import uuid
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from reelgrab.captions import thumbnail_dimensions
from reelgrab.config import (
    DEFAULT_YTDLP_FORMAT,
    LEGACY_YTDLP_FORMAT,
    ConvertConfig,
    DownloadConfig,
)
from reelgrab.urls import is_amplify_video_url, is_http_url

log = logging.getLogger("reelgrab.downloader")

VIDEO_EXTENSIONS = {".mp4", ".webm", ".mkv", ".mov", ".m4v"}
MIN_BYTES = 8_192  # reject empty / stub files
MAX_DIRECT_BYTES = 512 * 1024 * 1024
AMPLIFY_HOSTS = frozenset({"video.twimg.com"})
_DIRECT_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36"
)


class DownloadError(Exception):
    """Raised when a download cannot be completed."""


@dataclass
class MediaFile:
    """A fully downloaded local media file ready to upload."""

    path: Path
    mime: str
    size: int
    duration_ms: int | None = None
    width: int | None = None
    height: int | None = None
    thumbnail: Path | None = None
    thumbnail_width: int | None = None
    thumbnail_height: int | None = None
    title: str | None = None
    uploader: str | None = None
    video_id: str | None = None
    blurhash: str | None = None


@dataclass
class ProbeInfo:
    duration_ms: int | None = None
    width: int | None = None
    height: int | None = None
    video_codec: str | None = None
    audio_codec: str | None = None
    pix_fmt: str | None = None
    container: str | None = None


def guess_mime(path: Path) -> str:
    mime, _ = mimetypes.guess_type(str(path))
    if mime:
        return mime
    ext = path.suffix.lower()
    if ext in VIDEO_EXTENSIONS:
        return (
            "video/mp4"
            if ext in {".mp4", ".m4v", ".mov"}
            else f"video/{ext.lstrip('.')}"
        )
    return "application/octet-stream"


def _run_cmd(args: list[str], *, timeout: float = 120) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        args,
        check=False,
        capture_output=True,
        text=True,
        timeout=timeout,
    )


def probe_full(path: Path) -> ProbeInfo:
    """Full stream probe via ffprobe."""
    info = ProbeInfo()
    if not shutil.which("ffprobe"):
        return info
    try:
        proc = _run_cmd(
            [
                "ffprobe",
                "-v",
                "error",
                "-show_entries",
                "format=format_name,duration:stream=index,codec_type,codec_name,width,height,pix_fmt",
                "-of",
                "json",
                str(path),
            ],
            timeout=60,
        )
        if proc.returncode != 0:
            log.warning("ffprobe failed: %s", (proc.stderr or "")[:200])
            return info
        data = json.loads(proc.stdout or "{}")
        fmt = data.get("format") or {}
        info.container = (fmt.get("format_name") or "").lower() or None
        if fmt.get("duration") is not None:
            try:
                info.duration_ms = int(float(fmt["duration"]) * 1000)
            except (TypeError, ValueError):
                pass
        for stream in data.get("streams") or []:
            ctype = stream.get("codec_type")
            if ctype == "video" and info.video_codec is None:
                info.video_codec = (stream.get("codec_name") or "").lower() or None
                info.pix_fmt = (stream.get("pix_fmt") or "").lower() or None
                try:
                    info.width = int(stream.get("width") or 0) or None
                    info.height = int(stream.get("height") or 0) or None
                except (TypeError, ValueError):
                    pass
            elif ctype == "audio" and info.audio_codec is None:
                info.audio_codec = (stream.get("codec_name") or "").lower() or None
        return info
    except Exception as exc:
        log.warning("ffprobe error for %s: %s", path, exc)
        return info


def probe_media(path: Path) -> tuple[int | None, int | None, int | None]:
    """Return (duration_ms, width, height) via ffprobe."""
    p = probe_full(path)
    return p.duration_ms, p.width, p.height


def already_bridge_compatible(path: Path, conv: ConvertConfig) -> bool:
    """True if file is already H.264 + AAC + yuv420p in an MP4-family container."""
    p = probe_full(path)
    if not p.video_codec:
        return False
    ok_v = p.video_codec in ("h264", "avc1")
    ok_a = p.audio_codec in (None, "aac", "mp4a")  # silent ok
    ok_pix = (p.pix_fmt or "yuv420p") == "yuv420p" or p.pix_fmt is None
    container = p.container or ""
    ok_c = any(x in container for x in ("mp4", "isom", "iso2", "avc1", "m4a", "mov"))
    # Respect max dimensions if set
    if conv.max_width and p.width and p.width > conv.max_width:
        return False
    if conv.max_height and p.height and p.height > conv.max_height:
        return False
    return bool(ok_v and ok_a and ok_pix and ok_c and path.suffix.lower() in {".mp4", ".m4v"})


def build_convert_args(src: Path, dest: Path, conv: ConvertConfig) -> list[str]:
    """Build ffmpeg argv for mobile/bridge-friendly re-encode."""
    args: list[str] = [
        "ffmpeg",
        "-y",
        "-hide_banner",
        "-loglevel",
        "error",
        "-i",
        str(src),
    ]

    # Scale: fit within max_width x max_height, keep AR, force even dims for yuv420p.
    max_w = max(0, int(conv.max_width or 0))
    max_h = max(0, int(conv.max_height or 0))
    vf_parts: list[str] = []
    if max_w > 0 or max_h > 0:
        w = max_w if max_w > 0 else 99999
        h = max_h if max_h > 0 else 99999
        # scale=w:h:force_original_aspect_ratio=decrease then pad to even
        vf_parts.append(
            f"scale='min({w},iw)':'min({h},ih)':force_original_aspect_ratio=decrease"
        )
        vf_parts.append("scale=trunc(iw/2)*2:trunc(ih/2)*2")
    else:
        vf_parts.append("scale=trunc(iw/2)*2:trunc(ih/2)*2")

    pix = (conv.pixel_format or "yuv420p").strip()
    vf_parts.append(f"format={pix}")

    args += ["-vf", ",".join(vf_parts)]
    args += [
        "-c:v",
        (conv.video_codec or "libx264").strip(),
        "-preset",
        (conv.video_preset or "veryfast").strip(),
        "-crf",
        str(int(conv.video_crf)),
        "-pix_fmt",
        pix,
    ]
    if conv.profile:
        args += ["-profile:v", str(conv.profile).strip()]
    if conv.level:
        args += ["-level", str(conv.level).strip()]
    maxrate = (conv.max_bitrate or "").strip()
    if maxrate:
        args += ["-maxrate", maxrate]
        bufsize = (conv.bufsize or "").strip() or maxrate
        args += ["-bufsize", bufsize]

    # Audio: AAC stereo, constant bitrate — widely accepted on mobile clients
    args += [
        "-c:a",
        (conv.audio_codec or "aac").strip(),
        "-b:a",
        (conv.audio_bitrate or "128k").strip(),
        "-ac",
        "2",
        "-ar",
        "44100",
    ]
    # If source has no audio, still produce a valid track-less or silent file.
    # -shortest avoids hanging when streams differ; map best effort.
    args += ["-movflags", (conv.movflags or "+faststart").strip()]

    for extra in conv.extra_args or []:
        if extra is not None and str(extra).strip():
            args.append(str(extra))

    args.append(str(dest))
    return args


def resolve_ytdlp_format(fmt: str | None) -> str:
    """Use the H.264/AAC selector for the historical ``bv*+ba/b`` default."""
    raw = (fmt or "").strip()
    if raw in ("", LEGACY_YTDLP_FORMAT):
        return DEFAULT_YTDLP_FORMAT
    return raw


def _bitrate_kbps(value: str) -> int:
    text = (value or "").strip().lower()
    if not text:
        return 0
    try:
        if text.endswith("k"):
            return int(float(text[:-1]))
        if text.endswith("m"):
            return int(float(text[:-1]) * 1000)
        return int(float(text)) // 1000
    except ValueError:
        return 0


def _format_kbps(kbps: int) -> str:
    return f"{max(150, int(kbps))}k"


def bitrate_for_target(duration_ms: int | None, target_bytes: int, *, audio_kbps: int = 128) -> str:
    """Video bitrate that should land near ``target_bytes`` for this duration."""
    if not duration_ms or duration_ms < 500 or target_bytes <= 0:
        return "800k"
    seconds = max(0.5, duration_ms / 1000.0)
    total_kbps = (target_bytes * 8) / seconds / 1000.0
    video_kbps = int((total_kbps - audio_kbps) * 0.85)
    return _format_kbps(video_kbps)


def _cap_dim(configured: int, cap: int) -> int:
    if not configured or configured <= 0:
        return cap
    return min(int(configured), int(cap))


def quality_ladder(
    conv: ConvertConfig,
    *,
    duration_ms: int | None,
    target_bytes: int,
) -> list[ConvertConfig]:
    """Tighter encode settings, used when a file is over the upload limit.

    The first step keeps the configured CRF and profile, with a bitrate aimed
    at the homeserver limit. Later steps raise CRF and shrink the frame.
    """
    base = bitrate_for_target(duration_ms, target_bytes)
    base_kbps = _bitrate_kbps(base) or 800
    specs = [
        (
            int(conv.video_crf),
            _cap_dim(conv.max_width, 1280),
            _cap_dim(conv.max_height, 1280),
            base_kbps,
        ),
        (
            min(40, int(conv.video_crf) + 4),
            _cap_dim(conv.max_width, 960),
            _cap_dim(conv.max_height, 960),
            int(base_kbps * 0.6),
        ),
        (
            min(40, int(conv.video_crf) + 8),
            _cap_dim(conv.max_width, 720),
            _cap_dim(conv.max_height, 720),
            int(base_kbps * 0.35),
        ),
        (
            40,
            _cap_dim(conv.max_width, 540),
            _cap_dim(conv.max_height, 960),
            int(base_kbps * 0.22),
        ),
    ]
    steps: list[ConvertConfig] = []
    seen: set[tuple[int, int, int, str]] = set()
    for crf, width, height, kbps in specs:
        rate = _format_kbps(kbps)
        key = (crf, width, height, rate)
        if key in seen:
            continue
        seen.add(key)
        buf = _format_kbps(max(kbps * 2, kbps))
        steps.append(
            replace(
                conv,
                force=True,
                video_crf=crf,
                max_width=width,
                max_height=height,
                max_bitrate=rate,
                bufsize=buf,
            )
        )
    return steps


def _ffmpeg_convert(src: Path, dest: Path, conv: ConvertConfig) -> Path:
    """Re-encode ``src`` to ``dest``. Raises DownloadError on failure."""
    args = build_convert_args(src, dest, conv)
    timeout = max(30, int(conv.timeout_seconds or 600))
    log.info("ffmpeg convert start in=%s out=%s", src.name, dest.name)
    try:
        proc = _run_cmd(args, timeout=timeout)
    except subprocess.TimeoutExpired as exc:
        raise DownloadError(f"ffmpeg convert timed out after {timeout}s") from exc

    if proc.returncode != 0 or not dest.is_file() or dest.stat().st_size < MIN_BYTES:
        err = (proc.stderr or proc.stdout or "")[:400]
        log.warning("ffmpeg convert failed (will retry anullsrc): %s", err)
        dest.unlink(missing_ok=True)
        retry = build_convert_args(src, dest, conv)
        try:
            i_idx = retry.index("-i")
            retry = (
                retry[: i_idx + 2]
                + [
                    "-f",
                    "lavfi",
                    "-i",
                    "anullsrc=channel_layout=stereo:sample_rate=44100",
                    "-shortest",
                ]
                + retry[i_idx + 2 :]
            )
        except ValueError:
            pass
        try:
            proc = _run_cmd(retry, timeout=timeout)
        except subprocess.TimeoutExpired as exc:
            raise DownloadError(f"ffmpeg convert timed out after {timeout}s") from exc
        if proc.returncode != 0 or not dest.is_file() or dest.stat().st_size < MIN_BYTES:
            err2 = (proc.stderr or proc.stdout or "")[:400]
            raise DownloadError(f"ffmpeg convert failed: {err2 or 'unknown error'}")

    log.info("ffmpeg convert done size=%d", dest.stat().st_size)
    return dest


def _over_upload_target(path: Path, limit_bytes: int | None) -> bool:
    if not limit_bytes or limit_bytes <= 0:
        return False
    return path.is_file() and path.stat().st_size > int(limit_bytes * 0.92)


def shrink_to_upload_limit(
    path: Path,
    job_dir: Path,
    conv: ConvertConfig,
    limit_bytes: int,
    *,
    duration_ms: int | None = None,
) -> Path:
    """Re-encode ``path`` down the quality ladder until it fits ``limit_bytes``."""
    if not shutil.which("ffmpeg"):
        raise DownloadError(
            f"video is {path.stat().st_size} bytes, over the homeserver upload limit "
            f"({limit_bytes} bytes), and ffmpeg is not available"
        )
    target = int(limit_bytes * 0.92)
    source = path
    best = path
    ladder = quality_ladder(conv, duration_ms=duration_ms, target_bytes=target)
    for index, step in enumerate(ladder):
        dest = job_dir / f"{path.stem}_q{index}.mp4"
        try:
            _ffmpeg_convert(source, dest, step)
        except DownloadError as exc:
            log.warning("quality step %s failed: %s", index, exc)
            dest.unlink(missing_ok=True)
            continue
        if dest.stat().st_size < best.stat().st_size:
            if best is not source and best.is_file():
                best.unlink(missing_ok=True)
            best = dest
        else:
            dest.unlink(missing_ok=True)
        if best.stat().st_size <= target:
            break
    if best is not source and source.is_file():
        try:
            source.unlink(missing_ok=True)
        except OSError:
            pass
    if best.stat().st_size > limit_bytes:
        raise DownloadError(
            f"video is still {best.stat().st_size} bytes after reducing quality; "
            f"homeserver upload limit is {limit_bytes} bytes"
        )
    return best


def finalize_encode(
    path: Path,
    job_dir: Path,
    conv: ConvertConfig,
    *,
    limit_bytes: int | None = None,
    duration_ms: int | None = None,
) -> Path:
    """Remux/re-encode for playback, then shrink if the homeserver would reject it."""
    if not conv.enabled:
        if _over_upload_target(path, limit_bytes):
            raise DownloadError(
                "video exceeds the homeserver upload limit and conversion is disabled"
            )
        return path

    compatible = already_bridge_compatible(path, conv)
    if not conv.force and compatible and not _over_upload_target(path, limit_bytes):
        log.info("source already compatible — skip convert (%s)", path.name)
        return path

    if limit_bytes and _over_upload_target(path, limit_bytes):
        return shrink_to_upload_limit(
            path, job_dir, conv, limit_bytes, duration_ms=duration_ms
        )
    return convert_for_bridges(path, job_dir, conv)


def convert_for_bridges(path: Path, job_dir: Path, conv: ConvertConfig) -> Path:
    """Re-encode ``path`` to H.264 + AAC MP4, or return it when that is unnecessary.

    Returns path to the converted file (or original if conversion disabled / skipped).
    """
    if not conv.enabled:
        log.info("convert disabled — using source %s", path.name)
        return path

    if not shutil.which("ffmpeg"):
        log.warning("ffmpeg not found — cannot convert media")
        return path

    if not conv.force and already_bridge_compatible(path, conv):
        log.info("source already compatible — skip convert (%s)", path.name)
        return path

    dest = job_dir / f"{path.stem}_bridge.mp4"
    _ffmpeg_convert(path, dest, conv)
    try:
        if path.resolve() != dest.resolve() and path.is_file():
            path.unlink(missing_ok=True)
    except OSError:
        pass
    return dest


def make_thumbnail(path: Path, dest: Path) -> Path | None:
    """Extract a JPEG frame near 1s (or start) for Matrix / bridge clients."""
    if not shutil.which("ffmpeg"):
        return None
    try:
        dest.parent.mkdir(parents=True, exist_ok=True)
        vf = "scale='min(640,iw)':'-2'"
        proc = _run_cmd(
            [
                "ffmpeg",
                "-y",
                "-ss",
                "1",
                "-i",
                str(path),
                "-frames:v",
                "1",
                "-vf",
                vf,
                "-q:v",
                "4",
                str(dest),
            ],
            timeout=60,
        )
        if proc.returncode != 0 or not dest.is_file() or dest.stat().st_size < 100:
            proc = _run_cmd(
                [
                    "ffmpeg",
                    "-y",
                    "-i",
                    str(path),
                    "-frames:v",
                    "1",
                    "-vf",
                    vf,
                    "-q:v",
                    "4",
                    str(dest),
                ],
                timeout=60,
            )
        if dest.is_file() and dest.stat().st_size >= 100:
            return dest
        log.warning("thumbnail generation failed for %s: %s", path, (proc.stderr or "")[:200])
        return None
    except Exception as exc:
        log.warning("thumbnail error for %s: %s", path, exc)
        return None


def _pick_file(job_dir: Path, preferred: Path | None, merge_fmt: str | None) -> Path:
    if preferred is not None:
        if preferred.is_file() and preferred.stat().st_size >= MIN_BYTES:
            return preferred
        if merge_fmt:
            alt = preferred.with_suffix(f".{merge_fmt}")
            if alt.is_file() and alt.stat().st_size >= MIN_BYTES:
                return alt

    files = [
        f
        for f in job_dir.iterdir()
        if f.is_file()
        and not f.name.endswith((".part", ".ytdl", ".temp", ".tmp"))
        and not f.name.endswith("_thumb.jpg")
        and f.stat().st_size >= MIN_BYTES
    ]
    videos = [f for f in files if f.suffix.lower() in VIDEO_EXTENSIONS]
    pool = videos or files
    if not pool:
        raise DownloadError("download finished but no usable file found")
    pool.sort(key=lambda p: p.stat().st_size, reverse=True)
    return pool[0]


def cleanup_job_dir(job_dir: Path) -> None:
    """Best-effort remove a ``job_*`` work directory and its contents."""
    try:
        if not job_dir.is_dir():
            return
        for child in list(job_dir.iterdir()):
            try:
                if child.is_file() or child.is_symlink():
                    child.unlink(missing_ok=True)
            except OSError:
                pass
        job_dir.rmdir()
    except OSError as exc:
        log.warning("could not remove job dir %s: %s", job_dir, exc)


def cleanup_media_path(path: Path) -> None:
    """Remove a downloaded media file and its ``job_*`` parent directory."""
    try:
        path.unlink(missing_ok=True)
    except OSError:
        log.warning("could not remove temp file %s", path)
        return
    parent = path.parent
    if parent.name.startswith("job_"):
        cleanup_job_dir(parent)


class _PinnedRedirectHandler(urllib.request.HTTPRedirectHandler):
    """Follow redirects only when the next URL stays on an allowed host."""

    def __init__(self, allowed_hosts: frozenset[str]) -> None:
        super().__init__()
        self.allowed_hosts = {h.lower() for h in allowed_hosts}

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: ANN001
        parsed = urlparse(newurl)
        host = (parsed.hostname or "").lower()
        if parsed.scheme not in ("http", "https") or host not in self.allowed_hosts:
            raise DownloadError(f"refusing redirect to {newurl}")
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def fetch_direct_mp4(
    url: str,
    dest: Path,
    *,
    allowed_hosts: frozenset[str] = AMPLIFY_HOSTS,
    timeout: float = 60,
    max_bytes: int = MAX_DIRECT_BYTES,
) -> Path:
    """Stream an http(s) MP4 to ``dest``. Redirects must stay on ``allowed_hosts``."""
    if not is_http_url(url):
        raise DownloadError("refusing non-http URL")
    host = (urlparse(url).hostname or "").lower()
    if host not in {h.lower() for h in allowed_hosts}:
        raise DownloadError(f"refusing direct download from {host or 'unknown host'}")

    dest.parent.mkdir(parents=True, exist_ok=True)
    opener = urllib.request.build_opener(_PinnedRedirectHandler(allowed_hosts))
    req = urllib.request.Request(
        url,
        headers={"User-Agent": _DIRECT_UA, "Accept": "video/mp4,video/*;q=0.9,*/*;q=0.8"},
        method="GET",
    )
    try:
        with opener.open(req, timeout=timeout) as resp:
            ctype = (resp.headers.get("Content-Type") or "").split(";", 1)[0].strip().lower()
            if ctype.startswith("text/") or ctype in {"application/json", "application/xml"}:
                raise DownloadError(f"direct URL returned {ctype or 'no content-type'}, not video")
            total = 0
            with dest.open("wb") as fh:
                while True:
                    chunk = resp.read(64 * 1024)
                    if not chunk:
                        break
                    total += len(chunk)
                    if total > max_bytes:
                        raise DownloadError(
                            f"direct mp4 exceeds size cap ({max_bytes} bytes)"
                        )
                    fh.write(chunk)
    except DownloadError:
        dest.unlink(missing_ok=True)
        raise
    except urllib.error.URLError as exc:
        dest.unlink(missing_ok=True)
        raise DownloadError(f"direct mp4 download failed: {exc}") from exc
    except Exception as exc:
        dest.unlink(missing_ok=True)
        raise DownloadError(f"direct mp4 download failed: {exc}") from exc

    if not dest.is_file() or dest.stat().st_size < MIN_BYTES:
        dest.unlink(missing_ok=True)
        raise DownloadError("direct mp4 too small or missing")
    log.info("direct mp4 saved bytes=%d file=%s", dest.stat().st_size, dest.name)
    return dest


def _duration_limit_error(duration_s: float, max_seconds: int) -> str:
    minutes = max(1, int(max_seconds) // 60)
    got_min = int(duration_s) // 60
    got_sec = int(duration_s) % 60
    return (
        f"Video is {got_min} min {got_sec} s, over the {minutes} min limit"
    )


def _reject_if_too_long(duration_ms: int | None, max_seconds: int) -> None:
    if not max_seconds or duration_ms is None or duration_ms <= 0:
        return
    if duration_ms > int(max_seconds) * 1000:
        raise DownloadError(_duration_limit_error(duration_ms / 1000.0, max_seconds))


def try_blurhash(path: Path) -> str | None:
    """Blurhash of a thumbnail. Missing optional libraries skip it."""
    try:
        import blurhash
        from PIL import Image
    except ImportError:
        return None
    try:
        with Image.open(path) as image:
            return blurhash.encode(image.convert("RGB"), x_components=4, y_components=3)
    except Exception as exc:
        log.debug("blurhash skipped for %s: %s", path, exc)
        return None


def _finish_media(
    path: Path,
    job_dir: Path,
    cfg: DownloadConfig,
    *,
    title: str | None = None,
    uploader: str | None = None,
    video_id: str | None = None,
) -> MediaFile:
    """Convert, probe, and thumbnail a file already on disk."""
    size = path.stat().st_size
    if size < MIN_BYTES:
        raise DownloadError(f"downloaded file too small ({size} bytes): {path.name}")
    if path.suffix.lower() not in VIDEO_EXTENSIONS:
        raise DownloadError("No video in that link")

    early = probe_full(path)
    _reject_if_too_long(early.duration_ms, int(cfg.max_duration_seconds or 0))

    conv = cfg.convert if isinstance(cfg.convert, ConvertConfig) else ConvertConfig()
    limit = int(cfg.max_upload_bytes or 0) or None
    path = finalize_encode(
        path,
        job_dir,
        conv,
        limit_bytes=limit,
        duration_ms=early.duration_ms,
    )

    mime = guess_mime(path)
    probe = probe_full(path)
    duration_ms, width, height = probe.duration_ms, probe.width, probe.height
    _reject_if_too_long(duration_ms, int(cfg.max_duration_seconds or 0))
    if mime.startswith("video/") and duration_ms is not None and duration_ms < 100:
        raise DownloadError(
            f"downloaded video is empty/too short ({duration_ms}ms): {path.name}"
        )

    size = path.stat().st_size
    thumb_path = job_dir / f"{path.stem}_thumb.jpg"
    thumb = make_thumbnail(path, thumb_path) if mime.startswith("video/") else None
    thumb_w: int | None = None
    thumb_h: int | None = None
    blur: str | None = None
    if thumb and thumb.is_file():
        thumb_probe = probe_full(thumb)
        if thumb_probe.width and thumb_probe.height:
            thumb_w, thumb_h = thumb_probe.width, thumb_probe.height
        elif width and height:
            thumb_w, thumb_h = thumbnail_dimensions(width, height)
        blur = try_blurhash(thumb)
    elif width and height:
        thumb_w, thumb_h = thumbnail_dimensions(width, height)

    log.info(
        "media ready file=%s size=%d duration_ms=%s %sx%s v=%s a=%s thumb=%s",
        path.name,
        size,
        duration_ms,
        width,
        height,
        probe.video_codec,
        probe.audio_codec,
        bool(thumb),
    )
    return MediaFile(
        path=path,
        mime=mime,
        size=size,
        duration_ms=duration_ms,
        width=width,
        height=height,
        thumbnail=thumb,
        thumbnail_width=thumb_w,
        thumbnail_height=thumb_h,
        title=title,
        uploader=uploader,
        video_id=video_id,
        blurhash=blur,
    )


@dataclass
class SourceMeta:
    title: str | None = None
    uploader: str | None = None
    video_id: str | None = None


def _meta_from_info(info: dict[str, Any]) -> SourceMeta:
    uploader = info.get("uploader") or info.get("channel") or info.get("uploader_id")
    video_id = info.get("id")
    title = info.get("title")
    return SourceMeta(
        title=str(title).strip() if title else None,
        uploader=str(uploader).strip() if uploader else None,
        video_id=str(video_id).strip() if video_id else None,
    )


def _match_filter_for(cfg: DownloadConfig):
    max_seconds = int(cfg.max_duration_seconds or 0)

    def _match(info: dict[str, Any], *, incomplete: bool) -> str | None:
        if incomplete:
            return None
        duration = info.get("duration")
        if max_seconds and duration is not None:
            try:
                seconds = float(duration)
            except (TypeError, ValueError):
                seconds = 0
            if seconds > max_seconds:
                return _duration_limit_error(seconds, max_seconds)
        vcodec = str(info.get("vcodec") or "").lower()
        ext = str(info.get("ext") or "").lower()
        if vcodec == "none" or ext in {"jpg", "jpeg", "png", "webp", "gif"}:
            return "No video in that link"
        return None

    return _match


def _download_with_ytdlp(url: str, job_dir: Path, cfg: DownloadConfig) -> tuple[Path, SourceMeta]:
    import yt_dlp

    outtmpl = str(job_dir / "%(id)s.%(ext)s")
    fmt = resolve_ytdlp_format(cfg.format)
    merge_fmt = (cfg.merge_output_format or "mp4").strip() or "mp4"
    ydl_opts: dict[str, Any] = {
        "outtmpl": outtmpl,
        "format": fmt,
        "quiet": True,
        "no_warnings": True,
        "noplaylist": True,
        "restrictfilenames": True,
        "retries": 5,
        "fragment_retries": 5,
        "socket_timeout": 30,
        "concurrent_fragment_downloads": 1,
        "merge_output_format": merge_fmt,
        "match_filter": _match_filter_for(cfg),
    }

    cookies = Path(cfg.cookies_file)
    if cookies.is_file():
        ydl_opts["cookiefile"] = str(cookies)
        log.info("using cookies from %s", cookies)
    else:
        log.warning(
            "cookies file missing (%s); some sites may fail or return incomplete media",
            cookies,
        )

    log.info("yt-dlp download start url=%s job=%s format=%s", url, job_dir.name, fmt)
    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=True)
            if not info:
                raise DownloadError("yt-dlp returned no info")
            if "entries" in info and info["entries"]:
                info = next(e for e in info["entries"] if e)

            preferred: Path | None = None
            try:
                preferred = Path(ydl.prepare_filename(info))
            except Exception:
                preferred = None
            meta = _meta_from_info(info if isinstance(info, dict) else {})
    except DownloadError:
        raise
    except Exception as exc:
        text = str(exc)
        if "No video in that link" in text:
            raise DownloadError("No video in that link") from exc
        if "min limit" in text:
            # yt-dlp wraps the match_filter reason; keep the human sentence.
            for line in text.splitlines():
                if "min limit" in line:
                    cleaned = line.split(":", 1)[-1].strip() or line.strip()
                    raise DownloadError(cleaned) from exc
            raise DownloadError(text.splitlines()[-1].strip()) from exc
        raise

    return _pick_file(job_dir, preferred, merge_fmt), meta


async def download_url(url: str, cfg: DownloadConfig) -> MediaFile:
    """Download ``url`` (direct amplify MP4, otherwise yt-dlp) and return MediaFile."""
    work = Path(cfg.work_dir)
    work.mkdir(parents=True, exist_ok=True)
    job_dir = work / f"job_{uuid.uuid4().hex[:12]}"
    job_dir.mkdir(parents=True, exist_ok=True)
    direct = is_amplify_video_url(url)

    def _run() -> MediaFile:
        if direct:
            log.info("amplify mp4 download start url=%s job=%s", url, job_dir.name)
            path = fetch_direct_mp4(url, job_dir / "source.mp4")
            stem = Path(urlparse(url).path).stem or None
            meta = SourceMeta(video_id=stem)
        else:
            path, meta = _download_with_ytdlp(url, job_dir, cfg)
        return _finish_media(
            path,
            job_dir,
            cfg,
            title=meta.title,
            uploader=meta.uploader,
            video_id=meta.video_id,
        )

    try:
        return await asyncio.to_thread(_run)
    except DownloadError:
        # Caller logs with room context; just avoid leaking temp files.
        cleanup_job_dir(job_dir)
        raise
    except Exception as exc:
        cleanup_job_dir(job_dir)
        label = "direct mp4" if direct else "yt-dlp"
        raise DownloadError(f"{label} failed: {exc}") from exc
