# SetuHaul driver-exception service: decision register (D1-D13)

The 13 required decisions come from the brief (`docs/brief_checklist.md`). Every example below comes from one of
three sources:

- **Run**: the committed live scenario run `brief_scenarios.json`. It used model `nvidia/nemotron-3-ultra-550b-a55b`
  in `live` mode across scenarios S1-S8: 27 agent calls, 85.2% of decisions as expected, 0 agent errors, 13 guard
  interventions, 0 conflicting allocations, 1 option caught as stale at re-check, 3 escalations with 100%
  completeness and 0 priority violations.
- **Engine**: `slot_engine.py` run directly against a fresh in-memory seed through
  `python3 -c "from slot_engine import *; ..."`.
- **DB**: `setuhaul_freight_operations.db`.

Times are 4 Aug 2026 in Asia/Kolkata (+05:30).

**Operating principle:** the LLM agents (triage, dispatch and assessment) only advise. Code decides which slots are
feasible (`SlotEngine.check`), which selections are allowed (`guards.guard_answer`), and every state change
(`SlotEngine.hold/request/confirm`). `state_change_allowed` is always forced to `false`.

---

## D1: Fields collected before options are shown

**Decision:** Collect five facts before running a slot search or showing options: `shipment_id`, `eta`,
`eta_confidence`, `earliest_start` and `latest_finish`.

**Options considered:**
1. Search with whatever the message contains.
2. Collect the full list (`REQUIRED_FIELDS_BEFORE_OPTIONS`) first.
3. Always run a full intake questionnaire.

**Choice:** Option 2. Ask only for missing facts, with one short targeted question per turn, and withhold the
tool call until the facts are known. `earliest_start` and `latest_finish` default to the ETA and to "no limit"
when the driver states them or says there is no constraint.

**Why:** If the search runs on a vague ETA, the options it returns may be infeasible, and the driver gets a
promise that has to be withdrawn later. A full questionnaire would overload drivers, who send 15-25 messages on a
normal day. Asking one question per missing fact kept clarification to 2 turns across the whole run.

**Evidence (three example chats from the run):**

| Chat | Driver says | Missing | Agent/engine response | Result |
|---|---|---|---|---|
| THR011 / DRV001 (S1, 10:02) | "What slots are possible after 1 PM if I reach around 12:45?" | Nothing. Shipment linked to SHP1017. ETA 12:45 is MEDIUM, so the planning ETA is 13:00. `not_before` is 13:00. | Triage `NO_ACTION [NOT_APPLICABLE]`. Dispatch `PROPOSE SLOT-JAI-006`. The agent replies with options only: "1) D1 13:00 (unload done 13:50); 2) D2 13:00 ...; 3) D3 13:00 ...". | 0 holds; APT1017 is still CONFIRMED on SLOT-JAI-005. |
| THR003 / DRV013 (S2, 09:19) | "I am late by one hour." | `eta` (relative delay) and `latest_finish` | Engine: "No slot search run: required fields missing (tool call withheld)." Triage `CLARIFY, ask DRIVER`. Agent: "Does one hour mean you will reach the gate at 11:00, or could the delay grow? And is there a time by which you must leave?" | 09:25 "Reaching gate 11:00 for sure, no hurry after that." The search then runs on a planning ETA of 11:15. |
| THR010 / DRV004 (S2, 09:31) | "I will be late by 45 minutes." | `shipment_id` (driver has two loads) and `eta` | Triage `CLARIFY, ask DRIVER`, Q: "Which shipment are you referring to, SHP1004 ... or SHP1020 ...?" Agent: "Is this ORD-260804-004 (you're already at the gate) or the evening ORD-260804-020?" | 09:35 "The evening one, 020. Gate by 18:45." The search runs on SHP1020. |

**Where in code:**
- `brief_scenarios.REQUIRED_FIELDS_BEFORE_OPTIONS`
- `brief_scenarios.s1_options_only` and `s2_missing_facts`
- Triage prompt: `nvidia_agents.SYSTEM_PROMPTS[AgentRole.TRIAGE]` ("CLARIFY when the shipment or time is missing, vague, or contradictory")

**Risks / what would change it:**
- The field gate is enforced by the scenario flow, not by a single reusable `intake()` function. In production
  it should become a hard pre-condition on the dispatch tool.
- If ETA telematics existed (there is no live GPS here), `eta` could be filled in automatically and the
  question skipped.

---

## D2: Linking a conversation to driver, shipment and appointment

**Decision:** Resolve each message through this chain:
1. `chat_messages.thread_id` to `chat_threads`
2. The thread's `driver_id` and `shipment_id`
3. `appointments` where `is_current=1`
4. `appointment_slots`

If the thread has no `shipment_id`, list the driver's shipments for the day. Never guess between them.

**Options considered:**
1. Trust the thread's shipment, or else take the driver's nearest load.
2. Let the LLM pick the most likely load.
3. Use deterministic lookup, and ask the driver when there are several candidates.

**Choice:** Option 3. Show order references to the driver, because drivers know orders, not internal shipment
IDs.

**Why:** A wrong link is a silent failure: it re-books the wrong truck. A clarifying question costs one turn.
DRV004's two loads are 9 hours apart and one truck is already in the yard, so a guess could move the wrong
booking.

**Evidence (message-to-rows trace from the DB and the run):**

