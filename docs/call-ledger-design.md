<!-- Copied 2026-10-11 from the application's monorepo (data-plane/docs/call-ledger-design.md) because the gateway's code cites it.
     It is a design record: paths and modules it names are the application's unless they say vogon/. -->

# The Call Ledger — Design

**Status:** DECIDED 2026-08-25 (the principle and the D4 resolution).
The detailed API design — put-APIs and view-APIs — is **parked**; see §7.
**Companion:** `../../voice-gateway/docs/voice-gateway-phase1-standalone.md` (§2.7 is the summary of
this document; D4 in its §8 records the decision this document explains).
**Shared class:** `app/ledger/call_ledger.py` (`CallLedger`), §6.

The principle, in one sentence: **each service appends only what it alone
knows to a shared, totally-ordered, per-call ledger; state is a fold of the
ledger; every consumer-facing history is a read-time projection (view) —
agreement between services is computed at read time from the records, never
negotiated at write time.**

---

## 1. How we got here: the barged-filler discard problem

The gateway masks LLM latency with **fillers** ("Sure, one moment.") played
while a turn computes. A **hold** brackets that window. In the single
process, the turn side opens it (`driver.begin_hold()`) before awaiting its
compute task and closes it after (`await driver.end_hold()`), receiving a
`HoldReport` whose `interrupted` field answers: *did the caller talk over a
filler?* If yes, the executor **discards the turn** — it returns before
`compose_and_commit_turn`, so no ask is registered, no audio_id minted, no
session state committed. "A discarded turn commits nothing."

```
 CALLER                GATEWAY (transport)              TURN SIDE (executor)
   │ "What's my balance?"   │                                │
   ├───────────────────────▶│ UtteranceFinal ───────────────▶│ process_events begins
   │                        │◀──── begin_hold() ─────────────┤ ┐
   │      ~1.1s later       │ filler timers armed            │ │ compute runs
   │  ♪ "Sure, one moment." │                                │ │ (1–3 s)
   │◀───────────────────────┤                                │ │
   │ "Actually never mind—" │                                │ │
   ├───────────────────────▶│ BARGE during filler            │ │
   │                        │ · filler cut, barge queued     │ ┘ compute finishes
   │                        │◀──── end_hold() ───────────────┤
   │                        ├─ HoldReport{interrupted:TRUE}─▶│
   │                        │                ╔═══════════════▼════════════════╗
   │                        │                ║ THE WINDOW: after compute,     ║
   │                        │                ║ before compose_and_commit_turn ║
   │                        │                ║ → bare `return` = DISCARD      ║
   │                        │                ║   nothing registered,          ║
   │                        │                ║   NO STATE COMMIT             ║
   │                        │                ╚════════════════════════════════╝
```

The mechanism depends on a **mid-request, bidirectional conversation**: the
turn side asks the gateway a question (`end_hold`) whose answer only the
gateway knows, *between* compute and compose. Over HTTP — one POST, one
response — that moment does not exist. The response must carry the composed
speak text, so composition necessarily runs before the turn's fate is
known:

```
 CALLER            GATEWAY (process A)                TURN SERVICE (process B)
   ├─ "balance?" ──────▶│                                  │
   │                    ├── POST /process_events ─────────▶│ compute…
   │  ♪ filler          │   (request outstanding =         │    │  no way to ask
   │◀───────────────────┤    the hold window, local)       │    │  the gateway
   ├─ BARGE! ──────────▶│ gateway KNOWS,                   │    ▼  anything
   │                    │ turn side doesn't                │ compose, commit
   │                    │◀───────── response ──────────────┤
   │        Response arrives ALREADY composed and committed.
```

## 2. The rejected design: staged commit (a two-phase protocol)

The first proposal (decision D4 option (a)): mark the response
*speculative*, defer the session-state CAS into a **staging key**
(`call:{id}:staged:{request_id}` in Redis), promote it on the next request
(implicit confirm), delete it on an explicit `discard_turn` POST.

