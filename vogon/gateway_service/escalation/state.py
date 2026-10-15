"""State for active escalation transfers.

Twilio's agent-leg callbacks (whisper, keypress, status) and the caller's
own status webhook arrive AFTER the media stream has ended — seconds to
minutes later, on whichever gateway worker receives them. In production the
state therefore lives in Redis beside the session store (booted together in
routes.default_store_from_config); the in-process store serves dev, text
mode and tests.

The Redis layout is one HASH per transfer, ``gw:esc:{caller_call_sid}``,
one field per attribute. Updates write only the fields they name, so a
keypress (connected=True) and a status callback landing together on two
workers cannot overwrite each other, and an update never recreates a
transfer that was already removed. Advancing to the next leg is a
compare-and-set on the leg's sid, so a duplicate or late status callback
cannot dial twice.
"""
from __future__ import annotations

import asyncio
import base64
import json
from dataclasses import asdict, dataclass, field, fields
from typing import Optional, Protocol

# A transfer's life: hold + up to N agent legs (each ≤ the order's timeout,
# Twilio caps a dial at 600 s) + a bridged conversation. The TTL bounds an
# orphan (a lost final callback) — it is not a limit on a live transfer's
# talk time, which needs no state once connected.
TRANSFER_TTL_SEC = 4 * 3600


@dataclass
class TransferState:
    call_sid: str
    owner_id: str
    workflow_id: str
    conference_name: str
    summary_text: str
    config: dict
    targets: list[dict]          # enabled targets ordered by priority
    current_target_idx: int = 0
    agent_call_sid: str | None = None
    connected: bool = False      # True once caller+agent are in conference
    agent_answered: bool = False  # the current leg fetched its whisper
    # Reporting (post-call facts → the turn service, escalation/report.py)
    call_id: str = ""            # the turn service's call id
    escalation_id: str = ""      # the order's join key
    report_url: str = ""         # the turn service base URL (…/turn/v1)
    report_token: str = ""       # its bearer, as the session record holds it
    event_seq: int = 0           # last fact seq issued (next_seq)
    legs_placed: int = 0
    hold_started_utc: float = 0.0
    connected_utc: float = 0.0
    resolved: bool = False       # the one EscalationResolved was issued
    # Lines rendered in the persona's voice: name → audio token (escalation/
    # voice.py). Deleted with the transfer — the whisper names the caller.
    audio: dict = field(default_factory=dict)
    audio_failed: dict = field(default_factory=dict)   # name → why it is <Say>
    audio_rendering: list = field(default_factory=list)  # lines still to land


_FIELDS = {f.name for f in fields(TransferState)}


class TransferStore(Protocol):
    async def set(self, state: TransferState) -> None: ...
    async def get(self, call_sid: str) -> Optional[TransferState]: ...
    async def update(self, call_sid: str, **changes) -> bool: ...
    async def compare_and_set(self, call_sid: str, expect: dict,
                              changes: dict) -> bool: ...
    async def incr(self, call_sid: str, field: str) -> int: ...
    async def remove(self, call_sid: str) -> None: ...
    async def put_audio(self, token: str, audio: bytes) -> None: ...
    async def get_audio(self, token: str) -> Optional[bytes]: ...
    async def delete_audio(self, tokens: list[str]) -> None: ...
    async def expire_audio(self, tokens: list[str], ttl_sec: float) -> None: ...


