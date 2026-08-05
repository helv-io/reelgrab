"""Message pipeline: commands + URL download → post video."""

from __future__ import annotations

import asyncio
import logging
import time
import traceback
from pathlib import Path

from reelgrab.commands import (
    KNOWN_COMMANDS,
    effective_auto,
    effective_caption,
    effective_notify,
    force_prefixes,
    grab_urls_from_command,
    handle_command,
    is_admin,
    parse_command,
    room_allowed_effective,
)
from reelgrab.config import AppConfig, DownloadConfig
from reelgrab.downloader import download_url
from reelgrab.matrix_client import MatrixBot, MatrixGateway
from reelgrab.state import StateStore
from reelgrab.urls import canonicalize_url, find_matching_urls

log = logging.getLogger("reelgrab.handlers")


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


def _download_cfg(cfg: AppConfig) -> DownloadConfig:
    d = cfg.download
    return DownloadConfig(
        work_dir=str(cfg.work_dir_path),
        cookies_file=str(cfg.cookies_file_path),
        format=d.format,
        merge_output_format=d.merge_output_format,
        convert=d.convert,
    )


def extract_urls_from_message(body: str, cfg: AppConfig, *, auto: bool) -> list[str]:
    text = (body or "").strip()
    patterns = cfg.url_patterns
    urls: list[str] = []

    matched_prefix = False
    for prefix in force_prefixes(cfg):
        if text.startswith(prefix):
            matched_prefix = True
            rest = text[len(prefix) :].strip()
            urls = find_matching_urls(rest, patterns)
            if not urls and rest:
                token = rest.split()[0]
                if token.startswith("http"):
                    urls = [token]
            break
    if not matched_prefix and auto:
        urls = find_matching_urls(text, patterns)

    seen: set[str] = set()
    out: list[str] = []
    for u in urls:
        key = canonicalize_url(u)
        if key not in seen:
            seen.add(key)
            out.append(u)
    return out


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
) -> None:
    if sender == cfg.user_id or sender == bot.user_id:
        return

    text_strip = (body or "").strip()
    low = text_strip.lower()
    prefixes = force_prefixes(cfg)
    first = low.split()[0] if low.split() else ""
    if first in ("reelgrab", "grabbot", "bot", "rg") and len(low.split()) > 1:
        first = low.split()[1]
    looks_like_cmd = (
        is_direct
        or text_strip.startswith("!")
        or any(text_strip.startswith(p) for p in prefixes)
        or low.startswith(("reelgrab ", "grabbot ", "rg ", "bot "))
        or first in KNOWN_COMMANDS
    )
    parsed = parse_command(body, cfg) if looks_like_cmd else None
    if looks_like_cmd and parsed:
        log.info(
            "command room=%s sender=%s cmd=%s direct=%s",
            room_id,
            sender,
            parsed[0],
            is_direct,
        )
    elif looks_like_cmd and not parsed:
        log.debug("looks like cmd but unparsed body=%r", text_strip[:120])
    if parsed:
        cmd, args = parsed
        forced = grab_urls_from_command(cmd, args, body, cfg)
        if forced is not None:
            if not is_admin(sender, cfg):
                if is_direct:
                    await bot.send_text(
                        room_id,
                        "Not authorized. Add your MXID to bot.admin_users in config.yaml.",
                        reply_to_event_id=event_id,
                    )
                return
            if not room_allowed_effective(room_id, cfg, store) and not is_direct:
                await bot.send_text(
                    room_id,
                    "This room is not on the allow-list. DM me: allow " + room_id,
                    reply_to_event_id=event_id,
                )
                return
            await _queue_downloads(
                bot,
                cfg,
                store,
                room_id=room_id,
                event_id=event_id,
                urls=forced,
                dedupe=dedupe,
                sem=sem,
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
        )
        if handled:
            return

    if not room_allowed_effective(room_id, cfg, store):
        return
    if not effective_auto(cfg, store):
        return

    urls = extract_urls_from_message(body, cfg, auto=True)
    if not urls:
        return

    await _queue_downloads(
        bot,
        cfg,
        store,
        room_id=room_id,
        event_id=event_id,
        urls=urls,
        dedupe=dedupe,
        sem=sem,
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
) -> None:
    for url in urls:
        if dedupe and dedupe.already_done(room_id, url):
            log.info("skip deduped %s in %s", url, room_id)
            continue
        log.info("url in %s: %s", room_id, url)

        async def _job(u: str = url) -> None:
            if sem:
                async with sem:
                    await _process_one(bot, cfg, store, room_id, event_id, u, dedupe)
            else:
                await _process_one(bot, cfg, store, room_id, event_id, u, dedupe)

        asyncio.create_task(_job(), name=f"reelgrab:{room_id}:{url[:40]}")


