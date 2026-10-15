<!-- Copied 2026-10-11 from the application's monorepo (data-plane/docs/name_recognition_design.md) because the gateway's code cites it.
     It is a design record: paths and modules it names are the application's unless they say vogon/. -->

# Name Recognition — Layered Accuracy Design

## Problem

Proper nouns — especially names of South Asian, Latin, and other non-Western origin — are mis-transcribed by Deepgram with high frequency on loan servicing calls. The typical failure mode is a phonetically plausible but wrong spelling ("Kabita" for "Kavita", "Abhay" for "Avay") that either passes confirmation silently or creates a retry loop that never converges.

Regex and keyword matching cannot solve this class of problem because the error space is open-ended and caller-specific. The solution is a layered defense that improves accuracy at each stage of the pipeline: before transcription, during transcription, during extraction, and after the loan CRM lookup.

---

## Architecture: Four Layers

```
Caller audio
    │
    ▼
┌─────────────────────────────────────────────────────┐
│  Layer 1 — Deepgram keyterm boost                   │
│  known name → weighted STT prior                    │
│  (only when name is known at call start)            │
└──────────────────────┬──────────────────────────────┘
                       │ transcript + per-word confidence
                       ▼
┌─────────────────────────────────────────────────────┐
│  Layer 2 — Low-confidence word annotation           │
│  confidence < 0.75 → LLM input annotated            │
│  transcript display stays clean                     │
└──────────────────────┬──────────────────────────────┘
                       │ annotated utterance
                       ▼
┌─────────────────────────────────────────────────────┐
│  Layer 3 — Hard spelling trigger                    │
│  1 rejection on name field → override speak         │
│  "Could you spell it letter by letter?"             │
└──────────────────────┬──────────────────────────────┘
                       │ confirmed field value
                       ▼
┌─────────────────────────────────────────────────────┐
│  Layer 4 — CRM name reconciliation                  │
│  fuzzy match collected vs CRM authoritative         │
│  ratio ≥ 0.6 → silently adopt CRM spelling          │
└─────────────────────────────────────────────────────┘
```

Each layer is independent and additive. A call that benefits from Layer 1 still passes through Layers 2–4. Layer 4 is the backstop for any residual error that makes it through the earlier layers.

---

## Layer 1 — Deepgram Keyterm Boost

**Where:** `STTSession.start()` in `app/pipeline/stt.py`

**Trigger:** `name_hint` passed in the `POST /api/v1/calls/initiate` JSON body.

**Mechanism:** Deepgram's v2 Flux API accepts a `keyterm` parameter at connection time. The model weights transcription posteriors toward those terms. Passing `["Maria", "Garcia"]` means Deepgram already expects those phonemes before the caller speaks.

**Data flow:**
```
POST /calls/initiate  {name_hint: "Maria Garcia"}
  → config["_call_hints"]["name_hint"] = "Maria Garcia"
  → set_cached_config(token, config)
  → VoiceTransport.start() reads name_hint, splits into ["Maria", "Garcia"]
  → STTSession(keyterms=["Maria", "Garcia"])
  → client.listen.v2.connect(keyterm=["Maria", "Garcia"])
```

**Coverage:** Any outbound loan servicing call where the borrower's name is known before dialing (i.e., pulled from the LMS/CRM before initiating the call). For inbound calls without a name hint, this layer is a no-op.

**Why split into tokens:** Deepgram matches keyterms at the word level. "Maria Garcia" as a single keyterm would require the model to hear both words together; splitting gives independent boosts to each token.

---

## Layer 2 — Low-Confidence Word Annotation

**Where:** `STTSession._on_message()` → `VoiceTransport.on_final()` → `handler._process_utterance()`

**Trigger:** Any word in `ListenV2TurnInfo.words` with `confidence < 0.75`.

**Mechanism:** Word-level confidence scores already exist in the Deepgram v2 response but were previously unused. This layer extracts them, threads them as metadata alongside the utterance, and injects a plaintext annotation into the LLM input. The transcript display (stored in `session.transcript_lines`) always receives the raw text.

**Data flow:**
```
ListenV2TurnInfo.words = [{word: "Kabita", confidence: 0.43}, ...]
  → low_conf_words = ["Kabita"]  (words below 0.75 threshold)
  → utterance_queue.put(("Kabita Sharma", ["Kabita"]))
  → handler unpacks: caller_text="Kabita Sharma", low_conf_words=["Kabita"]
  → session.add_user_turn("Kabita Sharma")          ← clean transcript
  → utterance_for_llm = "[Note: low transcription confidence on: Kabita] Kabita Sharma"
  → extraction LLM sees annotation, treats name as uncertain
```

**Effect on extraction:** The DataCollectionSubagent mega-prompt's existing rules ("if the name is garbled or phonetically ambiguous, ask the caller to spell it") are now reliably triggered because the LLM is explicitly told which words are uncertain, rather than having to infer this from the phonetics alone.

