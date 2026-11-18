# SetuHaul driver-exception service — Product Requirements

Status: prototype complete (offline and live-LLM scenario runs); production service in planning.
Sources: `21182914-SetuHaul_Logistics_FDE_Challenge_Project_Brief.pdf`, `docs/brief_checklist.md`,
`setuhaul_agent_workflow.md`. Measured values come from `brief_scenarios.json` (live run with
`nvidia/nemotron-3-ultra-550b-a55b`, 27 agent calls, 588 s), `sandbox_report.json` and
`nvidia_stress_report_after_fixes.json`.

## 1. Problem

SetuHaul's inbound facilities (snapshot: Jaipur `FAC-JAI-01`, 9 docks across 2 facilities) receive
280-360 loads/day and 190-240 receiving appointments/day. When trucks run late, break down or arrive
early, drivers message coordinators over chat. Today those messages are handled by hand:

- Coordinators re-derive slot feasibility (dock type, reefer, weight, unload length, dock outages,
  facility rules, competing trucks) for every exception, under time pressure.
- "Available" is ambiguous while another driver is still deciding, so two trucks can be promised
  the same capacity.
- Driver statements ("I'm at the gate", "D2 is broken") are acted on without verification; there
  is no live GPS.
- Escalations go out incomplete or to the wrong place (seed data: OM004 FAILED to a night-shift
  address; FAC-JAI-01 has no night contact).
- The data itself has latent conflicts the database constraints do not catch (APT1014A 21,000 kg
  pending on a 20,000 kg dock; 75-minute unloads booked into 60-minute slots that overrun the next
  truck; seed views trust LOW-confidence ETAs and ignore dock events).

We need a conversational service that turns a driver exception into a safe, auditable outcome —
options, a held/requested/confirmed slot, or a complete hand-off — without ever double-allocating
capacity or committing on an unverified claim.

## 2. Users

