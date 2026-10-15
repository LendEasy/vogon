"""Gateway clock engine — silence ladder, duration ladder, filler hold.

voice-gateway-phase0 §4.1/§7, migration step 3. Owns every timer the
reasoning side used to poll for; the reasoning side now only reacts to
SilenceThreshold / DurationThreshold events arriving on the transport's
utterance queue.

Silence re-arm rule (ratified 2026-07-21, phase0 doc §4.1; refined since).
Three things touch the window, and the difference between them is the whole
design:

  * CALLER ACTIVITY re-arms it — that is what the window measures. Every
    signal the caller is audibly there counts: finals, eager ends, absorbed
    barges, and the per-Update interims that carry a long uninterrupted
    answer (Flux sends no intermediate final, so without those the window
    accumulated straight through live speech and the probe was spoken over
    the caller). The transport gates the INTERIM half on not-playing —
    interims also fire over our own audio, and an echo must never re-arm.
  * A REAL AGENT TURN finishing resets it (note_playout_end(reset_window=
    True), and its stand-in when the mark is lost, the watchdog below).
    "Are you still there?" asks how long the caller has been quiet SINCE WE
    STOPPED TALKING.
  * THE PROBE'S OWN PLAYBACK only pauses: accumulated idle survives, so a
    fully silent call still reaches exit deterministically and is hung up.
    This is what replaced the old behavior, where exit reachability after a
    probe was a race with the Twilio mark echo (see git history of
    tests/test_silence_ladder.py).

The silence clock arms at the first playout end — silence before the agent
has ever spoken is not counted (same as the old seconds_since_last_speech
returning None before any TTS).

Gateway-pure module: imports the contract and stdlib only.
"""
from __future__ import annotations

import asyncio
import logging
import random
import time
from collections.abc import Awaitable, Callable, Iterator
from dataclasses import dataclass

from vogon_contract import (
    CallConfig,
    DurationThreshold,
    FillerPolicy,
    SilenceThreshold,
    Threshold,
)


def silence_deadline(threshold_id: str, ordinary_seconds: float, hold_seconds: float) -> float:
    """An explicit hold ends at its probe, with 15 seconds to respond.

    Ordinary idle windows keep their configured ladder. A hold replaces
    that ladder instead of adding a second ordinary grace to the pause.
    """
    if hold_seconds > 0:
        return hold_seconds + (15.0 if threshold_id == "exit" else 0.0)
    return ordinary_seconds


logger = logging.getLogger(__name__)

# Playout watchdog bounds (call audit P3 — lost delivery mark must not freeze
# the silence ladder). Blanket cap covers the no-length-known window between
# playout start and streaming completion ONLY; once the length is known it
# replaces the cap rather than being clamped by it. The margin covers telco
# buffering after streaming completes. Values and the incidents behind them:
# docs/voice-timing-constants.md §1.
PLAYOUT_STUCK_CAP_SEC = 90.0
PLAYOUT_MARK_MARGIN_SEC = 10.0

# A silence pause lifts itself after this long. The pause spans an outstanding
# turn request, and the turn client's ladder is bounded but NOT short: 6
# attempts at turn_service_timeout_sec (40s) plus ~15s of backoff is over four
# minutes. Freezing the ladder for that is the failure the P3 audit was written
# about — a swallowed mark held it for 9.5 minutes and the call ran to the
# max-duration watchdog. No legitimate reasoning gap approaches 30s, and the
# pause preserves accumulated idle, so the exit threshold stays reachable the
# moment it lifts. See docs/voice-timing-constants.md §2 for the
# whole silence picture, including which call mode configures which half.
SILENCE_PAUSE_CAP_SEC = 30.0


@dataclass(frozen=True)
class HoldReport:
    """Outcome of one begin_hold/end_hold span (the filler window)."""

    fillers_played: int = 0
    interrupted: bool = False   # a filler playout was truncated by a barge-in


