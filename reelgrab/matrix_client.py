"""Matrix client: appservice as_token for outbound CS API + transaction push intake."""

from __future__ import annotations

import logging
import mimetypes
import time
import uuid
from pathlib import Path
from typing import Any, Awaitable, Callable, Protocol
from urllib.parse import quote

import aiofiles
import aiohttp

from reelgrab.appservice import text_body_from_event
from reelgrab.config import AppConfig

log = logging.getLogger("reelgrab.matrix")

MessageHandler = Callable[..., Awaitable[None]]


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
        caption: str | None = None,
        mime: str | None = None,
        size: int | None = None,
        duration_ms: int | None = None,
        width: int | None = None,
        height: int | None = None,
        thumbnail_mxc: str | None = None,
        thumbnail_path: Path | None = None,
    ) -> None: ...

    async def send_text(
        self,
        room_id: str,
        body: str,
        *,
        reply_to_event_id: str | None = None,
        formatted_body: str | None = None,
    ) -> None: ...


class MatrixBot:
    """Outbound CS API with as_token; inbound events via appservice transactions."""

    def __init__(self, cfg: AppConfig) -> None:
        self.cfg = cfg
        self._session: aiohttp.ClientSession | None = None
        self._message_handler: MessageHandler | None = None
        self._ready: bool = False
        # room_id -> set of joined member MXIDs (best-effort from push + API)
        self._members: dict[str, set[str]] = {}
        self._joined: set[str] = set()
        # Rooms known to be DMs (m.room.member invite is_direct, or 2 members)
        self._direct_rooms: set[str] = set()

    @property
    def user_id(self) -> str:
        return self.cfg.user_id

    @property
    def ready(self) -> bool:
        return self._ready

    def on_text_message(self, handler: MessageHandler) -> None:
        self._message_handler = handler

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

        self._session = aiohttp.ClientSession(
            headers=self._auth_headers(),
            timeout=aiohttp.ClientTimeout(total=120),
        )
        log.info("appservice auth as %s @ %s", self.user_id, self.cfg.homeserver.address)
        await self._appservice_ensure_registered()
        await self._ensure_profile()
        await self._refresh_joined_rooms()
        self._ready = True
        log.info("matrix client ready as %s (joined=%d)", self.user_id, len(self._joined))

    async def close(self) -> None:
        self._ready = False
        if self._session is not None:
            await self._session.close()
            self._session = None

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
            if resp.status >= 400:
                raise RuntimeError(f"{method} {path} -> {resp.status}: {body}")
            return body

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
        except RuntimeError as exc:
            msg = str(exc)
            if "M_USER_IN_USE" in msg or "M_USER_EXISTS" in msg or "400" in msg:
                # Already exists is fine; other 400s logged below.
                if "M_USER_IN_USE" in msg or "M_USER_EXISTS" in msg:
                    log.debug("appservice user already exists")
                    return
            log.warning("appservice register attempt failed: %s", exc)
        except Exception as exc:
            log.warning("appservice register attempt failed: %s", exc)

    async def _ensure_profile(self) -> None:
        name = (self.cfg.appservice.bot.displayname or "").strip()
        if not name:
            return
        try:
            uid = quote(self.user_id, safe="")
            await self._request(
                "PUT",
                f"/_matrix/client/v3/profile/{uid}/displayname",
                json={"displayname": name},
            )
            log.info("display name set to %r", name)
        except Exception as exc:
            log.debug("set_displayname failed (may be ok): %s", exc)

    async def _refresh_joined_rooms(self) -> None:
        try:
            data = await self._request("GET", "/_matrix/client/v3/joined_rooms")
            rooms = data.get("joined_rooms") or []
            self._joined = set(rooms)
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
        # After invite/join we often only know ourselves; refresh until we have a
        # complete picture (needed for is_direct detection).
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

        if etype == "m.room.member":
            await self._handle_member(event)
            return

        if etype == "m.room.encrypted":
            log.warning(
                "ignoring encrypted event in %s — enable encryption support or "
                "disable E2EE for this room/DM",
                room_id,
            )
            return

        if etype != "m.room.message":
            log.debug("ignore event type=%s room=%s", etype, room_id)
            return

        if not self._message_handler:
            return

        sender = event.get("sender") or ""
        if sender == self.user_id:
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

        event_id = event.get("event_id") or ""
        await self._ensure_member_cache(room_id, force=True)
        is_direct = self.is_direct_room(room_id)
        log.info(
            "message room=%s sender=%s direct=%s body=%r",
            room_id,
            sender,
            is_direct,
            (body or "")[:80],
        )

        await self._message_handler(
            room_id=room_id,
            event_id=event_id,
            sender=sender,
            body=body,
            is_direct=is_direct,
        )

    async def _handle_member(self, event: dict[str, Any]) -> None:
        room_id = event.get("room_id") or ""
        state_key = event.get("state_key") or ""
        content = event.get("content") or {}
        membership = content.get("membership") or ""
        # Matrix DM invites often set is_direct on the invite content.
        if content.get("is_direct") and state_key == self.user_id:
            self._direct_rooms.add(room_id)

        members = self._members.setdefault(room_id, set())
        if membership == "join":
            members.add(state_key)
            if state_key == self.user_id:
                self._joined.add(room_id)
                # Pull full member list so is_direct works immediately.
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
        # CS media upload (authenticated)
        url = self._hs("/_matrix/media/v3/upload")
        params = {"filename": path.name}
        headers = {
            **self._auth_headers(),
            "Content-Type": mime,
        }
        async with aiofiles.open(path, "rb") as f:
            data = await f.read()
        async with self._session.post(
            url, params=params, data=data, headers=headers
        ) as resp:
            body = await resp.json(content_type=None)
            if resp.status >= 400:
                # Fallback older path
                if resp.status == 404:
                    return await self._upload_r0(path, mime, data)
                raise RuntimeError(f"upload failed {resp.status}: {body}")
            mxc = body.get("content_uri")
            if not mxc:
                raise RuntimeError(f"upload missing content_uri: {body}")
            log.info("uploaded %s -> %s (%s bytes)", path.name, mxc, size)
            return mxc

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
        caption: str | None = None,
        mime: str | None = None,
        size: int | None = None,
        duration_ms: int | None = None,
        width: int | None = None,
        height: int | None = None,
        thumbnail_mxc: str | None = None,
        thumbnail_path: Path | None = None,
    ) -> None:
        path = Path(path)
        if size is None:
            size = path.stat().st_size if path.is_file() else 0
        if not mime:
            mime, _ = mimetypes.guess_type(str(path))
            mime = mime or "video/mp4"

        is_video = mime.startswith("video/")
        body = caption or path.name
        info: dict[str, Any] = {"size": size, "mimetype": mime}
        if duration_ms is not None and duration_ms > 0:
            info["duration"] = int(duration_ms)
        if width:
            info["w"] = int(width)
        if height:
            info["h"] = int(height)
        if thumbnail_mxc:
            info["thumbnail_url"] = thumbnail_mxc
            thumb_info: dict[str, Any] = {"mimetype": "image/jpeg"}
            if thumbnail_path and thumbnail_path.is_file():
                thumb_info["size"] = thumbnail_path.stat().st_size
            if width:
                # Approximate thumb dimensions (long edge ~640 from generator)
                tw, th = int(width), int(height or width)
                if max(tw, th) > 640:
                    scale = 640 / max(tw, th)
                    tw = max(1, int(tw * scale))
                    th = max(1, int(th * scale))
                thumb_info["w"] = tw
                thumb_info["h"] = th
            info["thumbnail_info"] = thumb_info

        content: dict[str, Any] = {
            "body": body,
            "info": info,
            "msgtype": "m.video" if is_video else "m.file",
            "url": mxc,
        }
        if not is_video:
            content["filename"] = path.name
        if reply_to_event_id:
            content["m.relates_to"] = {
                "m.in_reply_to": {"event_id": reply_to_event_id}
            }

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
        formatted_body: str | None = None,
    ) -> None:
        content: dict[str, Any] = {"msgtype": "m.notice", "body": body}
        if formatted_body:
            content["format"] = "org.matrix.custom.html"
            content["formatted_body"] = formatted_body
        if reply_to_event_id:
            content["m.relates_to"] = {
                "m.in_reply_to": {"event_id": reply_to_event_id}
            }
        await self._room_send(room_id, content)
        log.info("sent notice to %s (%d chars)", room_id, len(body))

    async def refresh_joined_rooms(self) -> None:
        await self._refresh_joined_rooms()

    async def _room_send(self, room_id: str, content: dict[str, Any]) -> None:
        txn_id = f"{int(time.time() * 1000)}{uuid.uuid4().hex[:8]}"
        rid = quote(room_id, safe="")
        await self._request(
            "PUT",
            f"/_matrix/client/v3/rooms/{rid}/send/m.room.message/{txn_id}",
            json=content,
        )
