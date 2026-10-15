"""The front model — the filler hold's source of words.

docs/voice-gateway-front-model.md. The hold that opens on every final used
to draw from a random iterator over canned phrases; with this installed it
draws ONE sentence composed for the moment (tier 1, the model) or, when the
model had nothing by ``beat_after_ms``, a beat from an approved pool
(tier 0, rules on the gateway's own clocks). Both tiers play through the
same hold and the same send path; both go into the one per-call ledger of
lines played, which the model sees as ALREADY SAID.

Gateway-owned, like the barge judge (D1): talks to the provider directly,
reads only ``app.config`` for credentials and knobs, and knows nothing about
the account — its only context is what physically crossed the line: the
borrower's finals and our lines as delivered.

Failure of any kind is silence. The source never raises into the hold.
"""
from __future__ import annotations

import asyncio
import json
import logging
import random
import time

from cerebras.cloud.sdk import AsyncCerebras

from vogon.gateway.settings import settings
from vogon.gateway.transport import front_moves
from vogon.gateway.transport.front_guard import BEAT_MAX_WORDS, guard, lint_pool
from vogon.gateway.transport.front_pick import REPEAT, SILENCE, Pick, pick

logger = logging.getLogger(__name__)

_client = None


def _cerebras():
    global _client
    if _client is None:
        # The request may run to the hold's own deadline (beat_after): a line
        # that lands at 700 ms still beats a beat at 900. front_budget_ms is
        # the target, metered as `front_late` when missed, not the cut-off
        # (the 2026-09-20 smoke lost a sympathy line to a 500 ms cut).
        _client = AsyncCerebras(
            api_key=settings.cerebras_api_key,
            timeout=max(0.2, settings.front_beat_after_ms / 1000.0),
            max_retries=0,
        )
    return _client


async def warm() -> None:
    """Open the provider connection before the first turn needs it: the
    first request on a cold client paid 664 ms (TLS + pool) in the
    2026-09-20 smoke against ~250 ms warm. Fire-and-forget at install."""
    try:
        await _cerebras().models.list()
    except Exception as exc:
        logger.debug("FRONT | warm-up failed: %s", exc)


class FrontConfig:
    """The domain's front artifact (prompt, beat pools, must_not_say), loaded
    once per process from ``settings.front_config_path``. The gateway ships
    no words of its own: a missing or invalid file disables the model tier
    and leaves the hold with whatever pools did load."""

    def __init__(self, prompt: str, beats: dict[str, list[str]], must_not_say: list[str],
                 tiers: dict | None = None,
                 move_criteria: dict | None = None, move_riders: dict | None = None,
                 move_question: str = ""):
        self.prompt = prompt
        self.move_question = move_question
        self.beats = {k: list(v) for k, v in beats.items()}
        self.must_not_say = list(must_not_say)
        # docs/FRONT_COMPLEMENT_DESIGN.md — derived by surveying the 204
        # recited lines, not invented.
        self.tiers = {k: list(v) for k, v in (tiers or {}).items()}
        self.move_criteria = dict(move_criteria or {})
        self.move_riders = dict(move_riders or {})

    @property
    def can_read_moves(self) -> bool:
        """The Jev engine needs criteria AND somewhere to draw words from."""
        return bool(self.move_criteria) and bool(self.tiers)

    @classmethod
    def load(cls, path: str) -> FrontConfig | None:
        try:
            import yaml  # noqa: PLC0415 — PyYAML is not a gateway requirement
            with open(path, encoding="utf-8") as f:
                doc = yaml.safe_load(f) or {}
        except Exception as exc:
            logger.error("FRONT | config not loaded (%s): %s — model tier OFF", path, exc)
            return None
        prompt = str(doc.get("prompt") or "").strip()
        must = [str(x) for x in (doc.get("must_not_say") or [])]
        beats: dict[str, list[str]] = {}
        for kind, pool in (doc.get("beats") or {}).items():
            pool = [str(x).strip() for x in (pool or []) if str(x).strip()]
            bad = lint_pool(pool, must_not_say=must)
            if bad:
                # A pool that fails the guards does not load: a beat must be
                # safe at ANY moment of the call, that is the whole reason
                # it may be chosen without reading the borrower.
                logger.error("FRONT | beat pool %r refused: %s", kind, bad)
                continue
            beats[str(kind)] = pool
        # Every tier, the repeat acknowledgement included, is linted exactly
        # like the beat pools: each line may be chosen without the gateway
        # re-reading what the borrower said, so each has to be safe on its own.
        tiers: dict[str, list[str]] = {}
        for kind, pool in (doc.get("tiers") or {}).items():
            pool = [str(x).strip() for x in (pool or []) if str(x).strip()]
            cap = BEAT_MAX_WORDS if kind == "beat" else settings.front_max_words
            bad = [(ln, t) for ln in pool
                   if (t := guard(ln, max_words=cap, must_not_say=must))]
            if bad:
                logger.error("FRONT | tier pool %r refused: %s", kind, bad)
                continue
            tiers[str(kind)] = pool
        if not prompt:
            logger.error("FRONT | %s has no prompt — LLM tier OFF", path)
        return cls(prompt, beats, must, tiers,
                   doc.get("move_criteria") or {}, doc.get("move_riders") or {},
                   str(doc.get("move_question") or "").strip())


