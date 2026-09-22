"""Admin / DM command handling."""

from __future__ import annotations

import html
import logging
import re
import shlex
from typing import TYPE_CHECKING

from reelgrab.config import AppConfig
from reelgrab.state import StateStore
from reelgrab.urls import is_http_url, normalize_url

if TYPE_CHECKING:
    from reelgrab.matrix_client import MatrixGateway

log = logging.getLogger("reelgrab.commands")

# Exact listen / command token. Not configurable: bare words must not trigger.
REEL_PREFIX = "!reel"
_REEL_PREFIX_RE = re.compile(r"(?:^|\s)!reel(?=\s|$)")

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
    ("!reel notify on|off", "Failure notices"),
    ("!reel caption <text>", "Success caption (caption clear = default)"),
    ("!reel <url>", "Download one URL now"),
]


def format_help_text() -> tuple[str, str]:
    """Return (plain body, html formatted_body) with aligned columns."""
    cmd_w = max(len(c) for c, _ in _HELP_ROWS)
    lines = [
        "reelgrab: short-form video grabber",
        "Commands need the !reel prefix. A supported video URL is grabbed on its own.",
        "Anything else is ignored.",
        "",
        f"{'Command'.ljust(cmd_w)}  Description",
        f"{'-' * cmd_w}  -----------",
    ]
    for cmd, desc in _HELP_ROWS:
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


def effective_auto(cfg: AppConfig, store: StateStore) -> bool:
    if store.state.auto_download is not None:
        return store.state.auto_download
    return cfg.bot.auto_download


def effective_allowed_rooms(cfg: AppConfig, store: StateStore) -> list[str]:
    if store.state.allowed_rooms is not None:
        return list(store.state.allowed_rooms)
    return list(cfg.bot.allowed_rooms or [])


def effective_notify(cfg: AppConfig, store: StateStore) -> bool:
    if store.state.notify_on_failure is not None:
        return store.state.notify_on_failure
    return cfg.bot.notify_on_failure


def effective_caption(cfg: AppConfig, store: StateStore) -> str:
    if store.state.success_caption is not None:
        return store.state.success_caption
    return cfg.bot.success_caption


def room_allowed_effective(room_id: str, cfg: AppConfig, store: StateStore) -> bool:
    allowed = effective_allowed_rooms(cfg, store)
    if not allowed:
        return True
    return room_id in allowed


def text_after_reel_prefix(text: str) -> str | None:
    """Text after the ``!reel`` token, or None when that exact token is absent.

    ``!reel`` must be a whole token (start or whitespace before it, whitespace
    or end after it) so ``!reelgrab`` and ``!reels`` do not match.
    """
    raw = text or ""
    match = _REEL_PREFIX_RE.search(raw)
    if not match:
        return None
    return raw[match.end() :].strip()


def message_has_reel_prefix(text: str) -> bool:
    return text_after_reel_prefix(text) is not None


def force_prefixes(cfg: AppConfig) -> tuple[str, ...]:
    """The only command prefix. ``cfg`` is unused; the token is fixed."""
    del cfg
    return (REEL_PREFIX,)


