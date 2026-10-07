"""Admin / DM command handling."""

from __future__ import annotations

import html
import logging
import re
import shlex
from typing import TYPE_CHECKING, Any

from reelgrab.config import AppConfig
from reelgrab.state import StateStore
from reelgrab.urls import is_http_url, normalize_url

if TYPE_CHECKING:
    from reelgrab.matrix_client import MatrixGateway

log = logging.getLogger("reelgrab.commands")

# Default listen / command token. ``bot.command_prefix`` overrides this.
REEL_PREFIX = "!reel"

# Public vs admin after alias normalization (see parse_command).
PUBLIC_COMMANDS = frozenset({"help", "ping", "whoami"})
ADMIN_COMMANDS = frozenset(
    {
        "status",
        "rooms",
        "allow",
        "deny",
        "auto",
        "notify",
        "caption",
        "room",
        "grab",
    }
)

# Fixed-width help so Matrix clients can show aligned columns inside <pre>.
_HELP_ROWS: list[tuple[str, str]] = [
    ("!reel help", "Show this help"),
    ("!reel ping", "Liveness check"),
    ("!reel status", "Config, cookies, identity"),
    ("!reel whoami", "Your MXID as seen by the bot"),
    ("!reel rooms", "Joined room IDs"),
    ("!reel allow <room_id>", "Add room to allow-list"),
    ("!reel deny <room_id>", "Remove room from allow-list"),
    ("!reel allow clear", "Clear allow-list (all rooms)"),
    ("!reel auto on|off", "Auto-download matching links"),
    ("!reel notify on|off", "Short failure line in the room"),
    ("!reel caption <text>", "Caption override (caption clear = metadata)"),
    ("!reel room", "This room's auto, notify, and caption"),
    ("!reel room auto on|off|default", "Auto-download override for this room"),
    ("!reel room notify on|off|default", "Failure line override for this room"),
    ("!reel room caption <text>", "Caption override for this room"),
    ("!reel <url>", "Download one URL now"),
]


def format_help_text(prefix: str = REEL_PREFIX) -> tuple[str, str]:
    """Return (plain body, html formatted_body) with aligned columns."""
    rows = [(cmd.replace("!reel", prefix, 1), desc) for cmd, desc in _HELP_ROWS]
    # The last row is ``!reel <url>``; replace() above only swaps the token once.
    cmd_w = max(len(c) for c, _ in rows)
    lines = [
        "reelgrab: short-form video grabber",
        f"Commands need the {prefix} prefix. A supported video URL is grabbed on its own.",
        "Anything else is ignored.",
        "",
        f"{'Command'.ljust(cmd_w)}  Description",
        f"{'-' * cmd_w}  -----------",
    ]
    for cmd, desc in rows:
        lines.append(f"{cmd.ljust(cmd_w)}  {desc}")
    lines.extend(
        [
            "",
            "Bare matching URLs are downloaded when auto is on.",
        ]
    )
    plain = "\n".join(lines)
    escaped = html.escape(plain)
    html_body = f"<pre><code>{escaped}</code></pre>"
    return plain, html_body


def is_admin(sender: str, cfg: AppConfig) -> bool:
    admins = cfg.bot.admin_users or []
    if not admins:
        return False
    return sender in admins


def _room_override(store: StateStore, room_id: str | None, key: str) -> Any:
    if not room_id:
        return None
    return store.room_value(room_id, key)


def effective_auto(cfg: AppConfig, store: StateStore, room_id: str | None = None) -> bool:
    override = _room_override(store, room_id, "auto_download")
    if override is not None:
        return bool(override)
    if store.state.auto_download is not None:
        return store.state.auto_download
    return cfg.bot.auto_download


def effective_allowed_rooms(cfg: AppConfig, store: StateStore) -> list[str]:
    if store.state.allowed_rooms is not None:
        return list(store.state.allowed_rooms)
    return list(cfg.bot.allowed_rooms or [])


def effective_notify(cfg: AppConfig, store: StateStore, room_id: str | None = None) -> bool:
    override = _room_override(store, room_id, "notify_on_failure")
    if override is not None:
        return bool(override)
    if store.state.notify_on_failure is not None:
        return store.state.notify_on_failure
    return cfg.bot.notify_on_failure


