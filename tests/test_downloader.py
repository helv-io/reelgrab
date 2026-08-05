"""Tests for downloader utilities (mime, probe helpers)."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from reelgrab.downloader import (
    DownloadError,
    _pick_file,
    guess_mime,
)


class TestDownloaderUtils(unittest.TestCase):
    def test_guess_mime_mp4(self) -> None:
        self.assertEqual(guess_mime(Path("x.mp4")), "video/mp4")

    def test_guess_mime_webm(self) -> None:
        self.assertEqual(guess_mime(Path("x.webm")), "video/webm")

    def test_guess_mime_unknown(self) -> None:
        self.assertEqual(guess_mime(Path("x.xyzunknown")), "application/octet-stream")

    def test_pick_file_prefers_largest_video(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            job = Path(td)
            small = job / "a.mp4"
            big = job / "b.mp4"
            small.write_bytes(b"x" * 10_000)
            big.write_bytes(b"y" * 50_000)
            (job / "note.txt").write_text("hi")
            picked = _pick_file(job, preferred=None, merge_fmt="mp4")
            self.assertEqual(picked.name, "b.mp4")

    def test_pick_file_rejects_empty(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            job = Path(td)
            (job / "empty.mp4").write_bytes(b"")
            with self.assertRaises(DownloadError):
                _pick_file(job, preferred=None, merge_fmt="mp4")

if __name__ == "__main__":
    unittest.main()
