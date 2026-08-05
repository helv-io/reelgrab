"""Tests for downloader utilities (mime, convert args, pick helpers)."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from reelgrab.config import ConvertConfig, DownloadConfig, parse_config_dict
from reelgrab.downloader import (
    DownloadError,
    _pick_file,
    build_convert_args,
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

    def test_build_convert_args_defaults(self) -> None:
        src = Path("/tmp/in.webm")
        dest = Path("/tmp/out.mp4")
        args = build_convert_args(src, dest, ConvertConfig())
        self.assertEqual(args[0], "ffmpeg")
        self.assertIn("-c:v", args)
        self.assertIn("libx264", args)
        self.assertIn("-c:a", args)
        self.assertIn("aac", args)
        self.assertIn("yuv420p", args)
        self.assertIn("baseline", args)
        self.assertIn("+faststart", args)
        self.assertEqual(args[-1], str(dest))

    def test_build_convert_args_overrides(self) -> None:
        conv = ConvertConfig(
            video_codec="libx264",
            audio_bitrate="96k",
            video_crf=28,
            profile="main",
            level="4.0",
            max_width=720,
            max_height=720,
            extra_args=["-bf", "0"],
        )
        args = build_convert_args(Path("a.mp4"), Path("b.mp4"), conv)
        self.assertIn("96k", args)
        self.assertIn("28", args)
        self.assertIn("main", args)
        self.assertIn("4.0", args)
        joined = " ".join(args)
        self.assertIn("min(720,iw)", joined)
        self.assertIn("-bf", args)
        self.assertIn("0", args)

    def test_convert_config_from_yaml(self) -> None:
        cfg = parse_config_dict(
            {
                "download": {
                    "convert": {
                        "enabled": True,
                        "force": False,
                        "video_crf": 20,
                        "max_width": 1080,
                    }
                }
            }
        )
        self.assertIsInstance(cfg.download, DownloadConfig)
        self.assertFalse(cfg.download.convert.force)
        self.assertEqual(cfg.download.convert.video_crf, 20)
        self.assertEqual(cfg.download.convert.max_width, 1080)
        # defaults preserved
        self.assertEqual(cfg.download.convert.video_codec, "libx264")


if __name__ == "__main__":
    unittest.main()