def effective_caption(cfg: AppConfig, store: StateStore, room_id: str | None = None) -> str:
    override = _room_override(store, room_id, "success_caption")
    if override is not None:
        return str(override)
    if store.state.success_caption is not None:
        return store.state.success_caption
    return cfg.bot.success_caption


def room_allowed_effective(room_id: str, cfg: AppConfig, store: StateStore) -> bool:
    allowed = effective_allowed_rooms(cfg, store)
    if not allowed:
        return True
    return room_id in allowed


def configured_prefix(cfg: AppConfig | None) -> str:
    """Command token from config, or ``!reel`` when unset."""
    if cfg is None:
        return REEL_PREFIX
    raw = (cfg.bot.command_prefix or "").strip()
    return raw or REEL_PREFIX


def _prefix_re(prefix: str) -> re.Pattern[str]:
    # Whole token only, so a prefix of ``!reel`` does not match ``!reelgrab``.
    return re.compile(rf"(?:^|\s){re.escape(prefix)}(?=\s|$)")


def text_after_reel_prefix(text: str, cfg: AppConfig | None = None) -> str | None:
    """Text after the configured command token, or None when that token is absent.

    The token must be whole (start or whitespace before it, whitespace or end
    after it) so ``!reelgrab`` and ``!reels`` do not match ``!reel``.
    """
    raw = text or ""
    match = _prefix_re(configured_prefix(cfg)).search(raw)
    if not match:
        return None
    return raw[match.end() :].strip()


def message_has_reel_prefix(text: str, cfg: AppConfig | None = None) -> bool:
    return text_after_reel_prefix(text, cfg) is not None


def message_has_prefix(text: str, cfg: AppConfig | None = None) -> bool:
    return message_has_reel_prefix(text, cfg)


def force_prefixes(cfg: AppConfig) -> tuple[str, ...]:
    """Configured command prefix (default ``!reel``)."""
    return (configured_prefix(cfg),)


def parse_command(body: str, cfg: AppConfig) -> tuple[str, list[str]] | None:
    """Parse a command only when the body contains the configured prefix token."""
    text = (body or "").strip()
    if not text:
        return None

    rest = text_after_reel_prefix(text, cfg)
    if rest is None:
        return None
    if not rest:
        return ("help", [])

    first = rest.split(maxsplit=1)[0]
    if is_http_url(normalize_url(first)):
        return ("grab", [rest])

    try:
        parts = shlex.split(rest)
    except ValueError:
        parts = rest.split()
    if not parts:
        return None

    cmd = parts[0].lower()
    args = parts[1:]
    aliases = {
        "ig": "grab",
        "download": "grab",
        "dl": "grab",
        "grab": "grab",
    }
    cmd = aliases.get(cmd, cmd)

    if cmd not in PUBLIC_COMMANDS and cmd not in ADMIN_COMMANDS:
        return None
    return cmd, args


def ytdlp_version() -> str:
    try:
        import yt_dlp.version

        return str(yt_dlp.version.__version__)
    except Exception:
        return "unknown"


