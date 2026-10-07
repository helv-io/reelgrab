"""Message pipeline: commands + URL download → post video."""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Coroutine
from pathlib import Path
from typing import Any

from reelgrab.captions import build_caption, caption_html, media_filename
from reelgrab.commands import (
    configured_prefix,
    effective_auto,
    effective_caption,
    effective_notify,
    grab_urls_from_command,
    handle_command,
    is_admin,
    message_has_reel_prefix,
    parse_command,
    room_allowed_effective,
    text_after_reel_prefix,
)
from reelgrab.config import AppConfig, DownloadConfig
from reelgrab.downloader import DownloadError, MediaFile, cleanup_media_path, download_url
from reelgrab.matrix_client import (
    REACT_DONE,
    REACT_FAILED,
    REACT_WORKING,
    MatrixBot,
    MatrixGateway,
)
from reelgrab.media_cache import CachedMedia, MediaCache
from reelgrab.messages import strip_reply_fallback
from reelgrab.state import StateStore
from reelgrab.urls import (
    canonicalize_url,
    find_matching_urls,
    is_http_url,
    is_matching_url,
    normalize_url,
)

log = logging.getLogger("reelgrab.handlers")

# Strong refs so fire-and-forget download tasks are not GC'd mid-flight.
_background_tasks: set[asyncio.Task[Any]] = set()


class DedupeCache:
    """In-memory (room, url) → expiry for avoiding re-downloads."""

    def __init__(self, ttl_seconds: int) -> None:
        self.ttl = max(0, ttl_seconds)
        self._seen: dict[tuple[str, str], float] = {}

    def _purge(self) -> None:
        now = time.monotonic()
        expired = [k for k, exp in self._seen.items() if exp <= now]
        for k in expired:
            del self._seen[k]

    def already_done(self, room_id: str, url: str) -> bool:
        if self.ttl <= 0:
            return False
        self._purge()
        key = (room_id, canonicalize_url(url))
        return key in self._seen

    def mark(self, room_id: str, url: str) -> None:
        if self.ttl <= 0:
            return
        key = (room_id, canonicalize_url(url))
        self._seen[key] = time.monotonic() + self.ttl


def _download_cfg(cfg: AppConfig, *, max_upload_bytes: int | None = None) -> DownloadConfig:
    d = cfg.download
    configured = int(d.max_upload_bytes or 0)
    limit = configured
    if max_upload_bytes:
        limit = min(configured, int(max_upload_bytes)) if configured else int(max_upload_bytes)
    return DownloadConfig(
        work_dir=str(cfg.work_dir_path),
        cookies_file=str(cfg.cookies_file_path),
        format=d.format,
        merge_output_format=d.merge_output_format,
        max_duration_seconds=d.max_duration_seconds,
        max_upload_bytes=limit,
        convert=d.convert,
    )


def extract_urls_from_message(body: str, cfg: AppConfig, *, auto: bool) -> list[str]:
    text = (body or "").strip()
    patterns = cfg.url_patterns
    urls: list[str] = []

    rest = text_after_reel_prefix(text, cfg)
    if rest is not None:
        urls = find_matching_urls(rest, patterns) if rest else []
        if not urls and rest:
            token = normalize_url(rest.split()[0])
            if is_http_url(token):
                urls = [token]
        # URL may sit before the prefix: "https://... !reel"
        if not urls:
            urls = find_matching_urls(text, patterns)
    elif auto:
        urls = find_matching_urls(text, patterns)

    seen: set[str] = set()
    out: list[str] = []
    for u in urls:
        key = canonicalize_url(u)
        if key not in seen:
            seen.add(key)
            out.append(u)
    return out


def _spawn_background(coro: Coroutine[Any, Any, None], *, name: str) -> None:
    task = asyncio.create_task(coro, name=name)
    _background_tasks.add(task)
    task.add_done_callback(_background_tasks.discard)


