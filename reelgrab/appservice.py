"""
mautrix-style Application Service HTTP server.

Synapse (and other homeservers) push events to registration ``url``:

  PUT  /_matrix/app/v1/transactions/{txnId}
  GET  /_matrix/app/v1/users/{userId}
  GET  /_matrix/app/v1/rooms/{roomAlias}

Legacy paths without the ``/_matrix/app/v1`` prefix are also accepted.
Auth: ``Authorization: Bearer <hs_token>`` or ``?access_token=<hs_token>``.
"""

from __future__ import annotations

import logging
from collections import OrderedDict
from collections.abc import Awaitable, Callable, Iterable
from typing import Any

from aiohttp import web

from reelgrab.config import AppConfig

log = logging.getLogger("reelgrab.appservice")

EventHandler = Callable[[list[dict[str, Any]]], Awaitable[None]]
ReadyCheck = Callable[[], bool]


class AppserviceNotReady(RuntimeError):
    """Raised when a transaction arrives before the homeserver client is ready.

    The HTTP handler turns this into 503 and forgets the txn id so the
    homeserver can deliver the same transaction again.
    """


class _TxnDeduper:
    """Remember recent transaction IDs so retries are idempotent."""

    def __init__(self, capacity: int = 512) -> None:
        self._capacity = max(32, capacity)
        self._seen: OrderedDict[str, None] = OrderedDict()

    def seen_or_add(self, txn_id: str) -> bool:
        if txn_id in self._seen:
            self._seen.move_to_end(txn_id)
            return True
        self._seen[txn_id] = None
        while len(self._seen) > self._capacity:
            self._seen.popitem(last=False)
        return False

    def discard(self, txn_id: str) -> None:
        self._seen.pop(txn_id, None)


class AppserviceServer:
    """HTTP endpoint the homeserver calls (mautrix / Matrix AS protocol)."""

    def __init__(
        self,
        cfg: AppConfig,
        *,
        on_events: EventHandler | None = None,
    ) -> None:
        self.cfg = cfg
        self._on_events = on_events
        self._ready_check: ReadyCheck | None = None
        self._txn = _TxnDeduper()
        self._runner: web.AppRunner | None = None
        self._site: web.TCPSite | None = None
        self.app = web.Application(middlewares=[self._auth_middleware])
        self._add_routes(self.app)

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

    def _add_routes(self, app: web.Application) -> None:
        # Spec paths
        app.router.add_put(
            "/_matrix/app/v1/transactions/{txnId}", self._put_transaction
        )
        app.router.add_get("/_matrix/app/v1/users/{userId}", self._get_user)
        app.router.add_get("/_matrix/app/v1/rooms/{roomAlias}", self._get_room)
        # Legacy paths (older Synapse / bridges)
        app.router.add_put("/transactions/{txnId}", self._put_transaction)
        app.router.add_get("/users/{userId}", self._get_user)
        app.router.add_get("/rooms/{roomAlias}", self._get_room)
        # Health for operators
        app.router.add_get("/_matrix/app/v1/thirdparty/protocol/{protocol}", self._empty_ok)
        app.router.add_get("/health", self._health)

    @web.middleware
    async def _auth_middleware(
        self, request: web.Request, handler: Callable
    ) -> web.StreamResponse:
        if request.path in ("/health", "/"):
            return await handler(request)
        if not self._authorized(request):
            return web.json_response(
                {"errcode": "M_FORBIDDEN", "error": "Invalid hs_token"},
                status=401,
            )
        return await handler(request)

    def _authorized(self, request: web.Request) -> bool:
        expected = (self.cfg.hs_token or "").strip()
        if not expected or expected.lower() == "generate":
            return False
        auth = request.headers.get("Authorization", "")
        if auth.lower().startswith("bearer "):
            token = auth[7:].strip()
            if token == expected:
                return True
        q = request.rel_url.query.get("access_token", "")
        return bool(q) and q == expected

    async def _put_transaction(self, request: web.Request) -> web.Response:
        txn_id = request.match_info.get("txnId", "")
        try:
            body = await request.json()
        except Exception:
            return web.json_response(
                {"errcode": "M_BAD_JSON", "error": "Invalid JSON"},
                status=400,
            )
        events = body.get("events") or []
        if not isinstance(events, list):
            return web.json_response(
                {"errcode": "M_BAD_JSON", "error": "events must be a list"},
                status=400,
            )

        if self._txn.seen_or_add(txn_id):
            log.debug("duplicate txn %s (%d events) — ack only", txn_id, len(events))
            return web.json_response({})

        log.info("txn %s: %d event(s)", txn_id, len(events))
        if self._on_events and events:
            # Process in-task but do not fail the HS ack on handler errors.
            # Not-ready is different: the homeserver should retry the txn.
            try:
                await self._on_events(events)
            except AppserviceNotReady:
                self._txn.discard(txn_id)
                log.info("txn %s deferred; client not ready", txn_id)
                return web.json_response(
                    {"errcode": "M_UNKNOWN", "error": "appservice not ready"},
                    status=503,
                )
            except Exception:
                log.exception("event handler failed for txn %s", txn_id)

        return web.json_response({})

    async def _get_user(self, request: web.Request) -> web.Response:
        user_id = request.match_info.get("userId", "")
        # Claim only our exclusive bot user (namespace is a single MXID).
        if user_id == self.cfg.user_id:
            log.debug("user query claim %s", user_id)
            return web.json_response({})
        return web.json_response(
            {"errcode": "M_NOT_FOUND", "error": "User not found"},
            status=404,
        )

    async def _get_room(self, request: web.Request) -> web.Response:
        # We do not claim room aliases.
        return web.json_response(
            {"errcode": "M_NOT_FOUND", "error": "Room not found"},
            status=404,
        )

    async def _empty_ok(self, request: web.Request) -> web.Response:
        return web.json_response({})

    async def _health(self, request: web.Request) -> web.Response:
        ready = self.ready
        return web.json_response(
            {"ok": ready, "ready": ready, "bot": self.cfg.user_id},
            status=200 if ready else 503,
        )

    async def start(self) -> None:
        host = self.cfg.appservice.hostname or "0.0.0.0"
        port = int(self.cfg.appservice.port or 29399)
        self._runner = web.AppRunner(self.app, access_log=None)
        await self._runner.setup()
        self._site = web.TCPSite(self._runner, host, port)
        await self._site.start()
        log.info(
            "appservice listening on %s:%s (hs url should be %s)",
            host,
            port,
            self.cfg.appservice.address,
        )

    async def stop(self) -> None:
        if self._runner is not None:
            await self._runner.cleanup()
            self._runner = None
            self._site = None


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
