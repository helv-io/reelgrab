"""SQLite cache of canonical URL → already-uploaded Matrix media."""

from __future__ import annotations

import logging
import sqlite3
import threading
import time
from dataclasses import dataclass
from pathlib import Path

from reelgrab.urls import canonicalize_url

log = logging.getLogger("reelgrab.cache")

_SCHEMA = """\
CREATE TABLE IF NOT EXISTS media (
    url_key TEXT PRIMARY KEY,
    source_url TEXT NOT NULL,
    mxc TEXT NOT NULL,
    mime TEXT,
    size INTEGER,
    duration_ms INTEGER,
    width INTEGER,
    height INTEGER,
    filename TEXT,
    uploader TEXT,
    title TEXT,
    thumbnail_mxc TEXT,
    thumbnail_width INTEGER,
    thumbnail_height INTEGER,
    thumbnail_size INTEGER,
    blurhash TEXT,
    created_at INTEGER NOT NULL
)
"""


@dataclass
class CachedMedia:
    url_key: str
    source_url: str
    mxc: str
    mime: str
    size: int
    duration_ms: int | None
    width: int | None
    height: int | None
    filename: str
    uploader: str | None
    title: str | None
    thumbnail_mxc: str | None
    thumbnail_width: int | None
    thumbnail_height: int | None
    thumbnail_size: int | None
    blurhash: str | None


class MediaCache:
    """Process-wide URL cache. Repeats skip download and upload."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self._lock = threading.Lock()
        self._ensure()

    def _connect(self) -> sqlite3.Connection:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(self.path, timeout=5)
        conn.row_factory = sqlite3.Row
        return conn

    def _ensure(self) -> None:
        with self._lock, self._connect() as conn:
            conn.execute(_SCHEMA)

    def get(self, url: str) -> CachedMedia | None:
        key = canonicalize_url(url)
        try:
            with self._lock, self._connect() as conn:
                row = conn.execute(
                    "SELECT * FROM media WHERE url_key = ?", (key,)
                ).fetchone()
        except sqlite3.Error as exc:
            log.warning("media cache read failed: %s", exc)
            return None
        if row is None:
            return None
        return CachedMedia(
            url_key=row["url_key"],
            source_url=row["source_url"],
            mxc=row["mxc"],
            mime=row["mime"] or "video/mp4",
            size=int(row["size"] or 0),
            duration_ms=row["duration_ms"],
            width=row["width"],
            height=row["height"],
            filename=row["filename"] or "video.mp4",
            uploader=row["uploader"],
            title=row["title"],
            thumbnail_mxc=row["thumbnail_mxc"],
            thumbnail_width=row["thumbnail_width"],
            thumbnail_height=row["thumbnail_height"],
            thumbnail_size=row["thumbnail_size"],
            blurhash=row["blurhash"],
        )

    def put(self, url: str, item: CachedMedia) -> None:
        key = canonicalize_url(url)
        try:
            with self._lock, self._connect() as conn:
                conn.execute(
                    """
                    INSERT INTO media (
                        url_key, source_url, mxc, mime, size, duration_ms, width, height,
                        filename, uploader, title, thumbnail_mxc, thumbnail_width,
                        thumbnail_height, thumbnail_size, blurhash, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(url_key) DO UPDATE SET
                        source_url = excluded.source_url,
                        mxc = excluded.mxc,
                        mime = excluded.mime,
                        size = excluded.size,
                        duration_ms = excluded.duration_ms,
                        width = excluded.width,
                        height = excluded.height,
                        filename = excluded.filename,
                        uploader = excluded.uploader,
                        title = excluded.title,
                        thumbnail_mxc = excluded.thumbnail_mxc,
                        thumbnail_width = excluded.thumbnail_width,
                        thumbnail_height = excluded.thumbnail_height,
                        thumbnail_size = excluded.thumbnail_size,
                        blurhash = excluded.blurhash,
                        created_at = excluded.created_at
                    """,
                    (
                        key,
                        item.source_url or url,
                        item.mxc,
                        item.mime,
                        item.size,
                        item.duration_ms,
                        item.width,
                        item.height,
                        item.filename,
                        item.uploader,
                        item.title,
                        item.thumbnail_mxc,
                        item.thumbnail_width,
                        item.thumbnail_height,
                        item.thumbnail_size,
                        item.blurhash,
                        int(time.time()),
                    ),
                )
        except sqlite3.Error as exc:
            log.warning("media cache write failed: %s", exc)
