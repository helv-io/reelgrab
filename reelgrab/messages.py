"""Matrix message filtering: edits, replies, history, and the link/command gate."""

from __future__ import annotations

from typing import Any

from reelgrab.commands import configured_prefix, message_has_prefix
from reelgrab.config import AppConfig
from reelgrab.urls import find_matching_urls

# Events older than process start by more than this are backlog, not live chat.
HISTORY_GRACE_MS = 15_000


def is_edit(event: dict[str, Any]) -> bool:
    content = event.get("content") or {}
    if not isinstance(content, dict):
        return False
    relates = content.get("m.relates_to") or {}
    if isinstance(relates, dict) and relates.get("rel_type") == "m.replace":
        return True
    return "m.new_content" in content


def is_reply(event: dict[str, Any]) -> bool:
    content = event.get("content") or {}
    relates = content.get("m.relates_to") or {}
    if not isinstance(relates, dict):
        return False
    return isinstance(relates.get("m.in_reply_to"), dict)


def thread_root_id(event: dict[str, Any]) -> str | None:
    content = event.get("content") or {}
    relates = content.get("m.relates_to") or {}
    if not isinstance(relates, dict) or relates.get("rel_type") != "m.thread":
        return None
    root = relates.get("event_id")
    return str(root) if root else None


def strip_reply_fallback(body: str) -> str:
    """Drop the plaintext quote block so a replied-to link is not grabbed again.

    Matrix reply fallbacks look like::

        > <@user:example.com> quoted text

        the actual reply
    """
    text = body or ""
    lines = text.splitlines()
    if not lines or not lines[0].startswith("> "):
        return text.strip()
    index = 0
    while index < len(lines) and lines[index].startswith(">"):
        index += 1
    if index < len(lines) and lines[index].strip() == "":
        index += 1
    return "\n".join(lines[index:]).strip()


def is_historical(
    event: dict[str, Any],
    *,
    started_ms: int,
    grace_ms: int = HISTORY_GRACE_MS,
) -> bool:
    """True when the event was sent before this process started (plus grace)."""
    ts = event.get("origin_server_ts")
    if isinstance(ts, (int, float)):
        return int(ts) < int(started_ms) - int(grace_ms)
    unsigned = event.get("unsigned") or {}
    if isinstance(unsigned, dict):
        age = unsigned.get("age")
        if isinstance(age, (int, float)) and int(age) > int(grace_ms):
            return True
    return False


def message_is_actionable(body: str, cfg: AppConfig) -> bool:
    """True when the text contains the command prefix or a supported media URL."""
    text = (body or "").strip()
    if not text:
        return False
    if message_has_prefix(text, cfg):
        return True
    return bool(find_matching_urls(text, cfg.url_patterns))


def prefix_token(cfg: AppConfig) -> str:
    return configured_prefix(cfg)
