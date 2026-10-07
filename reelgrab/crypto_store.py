"""SQLite-backed Olm store.

mautrix ships an in-memory store and a Postgres store. reelgrab keeps the
account, sessions, and device list in the data directory so a restart does
not mint a new device and break encrypted rooms.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timedelta
from pathlib import Path

from mautrix.crypto.account import OlmAccount
from mautrix.crypto.sessions import (
    InboundGroupSession,
    OutboundGroupSession,
    RatchetSafety,
    Session,
)
from mautrix.crypto.store.memory import MemoryCryptoStore
from mautrix.types import (
    CrossSigner,
    CrossSigningUsage,
    DeviceID,
    DeviceIdentity,
    EventID,
    IdentityKey,
    RoomID,
    SessionID,
    SigningKey,
    TOFUSigningKey,
    TrustState,
    UserID,
)


def _dt(value: str | None) -> datetime | None:
    if not value:
        return None
    return datetime.fromisoformat(value)


def _iso(value: datetime | None) -> str | None:
    if value is None:
        return None
    return value.isoformat()


class SQLiteCryptoStore(MemoryCryptoStore):
    """MemoryCryptoStore that rewrites a sqlite file after each mutation."""

    def __init__(self, path: str | Path, pickle_key: str, account_id: str = "reelgrab") -> None:
        super().__init__(account_id=account_id, pickle_key=pickle_key)
        self.path = Path(path)
        self._db: sqlite3.Connection | None = None

    async def open(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._db = sqlite3.connect(self.path, check_same_thread=False)
        self._db.execute("PRAGMA journal_mode=WAL")
        self._schema()
        self._load()

    async def flush(self) -> None:
        self._save()

    async def close(self) -> None:
        self._save()
        if self._db is not None:
            self._db.close()
            self._db = None

    def _schema(self) -> None:
        assert self._db is not None
        self._db.executescript(
            """
            CREATE TABLE IF NOT EXISTS account (
                id INTEGER PRIMARY KEY CHECK (id = 1),
                pickle BLOB NOT NULL,
                shared INTEGER NOT NULL
            );
            CREATE TABLE IF NOT EXISTS device_id (
                id INTEGER PRIMARY KEY CHECK (id = 1),
                device_id TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS olm_session (
                identity_key TEXT NOT NULL,
                idx INTEGER NOT NULL,
                pickle BLOB NOT NULL,
                creation TEXT,
                last_encrypted TEXT,
                last_decrypted TEXT,
                PRIMARY KEY (identity_key, idx)
            );
            CREATE TABLE IF NOT EXISTS inbound_session (
                room_id TEXT NOT NULL,
                session_id TEXT NOT NULL,
                pickle BLOB NOT NULL,
                signing_key TEXT NOT NULL,
                sender_key TEXT NOT NULL,
                forwarding TEXT NOT NULL,
                ratchet TEXT NOT NULL,
                received_at TEXT,
                max_age_ms INTEGER,
                max_messages INTEGER,
                is_scheduled INTEGER NOT NULL,
                PRIMARY KEY (room_id, session_id)
            );
            CREATE TABLE IF NOT EXISTS outbound_session (
                room_id TEXT PRIMARY KEY,
                pickle BLOB NOT NULL,
                max_age_ms INTEGER,
                max_messages INTEGER,
                creation TEXT,
                use_time TEXT,
                message_count INTEGER,
                shared INTEGER NOT NULL,
                shared_with TEXT NOT NULL,
                ignored TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS message_index (
                sender_key TEXT NOT NULL,
                session_id TEXT NOT NULL,
                idx INTEGER NOT NULL,
                event_id TEXT NOT NULL,
                timestamp INTEGER NOT NULL,
                PRIMARY KEY (sender_key, session_id, idx)
            );
            CREATE TABLE IF NOT EXISTS tracked_user (
                user_id TEXT PRIMARY KEY
            );
            CREATE TABLE IF NOT EXISTS device (
                user_id TEXT NOT NULL,
                device_id TEXT NOT NULL,
                identity_key TEXT NOT NULL,
                signing_key TEXT NOT NULL,
                trust INTEGER NOT NULL,
                deleted INTEGER NOT NULL,
                name TEXT NOT NULL,
                PRIMARY KEY (user_id, device_id)
            );
            CREATE TABLE IF NOT EXISTS cross_signing (
                user_id TEXT NOT NULL,
                usage TEXT NOT NULL,
                key TEXT NOT NULL,
                first TEXT NOT NULL,
                PRIMARY KEY (user_id, usage)
            );
            CREATE TABLE IF NOT EXISTS signature (
                signer_user TEXT NOT NULL,
                signer_key TEXT NOT NULL,
                target_user TEXT NOT NULL,
                target_key TEXT NOT NULL,
                signature TEXT NOT NULL,
                PRIMARY KEY (signer_user, signer_key, target_user, target_key)
            );
            """
        )

    def _save(self) -> None:
        db = self._db
        if db is None:
            return
        key = self.pickle_key
        db.execute("DELETE FROM account")
        if self._account is not None:
            db.execute(
                "INSERT INTO account (id, pickle, shared) VALUES (1, ?, ?)",
                (self._account.pickle(key), 1 if self._account.shared else 0),
            )
        db.execute("DELETE FROM device_id")
        if self._device_id:
            db.execute(
                "INSERT INTO device_id (id, device_id) VALUES (1, ?)",
                (self._device_id,),
            )
        db.execute("DELETE FROM olm_session")
        for identity, sessions in self._olm_sessions.items():
            for idx, session in enumerate(sessions):
                db.execute(
                    """
                    INSERT INTO olm_session (
                        identity_key, idx, pickle, creation, last_encrypted, last_decrypted
                    ) VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (
                        identity,
                        idx,
                        session.pickle(key),
                        _iso(session.creation_time),
                        _iso(session.last_encrypted),
                        _iso(session.last_decrypted),
                    ),
                )
        db.execute("DELETE FROM inbound_session")
        for (room_id, session_id), session in self._inbound_sessions.items():
            ratchet = session.ratchet_safety
            db.execute(
                """
                INSERT INTO inbound_session (
                    room_id, session_id, pickle, signing_key, sender_key, forwarding,
                    ratchet, received_at, max_age_ms, max_messages, is_scheduled
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    room_id,
                    session_id,
                    session.pickle(key),
                    session.signing_key,
                    session.sender_key,
                    json.dumps(list(session.forwarding_chain)),
                    json.dumps(
                        {
                            "next_index": ratchet.next_index,
                            "missed_indices": list(ratchet.missed_indices),
                            "lost_indices": list(ratchet.lost_indices),
                        }
                    ),
                    _iso(session.received_at),
                    int(session.max_age.total_seconds() * 1000) if session.max_age else None,
                    session.max_messages,
                    1 if session.is_scheduled else 0,
                ),
            )
        db.execute("DELETE FROM outbound_session")
        for room_id, session in self._outbound_sessions.items():
            db.execute(
                """
                INSERT INTO outbound_session (
                    room_id, pickle, max_age_ms, max_messages, creation, use_time,
                    message_count, shared, shared_with, ignored
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    room_id,
                    session.pickle(key),
                    int(session.max_age.total_seconds() * 1000) if session.max_age else None,
                    session.max_messages,
                    _iso(session.creation_time),
                    _iso(session.use_time),
                    session.message_count,
                    1 if session.shared else 0,
                    json.dumps([list(pair) for pair in session.users_shared_with]),
                    json.dumps([list(pair) for pair in session.users_ignored]),
                ),
            )
        db.execute("DELETE FROM message_index")
        for (sender, session_id, index), (event_id, timestamp) in self._message_indices.items():
            db.execute(
                """
                INSERT INTO message_index (sender_key, session_id, idx, event_id, timestamp)
                VALUES (?, ?, ?, ?, ?)
                """,
                (sender, session_id, index, event_id, timestamp),
            )
        db.execute("DELETE FROM tracked_user")
        db.execute("DELETE FROM device")
        for user_id, devices in self._devices.items():
            db.execute("INSERT INTO tracked_user (user_id) VALUES (?)", (user_id,))
            for device in devices.values():
                db.execute(
                    """
                    INSERT INTO device (
                        user_id, device_id, identity_key, signing_key, trust, deleted, name
                    ) VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        device.user_id,
                        device.device_id,
                        device.identity_key,
                        device.signing_key,
                        int(device.trust),
                        1 if device.deleted else 0,
                        device.name or "",
                    ),
                )
        db.execute("DELETE FROM cross_signing")
        for user_id, usages in self._cross_signing_keys.items():
            for usage, tofu in usages.items():
                db.execute(
                    """
                    INSERT INTO cross_signing (user_id, usage, key, first)
                    VALUES (?, ?, ?, ?)
                    """,
                    (user_id, str(usage), tofu.key, tofu.first),
                )
        db.execute("DELETE FROM signature")
        for signer, targets in self._signatures.items():
            for target, signature in targets.items():
                db.execute(
                    """
                    INSERT INTO signature (
                        signer_user, signer_key, target_user, target_key, signature
                    ) VALUES (?, ?, ?, ?, ?)
                    """,
                    (signer.user_id, signer.key, target.user_id, target.key, signature),
                )
        db.commit()

    def _load(self) -> None:
        db = self._db
        assert db is not None
        key = self.pickle_key
        row = db.execute("SELECT pickle, shared FROM account").fetchone()
        if row:
            self._account = OlmAccount.from_pickle(bytes(row[0]), key, bool(row[1]))
        dev = db.execute("SELECT device_id FROM device_id").fetchone()
        self._device_id = DeviceID(dev[0]) if dev else None

        self._olm_sessions = {}
        for identity, _idx, blob, creation, last_enc, last_dec in db.execute(
            "SELECT identity_key, idx, pickle, creation, last_encrypted, last_decrypted "
            "FROM olm_session ORDER BY identity_key, idx"
        ):
            created = _dt(creation) or datetime.now()
            session = Session.from_pickle(
                bytes(blob),
                key,
                creation_time=created,
                last_encrypted=_dt(last_enc),
                last_decrypted=_dt(last_dec),
            )
            self._olm_sessions.setdefault(IdentityKey(identity), []).append(session)

        self._inbound_sessions = {}
        for row in db.execute("SELECT * FROM inbound_session"):
            (
                room_id,
                session_id,
                blob,
                signing_key,
                sender_key,
                forwarding,
                ratchet_raw,
                received_at,
                max_age_ms,
                max_messages,
                is_scheduled,
            ) = row
            ratchet_data = json.loads(ratchet_raw)
            session = InboundGroupSession.from_pickle(
                bytes(blob),
                key,
                signing_key=SigningKey(signing_key),
                sender_key=IdentityKey(sender_key),
                room_id=RoomID(room_id),
                forwarding_chain=json.loads(forwarding),
                ratchet_safety=RatchetSafety(
                    next_index=int(ratchet_data.get("next_index") or 0),
                    missed_indices=list(ratchet_data.get("missed_indices") or []),
                    lost_indices=list(ratchet_data.get("lost_indices") or []),
                ),
                received_at=_dt(received_at),
                max_age=timedelta(milliseconds=max_age_ms) if max_age_ms is not None else None,
                max_messages=max_messages,
                is_scheduled=bool(is_scheduled),
            )
            self._inbound_sessions[(RoomID(room_id), SessionID(session_id))] = session

        self._outbound_sessions = {}
        for row in db.execute("SELECT * FROM outbound_session"):
            (
                room_id,
                blob,
                max_age_ms,
                max_messages,
                creation,
                use_time,
                message_count,
                shared,
                shared_with,
                ignored,
            ) = row
            created = _dt(creation) or datetime.now()
            if max_age_ms is None:
                max_age = timedelta(days=7)
            else:
                max_age = timedelta(milliseconds=max_age_ms)
            session = OutboundGroupSession.from_pickle(
                bytes(blob),
                key,
                max_age=max_age,
                max_messages=int(max_messages or 100),
                creation_time=created,
                use_time=_dt(use_time) or created,
                message_count=int(message_count or 0),
                room_id=RoomID(room_id),
                shared=bool(shared),
            )
            session.users_shared_with = {tuple(pair) for pair in json.loads(shared_with)}
            session.users_ignored = {tuple(pair) for pair in json.loads(ignored)}
            self._outbound_sessions[RoomID(room_id)] = session

        self._message_indices = {}
        for sender, session_id, index, event_id, timestamp in db.execute(
            "SELECT sender_key, session_id, idx, event_id, timestamp FROM message_index"
        ):
            self._message_indices[(IdentityKey(sender), SessionID(session_id), int(index))] = (
                EventID(event_id),
                int(timestamp),
            )

        self._devices = {
            UserID(user_id): {}
            for (user_id,) in db.execute("SELECT user_id FROM tracked_user")
        }
        for row in db.execute(
            "SELECT user_id, device_id, identity_key, signing_key, trust, deleted, name FROM device"
        ):
            user_id, device_id, identity_key, signing_key, trust, deleted, name = row
            identity = DeviceIdentity(
                user_id=UserID(user_id),
                device_id=DeviceID(device_id),
                identity_key=IdentityKey(identity_key),
                signing_key=SigningKey(signing_key),
                trust=TrustState(int(trust)),
                deleted=bool(deleted),
                name=name or "",
            )
            self._devices.setdefault(UserID(user_id), {})[DeviceID(device_id)] = identity

        self._cross_signing_keys = {}
        for user_id, usage, current, first in db.execute(
            "SELECT user_id, usage, key, first FROM cross_signing"
        ):
            self._cross_signing_keys.setdefault(UserID(user_id), {})[
                CrossSigningUsage(usage)
            ] = TOFUSigningKey(key=SigningKey(current), first=SigningKey(first))

        self._signatures = {}
        for signer_user, signer_key, target_user, target_key, signature in db.execute(
            "SELECT signer_user, signer_key, target_user, target_key, signature FROM signature"
        ):
            signer = CrossSigner(user_id=UserID(signer_user), key=SigningKey(signer_key))
            target = CrossSigner(user_id=UserID(target_user), key=SigningKey(target_key))
            self._signatures.setdefault(signer, {})[target] = signature

    async def redact_expired_group_sessions(self) -> list[SessionID]:
        return []

    async def redact_outdated_group_sessions(self) -> list[SessionID]:
        return []


def _persist(method_name: str):
    base = getattr(MemoryCryptoStore, method_name)

    async def wrapper(self: SQLiteCryptoStore, *args, **kwargs):
        result = await base(self, *args, **kwargs)
        self._save()
        return result

    return wrapper


for _name in (
    "put_device_id",
    "delete",
    "put_account",
    "add_session",
    "update_session",
    "put_group_session",
    "redact_group_session",
    "redact_group_sessions",
    "add_outbound_group_session",
    "update_outbound_group_session",
    "remove_outbound_group_session",
    "remove_outbound_group_sessions",
    "validate_message_index",
    "put_devices",
    "put_cross_signing_key",
    "put_signature",
    "drop_signatures_by_key",
):
    setattr(SQLiteCryptoStore, _name, _persist(_name))
