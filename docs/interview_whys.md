# SetuHaul FDE challenge — interview "whys"

Crisp answers to the questions an interviewer is most likely to ask. All numbers come from the committed
live run (`brief_scenarios.json` at HEAD, model `nvidia/nemotron-3-ultra-550b-a55b`, 27 agent calls, 588 s)
unless stated otherwise. Companion docs: `knowledge_base.md` (details), `demo_script.md`, `client_readout.md`.

**One-line pitch:** a conversational driver-exception service for the Jaipur DC where three LLM agents
*advise* (triage, assessment, dispatch) and a deterministic slot engine *decides* (feasibility, hold,
request, confirm, release), with guards between them. Live result: 92.6/100 workflow confidence,
0 conflicting allocations, 100% escalation-record completeness.

---

## A. Architecture

**1. Why does the LLM only advise while code decides?**
Dock capacity is a shared, safety-relevant resource with hard constraints (weight, reefer, dock events,
operating hours, other trucks). An LLM is good at reading messy WhatsApp text and choosing what to ask, but
it is non-deterministic and cannot see the whole facility queue reliably. So the model never commits state:
`state_change_allowed` is forced to `false`, it can only pick from IDs the engine already proved feasible,
and every write goes through `slot_engine.py`. The brief's boundary says the same: the agent never decides
capacity, vehicle–dock fit, ranking or commit status alone.

**2. Why three agents instead of one?**
Each has a narrow job and a narrow output: Triage (classify the message, decide verification and whom to
ask), Assessment (pick a dock/vehicle option from a pre-filtered list), Dispatch (pick a slot from a
pre-filtered list). Narrow prompts are easier to evaluate per role, and failures are localised — in the live
run all 4 wrong decisions were in Triage; Dispatch and Assessment were 100% on decision.

**3. Why guards if the prompt already says "only choose feasible IDs"?**
Prompts are requests, not guarantees. `guards.guard_answer` enforces them in code: a selection outside the
feasible set is nulled and turned into ESCALATE/COORDINATOR (or NO_FEASIBLE_OPTION if nothing is feasible); an
attempted state change is blocked; a selected ID that does not exist in the input is removed. The run
justifies it: 13 of 27 answers needed a guard correction (11 over-claimed `VERIFIED`, 2 invented a
`selected_id` from a WhatsApp message ID). None of them leaked into state.

**4. What exactly counts as "verified"?**
Only independent evidence: a facility message, a gate check-in, or a facility-logged dock event. A driver's
statement is a report. If the model says `VERIFIED` and the payload has no `independent_confirmation`, the
harness downgrades it to `UNVERIFIED` and records an intervention.

**5. Why pre-filter feasibility *before* the agent sees options?**
So the model chooses among valid options instead of being asked to compute feasibility (which it does
badly — an early stress run had dispatch choose a 60-minute slot for a 90-minute unload). The pre-filter
also shrinks the prompt and makes the guard trivial: "is the selected ID in this list?"

**6. Why `ungrounded_evidence`?**
To catch hallucinated IDs, clock times and numbers in the model's cited evidence. Anything cited that is
not in the input (with list lengths and slot durations allowed as derived facts) is flagged and costs 10% in
the evals. Live run: 1 flagged answer — and it was a false positive (see Q30).

## B. Slot engine and concurrency

**7. Why a hold at all? Why not book on "yes"?**
The brief asks what "available" means while another driver decides (D5). Showing an option reserves
nothing (S1 proves 0 holds after an options-only chat). When the driver picks one, the engine places a
short `HELD` claim so a second truck is not offered the same slot during the warehouse round-trip. States:
OPTION → HELD → REQUESTED (driver said yes) → CONFIRMED (facility accepted), with RELEASED/EXPIRED exits.

**8. Why a 10-minute hold TTL?**
Long enough for a driver to read a WhatsApp and reply YES; short enough that an unanswered offer does not
block capacity during a 30-minute disruption spike (20–35 messages). At 10 minutes at most ~3 offer cycles
fit in a spike, and an abandoned hold blocks a slot for at most 10 minutes. Coordinator-driven holds in
S4/S5 use 30 minutes because a human decision takes longer. It is a parameter (`HOLD_TTL_MIN`) to tune
with real reply-time data.

**9. Why does REQUESTED have no expiry?**
Once the driver has said yes, the ball is in the facility's court; expiring it silently would let another
truck take a slot the driver believes is coming. Instead it stays until the facility confirms (re-check) or
it is released explicitly. A production version would add a facility SLA timer that escalates, not expires.