**Threshold rationale:** 0.75 is conservative. Deepgram word confidence on correct common English words is typically > 0.90. Proper nouns in the 0.40–0.74 range are almost always partially speculative.

---

## Layer 3 — Hard Spelling Trigger

**Where:** `DataCollectionSubagent` in `app/framework/agents/data_collection.py`

**Trigger:** A `confirm_no` intent on a field with `data_type == "name"` (i.e., the caller said "no that's wrong" at least once).

**Mechanism:** Rather than leaving it to the LLM to decide whether to ask for spelling (which it does inconsistently), this is a deterministic code path. A rejection counter is maintained in `node_state.internal_state` per field. On the first rejection, the next `speak` is overridden unconditionally.

**State tracking:**
```python
internal_state["_name_rejections_full_name"] = 0   # before any rejection
internal_state["_name_rejections_full_name"] = 1   # after confirm_no
# → triggers spelling override on next speak

internal_state.pop("_name_rejections_full_name")   # on successful confirm_yes
```

**Override text:**
> "I'm having a little trouble with the name. Could you please spell it out for me, one letter at a time? For example, say 'S-A-R-A-H' for Sarah."

**Coverage:** Both the fast-path classifier (for clear confirm_no signals) and the mega-prompt path (for ambiguous cases that the LLM classifies). Does not fire for `date`, `phone`, `email`, or `number` fields.

**Why only after 1 rejection (not 2):** Names are the most failure-prone field on phone calls. Asking for spelling after the first miss rather than the second eliminates one unnecessary round-trip. The spelling ask is itself gentle and non-alarming.

---

## Layer 4 — CRM Name Reconciliation

**Where:** `LoanLookupAgent.handle()` in `app/agents/loan/loan_lookup.py`

**Trigger:** Loan reference verification succeeds (`match_type != "none"`), and `_crm_customer.full_name` is present in tool results.

**Mechanism:** After identity verification, `LoanLookupTool` performs a CRM lookup by `ssn_last4` and stores the authoritative name from the customer record. This ground truth is compared against the caller-provided name using `difflib.SequenceMatcher`. A fuzzy ratio ≥ 0.6 is treated as "phonetically plausible — same person, STT error" and the CRM spelling replaces the collected value silently via a `FieldConfirmed` mutation.

**Decision table:**

| collected_name   | crm_name       | ratio | action                        |
|------------------|----------------|-------|-------------------------------|
| Kabita Sharma    | Kavita Sharma  | 0.87  | adopt CRM spelling            |
| Maria Gracia     | Maria Garcia   | 0.92  | adopt CRM spelling            |
| John Smith       | Maria Garcia   | 0.24  | keep collected, log WARNING   |
| Kavita Sharma    | Kavita Sharma  | 1.00  | no-op (names identical)       |

**Why 0.6 as threshold:** Names that are phonetically close enough to be the same person (e.g., "Kavita"/"Kabita", "Garcia"/"Gracia") score 0.75+. A ratio of 0.6 allows for some edit distance on longer names while still rejecting completely unrelated names. Below 0.6 the mismatch is large enough to be a security concern rather than an STT error.

**Why silent (no caller prompt):** The CRM name was established through a prior KYC process — it is more reliable than a live STT transcription. Silently correcting an obvious STT error is better UX than asking the caller "did you mean Kavita?" when the SSN already confirmed their identity. A WARNING log is emitted so operators can audit corrections.

---

## Coverage by Call Type

| Call type                       | L1 keyterm | L2 low-conf | L3 spelling | L4 CRM |
|---------------------------------|------------|-------------|-------------|--------|
| Outbound with known name        | ✓          | ✓           | ✓           | ✓      |
| Inbound, name easily heard      | ✗          | ✓ (no-op)   | ✗ (no-op)   | ✓      |
| Inbound, name mis-heard once    | ✗          | ✓           | ✓           | ✓      |
| Inbound, name mis-heard 2+ times| ✗          | ✓           | ✓           | ✓      |

The CRM reconciliation (Layer 4) is the universal backstop for all inbound calls — as long as the SSN lookup succeeds, any residual STT error in the name is corrected automatically.

---

## Files Changed

| File | Layer | Change |
|------|-------|--------|
| `app/routers/calls.py` | 1 | Accept `name_hint` in initiate body |
| `app/pipeline/stt.py` | 1, 2 | keyterm param; low-conf word extraction |
| `app/transport/voice_transport.py` | 1, 2 | Pass keyterms; thread low-conf words |
| `vogon/websocket/handler.py` | 2 | Unpack tuple; annotate LLM input |
| `app/framework/agents/data_collection.py` | 3 | Rejection counter; spelling override |
| `app/agents/loan/loan_lookup.py` | 4 | CRM fuzzy compare; FieldConfirmed mutation |

No changes to agent interfaces, workflow graph, or call protocol.
