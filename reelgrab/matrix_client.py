"""Matrix client: appservice as_token for outbound CS API + transaction push intake."""

from __future__ import annotations

import asyncio
import hashlib
import logging
import mimetypes
import secrets
import time
import uuid
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any, Protocol
from urllib.parse import quote

import aiofiles
import aiohttp
import yaml

from reelgrab.appservice import AppserviceNotReady, text_body_from_event
from reelgrab.config import AppConfig
from reelgrab.matrix_content import build_video_content, relates_to
from reelgrab.messages import (
    is_edit,
    is_historical,
    is_reply,
    message_is_actionable,
    strip_reply_fallback,
    thread_root_id,
)
from reelgrab.urls import find_matching_urls

log = logging.getLogger("reelgrab.matrix")

MessageHandler = Callable[..., Awaitable[None]]

REACT_WORKING = "⏳"
REACT_DONE = "✅"
REACT_FAILED = "❌"

_MEDIA_CONFIG_PATHS = (
    "/_matrix/client/v1/media/config",
    "/_matrix/media/v3/config",
)


class HomeserverUnavailable(RuntimeError):
    """The homeserver did not answer. Startup backs off and tries again."""


class MatrixGateway(Protocol):
    """Outbound Matrix operations used by handlers (testable seam)."""

    @property
    def user_id(self) -> str: ...

    def joined_room_ids(self) -> list[str]: ...

    async def upload_media(self, path: Path, mime: str | None = None) -> str: ...

    async def send_video(
        self,
        room_id: str,
        mxc: str,
        path: Path,
        *,
        reply_to_event_id: str | None = None,
        thread_root_event_id: str | None = None,
        caption: str | None = None,
        filename: str | None = None,
        formatted_body: str | None = None,
        mime: str | None = None,
        size: int | None = None,
        duration_ms: int | None = None,
        width: int | None = None,
        height: int | None = None,
        thumbnail_mxc: str | None = None,
        thumbnail_path: Path | None = None,
        thumbnail_width: int | None = None,
        thumbnail_height: int | None = None,
        thumbnail_size: int | None = None,
        blurhash: str | None = None,
    ) -> None: ...

    async def send_text(
        self,
        room_id: str,
        body: str,
        *,
        reply_to_event_id: str | None = None,
        thread_root_event_id: str | None = None,
        formatted_body: str | None = None,
    ) -> None: ...

    async def send_reaction(self, room_id: str, event_id: str, key: str) -> str: ...

    async def redact_event(self, room_id: str, event_id: str) -> None: ...