```
MSG016 (THR010, DRIVER DRV004, wa-9010, 09:31, "I will be late by 45 minutes.", requires_human_review=1)
 -> THR010 (driver_id DRV004, shipment_id NULL, status WAITING_FOR_DRIVER, intent UNKNOWN)
 -> EXC010 (shipment NULL, reported_delay_min 45, NEEDS_INFORMATION, dedupe_key DRV004-UNKNOWN-20260804-0931)
 -> candidates for DRV004: SHP1004 / ORD-260804-004 (WAITING, CHK1004 gate-in 09:25, APT1004 on SLOT-JAI-016 D2 09:00)
                           SHP1020 / ORD-260804-020 (ASSIGNED, APT1020 on SLOT-JAI-025 D2 18:00-19:00)
 -> ask (MSG017 in seed; run: "Is this ORD-260804-004 ... or the evening ORD-260804-020?")
 -> reply "The evening one, 020. Gate by 18:45."
 -> SHP1020 -> APT1020 (CONFIRMED, is_current=1) -> SLOT-JAI-025 (DOCK-JAI-D2, 18:00-19:00, OPEN)
 -> replaced by APT-NEW-1020-583 on SLOT-JAI-026 (D2 19:00), warehouse ref WH-JAI-9120, replaced_appointment_id=APT1020
```

The run logs this line verbatim:

> "Trace: MSG016 → THR010 → DRV004 → SHP1020 → APT1020 (SLOT-JAI-025 D2 18:00)."

**Where in code:**
- `brief_scenarios.s2_missing_facts` (candidate list and trace)
- `brief_scenarios.shipment_card` (current appointment join)
- `SlotEngine.confirm` (writes `replaced_appointment_id`)

**Risks / what would change it:**
- Linking is written per scenario, not as a library function.
- A driver who sends a wrong order number would be linked to that order. The driver-to-shipment ownership check
  (`shipments.driver_id`) must stay in place.
- If threads were always opened with a shipment ID by dispatch, the clarification turn could be skipped.

---

## D3: Revised ETA and confidence

**Decision:** Convert each driver message into an ETA, a confidence and a planning ETA (the ETA the scheduler
plans against) with this deterministic rule:

| Message contains | Confidence | Planning ETA |
|---|---|---|
| An explicit clock time consistent with the last report (moved 30 min or less) | HIGH | The ETA |
| A hedged clock time ("around", "about", "maybe") | MEDIUM | ETA + 15 min |
| A clock time that moved more than 30 min since the last report | MEDIUM | ETA + 15 min |
| A relative delay ("late by one hour") or a range | LOW | None; ask before any slot search |
| No time | NONE | None; ask |

The rule treats an hour below 7 without "pm" as afternoon or evening.

**Options considered:**
1. Take the driver's time at face value.
2. Let the LLM estimate the ETA.
3. Use a deterministic rule with confidence and a buffer.

**Choice:** Option 3: `brief_scenarios.revise_eta`.

**Why:** Drivers under-report delay, and there is no GPS to verify a report. A 15-minute buffer on large moves
avoids offering slots the truck will miss. LOW-confidence ETAs are never planned on. This matches
`apply_view_fixes`, under which `v_latest_eta` ignores LOW-confidence driver ETAs.

**Evidence (rule applied to real messages; output verified with `revise_eta`):**

| Message | History | Output | Consequence in run |
|---|---|---|---|
| THR003 "I am late by one hour." | plan 10:00 | `LOW`, planning_eta None: "relative delay or range: ambiguous base time -> LOW, ask before any slot search" | No search; driver asked (D1). |
| THR003 "Reaching gate 11:00 for sure, no hurry after that." | plan 10:00 | `MEDIUM`, eta 660 (11:00), planning_eta 675 (11:15): "clock time 11:00 moved 60 min since last report -> MEDIUM, +15 min buffer" | Pending APT1013A (D2 11:00) found unsafe: "starts 11:00 before ETA 11:15"; OM002 (QUEUED) must not be sent as is. Re-planned to SLOT-JAI-019 (D2 12:00). |
| THR005 "Evening traffic. ETA 6:30. Can the reefer unload tonight?" | plan 17:30 | `MEDIUM`, eta 18:30 (the "6" is read as an evening hour), planning_eta 18:45 | Feeds D4/D10 for SHP1015. |
| THR011 "...if I reach around 12:45?" | 12:15 | `MEDIUM` hedged, planning_eta 13:00 | Only 13:00 options were shown. |

**Where in code:**
- `brief_scenarios.revise_eta`
- `workflow_simulation.apply_view_fixes` (`v_latest_eta` ignores LOW-confidence ETAs)

**Risks / what would change it:**
- Regex parsing misses phrasings such as "half past 11" or "in 40 mins".
- The rule reads any hour below 7 as afternoon or evening, which would misread a genuine early-morning ETA.
- "For sure" is not treated as more reliable. Confidence comes from how far the ETA moved, not from the
  driver's wording.
- If telematics or check-in history showed a driver's systematic bias, the buffer should be set per carrier or
  driver instead of a flat 15 minutes.

---

## D4: Slot feasibility

