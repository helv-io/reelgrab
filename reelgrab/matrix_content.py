"""Build Matrix media event content (MSC2530 captions, threads, thumbnails)."""

from __future__ import annotations

from typing import Any

from reelgrab.captions import thumbnail_dimensions


def relates_to(
    *,
    reply_to_event_id: str | None = None,
    thread_root_event_id: str | None = None,
) -> dict[str, Any] | None:
    """Reply relation, including ``m.thread`` when the source event was in a thread."""
    if thread_root_event_id:
        in_reply = reply_to_event_id or thread_root_event_id
        return {
            "rel_type": "m.thread",
            "event_id": thread_root_event_id,
            "is_falling_back": in_reply == thread_root_event_id,
            "m.in_reply_to": {"event_id": in_reply},
        }
    if reply_to_event_id:
        return {"m.in_reply_to": {"event_id": reply_to_event_id}}
    return None


def build_video_content(
    *,
    mxc: str,
    body: str,
    filename: str,
    mime: str,
    size: int,
    duration_ms: int | None = None,
    width: int | None = None,
    height: int | None = None,
    thumbnail_mxc: str | None = None,
    thumbnail_size: int | None = None,
    thumbnail_width: int | None = None,
    thumbnail_height: int | None = None,
    blurhash: str | None = None,
    formatted_body: str | None = None,
    reply_to_event_id: str | None = None,
    thread_root_event_id: str | None = None,
) -> dict[str, Any]:
    """``m.video`` / ``m.file`` content with a real filename and caption body."""
    is_video = (mime or "").startswith("video/")
    info: dict[str, Any] = {"size": int(size), "mimetype": mime or "application/octet-stream"}
    if duration_ms is not None and duration_ms > 0:
        info["duration"] = int(duration_ms)
    if width:
        info["w"] = int(width)
    if height:
        info["h"] = int(height)
    if blurhash:
        info["blurhash"] = blurhash
    if thumbnail_mxc:
        info["thumbnail_url"] = thumbnail_mxc
        thumb_info: dict[str, Any] = {"mimetype": "image/jpeg"}
        if thumbnail_size:
            thumb_info["size"] = int(thumbnail_size)
        tw = int(thumbnail_width or 0)
        th = int(thumbnail_height or 0)
        if (tw <= 0 or th <= 0) and width and height:
            tw, th = thumbnail_dimensions(int(width), int(height))
        if tw > 0 and th > 0:
            thumb_info["w"] = tw
            thumb_info["h"] = th
        info["thumbnail_info"] = thumb_info

    content: dict[str, Any] = {
        "body": body or filename,
        "filename": filename,
        "info": info,
        "msgtype": "m.video" if is_video else "m.file",
        "url": mxc,
    }
    if formatted_body and content["body"] != filename:
        content["format"] = "org.matrix.custom.html"
        content["formatted_body"] = formatted_body
    relation = relates_to(
        reply_to_event_id=reply_to_event_id,
        thread_root_event_id=thread_root_event_id,
    )
    if relation:
        content["m.relates_to"] = relation
    return content