async def _process_one(
    bot: MatrixGateway,
    cfg: AppConfig,
    store: StateStore,
    room_id: str,
    event_id: str,
    url: str,
    dedupe: DedupeCache | None,
) -> None:
    media_path: Path | None = None
    thumb_path: Path | None = None
    dl_cfg = _download_cfg(cfg)
    started = time.monotonic()
    # Quiet by default: only the m.video (or an error notice) hits the room.
    # No "Downloading…" / success chatter — useful when the goal is Matrix
    # playback, not re-relay to the original chat platform.

    try:
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
        if thumb_path and thumb_path.is_file():
            try:
                thumb_mxc = await bot.upload_media(thumb_path, mime="image/jpeg")
            except Exception:
                log.warning("thumbnail upload failed", exc_info=True)
                thumb_mxc = None

        mxc = await bot.upload_media(media.path, mime=media.mime)
        reply_to = event_id if cfg.bot.reply_to_original else None
        # Empty caption → filename as m.video body (Matrix requires a body).
        caption = (effective_caption(cfg, store) or "").strip() or media.path.name
        await bot.send_video(
            room_id,
            mxc,
            media.path,
            reply_to_event_id=reply_to,
            caption=caption,
            mime=media.mime,
            size=media.size,
            duration_ms=media.duration_ms,
            width=media.width,
            height=media.height,
            thumbnail_mxc=thumb_mxc,
            thumbnail_path=thumb_path,
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
    except Exception as exc:
        log.exception("failed for %s: %s", url, exc)
        if effective_notify(cfg, store):
            try:
                stack = traceback.format_exc()
                body = f"Failed to grab media:\n{exc}\n\n{stack}".strip()
                # Keep notices readable in clients / bridges.
                if len(body) > 3500:
                    body = body[:3490] + "\n…"
                await bot.send_text(
                    room_id,
                    body,
                    reply_to_event_id=event_id if cfg.bot.reply_to_original else None,
                )
            except Exception:
                log.exception("also failed to send error notice")
    finally:
        if media_path is not None:
            cleanup_path(media_path)
        if thumb_path is not None:
            cleanup_path(thumb_path)


def cleanup_path(path: Path) -> None:
    try:
        path.unlink(missing_ok=True)
        parent = path.parent
        if parent.name.startswith("job_") and parent.is_dir():
            # Remove leftover job files (thumb, partials) then the dir.
            for child in list(parent.iterdir()):
                try:
                    child.unlink(missing_ok=True)
                except OSError:
                    pass
            try:
                parent.rmdir()
            except OSError:
                pass
    except OSError:
        log.warning("could not remove temp file %s", path)


async def run_bot(cfg: AppConfig) -> None:
    from reelgrab.appservice import AppserviceServer

    bot = MatrixBot(cfg)
    store = StateStore(cfg.state_file_path)
    dedupe = DedupeCache(cfg.bot.dedupe_ttl_seconds)
    sem = asyncio.Semaphore(max(1, cfg.bot.max_concurrent))
    appservice = AppserviceServer(cfg)

    async def _on_message(
        *,
        room_id: str,
        event_id: str,
        sender: str,
        body: str,
        is_direct: bool = False,
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
            dedupe=dedupe,
            sem=sem,
        )

    bot.on_text_message(_on_message)
    appservice.on_events(bot.handle_appservice_events)

    log.info(
        "starting user=%s downloader=yt-dlp auto=%s admins=%s data=%s as_url=%s",
        cfg.user_id,
        effective_auto(cfg, store),
        cfg.bot.admin_users or "(none)",
        cfg.data_dir,
        cfg.appservice.address,
    )

    try:
        await bot.start()
        await appservice.start()
        log.info(
            "ready — listening for appservice transactions at %s",
            cfg.appservice.address,
        )
        await asyncio.Event().wait()
    finally:
        await appservice.stop()
        await bot.close()
