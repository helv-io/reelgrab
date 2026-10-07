"""E2EE store, registration migration, encrypted-event dispatch, per-room settings."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from typing import Any

from aiohttp.test_utils import TestClient, TestServer

from reelgrab.commands import effective_auto, effective_caption, effective_notify
from reelgrab.config import (
    AppConfig,
    AppserviceConfig,
    HomeserverConfig,
    bootstrap,
    build_registration,
    parse_config_dict,
    write_default_config,
)
from reelgrab.crypto_store import SQLiteCryptoStore
from reelgrab.matrix_client import MatrixBot
from reelgrab.state import StateStore


class TestCryptoStore(unittest.IsolatedAsyncioTestCase):
    async def test_account_survives_restart(self) -> None:
        from mautrix.crypto.account import OlmAccount

        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "crypto.sqlite"
            store = SQLiteCryptoStore(path, "test-pickle-key")
            await store.open()
            account = OlmAccount()
            identity = account.identity_key
            await store.put_device_id("REELGRABTEST")
            await store.put_account(account)
            await store.close()

            again = SQLiteCryptoStore(path, "test-pickle-key")
            await again.open()
            try:
                self.assertEqual(await again.get_device_id(), "REELGRABTEST")
                loaded = await again.get_account()
                self.assertIsNotNone(loaded)
                assert loaded is not None
                self.assertEqual(loaded.identity_key, identity)
                self.assertEqual(loaded.shared, account.shared)
            finally:
                await again.close()


class TestRegistrationMigration(unittest.TestCase):
    def test_registration_keeps_url_and_sets_msc3202(self) -> None:
        cfg = parse_config_dict(
            {
                "homeserver": {"address": "http://hs:8008", "domain": "example.org"},
                "appservice": {
                    "as_token": "a" * 32,
                    "hs_token": "b" * 32,
                    "address": "http://reelgrab:29399",
                },
            }
        )
        reg = build_registration(cfg)
        self.assertEqual(reg["url"], "http://reelgrab:29399")
        self.assertIs(reg["org.matrix.msc3202"], True)
        self.assertNotEqual(reg["url"], None)

    def test_existing_registration_gains_msc3202(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            d = Path(td)
            write_default_config(d / "config.yaml")
            text = (d / "config.yaml").read_text()
            text = text.replace("domain: example.com", "domain: myserver.test")
            text = text.replace(
                "address: http://localhost:8008",
                "address: http://synapse:8008",
            )
            text = text.replace("as_token: generate", 'as_token: "aa"')
            text = text.replace("hs_token: generate", 'hs_token: "bb"')
            # Tokens must not look like placeholders. token_hex is long; use fixed.
            text = text.replace('as_token: "aa"', 'as_token: "' + ("a" * 32) + '"')
            text = text.replace('hs_token: "bb"', 'hs_token: "' + ("b" * 32) + '"')
            (d / "config.yaml").write_text(text)
            old = {
                "id": "reelgrab",
                "url": "http://reelgrab:29399",
                "as_token": "a" * 32,
                "hs_token": "b" * 32,
                "sender_localpart": "reelgrab",
                "namespaces": {
                    "users": [
                        {
                            "regex": r"^@reelgrab:myserver\.test$",
                            "exclusive": True,
                        }
                    ]
                },
            }
            import yaml

            (d / "registration.yaml").write_text(yaml.safe_dump(old), encoding="utf-8")
            result = bootstrap(data_dir=td)
            self.assertFalse(result.exit_after)
            self.assertTrue(result.created_registration)
            blob = (d / "registration.yaml").read_text()
            self.assertIn("org.matrix.msc3202: true", blob)
            self.assertIn("url: http://reelgrab:29399", blob)
            self.assertNotIn("url: null", blob)
            self.assertTrue(any("msc3202" in msg.lower() for msg in result.messages))


class TestEncryptedDispatch(unittest.IsolatedAsyncioTestCase):
    async def test_encrypted_event_is_decrypted_before_commands(self) -> None:
        cfg = AppConfig(
            homeserver=HomeserverConfig(address="http://hs:8008", domain="example.com"),
            appservice=AppserviceConfig(as_token="as" * 16, hs_token="hs" * 16),
        )
        bot = MatrixBot(cfg)
        bot._ready = True
        bot._started_ms = 1
        seen: dict[str, Any] = {}

        async def _handler(**kwargs: Any) -> None:
            seen.update(kwargs)

        bot._message_handler = _handler

        class _Machine:
            async def decrypt_megolm_event(self, evt: Any) -> Any:
                class _Decrypted:
                    def serialize(self) -> dict[str, Any]:
                        return {
                            "type": "m.room.message",
                            "room_id": evt.room_id,
                            "event_id": evt.event_id,
                            "sender": evt.sender,
                            "origin_server_ts": 10_000_000,
                            "content": {"msgtype": "m.text", "body": "!reel help"},
                        }

                return _Decrypted()

        bot._machine = _Machine()
        await bot._handle_one_event(
            {
                "type": "m.room.encrypted",
                "room_id": "!dm:example.com",
                "event_id": "$enc",
                "sender": "@user:example.com",
                "origin_server_ts": 10_000_000,
                "content": {
                    "algorithm": "m.megolm.v1.aes-sha2",
                    "ciphertext": "abc",
                    "session_id": "sess",
                },
            }
        )
        self.assertIn("!reel help", seen["body"])
        self.assertEqual(seen["event_id"], "$enc")

    async def test_send_encrypts_when_room_is_encrypted(self) -> None:
        from aiohttp import web

        sent: list[dict[str, Any]] = []

        async def handler(request: web.Request) -> web.Response:
            sent.append({"path": request.path, "body": await request.json()})
            return web.json_response({"event_id": "$out"})

        app = web.Application()
        app.router.add_route("*", "/{tail:.*}", handler)
        server = TestServer(app)
        client = TestClient(server)
        await client.start_server()
        try:
            cfg = AppConfig(
                homeserver=HomeserverConfig(
                    address=str(client.make_url("")).rstrip("/"),
                    domain="example.com",
                ),
                appservice=AppserviceConfig(as_token="as" * 16, hs_token="hs" * 16),
            )
            bot = MatrixBot(cfg)
            from aiohttp import ClientSession

            bot._session = ClientSession()
            bot._device_id = "REELGRABTEST"

            class _State:
                async def is_encrypted(self, _room_id: str) -> bool:
                    return True

            class _Crypto:
                def __init__(self) -> None:
                    self.crypto = object()
                    self.state_store = _State()

                async def encrypt(self, _room_id: str, _event_type: Any, content: dict) -> dict:
                    return {
                        "algorithm": "m.megolm.v1.aes-sha2",
                        "ciphertext": "cipher",
                        "session_id": "sess",
                        "device_id": "REELGRABTEST",
                        "plain_msgtype": content.get("msgtype"),
                    }

            bot._crypto_client = _Crypto()
            await bot.send_text("!room:example.com", "!reel help reply")
            await bot.send_reaction("!room:example.com", "$src", "⏳")
            self.assertIn("encrypted", sent[0]["path"])
            self.assertEqual(sent[0]["body"]["ciphertext"], "cipher")
            self.assertEqual(sent[0]["body"]["plain_msgtype"], "m.notice")
            self.assertIn("reaction", sent[1]["path"])
            self.assertEqual(sent[1]["body"]["m.relates_to"]["key"], "⏳")
        finally:
            await bot.close()
            await client.close()


class TestRoomSettings(unittest.TestCase):
    def test_room_override_beats_global_and_persists(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            store = StateStore(Path(td) / "runtime_state.yaml")
            cfg = parse_config_dict({"bot": {"auto_download": True, "notify_on_failure": True}})
            store.update(auto_download=True)
            store.set_room("!r:example.com", auto_download=False, success_caption="clip")
            self.assertFalse(effective_auto(cfg, store, "!r:example.com"))
            self.assertTrue(effective_auto(cfg, store, "!other:example.com"))
            self.assertEqual(effective_caption(cfg, store, "!r:example.com"), "clip")
            self.assertTrue(effective_notify(cfg, store, "!r:example.com"))

            store.set_room("!r:example.com", auto_download=None)
            reloaded = StateStore(Path(td) / "runtime_state.yaml")
            self.assertTrue(effective_auto(cfg, reloaded, "!r:example.com"))
            self.assertEqual(effective_caption(cfg, reloaded, "!r:example.com"), "clip")
            self.assertTrue(effective_notify(cfg, reloaded, "!r:example.com"))
