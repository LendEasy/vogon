"""The transfer state outlives the process that started it.

Twilio's agent-leg callbacks arrive after the media stream has ended —
seconds to minutes later — on whichever gateway worker the load balancer
picks. In-process state strands the agent on a restart or a second
worker ("Transfer session not found"). The Redis store is per-FIELD, so a
keypress (connected=True) and a status callback landing together cannot
overwrite each other.

    cd voice-gateway
    python3 -m pytest tests/escalation/test_transfer_store.py -q
"""
from __future__ import annotations

import asyncio

import fakeredis.aioredis

from vogon.gateway_service import routes
from vogon.gateway_service.config import GatewayConfig
from vogon.gateway_service.escalation import state as esc_state


def _state(**kw):
    base = dict(call_sid="CAx", owner_id="", workflow_id="",
                conference_name="esc_CAx", summary_text="w",
                config={"agent_connect_timeout_sec": 25},
                targets=[{"phone_number": "+1555", "label": "A"}])
    base.update(kw)
    return esc_state.TransferState(**base)


def _redis_store(server):
    store_cls = getattr(esc_state, "RedisTransferStore", None)
    assert store_cls is not None, "no Redis-backed transfer store exists"
    return store_cls(fakeredis.aioredis.FakeRedis(server=server,
                                                  decode_responses=True))


def test_a_transfer_written_by_one_worker_is_read_by_another():
    server = fakeredis.FakeServer()

    async def scenario():
        await _redis_store(server).set(_state(agent_call_sid="CAleg1"))
        return await _redis_store(server).get("CAx")

    got = asyncio.run(scenario())
    assert got is not None and got.agent_call_sid == "CAleg1"
    assert got.targets == [{"phone_number": "+1555", "label": "A"}]


def test_updates_are_per_field_and_never_resurrect_a_removed_transfer():
    server = fakeredis.FakeServer()

    async def scenario():
        a, b = _redis_store(server), _redis_store(server)
        await a.set(_state())
        await a.update("CAx", connected=True)          # keypress, worker A
        await b.update("CAx", current_target_idx=0)    # status, worker B
        both = await a.get("CAx")
        await a.remove("CAx")
        await b.update("CAx", connected=False)         # a late write
        return both, await a.get("CAx")

    both, after = asyncio.run(scenario())
    assert both.connected is True
    assert after is None


def test_compare_and_set_lets_exactly_one_callback_advance_a_leg():
    server = fakeredis.FakeServer()

    async def scenario():
        s = _redis_store(server)
        await s.set(_state(agent_call_sid="CAleg1"))
        first = await s.compare_and_set("CAx", {"agent_call_sid": "CAleg1"},
                                        {"agent_call_sid": "", "current_target_idx": 1})
        second = await s.compare_and_set("CAx", {"agent_call_sid": "CAleg1"},
                                         {"agent_call_sid": "", "current_target_idx": 2})
        return first, second, await s.get("CAx")

    first, second, got = asyncio.run(scenario())
    assert (first, second) == (True, False)
    assert got.current_target_idx == 1


def test_the_transfer_expires():
    server = fakeredis.FakeServer()

    async def scenario():
        s = _redis_store(server)
        await s.set(_state())
        return await s._r.ttl(s._key("CAx"))

    ttl = asyncio.run(scenario())
    assert 0 < ttl <= esc_state.TRANSFER_TTL_SEC


def test_the_gateway_boots_the_redis_transfer_store_with_the_redis_session_store(monkeypatch):
    fake = fakeredis.aioredis.FakeRedis(decode_responses=True)

    async def _get_redis():
        return fake
    monkeypatch.setattr("vogon.gateway.redis_client.get_redis", _get_redis)   # the client routes use (vogon step 2)
    asyncio.run(routes.default_store_from_config(
        GatewayConfig(session_store="redis")))
    assert isinstance(esc_state.transfer_store(), esc_state.RedisTransferStore)


def test_the_redis_counter_and_audio():
    server = fakeredis.FakeServer()

    async def scenario():
        s = _redis_store(server)
        await s.set(_state())
        seqs = [await s.incr("CAx", "event_seq") for _ in range(3)]
        await s.put_audio("tok1", b"\x00\xffID3 binary")
        audio = await s.get_audio("tok1")
        await s.remove("CAx")
        return seqs, audio, await s.incr("CAx", "event_seq")

    seqs, audio, after = asyncio.run(scenario())
    assert seqs == [1, 2, 3]
    assert audio == b"\x00\xffID3 binary"
    assert after == 0                       # no transfer, no number


def test_removing_a_transfer_deletes_its_audio():
    server = fakeredis.FakeServer()

    async def scenario():
        s = _redis_store(server)
        esc_state.use_transfer_store(s)
        await esc_state.set_transfer("CAx", _state(audio={"whisper": "tokW"}))
        await esc_state.put_audio("tokW", b"whisper")
        await esc_state.remove_transfer("CAx")
        return await esc_state.get_audio("tokW")

    assert asyncio.run(scenario()) is None