**10. Why re-check feasibility at confirm?**
Because the world changes between offer and confirmation (S5: a dock breaks, a hold expires and another
truck takes the slot). `confirm()` re-runs the full `check()` inside `BEGIN IMMEDIATE`; if the slot is no
longer feasible it releases the hold and returns the reasons instead of committing a stale option. `hold()`
also re-checks. `request()` checks that the hold is still live (not expired/released) — that is how S5's
late "YES" was caught as `STALE` (the brief's "options later found infeasible" measure = 1).

**11. Why `BEGIN IMMEDIATE` instead of optimistic locking?**
SQLite has a single writer. `BEGIN IMMEDIATE` takes the write lock up front, so check-then-insert is
atomic: no other connection can insert a hold between our feasibility check and our insert. Optimistic
locking (version columns + retry on conflict) is the right choice on Postgres with many writers, but in
SQLite it adds complexity without adding concurrency. Contention is tiny (≤35 messages per 30 min), so
serialising writes costs nothing. Losers get "database is locked" and retry (the race does 50×10 ms).

**12. Why also a partial unique index on holds?**
Defence in depth. `ux_live_hold_per_slot` (unique `slot_id` where state is HELD/REQUESTED/CONFIRMED) means
that even a buggy code path that skips the check cannot create two live claims; the insert fails with an
IntegrityError and is rolled back. Same pattern as the seed's `ux_active_appointment_per_slot`.

**13. What do the seed's two partial unique indexes NOT cover? (the brief asks this)**
They stop two active appointments on one slot and two current active appointments per shipment. They do not
cover: (a) weight/dock-type/reefer compatibility (APT1014A 21,000 kg pending on D1, limit 20,000; APT1005
20,500 kg on D3); (b) unloads longer than the slot spilling into the next slot (APT1002 70 min,
APT1005/1009/1014A 75 min, in 60-min slots); (c) dock events — slots overlapping a breakdown can still be
booked; (d) tentative claims — nothing represents "being offered/held"; (e) cross-slot time overlap;
(f) operating rules such as RULE005.

**14. How did you prove concurrency safety (D13)?**
`race_for_slot`: a file-backed DB, 3 threads with their own connections, a `threading.Barrier` so they fire
together, each trying to hold SLOT-JAI-020. Result: 1 WON, 2 REJECTED with the reason
"SLOT-JAI-020 taken (hold_held SHP1017)", 1 live hold afterwards. A unit test repeats it.

**15. How are long unloads handled?**
A 75-minute unload in 60-minute slots needs the following slot on the same dock. `check()` builds a chain
of consecutive slots and `hold()` claims all of them under one group key; `overrun_claims()` marks slots
implicitly used by existing long bookings, so SHP1009's D4 11:00 also blocks D4 12:00.

**16. Why idempotency keys?**
WhatsApp retries, duplicate webhooks and replayed cancellations are normal. `processed_messages` stores a
key per message/request; `request()` returns `DUPLICATE` for a key it has seen. S5: retried "YES" (wa-9014)
→ DUPLICATE; replayed APT1008 cancellation → no-op; THR009 duplicate of THR001 → no second hold, no second
coordinator alert.

## C. Policy decisions

**17. Why that priority order (D7)?**
1) CRITICAL > HIGH > NORMAL > LOW — business priority is in the data and is the customer's own ranking.
2) Same priority: fewest feasible options first — the most constrained truck goes first so flexible trucks
still fit (classic most-constrained-variable heuristic). 3) LOW-confidence ETAs are not held. 4) Then
earliest report — fairness, and it cannot be gamed by sending more messages. Ties between docks go to the
option that leaves the others the most options. Result: 0 priority-policy violations in S4.

**18. Why not first-come-first-served?**
It rewards whoever texts first, not what the business needs, and it lets a NORMAL truck take the only slot a
CRITICAL truck can use. FCFS is only the last tie-breaker.

**19. Why are LOW-confidence ETAs not held?**
"Late by one hour" or "between 12:30 and 13:00" has no reliable base time. Holding a slot for it blocks a
truck with a firm ETA on a guess. We ask one short question first (S2: THR003, clarification turn 1) and keep
the existing pending appointment until the driver gives a clock time.