async def handle_command(
    bot: MatrixGateway,
    cfg: AppConfig,
    store: StateStore,
    *,
    room_id: str,
    event_id: str,
    sender: str,
    cmd: str,
    args: list[str],
    is_direct: bool,
    thread_root_event_id: str | None = None,
) -> bool:
    admin = is_admin(sender, cfg)

    async def reply_text(body: str, formatted_body: str | None = None) -> None:
        await bot.send_text(
            room_id,
            body,
            reply_to_event_id=event_id,
            formatted_body=formatted_body,
            thread_root_event_id=thread_root_event_id,
        )

    if cmd in ADMIN_COMMANDS and not admin:
        if is_direct or cmd == "grab":
            await reply_text(
                "Not authorized. Add your MXID to bot.admin_users in config.yaml.",
            )
            return True
        return False

    if cmd not in PUBLIC_COMMANDS and cmd not in ADMIN_COMMANDS:
        return False

    if cmd == "help":
        plain, formatted = format_help_text(configured_prefix(cfg))
        await reply_text(plain, formatted)
        return True

    if cmd == "ping":
        await reply_text("pong")
        return True

    if cmd == "whoami":
        plain = f"you={sender}\nbot={bot.user_id}\nadmin={admin}"
        await reply_text(plain, f"<pre><code>{html.escape(plain)}</code></pre>")
        return True

    if cmd == "status":
        cookies = cfg.cookies_file_path
        allowed = effective_allowed_rooms(cfg, store)
        upload_limit = getattr(bot, "max_upload_bytes", None)
        rows = [
            ("bot", bot.user_id),
            ("homeserver", cfg.homeserver.address),
            ("domain", cfg.homeserver.domain),
            ("appservice.id", cfg.appservice.id),
            ("command_prefix", configured_prefix(cfg)),
            ("downloader", f"yt-dlp {ytdlp_version()}"),
            (
                "upload_limit",
                f"{upload_limit} bytes" if upload_limit else "homeserver default",
            ),
            (
                "convert",
                (
                    f"on force={cfg.download.convert.force} "
                    f"{cfg.download.convert.video_codec}+{cfg.download.convert.audio_codec}"
                    if cfg.download.convert.enabled
                    else "off"
                ),
            ),
            ("auto_download", str(effective_auto(cfg, store, room_id))),
            ("notify_on_failure", str(effective_notify(cfg, store, room_id))),
            ("caption", repr(effective_caption(cfg, store, room_id))),
            (
                "e2ee",
                f"device {bot.device_id}" if getattr(bot, "device_id", None) else "off",
            ),
            ("allowed_rooms", str(allowed or "(all invited)")),
            (
                "cookies",
                f"{'present' if cookies.is_file() else 'MISSING'} ({cookies})",
            ),
            ("data_dir", str(cfg.data_dir)),
            ("admins", str(cfg.bot.admin_users or "(none configured)")),
            ("joined_rooms", str(len(bot.joined_room_ids()))),
        ]
        key_w = max(len(k) for k, _ in rows)
        plain = "\n".join(f"{k.ljust(key_w)}  {v}" for k, v in rows)
        await reply_text(plain, f"<pre><code>{html.escape(plain)}</code></pre>")
        return True

    if cmd == "rooms":
        # Refresh from HS so status is current after restarts.
        if hasattr(bot, "refresh_joined_rooms"):
            await bot.refresh_joined_rooms()  # type: ignore[attr-defined]
        rooms = bot.joined_room_ids()
        if not rooms:
            await reply_text("No joined rooms yet.")
            return True
        allowed = set(effective_allowed_rooms(cfg, store))
        lines = []
        for rid in sorted(rooms):
            mark = ""
            if allowed:
                mark = " [allowed]" if rid in allowed else " [blocked by allow-list]"
            lines.append(f"{rid}{mark}")
        plain = "Joined rooms:\n" + "\n".join(lines)
        await reply_text(plain, f"<pre><code>{html.escape(plain)}</code></pre>")
        return True

    prefix = configured_prefix(cfg)

    if cmd == "allow":
        if not args:
            await reply_text(f"Usage: {prefix} allow <room_id> | {prefix} allow clear")
            return True
        if args[0].lower() == "clear":
            store.update(allowed_rooms=[])
            await reply_text("Allow-list cleared. All invited rooms are active.")
            return True
        rid = args[0]
        current = effective_allowed_rooms(cfg, store)
        if rid not in current:
            current.append(rid)
        store.update(allowed_rooms=current)
        await reply_text(f"Allow-list now: {current}")
        return True

    if cmd == "deny":
        if not args:
            await reply_text(f"Usage: {prefix} deny <room_id>")
            return True
        rid = args[0]
        current = [r for r in effective_allowed_rooms(cfg, store) if r != rid]
        store.update(allowed_rooms=current)
        await reply_text(f"Removed {rid}. Allow-list now: {current or '(all invited)'}")
        return True

    if cmd == "auto":
        if not args or args[0].lower() not in ("on", "off"):
            await reply_text(f"Usage: {prefix} auto on|off")
            return True
        on = args[0].lower() == "on"
        store.update(auto_download=on)
        await reply_text(f"auto_download = {on}")
        return True

    if cmd == "notify":
        if not args or args[0].lower() not in ("on", "off"):
            await reply_text(f"Usage: {prefix} notify on|off")
            return True
        on = args[0].lower() == "on"
        store.update(notify_on_failure=on)
        await reply_text(f"notify_on_failure = {on}")
        return True

    if cmd == "caption":
        if not args:
            await reply_text(f"Usage: {prefix} caption <text> | {prefix} caption clear")
            return True
        if args[0].lower() == "clear":
            store.update(success_caption="")
            await reply_text("caption cleared")
            return True
        text = " ".join(args)
        store.update(success_caption=text)
        await reply_text(f"caption = {text!r}")
        return True

    if cmd == "room":
        await _room_settings_command(
            reply_text, cfg, store, room_id=room_id, prefix=prefix, args=args
        )
        return True

    if cmd == "grab":
        return False

    return False


