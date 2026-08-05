"""Admin / DM command handling."""

from __future__ import annotations

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

HELP_TEXT = """\
reelgrab commands (DM me, or use in a room if you are an admin)

  help              Show this help
  ping              Liveness check
  status            Config + cookies + identity
  whoami            Your MXID as seen by the bot
  rooms             Joined rooms (ids)
  allow <room_id>   Restrict downloads to this room (adds to allow-list)
  deny <room_id>    Remove room from allow-list
  allow clear       Clear allow-list (all invited rooms)
  auto on|off       Toggle auto-download on matching links
  backend ytdlp|metube
  notify on|off     Failure notices
  caption <text>    Success caption (use caption clear for default/filename)
  grab <url>        Download one URL now (same as !grab / !ig)

Also works without a keyword:
  !grab <url>   or   !ig <url>
  bare matching URLs when auto is on
"""


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


def effective_backend(cfg: AppConfig, store: StateStore) -> str:
    if store.state.backend:
        return store.state.backend
    return cfg.download.backend


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
        "backend",
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
        "backend",
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
        await bot.send_text(room_id, HELP_TEXT, reply_to_event_id=reply)
        return True

    if cmd == "ping":
        await bot.send_text(room_id, "pong", reply_to_event_id=reply)
        return True

    if cmd == "whoami":
        await bot.send_text(
            room_id,
            f"you={sender}\nbot={bot.user_id}\nadmin={admin}",
            reply_to_event_id=reply,
        )
        return True

    if cmd == "status":
        cookies = cfg.cookies_file_path
        allowed = effective_allowed_rooms(cfg, store)
        body = "\n".join(
            [
                f"bot: {bot.user_id}",
                f"homeserver: {cfg.homeserver.address}",
                f"domain: {cfg.homeserver.domain}",
                f"appservice.id: {cfg.appservice.id}",
                f"backend: {effective_backend(cfg, store)}",
                f"auto_download: {effective_auto(cfg, store)}",
                f"notify_on_failure: {effective_notify(cfg, store)}",
                f"caption: {effective_caption(cfg, store)!r}",
                f"allowed_rooms: {allowed or '(all invited)'}",
                f"cookies: {'present' if cookies.is_file() else 'MISSING'} ({cookies})",
                f"data_dir: {cfg.data_dir}",
                f"admins: {cfg.bot.admin_users or '(none configured)'}",
            ]
        )
        await bot.send_text(room_id, body, reply_to_event_id=reply)
        return True

    if cmd == "rooms":
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
        await bot.send_text(
            room_id, "Joined rooms:\n" + "\n".join(lines), reply_to_event_id=reply
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

    if cmd == "backend":
        if not args or args[0].lower() not in ("ytdlp", "metube"):
            await bot.send_text(
                room_id, "Usage: backend ytdlp|metube", reply_to_event_id=reply
            )
            return True
        store.update(backend=args[0].lower())
        await bot.send_text(
            room_id, f"backend = {args[0].lower()}", reply_to_event_id=reply
        )
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