def parse_command(body: str, cfg: AppConfig) -> tuple[str, list[str]] | None:
    """Parse a command only when the body contains the ``!reel`` token."""
    del cfg
    text = (body or "").strip()
    if not text:
        return None

    rest = text_after_reel_prefix(text)
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
) -> bool:
    admin = is_admin(sender, cfg)

    if cmd in ADMIN_COMMANDS and not admin:
        if is_direct or cmd == "grab":
            await bot.send_text(
                room_id,
                "Not authorized. Add your MXID to bot.admin_users in config.yaml.",
                reply_to_event_id=event_id,
            )
            return True
        return False

    if cmd not in PUBLIC_COMMANDS and cmd not in ADMIN_COMMANDS:
        return False

    reply = event_id

    if cmd == "help":
        plain, formatted = format_help_text()
        await bot.send_text(
            room_id,
            plain,
            reply_to_event_id=reply,
            formatted_body=formatted,
        )
        return True

    if cmd == "ping":
        await bot.send_text(room_id, "pong", reply_to_event_id=reply)
        return True

    if cmd == "whoami":
        plain = f"you={sender}\nbot={bot.user_id}\nadmin={admin}"
        await bot.send_text(
            room_id,
            plain,
            reply_to_event_id=reply,
            formatted_body=f"<pre><code>{html.escape(plain)}</code></pre>",
        )
        return True

    if cmd == "status":
        cookies = cfg.cookies_file_path
        allowed = effective_allowed_rooms(cfg, store)
        rows = [
            ("bot", bot.user_id),
            ("homeserver", cfg.homeserver.address),
            ("domain", cfg.homeserver.domain),
            ("appservice.id", cfg.appservice.id),
            ("downloader", "yt-dlp"),
            (
                "convert",
                (
                    f"on force={cfg.download.convert.force} "
                    f"{cfg.download.convert.video_codec}+{cfg.download.convert.audio_codec}"
                    if cfg.download.convert.enabled
                    else "off"
                ),
            ),
            ("auto_download", str(effective_auto(cfg, store))),
            ("notify_on_failure", str(effective_notify(cfg, store))),
            ("caption", repr(effective_caption(cfg, store))),
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
        await bot.send_text(
            room_id,
            plain,
            reply_to_event_id=reply,
            formatted_body=f"<pre><code>{html.escape(plain)}</code></pre>",
        )
        return True

    if cmd == "rooms":
        # Refresh from HS so status is current after restarts.
        if hasattr(bot, "refresh_joined_rooms"):
            await bot.refresh_joined_rooms()  # type: ignore[attr-defined]
        rooms = bot.joined_room_ids()
        if not rooms:
            await bot.send_text(room_id, "No joined rooms yet.", reply_to_event_id=reply)
            return True
        allowed = set(effective_allowed_rooms(cfg, store))
        lines = []
        for rid in sorted(rooms):
            mark = ""
            if allowed:
                mark = " [allowed]" if rid in allowed else " [blocked by allow-list]"
            lines.append(f"{rid}{mark}")
        plain = "Joined rooms:\n" + "\n".join(lines)
        await bot.send_text(
            room_id,
            plain,
            reply_to_event_id=reply,
            formatted_body=f"<pre><code>{html.escape(plain)}</code></pre>",
        )
        return True

    if cmd == "allow":
        if not args:
            await bot.send_text(
                room_id,
                "Usage: !reel allow <room_id> | !reel allow clear",
                reply_to_event_id=reply,
            )
            return True
        if args[0].lower() == "clear":
            store.update(allowed_rooms=[])
            await bot.send_text(
                room_id,
                "Allow-list cleared. All invited rooms are active.",
                reply_to_event_id=reply,
            )
            return True
        rid = args[0]
        current = effective_allowed_rooms(cfg, store)
        if rid not in current:
            current.append(rid)
        store.update(allowed_rooms=current)
        await bot.send_text(room_id, f"Allow-list now: {current}", reply_to_event_id=reply)
        return True

    if cmd == "deny":
        if not args:
            await bot.send_text(room_id, "Usage: !reel deny <room_id>", reply_to_event_id=reply)
            return True
        rid = args[0]
        current = [r for r in effective_allowed_rooms(cfg, store) if r != rid]
        store.update(allowed_rooms=current)
        await bot.send_text(
            room_id,
            f"Removed {rid}. Allow-list now: {current or '(all invited)'}",
            reply_to_event_id=reply,
        )
        return True

    if cmd == "auto":
        if not args or args[0].lower() not in ("on", "off"):
            await bot.send_text(room_id, "Usage: !reel auto on|off", reply_to_event_id=reply)
            return True
        on = args[0].lower() == "on"
        store.update(auto_download=on)
        await bot.send_text(room_id, f"auto_download = {on}", reply_to_event_id=reply)
        return True

    if cmd == "notify":
        if not args or args[0].lower() not in ("on", "off"):
            await bot.send_text(room_id, "Usage: !reel notify on|off", reply_to_event_id=reply)
            return True
        on = args[0].lower() == "on"
        store.update(notify_on_failure=on)
        await bot.send_text(
            room_id, f"notify_on_failure = {on}", reply_to_event_id=reply
        )
        return True

    if cmd == "caption":
        if not args:
            await bot.send_text(
                room_id,
                "Usage: !reel caption <text> | !reel caption clear",
                reply_to_event_id=reply,
            )
            return True
        if args[0].lower() == "clear":
            store.update(success_caption="")
            await bot.send_text(room_id, "caption cleared", reply_to_event_id=reply)
            return True
        text = " ".join(args)
        store.update(success_caption=text)
        await bot.send_text(room_id, f"caption = {text!r}", reply_to_event_id=reply)
        return True

    if cmd == "grab":
        return False

    return False


def grab_urls_from_command(
    cmd: str, args: list[str], body: str, cfg: AppConfig
) -> list[str] | None:
    if cmd != "grab":
        return None
    from reelgrab.urls import find_matching_urls

    rest = " ".join(args).strip() or body
    after = text_after_reel_prefix(rest)
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
