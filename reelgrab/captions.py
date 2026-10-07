"""Media captions and thumbnail dimensions for Matrix m.video."""

from __future__ import annotations

import re

_FILENAME_SAFE = re.compile(r"[^A-Za-z0-9._-]+")
_VIDEO_EXTS = {"mp4", "webm", "mkv", "mov", "m4v"}


def thumbnail_dimensions(width: int, height: int, *, max_width: int = 640) -> tuple[int, int]:
    """Match ffmpeg ``scale='min(640,iw)':-2``.

    The generator caps the *width* at ``max_width`` and rounds height to an even
    number. It does not fit the long edge into 640, so a vertical 1080x1920
    frame becomes 640x1138, not 360x640.
    """
    if width <= 0 or height <= 0:
        return 0, 0
    tw = min(int(width), int(max_width))
    if tw % 2:
        tw -= 1
    tw = max(2, tw)
    raw_h = height * (tw / float(width))
    th = int(round(raw_h / 2.0)) * 2
    return tw, max(2, th)


def media_filename(video_id: str | None, ext: str = ".mp4") -> str:
    """Stable upload name such as ``AbC123.mp4`` (never the ``*_bridge.mp4`` temp)."""
    suffix = ext if ext.startswith(".") else f".{ext}"
    raw = (video_id or "").strip() or "video"
    cleaned = _FILENAME_SAFE.sub("_", raw).strip("._") or "video"
    stem, dot, existing = cleaned.rpartition(".")
    if dot and existing.lower() in _VIDEO_EXTS:
        return cleaned[:120]
    name = f"{cleaned}{suffix}"
    return name[:120]


def build_caption(
    *,
    uploader: str | None,
    title: str | None,
    source_url: str,
    filename: str,
    custom: str | None = None,
) -> str:
    """Caption body. A non-empty ``custom`` caption replaces the metadata line.

    Default shape: ``@uploader: title · https://source``. With no metadata the
    filename is the body, which Matrix treats as the file name rather than a caption.
    """
    override = (custom or "").strip()
    if override:
        return override
    name = (uploader or "").strip().lstrip("@")
    title_text = (title or "").strip()
    if len(title_text) > 180:
        title_text = title_text[:179].rstrip() + "…"
    link = (source_url or "").strip()
    if name and title_text:
        main = f"@{name}: {title_text}"
    elif title_text:
        main = title_text
    elif name:
        main = f"@{name}"
    else:
        main = ""
    if not main:
        return filename
    if link:
        return f"{main} · {link}"
    return main


def caption_html(body: str, source_url: str) -> str | None:
    """HTML caption with the source URL as a link, when the body contains that URL."""
    import html

    link = (source_url or "").strip()
    if not link or link not in body or body == link:
        return None
    escaped_link = html.escape(link, quote=True)
    escaped_body = html.escape(body)
    linked = escaped_body.replace(
        html.escape(link),
        f'<a href="{escaped_link}">{escaped_link}</a>',
        1,
    )
    return linked
