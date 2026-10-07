"""Matrix client paths: filtering, reactions, startup retry, avatar, health."""

from __future__ import annotations

import asyncio
import logging
import tempfile
import unittest
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, patch

from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from reelgrab.appservice import AppserviceNotReady, AppserviceServer
from reelgrab.config import (
    AppConfig,
    AppserviceBotConfig,
    AppserviceConfig,
    BotConfig,
    HomeserverConfig,
)
from reelgrab.matrix_client import REACT_DONE, MatrixBot


def _cfg(data_dir: Path, hs: str, *, avatar: str = "") -> AppConfig:
    cfg = AppConfig(
        homeserver=HomeserverConfig(address=hs, domain="example.com"),
        appservice=AppserviceConfig(
            as_token="as" * 16,
            hs_token="hs" * 16,
            address="http://reelgrab:29399",
            bot=AppserviceBotConfig(username="reelgrab", avatar=avatar, displayname="Reelgrab"),
        ),
        bot=BotConfig(admin_users=["@admin:example.com"], ignore_history=True),
    )
    cfg.data_dir = data_dir
    return cfg


class _FakeHS:
    def __init__(self) -> None:
        self.fail_versions = 0
        self.uploads = 0
        self.sent: list[dict[str, Any]] = []
        self.paths: list[str] = []
        self.avatar_url: str | None = None

    async def handler(self, request: web.Request) -> web.Response:
        self.paths.append(f"{request.method} {request.path}")
        path = request.path
        if path.endswith("/versions"):
            if self.fail_versions:
                self.fail_versions -= 1
                return web.Response(status=503, text="booting")
            return web.json_response({"versions": ["v1.11"]})
        if path.endswith("/register"):
            return web.json_response({"user_id": "@reelgrab:example.com"})
        if path.endswith("/media/config") or path.endswith("/config"):
            return web.json_response({"m.upload.size": 50_000_000})
        if "/profile/" in path and request.method == "GET":
            return web.json_response({"avatar_url": self.avatar_url, "displayname": "Reelgrab"})
        if path.endswith("/displayname") and request.method == "PUT":
            return web.json_response({})
        if path.endswith("/avatar_url") and request.method == "PUT":
            body = await request.json()
            self.avatar_url = body.get("avatar_url")
            return web.json_response({})
        if path.endswith("/upload") and "/keys/" not in path:
            self.uploads += 1
            return web.json_response({"content_uri": f"mxc://example.com/up{self.uploads}"})
        if path.endswith("/joined_rooms"):
            return web.json_response({"joined_rooms": ["!r:example.com"]})
        if "/send/" in path and request.method == "PUT":
            body = await request.json()
            self.sent.append({"path": path, "body": body})
            return web.json_response({"event_id": f"$sent{len(self.sent)}"})
        if "/redact/" in path:
            return web.json_response({"event_id": "$redacted"})
        if path.endswith("/joined_members"):
            return web.json_response(
                {"joined": {"@reelgrab:example.com": {}, "@user:example.com": {}}}
            )
        return web.json_response({})