**Decision:** A slot start is feasible for a shipment only when `SlotEngine.check` returns no reasons. Each
candidate includes the following slots on the same dock when the unload is longer than one slot. The written
check covers:
- Dock type
- RULE003: a reefer load needs D5
- RULE004: a load over 25,000 kg needs the heavy dock
- The dock weight limit
- The slot starts at or after the planning ETA, and has not already started
- The driver's `not_before` and `finish_by` times
- RULE005: no new start at or after 21:00
- A following slot exists for a long unload
- `slot_status=OPEN`
- No hard dock event (BREAKDOWN or MAINTENANCE) overlapping the slot
- No other active appointment or live hold
- No implicit overrun claim from another booked long unload

**Options considered:**
1. Use the seed view `v_slot_availability` alone.
2. Let the LLM judge compatibility.
3. Use an explicit, reason-returning check that covers multi-slot unloads and overruns.

**Choice:** Option 3. The agent only ever sees the IDs that pass (`deterministic_feasibility.feasible_ids`), and
`guard_answer` blocks any other selection.

**Why:**
- The seed view ignores dock events (patched at runtime) and implicit overruns.
- 75-minute unloads in 60-minute slots overrun into the next slot: APT1002 into SLOT-JAI-016, APT1005 into 031,
  APT1009 into 047, and APT1014A into 005.
- Returning reasons makes refusals explainable (D11).

**Evidence (check run on three shipments; engine output, consistent with S3 in the run):**

| Shipment | Load | ETA / now | Result per dock (first start at or after ETA) | First feasible |
|---|---|---|---|---|
| SHP1010 | 16,000 kg, REEFER, 55 min, HIGH | 10:55 / 09:38 | D1 11:00: "needs REEFER dock, D1 is STANDARD", "RULE003 reefer load needs D5", "SLOT-JAI-004 taken (pending_confirmation SHP1014)"; D3 11:00: "SLOT-JAI-032 blocked: Hydraulic leveller breakdown"; D4 11:00: "SLOT-JAI-046 taken (confirmed SHP1009)"; D6 11:00: "needs REEFER dock, D6 is HEAVY"; **D5 11:00 (SLOT-JAI-060): no reasons** | **D5 11:00** (assessment agent: `PROPOSE DOCK-JAI-D5`) |
| SHP1016 | 31,000 kg, HEAVY, 90 min, HIGH | 12:00 / 09:39 | D2 12:00: "needs HEAVY dock ...", "RULE004 31000 kg > 25000 kg needs heavy dock", "31000 kg > D2 limit 25000 kg"; D4 12:00 also "SLOT-JAI-047 needed by APT1009 (SHP1009) long unload"; D5 12:00: "31000 kg > D5 limit 22000 kg"; **D6 12:30 (SLOT-JAI-074): no reasons** | **D6 12:30** (dispatch: `PROPOSE SLOT-JAI-074`) |
| SHP1015 | 15,000 kg, REEFER, 60 min, HIGH | 18:30 (planning 18:45) / 16:24 | D1-D4, D6: "needs REEFER dock ...", "RULE003 reefer load needs D5"; D5 18:00: "starts 18:00 before ETA 18:45", "SLOT-JAI-067 blocked: Refrigeration power maintenance"; D5 19:00/20:00: "SLOT-JAI-068/069 blocked: Refrigeration power maintenance"; D5 21:00: "RULE005 no new start at/after 21:00 without approval" + blocked | **NONE** (assessment: `NO_FEASIBLE_OPTION`, escalated in D10) |

The check also confirms that RULE005 rejects any 21:00 start: SLOT-JAI-014 (D1) and SLOT-JAI-028 (D2) both return
"RULE005 no new start at/after 21:00 without approval".

**Where in code:**
- `slot_engine.SlotEngine.check`, `feasible` and `overrun_claims`
- `guards.feasible_option_ids` (assessment pre-filter)
- `guards.guard_answer`
- `brief_scenarios.s3_rules`

**Risks / what would change it:**
- `CAPACITY_REDUCTION` events (DEVT003 on D2, 08:00-09:20) are not treated as blocking. Only BREAKDOWN and
  MAINTENANCE block a slot.
- There is no turnover buffer between trucks.
- Vehicle and dock fit beyond weight is not modelled (for example, vehicle length).
- A facility that allows long unloads to straddle a dock change would need the chain logic relaxed.

---

## D5: What "available" means while another driver decides

**Decision:** A slot is *available* only when it passes D4 **and** has no live claim. Live claims are:
- An active appointment: `CONFIRMED`, `PENDING_CONFIRMATION` or `IN_PROGRESS`
- A `slot_holds` row in `HELD`, `REQUESTED` or `CONFIRMED`
- An overrun claim from a booked long unload

Options that are only *shown* place no claim.

**Options considered:**
1. First to confirm wins, with no holds.
2. Showing an option reserves it.
3. Use a short, explicit hold created only when the driver picks an option, and expire it automatically.

**Choice:** Option 3. Showing options is free. A pick creates a 10-minute `HELD` claim (`HOLD_TTL_MIN = 10`) that
other drivers see as "taken". It expires automatically.

**Why:**
- With option 1, two drivers could be told the same slot and one would lose it at confirmation.
- With option 2, every query would freeze capacity for 15-25 daily exceptions.
- A short hold balances fairness with utilisation: the 11:00-14:00 standard-dock utilisation in S4 was 0.7.