class MatrixBot:
    """Outbound CS API with as_token; inbound events via appservice transactions."""

    def __init__(self, cfg: AppConfig) -> None:
        self.cfg = cfg
        self._session: aiohttp.ClientSession | None = None
        self._message_handler: MessageHandler | None = None
        self._ready: bool = False
        self._started_ms: int = 0
        self.max_upload_bytes: int | None = None
        # room_id -> set of joined member MXIDs (best-effort from push + API)
        self._members: dict[str, set[str]] = {}
        self._joined: set[str] = set()
        # Rooms known to be DMs (m.room.member invite is_direct, or 2 members)
        self._direct_rooms: set[str] = set()
        self._appservice: Any = None
        self._device_id: str | None = None
        self._machine: Any = None
        self._crypto_client: Any = None
        self._crypto_store: Any = None
        self._plain_rooms: set[str] = set()

    @property
    def user_id(self) -> str:
        return self.cfg.user_id

    @property
    def ready(self) -> bool:
        return self._ready

    @property
    def device_id(self) -> str | None:
        return self._device_id

    def on_text_message(self, handler: MessageHandler) -> None:
        self._message_handler = handler

    def bind_appservice(self, appservice: Any) -> None:
        """Share the mautrix appservice state store and MSC3202 transaction hooks."""
        self._appservice = appservice

    def joined_room_ids(self) -> list[str]:
        return sorted(self._joined)

    def is_direct_room(self, room_id: str) -> bool:
        if room_id in self._direct_rooms:
            return True
        members = self._members.get(room_id)
        if members is not None and len(members) == 2 and self.user_id in members:
            return True
        return False

    def _hs(self, path: str) -> str:
        base = self.cfg.homeserver.address.rstrip("/")
        if not path.startswith("/"):
            path = "/" + path
        return base + path

    def _auth_headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.cfg.as_token}"}

    async def start(self) -> None:
        token = (self.cfg.as_token or "").strip()
        if not token or token.lower() == "generate":
            raise RuntimeError(
                "appservice.as_token missing — run once to generate config/registration"
            )

        self._started_ms = int(time.time() * 1000)
        self._session = aiohttp.ClientSession(
            headers=self._auth_headers(),
            timeout=aiohttp.ClientTimeout(total=120),
        )
        log.info("appservice auth as %s @ %s", self.user_id, self.cfg.homeserver.address)
        delay = 1.0
        while True:
            try:
                await self._startup_once()
                self._ready = True
                log.info(
                    "matrix client ready as %s (joined=%d)", self.user_id, len(self._joined)
                )
                return
            except HomeserverUnavailable as exc:
                self._ready = False
                log.warning("homeserver not reachable (%s); retrying in %.0fs", exc, delay)
                await asyncio.sleep(delay)
                delay = min(delay * 2, 30.0)
            except Exception:
                self._ready = False
                if self._session is not None:
                    await self._session.close()
                    self._session = None
                raise

    async def _startup_once(self) -> None:
        await self._request("GET", "/_matrix/client/versions")
        log.info("homeserver reachable at %s", self.cfg.homeserver.address)
        await self._appservice_ensure_registered()
        await self._load_media_config()
        await self._ensure_profile()
        await self._refresh_joined_rooms()
        await self._start_encryption()

    async def close(self) -> None:
        self._ready = False
        if self._crypto_store is not None:
            try:
                await self._crypto_store.close()
            except Exception:
                log.debug("crypto store close failed", exc_info=True)
            self._crypto_store = None
        self._machine = None
        self._crypto_client = None
        if self._session is not None:
            await self._session.close()
            self._session = None

    def _pickle_key(self) -> str:
        path = self.cfg.data_dir / "crypto_pickle.key"
        if path.is_file():
            key = path.read_text(encoding="utf-8").strip()
            if key:
                return key
        path.parent.mkdir(parents=True, exist_ok=True)
        key = secrets.token_hex(32)
        path.write_text(key + "\n", encoding="utf-8")
        try:
            path.chmod(0o600)
        except OSError:
            pass
        return key

    def _disable_encryption(self, reason: str) -> None:
        """Drop the live device id so later sends stay ordinary Client-Server calls.

        The sqlite store keeps the device id for the next start. Attaching it
        to ``/send`` before ``/keys/upload`` succeeds makes Synapse 1.162 reject
        those sends as well.
        """
        self._device_id = None
        self._machine = None
        self._crypto_client = None
        if self._appservice is not None:
            service = self._appservice.service
            service.to_device_handler = None
            service.device_list_handler = None
            service.otk_handler = None
        log.warning(
            "E2EE disabled: %s. Unencrypted rooms keep working; "
            "the device id is not attached to sends.",
            reason,
        )

    async def _login_appservice_device(self, device_id: str) -> None:
        """Create the bot device with ``m.login.application_service``.

        Synapse only accepts ``device_id`` on later requests after this login
        has inserted the device row. The login itself must not send
        ``device_id`` as a query parameter: the row does not exist yet, and
        Synapse 1.162 looks that query up with the appservice sender object
        (a ``UserID``, which Postgres cannot store) unless ``user_id`` is also
        a query string.
        """
        data = await self._request(
            "POST",
            "/_matrix/client/v3/login",
            json={
                "type": "m.login.application_service",
                "identifier": {"type": "m.id.user", "user": self.user_id},
                "device_id": device_id,
                "initial_device_display_name": "reelgrab",
            },
            params={"user_id": self.user_id},
        )
        returned = data.get("device_id") if isinstance(data, dict) else None
        if returned != device_id:
            raise RuntimeError(
                f"appservice login did not return device {device_id} (got {returned!r})"
            )
        log.info("appservice device %s logged in", device_id)

    async def _start_encryption(self) -> None:
        """Log in a device, then publish Olm keys (MSC3202).

        ``self._device_id`` is set only after ``/keys/upload`` succeeds. Until
        then, and if setup fails, room sends omit ``device_id`` and ``user_id``.
        """
        enabled = True
        encryption = getattr(self.cfg, "encryption", None)
        if encryption is not None:
            enabled = bool(getattr(encryption, "enabled", True))
        if not enabled:
            log.info("encryption disabled in config")
            return
        if self._session is None:
            return
        # Never inherit a device id from a previous attempt in this process.
        self._device_id = None
        try:
            from mautrix.api import HTTPAPI
            from mautrix.client import Client
            from mautrix.crypto import OlmMachine

            from reelgrab.crypto_store import SQLiteCryptoStore

            store = SQLiteCryptoStore(self.cfg.data_dir / "crypto.sqlite", self._pickle_key())
            await store.open()
            self._crypto_store = store
            device_id = await store.get_device_id()
            if not device_id:
                device_id = "REELGRAB" + secrets.token_hex(4).upper()
                await store.put_device_id(device_id)
            device_id = str(device_id)
            await self._login_appservice_device(device_id)

            if self._appservice is not None:
                state_store = self._appservice.service.state_store
            else:
                from mautrix.appservice.state_store import FileASStateStore

                state_path = self.cfg.data_dir / "mx-state.json"
                state_store = FileASStateStore(path=state_path, binary=False)
                await state_store.open()

            api = HTTPAPI(
                base_url=self.cfg.homeserver.address,
                token=self.cfg.as_token,
                client_session=self._session,
            )
            # Both query params. device_id alone is what makes Synapse 1.162
            # pass a UserID object into get_device and return HTTP 500.
            api.as_user_id = self.user_id
            api.as_device_id = device_id
            crypto_client = Client(
                self.user_id,
                device_id,
                api=api,
                state_store=state_store,
            )
            machine = OlmMachine(crypto_client, store, state_store)
            await machine.load()
            crypto_client.crypto = machine
            await machine.share_keys()
        except Exception as exc:
            text = str(exc).strip().splitlines()[0] if str(exc).strip() else type(exc).__name__
            self._disable_encryption(text)
            return

        self._crypto_client = crypto_client
        self._machine = machine
        self._device_id = device_id
        if self._appservice is not None:
            service = self._appservice.service
            service.to_device_handler = machine.handle_as_to_device_event
            service.device_list_handler = machine.handle_as_device_lists
            service.otk_handler = machine.handle_as_otk_counts
        log.info("e2ee ready device_id=%s", device_id)

    async def _request(
        self,
        method: str,
        path: str,
        *,
        json: Any = None,
        data: Any = None,
        headers: dict[str, str] | None = None,
        params: dict[str, str] | None = None,
        expect_json: bool = True,
    ) -> Any:
        if not self._session:
            raise RuntimeError("client not started")
        url = self._hs(path)
        hdrs = dict(headers or {})
        try:
            async with self._session.request(
                method, url, json=json, data=data, headers=hdrs, params=params
            ) as resp:
                body: Any
                if expect_json:
                    try:
                        body = await resp.json(content_type=None)
                    except Exception:
                        body = {"raw": await resp.text()}
                else:
                    body = await resp.read()
                if resp.status in (502, 503, 504):
                    raise HomeserverUnavailable(f"{method} {path} -> {resp.status}")
                if resp.status >= 400:
                    raise RuntimeError(f"{method} {path} -> {resp.status}: {body}")
                return body
        except HomeserverUnavailable:
            raise
        except (aiohttp.ClientError, TimeoutError, OSError) as exc:
            raise HomeserverUnavailable(f"{method} {path} failed: {exc}") from exc

    async def _appservice_ensure_registered(self) -> None:
        localpart = self.cfg.appservice.bot.username
        try:
            data = await self._request(
                "POST",
                "/_matrix/client/v3/register",
                json={
                    "type": "m.login.application_service",
                    "username": localpart,
                },
            )
            log.info("appservice user registered: %s", data.get("user_id"))
        except HomeserverUnavailable:
            raise
        except RuntimeError as exc:
            msg = str(exc)
            if "M_USER_IN_USE" in msg or "M_USER_EXISTS" in msg:
                log.debug("appservice user already exists")
                return
            log.warning("appservice register attempt failed: %s", exc)
        except Exception as exc:
            log.warning("appservice register attempt failed: %s", exc)

    async def _load_media_config(self) -> None:
        """Read ``m.upload.size`` so encodes can fit the homeserver limit."""
        configured = int(self.cfg.download.max_upload_bytes or 0)
        reported: int | None = None
        for path in _MEDIA_CONFIG_PATHS:
            try:
                data = await self._request("GET", path)
            except HomeserverUnavailable:
                raise
            except Exception as exc:
                log.debug("media config %s failed: %s", path, exc)
                continue
            size = data.get("m.upload.size") if isinstance(data, dict) else None
            if size:
                try:
                    reported = int(size)
                except (TypeError, ValueError):
                    reported = None
                if reported:
                    break
        if configured and reported:
            self.max_upload_bytes = min(configured, reported)
        else:
            self.max_upload_bytes = configured or reported
        if self.max_upload_bytes:
            log.info("homeserver upload limit %s bytes", self.max_upload_bytes)
        else:
            log.info("homeserver upload limit unknown; quality stepping disabled")

    async def _ensure_profile(self) -> None:
        uid = quote(self.user_id, safe="")
        name = (self.cfg.appservice.bot.displayname or "").strip()
        if name:
            try:
                await self._request(
                    "PUT",
                    f"/_matrix/client/v3/profile/{uid}/displayname",
                    json={"displayname": name},
                )
                log.info("display name set to %r", name)
            except HomeserverUnavailable:
                raise
            except Exception as exc:
                log.debug("set_displayname failed (may be ok): %s", exc)
        await self._ensure_avatar()

    def _avatar_state_path(self) -> Path:
        return self.cfg.data_dir / "avatar_state.yaml"

    def _read_avatar_state(self) -> dict[str, Any]:
        path = self._avatar_state_path()
        if not path.is_file():
            return {}
        try:
            with path.open(encoding="utf-8") as fh:
                data = yaml.safe_load(fh) or {}
            return data if isinstance(data, dict) else {}
        except Exception:
            return {}

    def _write_avatar_state(self, digest: str, mxc: str) -> None:
        path = self._avatar_state_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("w", encoding="utf-8") as fh:
            yaml.safe_dump({"sha256": digest, "mxc": mxc}, fh, sort_keys=True)

    async def _profile_avatar_url(self) -> str | None:
        uid = quote(self.user_id, safe="")
        try:
            data = await self._request("GET", f"/_matrix/client/v3/profile/{uid}")
        except HomeserverUnavailable:
            raise
        except Exception as exc:
            log.debug("get profile failed: %s", exc)
            return None
        if isinstance(data, dict):
            return data.get("avatar_url") or None
        return None

    async def _set_avatar_url(self, mxc: str) -> None:
        uid = quote(self.user_id, safe="")
        await self._request(
            "PUT",
            f"/_matrix/client/v3/profile/{uid}/avatar_url",
            json={"avatar_url": mxc},
        )

    async def _ensure_avatar(self) -> None:
        """Upload the avatar only when the file contents changed."""
        path = self.cfg.avatar_path()
        if path is None:
            return
        if not path.is_file():
            log.warning("avatar file missing (%s); skipping", path)
            return
        try:
            digest = hashlib.sha256(path.read_bytes()).hexdigest()
            saved = self._read_avatar_state()
            saved_mxc = str(saved.get("mxc") or "")
            if saved.get("sha256") == digest and saved_mxc:
                current = await self._profile_avatar_url()
                if current == saved_mxc:
                    log.info("avatar unchanged (%s)", digest[:12])
                    return
                await self._set_avatar_url(saved_mxc)
                log.info("avatar profile restored from cache %s", saved_mxc)
                return
            mxc = await self.upload_media(path)
            await self._set_avatar_url(mxc)
            self._write_avatar_state(digest, mxc)
            log.info("avatar set from %s -> %s", path, mxc)
        except HomeserverUnavailable:
            raise
        except Exception as exc:
            log.warning("set_avatar failed (may be ok): %s", exc)

    async def _refresh_joined_rooms(self) -> None:
        try:
            data = await self._request("GET", "/_matrix/client/v3/joined_rooms")
            rooms = data.get("joined_rooms") or []
            self._joined = set(rooms)
        except HomeserverUnavailable:
            raise
        except Exception as exc:
            log.warning("joined_rooms refresh failed: %s", exc)

    async def _join(self, room_id: str) -> None:
        log.info("joining room %s", room_id)
        try:
            rid = quote(room_id, safe="")
            await self._request("POST", f"/_matrix/client/v3/join/{rid}", json={})
            self._joined.add(room_id)
            self._members.setdefault(room_id, set()).add(self.user_id)
            log.info("joined %s", room_id)
        except Exception as exc:
            log.warning("join %s failed: %s", room_id, exc)

    async def _ensure_member_cache(self, room_id: str, *, force: bool = False) -> None:
        cached = self._members.get(room_id) or set()
        if not force and len(cached) >= 2:
            return
        try:
            rid = quote(room_id, safe="")
            data = await self._request(
                "GET", f"/_matrix/client/v3/rooms/{rid}/joined_members"
            )
            joined = data.get("joined") or {}
            self._members[room_id] = set(joined.keys())
            if self.user_id in self._members[room_id]:
                self._joined.add(room_id)
            if len(self._members[room_id]) == 2 and self.user_id in self._members[room_id]:
                self._direct_rooms.add(room_id)
        except Exception as exc:
            log.debug("joined_members %s failed: %s", room_id, exc)

    async def handle_appservice_events(self, events: list[dict[str, Any]]) -> None:
        """Process one transaction batch from the homeserver."""
        if not self._ready:
            raise AppserviceNotReady("homeserver client is not ready")
        for event in events:
            if not isinstance(event, dict):
                continue
            try:
                await self._handle_one_event(event)
            except Exception:
                log.exception(
                    "failed handling event %s type=%s",
                    event.get("event_id"),
                    event.get("type"),
                )

    async def _handle_one_event(self, event: dict[str, Any]) -> None:
        etype = event.get("type")
        room_id = event.get("room_id") or ""
        if not room_id:
            return

        if etype == "m.room.encryption":
            self._plain_rooms.discard(room_id)
            return

        if etype == "m.room.member":
            await self._note_member_for_crypto(event)
            await self._handle_member(event)
            return

        if etype == "m.room.encrypted":
            decrypted = await self._decrypt_to_dict(event)
            if decrypted is None:
                return
            await self._handle_one_event(decrypted)
            return

        if etype != "m.room.message":
            log.debug("ignore event type=%s room=%s", etype, room_id)
            return

        if not self._message_handler:
            return

        sender = event.get("sender") or ""
        if sender == self.user_id:
            return

        if is_edit(event):
            log.debug("ignore edit event=%s room=%s", event.get("event_id"), room_id)
            return

        if self.cfg.bot.ignore_history and self._started_ms and is_historical(
            event, started_ms=self._started_ms
        ):
            log.debug("ignore historical event=%s room=%s", event.get("event_id"), room_id)
            return

        body = text_body_from_event(event)
        if body is None:
            content = event.get("content") or {}
            log.debug(
                "ignore non-text message room=%s msgtype=%s",
                room_id,
                content.get("msgtype"),
            )
            return

        reply = is_reply(event)
        if reply:
            body = strip_reply_fallback(body)

        # Gate before any member fetch or log line. Unrelated chatter stays quiet.
        if not message_is_actionable(body, self.cfg):
            return

        event_id = event.get("event_id") or ""
        await self._ensure_member_cache(room_id, force=True)
        direct = self.is_direct_room(room_id)
        urls = find_matching_urls(body, self.cfg.url_patterns)
        if urls:
            log.info(
                "link room=%s sender=%s direct=%s urls=%s",
                room_id,
                sender,
                direct,
                urls,
            )
        else:
            log.info("command room=%s sender=%s direct=%s", room_id, sender, direct)

        await self._message_handler(
            room_id=room_id,
            event_id=event_id,
            sender=sender,
            body=body,
            is_direct=direct,
            is_reply=reply,
            thread_root_event_id=thread_root_id(event),
        )

    async def _note_member_for_crypto(self, event: dict[str, Any]) -> None:
        if self._machine is None:
            return
        try:
            from mautrix.types import StateEvent

            await self._machine.handle_member_event(StateEvent.deserialize(event))
        except Exception:
            log.debug("member crypto update failed", exc_info=True)

    async def _decrypt_to_dict(self, event: dict[str, Any]) -> dict[str, Any] | None:
        room_id = event.get("room_id") or ""
        if self._machine is None:
            log.warning(
                "encrypted event in %s but e2ee is not ready "
                "(homeserver MSC3202 extensions, or encryption.enabled)",
                room_id,
            )
            return None
        from mautrix.errors import DecryptionError
        from mautrix.types import EncryptedEvent

        try:
            decrypted = await self._machine.decrypt_megolm_event(EncryptedEvent.deserialize(event))
        except DecryptionError as exc:
            log.warning("decrypt failed %s: %s", event.get("event_id"), exc)
            return None
        except Exception:
            log.warning("decrypt failed %s", event.get("event_id"), exc_info=True)
            return None
        data = decrypted.serialize() if hasattr(decrypted, "serialize") else dict(decrypted)
        if not isinstance(data, dict):
            return None
        etype = data.get("type")
        if etype is not None and not isinstance(etype, str):
            data["type"] = str(etype)
        data.setdefault("room_id", room_id)
        data.setdefault("event_id", event.get("event_id"))
        data.setdefault("sender", event.get("sender"))
        if event.get("origin_server_ts") is not None:
            data.setdefault("origin_server_ts", event.get("origin_server_ts"))
        return data

    async def _maybe_encrypt(
        self, room_id: str, event_type: str, content: dict[str, Any]
    ) -> tuple[dict[str, Any], str]:
        """Megolm-encrypt room messages when the room is encrypted.

        Reactions stay unencrypted; clients accept ``m.reaction`` in encrypted rooms.
        """
        client = self._crypto_client
        if client is None or not getattr(client, "crypto", None) or event_type == "m.reaction":
            return content, event_type
        try:
            encrypted = await self._room_is_encrypted(room_id)
        except Exception:
            log.debug("encryption state lookup failed for %s", room_id, exc_info=True)
            return content, event_type
        if not encrypted:
            return content, event_type
        from mautrix.types import EventType

        payload = await client.encrypt(room_id, EventType.find(event_type), content)
        if hasattr(payload, "serialize"):
            payload = payload.serialize()
        if not isinstance(payload, dict):
            raise RuntimeError("encrypt did not return event content")
        return payload, "m.room.encrypted"

    async def _room_is_encrypted(self, room_id: str) -> bool:
        client = self._crypto_client
        if client is None or client.state_store is None:
            return False
        flag = await client.state_store.is_encrypted(room_id)
        if flag is not None:
            return bool(flag)
        if room_id in self._plain_rooms:
            return False
        from mautrix.errors import MNotFound
        from mautrix.types import EventType

        try:
            content = await client.get_state_event(room_id, EventType.ROOM_ENCRYPTION)
        except MNotFound:
            self._plain_rooms.add(room_id)
            return False
        except Exception:
            log.debug("room encryption state unavailable for %s", room_id, exc_info=True)
            return False
        if isinstance(content, dict):
            algorithm = content.get("algorithm")
        else:
            algorithm = getattr(content, "algorithm", None)
        if not algorithm:
            self._plain_rooms.add(room_id)
            return False
        setter = getattr(client.state_store, "set_encryption_info", None)
        if setter is not None and content is not None:
            await setter(room_id, content)
        return True

    async def _handle_member(self, event: dict[str, Any]) -> None:
        room_id = event.get("room_id") or ""
        state_key = event.get("state_key") or ""
        content = event.get("content") or {}
        membership = content.get("membership") or ""
        if content.get("is_direct") and state_key == self.user_id:
            self._direct_rooms.add(room_id)

        members = self._members.setdefault(room_id, set())
        if membership == "join":
            members.add(state_key)
            if state_key == self.user_id:
                self._joined.add(room_id)
                await self._ensure_member_cache(room_id, force=True)
        elif membership in ("leave", "ban"):
            members.discard(state_key)
            if state_key == self.user_id:
                self._joined.discard(room_id)
                self._direct_rooms.discard(room_id)
        elif membership == "invite":
            if state_key == self.user_id and self.cfg.bot.join_on_invite:
                await self._join(room_id)

    async def upload_media(self, path: Path, mime: str | None = None) -> str:
        if not self._session:
            raise RuntimeError("client not started")
        path = Path(path)
        if not path.is_file():
            raise FileNotFoundError(path)

        if not mime:
            mime, _ = mimetypes.guess_type(str(path))
            mime = mime or "application/octet-stream"

        size = path.stat().st_size
        url = self._hs("/_matrix/media/v3/upload")
        params = {"filename": path.name}
        headers = {
            **self._auth_headers(),
            "Content-Type": mime,
        }
        async with aiofiles.open(path, "rb") as f:
            data = await f.read()
        try:
            async with self._session.post(
                url, params=params, data=data, headers=headers
            ) as resp:
                body = await resp.json(content_type=None)
                if resp.status in (502, 503, 504):
                    raise HomeserverUnavailable(f"upload -> {resp.status}")
                if resp.status >= 400:
                    if resp.status == 404:
                        return await self._upload_r0(path, mime, data)
                    if resp.status == 413:
                        raise RuntimeError(
                            f"upload failed 413: file is {size} bytes, over the homeserver limit"
                        )
                    raise RuntimeError(f"upload failed {resp.status}: {body}")
                mxc = body.get("content_uri")
                if not mxc:
                    raise RuntimeError(f"upload missing content_uri: {body}")
                log.info("uploaded %s -> %s (%s bytes)", path.name, mxc, size)
                return mxc
        except HomeserverUnavailable:
            raise
        except (aiohttp.ClientError, TimeoutError, OSError) as exc:
            raise HomeserverUnavailable(f"upload failed: {exc}") from exc

    async def _upload_r0(self, path: Path, mime: str, data: bytes) -> str:
        assert self._session
        url = self._hs("/_matrix/media/r0/upload")
        headers = {**self._auth_headers(), "Content-Type": mime}
        async with self._session.post(
            url, params={"filename": path.name}, data=data, headers=headers
        ) as resp:
            body = await resp.json(content_type=None)
            if resp.status >= 400:
                raise RuntimeError(f"upload r0 failed {resp.status}: {body}")
            mxc = body.get("content_uri")
            if not mxc:
                raise RuntimeError(f"upload missing content_uri: {body}")
            log.info("uploaded %s -> %s", path.name, mxc)
            return mxc

    async def send_video(
        self,
        room_id: str,
        mxc: str,
        path: Path,
        *,
        reply_to_event_id: str | None = None,
        thread_root_event_id: str | None = None,
        caption: str | None = None,
        filename: str | None = None,
        formatted_body: str | None = None,
        mime: str | None = None,
        size: int | None = None,
        duration_ms: int | None = None,
        width: int | None = None,
        height: int | None = None,
        thumbnail_mxc: str | None = None,
        thumbnail_path: Path | None = None,
        thumbnail_width: int | None = None,
        thumbnail_height: int | None = None,
        thumbnail_size: int | None = None,
        blurhash: str | None = None,
    ) -> None:
        path = Path(path)
        if size is None:
            size = path.stat().st_size if path.is_file() else 0
        if not mime:
            mime, _ = mimetypes.guess_type(str(path))
            mime = mime or "video/mp4"
        if not filename:
            filename = path.name or "video.mp4"
        body = caption or filename
        if thumbnail_size is None and thumbnail_path and thumbnail_path.is_file():
            thumbnail_size = thumbnail_path.stat().st_size

        content = build_video_content(
            mxc=mxc,
            body=body,
            filename=filename,
            mime=mime,
            size=size,
            duration_ms=duration_ms,
            width=width,
            height=height,
            thumbnail_mxc=thumbnail_mxc,
            thumbnail_size=thumbnail_size,
            thumbnail_width=thumbnail_width,
            thumbnail_height=thumbnail_height,
            blurhash=blurhash,
            formatted_body=formatted_body,
            reply_to_event_id=reply_to_event_id,
            thread_root_event_id=thread_root_event_id,
        )
        await self._room_send(room_id, content)
        log.info(
            "sent %s to %s size=%s duration_ms=%s thumb=%s",
            content["msgtype"],
            room_id,
            size,
            duration_ms,
            bool(thumbnail_mxc),
        )

    async def send_text(
        self,
        room_id: str,
        body: str,
        *,
        reply_to_event_id: str | None = None,
        thread_root_event_id: str | None = None,
        formatted_body: str | None = None,
    ) -> None:
        content: dict[str, Any] = {"msgtype": "m.notice", "body": body}
        if formatted_body:
            content["format"] = "org.matrix.custom.html"
            content["formatted_body"] = formatted_body
        relation = relates_to(
            reply_to_event_id=reply_to_event_id,
            thread_root_event_id=thread_root_event_id,
        )
        if relation:
            content["m.relates_to"] = relation
        await self._room_send(room_id, content)
        log.info("sent notice to %s (%d chars)", room_id, len(body))

    async def send_reaction(self, room_id: str, event_id: str, key: str) -> str:
        content = {
            "m.relates_to": {
                "rel_type": "m.annotation",
                "event_id": event_id,
                "key": key,
            }
        }
        sent = await self._room_send(room_id, content, event_type="m.reaction")
        return sent or ""

    async def redact_event(self, room_id: str, event_id: str, reason: str = "") -> None:
        if not event_id:
            return
        txn_id = f"{int(time.time() * 1000)}{uuid.uuid4().hex[:8]}"
        rid = quote(room_id, safe="")
        eid = quote(event_id, safe="")
        payload: dict[str, Any] = {}
        if reason:
            payload["reason"] = reason
        await self._request(
            "PUT",
            f"/_matrix/client/v3/rooms/{rid}/redact/{eid}/{txn_id}",
            json=payload,
        )

    async def refresh_joined_rooms(self) -> None:
        await self._refresh_joined_rooms()

    async def _room_send(
        self,
        room_id: str,
        content: dict[str, Any],
        *,
        event_type: str = "m.room.message",
    ) -> str:
        content, event_type = await self._maybe_encrypt(room_id, event_type, content)
        txn_id = f"{int(time.time() * 1000)}{uuid.uuid4().hex[:8]}"
        rid = quote(room_id, safe="")
        etype = quote(event_type, safe="")
        params = None
        if self._device_id:
            params = {
                "user_id": self.user_id,
                "org.matrix.msc3202.device_id": self._device_id,
                "device_id": self._device_id,
            }
        data = await self._request(
            "PUT",
            f"/_matrix/client/v3/rooms/{rid}/send/{etype}/{txn_id}",
            json=content,
            params=params,
        )
        if isinstance(data, dict):
            return str(data.get("event_id") or "")
        return ""
