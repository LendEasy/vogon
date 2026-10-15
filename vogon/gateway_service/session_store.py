"""The gateway's session store — its ONE persistence dependency, modeled.

Design: docs/voice-gateway-phase1-standalone.md §2.4. Transport state only,
never meaning: the application's ``CallRecord`` remains the business record;
the two share nothing but ``call_id``. Replaces the app-DB lookups the WS
endpoints and TwiML webhook used to make, and survives a gateway restart
between dial and answer (any replica can serve a webhook; only the media
socket pins, via ``claim_socket``).

Schema is VERSIONED (``schema_version``); readers migrate-on-read, never
discard — the same rule Phase 0 set for the app's session state.
"""
from __future__ import annotations

import json
import logging
import time
from dataclasses import asdict, dataclass, field
from typing import Optional, Protocol

logger = logging.getLogger(__name__)

SCHEMA_VERSION = 1

# created → dialing → ringing → connected → ended | failed | cancelled
STATES = ("created", "dialing", "ringing", "connected",
          "ended", "failed", "cancelled")
_TERMINAL = ("ended", "failed", "cancelled")

_LIVE_TTL_SEC = 86400     # a record's life while the call may still exist
_ENDED_TTL_SEC = 3600     # kept briefly after end for late webhooks/pollers


@dataclass
class CallSessionRecord:
    """One call's transport state — schema v1 (plan §2.4)."""

    tenant: str
    call_id: str
    session_token: str
    mode: str                              # twilio | widget | text
    app_base_url: str = ""
    app_auth_token: str = ""               # *_ref indirection is M2 hardening
    app_status_webhook: str = ""
    app_payload: dict = field(default_factory=dict)   # opaque, echoed verbatim
    route: str = ""                        # opaque sub-scope label
    state: str = "created"
    provider_call_id: str = ""             # Twilio CallSid, bound at dial
    call_config: Optional[dict] = None     # resolved snapshot (config §2.5)
    gateway_instance: str = ""             # which replica holds the socket
    created_at_utc: float = 0.0
    connected_at_utc: float = 0.0
    ended_at_utc: float = 0.0
    schema_version: int = SCHEMA_VERSION

    def to_json(self) -> str:
        return json.dumps(asdict(self))

    @classmethod
    def from_json(cls, raw: str) -> CallSessionRecord:
        data = json.loads(raw)
        data.pop("schema_version", None)
        known = {f for f in cls.__dataclass_fields__ if f != "schema_version"}
        # migrate-on-read: unknown fields from a NEWER writer are dropped,
        # absent fields take defaults — never discard the record.
        return cls(**{k: v for k, v in data.items() if k in known})


class GatewaySessionStore(Protocol):
    """Injected at boot; every operation is tenant-checked where a tenant is
    known. Index lookups (token, provider id) return the record with its
    tenant — the caller verifies scope."""

    async def create(self, rec: CallSessionRecord) -> None: ...
    async def get(self, tenant: str, call_id: str) -> Optional[CallSessionRecord]: ...
    async def get_by_token(self, token: str) -> Optional[CallSessionRecord]: ...
    async def get_by_provider_id(self, provider_call_id: str) -> Optional[CallSessionRecord]: ...
    async def bind_provider_id(self, tenant: str, call_id: str, provider_call_id: str) -> None: ...
    async def transition(self, tenant: str, call_id: str, state: str) -> None: ...
    async def save_call_config(self, tenant: str, call_id: str, snapshot: dict) -> None: ...
    async def claim_socket(self, tenant: str, call_id: str, instance_id: str) -> bool: ...


class MemorySessionStore:
    """Single-process store: dev, text mode, tests."""

    def __init__(self) -> None:
        self._by_key: dict[tuple[str, str], CallSessionRecord] = {}
        self._token_idx: dict[str, tuple[str, str]] = {}
        self._sid_idx: dict[str, tuple[str, str]] = {}
        self._claims: dict[tuple[str, str], str] = {}

    async def create(self, rec: CallSessionRecord) -> None:
        rec.created_at_utc = rec.created_at_utc or time.time()
        self._by_key[(rec.tenant, rec.call_id)] = rec
        self._token_idx[rec.session_token] = (rec.tenant, rec.call_id)

    async def get(self, tenant: str, call_id: str) -> Optional[CallSessionRecord]:
        return self._by_key.get((tenant, call_id))

    async def get_by_token(self, token: str) -> Optional[CallSessionRecord]:
        key = self._token_idx.get(token)
        return self._by_key.get(key) if key else None

    async def get_by_provider_id(self, provider_call_id: str) -> Optional[CallSessionRecord]:
        key = self._sid_idx.get(provider_call_id)
        return self._by_key.get(key) if key else None

    async def bind_provider_id(self, tenant: str, call_id: str,
                               provider_call_id: str) -> None:
        rec = self._by_key[(tenant, call_id)]
        rec.provider_call_id = provider_call_id
        self._sid_idx[provider_call_id] = (tenant, call_id)

    async def transition(self, tenant: str, call_id: str, state: str) -> None:
        rec = self._by_key.get((tenant, call_id))
        if rec is None:
            return
        rec.state = state
        now = time.time()
        if state == "connected":
            rec.connected_at_utc = rec.connected_at_utc or now
        if state in _TERMINAL:
            rec.ended_at_utc = rec.ended_at_utc or now

    async def save_call_config(self, tenant: str, call_id: str,
                               snapshot: dict) -> None:
        rec = self._by_key.get((tenant, call_id))
        if rec is not None:
            rec.call_config = snapshot

    async def claim_socket(self, tenant: str, call_id: str,
                           instance_id: str) -> bool:
        holder = self._claims.setdefault((tenant, call_id), instance_id)
        return holder == instance_id