class MemoryTransferStore:
    """Single-process store: dev, text mode, tests."""

    def __init__(self) -> None:
        self._store: dict[str, TransferState] = {}
        self._audio: dict[str, bytes] = {}
        self._lock = asyncio.Lock()

    async def put_audio(self, token: str, audio: bytes) -> None:
        self._audio[token] = audio

    async def get_audio(self, token: str) -> Optional[bytes]:
        return self._audio.get(token)

    async def delete_audio(self, tokens: list[str]) -> None:
        for t in tokens:
            self._audio.pop(t, None)

    async def expire_audio(self, tokens: list[str], ttl_sec: float) -> None:
        loop = asyncio.get_running_loop()
        for t in tokens:
            loop.call_later(ttl_sec, self._audio.pop, t, None)

    async def set(self, state: TransferState) -> None:
        async with self._lock:
            self._store[state.call_sid] = TransferState(**asdict(state))

    async def get(self, call_sid: str) -> Optional[TransferState]:
        async with self._lock:
            state = self._store.get(call_sid)
            return TransferState(**asdict(state)) if state else None

    async def update(self, call_sid: str, **changes) -> bool:
        return await self.compare_and_set(call_sid, {}, changes)

    async def compare_and_set(self, call_sid: str, expect: dict,
                              changes: dict) -> bool:
        async with self._lock:
            state = self._store.get(call_sid)
            if state is None or any(getattr(state, k) != v
                                    for k, v in expect.items()):
                return False
            for k, v in changes.items():
                setattr(state, k, v)
            return True

    async def incr(self, call_sid: str, field: str) -> int:
        async with self._lock:
            state = self._store.get(call_sid)
            if state is None:
                return 0
            setattr(state, field, getattr(state, field) + 1)
            return getattr(state, field)

    async def remove(self, call_sid: str) -> None:
        async with self._lock:
            self._store.pop(call_sid, None)


# KEYS[1] = the transfer hash. ARGV = n_expect, then n_expect (field, json)
# pairs to compare, then (field, json) pairs to write. Absent hash = no-op.
_CAS_LUA = """
if redis.call('exists', KEYS[1]) == 0 then return 0 end
local n = tonumber(ARGV[1])
for i = 0, n - 1 do
  if redis.call('hget', KEYS[1], ARGV[2 + 2*i]) ~= ARGV[3 + 2*i] then return 0 end
end
local w = {}
for i = 2 + 2*n, #ARGV do w[#w + 1] = ARGV[i] end
if #w > 0 then redis.call('hset', KEYS[1], unpack(w)) end
return 1
"""

# Atomic counter on one field of an existing transfer; 0 when it is gone.
# The JSON encoding of an int is its decimal text, which HINCRBY accepts.
_INCR_LUA = """
if redis.call('exists', KEYS[1]) == 0 then return 0 end
return redis.call('hincrby', KEYS[1], ARGV[1], 1)
"""


class RedisTransferStore:
    """Production store: one hash per transfer, JSON-encoded fields."""

    def __init__(self, redis) -> None:
        self._r = redis

    @staticmethod
    def _key(call_sid: str) -> str:
        return f"gw:esc:{call_sid}"

    @staticmethod
    def _enc(value) -> str:
        return json.dumps(value, sort_keys=True)

    async def set(self, state: TransferState) -> None:
        key = self._key(state.call_sid)
        mapping = {k: self._enc(v) for k, v in asdict(state).items()}
        # One transaction on one key: a crash cannot leave a TTL-less hash.
        async with self._r.pipeline(transaction=True) as pipe:
            pipe.delete(key)
            pipe.hset(key, mapping=mapping)
            pipe.expire(key, TRANSFER_TTL_SEC)
            await pipe.execute()

    async def get(self, call_sid: str) -> Optional[TransferState]:
        raw = await self._r.hgetall(self._key(call_sid))
        if not raw:
            return None
        data = {}
        for k, v in raw.items():
            k = k.decode() if isinstance(k, bytes) else k
            v = v.decode() if isinstance(v, bytes) else v
            if k in _FIELDS:
                data[k] = json.loads(v)
        return TransferState(**data)

    async def update(self, call_sid: str, **changes) -> bool:
        return await self.compare_and_set(call_sid, {}, changes)

    async def compare_and_set(self, call_sid: str, expect: dict,
                              changes: dict) -> bool:
        unknown = (set(expect) | set(changes)) - _FIELDS
        if unknown:
            raise ValueError(f"unknown transfer fields: {sorted(unknown)}")
        args: list = [len(expect)]
        for k, v in expect.items():
            args += [k, self._enc(v)]
        for k, v in changes.items():
            args += [k, self._enc(v)]
        return bool(await self._r.eval(_CAS_LUA, 1, self._key(call_sid), *args))

    async def incr(self, call_sid: str, field: str) -> int:
        if field not in _FIELDS:
            raise ValueError(f"unknown transfer field: {field}")
        return int(await self._r.eval(_INCR_LUA, 1, self._key(call_sid), field))

    async def remove(self, call_sid: str) -> None:
        await self._r.delete(self._key(call_sid))

    # Audio is binary; the shared client decodes responses as text, so it is
    # stored base64-encoded, one key per line, with the transfer's TTL.
    @staticmethod
    def _audio_key(token: str) -> str:
        return f"gw:esc:audio:{token}"

    async def put_audio(self, token: str, audio: bytes) -> None:
        await self._r.set(self._audio_key(token),
                          base64.b64encode(audio).decode("ascii"),
                          ex=TRANSFER_TTL_SEC)

    async def get_audio(self, token: str) -> Optional[bytes]:
        raw = await self._r.get(self._audio_key(token))
        if raw is None:
            return None
        return base64.b64decode(raw if isinstance(raw, (bytes, str)) else str(raw))

    async def delete_audio(self, tokens: list[str]) -> None:
        for t in tokens:
            await self._r.delete(self._audio_key(t))

    async def expire_audio(self, tokens: list[str], ttl_sec: float) -> None:
        for t in tokens:
            await self._r.expire(self._audio_key(t), int(ttl_sec))


