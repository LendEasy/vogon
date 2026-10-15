"""Who answered the line — decided in the gateway on the far end's FIRST
utterance (docs/VOICEMAIL_DETECTION_DESIGN.md).

The detector sits behind the AEC and the floor controller: it reads the
episodes the floor controller builds from post-AEC transcripts, asks one
instrument (the Jev recording question, here; Twilio AMD or a tone detector
later, in the same slot) and issues exactly one ``LineVerdict`` per call:

    machine   the opening is CUT (the same break-through path as a safety
              phrase), the episode's final is never absorbed, and the
              verdict rides the turn service request AHEAD of the transcript
              so the gate reads it as the ``answered_by`` annotation
    human     the fact alone; the opening's absorb policy runs unchanged
    unknown   the fact alone, issued when the episode's final left the
              question in the band, when the instrument failed, or when the
              ask window after the opening expired with nothing to ask about

Rules that hold here: the far end's first STT TURN binds — from the first
revision that reaches ``min_words`` to that turn's end-of-turn — whatever
acoustic episodes the floor controller cuts it into (measured 2026-09-23:
one greeting spanned four energy episodes; binding to the first of them
stopped the asks at 3.5 s and never saw the final); a revision the floor
controller marked as our own echo is never asked about; one ask
is in flight at a time and changed text replaces pending work (the floor
controller's discipline); the ask is never awaited inside the frame loop;
the instrument's failure is a verdict of ``unknown`` with source ``none``,
never a guess. After the verdict the detector is closed for the call
(§13 — ``line_checked``).

The hold (§6): ``wait_quiet`` returns once the judged episode has reached
its end-of-turn and ``quiet_ms`` have passed with no further speech, or
``cap_s`` after the verdict, or when ``release_hold`` is called (a tone or
AMD event, later). The driver awaits it before a ``play_after="line_quiet"``
playout.
"""
from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable
from typing import Any, Optional

from vogon_contract import LineDetection, LineVerdict, UtteranceFinal

logger = logging.getLogger(__name__)

# The recording question — the wording the 2026-09-23 probes settled on
# (14/14 greetings, 8/8 live neighbours at 0.40; 26/30 on the counter-example
# banks). Domain-free: "the person we are calling for" is the CallConfig name
# hint, not a domain noun.
def _noul(instructions: str, yes: str, no: str) -> dict:
    """The decisions endpoint's yes/no question shape (vogon.gateway.jev.noul),
    spelled out here so the gateway tree imports nothing from the reasoning
    side — the instrument that sends it is injected by the driver."""
    return {"type": "noul", "instructions": instructions,
            "criteria": {"true": yes, "false": no}}


RECORDING_QUESTION = _noul(
    "This is the FIRST thing heard after the phone was answered. Is it a RECORDING — a "
    "voicemail greeting, an answering machine, or a carrier announcement — rather than a "
    "live person talking to us? A recording ANNOUNCES an absence and speaks to nobody in "
    "particular: it says the person cannot come to the phone, is unavailable, is away or not "
    "around, invites a message, names a number, or says a mailbox is full or a number is out "
    "of service. It may say the account holder's own name in the first person ('Hey it's Alex, "
    "I'm not around') — that is still a recording, because a live person answering a call does "
    "not announce that they are not there. A LIVE person reacts to the call: answers, asks who "
    "is calling, says they are busy or driving, says the person is not here, offers to take a "
    "message, or gives a wrong-number reply.",
    yes="a recording: a greeting or announcement made in advance, addressed to whoever calls — "
        "'you've reached', 'not available', 'leave a message', 'after the tone', 'I'm not around, "
        "you know what to do', 'this is the Morgan residence, we can't come to the phone', 'the "
        "mailbox is full', 'not in service'",
    no="a live person responding to this call right now — 'Hello?', 'Speaking', 'Yes, this is "
       "Alex Morgan', 'Alex isn't here, can I take a message?', 'call back later', 'I'm driving, "
       "can I call you back?', 'wrong number'")