class RedisSessionStore:
    """Production store. Layout (§2.6 — every key tenant-prefixed):

        gw:{tenant}:call:{call_id}   the record, JSON
        gw:idx:token:{token}         "tenant|call_id"
        gw:idx:sid:{provider_id}     "tenant|call_id"
        gw:{tenant}:claim:{call_id}  socket claim (SET NX)

    Index values carry the tenant so scope is verified against the record,
    never trusted from the caller.
    """

    def __init__(self, redis) -> None:
        self._r = redis

    @staticmethod
    def _key(tenant: str, call_id: str) -> str:
        return f"gw:{tenant}:call:{call_id}"

    async def _write(self, rec: CallSessionRecord, ttl: int) -> None:
        await self._r.set(self._key(rec.tenant, rec.call_id), rec.to_json(),
                          ex=ttl)

    async def create(self, rec: CallSessionRecord) -> None:
        rec.created_at_utc = rec.created_at_utc or time.time()
        await self._write(rec, _LIVE_TTL_SEC)
        await self._r.set(f"gw:idx:token:{rec.session_token}",
                          f"{rec.tenant}|{rec.call_id}", ex=_LIVE_TTL_SEC)

    async def get(self, tenant: str, call_id: str) -> Optional[CallSessionRecord]:
        raw = await self._r.get(self._key(tenant, call_id))
        if raw is None:
            return None
        if isinstance(raw, bytes):
            raw = raw.decode("utf-8")
        return CallSessionRecord.from_json(raw)

    async def _get_by_index(self, idx_key: str) -> Optional[CallSessionRecord]:
        ref = await self._r.get(idx_key)
        if ref is None:
            return None
        if isinstance(ref, bytes):
            ref = ref.decode("utf-8")
        tenant, _, call_id = ref.partition("|")
        return await self.get(tenant, call_id)

    async def get_by_token(self, token: str) -> Optional[CallSessionRecord]:
        return await self._get_by_index(f"gw:idx:token:{token}")

    async def get_by_provider_id(self, provider_call_id: str) -> Optional[CallSessionRecord]:
        return await self._get_by_index(f"gw:idx:sid:{provider_call_id}")

    async def bind_provider_id(self, tenant: str, call_id: str,
                               provider_call_id: str) -> None:
        rec = await self.get(tenant, call_id)
        if rec is None:
            return
        rec.provider_call_id = provider_call_id
        await self._write(rec, _LIVE_TTL_SEC)
        await self._r.set(f"gw:idx:sid:{provider_call_id}",
                          f"{tenant}|{call_id}", ex=_LIVE_TTL_SEC)

    async def transition(self, tenant: str, call_id: str, state: str) -> None:
        rec = await self.get(tenant, call_id)
        if rec is None:
            return
        rec.state = state
        now = time.time()
        if state == "connected":
            rec.connected_at_utc = rec.connected_at_utc or now
        if state in _TERMINAL:
            rec.ended_at_utc = rec.ended_at_utc or now
        await self._write(rec,
                          _ENDED_TTL_SEC if state in _TERMINAL else _LIVE_TTL_SEC)

    async def save_call_config(self, tenant: str, call_id: str,
                               snapshot: dict) -> None:
        rec = await self.get(tenant, call_id)
        if rec is None:
            return
        rec.call_config = snapshot
        await self._write(rec, _LIVE_TTL_SEC)

    async def claim_socket(self, tenant: str, call_id: str,
                           instance_id: str) -> bool:
        key = f"gw:{tenant}:claim:{call_id}"
        claimed = await self._r.set(key, instance_id, nx=True, ex=_LIVE_TTL_SEC)
        if claimed:
            return True
        holder = await self._r.get(key)
        if isinstance(holder, bytes):
            holder = holder.decode("utf-8")
        return holder == instance_id
