"""E2EE store, registration migration, encrypted-event dispatch, per-room settings."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from typing import Any

from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from reelgrab.commands import effective_auto, effective_caption, effective_notify
from reelgrab.config import (
    AppConfig,
    AppserviceBotConfig,
    AppserviceConfig,
    EncryptionConfig,
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


class _Synapse162HS:
    """Homeserver that 500s on device_id lookups the way Synapse 1.162 does.

    ``keys/upload`` (and any other request) that sends ``device_id`` without
    ``user_id``, or before ``m.login.application_service`` created the device,
    returns HTTP 500. A successful login records the device. After that,
    upload succeeds only when both query params match.
    """

    def __init__(self, *, upload_ok: bool) -> None:
        self.upload_ok = upload_ok
        self.calls: list[dict[str, Any]] = []
        self.device_id: str | None = None

    async def handler(self, request: web.Request) -> web.Response:
        body: Any = None
        if request.method in {"POST", "PUT", "PATCH"} and request.can_read_body:
            try:
                body = await request.json()
            except Exception:
                body = None
        query = {key: request.query.get(key) for key in request.query}
        self.calls.append(
            {
                "method": request.method,
                "path": request.path,
                "query": query,
                "body": body,
            }
        )
        path = request.path
        if path.endswith("/versions"):
            return web.json_response({"versions": ["v1.11"]})
        if path.endswith("/register"):
            return web.json_response({"user_id": "@reelgrab:example.com"})
        if path.endswith("/media/config") or path.endswith("/config"):
            return web.json_response({"m.upload.size": 50_000_000})
        if "/profile/" in path and request.method == "GET":
            return web.json_response({"displayname": "Reelgrab"})
        if path.endswith("/displayname") and request.method == "PUT":
            return web.json_response({})
        if path.endswith("/joined_rooms"):
            return web.json_response({"joined_rooms": ["!r:example.com"]})
        if path.endswith("/login") and request.method == "POST":
            return self._login(query, body if isinstance(body, dict) else {})
        if path.endswith("/keys/upload"):
            return self._keys_upload(query)
        if "/send/" in path and request.method == "PUT":
            return web.json_response({"event_id": "$sent"})
        if "/state/" in path:
            return web.json_response({"errcode": "M_NOT_FOUND"}, status=404)
        return web.json_response({})

    def _login(self, query: dict[str, str], body: dict[str, Any]) -> web.Response:
        # device_id in the query, before the row exists, is the UserID 500.
        if query.get("device_id") or query.get("org.matrix.msc3202.device_id"):
            return web.json_response({"error": "can't adapt type 'UserID'"}, status=500)
        device_id = body.get("device_id")
        identifier = body.get("identifier") if isinstance(body.get("identifier"), dict) else {}
        ok = (
            body.get("type") == "m.login.application_service"
            and identifier.get("type") == "m.id.user"
            and identifier.get("user") == "@reelgrab:example.com"
            and query.get("user_id") == "@reelgrab:example.com"
            and isinstance(device_id, str)
            and device_id
        )
        if not ok:
            return web.json_response({})
        self.device_id = device_id
        return web.json_response(
            {
                "user_id": "@reelgrab:example.com",
                "device_id": device_id,
                "access_token": "as-login",
            }
        )

    def _keys_upload(self, query: dict[str, str]) -> web.Response:
        user_id = query.get("user_id")
        device_id = query.get("device_id")
        ready = (
            self.upload_ok
            and self.device_id is not None
            and user_id == "@reelgrab:example.com"
            and device_id == self.device_id
        )
        if not ready:
            return web.json_response({"error": "can't adapt type 'UserID'"}, status=500)
        return web.json_response({"one_time_key_counts": {"signed_curve25519": 50}})


class TestEncryptionStartup(unittest.IsolatedAsyncioTestCase):
    async def _start(
        self, *, upload_ok: bool, enabled: bool = True
    ) -> tuple[MatrixBot, _Synapse162HS, TestClient]:
        hs = _Synapse162HS(upload_ok=upload_ok)
        app = web.Application()
        app.router.add_route("*", "/{tail:.*}", hs.handler)
        client = TestClient(TestServer(app))
        await client.start_server()
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.addAsyncCleanup(client.close)
        cfg = AppConfig(
            homeserver=HomeserverConfig(
                address=str(client.make_url("")).rstrip("/"),
                domain="example.com",
            ),
            appservice=AppserviceConfig(
                as_token="as" * 16,
                hs_token="hs" * 16,
                bot=AppserviceBotConfig(username="reelgrab", avatar="", displayname="Reelgrab"),
            ),
            encryption=EncryptionConfig(enabled=enabled),
        )
        cfg.data_dir = Path(tmp.name)
        bot = MatrixBot(cfg)
        self.addAsyncCleanup(bot.close)
        return bot, hs, client

    async def test_failed_key_upload_does_not_poison_sends(self) -> None:
        bot, hs, _client = await self._start(upload_ok=False)
        with self.assertLogs("reelgrab.matrix", level="WARNING") as logs:
            await bot.start()
        disabled = [rec for rec in logs.records if "E2EE disabled" in rec.getMessage()]
        self.assertEqual(len(disabled), 1)
        self.assertEqual(len(logs.records), 1)
        self.assertIsNone(disabled[0].exc_info)
        self.assertIn("Unencrypted rooms keep working", disabled[0].getMessage())
        self.assertIsNone(bot.device_id)
        self.assertIsNone(bot._machine)
        self.assertIsNone(bot._crypto_client)
        stored = await bot._crypto_store.get_device_id()
        self.assertTrue(str(stored).startswith("REELGRAB"))

        await bot.send_text("!r:example.com", "still here")
        await bot.send_reaction("!r:example.com", "$src", "⏳")
        sends = [call for call in hs.calls if "/send/" in call["path"]]
        self.assertEqual(len(sends), 2)
        for call in sends:
            self.assertNotIn("device_id", call["query"])
            self.assertNotIn("user_id", call["query"])
            self.assertNotIn("org.matrix.msc3202.device_id", call["query"])
        self.assertTrue(bot.ready)

    async def test_login_then_upload_with_user_id(self) -> None:
        bot, hs, _client = await self._start(upload_ok=True)
        await bot.start()
        self.assertTrue(bot.ready)
        self.assertTrue(str(bot.device_id).startswith("REELGRAB"))
        device_id = bot.device_id
        assert device_id is not None

        logins = [call for call in hs.calls if call["path"].endswith("/login")]
        self.assertEqual(len(logins), 1)
        login = logins[0]
        self.assertEqual(login["method"], "POST")
        self.assertEqual(login["query"].get("user_id"), "@reelgrab:example.com")
        self.assertNotIn("device_id", login["query"])
        self.assertNotIn("org.matrix.msc3202.device_id", login["query"])
        self.assertEqual(login["body"]["type"], "m.login.application_service")
        self.assertEqual(
            login["body"]["identifier"],
            {"type": "m.id.user", "user": "@reelgrab:example.com"},
        )
        self.assertEqual(login["body"]["device_id"], device_id)

        uploads = [call for call in hs.calls if call["path"].endswith("/keys/upload")]
        self.assertGreaterEqual(len(uploads), 1)
        login_at = hs.calls.index(login)
        for call in uploads:
            self.assertGreater(hs.calls.index(call), login_at)
            self.assertEqual(call["query"].get("user_id"), "@reelgrab:example.com")
            self.assertEqual(call["query"].get("device_id"), device_id)
            self.assertEqual(call["query"].get("org.matrix.msc3202.device_id"), device_id)

        await bot.send_text("!r:example.com", "hello")
        sends = [call for call in hs.calls if "/send/" in call["path"]]
        self.assertEqual(len(sends), 1)
        self.assertEqual(sends[0]["query"].get("user_id"), "@reelgrab:example.com")
        self.assertEqual(sends[0]["query"].get("device_id"), device_id)
        self.assertEqual(sends[0]["query"].get("org.matrix.msc3202.device_id"), device_id)
        self.assertNotIn("encrypted", sends[0]["path"])

    async def test_encryption_disabled_skips_login(self) -> None:
        bot, hs, _client = await self._start(upload_ok=True, enabled=False)
        with self.assertNoLogs("reelgrab.matrix", level="WARNING"):
            await bot.start()
        self.assertTrue(bot.ready)
        self.assertIsNone(bot.device_id)
        paths = [call["path"] for call in hs.calls]
        self.assertFalse(any(path.endswith("/login") for path in paths))
        self.assertFalse(any(path.endswith("/keys/upload") for path in paths))
        await bot.send_text("!r:example.com", "plain")
        sends = [call for call in hs.calls if "/send/" in call["path"]]
        self.assertEqual(len(sends), 1)
        self.assertEqual(sends[0]["query"], {})


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