async def handle_message(
    bot: MatrixGateway,
    cfg: AppConfig,
    store: StateStore,
    *,
    room_id: str,
    event_id: str,
    sender: str,
    body: str,
    is_direct: bool = False,
    dedupe: DedupeCache | None = None,
    sem: asyncio.Semaphore | None = None,
    cache: MediaCache | None = None,
    thread_root_event_id: str | None = None,
    is_reply: bool = False,
) -> None:
    if sender == cfg.user_id or sender == bot.user_id:
        return

    text_strip = (body or "").strip()
    if is_reply:
        text_strip = strip_reply_fallback(text_strip)
    # Listen gate: command prefix, or a supported media URL. Everything else is silence.
    has_reel = message_has_reel_prefix(text_strip, cfg)
    matched_urls = find_matching_urls(text_strip, cfg.url_patterns)
    if not has_reel and not matched_urls:
        return

    if has_reel:
        parsed = parse_command(text_strip, cfg)
        if parsed:
            log.info(
                "command room=%s sender=%s cmd=%s direct=%s",
                room_id,
                sender,
                parsed[0],
                is_direct,
            )
            cmd, args = parsed
            forced = grab_urls_from_command(cmd, args, text_strip, cfg)
            if forced is not None:
                await _dispatch_grab(
                    bot,
                    cfg,
                    store,
                    room_id=room_id,
                    event_id=event_id,
                    sender=sender,
                    is_direct=is_direct,
                    urls=forced,
                    dedupe=dedupe,
                    sem=sem,
                    cache=cache,
                    thread_root_event_id=thread_root_event_id,
                )
                return

            handled = await handle_command(
                bot,
                cfg,
                store,
                room_id=room_id,
                event_id=event_id,
                sender=sender,
                cmd=cmd,
                args=args,
                is_direct=is_direct,
                thread_root_event_id=thread_root_event_id,
            )
            if handled:
                return
        else:
            log.debug("command prefix present but unparsed")

    if not matched_urls:
        return
    if not room_allowed_effective(room_id, cfg, store):
        return
    if not effective_auto(cfg, store):
        return

    urls = matched_urls

    await _queue_downloads(
        bot,
        cfg,
        store,
        room_id=room_id,
        event_id=event_id,
        urls=urls,
        dedupe=dedupe,
        sem=sem,
        cache=cache,
        thread_root_event_id=thread_root_event_id,
    )


def _thread_kwargs(
    *,
    reply_to: str | None,
    thread_root_event_id: str | None,
) -> dict[str, Any]:
    return {
        "reply_to_event_id": reply_to,
        "thread_root_event_id": thread_root_event_id,
    }


def human_failure(exc: BaseException) -> str:
    """One room-safe line. Tracebacks stay in the log."""
    text = str(exc).strip().splitlines()[0] if str(exc).strip() else exc.__class__.__name__
    lowered = text.lower()
    if "413" in text or "m_too_large" in lowered or "upload limit" in lowered:
        text = "the video is over the homeserver upload limit"
    elif "no video" in lowered:
        text = "no video in that link"
    if len(text) > 240:
        text = text[:239].rstrip() + "…"
    return f"Failed to grab media: {text}"


def _reply_target(cfg: AppConfig, event_id: str) -> str | None:
    return event_id if cfg.bot.reply_to_original else None


async def _react(bot: MatrixGateway, room_id: str, event_id: str, key: str) -> str | None:
    fn = getattr(bot, "send_reaction", None)
    if fn is None or not event_id:
        return None
    try:
        sent = await fn(room_id, event_id, key)
        return str(sent) if sent else ""
    except Exception:
        log.debug("reaction %s failed in %s", key, room_id, exc_info=True)
        return None


async def _clear_reaction(bot: MatrixGateway, room_id: str, reaction_event_id: str | None) -> None:
    if not reaction_event_id:
        return
    fn = getattr(bot, "redact_event", None)
    if fn is None:
        return
    try:
        await fn(room_id, reaction_event_id)
    except Exception:
        log.debug("redact reaction failed in %s", room_id, exc_info=True)


def _caption_for(
    cfg: AppConfig,
    store: StateStore,
    *,
    url: str,
    filename: str,
    uploader: str | None,
    title: str | None,
) -> tuple[str, str | None]:
    custom = effective_caption(cfg, store)
    body = build_caption(
        uploader=uploader,
        title=title,
        source_url=url,
        filename=filename,
        custom=custom,
    )
    html_body = caption_html(body, url) if body != filename else None
    return body, html_body


