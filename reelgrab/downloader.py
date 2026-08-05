"""In-process media download via yt-dlp (+ ffmpeg convert / probe / thumbnail)."""

from __future__ import annotations

import asyncio
import json
import logging
import mimetypes
import shutil
import subprocess
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from reelgrab.config import ConvertConfig, DownloadConfig

log = logging.getLogger("reelgrab.downloader")

VIDEO_EXTENSIONS = {".mp4", ".webm", ".mkv", ".mov", ".m4v"}
MIN_BYTES = 8_192  # reject empty / stub files


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
    """Build ffmpeg argv for WhatsApp/bridge-friendly re-encode."""
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

    # Audio: AAC mono/stereo, constant bitrate — WhatsApp-friendly
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


def convert_for_bridges(path: Path, job_dir: Path, conv: ConvertConfig) -> Path:
    """
    Re-encode ``path`` to a bridge-friendly MP4 (default: H.264 baseline + AAC).

    Returns path to the converted file (or original if conversion disabled / skipped).
    """
    if not conv.enabled:
        log.info("convert disabled — using source %s", path.name)
        return path

    if not shutil.which("ffmpeg"):
        log.warning("ffmpeg not found — cannot convert for bridges")
        return path

    if not conv.force and already_bridge_compatible(path, conv):
        log.info("source already bridge-compatible — skip convert (%s)", path.name)
        return path

    dest = job_dir / f"{path.stem}_bridge.mp4"
    args = build_convert_args(path, dest, conv)
    timeout = max(30, int(conv.timeout_seconds or 600))
    log.info("ffmpeg convert start in=%s out=%s", path.name, dest.name)
    try:
        proc = _run_cmd(args, timeout=timeout)
    except subprocess.TimeoutExpired as exc:
        raise DownloadError(f"ffmpeg convert timed out after {timeout}s") from exc

    if proc.returncode != 0 or not dest.is_file() or dest.stat().st_size < MIN_BYTES:
        err = (proc.stderr or proc.stdout or "")[:400]
        # Retry without audio if source has no audio stream
        log.warning("ffmpeg convert failed (will retry anullsrc): %s", err)
        dest.unlink(missing_ok=True)
        retry = build_convert_args(path, dest, conv)
        # Insert silent audio generation for video-only sources
        # Replace -i src with -i src -f lavfi -i anullsrc=channel_layout=stereo:sample_rate=44100
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
        proc = _run_cmd(retry, timeout=timeout)
        if proc.returncode != 0 or not dest.is_file() or dest.stat().st_size < MIN_BYTES:
            err2 = (proc.stderr or proc.stdout or "")[:400]
            raise DownloadError(f"ffmpeg convert failed: {err2 or 'unknown error'}")

    # Prefer converted; drop original to save space
    try:
        if path.resolve() != dest.resolve() and path.is_file():
            path.unlink(missing_ok=True)
    except OSError:
        pass

    log.info("ffmpeg convert done size=%d", dest.stat().st_size)
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


async def download_url(url: str, cfg: DownloadConfig) -> MediaFile:
    """Download ``url`` with yt-dlp, convert for bridges, return MediaFile."""
    work = Path(cfg.work_dir)
    work.mkdir(parents=True, exist_ok=True)
    job_dir = work / f"job_{uuid.uuid4().hex[:12]}"
    job_dir.mkdir(parents=True, exist_ok=True)

    def _run() -> MediaFile:
        import yt_dlp

        outtmpl = str(job_dir / "%(id)s.%(ext)s")
        fmt = (cfg.format or "bv*+ba/b").strip()
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
        }

        cookies = Path(cfg.cookies_file)
        if cookies.is_file():
            ydl_opts["cookiefile"] = str(cookies)
            log.info("using cookies from %s", cookies)
        else:
            log.warning(
                "cookies file missing (%s); Instagram and some sites may fail or "
                "return incomplete media",
                cookies,
            )

        log.info("yt-dlp download start url=%s job=%s", url, job_dir.name)
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

        path = _pick_file(job_dir, preferred, cfg.merge_output_format)
        size = path.stat().st_size
        if size < MIN_BYTES:
            raise DownloadError(f"downloaded file too small ({size} bytes): {path.name}")

        # Bridge/WhatsApp-friendly re-encode (configurable).
        conv = cfg.convert if isinstance(cfg.convert, ConvertConfig) else ConvertConfig()
        path = convert_for_bridges(path, job_dir, conv)

        mime = guess_mime(path)
        probe = probe_full(path)
        duration_ms, width, height = probe.duration_ms, probe.width, probe.height
        if mime.startswith("video/") and duration_ms is not None and duration_ms < 100:
            raise DownloadError(
                f"downloaded video is empty/too short ({duration_ms}ms): {path.name}"
            )

        size = path.stat().st_size
        thumb_path = job_dir / f"{path.stem}_thumb.jpg"
        thumb = make_thumbnail(path, thumb_path) if mime.startswith("video/") else None

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
        )

    try:
        return await asyncio.to_thread(_run)
    except DownloadError:
        raise
    except Exception as exc:
        raise DownloadError(f"yt-dlp failed: {exc}") from exc