**Evidence:**
- Run S1: options were shown and compared, then "holds before=0, after=0; APT1017 still CONFIRMED on SLOT-JAI-005.
  Nothing was booked."
- Run S5, 09:34: "D5: D2 12:00 is HELD for SHP1005 (coordinator deciding) → not offered as available." SHP1006 was
  offered D1 13:00 instead.
- Engine (verified): after `hold('SHP1006', SLOT-JAI-019 D2 12:00)` at 09:34:
  - `check('SHP1013', ...)` for that slot returns `['SLOT-JAI-019 taken (hold_held SHP1006)']`
  - `board` shows `['HELD', 'SHP1006']`
  - After `expire_holds(09:44)`, 1 hold expired and the board shows `['FREE', '']`

**Where in code:**
- `SlotEngine.occupant`, `expire_holds`, `board` and `check` ("taken (...)" reasons)
- `HOLDS_SCHEMA` unique index `ux_live_hold_per_slot`

**Risks / what would change it:**
- 10 minutes is a guess. If drivers often reply later (for example, while driving), use a longer hold (S4 used
  `ttl=30` for coordinator-batched allocation) or a "notify me" queue.

---

## D6: Hold, request, reservation and confirmation states

**Decision:** Use this state machine:

```
OPTION (not persisted) -> HELD (10-min TTL) -> REQUESTED -> CONFIRMED
                            |                    |
                            +-> EXPIRED          +-> RELEASED
                            +-> RELEASED
```

- **HELD** reserves capacity for the driver's decision.
- **REQUESTED** is the driver's acceptance, sent to the warehouse. There is no TTL while the warehouse decides.
- **CONFIRMED** is written only after the warehouse accepts. It creates a `CONFIRMED` appointment and cancels the
  old one in the same transaction.

The check that runs at each step:

| Transition | What is re-checked |
|---|---|
| OPTION → HELD (`hold`) | `BEGIN IMMEDIATE`, expire holds, then a full `check`. If no longer feasible, roll back, increment `rechecks_failed` and return the reasons. Any earlier `HELD` of the same shipment is RELEASED. |
| HELD → REQUESTED (`request`) | Idempotency key is checked first (`DUPLICATE`). Expire holds, then require every row of the group to still be `HELD`. Otherwise return `STALE` ("hold is EXPIRED; option must be re-checked"). |
| REQUESTED → CONFIRMED (`confirm`) | `BEGIN IMMEDIATE` and a full `check` again. On failure, the group is RELEASED and the reasons are returned. On success, the old appointment is set to `CANCELLED`/`is_current=0`, the new appointment carries `replaced_appointment_id`, and the holds become `CONFIRMED`. |

**Options considered:**
1. Two states (proposed and booked).
2. Write directly into `appointments` as `PENDING_CONFIRMATION`.
3. Keep a separate hold ledger with explicit states and re-checks.

**Choice:** Option 3. A `slot_holds` table is added at runtime. The shipped schema is unchanged, and
`appointments` keeps the brief's history semantics.

**Why:**
- The brief requires a distinction between proposed, pending and confirmed actions.
- Re-checking at confirm catches the world changing in between (for example, a dock event or an expiry).
- A separate ledger keeps the seed's partial unique indexes intact (one active appointment per slot, one current
  appointment per shipment).

**Evidence (run S2, SHP1020):**

| Time | Event | Engine result |
|---|---|---|
| 09:35 | "HOLD SLOT-JAI-026 (D2 19:00) for 10 min" | `True` |
| 09:37 | Driver: "YES" | "REQUEST → REQUESTED; warehouse asked to confirm" |
| 09:43 | Warehouse: "accepted, ref WH-JAI-9120" | "CONFIRM after re-check → True: APT-NEW-1020-583 replaces APT1020" |

The other transitions also appear in the run:
- **EXPIRED** (S5): "09:44: SHP1006 hold expired (no reply): 1".
- **STALE** request (S5): see D9.
- `options_infeasible_at_recheck = 1` in the run measures.

**Where in code:**
- `slot_engine.SlotEngine.hold`, `request`, `confirm`, `expire_holds` and `release_appointment`
- `HOLDS_SCHEMA`

**Risks / what would change it:**
- `request` does not re-run the full `check`. It relies on the live hold's exclusivity, so a dock event logged
  between hold and request is caught only at `confirm`.
- REQUESTED has no TTL. A silent warehouse leaves the slot claimed until a human acts, so a warehouse SLA timer
  is needed.

---

## D7: Ordering of simultaneous requests

**Decision:** When several trucks contend for the same capacity, process them in this order:
1. Shipment priority (CRITICAL > HIGH > NORMAL > LOW).
2. Within the same priority, the truck with fewer feasible options goes first.
3. LOW-confidence ETAs are not held until they are clarified.
4. Then the earliest report.

Each truck gets its earliest feasible start. When several starts tie, choose the one that leaves the remaining
trucks the most options. Existing confirmed bookings that are still feasible are kept.

**Options considered:**
1. First come, first served.
2. Pure priority.
3. Minimise total lateness with a solver.
4. Priority, then scarcity, with a confidence gate.

**Choice:** Option 4. It is easy to explain to drivers and coordinators, and it can be checked mechanically for
violations.

**Why:**
- First come, first served rewards whoever messages first, not operational impact.
- A solver is opaque at 20-35 messages per 30 minutes and hard to audit.
- The confidence gate stops a vague ETA from blocking a sure one.