class TestMatrixClient(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.hs = _FakeHS()
        app = web.Application()
        app.router.add_route("*", "/{tail:.*}", self.hs.handler)
        self.server = TestServer(app)
        self.client = TestClient(self.server)
        await self.client.start_server()
        self.tmp = tempfile.TemporaryDirectory()
        base = str(self.client.make_url("")).rstrip("/")
        self.cfg = _cfg(Path(self.tmp.name), base, avatar="")
        self.bot = MatrixBot(self.cfg)

    async def asyncTearDown(self) -> None:
        await self.bot.close()
        await self.client.close()
        self.tmp.cleanup()

    async def test_startup_retries_until_homeserver_answers(self) -> None:
        self.hs.fail_versions = 2

        async def _no_sleep(_delay: float) -> None:
            return None

        with patch("reelgrab.matrix_client.asyncio.sleep", _no_sleep):
            await self.bot.start()
        self.assertTrue(self.bot.ready)
        self.assertEqual(self.bot.max_upload_bytes, 50_000_000)
        versions = [p for p in self.hs.paths if p.endswith("/versions")]
        self.assertEqual(len(versions), 3)
        self.assertEqual(self.bot.joined_room_ids(), ["!r:example.com"])

    async def test_unrelated_chat_does_not_fetch_members_or_log_body(self) -> None:
        self.bot._ready = True
        self.bot._message_handler = AsyncMock()
        event = {
            "type": "m.room.message",
            "room_id": "!r:example.com",
            "event_id": "$e",
            "sender": "@user:example.com",
            "origin_server_ts": int(asyncio.get_event_loop().time() * 1000) + 10**15,
            "content": {"msgtype": "m.text", "body": "secret family plans for dinner"},
        }
        captured: list[str] = []

        class _Grab(logging.Handler):
            def emit(self, record: logging.LogRecord) -> None:
                captured.append(record.getMessage())

        handler = _Grab()
        logger = logging.getLogger("reelgrab.matrix")
        logger.addHandler(handler)
        previous = logger.level
        logger.setLevel(logging.DEBUG)
        try:
            await self.bot._handle_one_event(event)
        finally:
            logger.removeHandler(handler)
            logger.setLevel(previous)
        self.bot._message_handler.assert_not_called()
        joined = "\n".join(self.hs.paths)
        self.assertNotIn("joined_members", joined)
        blob = "\n".join(captured)
        self.assertNotIn("secret family", blob)

    async def test_link_logs_url_only_and_strips_reply_fallback(self) -> None:
        self.bot._ready = True
        self.bot._started_ms = 1
        seen: dict[str, Any] = {}

        async def _handler(**kwargs: Any) -> None:
            seen.update(kwargs)

        self.bot._message_handler = _handler
        quoted = "https://www.instagram.com/reel/QUOTED/"
        actual = "https://www.instagram.com/reel/ACTUAL/"
        event = {
            "type": "m.room.message",
            "room_id": "!r:example.com",
            "event_id": "$e",
            "sender": "@user:example.com",
            "origin_server_ts": 10_000,
            "content": {
                "msgtype": "m.text",
                "body": f"> <@a:example.com> secret {quoted}\n\nplease grab {actual}",
                "m.relates_to": {
                    "rel_type": "m.thread",
                    "event_id": "$root",
                    "m.in_reply_to": {"event_id": "$parent"},
                },
            },
        }
        with self.assertLogs("reelgrab.matrix", level="INFO") as logs:
            await self.bot._handle_one_event(event)
        blob = "\n".join(logs.output)
        self.assertIn(actual, blob)
        self.assertNotIn("secret", blob)
        self.assertNotIn("QUOTED", seen["body"])
        self.assertIn("ACTUAL", seen["body"])
        self.assertEqual(seen["thread_root_event_id"], "$root")
        self.assertTrue(seen["is_reply"])

    async def test_ignores_edits_notices_and_history(self) -> None:
        self.bot._ready = True
        self.bot._started_ms = 1_000_000
        self.bot._message_handler = AsyncMock()
        edit = {
            "type": "m.room.message",
            "room_id": "!r:example.com",
            "event_id": "$edit",
            "sender": "@user:example.com",
            "origin_server_ts": 2_000_000,
            "content": {
                "msgtype": "m.text",
                "body": "https://www.instagram.com/reel/ABC/",
                "m.new_content": {"body": "https://www.instagram.com/reel/ABC/"},
                "m.relates_to": {"rel_type": "m.replace", "event_id": "$old"},
            },
        }
        notice = {
            "type": "m.room.message",
            "room_id": "!r:example.com",
            "event_id": "$n",
            "sender": "@user:example.com",
            "origin_server_ts": 2_000_000,
            "content": {
                "msgtype": "m.notice",
                "body": "https://www.instagram.com/reel/ABC/",
            },
        }
        old = {
            "type": "m.room.message",
            "room_id": "!r:example.com",
            "event_id": "$oldmsg",
            "sender": "@user:example.com",
            "origin_server_ts": 1_000,
            "content": {
                "msgtype": "m.text",
                "body": "https://www.instagram.com/reel/ABC/",
            },
        }
        await self.bot._handle_one_event(edit)
        await self.bot._handle_one_event(notice)
        await self.bot._handle_one_event(old)
        self.bot._message_handler.assert_not_called()

    async def test_send_video_sets_filename_reaction_and_thread(self) -> None:
        await self.bot.start()
        with tempfile.NamedTemporaryFile(suffix=".mp4") as tmp:
            tmp.write(b"v" * 20)
            tmp.flush()
            await self.bot.send_reaction("!r:example.com", "$src", "⏳")
            await self.bot.send_video(
                "!r:example.com",
                "mxc://example.com/vid",
                Path(tmp.name),
                caption="@clips: Title · https://example.com/v",
                filename="clip.mp4",
                mime="video/mp4",
                size=20,
                width=1080,
                height=1920,
                thumbnail_mxc="mxc://example.com/t",
                thumbnail_width=640,
                thumbnail_height=1138,
                reply_to_event_id="$src",
                thread_root_event_id="$root",
            )
            await self.bot.redact_event("!r:example.com", "$sent1")
            await self.bot.send_reaction("!r:example.com", "$src", REACT_DONE)
        bodies = [item["body"] for item in self.hs.sent]
        video = next(b for b in bodies if b.get("msgtype") == "m.video")
        self.assertEqual(video["filename"], "clip.mp4")
        self.assertIn("@clips", video["body"])
        self.assertEqual(video["info"]["thumbnail_info"]["w"], 640)
        self.assertEqual(video["info"]["thumbnail_info"]["h"], 1138)
        self.assertEqual(video["m.relates_to"]["rel_type"], "m.thread")
        reaction = next(
            b
            for b in bodies
            if "m.relates_to" in b and b["m.relates_to"].get("rel_type") == "m.annotation"
        )
        self.assertEqual(reaction["m.relates_to"]["key"], "⏳")

    async def test_not_ready_raises_for_transactions(self) -> None:
        self.bot._ready = False
        with self.assertRaises(AppserviceNotReady):
            await self.bot.handle_appservice_events([{"type": "m.room.message"}])

    async def test_avatar_is_not_reuploaded_when_unchanged(self) -> None:
        icon = Path(self.tmp.name) / "icon.jpg"
        icon.write_bytes(b"\xff\xd8" + b"avatar-bytes" + b"\x00" * 32)
        self.cfg.appservice.bot.avatar = "icon.jpg"
        await self.bot.start()
        self.assertEqual(self.hs.uploads, 1)
        await self.bot.close()
        bot2 = MatrixBot(self.cfg)
        try:
            await bot2.start()
            self.assertEqual(self.hs.uploads, 1)
            self.assertEqual(self.hs.avatar_url, "mxc://example.com/up1")
        finally:
            await bot2.close()


class TestHealthAndTxn(unittest.IsolatedAsyncioTestCase):
    async def test_health_reflects_readiness_and_txn_retries(self) -> None:
        cfg = AppConfig(
            homeserver=HomeserverConfig(address="http://hs:8008", domain="example.com"),
            appservice=AppserviceConfig(
                as_token="as" * 16,
                hs_token="hs" * 16,
                hostname="127.0.0.1",
                port=29399,
                address="http://reelgrab:29399",
            ),
        )
        ready = {"ok": False}
        received: list[int] = []

        async def on_events(events: list[dict[str, Any]]) -> None:
            if not ready["ok"]:
                raise AppserviceNotReady("booting")
            received.append(len(events))

        server = AppserviceServer(cfg, on_events=on_events)
        server.set_ready_check(lambda: bool(ready["ok"]))
        aio_server = TestServer(server.app)
        client = TestClient(aio_server)
        await client.start_server()
        try:
            health = await client.get("/health")
            self.assertEqual(health.status, 503)
            payload = {
                "events": [
                    {
                        "type": "m.room.message",
                        "room_id": "!r:example.com",
                        "event_id": "$e",
                        "sender": "@u:example.com",
                        "content": {"msgtype": "m.text", "body": "hi"},
                    }
                ]
            }
            headers = {"Authorization": f"Bearer {cfg.hs_token}"}
            first = await client.put(
                "/_matrix/app/v1/transactions/txn-boot",
                json=payload,
                headers=headers,
            )
            self.assertEqual(first.status, 503)
            ready["ok"] = True
            second = await client.put(
                "/_matrix/app/v1/transactions/txn-boot",
                json=payload,
                headers=headers,
            )
            self.assertEqual(second.status, 200)
            self.assertEqual(received, [1])
            healthy = await client.get("/health")
            self.assertEqual(healthy.status, 200)
            data = await healthy.json()
            self.assertTrue(data["ready"])
        finally:
            await client.close()


if __name__ == "__main__":
    unittest.main()