_transfer_store: TransferStore = MemoryTransferStore()


def transfer_store() -> TransferStore:
    return _transfer_store


def use_transfer_store(store: TransferStore) -> None:
    global _transfer_store
    _transfer_store = store


# ── The verbs the orchestrator and the callbacks use ────────────────────────

async def set_transfer(call_sid: str, state: TransferState) -> None:
    assert state.call_sid == call_sid
    await _transfer_store.set(state)


async def get_transfer(call_sid: str) -> TransferState | None:
    return await _transfer_store.get(call_sid)


async def update_transfer(call_sid: str, **kwargs) -> bool:
    return await _transfer_store.update(call_sid, **kwargs)


async def claim_transfer(call_sid: str, expect: dict, changes: dict) -> bool:
    """Atomically apply `changes` only if every `expect` field still holds —
    exactly one of two racing callbacks wins."""
    return await _transfer_store.compare_and_set(call_sid, expect, changes)


async def count_leg_placed(call_sid: str) -> int:
    """One more agent leg was placed (ringing) for this transfer."""
    return await _transfer_store.incr(call_sid, "legs_placed")


async def next_seq(call_sid: str) -> int:
    """The transfer's next post-call fact number (1, 2, …); 0 = no transfer."""
    return await _transfer_store.incr(call_sid, "event_seq")


# A fallback line (voicemail, callback) is fetched by Twilio AFTER the
# transfer closed — the voicemail call is answered seconds later, the
# callback TwiML plays after the update. In Redis the audio already carries
# the transfer's TTL; the in-process store expires a kept line after this.
KEPT_AUDIO_TTL_SEC = 600.0


async def remove_transfer(call_sid: str, keep_audio: tuple[str, ...] = ()) -> None:
    """The transfer is over: its state goes, and with it every rendered
    line that can no longer play. ``keep_audio`` names the lines Twilio
    still has to fetch (the fallback's voicemail and callback, found
    2026-10-01 on the local run: both 404ed before Twilio could play
    them); those live on until their TTL."""
    state = await _transfer_store.get(call_sid)
    if state is not None and state.audio:
        gone = [t for n, t in state.audio.items() if n not in keep_audio]
        kept = [t for n, t in state.audio.items() if n in keep_audio]
        if gone:
            await _transfer_store.delete_audio(gone)
        if kept:
            await _transfer_store.expire_audio(kept, KEPT_AUDIO_TTL_SEC)
    await _transfer_store.remove(call_sid)


async def put_audio(token: str, audio: bytes) -> None:
    await _transfer_store.put_audio(token, audio)


async def delete_audio(tokens: list[str]) -> None:
    await _transfer_store.delete_audio(tokens)


async def get_audio(token: str) -> Optional[bytes]:
    return await _transfer_store.get_audio(token)