def _filename_for(media_name: str | None, video_id: str | None, mime: str) -> str:
    ext = ".mp4"
    if mime == "video/webm":
        ext = ".webm"
    elif media_name and "." in Path(media_name).name:
        ext = "." + Path(media_name).name.rsplit(".", 1)[-1].lower()
    if video_id:
        return media_filename(video_id, ext)
    stem = Path(media_name or "video").stem
    if stem.endswith("_bridge"):
        stem = stem[: -len("_bridge")]
    return media_filename(stem or "video", ext)


async def _send_cached(
    bot: MatrixGateway,
    cfg: AppConfig,
    store: StateStore,
    *,
    room_id: str,
    event_id: str,
    url: str,
    item: CachedMedia,
    thread_root_event_id: str | None,
) -> None:
    filename = item.filename or _filename_for(None, None, item.mime)
    caption, html_body = _caption_for(
        cfg,
        store,
        url=url,
        filename=filename,
        uploader=item.uploader,
        title=item.title,
    )
    await bot.send_video(
        room_id,
        item.mxc,
        Path(filename),
        caption=caption,
        filename=filename,
        formatted_body=html_body,
        mime=item.mime,
        size=item.size,
        duration_ms=item.duration_ms,
        width=item.width,
        height=item.height,
        thumbnail_mxc=item.thumbnail_mxc,
        thumbnail_width=item.thumbnail_width,
        thumbnail_height=item.thumbnail_height,
        thumbnail_size=item.thumbnail_size,
        blurhash=item.blurhash,
        **_thread_kwargs(
            reply_to=_reply_target(cfg, event_id),
            thread_root_event_id=thread_root_event_id,
        ),
    )


async def _dispatch_grab(
    bot: MatrixGateway,
    cfg: AppConfig,
    store: StateStore,
    *,
    room_id: str,
    event_id: str,
    sender: str,
    is_direct: bool,
    urls: list[str],
    dedupe: DedupeCache | None,
    sem: asyncio.Semaphore | None,
    cache: MediaCache | None = None,
    thread_root_event_id: str | None = None,
) -> None:
    """Handle ``!reel <url>``.

    Supported media URLs download for anyone in an allowed room (same as a
    paste). Any other http(s) URL still requires an admin.
    """
    prefix = configured_prefix(cfg)
    kw = _thread_kwargs(
        reply_to=event_id,
        thread_root_event_id=thread_root_event_id,
    )
    if not urls:
        if is_direct:
            await bot.send_text(room_id, f"Usage: {prefix} <url>", **kw)
        return

    patterns = cfg.url_patterns
    supported = [u for u in urls if is_matching_url(u, patterns)]
    other = [u for u in urls if u not in supported]
    if other and not is_admin(sender, cfg):
        if not supported:
            if is_direct:
                await bot.send_text(
                    room_id,
                    "Not authorized. Add your MXID to bot.admin_users in config.yaml.",
                    **kw,
                )
            return
        other = []

    chosen = supported + other
    if not room_allowed_effective(room_id, cfg, store) and not is_direct:
        await bot.send_text(
            room_id,
            f"This room is not on the allow-list. DM me: {prefix} allow {room_id}",
            **kw,
        )
        return
    await _queue_downloads(
        bot,
        cfg,
        store,
        room_id=room_id,
        event_id=event_id,
        urls=chosen,
        dedupe=dedupe,
        sem=sem,
        cache=cache,
        thread_root_event_id=thread_root_event_id,
    )


async def _queue_downloads(
    bot: MatrixGateway,
    cfg: AppConfig,
    store: StateStore,
    *,
    room_id: str,
    event_id: str,
    urls: list[str],
    dedupe: DedupeCache | None,
    sem: asyncio.Semaphore | None,
    cache: MediaCache | None = None,
    thread_root_event_id: str | None = None,
) -> None:
    for url in urls:
        if dedupe and dedupe.already_done(room_id, url):
            log.info("skip duplicate url in %s", room_id)
            continue
        log.info("url in %s: %s", room_id, url)

        async def _job(u: str = url) -> None:
            if sem:
                async with sem:
                    await _process_one(
                        bot,
                        cfg,
                        store,
                        room_id,
                        event_id,
                        u,
                        dedupe,
                        cache,
                        thread_root_event_id,
                    )
            else:
                await _process_one(
                    bot,
                    cfg,
                    store,
                    room_id,
                    event_id,
                    u,
                    dedupe,
                    cache,
                    thread_root_event_id,
                )

        _spawn_background(_job(), name=f"reelgrab:{room_id}:{url[:40]}")