**Evidence (run S4, 09:45, competing trucks SHP1014, SHP1006, SHP1012, SHP1013 and SHP1009):**

| Truck | Priority | Load / unload | ETA (confidence) | Constraint | Feasible starts | Outcome |
|---|---|---|---|---|---|---|
| SHP1009 | CRITICAL | 20,000 kg / 75 min | 10:50 (HIGH) | Already CONFIRMED on D4 11:00 | — | **Keeps D4 11:00 + 12:00** (overrun claim) |
| SHP1014 | CRITICAL | 21,000 kg / 75 min | 11:25 (HIGH) | APT1014A on D1 was infeasible (20,000 kg limit) and released | 15 | **HELD D2 12:00 (SLOT-JAI-019+020)** (dispatch `PROPOSE SLOT-JAI-019`) |
| SHP1006 | HIGH | 7,800 kg / 45 min | 11:20 (HIGH) | After 12:00; APT1006 released (will be missed) | 32 | **HELD D1 13:00 (SLOT-JAI-006)** |
| SHP1012 | NORMAL | 8,000 kg / 45 min | 11:10 (HIGH) | Must finish by 13:30 | 1 (only D2 12:00, already taken by CRITICAL) | Dispatch `NO_FEASIBLE_OPTION`, then **ESC-SHP1012** to the coordinator |
| SHP1013 | NORMAL | 14,000 kg / 60 min | "late by one hour" (LOW) | Pending APT1013A | — | **No hold**: "LOW-confidence ETA → keep pending APT1013A, no new hold; clarify first." |

- Processing order logged: "SHP1014 → SHP1006 → SHP1012 → SHP1013".
- Run measures: `priority_violations = 0`, conflicting allocations 0, `utilisation_11_14_standard = 0.7`.
- ESC-SHP1012 recommends two alternatives for a human to approve:
  - "Offer D3 13:00 (unload ends 13:45, 15 min past driver limit)"
  - "Or move CRITICAL SHP1014 from D2 12:00 to D4 13:00 to free D2 12:00 — needs approval"

**Where in code:**
- `brief_scenarios.s4_competition` (sort key and `remaining()` tie-break)
- `slot_engine.PRIORITY_RANK`

**Risks / what would change it:**
- Priority beat scarcity here. SHP1012's only slot went to SHP1014, which had 15 alternatives, so a NORMAL truck
  was escalated. A swap search with a lateness bound (option 3) would find the D4 13:00 move automatically; today
  a human makes that call.
- The policy runs in batches. Under live arrival it should run at each D8 trigger, not on every message.

---

## D8 (optional): When the schedule recalculates

**Decision:** Recalculate the facility sequence on any of these triggers:
- A gate check-in
- A facility work-in-progress (WIP) update
- A verified dock event
- A confirmed ETA change of at least 15 minutes

An unverified driver message does **not** trigger a recalculation. A proposed sequence needs the facility's OK
before any truck moves.

**Options considered:**
1. Recalculate on every message.
2. Recalculate on a fixed timer.
3. Recalculate on verified, event-driven triggers.

**Choice:** Option 3.

**Why:**
- Recalculating on every message churns the plan and lets an exaggerated claim reorder other drivers.
- A timer is late for gate events.
- Verified triggers are rare, about 5 check-ins plus a few events per site per day, and each one changes the
  facts.

**Evidence (run S7):**
- 09:10, facility WIP update: SHP1002 is still unloading on D2 and expected to finish around 09:45. The engine
  notes that "APT1002's overrun covers SLOT-JAI-016 (SHP1004's D2 09:00)".
- 09:25, trigger CHK1004: SHP1004 checks in, 25 minutes after its slot but within RULE002's 30-minute grace.
- Proposed sequence:
  - SHP1003 docks at 08:50 on the idle D1 instead of waiting for 09:00.
  - SHP1004 keeps its D2 booking and is called in at 09:55, after SHP1002 clears plus 10 minutes turnover.
  - Average gate-in to dock-in time falls from **83.3 to 78.3 min**.
- 09:26, the yard supervisor agrees.
- Triage on THR008 returns `NO_ACTION [VERIFIED]` ("arrival in yard confirmed by gate check-in CHK1004 at 09:25").

**Where in code:**
- `brief_scenarios.s7_resequence` (trigger list and before/after plans)

**Risks / what would change it:**
- The S7 sequence is scripted, not produced by a general resequencer. A production version needs a yard-queue
  optimiser that uses `v_current_facility_queue`.

---

## D9: Stale options, cancellations, duplicates and retries

**Decision:**
- Every state change is re-checked (D6) and is idempotent.
- Driver replies carry an idempotency key (the WhatsApp message ID) that is recorded in `processed_messages`.
- Cancellation replays are keyed `cancel:<appointment_id>`.
- Duplicate reports are detected by the exception `dedupe_key` (driver, shipment, date and time), so they create
  no second hold and no second alert.
- A stale option is never booked. The driver is told why and gets a freshly checked alternative.

**Options considered:**
1. Last write wins.
2. Rely on the LLM to notice repeats.
3. Use deterministic idempotency keys plus a re-check.

**Choice:** Option 3. The LLM's `DUPLICATE` label is advisory only.