# The state shape the probes validated (JEV_CLASSIFICATION_DESIGN.md §33.1).
# Measured 2026-09-23 (prefix_probe, N=3): adding our opening text and the
# onset relation to the state depressed P(recording) by ~0.3 on every
# recording prefix ("Hi. You've reached Alex Morgan." 0.72 → 0.33) while
# moving no live line — so echo is handled by strip_echo on the text, and
# the state stays exactly this.
_DEFINITIONS = {
    "account_holder": "the person we are calling for",
    "first_turn": "the first words heard after the line connected",
}


async def no_instrument(state: dict) -> float:
    """The default when the driver injected nothing: every ask fails, every
    verdict is `unknown` with source `none` — never a guess."""
    raise RuntimeError("line detector: no instrument installed")


# A bound episode with no final yet keeps the window open this long past
# ask_window_s (a carrier announcement with menu options can run 20 s).
HARD_CAP_S = 20.0


def strip_echo(text: str, our_line: str, min_run: int = 3) -> str:
    """Drop from the far end's transcript every run of ``min_run`` or more
    consecutive words that also occur consecutively in OUR line — the
    text-level tail of echo cancellation (the same idea as
    barge_semantics.is_echo_of, applied to a mixed transcript instead of a
    whole one). Measured 2026-09-23 on the duplex harness at 0.3 echo gain:
    the far end's episode carried "Maya calling ... on a recorded line"
    from our opening inside its own words. Punctuation is kept on the words
    that survive."""
    if not text or not our_line:
        return text
    def norm(w):
        return "".join(ch for ch in w.lower() if ch.isalnum() or ch == "'")
    ours = [norm(w) for w in our_line.split()]
    ours = [w for w in ours if w]
    words = text.split()
    keys = [norm(w) for w in words]
    n = len(words)
    drop = [False] * n
    i = 0
    while i < n:
        best = 0
        if keys[i]:
            for j in range(len(ours)):
                if ours[j] != keys[i]:
                    continue
                k = 0
                while (i + k < n and j + k < len(ours) and keys[i + k]
                       and keys[i + k] == ours[j + k]):
                    k += 1
                best = max(best, k)
        if best >= min_run:
            for k in range(best):
                drop[i + k] = True
            i += best
        else:
            i += 1
    kept = [w for w, d in zip(words, drop, strict=False) if not d]
    return " ".join(kept)