For the **stateless** turn service this is even mechanically clean — every
request rebuilds from committed state, so "discard" is a key deletion with
nothing in memory to roll back. But it is a distributed commit protocol,
and its cost is the ceremony around a ~2-second window that only opens when
a caller barges a filler:

- a second *kind* of state write with its own lifecycle (TTL semantics),
- a resume-time disposition rule for dangling stages (dropped — nobody can
  know whether the caller heard a staged turn),
- a promote-on-next-contact rule interacting with the request_id retry
  cache,
- and on the stateful M1 registry, an in-memory rollback problem: by the
  time a discard arrives, the live context has a registered ask, a minted
  audio_id, a transcript line — a **phantom ask** that can mis-anchor the
  next turn's exchange resolution.

Four new failure-mode rules to prevent a state that turns out not to need
preventing. **Rejected 2026-08-25.**

## 3. The decision: it was never a commit problem — it is a delivery fact

The discard was a *delivery* decision entangled in the commit path. The
system already has, in production, a fully supported answer for "a turn
committed but the caller did not hear it": every caller who barges into a
*playing* response produces exactly that. The turn stays committed; the
delivery ledger records what was actually heard; the next turn reasons
against the heard record, not the transcript. A barged filler is the same
situation one beat earlier.

So, per the architecture's own division-of-knowledge rule:

- **The turn service commits unconditionally, in one write, as it does
  today.** No staging, no speculative flag, no confirm, no discard.
  `commit-then-speak` stays intact.
- **The gateway makes a purely local delivery decision.** Its filler was
  barged — the caller holds the floor — so it suppresses (or immediately
  cuts) the arriving playout, and reports the physical truth as a
  `DeliveryOutcome` fact in the next request.
- **The next turn reconciles at read time** through the same
  heard-horizon machinery every barged playout already uses.

```
 CALLER               GATEWAY (delivery ledger)            TURN SERVICE (state)
   ├─ "what's my balance?" ▶│                                   │
   │                        ├── POST process_events R7 ────────▶│ load state vN
   │  ♪ "One moment."       │   hold = request outstanding      │ compute the answer
   │◀───────────────────────┤   (fillers: LOCAL policy)         │    │
   ├─ "actually never mind"▶│ BARGE → ledger: floor=CALLER,     │    ▼
   │                        │   filler cut, barge queued        │ compose response
   │                        │◀── response {speak a9} ───────────┤ ┌──────────────────┐
   │                        │ LOCAL decision: caller has        │ │ ONE WRITE, FINAL:│
   │   (caller never hears  │ the floor → do NOT play a9        │ │ CAS vN → vN+1    │
   │    the stale answer)   │ ledger: a9 → 0 sentences          │ └──────────────────┘
   │                        ├── POST process_events R8 ────────▶│ load vN+1
   │                        │   [ DeliveryOutcome{a9,           │ FACTS FIRST: a9 was
   │                        │       truncated, heard:""}, ◀fact │ NEVER HEARD
   │                        │     UtteranceFinal{"never         │ TRIGGER LAST: answer
   │                        │       mind…"} ]        ◀ trigger  │ against what the
   │                        │◀── response (on-topic) ───────────┤ caller ACTUALLY heard
   │◀── ♪ correct answer ───┤                                   │
```

**Failure cases — the payoff over the staged design:**

```
 CALL DIES between commit and next request:
   committed vN+1 is the durable record; ledger holds "a9 unplayed";
   cleanup: NOTHING (no staged key, no TTL, no disposition rule).
   Resume loads vN+1; re-surface machinery re-asks the unheard question.
 GATEWAY dies mid-request:   turn commits; nobody plays it; same as above.
 TURN SERVICE dies mid-req:  nothing committed; gateway retries request_id;
                             computed once, played once (response cache).
 REDIS blip:                 the one existing rule applies (commit fails →
                             call fails, accepted risk). Nothing added.
```

Cost, stated honestly: the committed transcript can contain an answer the
caller never heard (true today of every barged playout), and the old
pre-compose discard optimization is gone — replaced by the same unheard-
turn path. Caller-audible difference: none (the playout is suppressed).

