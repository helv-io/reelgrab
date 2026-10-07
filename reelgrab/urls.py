"""Detect short-form video URLs in message text."""

from __future__ import annotations

import html
import re
from urllib.parse import urlparse, urlunparse

_URL_RE = re.compile(
    r"https?://[^\s<>\[\]()\"']+",
    re.IGNORECASE,
)

_TRAIL_PUNCT = ".,;:!?)》」』】＞>"

# Default patterns: short-form only (reels / shorts / short clips), not long-form.
DEFAULT_URL_PATTERNS: list[str] = [
    # Instagram Reels
    r"instagram\.com/reel/",
    r"instagram\.com/reels/",
    r"instagr\.am/",
    r"l\.instagram\.com/",
    # YouTube Shorts (not full watch?v= videos)
    r"youtube\.com/shorts/",
    r"youtube\.com/short/",
    r"m\.youtube\.com/shorts/",
    # Facebook Reels / short clips
    r"facebook\.com/reel/",
    r"facebook\.com/reels/",
    r"facebook\.com/share/r/",
    r"fb\.watch/",
    r"fb\.com/reel/",
    r"fb\.com/reels/",
    # TikTok (short-form by nature)
    r"tiktok\.com/.*/video/",
    r"tiktok\.com/t/",
    r"vm\.tiktok\.com/",
    r"vt\.tiktok\.com/",
    # Twitter/X amplify_video CDN: direct MP4.
    r"video\.twimg\.com/amplify_video/.+\.mp4",
    # Instagram feed posts and IGTV (often video).
    r"instagram\.com/p/",
    r"instagram\.com/tv/",
    # X / Twitter status pages (host-anchored so "box.com" does not match).
    r"https?://(?:www\.|mobile\.)?(?:twitter\.com|x\.com)/[^/?#\s]+/status/\d+",
    # Threads, Bluesky, Reddit.
    r"https?://(?:www\.)?threads\.(?:net|com)/(?:@|t/)",
    r"https?://(?:www\.)?bsky\.app/profile/[^/?#\s]+/post/",
    r"https?://v\.redd\.it/",
    r"https?://(?:www\.|old\.|m\.)?reddit\.com/r/[^/?#\s]+/comments/",
    r"https?://(?:www\.)?redd\.it/[A-Za-z0-9]+",
]

# Always merged into configured patterns so existing config.yaml files
# pick up amplify_video without a manual edit.
AMPLIFY_URL_PATTERN = r"video\.twimg\.com/amplify_video/.+\.mp4"

# Patterns added after the v0.5.0 defaults. Merged at load time so an existing
# ``urls.url_patterns`` list picks them up without a rewrite.
EXTRA_URL_PATTERNS: list[str] = [
    r"instagram\.com/p/",
    r"instagram\.com/tv/",
    r"https?://(?:www\.|mobile\.)?(?:twitter\.com|x\.com)/[^/?#\s]+/status/\d+",
    r"https?://(?:www\.)?threads\.(?:net|com)/(?:@|t/)",
    r"https?://(?:www\.)?bsky\.app/profile/[^/?#\s]+/post/",
    r"https?://v\.redd\.it/",
    r"https?://(?:www\.|old\.|m\.)?reddit\.com/r/[^/?#\s]+/comments/",
    r"https?://(?:www\.)?redd\.it/[A-Za-z0-9]+",
]


def normalize_url(url: str) -> str:
    """Unescape HTML entities and strip trailing punctuation."""
    url = html.unescape(url.strip())
    return url.rstrip(_TRAIL_PUNCT)


def is_http_url(url: str) -> bool:
    """True for absolute http(s) URLs with a host (force-grab / outbound fetch)."""
    if not url or not isinstance(url, str):
        return False
    try:
        p = urlparse(url.strip())
    except Exception:
        return False
    if p.scheme not in ("http", "https") or not (p.netloc or "").strip():
        return False
    # Short-form links never need embedded credentials; reject userinfo so the
    # effective host cannot be obscured (https://user@host/...).
    if p.username is not None or p.password is not None:
        return False
    return True


def extract_urls(text: str) -> list[str]:
    if not text:
        return []
    found: list[str] = []
    candidates = [text, html.unescape(text)]
    seen_local: set[str] = set()
    for blob in candidates:
        for match in _URL_RE.finditer(blob):
            url = normalize_url(match.group(0))
            if url and is_http_url(url) and url not in seen_local:
                seen_local.add(url)
                found.append(url)
    return found


def is_amplify_video_url(url: str) -> bool:
    """True for video.twimg.com amplify_video direct MP4 URLs.

    Close variants (http/https, ``?tag=N``, ``vid/avc1/{WxH}`` or ``vid/{WxH}``)
    match. Playlists and other twimg paths do not.
    """
    if not is_http_url(url):
        return False
    try:
        parsed = urlparse(url.strip())
    except Exception:
        return False
    host = (parsed.hostname or "").lower()
    if host != "video.twimg.com":
        return False
    path = (parsed.path or "").lower()
    if "/amplify_video/" not in path:
        return False
    return path.endswith(".mp4")


def is_matching_url(url: str, patterns: list[str]) -> bool:
    """Return True if url matches any configured pattern."""
    if not url or not is_http_url(url):
        return False
    for pattern in patterns:
        if re.search(pattern, url, re.IGNORECASE):
            return True
    return False


def canonicalize_url(url: str) -> str:
    """Stable key for dedupe: strip query/fragment, normalize host."""
    try:
        p = urlparse(url)
        host = p.netloc.lower()
        if host.startswith("www."):
            host = host[4:]
        path = p.path.rstrip("/") or "/"
        scheme = (p.scheme or "https").lower()
        if scheme not in ("http", "https"):
            scheme = "https"
        return urlunparse((scheme, host, path, "", "", ""))
    except Exception:
        return url


def find_matching_urls(text: str, patterns: list[str]) -> list[str]:
    """Return unique matching URLs in text, order preserved."""
    seen: set[str] = set()
    out: list[str] = []
    for url in extract_urls(text):
        if not is_matching_url(url, patterns):
            continue
        key = canonicalize_url(url)
        if key not in seen:
            seen.add(key)
            out.append(url)
    return out
