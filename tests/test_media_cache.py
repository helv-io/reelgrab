"""Persistent URL → mxc cache."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from reelgrab.media_cache import CachedMedia, MediaCache


class TestMediaCache(unittest.TestCase):
    def test_round_trip_canonical_url(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            cache = MediaCache(Path(td) / "media_cache.sqlite")
            item = CachedMedia(
                url_key="",
                source_url="https://www.instagram.com/reel/ABC/?igsh=1",
                mxc="mxc://example.com/vid",
                mime="video/mp4",
                size=1234,
                duration_ms=2500,
                width=720,
                height=1280,
                filename="ABC.mp4",
                uploader="clips",
                title="hello",
                thumbnail_mxc="mxc://example.com/thumb",
                thumbnail_width=640,
                thumbnail_height=1138,
                thumbnail_size=4000,
                blurhash=None,
                description="A fox at dawn.",
            )
            cache.put("https://www.instagram.com/reel/ABC/?igsh=1", item)
            again = MediaCache(Path(td) / "media_cache.sqlite")
            got = again.get("https://instagram.com/reel/ABC/?utm=2")
            self.assertIsNotNone(got)
            assert got is not None
            self.assertEqual(got.mxc, "mxc://example.com/vid")
            self.assertEqual(got.filename, "ABC.mp4")
            self.assertEqual(got.uploader, "clips")
            self.assertEqual(got.thumbnail_height, 1138)
            self.assertEqual(got.description, "A fox at dawn.")

    def test_legacy_row_without_description(self) -> None:
        import sqlite3

        from reelgrab.urls import canonicalize_url

        url = "https://www.instagram.com/reel/OLD/"
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "media_cache.sqlite"
            conn = sqlite3.connect(path)
            conn.execute(
                """
                CREATE TABLE media (
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
            )
            conn.execute(
                """
                INSERT INTO media (url_key, source_url, mxc, filename, title, created_at)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (canonicalize_url(url), url, "mxc://example.com/old", "OLD.mp4", "Again", 1),
            )
            conn.commit()
            conn.close()

            got = MediaCache(path).get(url)
            self.assertIsNotNone(got)
            assert got is not None
            self.assertEqual(got.mxc, "mxc://example.com/old")
            self.assertEqual(got.title, "Again")
            self.assertIsNone(got.description)

            got.description = "now known"
            MediaCache(path).put(url, got)
            again = MediaCache(path).get(url)
            self.assertIsNotNone(again)
            assert again is not None
            self.assertEqual(again.description, "now known")
            self.assertEqual(again.mxc, "mxc://example.com/old")

    def test_miss(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            cache = MediaCache(Path(td) / "media_cache.sqlite")
            self.assertIsNone(cache.get("https://example.com/reel/nope"))


if __name__ == "__main__":
    unittest.main()