class FrontModelSource:
    """One per call. Installed on the transport's clocks by the driver."""

    def __init__(self, cfg: FrontConfig, *, agent_name: str, lender_name: str,
                 borrower_first_name: str = "") -> None:
        self._cfg = cfg
        system = cfg.prompt.replace("{agent_name}", agent_name or "the agent")
        system = (system.replace("{lender_name}", lender_name) if lender_name
                  else system.replace(" for {lender_name}", "").replace("{lender_name}", "the lender"))
        self._system = system
        # v1: the front never learns verification, so the borrower's name is
        # never sayable — it is a forbidden token, not a conditional one.
        self._forbidden_names = tuple(n for n in (borrower_first_name,) if n)
        self._history: list[tuple[str, str]] = []      # ("Borrower"|"Agent"|"Agent (your waiting line)", text)
        self.lines_played: list[str] = []
        self._task: asyncio.Task | None = None
        self._primed_text = ""
        self._result: str = ""
        self._result_for = ""
        self._model_empty = False       # the model answered "" on purpose (vs late/failed)
        self._speech_gen = 0            # bumped whenever the borrower is heard again
        self._pool_state: dict[str, list[str]] = {}
        self._last_beat: dict[str, str] = {}
        self._last_line_t = 0.0
        self._n = 0
        self._distress_latch = False    # distress is STICKY for the rest of the turn
        self._deliberate_silence = False  # the read chose to be quiet, for a reason
        self.metrics: dict[str, int] = {}

    def _can_compose(self) -> bool:
        """The LLM engine needs a prompt; the Jev engine needs criteria and
        pools. ``off`` composes nothing and leaves the hold on beats."""
        eng = settings.front_engine
        if eng == "off":
            return False
        if eng == "jev":
            return self._cfg.can_read_moves
        return bool(self._cfg.prompt)

    # ── what the driver tells the source ─────────────────────────────────
    def note_borrower(self, text: str) -> None:
        if text and text.strip():
            self._history.append(("Borrower", text.strip()))

    def note_agent(self, text: str, *, front: bool = False) -> None:
        if text and text.strip():
            self._history.append(("Agent (your waiting line)" if front else "Agent", text.strip()))
        if not front:
            # The desk has answered: a new borrower turn starts clean.
            self._distress_latch = False

    def prime(self, text: str) -> None:
        """Start composing for this utterance (the eager, or the final when
        no eager preceded it). Idempotent per text; a new text supersedes."""
        if text and text != self._primed_text:
            # The borrower is speaking again: any beat still waiting for its
            # deadline must not fire into their speech (2026-09-20 smoke —
            # Flux finalised "Yes." and the beat landed under "go ahead and
            # take the seventy five"). The hold's barge rule would cut it,
            # but the borrower still hears us start.
            self._speech_gen += 1
        if not self._can_compose() or not text or text == self._primed_text:
            return
        self.cancel()
        self._primed_text = text
        self._deliberate_silence = False
        self._task = asyncio.create_task(self._generate(text))

    def cancel(self) -> None:
        """The borrower resumed, or the turn was superseded: nothing composed
        so far may play."""
        if self._task is not None and not self._task.done():
            self._task.cancel()
        self._task = None
        self._result, self._result_for = "", ""

    # ── what the hold asks the source ─────────────────────────────────────
    async def hold_line(self, deadline_s: float, deadline_empty_s: float | None = None) -> tuple[str, str]:
        """The line for the turn just started, or "" — waits for the model up
        to ``deadline_s`` (from now), then falls back to the ``hold`` beat.

        Two kinds of nothing: a model that was LATE or failed gets the beat
        at ``deadline_s``; a model that deliberately answered "" (a plain
        answer, identity data) gets it only at ``deadline_empty_s`` — the
        desk is usually fast on those turns, and a beat at 900 ms landed in
        the borrower's own mid-sentence pause twice in the 2026-09-20 smoke
        (Flux finalises "Yes." before "this is Alex" arrives).
        Returns (text, audio_id)."""
        t0 = time.monotonic()
        text = ""
        empty_on_purpose = False
        task = self._task
        if task is not None:
            try:
                text = await asyncio.wait_for(asyncio.shield(task), timeout=max(0.0, deadline_s))
                empty_on_purpose = not text
            except (asyncio.TimeoutError, asyncio.CancelledError):
                self._bump("front_late")
            except Exception:
                self._bump("front_error")
        gen = self._speech_gen
        if not text and empty_on_purpose and self._deliberate_silence:
            # A reasoned silence is absolute: no beat over the top of it.
            self._bump("front_silent_on_purpose")
            return "", ""
        if not text and empty_on_purpose and deadline_empty_s is not None:
            # Wait out the longer deadline in small steps, and give up the
            # moment the borrower is heard again.
            while (time.monotonic() - t0) < deadline_empty_s:
                await asyncio.sleep(0.05)
                if self._speech_gen != gen:
                    self._bump("front_beat_withheld")
                    return "", ""
        if not text:
            if self._speech_gen != gen:
                self._bump("front_beat_withheld")
                return "", ""
            text = self.beat("hold")
        return self._admit(text)

    def wait_line(self) -> tuple[str, str]:
        return self._admit(self.beat("wait"))

    def beat(self, kind: str) -> str:
        """Tier 0: rotate through the pool without repeats until exhausted,
        then reshuffle with the first of the new cycle != the last beat."""
        pool = self._cfg.beats.get(kind) or []
        if not pool:
            return ""
        q = self._pool_state.setdefault(kind, [])
        if not q:
            q.extend(random.sample(pool, len(pool)))
            if len(q) > 1 and q[0] == self._last_beat.get(kind):
                q.append(q.pop(0))
        line = q.pop(0)
        self._last_beat[kind] = line
        self._bump(f"beat_{kind}")
        return line

    def _rotate(self, key: str, pool: list[str]) -> str:
        """Tier-agnostic rotation: walk the pool without repeats, reshuffle at
        exhaustion, never open a new cycle with the line that just played."""
        if not pool:
            return ""
        q = self._pool_state.setdefault(key, [])
        if not q:
            q.extend(random.sample(pool, len(pool)))
            if len(q) > 1 and q[0] == self._last_beat.get(key):
                q.append(q.pop(0))
        line = q.pop(0)
        self._last_beat[key] = line
        return line

    def _admit(self, text: str) -> tuple[str, str]:
        """Apply the guards and the spacing rule; mint the audio id."""
        if not text:
            return "", ""
        now = time.monotonic()
        if self._last_line_t and now - self._last_line_t < settings.front_min_gap_sec:
            self._bump("front_guard_gap")
            return "", ""
        trips = guard(text, max_words=settings.front_max_words,
                      must_not_say=self._cfg.must_not_say,
                      forbidden_names=self._forbidden_names,
                      earlier=self.lines_played)
        if trips:
            for t in trips:
                self._bump(f"front_guard_{t.split(':')[0]}")
            logger.info("FRONT | guarded %r: %s", text, trips)
            return "", ""
        self._n += 1
        self._last_line_t = now
        return text, f"front-{self._n}"

    def note_played(self, text: str) -> None:
        self.lines_played.append(text)
        self.note_agent(text, front=True)
        self._bump("front_played")

    # ── the model ─────────────────────────────────────────────────────────
    def _user_message(self, utterance: str) -> str:
        lines = [f"{who}: {what}" for who, what in self._history[-8:]]
        already = ("ALREADY SAID THIS CALL (off limits, say it differently or say nothing):\n"
                   + "\n".join(f"- {a}" for a in self.lines_played) + "\n\n") if self.lines_played else ""
        return ("VERIFIED: no\nCALLER FIRST NAME: (withheld)\n\n"
                "TRANSCRIPT (most recent last):\n" + "\n".join(lines) + "\n\n"
                + already + f"CALLER JUST SAID: \"{utterance}\"")

    async def _generate(self, utterance: str) -> str:
        """Compose for this utterance. ``front_engine`` selects the reader;
        every path returns plain text, and every failure returns ""."""
        if settings.front_engine == "jev":
            return await self._read_and_pick(utterance)
        return await self._generate_llm(utterance)

    async def _read_and_pick(self, utterance: str) -> str:
        """The surveyed path (docs/FRONT_COMPLEMENT_DESIGN.md).

        ONE decisions call asks what move the DESK will open with, plus the
        riders; ``front_pick`` turns that into a tier, and the words come from
        a pool linted at load. The model never writes what the borrower hears,
        so a misread costs a WRONG line, never an unsafe one."""
        t0 = time.perf_counter()
        agent_last = next((t for who, t in reversed(self._history) if who == "Agent"), "")
        read = await front_moves.read_async(
            self._cfg.move_criteria, self._cfg.move_riders,
            agent_last=agent_last, utterance=utterance, history=self._history,
            question=self._cfg.move_question)
        if read.error:
            self._bump("front_error")
            return ""
        # Distress is STICKY until the desk answers. Flux cut "I can't do the
        # full amount, my hours got cut last month and money is really tight."
        # into two turns on the 2026-09-24 ladder; the front read the refusal
        # half first (distress 0.03) and spoke, and only the SECOND half
        # carried the hardship (0.92). Nothing in a prompt can see a sentence
        # that has not been said yet — but once any fragment of this turn
        # reads distress, every later fragment must stay quiet too, or the
        # gateway talks over the sympathy the desk is about to give.
        if read.rider("distress"):
            self._distress_latch = True
        if self._distress_latch:
            read = front_moves.FrontRead(
                move=read.move, confidence=read.confidence,
                riders={**read.riders, "distress": 1.0},
                latency_ms=read.latency_ms, cost_usd=read.cost_usd)
        # has_spoken: a caller cannot have missed what we never said.
        # move_floor used to become 1.1 when FRONT_REPEAT_ENABLED=0, which
        # silenced the WHOLE front model, not just repeats.
        got = pick(read, self._cfg.tiers, self._rotate,
                   has_spoken=any(who == "Agent" for who, _ in self._history),
                   move_floor=settings.front_move_floor)
        if got.tier == REPEAT and not settings.front_repeat_enabled:
            # Deliberately quiet: the desk re-delivers, and no beat may land
            # on top of a request the caller just made.
            got = Pick(SILENCE, "", "rider:repeat_disabled")
        ms = (time.perf_counter() - t0) * 1000
        if got is None or not got.text:
            # TWO KINDS OF QUIET, and only one of them may be overridden by a
            # beat. A rider fired means we KNOW to be quiet — the caller gave
            # a plain answer, read out identity data, said goodbye, or is in
            # distress the desk is about to acknowledge. An unsure or failed
            # read means we do not know, and there a beat still beats dead air.
            # Measured on a real call: "Yeah. This is John speaking." read
            # PROCEDURE 0.91 and chose silence correctly, and the hold played
            # "Let me check that for you now." over the top at the empty
            # deadline, which then collided with the desk's own line and drew
            # the silence ladder on top of that.
            # `got is not None`, never `if got`: a silent Pick has no text, and
            # an earlier __bool__ override made it falsy, so every reasoned
            # silence lost its reason here, _deliberate_silence was never set,
            # and the hold's beat played over it anyway. Found on a live
            # phone-path call: "Yes, that's fine." -> silent -> "Taking a look."
            reason = got.reason if got is not None else "repeat_disabled"
            self._deliberate_silence = reason.startswith("rider:")
            logger.info("FRONT | %.0f ms | move=%s conf=%.2f -> silent (%s)%s",
                        ms, read.move, read.confidence, reason,
                        " [no beat]" if self._deliberate_silence else "")
            self._bump("front_empty")
            return ""
        logger.info("FRONT | %.0f ms | move=%s conf=%.2f -> %s %r",
                    ms, read.move, read.confidence, got.tier, got.text[:80])
        self._bump(f"front_{got.tier}")
        self._bump("front_generated")
        return got.text

    async def _generate_llm(self, utterance: str) -> str:
        t0 = time.perf_counter()
        try:
            extra = ({"reasoning_effort": settings.front_model_effort}
                     if settings.front_model_effort not in ("", "default") else {})
            resp = await _cerebras().chat.completions.create(
                model=settings.front_model,
                messages=[{"role": "system", "content": self._system},
                          {"role": "user", "content": self._user_message(utterance)}],
                max_tokens=1024, temperature=0.3,
                response_format={"type": "json_object"},
                extra_body=extra,
            )
            raw = resp.choices[0].message.content or ""
            obj = json.loads(raw) if raw else {}
            text = (obj.get("say") or "").strip() if isinstance(obj, dict) else ""
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.warning("FRONT | model failed: %s", exc)
            self._bump("front_error")
            return ""
        ms = (time.perf_counter() - t0) * 1000
        # One sentence only: anything past the first is a register violation
        # and the guard silences the whole line; log what came back.
        logger.info("FRONT | %.0f ms | %r -> %r", ms, utterance[:60], text)
        self._bump("front_generated" if text else "front_empty")
        return text

    def _bump(self, key: str) -> None:
        self.metrics[key] = self.metrics.get(key, 0) + 1


def build_front_source(*, agent_name: str, lender_name: str,
                       borrower_first_name: str = "") -> FrontModelSource | None:
    """None when the front model is disabled or its config did not load —
    the hold then behaves exactly as before this module existed."""
    if not settings.front_model_enabled:
        return None
    if not settings.cerebras_api_key:
        logger.error("FRONT | enabled but CEREBRAS_API_KEY is empty — OFF")
        return None
    path = settings.resolved_front_config_path()
    if not path:
        logger.info("FRONT | FRONT_CONFIG_PATH unset — model tier OFF; the hold uses the call's filler pools")
        return None
    cfg = FrontConfig.load(path)
    if cfg is None or not cfg.prompt:
        return None
    try:
        asyncio.get_running_loop().create_task(warm())
    except RuntimeError:
        pass
    return FrontModelSource(cfg, agent_name=agent_name, lender_name=lender_name,
                            borrower_first_name=borrower_first_name)