**Why:** WhatsApp retries and resends are routine. Double-processing would double-book or double-alert
coordinators, and the brief warns against duplicate low-value alerts.

**Evidence (one replayed case each, from run S5):**

| Case | Replay | Result |
|---|---|---|
| **Stale option** | 09:34: SHP1006 shown D1 13:00 (SLOT-JAI-006), held until 09:44. No reply, so EXPIRED at 09:44. At 09:46 SHP1013 holds the same slot and requests it at 09:47. At 09:52 the driver replies `[wa-9013] "YES book D1 13:00"`. | `request` → **STALE** ("hold is EXPIRED; option must be re-checked"). Re-dispatch → `PROPOSE SLOT-JAI-034` (D3 13:00), held, then requested with wa-9014 and confirmed at 09:59 as **APT-NEW-1006-599**, ref WH-JAI-9106. |
| **Cancellation replay APT1008** | APT1008 (SHP1008, SLOT-JAI-017 D2 10:00) is CANCELLED 08:20 "Customer cancelled receipt", and the cancellation is applied twice. | `remember('cancel:APT1008')`: first **True**, replay **False**, so it is applied once. SLOT-JAI-017 is free, but too short for SHP1005's 75-minute unload before D2 11:00. |
| **Duplicate THR009** | MSG015 (THR009, wa-9009, 09:35:02, `is_duplicate=1`) has the same text as MSG001 (THR001, wa-9001, 09:34:00). EXC009 is `DUPLICATE` with dedupe_key `DRV006-SHP1006-20260804-0934`, the same as EXC001. | Triage → **DUPLICATE**. The guard nulled the hallucinated `selected_id wa-9009`. Engine: "Dedupe key ... already processed → no second hold, no second alert." |
| **Retry wa-9014** | At 09:53 the driver's "YES" is delivered twice with the same `wa-9014`. | "First YES → REQUESTED; retried YES → DUPLICATE (idempotent, one request)." |

The engine confirms the retry and stale paths:
- A second `request(..., request_key='wa-9013')` returns `('DUPLICATE', 'same request already processed')`.
- `conflicts()` returns `{slots_with_two_live_claims: 0, shipments_with_two_current: 0, foreign_key_errors: 0}`.

**Where in code:**
- `SlotEngine.seen`, `remember`, `request` (STALE/DUPLICATE) and `conflicts`
- `brief_scenarios.s5_world_changes`

**Risks / what would change it:**
- The THR009 duplicate has a *different* external ID (wa-9009 vs wa-9001), so `processed_messages` alone would not
  catch it. Detection depends on the `driver_exceptions.dedupe_key` comparison, which the scenario passes to
  triage, and on the triage label. The engine does not enforce it yet; it should become a unique key on intake.
- A dedupe key built from a minute-truncated timestamp can split or merge reports at minute boundaries.

---

## D10: No-slot escalation message and hand-off record

**Decision:** When no feasible slot exists:
- Send a hand-off record with all 13 `HANDOFF_FIELDS` to a named owner, through a channel that is known to work.
- Measure completeness.
- Tell the driver plainly what happens next and what to do meanwhile.
- Retry failed deliveries on another channel.

**Options considered:**
1. Free-text email to a shared inbox. The seed's OM004 did exactly this and **FAILED**.
2. Hand off to the coordinator queue only.
3. Send a structured record to the named on-shift owner, with a fallback channel.

**Choice:** Option 3.

**Why:**
- The data audit found that FAC-JAI-01 has no night-shift contact (CON005 is FAC-GGN-01 and vacant), and OM004 to
  `night.shift@setuhaul.example` FAILED.
- Recommended actions and a deadline let the human decide in one step.

**Evidence (run S6, 16:25, ESC-SHP1015, completeness 1.0, `missing_fields: []`):**

```json
{"case_id": "ESC-SHP1015", "shipment_id": "SHP1015", "driver_id": "DRV015", "facility_id": "FAC-JAI-01",
 "reason": "Reefer load ETA 18:30; only reefer dock D5 under maintenance 18:00–22:00 (DEVT002); RULE005 bars starts ≥21:00",
 "constraints": {"eta": "18:30", "temperature_control": true, "weight_kg": 15000, "unload_min": 60},
 "options_checked": 79,
 "blocking_rules": ["RULE003 reefer → D5", "DEVT002 D5 maintenance 18:00–22:00", "RULE005 no start ≥21:00"],
 "recommended_actions": ["Approve D5 22:00 start (RULE005 exception, reefer kept running)",
                         "Or hold truck with reefer unit on until 08:00 next day",
                         "Or divert to FAC-GGN-01 D3 (reefer) — needs customer approval"],
 "deadline": "17:30 (before D5 maintenance)", "owner": "Anita Joshi (SHIFT_MANAGER)",
 "channel": "PHONE +91-141-5001103 (email OM004 failed)", "delivery_status": "SENT"}
```

Driver message (THR005 / DRV015):

> "I can't find a safe reefer slot tonight: the only cold dock, D5, is in maintenance from 18:00 to 22:00. I've
> passed this to the Jaipur shift manager with your details — they'll call you before 17:30. Please keep the
> reefer unit running and don't come to the gate before you hear from them."