## 4. The ledger, generalized

What resolves the filler case is a general pattern the system was already
following in its most important read path — the history handed to the LLM
is horizon-rewritten from what the caller *heard*, not what the agent
*said*. Formalized:

```
  WRITERS (each appends only                THE LEDGER                     VIEWS (read-time projections —
  what it alone knows)                 call:{id}:log — append-only,        derived, never written)
                                       one per call, TOTAL ORDER
 ┌─────────────────┐                  ┌──────────────────────────────┐
 │ GATEWAY          │  physical facts │ seq  t     kind        keys  │        ┌────────────────────────┐
 │ (what physically │  ride requests, │ 41   12.0  UTTERANCE   turn7 │───────▶│ HeardHistory           │
 │  happened, when) │  logged on      │ 42   12.0  REQ_RECV    R7    │        │ (every LLM prompt)     │
 └─────────────────┘  arrival         │ 43   13.8  SPEAK_EMIT  a9    │        ├────────────────────────┤
          │                           │ 44   13.8  REQ_DONE R7 vN+1◀─┼─┐      │ Transcript (as-said)   │
          ▼                           │ 45   13.9  BARGE(f3)   —     │ │      ├────────────────────────┤
 ┌─────────────────┐  semantic        │ 46   14.2  UTTERANCE   turn8 │ │      │ SessionState           │
 │ TURN SERVICE     │  entries        │ 47   14.2  DELIV_OUT   a9:0s │ │      │ = fold(MUTATION rows)  │
 │ (what things     │                 │ 48   ...   MUTATION    ...   │ │      ├────────────────────────┤
 │  mean)           │                 └──────────────────────────────┘ │      │ DeliveryTimeline ·     │
 └─────────────────┘                    state_version stamped ─────────┘      │ Analytics · Compliance │
                                        into the commit row                   └────────────────────────┘
```

The same rows produce different, *equally correct* answers to different
questions:

```
  Transcript view (as-said):              HeardHistory view (as-heard):
  ┌────────────────────────────┐         ┌────────────────────────────┐
  │ Caller: what's my balance? │         │ Caller: what's my balance? │
  │ Agent:  Your balance is    │         │ Caller: actually never     │
  │         $310 and…    (a9)  │         │         mind…              │
  │ Caller: actually never     │         │ (a9 rewritten OUT — the    │
  │         mind…              │         │  ledger shows 0 sentences  │
  └────────────────────────────┘         │  reached the caller)       │
   audit truth: the system                └────────────────────────────┘
   composed and committed a9               reasoning truth: the agent must
                                           not assume the caller heard it
```

**Ledger vs distributed transaction:**

```
   Distributed transaction: both sides must AGREE before either advances
                            (coordination, staging, in-doubt windows)
   Ledger:                  each side records only what it alone knows,
                            immediately and unconditionally; agreement is
                            COMPUTED later from the records
```

## 5. The rules that make it work

1. **Entry envelope** (every entry, every writer): `redis_seq` (total
   order), `t` (call-relative seconds — ONE timeline, normalized at the
   gateway edge; two raw monotonic clocks would make the ledger lie),
   `request_id`, `turn`, `kind`, `payload`, and the physical join key
   (`audio_id`) where one exists. An entry missing envelope fields breaks
   every view downstream.
2. **Order is the ENTRY NUMBER — always (decision 2026-08-25).** Each
   entry increments the previous; the Lua INCR+XADD
   (`call-event-ordering.md` §5, commit `44c4862`) makes stream order
   equal `redis_seq` by construction, multi-writer safe, gaps
   detectable. `t` is an ANNOTATION (the gateway's authoritative clock,
   rebased): a view may present by it, but the ledger never re-sorts by
   time — a late-appended fact keeps its append position, its annotation
   showing when it occurred. `CallLog.ordered()` follows the same rule.
3. **Writers.** Semantic entries: the turn service. Physical facts: enter
   via `ProcessEventsRequest.events` and are logged on arrival (single
   writer today); post-split the gateway may append purely-physical rows
   directly — the Lua script already makes that ordered.
