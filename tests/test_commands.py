"""Tests for admin command parsing and allow-list helpers."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock

from reelgrab.commands import (
    effective_auto,
    grab_urls_from_command,
    handle_command,
    is_admin,
    parse_command,
    room_allowed_effective,
)
from reelgrab.config import (
    AppConfig,
    AppserviceBotConfig,
    AppserviceConfig,
    BotConfig,
    HomeserverConfig,
)
from reelgrab.state import StateStore


def _cfg(**kwargs) -> AppConfig:
    return AppConfig(
        homeserver=HomeserverConfig(domain="example.com"),
        appservice=AppserviceConfig(
            bot=AppserviceBotConfig(username="reelgrab"),
            as_token="x",
            hs_token="y",
        ),
        bot=BotConfig(
            admin_users=kwargs.get("admins", ["@admin:example.com"]),
            auto_download=kwargs.get("auto", True),
            allowed_rooms=kwargs.get("rooms", []),
            command_prefix=kwargs.get("prefix", "!grab"),
        ),
    )


class TestCommands(unittest.TestCase):
    def test_parse_help(self) -> None:
        cfg = _cfg()
        self.assertEqual(parse_command("help", cfg), ("help", []))
        self.assertEqual(parse_command("!status", cfg), ("status", []))
        self.assertEqual(parse_command("reelgrab rooms", cfg), ("rooms", []))

    def test_parse_grab_prefix(self) -> None:
        cfg = _cfg()
        cmd = parse_command("!grab https://instagram.com/reel/ABC/", cfg)
        self.assertIsNotNone(cmd)
        assert cmd is not None
        self.assertEqual(cmd[0], "grab")

    def test_parse_legacy_ig_prefix(self) -> None:
        cfg = _cfg()
        cmd = parse_command("!ig https://instagram.com/reel/ABC/", cfg)
        self.assertIsNotNone(cmd)
        assert cmd is not None
        self.assertEqual(cmd[0], "grab")

    def test_parse_unknown(self) -> None:
        cfg = _cfg()
        self.assertIsNone(parse_command("hello world", cfg))

    def test_admin(self) -> None:
        cfg = _cfg()
        self.assertTrue(is_admin("@admin:example.com", cfg))
        self.assertFalse(is_admin("@other:example.com", cfg))

    def test_state_auto_override(self) -> None:
        cfg = _cfg(auto=True)
        with tempfile.TemporaryDirectory() as td:
            store = StateStore(Path(td) / "state.yaml")
            self.assertTrue(effective_auto(cfg, store))
            store.update(auto_download=False)
            self.assertFalse(effective_auto(cfg, store))

    def test_allow_list_runtime(self) -> None:
        cfg = _cfg(rooms=[])
        with tempfile.TemporaryDirectory() as td:
            store = StateStore(Path(td) / "state.yaml")
            self.assertTrue(room_allowed_effective("!a:example.com", cfg, store))
            store.update(allowed_rooms=["!b:example.com"])
            self.assertFalse(room_allowed_effective("!a:example.com", cfg, store))
            self.assertTrue(room_allowed_effective("!b:example.com", cfg, store))

    def test_grab_urls_from_command(self) -> None:
        cfg = _cfg()
        urls = grab_urls_from_command(
            "grab",
            ["https://www.instagram.com/reel/ABC123/"],
            "",
            cfg,
        )
        self.assertIsNotNone(urls)
        assert urls is not None
        self.assertEqual(len(urls), 1)

    def test_handle_ping(self) -> None:
        import asyncio

        cfg = _cfg()
        bot = AsyncMock()
        bot.user_id = "@reelgrab:example.com"
        bot.joined_room_ids = lambda: []
        with tempfile.TemporaryDirectory() as td:
            store = StateStore(Path(td) / "s.yaml")

            async def _run() -> None:
                ok = await handle_command(
                    bot,
                    cfg,
                    store,
                    room_id="!r:example.com",
                    event_id="$e",
                    sender="@admin:example.com",
                    cmd="ping",
                    args=[],
                    is_direct=True,
                )
                self.assertTrue(ok)
                bot.send_text.assert_awaited()

            asyncio.run(_run())


if __name__ == "__main__":
    unittest.main()
