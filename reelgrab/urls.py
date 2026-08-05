"""Detect grab-able video URLs in message text."""

from __future__ import annotations

import html
import re
from urllib.parse import urlparse, urlunparse

_URL_RE = re.compile(
    r"https?://[^\s<>\[\]()\"']+",
    re.IGNORECASE,
)

_TRAIL_PUNCT = ".,;:!?)》」』】＞>"


def normalize_url(url: str) -> str:
    """Unescape HTML entities and strip trailing punctuation."""
    url = html.unescape(url.strip())
    return url.rstrip(_TRAIL_PUNCT)


def extract_urls(text: str) -> list[str]:
    if not text:
        return []
    found: list[str] = []
    candidates = [text, html.unescape(text)]
    seen_local: set[str] = set()
    for blob in candidates:
        for match in _URL_RE.finditer(blob):
            url = normalize_url(match.group(0))
            if url and url not in seen_local:
                seen_local.add(url)
                found.append(url)
    return found


def is_matching_url(url: str, patterns: list[str]) -> bool:
    """Return True if url matches any configured pattern."""
    if not url:
        return False
    try:
        parsed = urlparse(url)
        host = (parsed.netloc or "").lower()
    except Exception:
        return False

    for pattern in patterns:
        if re.search(pattern, url, re.IGNORECASE):
            return True

    # Instagram path heuristics when host is IG but pattern list is minimal
    host_ok = "instagram.com" in host or host == "instagr.am" or host.endswith(
        ".instagr.am"
    )
    if host_ok:
        path = (parsed.path or "").lower()
        if any(seg in path for seg in ("/reel/", "/p/", "/tv/", "/reels/")):
            return True
    return False


# Back-compat alias
is_instagram_url = is_matching_url


def canonicalize_url(url: str) -> str:
    """Stable key for dedupe: strip query/fragment, normalize host."""
    try:
        p = urlparse(url)
        host = p.netloc.lower()
        if host.startswith("www."):
            host = host[4:]
        path = p.path.rstrip("/") or "/"
        return urlunparse((p.scheme.lower() or "https", host, path, "", "", ""))
    except Exception:
        return url


canonicalize_instagram_url = canonicalize_url


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


find_instagram_urls = find_matching_urls
