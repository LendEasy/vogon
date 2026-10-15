"""Episode and floor arbitration for the Twilio adapter.

Frame evidence never gates the open STT feed. Only one classifier request is
in flight; changed text replaces pending work. Policy, deadlines and playback
ownership are deterministic and all decisions use the audio event stream.
"""
from __future__ import annotations

import asyncio
from collections import deque
from dataclasses import dataclass

from vogon.gateway.settings import settings
from vogon.gateway.transport.barge_semantics import classify_floor, heuristic_verdict, is_echo_of


@dataclass
class Episode:
    id: int
    onset: float
    generation: int
    last_speech: float
    speech_ms: float = 0
    text: str = ''
    revision: int = 0
    qualified: bool = False
    ended: bool = False
    decision: str = 'UNCERTAIN'
    failed_at: float | None = None
    final: bool = False
    echo: bool = False
    possible_echo: bool = False
    deferred: bool = False
    heard: str = ''
    unheard: str = ''


class FloorController:
    def __init__(self, transport, now):
        self.t = transport
        self.now = now
        self.episodes: deque[Episode] = deque(maxlen=16)
        self.current: Episode | None = None
        self.serial = 0
        self.generation = 0
        self.caller_floor = False
        # A final has been dispatched and the desk's reply for it has not
        # been admitted yet. Words in this window (a partial) mean the
        # caller resumed before the response — the reply is stale.
        self.awaiting_reply = False
        self.last_speech = 0.0
        self.final_seen = False
        self.released = asyncio.Event()
        self.released.set()
        self.protection_released = asyncio.Event()
        self.protection_released.set()
        self.worker: asyncio.Task | None = None
        self.timer: asyncio.Task | None = None
        self.pending = None
        self.requested = None
        self.accepted_generation = None
        self.pending_text = None

    @property
    def active_playback(self):
        return self.t._tts_playing or self.t._playback_draining

    def emit(self, event, reason, e=None, **detail):
        e = e or self.current
        self.t._audio_log.emit(self.now(), event, schema_version=1,
            reason=reason, episode_id=e.id if e else None,
            revision=e.revision if e else None, generation=self.generation,
            audio_id=self.t._last_audio_id,
            floor='caller' if self.caller_floor else 'agent' if self.active_playback else 'open',
            protected=self.t._protected_playout, intro_absorb=self.t._absorb_playout,
            unsuccessful_attempts=[p.id for p in self.episodes if p.failed_at is not None and
                                   self.now()-p.failed_at <= settings.barge_attempt_history_ms/1000],
            speech_ms=round(e.speech_ms) if e else 0, **detail)

    def start(self):
        if self.timer is None:
            self.timer = asyncio.create_task(self._heartbeat())
        self.emit('floor_policy', 'controller_started',
            decision_ms=settings.semantic_barge_window_ms,
            repeat_ms=settings.barge_repeat_speech_ms,
            persistent_ms=settings.barge_persistent_speech_ms,
            episode_gap_ms=settings.barge_episode_gap_ms,
            silence_ms=settings.barge_floor_silence_ms)

    async def _heartbeat(self):
        while True:
            await asyncio.sleep(.05)
            await self.tick()

    async def close(self):
        for task in (self.timer, self.worker):
            if task is not None:
                task.cancel()
        await asyncio.gather(*(t for t in (self.timer, self.worker) if t is not None),
                             return_exceptions=True)

    def episode(self):
        e = self.current
        if e is None or e.ended:
            # A new acoustic episode after the caller's final does NOT take
            # the floor: only words do (resume(), from a transcript or Flux's
            # TurnResumed). Energy alone used to bump the generation here.
            # Call b59be90d (2026-09-30): a 220 ms burst the STT never
            # transcribed took the floor 40 ms after it opened, the reply
            # computed for the caller's final was discarded as stale, and
            # with no words to set final_seen the floor stayed the caller's
            # for ten seconds — until the caller asked "Are you still
            # there?". A caller who really resumed is covered by the
            # partial that follows within a few hundred ms.
            self.serial += 1
            e = Episode(self.serial, self.now(), self.generation, self.now())
            e.heard, e.unheard = self.t._playout_heard_split()
            if self.pending_text is not None:
                e.text = self.pending_text
                e.revision = 1
                self.pending_text = None
            self.episodes.append(e)
            self.current = e
            self.emit('speech_episode_started', 'new_speech_attempt', e)
        return e

    def resume(self):
        """Words from the caller while nothing of ours is playing (a partial,
        or Flux's TurnResumed). If a final was dispatched since they took
        the floor, the reply being computed for it is stale: the generation
        moves. Energy never reaches here."""
        if self.caller_floor:
            if self.final_seen:
                self.generation += 1
                self.emit('floor_acquired', 'caller_resumed_after_final')
            self.final_seen = False
        elif not self.active_playback:
            self.generation += 1
            self.caller_floor = True
            self.t.pause_silence()
            self.final_seen = False
            self.released.clear()
            self.emit('floor_acquired', 'caller_resumed_before_response')

    async def frame(self, rms, threshold, duration_ms, confirmed):
        now = self.now()
        if rms > threshold:
            e = self.episode()
            e.speech_ms += min(duration_ms, 100)
            e.final = False
            e.last_speech = self.last_speech = now
            # Energy while the caller holds the floor does NOT undo their
            # EOT (it used to clear final_seen, and only a new final could
            # set it again — a breath inside the quiet window held the
            # floor, and the reply, until the caller spoke again). It only
            # restarts the quiet timer through last_speech.
            if confirmed and not e.qualified:
                e.qualified = True
                self.emit('speech_episode_qualified', 'energy_gate_confirmed', e,
                          clean_rms=rms, threshold=threshold)
                await self.review(e)
        elif self.current and not self.current.ended:
            if now - self.current.last_speech >= settings.barge_episode_gap_ms / 1000:
                self.current.ended = True
                self.emit('speech_episode_ended', 'speech_gap', self.current)
                # Gate rearming follows acoustic episodes, not partial revisions.
                self.t._barge_gate.reset()
        await self.tick()

    async def transcript(self, text, final=False):
        e = self.current
        if e is None:
            e = self.episode()
        elif e.ended and not final:
            # A delayed revision during silence is not a new acoustic attempt.
            # If new speech follows, bind this latest text when frames open it.
            self.pending_text = text
        if final:
            self.pending_text = None
        changed = text != e.text
        if text and (not final or changed) and (
                self.caller_floor or (self.awaiting_reply and not self.active_playback)):
            # Words from a caller who holds the floor, or who resumed after
            # a final whose reply is still to come: the reply computed for
            # that final is stale. This is the ONLY way energy after a
            # final turns into a floor — via the transcript it produces,
            # never the frames (frame() clears e.final, so the old
            # `e.final` test here missed every resumption the frames
            # reached first). Words over our playout with the floor open
            # still go through review().
            self.resume()
        if changed:
            if e.echo:
                e.speech_ms = 0
                e.qualified = False
                self.t._barge_gate.reset()
            e.text = text
            e.revision += 1
            e.decision = 'UNCERTAIN'
            e.echo = False
            self.emit('speech_revision', 'final' if final else 'partial', e, text=text)
        e.final = final
        if final:
            self.awaiting_reply = True
        if self.caller_floor and final:
            self.final_seen = True
        if final or changed:
            await self.review(e, final=final)
        if final:
            # EOT callback may wait for bounded semantic arbitration; the
            # websocket frame task and open STT feed continue independently.
            if e.decision == 'UNCERTAIN' and self.worker is not None and not self.worker.done():
                try:
                    await asyncio.wait_for(asyncio.shield(self.worker), settings.semantic_barge_llm_timeout_ms/1000)
                except asyncio.TimeoutError:
                    e.revision += 1  # later verdict cannot cut an already dispatched final
                    self.emit('floor_result_rejected', 'final_decision_deadline', e)
            # Commit a bounded answer for STT EOT; never wait on a provider in
            # the inbound frame loop. A pending classifier may revise pressure.
            if e.decision == 'UNCERTAIN' and self.active_playback and not self.caller_floor:
                self.fail(e, 'eot_unresolved')
            await self.tick()
        return e

    def fail(self, e, reason):
        if e.failed_at is None and not e.echo and not e.possible_echo:
            e.failed_at = self.now()
            self.emit('speech_attempt_unsuccessful', reason, e)

    def history(self, e):
        return [dict(episode_id=p.id, text=p.text, decision=p.decision,
                     unsuccessful=p.failed_at is not None,
                     age_ms=round((self.now()-p.onset)*1000))
                for p in self.episodes if p is not e and
                self.now()-p.last_speech <= settings.barge_attempt_history_ms/1000]

    async def review(self, e, final=False):
        if not e.text or not self.active_playback or self.caller_floor:
            return
        t = self.t
        e.possible_echo = False
        if is_echo_of(e.text, t._last_speak_text):
            e.echo = True
            e.failed_at = None
            await self.decide(e, 'CONTINUE', 'agent_echo', suppress=True)
            return
        h = heuristic_verdict(e.text,
            safety_phrases=t._safety_phrases, floor_phrases=t._floor_phrases,
            playout_text=t._last_speak_text,
            backchannel_max_tokens=settings.semantic_barge_backchannel_max_tokens,
            substantive_min_tokens=settings.semantic_barge_substantive_min_tokens)
        # A short matching partial (e.g. "Let me") can be the beginning of
        # agent echo. Wait for another revision or EOT before the classifier
        # can turn it into a cut. Explicit policy phrases still take priority.
        if not final and h == 'ambiguous' and is_echo_of(e.text, t._last_speak_text, min_words=2):
            e.possible_echo = True
            e.decision = 'UNCERTAIN'
            self.emit('floor_decision', 'possible_agent_echo', e,
                      decision='UNCERTAIN', action='GATHER_MORE')
            return
        if h == 'safety':
            await self.decide(e, 'YIELD', 'policy_breakthrough', breakthrough=True)
            return
        if t._protected_playout and t._absorb_playout:
            await self.decide(e, 'CONTINUE', 'protected_intro_absorb', suppress=True)
            return
        if not (e.qualified or final):
            self.emit('floor_decision', 'waiting_for_energy_gate', e,
                      decision='UNCERTAIN', action='GATHER_MORE')
            return
        if h == 'backchannel':
            await self.decide(e, 'CONTINUE', 'cooperative_backchannel')
        elif h in ('floor_request', 'substantive_interrupt'):
            await self.decide(e, 'YIELD', 'policy_floor_request' if h == 'floor_request' else 'substantive_speech')
        elif t._protected_playout:
            e.deferred = True
            self.protection_released.clear()
            self.emit('floor_decision', 'protected_span', e, decision='UNCERTAIN', action='DEFER')
        elif t._semantic_judge:
            key = (e.id, e.revision, self.generation, t._last_audio_id)
            if key != self.requested:
                self.pending = (e, key)
                if self.worker is None or self.worker.done():
                    self.worker = asyncio.create_task(self._classify())
                    t._yield_task = self.worker
        else:
            await self.decide(e, 'UNCERTAIN', 'classifier_disabled')

    async def _classify(self):
        while self.pending:
            e, key = self.pending
            self.pending = None
            self.requested = key
            heard, unheard = e.heard, e.unheard
            self.emit('floor_classifier_requested', 'ambiguous_speech', e,
                      heard_text=heard, unheard_text=unheard, text=e.text,
                      decision='UNCERTAIN', action='GATHER_MORE')
            started = self.now()
            try:
                request = asyncio.get_running_loop().run_in_executor(
                    None, classify_floor, e.text, heard, unheard,
                    self.t._last_speak_text, self.history(e))
                decision = await asyncio.wait_for(asyncio.shield(request), settings.semantic_barge_llm_timeout_ms/1000)
                reason = 'classifier_result'
            except asyncio.TimeoutError:
                decision, reason = 'UNCERTAIN', 'classifier_timeout'
            except Exception:
                decision, reason = 'UNCERTAIN', 'classifier_error'
            if key != (e.id, e.revision, self.generation, self.t._last_audio_id) or self.caller_floor or not self.active_playback:
                self.emit('floor_result_rejected', 'stale_revision_or_generation', e,
                          request_revision=key[1], request_generation=key[2])
            else:
                if decision not in ('CONTINUE', 'YIELD', 'UNCERTAIN'):
                    decision, reason = 'UNCERTAIN', 'invalid_classifier_result'
                self.emit('floor_classifier_result', reason, e, decision=decision,
                          latency_ms=round((self.now()-started)*1000))
                await self.decide(e, decision, reason)
            if reason == 'classifier_timeout':
                # A cancelled executor future does not cancel the underlying
                # provider call. Drain it before dispatching the latest revision.
                try:
                    await asyncio.shield(request)
                except Exception:
                    pass
                self.emit('floor_result_rejected', 'result_after_deadline', e)

    async def decide(self, e, decision, reason, breakthrough=False, suppress=False):
        e.decision = decision
        if decision == 'YIELD':
            if self.t._protected_playout and not breakthrough:
                e.deferred = True
                self.protection_released.clear()
                self.fail(e, 'protected_span')
                self.emit('floor_decision', 'protected_span', e, decision=decision, action='DEFER')
                return
            await self.yield_floor(e, reason)
        else:
            if decision == 'CONTINUE':
                e.failed_at = None
                e.deferred = False
                if not any(p.deferred for p in self.episodes):
                    self.protection_released.set()
            self.emit('floor_decision', reason, e, decision=decision,
                      action='KEEP_PLAYING' if decision == 'CONTINUE' else 'GATHER_MORE')
            # Suppression is provisional: every new partial/final is reviewed.
            self.t._absorbing_utterance = decision == 'CONTINUE'
            self.t._absorb_detail = dict(text=e.text,
                verdict='echo' if e.echo else 'intro_absorb' if suppress else 'backchannel',
                source='floor_controller', audio_id=self.t._last_audio_id)
        await self.tick()

    async def yield_floor(self, e, reason, cut=True):
        if self.caller_floor:
            return
        self.caller_floor = True
        self.t.pause_silence()
        self.final_seen = e.final and not any(p.last_speech > e.last_speech for p in self.episodes)
        if cut:
            self.generation += 1
        self.pending = None
        for episode in self.episodes:
            episode.deferred = False
        self.protection_released.set()
        self.released.clear()
        self.t._absorbing_utterance = False
        self.t._absorb_detail = None
        self.t._gap_active = False
        self.t._typing_arm_deadline = 0
        if self.t._ambience is not None:
            self.t._ambience.stop_typing()
        self.emit('floor_decision', reason, e, decision='YIELD', action='CUT' if cut else 'TAKE_FLOOR')
        if cut:
            was_streaming = self.t._tts_playing
            self.t._emit_barge_cut_telemetry(self.t._candidate_raw_rms, self.t._candidate_clean_rms)
            await self.t._cut_playback()
            if was_streaming:
                # Audible cut must not wait for the provider's next TTS chunk.
                await self.t._send_clear()
                self.t._pending_marks.clear()
                self.t._tts_ended_at = self.now()
                self.t._close_ledger(self.t._tts_ended_at)
                self.t._note_playout_end(reset_window=False)

    async def tick(self):
        now = self.now()
        if self.caller_floor:
            if self.final_seen and now-self.last_speech >= settings.barge_floor_silence_ms/1000:
                self.caller_floor = False
                self.t.resume_silence()
                self.released.set()
                self.emit('floor_released', 'caller_eot_and_silence')
            return
        e = self.current
        if not self.active_playback and not self.protection_released.is_set():
            await self.playback_finished()
            return
        if not e or not self.active_playback or e.echo or e.possible_echo:
            return
        if e.text and e.decision == 'UNCERTAIN' and now-e.onset >= settings.semantic_barge_window_ms/1000:
            self.fail(e, 'decision_deadline')
        if self.t._protected_playout or not e.qualified or now-e.last_speech > .1:
            return
        repeat = any(p is not e and p.failed_at is not None and
                     now-p.failed_at <= settings.barge_attempt_history_ms/1000
                     for p in self.episodes)
        if repeat and e.speech_ms >= settings.barge_repeat_speech_ms:
            await self.yield_floor(e, 'repeated_speech')
        elif e.text and e.speech_ms >= settings.barge_persistent_speech_ms:
            await self.yield_floor(e, 'persistent_speech')

    def take_floor_from_machine(self, audio_id=None):
        """The line verdict says a machine answered: there is no caller to
        yield to. The held voicemail playout takes the floor whatever the
        line's energy says. Production call 4108c2eb (2026-09-24): the box's
        beep after the greeting's final reset `final_seen`, no words ever
        set it again, `released` never fired, and the composed message was
        never played in the 50 s before the carrier hung up."""
        was_caller = self.caller_floor
        self.caller_floor = False
        self.awaiting_reply = False
        self.final_seen = False
        if was_caller:
            self.t.resume_silence()
        for episode in self.episodes:
            episode.deferred = False
        self.released.set()
        self.protection_released.set()
        self.emit('floor_decision', 'line_machine_playout', decision='AGENT',
                  action='TAKE_FLOOR', requested_audio_id=audio_id,
                  was_caller_floor=was_caller)

    async def playback_finished(self):
        self.protection_released.set()
        pending = next((e for e in reversed(self.episodes) if e.deferred), None)
        if pending:
            for e in self.episodes:
                e.deferred = False
            await self.yield_floor(pending, 'protected_span_completed', cut=False)
            await self.tick()
        elif not self.caller_floor and self.accepted_generation == self.generation:
            for e in self.episodes:
                e.failed_at = None
            self.accepted_generation = None
            self.emit('speech_history_cleared', 'response_played_after_caller_turn')

    async def allow_playback(self, generation=None, wait=False, audio_id=None):
        await self.tick()
        if not self.protection_released.is_set():
            self.emit('playback_blocked', 'protected_span_pending', action='HOLD' if wait else 'DROP', requested_audio_id=audio_id)
            if not wait:
                return False
            await self.protection_released.wait()
        if generation is not None and generation != self.generation:
            self.emit('playback_blocked', 'stale_generation', request_generation=generation, requested_audio_id=audio_id)
            return False
        if self.caller_floor:
            self.emit('playback_blocked', 'caller_owns_floor', action='HOLD' if wait else 'DROP', requested_audio_id=audio_id)
            if not wait:
                return False
            await self.released.wait()
            if generation != self.generation:
                self.emit('playback_blocked', 'stale_generation_after_hold', request_generation=generation, requested_audio_id=audio_id)
                return False
        self.awaiting_reply = False
        self.emit('playback_allowed', 'floor_available', requested_audio_id=audio_id)
        return True