async def _notify_failure(
    bot: MatrixGateway,
    cfg: AppConfig,
    store: StateStore,
    *,
    room_id: str,
    event_id: str,
    url: str,
    exc: BaseException,
    thread_root_event_id: str | None = None,
) -> None:
    if not effective_notify(cfg, store):
        return
    try:
        await bot.send_text(
            room_id,
            human_failure(exc),
            **_thread_kwargs(
                reply_to=_reply_target(cfg, event_id),
                thread_root_event_id=thread_root_event_id,
            ),
        )
    except Exception:
        log.exception("also failed to send error notice for %s", url)


async def _remember(
    cache: MediaCache | None,
    url: str,
    *,
    mxc: str,
    media: MediaFile,
    filename: str,
    thumbnail_mxc: str | None,
    thumbnail_size: int | None,
) -> None:
    if cache is None or not mxc:
        return
    cache.put(
        url,
        CachedMedia(
            url_key="",
            source_url=url,
            mxc=mxc,
            mime=media.mime,
            size=media.size,
            duration_ms=media.duration_ms,
            width=media.width,
            height=media.height,
            filename=filename,
            uploader=media.uploader,
            title=media.title,
            thumbnail_mxc=thumbnail_mxc,
            thumbnail_width=media.thumbnail_width,
            thumbnail_height=media.thumbnail_height,
            thumbnail_size=thumbnail_size,
            blurhash=media.blurhash,
        ),
    )


async def _process_one(
    bot: MatrixGateway,
    cfg: AppConfig,
    store: StateStore,
    room_id: str,
    event_id: str,
    url: str,
    dedupe: DedupeCache | None,
    cache: MediaCache | None = None,
    thread_root_event_id: str | None = None,
) -> None:
    media_path: Path | None = None
    thumb_path: Path | None = None
    started = time.monotonic()
    working = await _react(bot, room_id, event_id, REACT_WORKING)
    # Quiet by default: a reaction, then the m.video (or one short failure line).

    try:
        cached = cache.get(url) if cache is not None else None
        if cached is not None:
            await _send_cached(
                bot,
                cfg,
                store,
                room_id=room_id,
                event_id=event_id,
                url=url,
                item=cached,
                thread_root_event_id=thread_root_event_id,
            )
            if dedupe:
                dedupe.mark(room_id, url)
            log.info("posted cached media for %s -> %s", url, room_id)
            await _clear_reaction(bot, room_id, working)
            await _react(bot, room_id, event_id, REACT_DONE)
            return

        limit = getattr(bot, "max_upload_bytes", None)
        dl_cfg = _download_cfg(cfg, max_upload_bytes=limit if isinstance(limit, int) else None)
        media = await download_url(url, dl_cfg)
        media_path = media.path
        thumb_path = media.thumbnail
        elapsed = time.monotonic() - started
        log.info(
            "download ready url=%s size=%d elapsed=%.1fs duration_ms=%s",
            url,
            media.size,
            elapsed,
            media.duration_ms,
        )

        thumb_mxc: str | None = None
        thumb_size: int | None = None
        if thumb_path and thumb_path.is_file():
            thumb_size = thumb_path.stat().st_size
            try:
                thumb_mxc = await bot.upload_media(thumb_path, mime="image/jpeg")
            except Exception:
                log.warning("thumbnail upload failed url=%s", url, exc_info=True)
                thumb_mxc = None

        mxc = await bot.upload_media(media.path, mime=media.mime)
        filename = _filename_for(media.path.name, media.video_id, media.mime)
        caption, html_body = _caption_for(
            cfg,
            store,
            url=url,
            filename=filename,
            uploader=media.uploader,
            title=media.title,
        )
        await bot.send_video(
            room_id,
            mxc,
            media.path,
            caption=caption,
            filename=filename,
            formatted_body=html_body,
            mime=media.mime,
            size=media.size,
            duration_ms=media.duration_ms,
            width=media.width,
            height=media.height,
            thumbnail_mxc=thumb_mxc,
            thumbnail_path=thumb_path,
            thumbnail_width=media.thumbnail_width,
            thumbnail_height=media.thumbnail_height,
            thumbnail_size=thumb_size,
            blurhash=media.blurhash,
            **_thread_kwargs(
                reply_to=_reply_target(cfg, event_id),
                thread_root_event_id=thread_root_event_id,
            ),
        )
        await _remember(
            cache,
            url,
            mxc=mxc,
            media=media,
            filename=filename,
            thumbnail_mxc=thumb_mxc,
            thumbnail_size=thumb_size,
        )
        if dedupe:
            dedupe.mark(room_id, url)
        log.info(
            "posted video for %s -> %s size=%d elapsed=%.1fs",
            url,
            room_id,
            media.size,
            time.monotonic() - started,
        )
        await _clear_reaction(bot, room_id, working)
        await _react(bot, room_id, event_id, REACT_DONE)
    except DownloadError as exc:
        log.error(
            "download error url=%s room=%s elapsed=%.1fs: %s",
            url,
            room_id,
            time.monotonic() - started,
            exc,
        )
        await _clear_reaction(bot, room_id, working)
        await _react(bot, room_id, event_id, REACT_FAILED)
        await _notify_failure(
            bot,
            cfg,
            store,
            room_id=room_id,
            event_id=event_id,
            url=url,
            exc=exc,
            thread_root_event_id=thread_root_event_id,
        )
    except Exception as exc:
        log.exception(
            "unexpected failure url=%s room=%s elapsed=%.1fs: %s",
            url,
            room_id,
            time.monotonic() - started,
            exc,
        )
        await _clear_reaction(bot, room_id, working)
        await _react(bot, room_id, event_id, REACT_FAILED)
        await _notify_failure(
            bot,
            cfg,
            store,
            room_id=room_id,
            event_id=event_id,
            url=url,
            exc=exc,
            thread_root_event_id=thread_root_event_id,
        )
    finally:
        # Video and thumbnail share a job_* dir; one cleanup removes both.
        if media_path is not None:
            cleanup_media_path(media_path)
        elif thumb_path is not None:
            cleanup_media_path(thumb_path)


