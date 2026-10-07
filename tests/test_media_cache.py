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

    def test_miss(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            cache = MediaCache(Path(td) / "media_cache.sqlite")
            self.assertIsNone(cache.get("https://example.com/reel/nope"))


if __name__ == "__main__":
    unittest.main()
