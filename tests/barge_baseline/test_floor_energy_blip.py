"""Energy after the caller's final never takes the floor; only words do.

Call b59be90d (2026-09-30 01:55, audio event log): the floor opened at
162.651 s on the caller's final, a 220 ms burst the STT never transcribed
(episode 15, revision 0) took it back at 162.690 s and bumped the
generation 7 → 8, the desk's reply resp-9 was discarded at 163.875 s as
`stale_turn_response`, and — with no words to set final_seen — the floor
stayed the caller's for ten seconds, until they asked "Are you still
there?". The delivery fact called it a barge-in; nothing had played.
"""
import asyncio

from vogon.gateway.transport.floor_controller import FloorController


async def frames(h, count, level=3000):
    for _ in range(count):
        await h.frame(raw=level, clean=level)
        await asyncio.sleep(0)


def _no_playback(baseline):
    t = baseline.t
    t._tts_playing = False
    t._floor = FloorController(t, baseline.clock.monotonic)
    return t


async def _caller_final(h, t, text):
    await t.start()
    await t._stt.interim(text)
    await frames(h, 3)
    await frames(h, 25, 0)            # the pause Flux needs to close the turn
    await t._stt.final(text)
    await frames(h, 30, 0)            # EOT + quiet: the floor opens
    assert not t._floor.caller_floor
    return t.playback_generation()    # stamped on the UtteranceFinal


def test_a_burst_after_the_final_leaves_the_floor_open_and_the_reply_plays(baseline):
    h = baseline
    t = _no_playback(baseline)

    async def scenario():
        generation = await _caller_final(h, t, "I'm off until November so I can't really pay anything")
        rows_before = len(h.names())
        await frames(h, 11)               # 220 ms over the barge threshold, no transcript
        await frames(h, 30, 0)
        assert not t._floor.caller_floor
        assert generation == t.playback_generation()
        assert await t._floor.allow_playback(generation)   # resp-9 plays
        assert 'floor_acquired' not in h.names()[rows_before:]

    asyncio.run(scenario())


def test_continuous_noise_with_no_transcript_never_moves_the_generation(baseline):
    h = baseline
    t = _no_playback(baseline)

    async def scenario():
        generation = await _caller_final(h, t, "so I can't really pay anything")
        await frames(h, 250)              # five seconds over the threshold
        assert not t._floor.caller_floor
        assert generation == t.playback_generation()
        assert await t._floor.allow_playback(generation)

    asyncio.run(scenario())


def test_words_after_the_burst_still_supersede_the_reply(baseline):
    """Control: a caller who really resumed takes the floor through the
    partial their speech produces, frames first or not."""
    h = baseline
    t = _no_playback(baseline)

    async def scenario():
        generation = await _caller_final(h, t, "my situation changed")
        await frames(h, 11)               # the frames always reach us before the partial
        await t._stt.interim("and there is more")
        assert t._floor.caller_floor
        assert generation != t.playback_generation()
        assert not await t._floor.allow_playback(generation)
        await frames(h, 30, 0)
        assert t._floor.caller_floor      # released only by the new final
        await t._stt.final("and there is more")
        await frames(h, 30, 0)
        assert not t._floor.caller_floor

    asyncio.run(scenario())
