"""Tests for mautrix-style appservice HTTP (transactions + auth)."""

from __future__ import annotations

import asyncio
import unittest
from typing import Any

from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from reelgrab.appservice import AppserviceServer, text_body_from_event
from reelgrab.config import AppConfig, AppserviceConfig, HomeserverConfig


def _cfg() -> AppConfig:
    return AppConfig(
        homeserver=HomeserverConfig(address="http://hs:8008", domain="example.com"),
        appservice=AppserviceConfig(
            as_token="as" * 16,
            hs_token="hs" * 16,
            hostname="127.0.0.1",
            port=29399,
            address="http://reelgrab:29399",
        ),
    )


class TestTextBody(unittest.TestCase):
    def test_text_message(self) -> None:
        body = text_body_from_event(
            {
                "type": "m.room.message",
                "content": {"msgtype": "m.text", "body": "hello"},
            }
        )
        self.assertEqual(body, "hello")

    def test_ignores_video(self) -> None:
        body = text_body_from_event(
            {
                "type": "m.room.message",
                "content": {"msgtype": "m.video", "body": "x.mp4"},
            }
        )
        self.assertIsNone(body)


class TestAppserviceHTTP(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.cfg = _cfg()
        self.received: list[list[dict[str, Any]]] = []

        async def on_events(events: list[dict[str, Any]]) -> None:
            self.received.append(events)

        self.server = AppserviceServer(self.cfg, on_events=on_events)
        self.aio_server = TestServer(self.server.app)
        self.client = TestClient(self.aio_server)
        await self.client.start_server()

    async def asyncTearDown(self) -> None:
        await self.client.close()

    async def test_rejects_bad_token(self) -> None:
        resp = await self.client.put(
            "/_matrix/app/v1/transactions/t1",
            json={"events": []},
            headers={"Authorization": "Bearer wrong"},
        )
        self.assertEqual(resp.status, 401)

    async def test_transaction_ok(self) -> None:
        events = [
            {
                "type": "m.room.message",
                "room_id": "!r:example.com",
                "event_id": "$e1",
                "sender": "@u:example.com",
                "content": {"msgtype": "m.text", "body": "hi"},
            }
        ]
        resp = await self.client.put(
            "/_matrix/app/v1/transactions/txn-1",
            json={"events": events},
            headers={"Authorization": f"Bearer {self.cfg.hs_token}"},
        )
        self.assertEqual(resp.status, 200)
        self.assertEqual(await resp.json(), {})
        self.assertEqual(len(self.received), 1)
        self.assertEqual(self.received[0][0]["event_id"], "$e1")

    async def test_duplicate_txn_idempotent(self) -> None:
        headers = {"Authorization": f"Bearer {self.cfg.hs_token}"}
        payload = {
            "events": [
                {
                    "type": "m.room.message",
                    "room_id": "!r:example.com",
                    "event_id": "$e1",
                    "sender": "@u:example.com",
                    "content": {"msgtype": "m.text", "body": "hi"},
                }
            ]
        }
        r1 = await self.client.put(
            "/_matrix/app/v1/transactions/same", json=payload, headers=headers
        )
        r2 = await self.client.put(
            "/_matrix/app/v1/transactions/same", json=payload, headers=headers
        )
        self.assertEqual(r1.status, 200)
        self.assertEqual(r2.status, 200)
        self.assertEqual(len(self.received), 1)

    async def test_query_user_claim(self) -> None:
        headers = {"Authorization": f"Bearer {self.cfg.hs_token}"}
        ok = await self.client.get(
            "/_matrix/app/v1/users/@reelgrab:example.com", headers=headers
        )
        self.assertEqual(ok.status, 200)
        bad = await self.client.get(
            "/_matrix/app/v1/users/@other:example.com", headers=headers
        )
        self.assertEqual(bad.status, 404)

    async def test_legacy_path_and_query_token(self) -> None:
        resp = await self.client.put(
            f"/transactions/legacy?access_token={self.cfg.hs_token}",
            json={"events": []},
        )
        self.assertEqual(resp.status, 200)

    async def test_health_no_auth(self) -> None:
        resp = await self.client.get("/health")
        self.assertEqual(resp.status, 200)
        data = await resp.json()
        self.assertTrue(data.get("ok"))


if __name__ == "__main__":
    unittest.main()
