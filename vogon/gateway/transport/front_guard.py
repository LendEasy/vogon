"""Structural guards on a front line — the gateway's own speech.

docs/voice-gateway-front-model.md §8. A front line is text the gateway is
about to play WITHOUT the turn service having composed it, so it is checked
here, structurally, before it reaches TTS. Every rule is about the SHAPE of
the line or a closed content class (digits, a calendar, the words that carry
commitment), never about what the caller said — the gateway has no
meaning to reason from and does not try. The domain supplies its own token
list (``must_not_say``) through the front config; the gateway ships none.

Pure module: stdlib only. Any trip means silence — the safe answer is
always to say nothing.
"""
from __future__ import annotations

import re

# The content class the front is barred from — commitment — as the words
# that carry it in English. A filler that lands one of these on the wrong
# moment ("Sure, one moment" after "can you waive the fee?") is a compliance
# finding on a recorded line. Applied ANYWHERE in the line, to both the
# model's lines and the registry's beats (which are linted at load).
COMMITMENT = (
    "yes", "no", "sure", "okay", "ok", "of course", "absolutely",
    "understood", "got it", "no problem", "agreed", "fine", "done",
    "approved", "confirmed", "granted", "waived",
    "go ahead", "proceed",     # permission — the front said "Go ahead." on a
                               # fragment in the 2026-09-20 slow-speaker run
)

# Claims of ACTION — the other half of the no-authority rule. The front may
# say what it is attending to (checking, looking, pulling up); it may not
# say it DID something the desk does. "Noting that authorization." slipped
# past the prompt's ban in the 2026-09-20 ladder run, so the class is
# enforced here, on both tiers, like commitment.
ACTION_CLAIMS = (
    "noting", "noted", "processing", "processed", "updating", "updated",
    "submitting", "submitted", "sending", "sent", "applying", "applied",
    "scheduling", "scheduled", "recording", "recorded", "registering",
    "registered", "taking care of", "putting that through", "put that through",
)

# Closed lexicons — a number system and a calendar, not a list of things the
# agent might say. "one" is allowed only as "one moment" / "one second".
SPELLED_NUMBERS = (
    "two", "three", "four", "five", "six", "seven", "eight", "nine", "ten",
    "eleven", "twelve", "thirteen", "fourteen", "fifteen", "sixteen",
    "seventeen", "eighteen", "nineteen", "twenty", "thirty", "forty", "fifty",
    "sixty", "seventy", "eighty", "ninety", "hundred", "thousand", "half",
    "quarter", "percent", "dollar", "dollars", "cents",
)
CALENDAR = (
    "january", "february", "march", "april", "may", "june", "july", "august",
    "september", "october", "november", "december", "monday", "tuesday",
    "wednesday", "thursday", "friday", "saturday", "sunday", "today",
    "tomorrow", "tonight", "week", "month", "year", "first", "third",
    "ninth", "fifteenth", "twentieth", "pacific", "eastern", "central",
)

BEAT_MAX_WORDS = 4          # a beat: four words or fewer, may recur


def normalize(text: str) -> str:
    return re.sub(r"[^a-z ]", "", (text or "").lower()).strip()


def sentences(text: str) -> list[str]:
    return [s for s in re.split(r"(?<=[.!?])\s+", (text or "").strip()) if s]


def guard(
    text: str,
    *,
    max_words: int,
    must_not_say: tuple[str, ...] | list[str] = (),
    forbidden_names: tuple[str, ...] | list[str] = (),
    earlier: list[str] | None = None,
) -> list[str]:
    """Return the names of every guard the line trips (empty = clean).

    ``earlier`` is the list of front lines already played on this call, in
    order — the repeat rule and the opener rule read it.
    """
    trips: list[str] = []
    if not text:
        return trips
    low = text.lower()
    words = re.findall(r"[a-z']+", low)
    if "\n" in text:
        trips.append("shape:linebreak")
    if len(sentences(text)) > 1:
        # A second sentence silences the WHOLE line, never truncates it: a
        # model that ignored the register is not trusted on its first
        # sentence either (§8).
        trips.append("shape:sentences")
    if len(text.split()) > max_words:
        trips.append("length")
    if re.search(r"[0-9$%]", text):
        trips.append("content:number")
    if any(w in SPELLED_NUMBERS for w in words) or re.search(r"\bone\b(?! (moment|second|sec))", low):
        trips.append("content:spelled_number")
    if any(w in CALENDAR for w in words):
        trips.append("content:date_word")
    for tok in must_not_say:
        if tok and re.search(rf"\b{re.escape(tok.lower())}s?\b", low):
            trips.append(f"content:{tok}")
    for name in forbidden_names:
        if name and re.search(rf"\b{re.escape(name.lower())}\b", low):
            trips.append("content:name")
    if any(re.search(rf"\b{re.escape(c)}\b", low) for c in COMMITMENT):
        trips.append("commitment")
    if any(re.search(rf"\b{re.escape(c)}\b", low) for c in ACTION_CLAIMS):
        trips.append("action")
    if "?" in text:
        trips.append("question")
    if earlier:
        n = normalize(text)
        if len(text.split()) > BEAT_MAX_WORDS:
            if n in {normalize(e) for e in earlier}:
                trips.append("repeat")
            # The opener tic: not the same first THREE words as the previous
            # front line on the call. Three, not two — measured 2026-09-19,
            # two would have silenced 5 good hold lines of 35.
            prev = normalize(earlier[-1]).split()[:3]
            if prev and n.split()[:3] == prev:
                trips.append("opener")
    return trips


def lint_pool(lines: list[str], *, must_not_say=()) -> list[tuple[str, list[str]]]:
    """Check a registry pool the way a generated line is checked, minus the
    repeat rules (a pool rotates). Returns [(line, trips)] for the lines that
    fail; an empty list means the pool is safe at any moment of the call."""
    bad = []
    for line in lines:
        t = [x for x in guard(line, max_words=BEAT_MAX_WORDS, must_not_say=must_not_say)]
        if t:
            bad.append((line, t))
    return bad
