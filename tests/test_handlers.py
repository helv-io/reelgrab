"""Unit tests for message URL extraction, dedupe, and pipeline with fakes."""

from __future__ import annotations

import asyncio
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from reelgrab.commands import room_allowed_effective
from reelgrab.config import AppConfig, BotConfig, UrlPatternsConfig
from reelgrab.handlers import (
    DedupeCache,
    _background_tasks,
    extract_urls_from_message,
    handle_message,
)
from reelgrab.state import StateStore

AMPLIFY = (
    "https://video.twimg.com/amplify_video/2102222769186537472/"
    "vid/avc1/3840x2160/lweKF1l9KuqH6_Jl.mp4?tag=29"
)


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
            "!reel https://www.instagram.com/reel/ABC123/",
            cfg,
            auto=False,
        )
        self.assertEqual(len(urls), 1)

    def test_command_prefix_legacy_ig(self) -> None:
        cfg = _cfg(auto=False)
        # !ig is no longer a listen prefix. With auto off, nothing is grabbed.
        urls = extract_urls_from_message(
            "!ig https://www.instagram.com/reel/ABC123/",
            cfg,
            auto=False,
        )
        self.assertEqual(urls, [])

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

    def test_dedupe_disabled_when_ttl_zero(self) -> None:
        d = DedupeCache(0)
        room = "!r:example.com"
        url = "https://www.instagram.com/reel/ABC/"
        d.mark(room, url)
        self.assertFalse(d.already_done(room, url))

    def test_force_prefix_accepts_plain_https(self) -> None:
        cfg = _cfg(auto=False)
        urls = extract_urls_from_message(
            "!reel https://example.com/not-a-default-pattern",
            cfg,
            auto=False,
        )
        self.assertEqual(urls, ["https://example.com/not-a-default-pattern"])

    def test_force_prefix_rejects_non_http_schemes(self) -> None:
        cfg = _cfg(auto=False)
        urls = extract_urls_from_message(
            "!reel file:///tmp/evil.mp4",
            cfg,
            auto=False,
        )
        self.assertEqual(urls, [])

    def test_reel_ping_replies(self) -> None:
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
                    sender="@user:example.com",
                    body="!reel ping",
                    is_direct=True,
                )

            asyncio.run(_run())
            self.assertEqual(bot.sent_video, [])
            self.assertEqual(len(bot.sent_text), 1)
            self.assertIn("pong", bot.sent_text[0][1])

    def test_reel_arbitrary_url_from_non_admin_is_silent_in_room(self) -> None:
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
                    sender="@user:example.com",
                    body="!reel https://example.com/not-a-default-pattern",
                    is_direct=False,
                )

            asyncio.run(_run())
            self.assertEqual(bot.sent_text, [])
            self.assertEqual(bot.sent_video, [])

    def test_ping_chat_is_ignored(self) -> None:
        cfg = _cfg()
        bot = FakeBot()
        with tempfile.TemporaryDirectory() as td:
            store = StateStore(Path(td) / "s.yaml")

            async def _run() -> None:
                for body in ("ping", "ping are you there", "hello ping"):
                    await handle_message(
                        bot,
                        cfg,
                        store,
                        room_id="!r:example.com",
                        event_id="$e1",
                        sender="@user:example.com",
                        body=body,
                        is_direct=True,
                    )

            asyncio.run(_run())
            self.assertEqual(bot.sent_text, [])
            self.assertEqual(bot.sent_video, [])

    def test_reel_prefix_plus_url_grabs(self) -> None:
        self._assert_downloads(
            "!reel https://www.instagram.com/reel/ABC123/",
            "https://www.instagram.com/reel/ABC123/",
            sender="@user:example.com",
            auto=False,
        )

    def test_bare_supported_url_grabs(self) -> None:
        self._assert_downloads(
            "check https://www.tiktok.com/@user/video/7123456789012345678",
            "https://www.tiktok.com/@user/video/7123456789012345678",
        )

    def test_amplify_video_url_grabs(self) -> None:
        self._assert_downloads(AMPLIFY, AMPLIFY)

    def test_reel_prefix_mid_message_grabs(self) -> None:
        self._assert_downloads(
            "please !reel https://www.youtube.com/shorts/dQw4w9WgXcQ",
            "https://www.youtube.com/shorts/dQw4w9WgXcQ",
        )

    def _assert_downloads(
        self,
        body: str,
        expected_url: str,
        sender: str = "@user:example.com",
        *,
        auto: bool = True,
    ) -> None:
        from reelgrab.downloader import MediaFile

        cfg = _cfg(auto=auto)
        bot = FakeBot()
        seen: list[str] = []
        with tempfile.TemporaryDirectory() as td:
            store = StateStore(Path(td) / "s.yaml")
            fake_file = Path(td) / "vid.mp4"
            fake_file.write_bytes(b"\x00" * 20_000)

            async def _fake_dl(url, dl_cfg):
                seen.append(url)
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
                        sender=sender,
                        body=body,
                        is_direct=False,
                        dedupe=DedupeCache(3600),
                        sem=asyncio.Semaphore(1),
                    )
                    for _ in range(50):
                        if not _background_tasks:
                            break
                        await asyncio.sleep(0.02)

            asyncio.run(_run())
            self.assertEqual(seen, [expected_url])
            self.assertEqual(len(bot.sent_video), 1)
            self.assertEqual(bot.sent_text, [])

    def test_handle_message_notifies_on_download_failure(self) -> None:
        from reelgrab.downloader import DownloadError

        cfg = _cfg()
        cfg.bot.notify_on_failure = True
        bot = FakeBot()
        with tempfile.TemporaryDirectory() as td:
            store = StateStore(Path(td) / "s.yaml")

            async def _boom(url, dl_cfg):
                raise DownloadError("nope")

            async def _run() -> None:
                with patch("reelgrab.handlers.download_url", side_effect=_boom):
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
                    await asyncio.sleep(0.1)

            asyncio.run(_run())
            self.assertEqual(bot.sent_video, [])
            self.assertEqual(len(bot.sent_text), 1)
            self.assertIn("Failed to grab media", bot.sent_text[0][1])
            self.assertIn("nope", bot.sent_text[0][1])

    def test_handle_message_silent_failure_when_notify_off(self) -> None:
        from reelgrab.downloader import DownloadError

        cfg = _cfg()
        cfg.bot.notify_on_failure = False
        bot = FakeBot()
        with tempfile.TemporaryDirectory() as td:
            store = StateStore(Path(td) / "s.yaml")

            async def _boom(url, dl_cfg):
                raise DownloadError("nope")

            async def _run() -> None:
                with patch("reelgrab.handlers.download_url", side_effect=_boom):
                    await handle_message(
                        bot,
                        cfg,
                        store,
                        room_id="!r:example.com",
                        event_id="$e1",
                        sender="@user:example.com",
                        body="https://www.instagram.com/reel/ABC123/",
                        dedupe=DedupeCache(60),
                        sem=asyncio.Semaphore(1),
                    )
                    await asyncio.sleep(0.1)

            asyncio.run(_run())
            self.assertEqual(bot.sent_text, [])
            self.assertEqual(bot.sent_video, [])


if __name__ == "__main__":
    unittest.main()