4. **No side-channel writes.** The `LLM_CALLED` bypass
   (`executor.py:116-131` pushes a hand-built line straight onto the
   writer queue, skipping `CallLog`) is the counterexample: the archived
   ledger and the in-memory log become different event sets. Every append
   goes through the one front door.
5. **Snapshot discipline.** `call:{id}:state` (CAS on `state_version`)
   remains the live-call fast path but is conceptually a **cached fold**.
   Each commit's `REQUEST_COMPLETED` row carries the `state_version` it
   produced, making `snapshot(vN) == fold(ledger → that row)` a checkable
   invariant; reconstruction is a replay (the evaluator is the standing
   proof that replay works).
6. **Per-call scope.** Nothing on the live path reads another call's
   ledger. Cross-call views (dashboards, tenant analytics) are offline
   folds over many ledgers.

## 6. The shared class — `app/ledger/`

`CallLedger` (`app/ledger/call_ledger.py`) is the one home for ledger
*operations*, importable by both halves (the package sits outside both
`app/gateway` and the reasoning packages, like the contract):

- **Write substrate:** owns the writer queue + drain task (JSONL file +
  Redis stream + monitor fan-out), the atomic `append_log_entry`
  (INCR+XADD Lua), and the close protocol (sentinel → drain → seal TTLs).
  Created by `begin_call`, closed via `close_call_log` at teardown.
- **Read substrate:** `read_entries()` — the raw rows (Redis stream first,
  JSONL fallback) that views fold over.
- **Post-call append (decision 2026-10-01):** the one sanctioned write to a
  CLOSED ledger. Facts that physically resolve after the session is gone —
  a warm transfer's agent legs and how the transfer ended — still belong in
  that call's record, so `append_after_close` (`app/ledger/substrate.py`)
  appends them after `CALL_ENDED`, to the JSONL file and the Redis stream,
  marked `detail.post_call`. Their `order_seq` is the closed log's highest
  plus the fact's own gap-free seq — a pure function of (closed log, fact),
  so retried or out-of-order deliveries land in the same slot — and they are
  idempotent on the caller's key. Only accepted once the call is finalized
  (its session state is gone); the S3 call-log archive is re-uploaded after
  each append. Caller: `POST /turn/v1/record_transfer`
  (`app/turns/post_call.py`); design in
  `voice-gateway/docs/ESCALATION_WARM_TRANSFER.md`.
- `CallLog` (`app/framework/log.py`) remains the semantic *authoring*
  front — typed `EventType` appends and the in-memory view for the live
  turn; its append hook feeds the `CallLedger` writer. Unifying these two
  is part of the parked design (§7).

## 7. TODO — parked: the detailed ledger API design

> **2026-08-26:** the survey half of this work is DONE — every write, read
> and view touch point mapped, and the target boundary proposed:
> **`docs/call-ledger-boundaries.md`** (one `app/ledger/` package owning
> rows, authoring, substrate, the one durable reader, and all view folds;
> AST-fenced). The items below remain the checklist; the boundaries doc is
> where they now live in detail.

Deliberately not designed yet; to refine as its own piece of work
(tracked in the repo `TODO.md`):

- **Put-APIs.** Typed append with envelope enforcement (reject
  envelope-incomplete entries), the gateway's direct physical-append
  path post-split, and the `LLM_CALLED` bypass folded through the front
  door (T4).
- **View-APIs.** First-class, versioned fold functions over
  `read_entries()`: `heard_history()`, `transcript()`,
  `session_state()`, `delivery_timeline()`, `compliance()` — replacing
  today's scattered ad-hoc readers (`/calls/{id}/live-log`, the archive
  worker, the horizon rewriter) with one read surface.
- **Snapshot equivalence check.** CI: replay a corpus call's ledger,
  fold `session_state()`, diff against the committed snapshot at each
  `state_version`.
- **Cross-call folds.** Offline multi-ledger consumers (dashboards,
  per-tenant analytics) — never on the live path.
- **Envelope versioning.** A `v` field on entries before any consumer
  outside this repo reads the stream.