def _parse_toggle(token: str) -> bool | None:
    value = token.lower()
    if value == "on":
        return True
    if value == "off":
        return False
    if value in ("default", "clear", "inherit"):
        return None
    raise ValueError(token)


async def _room_settings_command(
    reply_text,
    cfg: AppConfig,
    store: StateStore,
    *,
    room_id: str,
    prefix: str,
    args: list[str],
) -> None:
    """Per-room auto / notify / caption. Omitted keys inherit the global setting."""
    if not args:
        auto = effective_auto(cfg, store, room_id)
        notify = effective_notify(cfg, store, room_id)
        caption = effective_caption(cfg, store, room_id)
        own = (store.state.rooms or {}).get(room_id) or {}
        await reply_text(
            f"room {room_id}\n"
            f"auto_download = {auto} (override {own.get('auto_download', 'inherit')})\n"
            f"notify_on_failure = {notify} (override {own.get('notify_on_failure', 'inherit')})\n"
            f"caption = {caption!r}"
        )
        return
    kind = args[0].lower()
    rest = args[1:]
    if kind == "auto":
        if not rest:
            await reply_text(f"Usage: {prefix} room auto on|off|default")
            return
        try:
            value = _parse_toggle(rest[0])
        except ValueError:
            await reply_text(f"Usage: {prefix} room auto on|off|default")
            return
        store.set_room(room_id, auto_download=value)
        await reply_text(f"room auto_download = {effective_auto(cfg, store, room_id)}")
        return
    if kind == "notify":
        if not rest:
            await reply_text(f"Usage: {prefix} room notify on|off|default")
            return
        try:
            value = _parse_toggle(rest[0])
        except ValueError:
            await reply_text(f"Usage: {prefix} room notify on|off|default")
            return
        store.set_room(room_id, notify_on_failure=value)
        await reply_text(f"room notify_on_failure = {effective_notify(cfg, store, room_id)}")
        return
    if kind == "caption":
        if not rest:
            await reply_text(f"Usage: {prefix} room caption <text> | {prefix} room caption clear")
            return
        if rest[0].lower() in ("clear", "default"):
            store.set_room(room_id, success_caption=None)
            await reply_text("room caption inherits the global caption")
            return
        text = " ".join(rest)
        store.set_room(room_id, success_caption=text)
        await reply_text(f"room caption = {text!r}")
        return
    await reply_text(
        f"Usage: {prefix} room | {prefix} room auto on|off|default | "
        f"{prefix} room notify on|off|default | {prefix} room caption <text>"
    )


def grab_urls_from_command(
    cmd: str, args: list[str], body: str, cfg: AppConfig
) -> list[str] | None:
    if cmd != "grab":
        return None
    from reelgrab.urls import find_matching_urls

    rest = " ".join(args).strip() or body
    after = text_after_reel_prefix(rest, cfg)
    if after is not None:
        rest = after
    urls = find_matching_urls(rest, cfg.url_patterns)
    if not urls:
        token = rest.split()[0] if rest else ""
        if is_http_url(token):
            urls = [token]
    return urls


# Back-compat alias
ig_urls_from_command = grab_urls_from_command
