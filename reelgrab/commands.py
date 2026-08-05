"""Admin / DM command handling."""

from __future__ import annotations

import html
import logging
import shlex
from typing import TYPE_CHECKING

from reelgrab.config import AppConfig
from reelgrab.state import StateStore

if TYPE_CHECKING:
    from reelgrab.matrix_client import MatrixGateway

log = logging.getLogger("reelgrab.commands")

# Force-download prefixes (primary + legacy)
FORCE_PREFIXES = ("!grab", "!ig")

# Known command verbs (first token). Used so DMs/rooms both accept bare commands.
KNOWN_COMMANDS = frozenset(
    {
        "help",
        "ping",
        "status",
        "whoami",
        "rooms",
        "allow",
        "deny",
        "auto",
        "notify",
        "caption",
        "grab",
        "ig",
        "download",
        "dl",
    }
)

# Fixed-width help so Matrix clients can show aligned columns inside <pre>.
_HELP_ROWS: list[tuple[str, str]] = [
    ("help", "Show this help"),
    ("ping", "Liveness check"),
    ("status", "Config, cookies, identity"),
    ("whoami", "Your MXID as seen by the bot"),
    ("rooms", "Joined room IDs"),
    ("allow <room_id>", "Add room to allow-list"),
    ("deny <room_id>", "Remove room from allow-list"),
    ("allow clear", "Clear allow-list (all rooms)"),
    ("auto on|off", "Auto-download matching links"),
    ("notify on|off", "Failure notices"),
    ("caption <text>", "Success caption (caption clear = default)"),
    ("grab <url>", "Download one URL now"),
    ("!grab <url>", "Same as grab (also !ig)"),
]


def format_help_text() -> tuple[str, str]:
    """Return (plain body, html formatted_body) with aligned columns."""
    cmd_w = max(len(c) for c, _ in _HELP_ROWS)
    lines = [
        "reelgrab — short-form video grabber",
        "DM me, or use commands in a room if you are an admin.",
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


def force_prefixes(cfg: AppConfig) -> tuple[str, ...]:
    """Configured force-download prefixes plus legacy aliases."""
    primary = (cfg.bot.command_prefix or "!grab").strip()
    out: list[str] = []
    for p in (primary, *FORCE_PREFIXES):
        if p and p not in out:
            out.append(p)
    return tuple(out)


def parse_command(body: str, cfg: AppConfig) -> tuple[str, list[str]] | None:
    text = (body or "").strip()
    if not text:
        return None

    for prefix in force_prefixes(cfg):
        if text.startswith(prefix):
            # Ensure "!grabx" is not treated as "!grab"
            rest = text[len(prefix) :]
            if rest and not rest[0].isspace() and rest[0] not in "/":
                # only allow if prefix is full token, unless rest empty after strip of one space
                if not rest.startswith((" ", "\t")):
                    continue
            rest = rest.strip()
            if not rest:
                return ("help", [])
            return ("grab", [rest])

    if text.startswith("!"):
        text = text[1:].strip()

    try:
        parts = shlex.split(text)
    except ValueError:
        parts = text.split()
    if not parts:
        return None

    cmd = parts[0].lower()
    if cmd in ("reelgrab", "grabbot", "bot", "rg") and len(parts) > 1:
        cmd = parts[1].lower()
        args = parts[2:]
    else:
        args = parts[1:]

    # normalize aliases
    aliases = {
        "ig": "grab",
        "download": "grab",
        "dl": "grab",
    }
    cmd = aliases.get(cmd, cmd)

    known = {
        "help",
        "ping",
        "status",
        "whoami",
        "rooms",
        "allow",
        "deny",
        "auto",
        "notify",
        "caption",
        "grab",
    }
    if cmd not in known:
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
    public_cmds = {"help", "ping", "whoami"}
    admin_cmds = {
        "status",
        "rooms",
        "allow",
        "deny",
        "auto",
        "notify",
        "caption",
        "grab",
    }

    if cmd in admin_cmds and not admin:
        if is_direct or cmd == "grab":
            await bot.send_text(
                room_id,
                "Not authorized. Add your MXID to bot.admin_users in config.yaml.",
                reply_to_event_id=event_id,
            )
            return True
        return False

    if cmd not in public_cmds and cmd not in admin_cmds:
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
                "Usage: allow <room_id> | allow clear",
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
            await bot.send_text(room_id, "Usage: deny <room_id>", reply_to_event_id=reply)
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
            await bot.send_text(room_id, "Usage: auto on|off", reply_to_event_id=reply)
            return True
        on = args[0].lower() == "on"
        store.update(auto_download=on)
        await bot.send_text(room_id, f"auto_download = {on}", reply_to_event_id=reply)
        return True

    if cmd == "notify":
        if not args or args[0].lower() not in ("on", "off"):
            await bot.send_text(room_id, "Usage: notify on|off", reply_to_event_id=reply)
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
                "Usage: caption <text> | caption clear",
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
    for prefix in force_prefixes(cfg):
        if rest.startswith(prefix):
            rest = rest[len(prefix) :].strip()
            break
    urls = find_matching_urls(rest, cfg.url_patterns)
    if not urls and rest.startswith("http"):
        urls = [rest.split()[0]]
    return urls


# Back-compat alias
ig_urls_from_command = grab_urls_from_command