**20. Why a +15-minute buffer for MEDIUM confidence?**
A hedged clock time ("around 12:45") or an ETA that moved >30 min since the last report is plausible but
drifting. 15 minutes is a quarter of a standard slot: it moves the truck off a slot it would likely miss
(S2: SHP1013 at 11:00 → planning 11:15 makes the pending 11:00 unsafe) without pushing it a whole hour. It is
a tunable assumption; real data on ETA error by source would replace it with a per-carrier buffer (the
sandbox already uses the mean of a driver's past claim errors, capped at 60 min).

**21. Why not trust drivers? Isn't that insulting?**
It is not about honesty — there is no GPS, so a driver's ETA is the only signal and it is often optimistic.
We act on it for *planning* (we re-slot on it), but we do not let it *verify* facts that affect other trucks:
"I'm at the gate" is checked against the gate log, "D2 is broken" against the facility. S8: a gate-backed
claim was accepted, an arrival claim with no check-in was contradicted, an outage claim was denied by the
facility — and no state changed falsely.

**22. Why no penalties for false claims?**
The brief puts penalties with humans, and we cannot prove intent: a driver may be at the wrong gate, or the
gate log may lag. Recording the claim as UNVERIFIED/contradicted is auditable; repeated contradictions go
to a coordinator digest, not to an automatic sanction. Penalising on model output would damage trust with
carriers and invite disputes.

**23. When do you escalate instead of acting automatically (D12)?**
No feasible slot (S6), a fix that needs someone else's booking to move or an overlap approval (S4, S5),
RULE005 exceptions (starts at/after 21:00), contradictory data, safety/regulated loads, emergencies,
penalties. Every hand-off carries 13 fields (case, shipment, driver, facility, reason, constraints, options
checked, blocking rules, recommended actions, deadline, owner, channel, delivery status) — 3 escalations,
100% complete.

**24. How do you scale to 35 messages in 30 minutes with 5 coordinators?**
(1) Suppress exact duplicates before any agent call (7 suppressed in the sandbox). (2) Resolve routine cases
automatically — 62% of the brief scenarios finished without manual takeover. (3) `review_priority` scores
each alert (type weight + 10/affected load + priority weight + 20 if impact ≤60 min); below 40 goes to a
digest, repeats of a pending alert are merged. (4) `CoordinatorQueue` simulates 5 coordinators × 15 min per
review: the sandbox peaked at 2 pending items with a 7.1-minute mean wait. Capacity: 5 × 2 reviews per
30 min = 10 reviews, so we need ≥70% auto-resolution or merging in a 35-message spike.

**25. Why does the schedule recalculate only on certain triggers (D8)?**
Recalculating on every chat message makes the plan churn and drivers get contradictory instructions.
Triggers are: gate check-in, facility WIP update, verified dock event, confirmed ETA change ≥15 min.
Unverified driver messages do not trigger it. S7: gate-in of SHP1004 triggers a resequence that cuts the
average gate-in→dock-in wait from 83.3 to 78.3 min, and it still needs a facility OK before any truck moves.

## D. Technology choices

**26. Why Python standard library only?**
Reproducible anywhere with `python3` (sqlite3, threading, urllib, json are built in): no dependency drift,
no install step in an interview or a customer's locked-down laptop, nothing to audit. The brief asks for
library versions: Python 3.15.2, SQLite 3.54.1, no third-party packages. Charts are hand-rolled SVG.

**27. Why SQLite and not Postgres?**
The challenge ships a SQLite snapshot, and SQLite is enough to prove the logic, including real multi-
connection concurrency. Production would move to Postgres: `SELECT … FOR UPDATE` or serializable
transactions, the same partial unique indexes, and an outbox for messages.

**28. Why NVIDIA Nemotron?**
It was the provided endpoint; it is OpenAI-compatible (swap model or URL with `NVIDIA_MODEL_ID`/
`NVIDIA_API_URL`, no code change); it supports JSON-object output mode; and it is open-weight, so it can be
self-hosted (NIM) in-region for data residency. Downsides seen: ~21.8 s mean latency per call and a habit
of over-claiming VERIFIED — both handled by the architecture, not by the model.

**29. What went wrong first and how did you fix it?**
The first NVIDIA eval scored **10/100**: answers were not parseable — prose around the JSON, `<think>`
reasoning blocks, code fences, and answers truncated when the model spent its budget reasoning. Fixes:
`response_format: json_object`, `max_tokens` 4096, strip think-blocks and fences, schema validation with
explicit "truncated/no answer" errors (scored zero, never guessed), and 3 retries with backoff on HTTP
429/5xx and network errors. A 6-case stress suite went 55 → 100/100 after schema and guard fixes (schema and
rubric changed too, so not strictly apples-to-apples).

## E. Evaluation

**30. Why those scoring weights?**
*Agent evals*: 40% safety (valid answer and no guard intervention) + 60% correct decision, −10% if evidence
is ungrounded. Safety alone should not pass a case, but an unsafe-but-right answer still gets 60% because the
guard caught it. *Sandbox*: 50% decisions + 30% five safety invariants + 20% on-plan arrivals.
*Brief run "workflow confidence"*: 50% agent decision accuracy + 30% safety (1 if zero conflicting
allocations, else 0) + 20% escalation completeness → 0.5×85.2 + 30 + 0.2×100 = **92.6**. They are rubric
metrics, not probabilities, and I say that up front. Caveat: guard interventions do not lower the 92.6
because decisions are judged after the guard — they are reported separately (13).
The one ungrounded flag ('9:20') was a false positive: `json.dumps` escapes the en-dash in "08:00–09:20" to
`\u2013`, so the time is preceded by a digit and the regex skips it in the source.

**31. Why scripted scenarios when you already had a live sandbox?**
The 15-minute sandbox (score 80.7, safety 5/5, decisions 71% of 35, on-plan arrivals 75% of 12) used random
personas, so it repeated the same situation types (late ETA, duplicate, false arrival) and never touched
most of the brief's ladder. `brief_scenarios.py` replays one distinct scenario per brief problem from the
real seed rows (S1–S8), covers every ladder level L1–L7, run item R1–R7 and decision D1–D13, and the agents
are still live. Sandbox = robustness under noise; scenarios = coverage and a reproducible demo.

**32. What were the 4 misses, and what did you change?**
All four were Triage asking the FACILITY whether a dock/slot was available (S2 THR003 reply, S5 OM003, S5
THR001, S6 THR005). Availability is the scheduler's job, and asking a human creates load and latency. The
triage prompt now says "Slot and dock availability is decided by the deterministic scheduler… do not ask
anyone whether a dock or slot is free" and restricts FACILITY questions to confirming outage/arrival claims.
The rerun is pending; I would quote it only after it lands, with `--repeats 3` for consistency.

**33. How do you know it's not just overfitting to your own expectations?**
Expectations are written from the brief's policy, not from the model's output, and several accept more than
one action where the policy allows it. The offline stand-in scores 100% by construction — it only proves
plumbing. The honest numbers are the live ones, and `--repeats N` reports per-call consistency.

## F. Limitations, risks, next steps

**34. Main limitations?**
Single facility (FAC-JAI-01) and a single day in minutes since midnight (no midnight crossing). Driver and
facility replies in S1–S8 are scripted; only agent decisions are live. Hold expiry is lazy (evaluated on
read), not a timer. `request()` reads the hold before taking the write lock (a small window; production
would move the read inside the transaction). Releasing a chat-confirmed appointment does not yet free its
CONFIRMED hold row (a `release_hold`/`release_confirmed_holds` fix was in progress, uncommitted). Latency
~22 s per agent call. Coordinator decisions are simulated.

**35. Failure modes and how each is handled?**
Model down/timeout → retries, then a safe fallback answer: ESCALATE to coordinator (logged as a guard
intervention). Malformed/truncated output → error, scored zero, never guessed. Hallucinated ID → guard nulls
it. Over-claimed verification → downgraded. DB lock → retry. Duplicate/retry message → idempotency key.
Stale option → STALE at request, re-offer. Failed email (OM004) → retry via another channel (phone to the
shift manager) and record delivery status. Prompt injection inside a message → payload is "untrusted data".

**36. Privacy?**
Agents get IDs, order references, weights, times and dock codes — no names, phones, emails or licence
numbers (the sandbox masks registrations). Hand-off records with phone numbers stay local. The key lives in
`.env` (git-ignored), is never logged, and existing env vars take precedence. For production: self-host the
model in-region, keep data processing agreements in place under India's DPDP Act, and log prompts with PII
redaction.

**37. What would you do next?**
Rerun live with the new triage prompt (`--repeats 3`); move to Postgres with an outbox and real timers for
hold/SLA expiry; per-carrier ETA error model instead of a fixed +15; a facility SLA on REQUESTED;
multi-facility (Gurugram); a cheaper/faster model for triage with the large model as fallback; shadow-mode
pilot at Jaipur for 2 weeks measuring auto-resolution, coordinator load and dock-in waits before any
automatic confirmation.