# Back-compat alias for tests / importers.
cleanup_path = cleanup_media_path


async def run_bot(cfg: AppConfig) -> None:
    from reelgrab.appservice import AppserviceServer
    from reelgrab.commands import ytdlp_version

    bot = MatrixBot(cfg)
    store = StateStore(cfg.state_file_path)
    dedupe = DedupeCache(cfg.bot.dedupe_ttl_seconds)
    cache = MediaCache(cfg.media_cache_path)
    sem = asyncio.Semaphore(max(1, cfg.bot.max_concurrent))
    appservice = AppserviceServer(cfg)
    appservice.set_ready_check(lambda: bot.ready)

    async def _on_message(
        *,
        room_id: str,
        event_id: str,
        sender: str,
        body: str,
        is_direct: bool = False,
        is_reply: bool = False,
        thread_root_event_id: str | None = None,
    ) -> None:
        await handle_message(
            bot,
            cfg,
            store,
            room_id=room_id,
            event_id=event_id,
            sender=sender,
            body=body,
            is_direct=is_direct,
            is_reply=is_reply,
            thread_root_event_id=thread_root_event_id,
            dedupe=dedupe,
            sem=sem,
            cache=cache,
        )

    bot.on_text_message(_on_message)
    appservice.on_events(bot.handle_appservice_events)

    log.info(
        "starting user=%s downloader=yt-dlp %s auto=%s admins=%s data=%s as_url=%s",
        cfg.user_id,
        ytdlp_version(),
        effective_auto(cfg, store),
        cfg.bot.admin_users or "(none)",
        cfg.data_dir,
        cfg.appservice.address,
    )

    try:
        # Listen first so /health can report "not ready" while the homeserver boots.
        await appservice.start()
        await bot.start()
        log.info(
            "ready — listening for appservice transactions at %s",
            cfg.appservice.address,
        )
        await asyncio.Event().wait()
    finally:
        await appservice.stop()
        await bot.close()