Two other hand-offs in the run were also 100% complete:
- ESC-SHP1012 (D7)
- ESC-SHP1005 (S5): the truck had been at the gate since 09:05 with D3 down, facing a 175-minute wait. The
  coordinator approved the held D2 12:00 at 09:45.

**Where in code:**
- `brief_scenarios.HANDOFF_FIELDS`
- `Scenario.escalate` (completeness score)
- `s6_no_slot`

**Risks / what would change it:**
- "D5 22:00" is off the slot grid: D5's last slot is 21:00-22:00 (SLOT-JAI-070). It needs an ad-hoc slot that a
  human creates.
- The phone hand-off is recorded as `SENT`, not acknowledged. Production needs acknowledgement tracking and a
  second-tier fallback (CON001/CON002).

---

## D11: Plain-language refusal messages

**Decision:** Every refusal must:
- State what cannot be done
- Give the concrete reason, quoting the rule or number
- Give the next safe option or next step
- Avoid internal IDs where the driver does not need them, and blame no one

**Options considered:**
1. Generic "not possible".
2. LLM-written refusals.
3. Templates filled from `SlotCheck.reasons`.

**Choice:** Option 3. The text is built from deterministic reasons, so the reason is always the true one.

**Why:** Drivers push back ("I'll be quick"). A specific reason plus an alternative ends the loop in one turn and
avoids promising anything that guards would block.

**Evidence (from the run):**
1. **Heavy load to a standard dock** (S3, THR006 / DRV007, 09:41). The driver said "D4 looks empty, put me there,
   I'll be quick." The reply:
   > "I can't book D4: your load is 31,000 kg and D4 takes up to 25,000 kg; site rule RULE004 sends anything over
   > 25,000 kg to the heavy dock. D6 12:30 is the earliest safe option."
2. **Stale option** (S5, THR001 / DRV006, 09:52, after "YES book D1 13:00"). The reply:
   > "Sorry — D1 13:00 was released when the 10-minute hold ran out and another truck has requested it. Next
   > option: D3 13:00, held until 10:02. Reply YES."

