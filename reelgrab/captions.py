"""Media captions and thumbnail dimensions for Matrix m.video."""

from __future__ import annotations

import re

_FILENAME_SAFE = re.compile(r"[^A-Za-z0-9._-]+")
_VIDEO_EXTS = {"mp4", "webm", "mkv", "mov", "m4v"}

# Title text in the metadata line. The ellipsis counts toward the limit.
TITLE_LIMIT = 180
# Default cut for the source description. ``0`` means no extra cut.
DEFAULT_MAX_DESCRIPTION_LENGTH = 500
# mautrix-whatsapp copies the m.video body (formatted_body when it is HTML)
# into VideoMessage.Caption. WhatsApp rejects a video caption past this length.
BRIDGE_CAPTION_LIMIT = 1024
_EMPTY_DESCRIPTION = frozenset({"", "none", "null"})


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


def _truncate(text: str, limit: int) -> str:
    """Cut ``text`` to ``limit`` characters, ending with an ellipsis when cut."""
    if limit <= 0 or len(text) <= limit:
        return text
    if limit == 1:
        return "…"
    return text[: limit - 1].rstrip() + "…"


def _is_hashtag_line(line: str) -> bool:
    parts = line.split()
    return bool(parts) and all(part.startswith("#") and len(part) > 1 for part in parts)


def _strip_trailing_hashtags(text: str) -> str:
    """Drop a trailing block of hashtag-only lines when other text remains.

    A line that mixes words and hashtags is left alone. A caption that is
    only hashtags is left alone.
    """
    lines = text.split("\n")
    end = len(lines)
    while end > 0:
        stripped = lines[end - 1].strip()
        if stripped and not _is_hashtag_line(stripped):
            break
        end -= 1
    removed = lines[end:]
    if not any(_is_hashtag_line(line.strip()) for line in removed):
        return text
    kept = "\n".join(lines[:end]).strip()
    if not kept:
        return text
    return kept


def prepare_description(description: str | None) -> str:
    """Source caption text, or ``""`` when there is nothing to show.

    ``None``, blank, and the strings ``None`` / ``null`` become empty so they
    never render as a line of their own.
    """
    if description is None:
        return ""
    text = str(description).replace("\r\n", "\n").replace("\r", "\n").strip()
    if text.casefold() in _EMPTY_DESCRIPTION:
        return ""
    return _strip_trailing_hashtags(text)


def _boundary_at(text: str, index: int) -> bool:
    if index >= len(text):
        return True
    nxt = text[index]
    return nxt.isspace() or nxt in ".,:;!?…-·|/"


_LEADING_SEP = re.compile(r"^[\s:|\-·.]+")


def _matches_prefix(text: str, prefix: str) -> bool:
    if not prefix or len(text) < len(prefix):
        return False
    if text[: len(prefix)].casefold() != prefix.casefold():
        return False
    return _boundary_at(text, len(prefix))


def _remainder_after(description: str, prefix: str) -> str:
    rest = description[len(prefix) :]
    return _LEADING_SEP.sub("", rest, count=1).strip()


def _split_overlap(title: str | None, description: str) -> tuple[str, bool]:
    """Return ``(text to append, drop title from the metadata line)``.

    The title stays when the description only adds more after it. The title
    is dropped when keeping it would show a second, truncated copy of the
    description.
    """
    title_text = (title or "").strip()
    desc = description.strip()
    if not desc or not title_text:
        return desc, False
    if desc.casefold() == title_text.casefold():
        if len(title_text) <= TITLE_LIMIT:
            return "", False
        return desc, True
    if _matches_prefix(desc, title_text):
        # The metadata line would cut this title, so keep a single full copy.
        if len(title_text) > TITLE_LIMIT:
            return desc, True
        return _remainder_after(desc, title_text), False
    shown = _truncate(title_text, TITLE_LIMIT) if len(title_text) > TITLE_LIMIT else title_text
    if shown.endswith("…"):
        stem = shown[:-1].rstrip()
        if stem and _matches_prefix(desc, stem):
            return desc, True
    return desc, False


def _metadata_line(uploader: str | None, title: str | None, link: str) -> str:
    name = (uploader or "").strip().lstrip("@")
    title_text = (title or "").strip()
    if len(title_text) > TITLE_LIMIT:
        title_text = _truncate(title_text, TITLE_LIMIT)
    if name and title_text:
        main = f"@{name}: {title_text}"
    elif title_text:
        main = title_text
    elif name:
        main = f"@{name}"
    else:
        main = ""
    if not main:
        return ""
    if link:
        return f"{main} · {link}"
    return main


def _assemble(header: str, description: str, max_length: int) -> str:
    """Join the metadata line and description, keeping the caption bridgeable."""
    desc = description.strip()
    if not desc:
        return header
    if max_length > 0:
        desc = _truncate(desc, max_length)
    sep = "\n\n" if header else ""
    room = BRIDGE_CAPTION_LIMIT - len(header) - len(sep)
    if room < 2:
        return header or _truncate(desc, BRIDGE_CAPTION_LIMIT)
    if len(desc) > room:
        desc = _truncate(desc, room)
    if not desc:
        return header
    if header:
        return f"{header}{sep}{desc}"
    return desc


def build_caption(
    *,
    uploader: str | None,
    title: str | None,
    source_url: str,
    filename: str,
    custom: str | None = None,
    description: str | None = None,
    include_description: bool = True,
    max_description_length: int = DEFAULT_MAX_DESCRIPTION_LENGTH,
) -> str:
    """Caption body. A non-empty ``custom`` caption replaces the metadata line.

    Default shape: ``@uploader: title · https://source``. A source description
    that adds something past that line follows after a blank line. With no
    metadata and no description the filename is the body, which Matrix treats
    as the file name rather than a caption.
    """
    link = (source_url or "").strip()
    override = (custom or "").strip()
    desc = prepare_description(description) if include_description else ""
    try:
        limit = int(max_description_length)
    except (TypeError, ValueError):
        limit = DEFAULT_MAX_DESCRIPTION_LENGTH

    if override:
        # The custom line replaces the metadata line, so compare the
        # description to that line. Keep the custom text even when it is long.
        if desc.casefold() == override.casefold():
            append = ""
        elif desc and _matches_prefix(desc, override):
            append = _remainder_after(desc, override)
        else:
            append = desc
        body = _assemble(override, append, limit)
        return body or filename

    append, drop_title = _split_overlap(title, desc) if desc else ("", False)
    header = _metadata_line(uploader, None if drop_title else title, link)
    # No uploader/title: the old body is the filename, which WhatsApp does not
    # show as a caption. A description needs a real body, and the source link
    # stays on the first line.
    if not header and append and link:
        header = link
    body = _assemble(header, append, limit)
    return body or filename


def caption_html(body: str, source_url: str) -> str | None:
    """HTML caption with the source URL as a link, when the body contains that URL.

    Newlines become ``<br>``. mautrix-whatsapp prefers ``formatted_body`` over
    the plain body, and HTML collapses raw newlines, so a multi-line caption
    would otherwise arrive on WhatsApp as one line.
    """
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
    return linked.replace("\n", "<br>\n")
