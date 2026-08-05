"""
Download backends (strategy pattern).

- Downloader protocol: single async download(url) -> Path
- YtDlpDownloader / MetubeDownloader implement it
- download_url() factory dispatches by config.backend
"""

from __future__ import annotations

import asyncio
import logging
import mimetypes
import time
import uuid
from pathlib import Path
from typing import Protocol, runtime_checkable

import aiohttp

from reelgrab.config import DownloadConfig

log = logging.getLogger("reelgrab.downloader")

VIDEO_EXTENSIONS = {".mp4", ".webm", ".mkv", ".mov", ".m4v"}


class DownloadError(Exception):
    """Raised when a download cannot be completed."""


@runtime_checkable
class Downloader(Protocol):
    """SOLID: depend on this abstraction, not a concrete backend."""

    async def download(self, url: str) -> Path:
        """Download media for url; return path to a local file."""
        ...


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


def get_downloader(cfg: DownloadConfig) -> Downloader:
    """Factory: pick backend from config."""
    backend = (cfg.backend or "ytdlp").lower().strip()
    if backend == "metube":
        return MetubeDownloader(cfg)
    if backend == "ytdlp":
        return YtDlpDownloader(cfg)
    raise DownloadError(f"unknown download backend: {cfg.backend}")


async def download_url(url: str, cfg: DownloadConfig) -> Path:
    """Convenience entry used by handlers."""
    work = Path(cfg.work_dir)
    work.mkdir(parents=True, exist_ok=True)
    return await get_downloader(cfg).download(url)


class YtDlpDownloader:
    """Download with yt-dlp into an isolated job directory."""

    def __init__(self, cfg: DownloadConfig) -> None:
        self.cfg = cfg

    async def download(self, url: str) -> Path:
        import yt_dlp

        cfg = self.cfg
        work = Path(cfg.work_dir)
        work.mkdir(parents=True, exist_ok=True)
        job_dir = work / f"job_{uuid.uuid4().hex[:12]}"
        job_dir.mkdir(parents=True, exist_ok=True)
        outtmpl = str(job_dir / "%(id)s.%(ext)s")

        ydl_opts: dict = {
            "outtmpl": outtmpl,
            "format": cfg.format,
            "quiet": True,
            "no_warnings": True,
            "noplaylist": True,
            "restrictfilenames": True,
            "retries": 3,
            "fragment_retries": 3,
        }
        if cfg.merge_output_format:
            ydl_opts["merge_output_format"] = cfg.merge_output_format

        cookies = Path(cfg.cookies_file)
        if cookies.is_file():
            ydl_opts["cookiefile"] = str(cookies)
            log.debug("using cookies from %s", cookies)
        else:
            log.warning(
                "cookies file missing (%s); some sites may fail without it",
                cookies,
            )

        def _run() -> Path:
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                info = ydl.extract_info(url, download=True)
                if not info:
                    raise DownloadError("yt-dlp returned no info")

                if "entries" in info and info["entries"]:
                    info = next(e for e in info["entries"] if e)

                path: Path | None = None
                try:
                    prepared = ydl.prepare_filename(info)
                    path = Path(prepared)
                    if cfg.merge_output_format and not path.is_file():
                        alt = path.with_suffix(f".{cfg.merge_output_format}")
                        if alt.is_file():
                            path = alt
                except Exception:
                    path = None

                if path and path.is_file():
                    return path

                files = [
                    f
                    for f in job_dir.iterdir()
                    if f.is_file()
                    and not f.name.endswith(".part")
                    and not f.name.endswith(".ytdl")
                ]
                files.sort(key=lambda p: p.stat().st_mtime, reverse=True)
                for f in files:
                    if f.suffix.lower() in VIDEO_EXTENSIONS or f.stat().st_size > 0:
                        return f
                raise DownloadError("download finished but file not found")

        try:
            return await asyncio.to_thread(_run)
        except DownloadError:
            raise
        except Exception as exc:
            raise DownloadError(f"yt-dlp failed: {exc}") from exc


class MetubeDownloader:
    """Queue on MeTube (internal network), poll history, copy finished file."""

    def __init__(self, cfg: DownloadConfig) -> None:
        self.cfg = cfg

    async def download(self, url: str) -> Path:
        cfg = self.cfg
        work = Path(cfg.work_dir)
        work.mkdir(parents=True, exist_ok=True)
        base = cfg.metube_url.rstrip("/")
        download_root = Path(cfg.metube_download_dir)

        async with aiohttp.ClientSession() as session:
            payload = {"url": url, "quality": "best", "format": "mp4"}
            try:
                async with session.post(f"{base}/add", json=payload) as resp:
                    body = await resp.json(content_type=None)
                    if resp.status >= 400 or body.get("status") not in (None, "ok"):
                        raise DownloadError(
                            f"MeTube /add failed status={resp.status} body={body}"
                        )
            except DownloadError:
                raise
            except Exception as exc:
                raise DownloadError(f"MeTube /add error: {exc}") from exc

            deadline = time.monotonic() + cfg.metube_timeout_seconds
            last_error: str | None = None

            while time.monotonic() < deadline:
                await asyncio.sleep(cfg.metube_poll_seconds)
                try:
                    async with session.get(f"{base}/history") as resp:
                        hist = await resp.json(content_type=None)
                except Exception as exc:
                    log.warning("MeTube history poll failed: %s", exc)
                    continue

                done = hist.get("done") or []
                queue = hist.get("queue") or []
                downloading = hist.get("downloading") or []

                for item in done:
                    item_url = item.get("url") or ""
                    if not urls_related(url, item_url):
                        continue
                    if item.get("error"):
                        raise DownloadError(f"MeTube error: {item['error']}")
                    filename = item.get("filename")
                    if not filename:
                        continue
                    candidates = [
                        download_root / filename,
                        download_root / Path(filename).name,
                        work / Path(filename).name,
                    ]
                    for cand in candidates:
                        if cand.is_file() and cand.stat().st_size > 0:
                            dest = work / f"metube_{uuid.uuid4().hex[:8]}_{cand.name}"
                            dest.write_bytes(cand.read_bytes())
                            return dest
                    last_error = f"finished but file missing: {filename}"

                active = list(queue) + list(downloading)
                still = any(urls_related(url, (i.get("url") or "")) for i in active)
                if still:
                    continue
                if last_error:
                    break

            raise DownloadError(
                last_error
                or f"MeTube timed out after {cfg.metube_timeout_seconds}s for {url}"
            )


def urls_related(a: str, b: str) -> bool:
    if not a or not b:
        return False
    if a == b:
        return True
    # Align with canonicalize_url: strip www, query, trailing slash
    from reelgrab.urls import canonicalize_url

    ca, cb = canonicalize_url(a), canonicalize_url(b)
    if ca == cb:
        return True
    return ca in cb or cb in ca