S8 adds two more: an unverified arrival claim ("The gate hasn't logged your truck yet, so I can't call you to a
dock ...") and an early arrival ("Arriving early doesn't move you ahead of trucks already booked or waiting ...").

**Where in code:**
- `brief_scenarios.s3_rules`, `s5_world_changes` and `s8_human_side`
- Reasons come from `SlotEngine.check` and `request` (STALE)

**Risks / what would change it:**
- The templates are English-only. Drivers may need Hindi or Rajasthani versions.
- Message length must stay short for WhatsApp.

---

## D12: Decisions needing human approval

**Decision:** The agent never alone decides capacity, vehicle-dock fit, ranking overrides or commit status
without the deterministic engine. Anything outside policy goes to a human with a trigger, and alerts are ranked
by `guards.review_priority`:

```
score = type weight (DOCK_OUTAGE 50, NO_FEASIBLE_SLOT 35, ARRIVAL_CLAIM 20, ETA_UPDATE 15)
      + 10 x affected loads
      + priority weight (CRITICAL 40, HIGH 25, NORMAL 10)
      + 20 if impact is within 60 min
```

`CoordinatorQueue` (5 coordinators) handles anything scoring 40 or more. Anything below 40 goes to a digest, and
alerts with the same key are merged.

| Decision needing a human | Trigger (deterministic) | Approver | Run example |
|---|---|---|---|
| Start at or after 21:00 (RULE005 exception) | `check` reason "RULE005 no new start at/after 21:00 without approval" | Shift manager | ESC-SHP1015 option "Approve D5 22:00 start" |
| No feasible slot | `feasible()` is empty | Coordinator / shift manager | ESC-SHP1015; ESC-SHP1012 (`review_priority('NO_FEASIBLE_SLOT', HIGH, ≤60 min)` = 90) |
| Moving a higher-priority truck to fit another | D7 leaves a truck unplaced, and the only fix displaces a CRITICAL or HIGH hold | Coordinator | ESC-SHP1012: "move CRITICAL SHP1014 ... — needs approval" |
| Long yard wait, or overlapping or overrun unloads | Earliest feasible start is more than 60 min after gate-in, or an overlap is needed | Coordinator | ESC-SHP1005: 175-min wait; "Approve 15-min overlap at D2 10:00"; approved 09:45 |
| Applying a dock outage | Only a driver reports it (no facility message, no dock event) | Facility contact | S8: "D2 is broken ..." is UNVERIFIED and asked of FACILITY; "D2 is working normally", so the outage is not applied |
| Re-sequencing the yard queue | D8 trigger fires | Yard supervisor / facility | S7: "Proposed new sequence (needs facility OK before any truck moves)" |
| Diverting to another facility or cancelling a load | Recommended only when no slot exists at the site | Customer + coordinator | ESC-SHP1015: "divert to FAC-GGN-01 D3 — needs customer approval" |
| Contradicted driver claims; any penalty | Facility or gate contradicts the claim | Coordinator (`ARRIVAL_CLAIM` NORMAL scores 40, exactly at the threshold, so it is queued; merged per key) | S8: "no state change and no penalty. Repeated contradicted claims go to the coordinator digest" |
| Safety, regulated loads, emergencies, contradictory data | Keywords, the regulated flag, or conflicting verified sources | Coordinator / safety lead | Policy only; no seed case |
| Failed outbound delivery | `delivery_status = FAILED` | Shift manager by phone | OM004 FAILED, retried to CON003 |

**Where in code:**
- `guards.review_priority`, `CoordinatorQueue`, `REVIEW_THRESHOLD = 40`
- `brief_scenarios.Scenario.escalate`
- RULE005 reason in `SlotEngine.check`

**Risks / what would change it:**
- In S4 the engine released APT1014A (weight-infeasible) and APT1006 (will be missed) *without* approval.
  That is defensible: one is infeasible by rule, and the other driver said the slot will be missed, which is
  against the driver's own interest. A stricter client may still want both approved.
- At 35-score digests, a NORMAL ETA update (35) never reaches a human. The threshold should be revisited against
  real spike data.

---

## D13: Concurrency test and result

**Decision:** Prove that one slot cannot be double-held when contenders really run in parallel. The test
(`race_for_slot`) works like this:
- Three threads each open their own SQLite connection to the same file DB.
- Each computes the option before a barrier.
- All of them then call `hold` at once.
- `hold` uses `BEGIN IMMEDIATE`, a re-check and the partial unique index `ux_live_hold_per_slot` as a final
  backstop.
- A lock error is retried, the way a real client would retry.

**Options considered:**
1. A single-threaded test with sequential calls.
2. Rely on the unique index only.
3. A multi-connection race with a write lock, a re-check and the index as defence in depth.

**Choice:** Option 3.

**Why:** Sequential tests cannot show a race. The index alone would raise an error, not give a clean "taken"
reason. Locking plus re-check gives a correct, explainable rejection.

**Evidence (S4 run, reproduced identically by the engine today):**

```json
{"slot_id": "SLOT-JAI-020", "contenders": ["SHP1006", "SHP1012", "SHP1017"],
 "results": {"SHP1017": "WON",
             "SHP1012": "REJECTED (SLOT-JAI-020 taken (hold_held SHP1017))",
             "SHP1006": "REJECTED (SLOT-JAI-020 taken (hold_held SHP1017))"},
 "winners": 1, "live_holds_after": 1}
```

Across all eight scenarios, the run measures report `conflicting_allocations = 0`, which sums
`slots_with_two_live_claims` and `shipments_with_two_current`.

**Where in code:**
- `slot_engine.race_for_slot`
- `SlotEngine.hold` and `conflicts`
- `HOLDS_SCHEMA` (`ux_live_hold_per_slot`)

**Risks / what would change it:**
- Which thread wins is not deterministic, and that is fine. The invariant is exactly one winner.
- SQLite serialises writers. A multi-instance service on Postgres would need `SELECT ... FOR UPDATE` or
  serialisable isolation, plus the same partial unique index.
- The test covers holds. `confirm` relies on the seed's active-slot index for appointments.

---

## Human side: drivers may exaggerate

Driver messages are **reports, not facts**. There is no live GPS, so the system never claims that a driver's
location or ETA was independently verified.

**What can verify a fact:**

| Source | Can it verify? | Example in run |
|---|---|---|
| Gate check-in (`facility_checkins`) | Yes, for arrival | THR007 "already at the gate" plus CHK1003 08:20 gives VERIFIED. THR008 "in the yard" plus CHK1004 09:25 gives VERIFIED and NO_ACTION. |
| Facility or warehouse message (`operational_messages`, yard/gate reply) | Yes, for dock state and arrival | Gate: "no truck WB23GT4112 (VEH012) at the gate" contradicts DRV012's "I'm at the gate now". Facility: "D2 is working normally" denies DRV013's outage claim. |
| Verified dock event (`dock_status_events` logged by facility) | Yes, for outages | DEVT001 plus OM003 plus CHK1005 support SHP1005's reassignment. |
| ETA history / appointment status | Plausibility only | DRV012's last ETA was 11:10 (ETA007, HIGH), which makes a 10:00 "at the gate" claim unlikely, so the agent asks the facility. |
| The driver's own statement | Never | A driver-sourced `VERIFIED` is downgraded by the harness ("VERIFIED without independent confirmation -> UNVERIFIED"). 13 guard interventions in the run, mostly this downgrade. |

**How the rules are applied:**
- Unverified claims are recorded as UNVERIFIED, and a short question goes to whoever can answer:
  - the FACILITY for arrival or outage claims
  - the DRIVER for the driver's own facts
- No dock is marked unavailable on a driver's word. Outages are applied only after facility (or coordinator)
  confirmation.
- No booking is confirmed on a driver's word. Confirmation comes only from the warehouse reference.
- No driver is penalised on a driver's word. Contradictions go to the coordinator digest, not to the driver.
- Unverified messages do not trigger a recalculation (D8).
- Statements against the driver's own interest, such as "I'll miss my 10:00", may release that driver's own slot
  (APT1006). This is the one case where a claim is acted on directly, because it costs the driver, not others.

**Known agent weaknesses from the run (caught by the design):** 4 of the 27 calls deviated from the expected
answer. Three triage answers asked the FACILITY whether a slot or dock was free (THR003 reply, THR001, THR005),
and one asked about OM003 instead of proposing. The prompt forbids asking about slot or dock availability. These
deviations caused no harm because availability is decided by `SlotEngine.check`, not by the triage answer. A
production gate should still suppress such questions before they are sent, so facility staff are not spammed.
