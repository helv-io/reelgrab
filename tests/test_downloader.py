"""Tests for downloader utilities (mime, convert args, pick helpers)."""

from __future__ import annotations

import asyncio
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from reelgrab.config import ConvertConfig, DownloadConfig, parse_config_dict
from reelgrab.downloader import (
    DownloadError,
    _pick_file,
    build_convert_args,
    cleanup_job_dir,
    download_url,
    fetch_direct_mp4,
    guess_mime,
)

AMPLIFY = (
    "https://video.twimg.com/amplify_video/2102222769186537472/"
    "vid/avc1/3840x2160/lweKF1l9KuqH6_Jl.mp4?tag=29"
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

    def test_pick_file_uses_preferred_and_merge_fmt(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            job = Path(td)
            preferred = job / "clip.webm"
            merged = job / "clip.mp4"
            preferred.write_bytes(b"x" * 100)  # below MIN_BYTES
            merged.write_bytes(b"y" * 20_000)
            picked = _pick_file(job, preferred=preferred, merge_fmt="mp4")
            self.assertEqual(picked, merged)

    def test_cleanup_job_dir(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            job = Path(td) / "job_abc123"
            job.mkdir()
            (job / "partial.mp4").write_bytes(b"x" * 100)
            cleanup_job_dir(job)
            self.assertFalse(job.exists())

    def test_download_url_cleans_job_dir_on_failure(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            work = Path(td) / "downloads"
            cfg = DownloadConfig(work_dir=str(work), cookies_file=str(Path(td) / "nope.txt"))
            saw_job = {"ok": False}

            class Boom:
                def __enter__(self):
                    return self

                def __exit__(self, *args):
                    return False

                def extract_info(self, url, download=True):
                    # Simulate yt-dlp creating junk then failing.
                    jobs = list(work.glob("job_*"))
                    if jobs:
                        saw_job["ok"] = True
                        (jobs[0] / "junk.part").write_bytes(b"partial")
                    raise RuntimeError("network down")

            async def _run() -> None:
                with patch("yt_dlp.YoutubeDL", return_value=Boom()):
                    with self.assertRaises(DownloadError):
                        await download_url("https://example.com/reel/x", cfg)

            asyncio.run(_run())
            self.assertTrue(saw_job["ok"])
            self.assertTrue(work.is_dir())
            self.assertEqual(list(work.glob("job_*")), [])

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
        self.assertIn("high", args)
        self.assertIn("4.0", args)
        self.assertIn("26", args)
        self.assertIn("2500k", args)
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

    def test_legacy_format_prefers_h264(self) -> None:
        from reelgrab.downloader import bitrate_for_target, quality_ladder, resolve_ytdlp_format

        chosen = resolve_ytdlp_format("bv*+ba/b")
        self.assertIn("avc1", chosen)
        self.assertIn("mp4a", chosen)
        self.assertEqual(resolve_ytdlp_format("worst"), "worst")
        rate = bitrate_for_target(60_000, 5_000_000)
        self.assertTrue(rate.endswith("k"))
        steps = quality_ladder(
            ConvertConfig(video_crf=23, profile="baseline", level="3.1"),
            duration_ms=60_000,
            target_bytes=5_000_000,
        )
        self.assertGreaterEqual(len(steps), 2)
        self.assertEqual(steps[0].video_crf, 23)
        self.assertEqual(steps[0].profile, "baseline")
        self.assertGreater(steps[-1].video_crf, steps[0].video_crf)

    def test_legacy_convert_settings_round_trip(self) -> None:
        cfg = parse_config_dict(
            {
                "download": {
                    "format": "bv*+ba/b",
                    "convert": {
                        "force": True,
                        "video_crf": 23,
                        "profile": "baseline",
                        "level": "3.1",
                    },
                }
            }
        )
        self.assertTrue(cfg.download.convert.force)
        self.assertEqual(cfg.download.convert.video_crf, 23)
        self.assertEqual(cfg.download.convert.profile, "baseline")
        self.assertEqual(cfg.download.convert.level, "3.1")
        self.assertEqual(cfg.download.format, "bv*+ba/b")

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

    def test_fetch_direct_mp4_local_server(self) -> None:
        import threading
        from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

        payload = b"v" * 20_000

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self) -> None:  # noqa: N802
                self.send_response(200)
                self.send_header("Content-Type", "video/mp4")
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

            def log_message(self, fmt: str, *args) -> None:
                return

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        port = server.server_address[1]
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            with tempfile.TemporaryDirectory() as td:
                dest = Path(td) / "source.mp4"
                url = f"http://127.0.0.1:{port}/amplify_video/1/vid/avc1/2x2/a.mp4"
                got = fetch_direct_mp4(
                    url, dest, allowed_hosts=frozenset({"127.0.0.1"})
                )
                self.assertEqual(got.read_bytes(), payload)
        finally:
            server.shutdown()
            server.server_close()

    def test_fetch_direct_mp4_refuses_off_host_redirect(self) -> None:
        import threading
        from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self) -> None:  # noqa: N802
                self.send_response(302)
                self.send_header("Location", "https://example.com/evil.mp4")
                self.end_headers()

            def log_message(self, fmt: str, *args) -> None:
                return

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        port = server.server_address[1]
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            with tempfile.TemporaryDirectory() as td:
                dest = Path(td) / "source.mp4"
                url = f"http://127.0.0.1:{port}/amplify_video/1/a.mp4"
                with self.assertRaises(DownloadError):
                    fetch_direct_mp4(url, dest, allowed_hosts=frozenset({"127.0.0.1"}))
                self.assertFalse(dest.exists())
        finally:
            server.shutdown()
            server.server_close()

    def test_fetch_direct_mp4_rejects_other_hosts(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            dest = Path(td) / "source.mp4"
            with self.assertRaises(DownloadError):
                fetch_direct_mp4("https://example.com/a.mp4", dest)

    def test_amplify_url_uses_direct_fetch_not_ytdlp(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            cfg = DownloadConfig(
                work_dir=str(Path(td) / "downloads"),
                cookies_file=str(Path(td) / "nope.txt"),
                convert=ConvertConfig(enabled=False),
            )

            def fake_fetch(url: str, dest: Path, **kwargs):
                self.assertEqual(url, AMPLIFY)
                dest.write_bytes(b"m" * 20_000)
                return dest

            async def _run() -> None:
                with (
                    patch("reelgrab.downloader.fetch_direct_mp4", side_effect=fake_fetch),
                    patch("yt_dlp.YoutubeDL") as ydl,
                    patch("reelgrab.downloader.probe_full") as probe,
                    patch("reelgrab.downloader.make_thumbnail", return_value=None),
                ):
                    from reelgrab.downloader import ProbeInfo

                    probe.return_value = ProbeInfo(duration_ms=2500, width=320, height=180)
                    media = await download_url(AMPLIFY, cfg)
                    ydl.assert_not_called()
                self.assertGreaterEqual(media.size, 8_192)
                self.assertEqual(media.mime, "video/mp4")
                self.assertTrue(str(media.path).endswith("source.mp4"))

            asyncio.run(_run())


if __name__ == "__main__":
    unittest.main()
