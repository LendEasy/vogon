<!-- Copied 2026-10-11 from the application's monorepo (data-plane/docs/call-event-ordering.md) because the gateway's code cites it.
     It is a design record: paths and modules it names are the application's unless they say vogon/. -->

# Total Ordering of Events in a Call

**Status:** design 2026-07-17; Phase 1 implemented in the same commit.
Companion: `full-duplex-turn-model.md` (§F exchange model — the first consumer
of a correct order), `../../voice-gateway/docs/speech-delivery-completion.md`.

---

## 1. The problem — two problems, actually

A live call is a small concurrent system. Several async producers all mutate
shared state and append to one `CallLog`:

- the STT reader (Deepgram callbacks: interim, eager-EOT, final, turn-resumed),
- the barge gate / semantic-absorb layer (candidate, cut, absorb),
- the playout / mark-echo handler (tts_end + per-sentence marks),
- the silence-timer loop,
- tool callbacks (async side effects),
- the ambience loop,
- the main turn processor.

Two distinct questions arise, and the literature answers them with *different*
tools:

1. **Ordering (logical).** "In what order did events happen, and which are
   concurrent?" Solved by Lamport counters, vector clocks, append-only logs.
2. **Duration (physical).** "How many milliseconds apart? Did the caller's
   onset precede the audio finishing playing?" Solved by physical-clock
   projection onto a reference — NOT by logical clocks, which discard
   magnitude by design.

This doc is about **ordering**. Duration/alignment (the media-clock projection)
is specified in `full-duplex-turn-model.md` §A/§F; the two compose (§6).

### The concrete failure

`CallLog` is append-only, but its order is **append order** — the order in
which our process happened to *get around to logging* each event, which can
differ from the order the events physically **occurred**, because producers are
async and a callback may be scheduled late. Real races we have seen:

- an **absorbed final** transcript arriving/logged *after* a new emission,
- a **mark echo** (playback complete) landing *after* the next turn already
  opened,
- **eager-vs-final** speculative runs interleaving.

If a consumer (the exchange resolver, the timeline UI, an eval trace) trusts
append order, it reasons about a sequence that never physically happened.

---

## 2. Clock skew — why we do NOT have the classic distributed problem

The instinctive worry: if we timestamp events using Twilio's server clock,
Deepgram's server clock, and our own, three unsynchronized wall-clocks make any
cross-source comparison meaningless, and no counter fixes it.

**We never take a foreign wall-clock.** Verified across the transport: every
timing signal is `time.monotonic()` — our clock. The foreign inputs are not
absolute timestamps:

| Signal | What it actually is | Skew exposure |
|---|---|---|
| Twilio **mark echo** | we send a *label*; Twilio round-trips it; we stamp `time.monotonic()` at **our** receive. RTT = (our recv − our send). | none — arrival on our clock |
| Deepgram **word `.start`** | seconds **into the audio stream** we sent (stream-relative), not Deepgram wall-clock | none — a delta in a stream we anchor |
| Twilio media `timestamp` / `sequenceNumber` | ms since stream start / a monotonic counter | none — stream-relative |

Everything reduces to **one clock (ours) plus stream-relative deltas.** Had we
compared absolute foreign timestamps, we would need NTP/PTP-grade sync across
three companies' fleets — impossible. Choosing a **single physical reference we
observe on our own monotonic clock** is exactly what sidesteps multi-server
skew.

**Consequence for the design — we can drop the HLC forward-clamp.** A textbook
Hybrid Logical Clock merges a received timestamp by `l = max(local, remote,
physical)` — the clamp exists *only* to tolerate a peer whose physical clock is
ahead of ours. We have no such peer in the timing path. So we do not need the
clamp: a **raw monotonic occurrence time is already a globally comparable,
trustworthy physical coordinate.** Our model is therefore simpler than a full
HLC — physical-primary with a logical tiebreaker, minus the merge.

### The one trap in the current code

`CallLog.ts` uses `time.time()` — wall clock (`log.py`). Single-server, so no
*cross-server* skew, but wall-clock is **non-monotonic**: an NTP step mid-call
can move it backward, and two events can tie. It is fine for the human-readable
display it feeds, but **ordering must never key on `ts`.** The order key is the
monotonic occurrence time + the sequence counter below.

---

## 3. The model — a single-sourced total-order stamp

Every event carries an **EventStamp**:

```
EventStamp = (occurred_at, seq)
  occurred_at : monotonic seconds, captured at the PHYSICAL event (the producer),
                single-sourced → skew-free, globally comparable.
  seq         : a per-call strictly-increasing counter, assigned when the stamp
                is created. Guarantees a STRICT total order (no two events tie)
                and preserves assignment order when occurred_at is equal.

TOTAL ORDER  = sort by (occurred_at, seq)   -- physical primary, seq tiebreaker.
```

Why this is correct and sufficient:

- **Recovers physical order despite async append lag.** An event that occurred
  at t=10 but was logged late still sorts before one that occurred at t=11 and
  was logged earlier, because `occurred_at` is the primary key — not append
  order. This is the whole point.
- **Strict total order.** `seq` breaks true ties (rare at monotonic's
  sub-µs resolution) and orders events with no captured occurrence time (which
  fall back to stamp-time monotonic, i.e. their natural stamp order).
- **Skew-free by construction** (§2): `occurred_at` is always our single
  monotonic clock.
- **Relationship to the toolbox.** This is a Lamport-style total order
  *anchored to a single physical clock* — the HLC idea (physical primary +
  logical secondary) with the foreign-clock merge removed. We deliberately do
  NOT use a **vector clock**: vector clocks detect *concurrency* between
  independent processes; for one call with a physical reference we need a
  *total* order, not a concurrency lattice, and the counter delivers it more
  cheaply. (If we ever need "were these two producers causally independent?" —
  e.g. barge-cut vs mark-echo — a vector clock is the right escalation; it is
  out of scope here.)

`occurred_at` must be captured **at the earliest observation of the physical
event** (when the mark echo is received, when the STT callback fires, when the
media frame arrives) and threaded to the log — not stamped at append time,
which reintroduces the lag. Many producers already capture this moment
(`onset_t`, ledger `end_t`, `_tts_ended_at`); Phase 2 threads it through.

---

## 4. What we do, exactly

1. A per-call **`CallClock`** holds the `seq` counter and mints `EventStamp`s.
2. **Every `CallLog` entry** carries `occurred_at` + `order_seq`, stamped on
   append (using a producer-supplied occurrence time when available, else
   `time.monotonic()` at append).
3. **`CallLog.ordered()`** returns entries sorted by `(occurred_at, order_seq)`
   — the authoritative total order. `entries` (append order) is unchanged, so
   nothing existing breaks; the ordered view is additive.
4. Consumers that must reason about "what happened before what" (exchange
   resolver, timeline UI, eval trace) read `ordered()`, not `entries`.
5. Ordering **never** keys on `ts` (wall clock); `ts` stays display-only.

---

## 5. Implementation phases

| Phase | Scope | Risk | Status |
|---|---|---|---|
| **1** | `CallClock` (pure) + `EventStamp`; `LogEntry.occurred_at`/`order_seq`; `CallLog` stamps on append + `ordered()`; serialization; tests. Additive — append order preserved, no consumer forced to change. | low | **DONE (this commit)** |
| **2** | Threading true **occurrence time** from producers into `append(occurred_at=…)`, so `ordered()` reflects physical, not append, order. DONE for the STT/barge family (the async-lag producers): `UtteranceEvent.captured_at` (callback-fire) + existing `onset_t`; UTTERANCE anchors on onset, EAGER_EOT/END_OF_TURN/TURN_RESUMED on captured_at, BARGE_ABSORBED on the absorbed capture. Synchronous producers (mutations, SPEAK_EMITTED inside the turn) correctly default to append-time. | medium | **DONE** |
| **3** | **Physical projection** for duration queries — `media_clock.py`: `stream_to_monotonic` anchors a Deepgram word `.start` onto our clock (the onset correction — STT now captures the first word's `.start` and the transport upgrades `_utterance_onset_t` from the interim-arrival proxy to the true onset, flowing into the exchange/horizon for free); `OffsetEstimator` tracks the outbound jitter lead from mark-echo RTT for projecting an in-flight playout-complete. | medium | **DONE** |
| **4** | **Consumers read `ordered()`** — exchange resolver, `annotate_delivery_view`, timeline UI, eval trace builder — instead of raw append order. Each flip gated + tested. | medium | after Phase 2 telemetry |

Phases 1–2 give correct *order*; Phase 3 gives correct *duration*; Phase 4
switches readers over. Each lands with tests in the same commit
(simulator-tracks-engine).

---

## 6. How this composes with the timing/exchange work

- The **exchange resolver** (`full-duplex-turn-model.md` §F) compares a caller
  onset against ask playback-complete times. Both are `occurred_at` values on
  the same single clock — so the resolver is already skew-free, and once it
  reads `ordered()` (Phase 4) it is also immune to append-lag reordering.
- **Duration** questions (heard-horizon, in-flight prefix) use the same
  `occurred_at` plus the media-clock projection (§A) — Phase 3.
- The `EXCHANGE_ANCHORED` / `HORIZON_DIVERGENCE` telemetry becomes trustworthy
  end-to-end: right *order* (this doc) and right *magnitude* (the timing doc).

---

## 7. Explicitly out of scope

- **Vector clocks** — no concurrency-lattice requirement for a single call; the
  total-order counter suffices. Revisit only if we need causal-independence
  detection between producers.
- **Cross-call / global ordering** — each call is its own ordering domain; we
  never order events across calls.
- **Wall-clock synchronization** — unnecessary; we never compare foreign
  wall-clocks (§2).
