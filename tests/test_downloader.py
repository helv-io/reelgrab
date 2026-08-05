"""Tests for downloader factory, mime guessing, URL relatedness."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

from reelgrab.config import DownloadConfig
from reelgrab.downloader import (
    DownloadError,
    MetubeDownloader,
    YtDlpDownloader,
    get_downloader,
    guess_mime,
    urls_related,
)


class TestDownloaderUtils(unittest.TestCase):
    def test_guess_mime_mp4(self) -> None:
        self.assertEqual(guess_mime(Path("x.mp4")), "video/mp4")

    def test_guess_mime_webm(self) -> None:
        self.assertEqual(guess_mime(Path("x.webm")), "video/webm")

    def test_guess_mime_unknown(self) -> None:
        self.assertEqual(guess_mime(Path("x.xyzunknown")), "application/octet-stream")

    def test_urls_related(self) -> None:
        a = "https://instagram.com/reel/ABC?igsh=1"
        b = "https://www.instagram.com/reel/ABC/"
        self.assertTrue(urls_related(a, b))
        self.assertFalse(urls_related(a, "https://instagram.com/reel/OTHER"))

    def test_factory_ytdlp(self) -> None:
        cfg = DownloadConfig(backend="ytdlp")
        self.assertIsInstance(get_downloader(cfg), YtDlpDownloader)

    def test_factory_metube(self) -> None:
        cfg = DownloadConfig(backend="metube")
        self.assertIsInstance(get_downloader(cfg), MetubeDownloader)

    def test_factory_unknown(self) -> None:
        cfg = DownloadConfig(backend="nope")
        with self.assertRaises(DownloadError):
            get_downloader(cfg)


class TestMetubeDownloader(unittest.TestCase):
    def test_metube_add_and_finish(self) -> None:
        import asyncio

        with tempfile.TemporaryDirectory() as td:
            work = Path(td) / "work"
            work.mkdir()
            dl_root = Path(td) / "downloads"
            dl_root.mkdir()
            finished = dl_root / "clip.mp4"
            finished.write_bytes(b"video")

            cfg = DownloadConfig(
                backend="metube",
                work_dir=str(work),
                metube_url="http://metube:8081",
                metube_download_dir=str(dl_root),
                metube_poll_seconds=0,
                metube_timeout_seconds=5,
            )

            url = "https://www.instagram.com/reel/ABC/"

            class FakeResp:
                def __init__(self, status, data):
                    self.status = status
                    self._data = data

                async def json(self, content_type=None):
                    return self._data

                async def __aenter__(self):
                    return self

                async def __aexit__(self, *a):
                    return False

            class FakeSession:
                def __init__(self):
                    self.calls = 0

                async def __aenter__(self):
                    return self

                async def __aexit__(self, *a):
                    return False

                def post(self, *a, **k):
                    return FakeResp(200, {"status": "ok"})

                def get(self, *a, **k):
                    self.calls += 1
                    return FakeResp(
                        200,
                        {
                            "done": [
                                {
                                    "url": url,
                                    "filename": "clip.mp4",
                                    "error": None,
                                }
                            ],
                            "queue": [],
                        },
                    )

            async def _run():
                with patch("reelgrab.downloader.aiohttp.ClientSession", FakeSession):
                    path = await MetubeDownloader(cfg).download(url)
                    self.assertTrue(path.is_file())
                    self.assertGreater(path.stat().st_size, 0)

            asyncio.run(_run())


if __name__ == "__main__":
    unittest.main()
