# The empathy split — catalog and design (no code)

> **Superseded (2026-09-24), not implemented.** The survey in `FRONT_COMPLEMENT_DESIGN.md`
> found the desk already acknowledges on 12 of 20 hardship lines, so the gateway's sympathy
> tier was dropped instead: on distress the gateway stays silent and the desk keeps its own
> acknowledgement. Kept for the catalog of registry openers.

**Status:** CATALOG + DESIGN, 2026-09-20. No code changes. Companion to
`voice-gateway-front-model.md` (the front model, v1 shipped in `b13a882`).

**The problem, measured.** Every hardship turn in the live ladders played
the same acknowledgement twice: the front model's "I'm sorry, that's a lot
to be carrying right now" and, 1–3 s later, the desk's recited template
opening with "I'm sorry — that's a lot to deal with". The desk's sentence is
not an LLM empathy pass and there is no empathy agent on the collections
call; it is baked into the templates. So the fix is in the registry: split
every recited line into its **empathy** clause and its **regulated** text,
let the front own the empathy when it is on, and have the desk speak the
combined line when it is off — or when the front had nothing to say.

---

## 1. Definitions

- **Recited line** — a template in a `*_lines.yaml` registry under
  `app/agents/collections/`, rendered by `renderer.render`, checked against
  its `must_say` / `must_not_say`. Every word the desk says on the
  collections_v3 call that is not LLM-generated comes from one of these.
- **Empathy clause** — the leading words of a template that acknowledge,
  sympathise, thank or soften and carry NO required phrase, NO figure and NO
  compliance content. "I'm sorry — that's a lot to deal with." "Thank you
  for telling me." "I hear you."
