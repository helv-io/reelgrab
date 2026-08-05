"""Unit tests for message URL extraction, dedupe, and pipeline with fakes."""

from __future__ import annotations

import asyncio
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from reelgrab.commands import room_allowed_effective
from reelgrab.config import AppConfig, BotConfig, UrlPatternsConfig
from reelgrab.handlers import (
    DedupeCache,
    extract_urls_from_message,
    handle_message,
)
from reelgrab.state import StateStore


def _cfg(
    *,
    auto: bool = True,
    prefix: str = "!grab",
    rooms: list[str] | None = None,
    admins: list[str] | None = None,
) -> AppConfig:
    return AppConfig(
        bot=BotConfig(
            auto_download=auto,
            command_prefix=prefix,
            allowed_rooms=rooms or [],
            admin_users=admins or ["@admin:example.com"],
        ),
        urls=UrlPatternsConfig(),
    )


class FakeBot:
    def __init__(self) -> None:
        self.user_id = "@reelgrab:example.com"
        self.sent_text: list[tuple] = []
        self.sent_video: list[tuple] = []
        self.uploads: list[Path] = []

    def joined_room_ids(self) -> list[str]:
        return ["!r:example.com"]

    async def upload_media(self, path: Path, mime: str | None = None) -> str:
        self.uploads.append(path)
        return "mxc://example.com/abc"

    async def send_video(self, room_id, mxc, path, **kwargs) -> None:
        self.sent_video.append((room_id, mxc, path, kwargs))

    async def send_text(self, room_id, body, **kwargs) -> None:
        # formatted_body optional
        self.sent_text.append((room_id, body, kwargs))


class TestHandlers(unittest.TestCase):
    def test_auto_finds_ig(self) -> None:
        cfg = _cfg()
        urls = extract_urls_from_message(
            "check https://www.instagram.com/reel/ABC123/",
            cfg,
            auto=True,
        )
        self.assertEqual(len(urls), 1)

    def test_auto_off_ignores(self) -> None:
        cfg = _cfg(auto=False)
        urls = extract_urls_from_message(
            "https://www.instagram.com/reel/ABC123/",
            cfg,
            auto=False,
        )
        self.assertEqual(urls, [])

    def test_command_prefix_grab(self) -> None:
        cfg = _cfg(auto=False)
        urls = extract_urls_from_message(
            "!grab https://www.instagram.com/reel/ABC123/",
            cfg,
            auto=False,
        )
        self.assertEqual(len(urls), 1)

    def test_command_prefix_legacy_ig(self) -> None:
        cfg = _cfg(auto=False)
        urls = extract_urls_from_message(
            "!ig https://www.instagram.com/reel/ABC123/",
            cfg,
            auto=False,
        )
        self.assertEqual(len(urls), 1)

    def test_room_allowed_empty_means_all(self) -> None:
        cfg = _cfg(rooms=[])
        with tempfile.TemporaryDirectory() as td:
            store = StateStore(Path(td) / "s.yaml")
            self.assertTrue(room_allowed_effective("!foo:example.com", cfg, store))

    def test_room_allowed_list(self) -> None:
        cfg = _cfg(rooms=["!a:example.com"])
        with tempfile.TemporaryDirectory() as td:
            store = StateStore(Path(td) / "s.yaml")
            self.assertTrue(room_allowed_effective("!a:example.com", cfg, store))
            self.assertFalse(room_allowed_effective("!b:example.com", cfg, store))

    def test_dedupe(self) -> None:
        d = DedupeCache(3600)
        room = "!r:example.com"
        url = "https://www.instagram.com/reel/ABC/?igsh=1"
        self.assertFalse(d.already_done(room, url))
        d.mark(room, url)
        self.assertTrue(
            d.already_done(room, "https://instagram.com/reel/ABC/?utm=2")
        )

    def test_handle_message_auto_download_pipeline(self) -> None:
        from reelgrab.downloader import MediaFile

        cfg = _cfg()
        bot = FakeBot()
        with tempfile.TemporaryDirectory() as td:
            store = StateStore(Path(td) / "s.yaml")
            fake_file = Path(td) / "vid.mp4"
            fake_file.write_bytes(b"\x00" * 20_000)

            async def _fake_dl(url, dl_cfg):
                return MediaFile(
                    path=fake_file,
                    mime="video/mp4",
                    size=fake_file.stat().st_size,
                    duration_ms=2500,
                    width=720,
                    height=1280,
                )

            async def _run() -> None:
                with patch("reelgrab.handlers.download_url", side_effect=_fake_dl):
                    await handle_message(
                        bot,
                        cfg,
                        store,
                        room_id="!r:example.com",
                        event_id="$e1",
                        sender="@user:example.com",
                        body="https://www.instagram.com/reel/ABC123/",
                        is_direct=False,
                        dedupe=DedupeCache(3600),
                        sem=asyncio.Semaphore(1),
                    )
                    # tasks are fire-and-forget
                    await asyncio.sleep(0.1)

            asyncio.run(_run())
            self.assertEqual(len(bot.sent_video), 1)
            self.assertEqual(bot.sent_video[0][1], "mxc://example.com/abc")
            # Quiet success path: video only, no progress / caption notices.
            self.assertEqual(bot.sent_text, [])
            caption = bot.sent_video[0][3].get("caption")
            self.assertEqual(caption, "vid.mp4")

    def test_handle_message_ignores_self(self) -> None:
        cfg = _cfg()
        bot = FakeBot()
        with tempfile.TemporaryDirectory() as td:
            store = StateStore(Path(td) / "s.yaml")

            async def _run() -> None:
                await handle_message(
                    bot,
                    cfg,
                    store,
                    room_id="!r:example.com",
                    event_id="$e1",
                    sender="@reelgrab:example.com",
                    body="https://www.instagram.com/reel/ABC123/",
                )

            asyncio.run(_run())
            self.assertEqual(bot.sent_video, [])


if __name__ == "__main__":
    unittest.main()
