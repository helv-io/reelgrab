"""Matrix client: appservice as_token auth, sync, media upload, send."""

from __future__ import annotations

import logging
import mimetypes
from pathlib import Path
from typing import Any, Awaitable, Callable, Protocol

import aiofiles

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
    ) -> None: ...

    async def send_text(
        self,
        room_id: str,
        body: str,
        *,
        reply_to_event_id: str | None = None,
    ) -> None: ...


class MatrixBot:
    """matrix-nio AsyncClient wrapper for the appservice bot user."""

    def __init__(self, cfg: AppConfig) -> None:
        self.cfg = cfg
        self._client: Any = None
        self._message_handler: MessageHandler | None = None
        self._ready: bool = False

    @property
    def user_id(self) -> str:
        if self._client and getattr(self._client, "user_id", None):
            return self._client.user_id
        return self.cfg.user_id

    @property
    def ready(self) -> bool:
        return self._ready

    def on_text_message(self, handler: MessageHandler) -> None:
        self._message_handler = handler

    def joined_room_ids(self) -> list[str]:
        if not self._client:
            return []
        return list(getattr(self._client, "rooms", {}) or {}.keys())

    def is_direct_room(self, room_id: str) -> bool:
        if not self._client:
            return False
        room = (getattr(self._client, "rooms", {}) or {}).get(room_id)
        if room is None:
            return False
        if hasattr(room, "is_group") and room.is_group is False:
            return True
        try:
            if getattr(room, "member_count", 0) == 2:
                return True
        except Exception:
            pass
        return False

    async def start(self) -> None:
        from nio import (
            AsyncClient,
            InviteEvent,
            MatrixRoom,
            RoomMessageText,
            SyncResponse,
        )

        homeserver = self.cfg.homeserver.address
        user_id = self.cfg.user_id
        token = (self.cfg.as_token or "").strip()
        if not token or token.lower() == "generate":
            raise RuntimeError(
                "appservice.as_token missing — run once to generate config/registration"
            )

        client = AsyncClient(homeserver, user_id)
        self._client = client
        client.access_token = token
        client.user_id = user_id
        if not getattr(client, "device_id", None):
            client.device_id = "REELGRAB_AS"

        log.info("appservice auth as %s @ %s", user_id, homeserver)
        await self._appservice_ensure_registered(client)
        await self._ensure_profile(client)

        if self.cfg.bot.join_on_invite:

            async def _on_invite(room: MatrixRoom, event: InviteEvent) -> None:
                log.info("joining invited room %s", room.room_id)
                join_resp = await client.join(room.room_id)
                log.info("join %s -> %s", room.room_id, type(join_resp).__name__)

            client.add_event_callback(_on_invite, InviteEvent)

        async def _on_text(room: MatrixRoom, event: RoomMessageText) -> None:
            if not self._ready or not self._message_handler:
                return
            if event.sender == client.user_id:
                return
            body = event.body or ""
            formatted = ""
            try:
                formatted = (event.source.get("content") or {}).get(
                    "formatted_body", ""
                ) or ""
            except Exception:
                pass
            text = body
            if formatted and formatted not in body:
                text = f"{body}\n{formatted}"

            is_direct = False
            try:
                if hasattr(room, "is_group") and room.is_group is False:
                    is_direct = True
                elif getattr(room, "member_count", 0) == 2:
                    is_direct = True
            except Exception:
                pass

            try:
                await self._message_handler(
                    room_id=room.room_id,
                    event_id=event.event_id,
                    sender=event.sender,
                    body=text,
                    is_direct=is_direct,
                )
            except Exception:
                log.exception("message handler failed room=%s", room.room_id)

        client.add_event_callback(_on_text, RoomMessageText)

        log.info("initial sync…")
        sync = await client.sync(timeout=30_000, full_state=True)
        if isinstance(sync, SyncResponse):
            log.info("initial sync ok next_batch=%s…", (sync.next_batch or "")[:16])
        else:
            log.warning("initial sync response: %s", sync)
        self._ready = True
        log.info("matrix client ready as %s", client.user_id)

    async def _appservice_ensure_registered(self, client: Any) -> None:
        import aiohttp

        localpart = self.cfg.appservice.bot.username
        url = f"{self.cfg.homeserver.address.rstrip('/')}/_matrix/client/v3/register"
        headers = {"Authorization": f"Bearer {self.cfg.as_token}"}
        body = {
            "type": "m.login.application_service",
            "username": localpart,
        }
        try:
            async with aiohttp.ClientSession() as session:
                async with session.post(url, json=body, headers=headers) as resp:
                    data = await resp.json(content_type=None)
                    if resp.status < 400:
                        log.info("appservice user registered: %s", data.get("user_id"))
                        if data.get("user_id"):
                            client.user_id = data["user_id"]
                        return
                    err = data.get("errcode", "")
                    if err in ("M_USER_IN_USE", "M_USER_EXISTS"):
                        log.debug("appservice user already exists")
                        return
                    log.warning(
                        "appservice register status=%s body=%s", resp.status, data
                    )
        except Exception as exc:
            log.warning("appservice register attempt failed: %s", exc)

    async def _ensure_profile(self, client: Any) -> None:
        name = (self.cfg.appservice.bot.displayname or "").strip()
        if not name:
            return
        try:
            await client.set_displayname(name)
            log.info("display name set to %r", name)
        except Exception as exc:
            log.debug("set_displayname failed (may be ok): %s", exc)

    async def sync_forever(self) -> None:
        if not self._client:
            raise RuntimeError("client not started")
        await self._client.sync_forever(timeout=30_000, full_state=False)

    async def close(self) -> None:
        if self._client is not None:
            await self._client.close()
            self._client = None
            self._ready = False

    async def upload_media(self, path: Path, mime: str | None = None) -> str:
        from nio import UploadResponse

        if not self._client:
            raise RuntimeError("client not started")
        path = Path(path)
        if not path.is_file():
            raise FileNotFoundError(path)

        if not mime:
            mime, _ = mimetypes.guess_type(str(path))
            mime = mime or "application/octet-stream"

        size = path.stat().st_size
        async with aiofiles.open(path, "rb") as f:
            resp, _maybe_keys = await self._client.upload(
                f,
                content_type=mime,
                filename=path.name,
                filesize=size,
            )
        if not isinstance(resp, UploadResponse):
            raise RuntimeError(f"upload failed: {resp}")
        log.info("uploaded %s -> %s (%s bytes)", path.name, resp.content_uri, size)
        return resp.content_uri

    async def send_video(
        self,
        room_id: str,
        mxc: str,
        path: Path,
        *,
        reply_to_event_id: str | None = None,
        caption: str | None = None,
        mime: str | None = None,
    ) -> None:
        if not self._client:
            raise RuntimeError("client not started")

        path = Path(path)
        size = path.stat().st_size if path.is_file() else 0
        if not mime:
            mime, _ = mimetypes.guess_type(str(path))
            mime = mime or "video/mp4"

        is_video = mime.startswith("video/")
        body = caption or path.name
        content: dict[str, Any] = {
            "body": body,
            "filename": path.name,
            "info": {"size": size, "mimetype": mime},
            "msgtype": "m.video" if is_video else "m.file",
            "url": mxc,
        }
        if reply_to_event_id:
            content["m.relates_to"] = {
                "m.in_reply_to": {"event_id": reply_to_event_id}
            }

        resp = await self._client.room_send(
            room_id=room_id,
            message_type="m.room.message",
            content=content,
        )
        log.info(
            "sent %s to %s resp=%s",
            content["msgtype"],
            room_id,
            type(resp).__name__,
        )

    async def send_text(
        self,
        room_id: str,
        body: str,
        *,
        reply_to_event_id: str | None = None,
    ) -> None:
        if not self._client:
            raise RuntimeError("client not started")

        content: dict[str, Any] = {"msgtype": "m.notice", "body": body}
        if reply_to_event_id:
            content["m.relates_to"] = {
                "m.in_reply_to": {"event_id": reply_to_event_id}
            }

        await self._client.room_send(
            room_id=room_id,
            message_type="m.room.message",
            content=content,
        )