- **Regulated text** — everything else in the template: the disclosure, the
  figure, the ask, the posture statement the register requires ("I'm not
  going to push you for a payment today" is `must_say`), the class-A
  signature. Only the desk says it.
- **Assent opener** — a leading word that RULES: "Of course", "Sure",
  "Absolutely", "Understood", "Got it", "Great", "No problem", "That's
  done", "All set". These are the desk's decision spoken aloud. They are
  not empathy, the front is barred from them, and they stay on the desk.
- **Compliance opener** — a leading sentence that looks like a softener but
  carries a required phrase or a fact ("Thank you, you're verified.",
  "Sorry — this is Maya … on a recorded line", "I understand, but I'm not
  able to waive the {late_fee_str} late fee"). Regulated; never split.

## 2. Catalog — `v3_lines.yaml` (the active registry, 204 lines)

Method: every template whose first words are one of the softener openers
(sorry / of course / thanks / thank you / understood / that's alright / I
hear you / no problem / take your time / I appreciate / got it / sure / all
set / that's done / absolutely / I understand / great / perfect), then the
opener classified by what it carries. 62 of 204 lines open that way.

### 2.1 SPLIT — the empathy clause moves to the front (22 lines)

The clause is what the front will own; the regulated remainder keeps every
`must_say` phrase (checked: none of these clauses contains one).

| line id | empathy clause (moves) | regulated text begins | note |
|---|---|---|---|
| `v3_hardship_options_soft` | I'm sorry — that's a lot to deal with. | I'm not going to push you for a payment today. Nothing has to be decided on this call. The simple paths are … | class A; the signature is in the regulated text; `must_say` "not going to push" stays |
| `v3_hardship_ladder_catchup` | I'm sorry — that's a lot to deal with. | I'm not going to push you for a payment today. One path I can set up on this call is a catch-up … | THE double from the ladders |
| `v3_hardship_ack` | I'm sorry — that sounds genuinely difficult. | I'm not going to pressure you. I've noted a hardship review request … | |
| `v3_hardship_preverify_ack` | I'm sorry — that sounds genuinely difficult. | I'm not going to pressure you. I've noted a hardship review request … verify … | |
| `v3_referral_open_disaster` | I'm sorry — that sounds frightening. | I've noted the {event_str} and that you're in {location_str}, and I'm opening a disaster relief … | |
| `v3_referral_open_medical` | Thank you for telling me — I hope the recovery goes smoothly. | Let me be clear about how a payment pause works … | a wish, not a promise |
| `v3_referral_open` | Thank you for telling me. | We don't need to go through the details now. What you're describing needs one of our hardship specialists … | |
| `v3_referral_open_modification` | Thank you for telling me. | A permanent drop in income is exactly what a longer-term modification review is for … | |
| `v3_referral_open_affordability` | Thank you for telling me. | That's a change to the arrangement itself, and it needs a specialist review … | |
| `v3_referral_open_exhaustion` | Thank you for being straight with me. | If {offer_str} is everything you have until next month, I don't want to just take it … | |
| `v3_partial_exhaustion_then_refer` | Thank you for being straight with me. | I'll take the {amt_str} today and I won't ask you for more than that … | |
| `v3_referral_open_settlement` | Thank you. | I've noted your offer of {offer_str} to settle the loan … | |
| `v3_referral_medical_noted` | Thank you. | I've noted that you're still employed … | |
| `v3_referral_medical_noted_generic` | Thank you. | I'll put those details in the specialist's case file. | |
| `v3_existing_payment_noted` | Thanks for telling me. | I've noted that you already have a payment arranged for this … | `must_say` "noted" is in the remainder |
| `v3_summary_full_ask_soft` | Thanks for talking this through with me. | The past-due amount on the account is {past_due_str}. I'm not here to pressure you … | |
| `v3_resolve_reask_soft` | I hear you, and thank you for telling me. | So I can find something that actually works — is there any amount you could safely pay today … | |
| `v3_resolve_alternatives_soft` | That's alright — nothing has to be settled on this call. | If it helps, we can look at a date rather than a payment today … | the clause is reassurance, not a ruling |
| `v3_preverify_defer` | I hear you, and there are things we can look at. | I'm not going to press you on anything today. I just need to confirm who I'm speaking with … | |
| `v3_partial_below_floor` | I appreciate the offer. | The smallest amount I'm able to take toward the past due is {floor_str} … | |
| `v3_fee_objection_waiver_conditional` | I hear you on the late fee. | Since this is the first time on this account, I can waive the {late_fee_str} … | |
| `v3_payment_failed`, `v3_payment_failed_leg` | I'm sorry. | That payment didn't go through; the card was declined by the processor … | the apology is the desk's; see §4 — the front cannot know a card declined |

Two in that table need a judgment call, flagged now so nobody discovers
them in review: `v3_resolve_alternatives_soft`'s "nothing has to be settled
on this call" reads as reassurance but is close to a posture statement;
and the two `v3_payment_failed*` apologies are about an event only the desk
knows about, so their clause should stay on the desk even when the front
is on (rule in §4.3).

### 2.2 KEEP — assent openers, the desk's ruling (27 lines)

Not empathy. The front never says these words (guard: `commitment`), and
the desk keeps them because they ARE the answer.

`v3_dnc_confirmation` (Absolutely.) · `v3_cease_collections` (I understand.)
· `v3_wrong_party_close` (Understood —) · `v3_bad_timing_close` (No problem
—) · `v3_split_open_ask` (Sure —) · `v3_just_checking` (Of course —) ·
`v3_no_resolution_close` (I understand.) · `v3_resolve_ask_amount_on_day`
(Got it —) · `v3_resolve_alternatives` (I understand.) ·
`v3_negotiation_obstacle` (Understood.) · `v3_negotiation_after_today_refused`
(Understood, no payment today.) · `v3_payment_discussion_declined`
(Understood.) · `v3_payment_failed_no_retry_ack` (Understood —) ·
`v3_captured` (That's done —) · `v3_scheduled_done` (All set —) ·
`v3_auth_declined` (No problem —) · `v3_purpose_reply_unverified` (Of course
—) · `v3_waiver_applied_ack` (Great —) · `v3_fee_objection_courtesy`,
`v3_fee_objection_explained`, `v3_waiver_window_lapsed`,
`v3_waiver_not_eligible` (I understand.) · `v3_assist_loan_orientation` (Of
course.) · `v3_sms_refused_portal` (Understood —) · `v3_over_cure_cure_only`
(Understood —) · `v3_backchannel_yield` (Of course — go ahead, I'm
listening.) · `v3_availability_ask` (Thanks.)

Two of these deserve a second look for a different reason than this
document's: "Understood, no payment today." after "I'm not paying anything
today" and "Of course — go ahead" are exactly the assent shapes the front
is barred from on a recorded line. That is a registry review, not a split.

### 2.3 KEEP — compliance openers, regulated by content (13 lines)

The opener carries a required phrase, a figure or a disclosure. Never split.

`v3_party_reask` (Sorry — this is {agent_name} … recorded line) ·
`v3_identity_ask_engaged` (must_say "made a note") ·
`v3_identity_fail_close` (the verification-failed statement) ·
`v3_disclosure` (Thank you, you're verified.) ·
`v3_dispute_hold_offer_ack` (class A, "noted exactly what you're offering") ·
`v3_hardship_ladder_clarify` (must_say "option") · `v3_close_plain`
(must_say {borrower_first}) · `v3_fee_objection_denied` (must_say "not able
to waive") · `v3_payment_pending_no_retry` (must_say {action_ref}) ·
`v3_rails_confirm_reask`, `v3_rails_reask`, `v3_payment_failed_choice_reask`
(Sorry — a clarification re-ask; the "sorry" is part of the re-ask's
function) · `v3_identity_ask_engaged`.

### 2.4 Outside the registries

- `hold_request.py:99` — "Of course — take your time. Just let me know when
  you're ready to continue." A hardcoded interrupt line. Assent opener;
  belongs in the registry (it is the only desk line the ladders hit that is
  not there), and its "take your time" is the front's ROOM register — with
  the front on, the desk's part is just "Let me know when you're ready to
  continue."
- LLM-generated speech (the `resolve_llm` seat, `assist` explanations,
  `generate`-class lines): not in the registries, so the split cannot
  reach it. Those prompts get one rule when the front is on: do not open
  with an acknowledgement. Out of scope here, listed so it is not lost.

### 2.5 The legacy registries (v2 agents, not on the collections_v3 path)

Same method, for completeness; touch only if v2 is ever revived.

| registry | lines opening with a softener | of which empathy (would split) | of which assent / compliance |
|---|---|---|---|
| `hardship_lines.yaml` (66) | 21 | 9 — `hd_afford_opener_no_offer`, `hd_afford_opener_exhaustion`, `hd_afford_opener_at_risk`, `hd_partial_floor_referral_amt/_noamt`, `hd_split_floor_escalation`, `hd_general_referral(_nomonthly)`, `hd_info_registered_email_issue` | 12 (Of course / Sure / Got it / Great / Thank you — I've captured …) |
| `negotiation_lines.yaml` (58) | 21 | 6 — `neg_unverified_defer`, `neg_fee_objection_stands`, `neg_filler_take_time`, `neg_filler_hardship_gentle`, `neg_auth_declined_partial`, `neg_alternatives_probe` | 15 (I understand — as a ruling opener on 8 lines; Of course / Absolutely / Perfect / Thank you — I've recorded) |
| `collections_lines.yaml` (8) | 3 | 1 — `hardship_waiver_conditions` (I'm sorry —) | 2 |

## 3. Totals

| | v3 (active) | legacy |
|---|---|---|
| lines opening with a softener | 62 / 204 | 45 / 132 |
| empathy clause → the front | 22 | 16 |
| assent opener → stays, the desk's ruling | 27 | 29 |
| compliance opener → stays, regulated | 13 | — |

The 22 v3 splits are the whole scope of the change on the desk side.

## 4. Design — split at the registry, decide at the gateway

### 4.1 Registry schema (additive)

```yaml
  v3_hardship_ladder_catchup:
    empathy: >-
      I'm sorry — that's a lot to deal with.
    template: >-
      I'm not going to push you for a payment today. One path I can set up
      on this call is a catch-up: would paying the missed {monthly_str}
      within about a week work for you?
    must_say: ["not going to push", "{monthly_str}", "week"]
```

Rules the renderer enforces at load (the same way the front's beat pools
are linted):

- `must_say` and `class_a_signature` are checked against `template` alone.
  A required phrase inside `empathy` fails the load — the clause is
  optional speech and can carry nothing required.
- `must_not_say` is checked against `empathy + template`.
- `empathy` passes the front guard's content and commitment lexicons
  (`front_guard.guard`, minus the repeat rules): one sentence, no figure,
  no assent word. That is what makes it safe for EITHER side to say.
- `render()` returns both parts; `recite()` (the single-string API today's
  agents call) returns the combined text so nothing that reads a string
  breaks.

### 4.2 The wire (additive)

`SpeakCommand.empathy: str = ""` — the clause travels beside `text`
(regulated). Old gateways ignore it and play `text` only, which is the
regulated-only behaviour; acceptable for a peer that is mid-upgrade.

### 4.3 The decision — the gateway knows whether the front spoke

The turn service does not know, in this turn, whether the front said
anything: the model may have been late, empty by design, guarded, withheld
because the borrower resumed, or barged. The gateway knows all of that from
its own hold. So the rule lives in the driver, at playout:

```
for each SpeakCommand:
    if cmd.empathy and not front_spoke_this_turn:
        play(cmd.empathy + " " + cmd.text)       # combined — today's line, verbatim
    else:
        play(cmd.text)                             # regulated only
```

where `front_spoke_this_turn` is true only when a TIER-1 line (the model's)
played to completion this turn. A **beat** does not count: "One moment."
on a hardship turn means the model was late, and the borrower still has
not been acknowledged. A **barged** front line does not count either, but
the D4 rule already suppresses that turn's reply, so the case never
reaches this branch.

With the front **disabled**, no hold line is ever a model line, so every
command plays combined — exactly today's audio, byte for byte. That is the
"combine if the front is disabled" rule, obtained without a flag crossing
the seam, and it holds for every way the front can be silent on a turn.

The two `v3_payment_failed*` apologies are excluded from the rule by a
registry marker (`empathy_owner: desk`): the event they apologise for is
one the front cannot know about.

### 4.4 What the call log records

Today's `DeliveryOutcome.text` (added for the front model) carries the
text the gateway actually played whenever it differs from the command. So:
the turn service logs `SPEAK_EMITTED` with `speak = empathy + " " + text`
as the intended line and `empathy` as its own field; the outcome fact
carries what played; the delivery-annotated view shows the borrower heard
the regulated text alone on turns where the front carried the empathy, and
the front's own `reason=front` row sits beside it. The transcript reads as
it sounded.

### 4.5 Why not the desk-side flag

The alternative — the desk strips the clause whenever the front is
enabled — is simpler on the wire and fails on every turn the front is
silent: the borrower who just disclosed a job loss would hear "I'm not
going to push you for a payment today" with no acknowledgement at all, and
the registry's own comment on the soft line records that exact omission as
a corpus defect. The gateway rule costs one string field and never leaves
a hardship disclosure unacknowledged.

## 5. The prompt — the front owns the acknowledgement

Today's prompt (`app/agents/collections/front.yaml`) treats SYMPATHY as one
register among several and lets the model decide whether a line is
warranted. With the split, on the turns whose registry line has an empathy
clause, the front's sentence is the ONLY acknowledgement the borrower will
hear. The prompt changes in three places. Nothing else moves.

### 5.1 New section, placed right after WHAT THE CALLER SAID -> WHAT YOU DO

```
YOU OWN THE ACKNOWLEDGEMENT
When the borrower has just told you something hard — lost work, illness,
a death, a disaster, money gone, fear — or has just been straight with you
about what they can and cannot do, YOU are the one who acknowledges it.
The reply that follows yours will not; it goes straight to what can be
done. So on those turns a line is not optional: say one sentence for what
they said, specific to it, in the shape below, and nothing about what
happens next.

The shapes, and when each fits (the agent's own voice — match it):
- something hard happened to them   -> "I'm sorry — that sounds genuinely difficult."
                                       "I'm sorry — that's a lot to deal with."
                                       "I'm sorry — that sounds frightening."
- they told you something private   -> "Thank you for telling me."
                                       "Thanks for telling me."
- they were honest about their limit -> "Thank you for being straight with me."
- they made an offer or proposal    -> "I appreciate the offer."   (never "Sure", never a readback)
- they pushed back on something     -> "I hear you."   "I hear you on the fee."   (never "Understood")
- they are recovering / going through treatment -> "I hope the recovery goes smoothly."

These are the same shapes the agent uses everywhere else on the call, so
the borrower hears one person. Say them in your own words around the
specific thing they said; never say the compliance clauses that follow
them — "I'm not going to push you", "I've noted", "a specialist", "nothing
has to be decided" — those are the next line's, not yours.
```

### 5.2 One change under WHAT YOU NEVER SAY

```
- Never the clause that comes AFTER an acknowledgement in the agent's
  lines: no "I'm not going to push / press / pressure you", no "I've
  noted", no "specialist", no "nothing has to be decided", no "we can look
  at". Those state a position; the desk states positions.
```

### 5.3 Examples — replace the three sympathy examples with catalog pairs

```
"I lost my job three weeks ago and I'm behind on everything." -> "I'm sorry — that sounds genuinely difficult."
"My hours got cut last month and money's really tight."        -> "I'm sorry — that's a lot to deal with."
"There was a flood, we're staying with my sister."             -> "I'm sorry — that sounds frightening."
"I'm on medical leave until October."                           -> "I hope the recovery goes smoothly."
"Honestly, two hundred is everything I have until payday."     -> "Thank you for being straight with me."
"I can do seventy five today and the rest next week."          -> "I appreciate the offer."   (NOT "Sure", NOT "You'd like to pay seventy five")
"Why is there a late fee on this at all?"                       -> "I hear you on the fee."   (NOT "Understood")
```

### 5.4 What does not change

The one-sentence, twelve-word, no-commitment-word, no-action-claim rules;
the silences on identity data and plain answers; the beats on
authorizations; ALREADY SAID; the three-word opener rule. The guard is
unchanged: every shape above already passes it (checked against the
lexicons in `front_guard.py`), and "Understood" and "Sure" are already
silenced.

## 6. Verification, before any wiring

1. **Registry lint** — every `empathy` clause passes the front guard, no
   `must_say` phrase is inside a clause, `recite()` of every split line
   equals today's template text (byte for byte, so nothing the agents say
   changes when the front is off).
2. **The prototype replay** — the 20 ladders through `prototypes/front_model/run.py`
   with the new prompt: every G/F turn draws a line (the shape check flips
   those categories from "spoken or silent" to "spoken"), zero guard trips,
   and a new check: the front's line and the desk's split clause never
   both open with the same three words (the double, measured to zero).
3. **The driver harness** — `test_phone_fillers_conversation.py` gains a
   turn whose scripted reply carries `empathy`: with a model line played,
   the borrower hears the regulated text alone and the log carries both;
   with the model scripted empty, the combined line plays and equals
   today's string; with the front disabled, the combined line plays.
4. **The live ladders** — scripts a and c through
   `smoke_front_live_collections.py`: the hardship turn shows ONE
   acknowledgement, the front's, followed by "I'm not going to push you
   for a payment today …".

## 7. Open questions for the review

| # | question | proposed |
|---|---|---|
| Q1 | `v3_resolve_alternatives_soft`'s "nothing has to be settled on this call" — empathy or posture? | posture; keep it on the desk, split only "That's alright." |
| Q2 | `v3_referral_open_medical`'s "I hope the recovery goes smoothly" — a wish the front may say? | yes; it promises nothing and the catalog shows the desk says it |
| Q3 | should the assent openers in §2.2 ("Understood, no payment today", "Of course — go ahead") be reviewed under the same lexicon the front is held to? | separate item; this document only catalogs them |
| Q4 | the legacy registries — split now for symmetry, or leave until v2 is touched? | leave; v2 is not on the path |
| Q5 | LLM-generated lines (`resolve_llm`, `assist`) — one prompt rule, or a post-check with the same lexicon? | the prompt rule first; measure the double rate in the ladders |