class LineDetector:
    def __init__(self, transport, cfg: LineDetection, *,
                 now: Callable[[], float] = time.monotonic,
                 ask: Callable[[dict], Awaitable[float]] = no_instrument,
                 sleep: Callable[[float], Awaitable[None]] = asyncio.sleep):
        self.t = transport
        self.cfg = cfg
        self.now = now
        self.ask = ask
        self.sleep = sleep          # a fake clock's sleep in tests
        self.verdict: Optional[LineVerdict] = None
        self.closed = False
        # the judged STT turn (bound from its first qualifying revision to
        # its end-of-turn; `episode` is the acoustic episode it started in,
        # which is what the floor controller's break-through path needs)
        self.bound = False
        self.episode: Any = None
        self.onset_before_audio: Optional[bool] = None
        self.final_seen = False
        self.final_t: Optional[float] = None
        self.last_text_t: Optional[float] = None     # last transcript revision of the episode
        # the instrument
        self.asked_text: Optional[str] = None
        self.pending: Optional[tuple[str, bool]] = None
        self.last_p: Optional[float] = None
        self.last_latency_ms = 0.0
        self.asks = 0
        self.failures = 0
        self.worker: Optional[asyncio.Task] = None
        # the window
        self.first_audio_t: Optional[float] = None
        self.opening_ended_t: Optional[float] = None
        self.retire_at: Optional[float] = None
        self.timer: Optional[asyncio.Task] = None
        # the hold
        self._release = asyncio.Event()

    # ── lifecycle ────────────────────────────────────────────────────────────

    def start(self) -> None:
        if self.timer is None:
            self.timer = asyncio.create_task(self._heartbeat())
        self._emit("line_detector", "started", min_words=self.cfg.min_words,
                   machine_p=self.cfg.machine_p, human_p=self.cfg.human_p,
                   ask_window_s=self.cfg.ask_window_s)

    async def close(self) -> None:
        for task in (self.timer, self.worker):
            if task is not None and not task.done():
                task.cancel()
        await asyncio.gather(*(t for t in (self.timer, self.worker) if t is not None),
                             return_exceptions=True)
        self.timer = None

    async def _heartbeat(self) -> None:
        while not self.closed:
            await self.sleep(0.05)
            await self.tick()

    def _emit(self, event: str, reason: str, **detail) -> None:
        log = getattr(self.t, "_audio_log", None)
        if log is not None:
            log.emit(self.now(), event, reason=reason, **detail)

    # ── signals from the transport ───────────────────────────────────────────

    def note_playout_start(self) -> None:
        if self.first_audio_t is None:
            self.first_audio_t = self.now()

    def note_playout_end(self) -> None:
        """The opening finished (played out or cut): the ask window starts."""
        if self.opening_ended_t is None and self.first_audio_t is not None:
            self.opening_ended_t = self.now()
            self.retire_at = self.opening_ended_t + float(self.cfg.ask_window_s)

    async def observe(self, episode, final: bool) -> None:
        """One transcript revision of a floor-controller episode, post-AEC."""
        if episode is None or not self.cfg.enabled:
            return
        if self.bound and self.final_seen:
            return                                   # the judged turn is over
        if self.bound:
            # The judged turn's revisions and end-of-turn are what the hold
            # waits for, noted whether or not the verdict is already in.
            self.last_text_t = self.now()
            if final:
                self.final_seen = True
                self.final_t = self.now()
        if self.closed:
            return
        if getattr(episode, "echo", False) or getattr(episode, "possible_echo", False):
            return                                   # our own voice, per the floor controller
        text = strip_echo((episode.text or "").strip(),
                          str(getattr(self.t, "_last_speak_text", "") or ""))
        if len(text.split()) < int(self.cfg.min_words):
            if final and not self.bound:
                return                               # "Hello?" alone: never binds, never asks
            return
        if not self.bound:
            self.bound = True
            self.episode = episode
            self.onset_before_audio = (self.first_audio_t is None
                                       or episode.onset < self.first_audio_t)
            self._emit("line_detector", "turn_bound", episode_id=episode.id,
                       onset_before_audio=self.onset_before_audio, text=text[:60])
            self.last_text_t = self.now()
            if final:                                # bound BY its final (one-shot greeting)
                self.final_seen = True
                self.final_t = self.now()
        self.pending = (text, final)
        if self.worker is None or self.worker.done():
            self.worker = asyncio.create_task(self._ask_loop())

    async def tick(self) -> None:
        """Retire: the ask window after the opening expired with no verdict.
        A bound episode that is still talking is not retired — its final
        decides — up to a hard cap (the ask window plus HARD_CAP_S)."""
        if self.closed or self.retire_at is None or self.now() < self.retire_at:
            return
        if self.worker is not None and not self.worker.done():
            return                                   # an answer is on its way
        if (self.bound and not self.final_seen
                and self.now() < self.retire_at + HARD_CAP_S):
            return                                   # the greeting is still playing
        await self._issue("unknown", self.last_p if self.last_p is not None else 0.0,
                          self.asked_text or "", self.last_latency_ms, reason="ask_window_expired")

    # ── the instrument ───────────────────────────────────────────────────────

    def _state(self, text: str) -> dict:
        cfg = getattr(self.t, "_cfg", None)
        holder = (getattr(cfg, "name_hint", None) or "the account holder")
        return {"definitions": _DEFINITIONS, "account_holder": holder,
                "first_turn": text}

    async def _ask_loop(self) -> None:
        while not self.closed and self.pending is not None:
            text, final = self.pending
            self.pending = None
            if text == self.asked_text and self.last_p is not None:
                p = self.last_p                      # same words: no new ask
            else:
                self.asked_text = text
                self.asks += 1
                t0 = self.now()
                try:
                    p = await self.ask(self._state(text))
                except Exception as exc:             # noqa: BLE001 — the instrument, not the call
                    p = None
                    self.failures += 1
                    logger.warning("line detector: instrument failed (%s)", exc)
                self.last_latency_ms = (self.now() - t0) * 1000.0
                self.last_p = p
                self._emit("line_detector", "asked", p=p, final=final,
                           latency_ms=round(self.last_latency_ms), text=text[:60])
                logger.info("line detector | ask=%d p=%s final=%s latency=%.0fms text=%r",
                            self.asks, "n/a" if p is None else f"{p:.2f}", final,
                            self.last_latency_ms, text[:80])
            if self.closed:
                return
            if p is not None and p >= float(self.cfg.machine_p):
                await self._issue("machine", p, text, self.last_latency_ms, reason="machine_band")
                return
            if final or self.final_seen:
                # No more revisions of this episode are coming.
                if p is not None and p <= float(self.cfg.human_p):
                    await self._issue("human", p, text, self.last_latency_ms, reason="human_band")
                elif p is None:
                    await self._issue("unknown", 0.0, text, self.last_latency_ms,
                                      reason="instrument_failed")
                else:
                    await self._issue("unknown", p, text, self.last_latency_ms,
                                      reason="overlap_band")
                return

    # ── the verdict ──────────────────────────────────────────────────────────

    async def _issue(self, kind: str, p: float, text: str, latency_ms: float,
                     *, reason: str) -> None:
        if self.closed:
            return
        self.closed = True
        source = "none" if (self.asks == 0 or (kind == "unknown" and reason == "instrument_failed")) else "jev"
        self.verdict = LineVerdict(answered_by=kind, probability=float(p), source=source,
                                   transcript=text, latency_ms=float(latency_ms),
                                   at=self.now())
        self._emit("line_verdict", reason, answered_by=kind, p=round(float(p), 3),
                   source=source, asks=self.asks, failures=self.failures, text=text[:60])
        logger.info("LINE VERDICT | answered_by=%s p=%.2f source=%s reason=%s asks=%d "
                    "latency=%.0fms text=%r", kind, p, source, reason, self.asks,
                    latency_ms, text[:80])
        if kind != "machine":
            await self.t._utterance_queue.put(self.verdict)
            return
        # A machine: cut the opening through the break-through path (the
        # one verdict that may cut a protected playout), which also takes
        # the floor so the episode's final is never absorbed.
        floor = getattr(self.t, "_floor", None)
        if floor is not None and self.episode is not None:
            await floor.decide(self.episode, "YIELD", "line_machine", breakthrough=True)
        await self.t._utterance_queue.put(self.verdict)
        if self.final_seen:
            # The final already went by (absorbed or swallowed under the
            # opening): re-issue the transcript as the turn's trigger so the
            # verdict has something to ride with.
            await self.t._utterance_queue.put(UtteranceFinal(
                text=text, confidence=1.0, low_conf_words=[],
                onset_at=float(getattr(self.episode, "onset", 0.0) or 0.0),
                at=self.now(), playback_generation=self.t.playback_generation()))

    # ── the hold ─────────────────────────────────────────────────────────────

    def release_hold(self) -> None:
        """A tone / AMD message-end event (later): play now."""
        self._release.set()

    async def wait_quiet(self) -> str:
        """Block until the line is quiet per §6. Returns why it released."""
        if self.verdict is None:
            return "no_verdict"
        deadline = self.verdict.at + float(self.cfg.cap_s)
        quiet_s = float(self.cfg.quiet_ms) / 1000.0
        while True:
            now = self.now()
            if self._release.is_set():
                why = "released"
                break
            if now >= deadline:
                why = "cap"
                break
            # Quiet = no transcript revision of the judged episode since its
            # end-of-turn. Line ENERGY is not used: measured 2026-09-23, our
            # own echo kept the energy floor "speaking" and the hold ran to
            # the cap.
            since = now - max(self.last_text_t or 0.0, self.final_t or 0.0)
            if self.final_seen and since >= quiet_s:
                why = "quiet"
                break
            await self.sleep(0.05)
        self._emit("line_hold_released", why, waited_s=round(self.now() - self.verdict.at, 2))
        return why