| User | Volume / context | Needs |
|---|---|---|
| Drivers (15 in snapshot; carriers' fleets in production) | Message from the cab, low attention, mixed language, no GPS | Short questions, clear options, a definite "booked / not booked / someone will call you by HH:MM" |
| Facility / yard staff (gate, dock supervisors, shift manager) | Own the physical truth: gate check-ins, dock events | Only be asked to confirm things they can see (gate, dock state); accept or decline slot requests |
| Operations coordinators (5 per shift) | 15-25 exception messages on a normal day; 20-35 in a 30-minute disruption spike | A ranked, de-duplicated queue of only the cases needing a human, each with a complete hand-off record |
| Operations manager | Owns policy and KPIs | Written allocation policy, audit trail, metrics (conflicts, takeover rate, waits, utilisation) |

## 3. Goals and non-goals

Goals
- G1 Resolve routine exceptions (delay with clear ETA, options request, duplicate, status question)
  without coordinator takeover.
- G2 Zero conflicting allocations: no slot with two live claims, no shipment with two current
  appointments, under concurrent requests.
- G3 Every option shown is re-checked at hold, request and confirm; stale options are caught and
  re-offered, never silently committed.
- G4 Every escalation carries a complete hand-off record (13 fields) to a reachable owner.
- G5 Never treat a driver claim as fact; never penalise a driver on an unverified claim.
- G6 Keep coordinator load within 5 people during a 35-message spike by ranking, merging and
  digesting alerts.

Non-goals (this phase)
- Live GPS / telematics ingestion; ETA prediction models.
- Optimising the whole day's dock plan (we re-plan affected trucks, not a global optimiser).
- Carrier billing, detention charges or driver penalties.
- Multi-day / cross-midnight scheduling; multi-facility routing beyond a recommended diversion.
- Letting the LLM decide capacity, compatibility, ranking or commit status.

## 4. User stories

1. As a driver running late, I send "ETA 11:25" and get 2-4 feasible slots with dock and time, or a
   clear statement that none fits and who will call me. (S2, S4)
2. As a driver, I can ask "what's possible after 1 PM?" without anything being booked. (S1)
3. As a driver who sends "late by one hour", I get one targeted question for a clock time before
   any slot search. (S2, D3)
4. As a driver, if I accept an option after it expired or was taken, I'm told it changed and get a
   fresh one, not a false confirmation. (S5)
5. As a driver resending the same message or tapping "yes" twice, nothing happens twice. (S5, D9)
6. As a gate supervisor, I'm asked only "has VEH012 checked in?" or "is D2 down?", never "is a slot
   free?". (S8)
7. As a facility, I confirm a requested slot and the old appointment is replaced atomically. (S2, S5)
8. As a coordinator, I see a ranked queue; repeats of an open case merge; low-impact items go to a
   digest. (`guards.py:CoordinatorQueue`)
9. As a coordinator handling a no-slot case, I get reason, constraints, options checked, blocking
   rules, recommended actions, deadline, owner and channel in one record. (S6)
10. As an ops manager, I can see why truck A got D2 12:00 before truck B (written D7 policy). (S4)

## 5. Functional requirements

### FR1 Intake and linking (D2)
- FR1.1 Every inbound message carries an idempotency key (channel message ID). Exact duplicates are
  suppressed before any agent call (`slot_engine.py:SlotEngine.seen/remember`, `processed_messages`).
- FR1.2 Link message → driver (sender) → shipment (in-transit assignment or order reference) →
  current appointment (`appointments.is_current=1`). If a driver has several in-transit shipments
  and no reference, ask which (`sandbox.py:Sandbox.resolve_shipment`, S2 THR010/DRV004).
- FR1.3 The Triage Agent extracts `report_type`, `shipment_id`, `claimed_eta`, `dock_code`,
  `confirms_claim` (`nvidia_agents.py` schema); extraction is advisory and re-validated in code.

### FR2 Clarification (D1)
- FR2.1 Before showing options the service must hold: `shipment_id`, `eta`, `eta_confidence`,
  `earliest_start`, `latest_finish` (`brief_scenarios.py:REQUIRED_FIELDS_BEFORE_OPTIONS`).
- FR2.2 Ask one short question per turn, reusing known details; route it to whoever can answer
  (driver: own shipment/ETA; facility: gate/dock state; coordinator: unresolvable conflicts).
- FR2.3 Never ask anyone whether a slot is free; availability is computed.

### FR3 ETA confidence (D3)
- Rule (`brief_scenarios.py:revise_eta`): explicit clock time consistent with history → HIGH, planning
  ETA = stated; hedged ("around") or moved >30 min since last report → MEDIUM, planning ETA +15 min;
  relative ("late by one hour") or a range → LOW, ask before searching; no time → ask.
- LOW-confidence ETAs never receive a new hold; their existing appointment stays as-is until
  clarified (S4 SHP1013).

### FR4 Options-only vs booking (D5, D6)
- FR4.1 An options request lists and compares feasible options and places **no** hold (S1).
- FR4.2 Choosing an option places a short hold; showing an option does not.

### FR5 Hold / request / confirm / release (D6)
- States: `OPTION` (computed, not stored) → `HELD` (TTL 10 min) → `REQUESTED` (driver accepted; no
  TTL; awaiting facility) → `CONFIRMED` (appointment written) ; terminal `RELEASED`, `EXPIRED`.
- FR5.1 `hold` re-checks feasibility inside a write transaction and holds every slot a long unload
  needs.
- FR5.2 `request` is idempotent on `request_key`; a non-HELD hold returns `STALE` and the option is
  re-computed and re-offered.
- FR5.3 `confirm` re-checks feasibility, cancels the old current appointment (kept as history with
  reason) and inserts the new one in one transaction; if infeasible it releases the hold.
- FR5.4 `release` cancels an appointment once; replayed cancellations are no-ops.

### FR6 Competing requests policy (D7)
Order: (1) priority CRITICAL > HIGH > NORMAL > LOW; (2) same priority: fewer feasible options first;
(3) LOW-confidence ETAs not held until clarified; (4) then earliest report. Each truck gets its
earliest feasible start; ties go to the dock leaving others the most options. Within the engine,
first committed transaction wins (`BEGIN IMMEDIATE`). Measured: 0 priority violations (S4).

### FR7 Recovery when the world changes (D8, D9)
- Verified dock events (facility-logged), gate check-ins and facility replies trigger re-plan of
  affected appointments (S5 DEVT001, S7 resequence on check-in).
- Holds expire; late acceptances are caught as `STALE`; retries and replays are no-ops.
- Long unloads implicitly claim the following slot (`SlotEngine.overrun_claims`).

### FR8 Escalation and hand-off (D10)
- When no feasible slot exists, or a decision needs approval, create a hand-off record with all
  `HANDOFF_FIELDS`: case_id, shipment_id, driver_id, facility_id, reason, constraints,
  options_checked, blocking_rules, recommended_actions, deadline, owner, channel, delivery_status.
- Track delivery; on a failed channel retry via another reachable contact (S6: email FAILED →
  phone to CON003 shift manager).
- Tell the driver what happens next and by when.

### FR9 Human approvals (D12)
Required (trigger → approver):
- Exception to a facility rule, e.g. RULE005 start ≥21:00 → shift manager.
- Moving a confirmed or higher-priority truck to free capacity → coordinator.
- Accepting a slot that overruns a driver's stated leave-by time → coordinator + driver.
- Applying a dock outage reported only by a driver → facility confirmation.
- Contradictory data (claim vs gate log, ETA vs check-in) with booking impact → coordinator.
- Diversion to another facility, regulated/hazardous loads, safety incidents, emergencies,
  penalties → coordinator/ops manager. Agent never decides these.

### FR10 Refusals (D11)
Plain-language refusals that state the reason and the next step, e.g. (S8):
"The gate hasn't logged your truck yet, so I can't call you to a dock. When you reach the gate,
check in there and I'll pick it up straight away." and "Arriving early doesn't move you ahead of
trucks already booked or waiting; if a dock is idle with no one due, the yard will call you in."

## 6. Non-functional requirements

Safety invariants (must hold at all times; checked by `SlotEngine.conflicts`)
- NFR-S1 At most one live claim (active appointment or HELD/REQUESTED/CONFIRMED hold) per slot.
- NFR-S2 At most one current active appointment per shipment.
- NFR-S3 `PRAGMA foreign_key_check` returns no rows.
- NFR-S4 LLM output never changes state: `state_change_allowed` is forced false and any selection
  outside the deterministic feasible set is nulled and escalated (`guards.py:guard_answer`).
- NFR-S5 A VERIFIED label without independent confirmation (facility message, gate check-in,
  verified dock event) is downgraded to UNVERIFIED.
- NFR-S6 Agent unavailable/invalid → safe fallback to the coordinator queue, never a guess.

Latency
- NFR-L1 Driver acknowledgement ≤ 5 s (deterministic); answer with options ≤ 60 s p95.
- NFR-L2 Spike capacity: 35 messages in 30 minutes with no message waiting > 2 min for first agent
  call. Current mean LLM latency is 21.8 s/call (live run), so the service needs ≥ 2 parallel workers.

Auditability
- NFR-A1 Every state transition is stored with time, actor, idempotency key and reason; cancelled
  and replaced appointments are kept as history, never deleted.
- NFR-A2 Every agent call stores input payload, raw answer, guarded answer, guard interventions and
  ungrounded evidence (as in `brief_scenarios.json`).

Privacy
- NFR-P1 No PII to the LLM: only synthetic IDs, order references, times, dock codes and load
  attributes. No names, phone numbers, emails or licence numbers (`sandbox.py` docstring;
  `nvidia_agent_evals.py` sends synthetic payloads).
- NFR-P2 API key from environment / `.env` only, never logged or committed.

## 7. Success metrics

| Metric | Target (pilot) | Current measured | Source |
|---|---|---|---|
| Workflow confidence (0.5·accuracy + 0.3·safety + 0.2·escalation completeness) | ≥ 95 | **92.6 / 100** | `brief_scenarios.json` |
| Agent decision accuracy | ≥ 95 % | **85.2 %** (23/27; all 4 misses are Triage) | same |
| Conflicting allocations | 0 | **0** | same |
| Escalation record completeness | 100 % | **100 %** (3 escalations) | same |
| Options infeasible at re-check (caught, not committed) | caught 100 % | **1 caught**, 0 committed | same |
| Priority-policy violations | 0 | **0** | same |
| Resolved without manual takeover | ≥ 70 % of exceptions | **62 %** of scenarios (by design S4/S5/S6 escalate) | same |
| Clarification turns | ≤ 1 per exception needing one | **2** across 8 scenarios | same |
| Avg turns / minutes to outcome | ≤ 4 turns | **5.4 turns, 81.8 min** (sim clock) | same |
| Agent errors (no/invalid answer) | 0 | **0** of 27 | same |
| Guard interventions | trending down | **13** (11 VERIFIED labels from dispatch/assessment, 2 lowercase message IDs as `selected_id`) | same |
| Answers with ungrounded evidence | 0 | **1** | same |
| Mean agent latency | ≤ 10 s | **21.8 s** | same |
| Avg wait gate-in → dock-in (S7) | -10 % | **83.3 → 78.3 min** (-6 %) | same |
| Standard-dock utilisation 11:00-14:00 | ≥ 70 % | **70 %** | same |
| Concurrency race (3 threads, 1 slot) | 1 winner | **1 winner, 1 live hold** | S4 `race_for_slot` |
| Stress suite rubric (18 live calls) | ≥ 95 | **100 / 100** after prompt fixes | `nvidia_stress_report_after_fixes.json` |
| Live sandbox score / decision accuracy | ≥ 90 / ≥ 90 % | **80.7 / 71 %**, safety 5/5 | `sandbox_report.json` |

## 8. Boundaries — what the agent never decides alone

The agent (LLM) advises; deterministic code decides; humans approve. The agent never alone:
- decides capacity or availability, vehicle–dock fit, ranking among competing trucks, or commit
  status (hold/confirm/cancel);
- marks a dock unavailable or a driver "arrived" on a driver's word;
- grants a facility-rule exception, moves another truck, diverts to another facility;
- handles safety incidents, penalties, no-slot cases, contradictory data, regulated loads or
  emergencies — these go to a human with a complete hand-off.

## 9. Risks

| Risk | Impact | Mitigation |
|---|---|---|
| Triage over-asks the facility (CLARIFY/FACILITY when PROPOSE expected) | Slower outcomes, facility fatigue | Measured prompt fix + regression eval (implementation plan P0) |
| Two feasibility implementations drift (`guards.feasible_slot_ids` vs `SlotEngine.check`) | Agent offered slots the engine rejects | Single source: engine computes the feasible set for agents |
| LLM latency 20+ s at spike | Driver wait, queue build-up | Parallel workers, deterministic acks, smaller model for triage |
| Seed data quality (overweight pending booking, overruns, missing night contact) | Wrong promises | Pre-allocation audit, overrun claims, contact validation before escalation |
| Driver gaming (false arrival/outage claims) | Queue jumping, wrong outages | Verification only from facility/gate/verified events; no penalties automatically |
| Coordinator overload in spike | Missed critical cases | Priority scoring, merge, digest; capacity 5 × 4 reviews/h |
| PII leakage to LLM | Compliance | Payload allow-list + redaction tests |

## 10. Rollout phases

1. **Shadow (2 weeks)**: service reads live messages, proposes outcomes; coordinators act as today.
   Compare proposals to coordinator decisions; target ≥ 90 % agreement, 0 unsafe proposals.
2. **Assisted (2-4 weeks)**: options-only, duplicates and status questions automated; holds
   require one-click coordinator approval; escalations auto-created.
3. **Supervised automation**: hold/request automated; facility confirms; coordinators handle the
   queue only. Gate: workflow confidence ≥ 95, 0 conflicts over 4 weeks.
4. **Scale**: second facility (FAC-GGN-01), real-time ETA feeds, learned ETA confidence.
