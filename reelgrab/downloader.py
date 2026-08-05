"""In-process media download via yt-dlp (+ ffmpeg for probe/thumbnail)."""

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

from reelgrab.config import DownloadConfig

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


def probe_media(path: Path) -> tuple[int | None, int | None, int | None]:
    """Return (duration_ms, width, height) via ffprobe, or Nones if unavailable."""
    if not shutil.which("ffprobe"):
        return None, None, None
    try:
        proc = _run_cmd(
            [
                "ffprobe",
                "-v",
                "error",
                "-select_streams",
                "v:0",
                "-show_entries",
                "stream=width,height:format=duration",
                "-of",
                "json",
                str(path),
            ],
            timeout=60,
        )
        if proc.returncode != 0:
            log.warning("ffprobe failed: %s", (proc.stderr or "")[:200])
            return None, None, None
        data = json.loads(proc.stdout or "{}")
        duration_ms: int | None = None
        fmt = data.get("format") or {}
        if fmt.get("duration") is not None:
            try:
                duration_ms = int(float(fmt["duration"]) * 1000)
            except (TypeError, ValueError):
                duration_ms = None
        width = height = None
        streams = data.get("streams") or []
        if streams:
            try:
                width = int(streams[0].get("width") or 0) or None
                height = int(streams[0].get("height") or 0) or None
            except (TypeError, ValueError):
                width = height = None
        return duration_ms, width, height
    except Exception as exc:
        log.warning("ffprobe error for %s: %s", path, exc)
        return None, None, None


def make_thumbnail(path: Path, dest: Path) -> Path | None:
    """Extract a JPEG frame near 1s (or start) for Matrix / bridge clients."""
    if not shutil.which("ffmpeg"):
        return None
    try:
        dest.parent.mkdir(parents=True, exist_ok=True)
        # Scale to max 640 on the long edge; keep aspect ratio.
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
            # Retry from t=0 (very short clips)
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
        and f.stat().st_size >= MIN_BYTES
    ]
    # Prefer real video containers over thumbnails / json
    videos = [f for f in files if f.suffix.lower() in VIDEO_EXTENSIONS]
    pool = videos or files
    if not pool:
        raise DownloadError("download finished but no usable file found")
    pool.sort(key=lambda p: p.stat().st_size, reverse=True)
    return pool[0]


async def download_url(url: str, cfg: DownloadConfig) -> MediaFile:
    """Download ``url`` with yt-dlp; return validated MediaFile (with optional thumb)."""
    work = Path(cfg.work_dir)
    work.mkdir(parents=True, exist_ok=True)
    job_dir = work / f"job_{uuid.uuid4().hex[:12]}"
    job_dir.mkdir(parents=True, exist_ok=True)

    def _run() -> MediaFile:
        import yt_dlp

        outtmpl = str(job_dir / "%(id)s.%(ext)s")
        fmt = (cfg.format or "bv*+ba/b").strip()
        merge_fmt = (cfg.merge_output_format or "mp4").strip() or "mp4"
        ydl_opts: dict = {
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
            # Mux separate video+audio streams into one container (needs ffmpeg).
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

        # Basic container sanity: reject tiny "success" stubs
        mime = guess_mime(path)
        duration_ms, width, height = probe_media(path)
        if mime.startswith("video/") and duration_ms is not None and duration_ms < 100:
            raise DownloadError(
                f"downloaded video is empty/too short ({duration_ms}ms): {path.name}"
            )

        thumb_path = job_dir / f"{path.stem}_thumb.jpg"
        thumb = make_thumbnail(path, thumb_path) if mime.startswith("video/") else None

        log.info(
            "yt-dlp done file=%s size=%d duration_ms=%s %sx%s thumb=%s",
            path.name,
            size,
            duration_ms,
            width,
            height,
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