class CallClocks:
    """Per-call clock engine owned by a transport.

    enqueue: puts a contract event on the transport's utterance queue
    (synchronous, e.g. queue.put_nowait).
    speak: the transport's own send_response — used for filler playback.
    """

    def __init__(
        self,
        config: CallConfig,
        enqueue: Callable[[object], None],
        speak: Callable[..., Awaitable[object]],
        *,
        tick_period: float = 1.0,
        audio_emit: Callable[..., None] | None = None,
    ) -> None:
        self._cfg = config
        self._enqueue = enqueue
        self._speak = speak
        self._tick_period = tick_period
        # Optional per-call audio-event sink (AudioEventLog.emit signature:
        # (now, event, **detail)). When wired (the Twilio transport passes
        # its log), the clock engine's decisions become visible on the audio
        # timeline: silence/duration ladder firings, the filler hold window,
        # and each filler playout. None = no-op (text/widget transports).
        self._audio_emit = audio_emit
        # Two silence ladders (VOICEMAIL_DETECTION_DESIGN.md §12): "default"
        # and the shorter "opening" one for the span before the far end has
        # answered. The turn side names the one in force at call start and
        # switches with ProcessEventsResponse.silence_ladder.
        self._ladders: dict[str, list[Threshold]] = {
            "default": sorted(config.silence_thresholds, key=lambda t: t.after_sec),
            "opening": sorted(getattr(config, "opening_silence_thresholds", []) or [],
                              key=lambda t: t.after_sec),
        }
        self._ladder_name = (getattr(config, "silence_ladder", "default") or "default")
        if not self._ladders.get(self._ladder_name):
            self._ladder_name = "default"
        self._silence_ths = self._ladders[self._ladder_name]
        self._duration_ths = sorted(config.duration_thresholds, key=lambda t: t.after_sec)

        self._task: asyncio.Task | None = None
        self._start_t = 0.0
        self._last_tick_t = 0.0
        self._dur_fired: set[str] = set()
        # Silence window state (accumulator model — the anchor survives
        # playouts; only caller activity resets it).
        self._sil_armed = False       # arms at first playout end
        self._sil_idle = 0.0          # accumulated idle seconds this window
        self._sil_fired: set[str] = set()
        # Outstanding caller-turn requests (nesting-safe). The cap's anchor is
        # stamped LAZILY at the first tick that observes the pause — same
        # reason as the playout watchdog's: one time source, and a fake
        # timeline stays testable.
        self._sil_pause_depth = 0
        self._sil_paused_since: float | None = None
        # Silence grace (an explicit hold the agent acknowledged): every rung
        # of the ladder is pushed out by this many seconds of idle, until the
        # caller is heard again. Granted per turn by the reasoning side
        # (ProcessEventsResponse.silence_grace_sec); the window itself is
        # still reset by the acknowledging turn's playout end, so the grace
        # counts from when we stopped talking, as the ladder does.
        self._sil_grace_sec = 0.0
        self._playing_depth = 0       # OVERLAPPING playouts: a front line and the
                                      # desk's answer can be in flight together
        self._playing = False         # agent audio playing or draining
        self._playing_since: float | None = None   # first tick observed playing
        self._playout_audio_ms: float | None = None  # known audio length (mark pending)
        # The watchdog times the NEWEST playout through the two fields above;
        # this holds the latest deadline of any EARLIER playout still live, so
        # a short line starting behind a long one cannot cut its watch short.
        self._watch_committed: float | None = None
        self._playing_is_turn = False   # is the current playout a REAL agent turn?
        # Filler hold state.
        self._hold_done: asyncio.Event | None = None
        self._hold_task: asyncio.Task | None = None
        self._hold_fillers = 0
        self._hold_interrupted = False
        self._hold_count = 0
        # The hold's LINE SOURCE (docs/voice-gateway-front-model.md): when the
        # driver installs a FrontModelSource the hold draws its words from it
        # instead of the FillerPolicy phrases — the same hold, the same
        # speak path, one sentence per _speak. None = the canned iterator.
        self._source = None
        # Timing knobs for the model source (settings, documented in
        # voice-timing-constants.md): set by install_line_source.
        self._src_beat_after_s = 0.9
        self._src_wait_again_s = 2.5
        self._src_beat_after_empty_s = None

    # ── Lifecycle ─────────────────────────────────────────────────────────

    def start(self, now: float | None = None) -> None:
        now = time.monotonic() if now is None else now
        self._start_t = now
        self._last_tick_t = now
        if self._silence_ths or self._duration_ths:
            self._task = asyncio.create_task(self._ticker())

    async def stop(self) -> None:
        for task in (self._task, self._hold_task):
            if task is not None and not task.done():
                task.cancel()
                try:
                    await task
                except (asyncio.CancelledError, Exception):
                    pass
        self._task = None
        self._hold_task = None

    # ── Signals from the transport ────────────────────────────────────────

    def note_caller_activity(self) -> None:
        """Caller made themselves heard (interim/final/absorbed barge) — the
        ONLY thing that re-arms the silence window. It also ends any hold
        grace: they are back, and the next hold is granted afresh."""
        self._sil_idle = 0.0
        self._sil_fired.clear()
        self._sil_grace_sec = 0.0

    def select_ladder(self, name: str) -> None:
        """Switch silence ladders mid-call. The idle accumulator is kept (a
        caller who went quiet before the switch is still quiet); the fired
        set is cleared because the new ladder's rungs have not fired — a
        rung already passed on the new ladder fires on the next tick, which
        is the honest reading of the idle it measures. Unknown or empty
        ladder names are ignored."""
        rungs = self._ladders.get(name)
        if not rungs or name == self._ladder_name:
            return
        self._ladder_name = name
        self._silence_ths = rungs
        self._sil_fired.clear()
        if self._audio_emit is not None:
            self._audio_emit(time.monotonic(), "silence_ladder", name=name,
                             idle_sec=round(self._sil_idle, 1))

    @property
    def ladder_name(self) -> str:
        return self._ladder_name

    def grant_silence_grace(self, seconds: float) -> None:
        """The agent acknowledged an explicit hold: defer every rung of the
        silence ladder by `seconds` of idle. A second hold while one is in
        force replaces it (back-to-back holds each get the full grace, since
        the acknowledging turn also resets the window). Non-positive = no
        grace."""
        self._sil_grace_sec = max(0.0, float(seconds))
        if self._audio_emit is not None:
            self._audio_emit(time.monotonic(), "silence_grace",
                             seconds=self._sil_grace_sec)

    def pause_silence(self) -> None:
        """A caller turn is being COMPUTED — stop counting it as their silence.

        "Are you still there?" asks how long the caller has been quiet since we
        stopped talking. The reasoning gap is neither: they answered, and we
        have not replied yet. Nothing paused the clock across it, so a turn
        slower than the alert threshold earned a probe — and because the
        processor loop is serial, that probe is delivered AFTER the real answer
        finishes playing, landing as "…here is your answer. Are you still
        there?".

        The filler hold was the only thing that ever paused this, and it cannot
        cover the case: the hold exists only while a FillerPolicy is configured
        (a per-assistant flag; until 2026-09-18 also never on a phone call), and
        the ladder must pause across the reasoning gap whether or not anyone
        chose fillers.

        PAUSE, never reset — the ratified rule (module header). Accumulated
        idle survives, so a caller who went quiet before the turn still reaches
        exit and a dead call is still hung up.
        """
        self._sil_pause_depth += 1

    def resume_silence(self) -> None:
        """The turn request finished (answered, failed, or gave up)."""
        self._sil_pause_depth = max(0, self._sil_pause_depth - 1)
        if self._sil_pause_depth == 0:
            self._sil_paused_since = None

    def note_playout_start(self, is_turn_audio: bool = False) -> None:
        """`is_turn_audio` marks a REAL agent turn (as opposed to the silence
        ladder's own probe). Only the watchdog below reads it — the ordinary
        completion path is told again, per-completion, by note_playout_end.
        Defaults False so a caller that does not say cannot accidentally reset
        a window, which is the same conservative default note_playout_end uses.
        """
        if self._playing_depth > 0:
            # An earlier playout is still live and its timing is about to be
            # overwritten by this one. Keep its deadline: the watchdog must
            # not release before the LONGEST live playout could have ended.
            earlier = self._newest_deadline()
            if earlier is not None:
                self._watch_committed = max(self._watch_committed or earlier, earlier)
        self._playing_depth += 1
        self._playing = True
        self._playing_is_turn = bool(is_turn_audio)
        # Watchdog anchor is set lazily at the first tick that observes
        # playing — keeps the clock testable on fake timelines and avoids a
        # second time source here.
        self._playing_since = None
        self._playout_audio_ms = None

    def _newest_deadline(self) -> float | None:
        """When the NEWEST playout must be treated as over if its mark never
        echoes: its known audio length plus margin, or the blanket cap while
        the length is not known yet. Measured from the first tick that saw it
        playing; before that tick, from the last tick (at most one tick
        early, against a 10 s margin)."""
        anchor = self._playing_since if self._playing_since is not None else self._last_tick_t
        if not anchor:
            return None
        limit = (
            PLAYOUT_STUCK_CAP_SEC if self._playout_audio_ms is None
            else self._playout_audio_ms / 1000.0 + PLAYOUT_MARK_MARGIN_SEC
        )
        return anchor + limit

    def note_playout_sent(self, audio_ms: float) -> None:
        """Streaming finished; `audio_ms` of audio is buffered at the telco and
        a mark is pending. Replaces the blanket stuck-playout cap with (audio
        length + margin) — a mark that hasn't echoed by then is lost (2026-07
        call audit P3: a swallowed mark froze the silence ladder for 9.5
        minutes and the call ran to the max-duration watchdog).

        `audio_ms` is EVERY frame this playout handed the telco, including the
        attempts before a mid-stream retry — they are still playing. The caller
        (twilio_transport) accumulates; passing only the last attempt's frames
        ends the playout while the earlier ones are still audible."""
        self._playout_audio_ms = float(audio_ms)

    def note_playout_end(self, reset_window: bool = False) -> None:
        """Agent audio finished actually playing (mark echo on Twilio).

        `reset_window` restarts the silence measurement from zero, and a real
        agent turn sets it. "Are you still there?" asks how long the caller has
        been quiet SINCE WE STOPPED TALKING; the window was instead measuring
        from their last activity, so everything in between — the LLM turn, the
        agent's whole spoken answer — was counted against them. On call
        c51a075a 3.8s of thinking time had already accrued before the agent
        opened its mouth, so the caller got 4.2s of the nominal 8s before the
        first threshold and 6.2s of the nominal 10s before being asked whether
        they were still there.

        A silence PROBE must NOT reset it: the probe is our own speech, and
        resetting on it would mean the exit threshold never arrives and a dead
        call is never hung up. That is why this defaults to False.
        """
        # OVERLAPPING PLAYOUTS. A single boolean cannot say "the front line
        # finished but the desk's answer is still playing", and on a real call
        # it said the wrong one: the gateway played "Let me check that for you
        # now.", the desk's 8.9s answer started behind it, the front line's
        # mark echoed, _playing went False, and the silence clock ran through
        # the agent's own speech until "Are you still there?" landed on top of
        # it. The caller was listening, not silent.
        #
        # The watchdog above still force-clears both, so a LOST mark cannot
        # leave the ladder frozen (call audit P3) — that failure direction is
        # the dangerous one and is unchanged.
        self._playing_depth = max(0, self._playing_depth - 1)
        self._playing = self._playing_depth > 0
        if not self._playing:
            self._playing_since = None
            self._playout_audio_ms = None
            self._watch_committed = None
        self._sil_armed = True
        if reset_window:
            self._sil_idle = 0.0
            self._sil_fired.clear()

    # ── Ticker ────────────────────────────────────────────────────────────

    async def _ticker(self) -> None:
        while True:
            await asyncio.sleep(self._tick_period)
            self.tick(time.monotonic())

    def tick(self, now: float) -> None:
        """One clock tick. Public for tests — pure state + enqueue calls."""
        dt = now - self._last_tick_t
        self._last_tick_t = now

        elapsed = now - self._start_t
        for th in self._duration_ths:
            if th.id not in self._dur_fired and elapsed >= th.after_sec:
                self._dur_fired.add(th.id)
                if self._audio_emit is not None:
                    self._audio_emit(now, "clock_duration",
                                     threshold_id=th.id,
                                     elapsed_sec=round(elapsed, 1))
                self._enqueue(DurationThreshold(
                    threshold_id=th.id, elapsed_sec=elapsed, at=now,
                ))

        # ── Playout watchdog (call audit P3): a lost mark must have a bounded
        # effect. While playing, bound the wait by the known audio length +
        # margin (or the blanket cap); past the bound, treat the playout as
        # ended so the silence ladder resumes.
        if self._playing:
            if self._playing_since is None:
                self._playing_since = now
            # The blanket cap covers ONLY the window where no length is known.
            # Once the transport reports how much audio it handed the telco,
            # that length IS the answer and the cap must not clamp it: on call
            # 18b3f7a3 a playout carried 84.6s of audio across two attempts, and
            # a 90s cap measured from playout start left 6s of slack against a
            # measured jitter lead of 592ms — the same defect with a longer
            # fuse. A playout cannot be over before the telco has had time to
            # play what it holds.
            deadline = self._newest_deadline()
            if self._watch_committed is not None:
                deadline = max(deadline, self._watch_committed)
            if now >= deadline:
                logger.warning(
                    "playout watchdog: mark not echoed %.1fs after playout "
                    "start — treating every live playout as ended (silence "
                    "ladder resumes)", now - self._playing_since,
                )
                self._playing_depth = 0
                self._playing = False
                self._playing_since = None
                self._playout_audio_ms = None
                self._watch_committed = None
                self._sil_armed = True
                # Stand in for the mark echo that never came, INCLUDING its
                # window reset. Without this the watchdog un-paused the clock
                # but left the idle banked before the agent spoke intact, so a
                # real turn whose mark was lost spent the caller's whole grace
                # window: measured, with 7 s banked, warn fired 1 s after the
                # watchdog released and the probe 2 s later — over a caller who
                # had just been asked a question. A PROBE still must not reset
                # (the exit threshold has to stay reachable on a dead call),
                # which is the distinction _is_turn_audio already draws at the
                # mark-echo site.
                if self._playing_is_turn:
                    self._sil_idle = 0.0
                    self._sil_fired.clear()
                self._playing_is_turn = False

        # ── Silence pause (an outstanding caller-turn request) ──────────
        # Self-lifting: the pause is released by the request's own finally, but
        # the ladder is what hangs up a call nobody is on, so it must not be
        # holdable indefinitely by a turn service that is timing out and
        # retrying.
        if self._sil_pause_depth > 0:
            if self._sil_paused_since is None:
                self._sil_paused_since = now
            elif now - self._sil_paused_since >= SILENCE_PAUSE_CAP_SEC:
                logger.warning(
                    "silence pause held %.0fs — lifting; the turn side has "
                    "been computing this turn longer than any real gap",
                    now - self._sil_paused_since,
                )
                self._sil_pause_depth = 0
                self._sil_paused_since = None

        if self._sil_armed and not self._playing and self._sil_pause_depth == 0:
            self._sil_idle += dt
            for th in self._silence_ths:
                if (th.id not in self._sil_fired
                        and self._sil_idle >= silence_deadline(
                            th.id, th.after_sec, self._sil_grace_sec)):
                    self._sil_fired.add(th.id)
                    if self._audio_emit is not None:
                        self._audio_emit(now, "clock_silence",
                                         threshold_id=th.id,
                                         silent_for_sec=round(self._sil_idle, 1))
                    self._enqueue(SilenceThreshold(
                        threshold_id=th.id, silent_for_sec=self._sil_idle, at=now,
                    ))

    # ── Filler hold ───────────────────────────────────────────────────────

    def install_line_source(self, source, *, beat_after_ms: float,
                            wait_again_after_ms: float,
                            beat_after_empty_ms: float | None = None) -> None:
        """Give the hold a line source (the front model + its beat pools).
        With a source installed the hold opens on every final whether or not
        a FillerPolicy was configured — the source IS the filler."""
        self._source = source
        self._src_beat_after_s = max(0.0, beat_after_ms / 1000.0)
        self._src_wait_again_s = max(0.0, wait_again_after_ms / 1000.0)
        self._src_beat_after_empty_s = (None if beat_after_empty_ms is None
                                        else max(0.0, beat_after_empty_ms / 1000.0))

    def install_hold_speak(self, speak: Callable[..., Awaitable[object]]) -> None:
        """Route the hold's playouts through the driver's send path so each
        one produces a DeliveryOutcome fact like any other playout — the
        only way a gateway-authored line reaches the call log."""
        self._speak = speak

    def begin_hold(self) -> bool:
        """Start the filler window (reasoning side is computing a turn).
        Returns True when a filler policy or a line source is active (a
        HoldReport will be meaningful); False when fillers are not configured."""
        if (self._cfg.filler is None and self._source is None) or self._hold_task is not None:
            return self._hold_task is not None
        self._hold_fillers = 0
        self._hold_interrupted = False
        self._hold_done = asyncio.Event()
        if self._audio_emit is not None:
            self._audio_emit(time.monotonic(), "hold_open")
        self._hold_task = asyncio.create_task(
            self._run_source_hold() if self._source is not None
            else self._run_hold(self._cfg.filler))
        return True

    async def end_hold(self) -> HoldReport:
        """End the filler window. An in-flight filler playout finishes
        naturally first (same as the old inline loop — the response never
        cuts a filler mid-word)."""
        task, self._hold_task = self._hold_task, None
        if task is not None:
            if self._hold_done is not None:
                self._hold_done.set()
            try:
                await task
            except asyncio.CancelledError:
                pass
            if self._audio_emit is not None:
                self._audio_emit(time.monotonic(), "hold_close",
                                 fillers_played=self._hold_fillers,
                                 interrupted=self._hold_interrupted)
        return HoldReport(self._hold_fillers, self._hold_interrupted)

    async def _wait_or_end(self, secs: float) -> bool:
        """Wait secs; True if the hold is still active afterwards."""
        try:
            await asyncio.wait_for(self._hold_done.wait(), timeout=secs)
            return False
        except asyncio.TimeoutError:
            return True

    async def _run_hold(self, policy: FillerPolicy) -> None:
        if not await self._wait_or_end(policy.first_after_ms / 1000.0):
            return
        self._hold_count += 1
        for phrase in _filler_phrases(policy):
            if self._hold_fillers >= policy.max or self._hold_done.is_set():
                return
            audio_id = f"filler-{self._hold_count}-{self._hold_fillers + 1}"
            logger.info(
                "FILLER | sending filler %d/%d | id=%s | text=%r",
                self._hold_fillers + 1, policy.max, audio_id, phrase,
            )
            if self._audio_emit is not None:
                self._audio_emit(time.monotonic(), "filler_start",
                                 audio_id=audio_id, text=phrase)
            dr = await self._speak(phrase, audio_id)
            self._hold_fillers += 1
            if dr is not None and getattr(dr, "status", "") == "truncated":
                self._hold_interrupted = True
                return
            if not await self._wait_or_end(policy.interval_ms / 1000.0):
                return


    async def _run_source_hold(self) -> None:
        """The hold with a line source: the model's sentence as soon as it is
        ready (or a beat at beat_after), then at most one wait beat. Same
        contract as _run_hold — one phrase per _speak, a truncated playout
        ends the hold, end_hold lets the sentence in flight finish."""
        source = self._source
        self._hold_count += 1
        # Tier 1 (or the hold beat if the model had nothing by the deadline).
        # A hold that ends while we wait (the answer beat the line) means
        # nothing plays — the drop.
        waiter = asyncio.ensure_future(
            source.hold_line(self._src_beat_after_s, self._src_beat_after_empty_s))
        done_wait = asyncio.ensure_future(self._hold_done.wait())
        try:
            await asyncio.wait({waiter, done_wait}, return_when=asyncio.FIRST_COMPLETED)
        finally:
            done_wait.cancel()
        if self._hold_done.is_set():
            waiter.cancel()
            try:
                await waiter
            except (asyncio.CancelledError, Exception):
                pass
            return
        text, audio_id = await waiter
        if not text:
            return
        if not await self._play_source_line(text, audio_id):
            return
        # Tier 0: still waiting after the line played.
        if not await self._wait_or_end(self._src_wait_again_s):
            return
        text, audio_id = source.wait_line()
        if text:
            await self._play_source_line(text, audio_id)

    async def _play_source_line(self, text: str, audio_id: str) -> bool:
        """One front playout. False when the hold must stop (a barge)."""
        if self._hold_done.is_set():
            return False
        logger.info("FRONT | speaking | id=%s | text=%r", audio_id, text)
        if self._audio_emit is not None:
            self._audio_emit(time.monotonic(), "front_start", audio_id=audio_id, text=text)
        dr = await self._speak(text, audio_id)
        self._hold_fillers += 1
        self._source.note_played(text)
        if dr is not None and getattr(dr, "status", "") == "truncated":
            self._hold_interrupted = True
            return False
        return True


def _filler_phrases(policy: FillerPolicy) -> Iterator[str]:
    """One random primary, then continuations cycled in shuffled order —
    mirrors app.pipeline.fillers.filler_sequence, driven by policy data."""
    if policy.phrases:
        yield random.choice(policy.phrases)
    pool = list(policy.continuations)
    if not pool:
        pool = list(policy.phrases)
    while pool:
        random.shuffle(pool)
        yield from pool
