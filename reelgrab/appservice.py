"""
Application service HTTP server on mautrix-python.

Synapse (and other homeservers) push events to registration ``url``:

  PUT  /_matrix/app/v1/transactions/{txnId}
  GET  /_matrix/app/v1/users/{userId}
  GET  /_matrix/app/v1/rooms/{roomAlias}

Legacy paths without the ``/_matrix/app/v1`` prefix are also accepted.
Auth: ``Authorization: Bearer <hs_token>`` or ``?access_token=<hs_token>``.

``encryption_events`` is on, so MSC3202 to-device messages, device lists, and
one-time-key counts in the transaction body are parsed and handed to the Olm
machine. The registration ``url`` stays set; this process does not poll ``/sync``.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable, Iterable
from typing import Any

from aiohttp import web
from mautrix.appservice import AppService
from mautrix.appservice.state_store import FileASStateStore

from reelgrab.config import AppConfig

log = logging.getLogger("reelgrab.appservice")

EventHandler = Callable[[list[dict[str, Any]]], Awaitable[None]]
ReadyCheck = Callable[[], bool]


class AppserviceNotReady(RuntimeError):
    """Raised when a transaction arrives before the homeserver client is ready.

    The HTTP handler turns this into 503 and forgets the txn id so the
    homeserver can deliver the same transaction again.
    """


class ReelgrabAppService(AppService):
    """mautrix AppService plus ``/health`` and a not-ready 503."""

    def __init__(self, cfg: AppConfig, owner: AppserviceServer) -> None:
        self._cfg = cfg
        self._owner = owner
        state_path = cfg.data_dir / "mx-state.json"
        state_path.parent.mkdir(parents=True, exist_ok=True)
        super().__init__(
            server=cfg.homeserver.address,
            domain=cfg.homeserver.domain,
            as_token=cfg.as_token,
            hs_token=cfg.hs_token,
            bot_localpart=cfg.appservice.bot.username,
            id=cfg.appservice.id,
            state_store=FileASStateStore(path=state_path, binary=False),
            encryption_events=True,
            log="reelgrab.appservice",
        )
        # Ack a transaction only after handlers finish, matching the previous server.
        self.synchronous_handlers = True
        self.app.router.add_get("/health", self._health)

        async def query_user(user_id: str) -> dict[str, str] | None:
            if user_id == cfg.user_id:
                return {"user_id": user_id}
            return None

        self.query_user = query_user
        self.matrix_event_handler(self._forward_event)
        self._injected_timestamps: set[str] = set()

    def _is_ready(self) -> bool:
        check = self._owner.ready_check
        if check is None:
            return True
        return bool(check())

    async def _health(self, _request: web.Request) -> web.Response:
        ready = self._is_ready()
        return web.json_response(
            {"ok": ready, "ready": ready, "bot": self._cfg.user_id},
            status=200 if ready else 503,
        )

    async def _http_handle_transaction(self, request: web.Request) -> web.Response:
        if not self._check_token(request):
            return web.json_response({"error": "Invalid auth token"}, status=401)
        # Refuse before the txn id is remembered so the homeserver retries.
        if not self._is_ready():
            return web.json_response(
                {"errcode": "M_UNKNOWN", "error": "appservice not ready"},
                status=503,
            )
        return await super()._http_handle_transaction(request)

    async def handle_transaction(
        self,
        txn_id: str,
        *,
        events: list[Any],
        extra_data: Any,
        ephemeral: list[Any] | None = None,
        to_device: list[Any] | None = None,
        otk_counts: Any = None,
        device_lists: Any = None,
    ) -> Any:
        # Homeserver events include origin_server_ts. Tolerate fixtures and
        # odd payloads so a missing timestamp does not drop the transaction.
        normalized: list[Any] = []
        injected: set[str] = set()
        for raw in events or []:
            if isinstance(raw, dict) and "origin_server_ts" not in raw:
                raw = dict(raw)
                raw["origin_server_ts"] = 0
                event_id = raw.get("event_id")
                if event_id:
                    injected.add(str(event_id))
            normalized.append(raw)
        self._injected_timestamps = injected
        try:
            return await super().handle_transaction(
                txn_id,
                events=normalized,
                extra_data=extra_data,
                ephemeral=ephemeral,
                to_device=to_device,
                otk_counts=otk_counts,
                device_lists=device_lists,
            )
        finally:
            self._injected_timestamps = set()

    async def _forward_event(self, event: Any) -> None:
        handler = self._owner._on_events
        if handler is None:
            return
        if hasattr(event, "serialize"):
            raw = event.serialize()
        elif isinstance(event, dict):
            raw = event
        else:
            return
        if not isinstance(raw, dict):
            return
        etype = raw.get("type")
        if etype is not None and not isinstance(etype, str):
            raw["type"] = str(etype)
        event_id = str(raw.get("event_id") or "")
        if event_id in self._injected_timestamps and raw.get("origin_server_ts") == 0:
            raw.pop("origin_server_ts", None)
        await handler([raw])


class AppserviceServer:
    """HTTP endpoint the homeserver calls (mautrix AppService)."""

    def __init__(
        self,
        cfg: AppConfig,
        *,
        on_events: EventHandler | None = None,
    ) -> None:
        self.cfg = cfg
        self._on_events = on_events
        self._ready_check: ReadyCheck | None = None
        self.service = ReelgrabAppService(cfg, self)
        self.app = self.service.app

    @property
    def ready_check(self) -> ReadyCheck | None:
        return self._ready_check

    def on_events(self, handler: EventHandler) -> None:
        self._on_events = handler

    def set_ready_check(self, check: ReadyCheck | None) -> None:
        """When set, ``/health`` is 503 until ``check()`` is true."""
        self._ready_check = check

    @property
    def ready(self) -> bool:
        if self._ready_check is None:
            return True
        return bool(self._ready_check())

    async def start(self) -> None:
        host = self.cfg.appservice.hostname or "0.0.0.0"
        port = int(self.cfg.appservice.port or 29399)
        await self.service.start(host, port)
        log.info(
            "appservice listening on %s:%s (hs url should be %s)",
            host,
            port,
            self.cfg.appservice.address,
        )

    async def stop(self) -> None:
        await self.service.stop()


def text_body_from_event(event: dict[str, Any]) -> str | None:
    """Plain text of an ``m.text`` / ``m.emote``. Notices and media are ignored.

    ``formatted_body`` is not appended: reply fallbacks put the quoted message
    (and its links) in the HTML, which would grab a URL the user did not send.
    """
    if event.get("type") != "m.room.message":
        return None
    content = event.get("content") or {}
    msgtype = content.get("msgtype")
    if msgtype not in ("m.text", "m.emote"):
        return None
    return content.get("body") or ""


def iter_room_events(events: Iterable[dict[str, Any]]) -> Iterable[dict[str, Any]]:
    for ev in events:
        if isinstance(ev, dict) and ev.get("room_id"):
            yield ev
