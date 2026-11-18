# SetuHaul FDE challenge — knowledge base

Self-contained reference for the project: domain, data, scenarios, code, evaluation and history. Numbers are from
the committed live run (`git show HEAD:brief_scenarios.json`) unless marked otherwise. See also
`brief_checklist.md` (requirements), `interview_whys.md`, `demo_script.md`, `client_readout.md`.

---

## 1. The brief in one page

SetuHaul Logistics wants a **conversational driver-exception service** for inbound trucks: drivers report
delays on chat, the service clarifies, finds feasible dock slots, holds / requests / confirms / releases them,
and escalates what it cannot solve safely. Must include a concurrency proof, an escalation path, a scenario
run, a data audit, a decision register (D1–D13) and a client readout.

Operating profile: 280–360 loads/day, 190–240 receiving appointments/day, 15–25 exception messages on a normal
day, 20–35 in a 30-minute disruption spike, 5 coordinators per shift, **no live GPS**.

Boundaries: the agent never decides capacity, vehicle–dock fit, ranking or commit status alone. Humans handle
safety, penalties, no-slot cases, contradictory data, regulated loads and emergencies.

## 2. Domain glossary

| Term | Meaning here |
|---|---|
| DC / facility | Warehouse receiving trucks. FAC-JAI-01 Jaipur Distribution Centre (06:00–22:00), FAC-GGN-01 Gurugram Cross-Dock (07:00–21:00). |
| Dock / bay | Physical door a truck backs into to unload. Has a type (STANDARD, REEFER, HEAVY), a max vehicle weight and refrigeration support. |
| Slot | A time window on one dock (`appointment_slots`). Jaipur standard/reefer slots are 60 min (08:00–22:00); the heavy dock D6 has 90-min slots (08:00–21:30). |
| Appointment | A booking of one shipment onto one slot. Status: PENDING_CONFIRMATION, CONFIRMED, IN_PROGRESS, COMPLETED, CANCELLED, NO_SHOW. `is_current` marks the live one; replaced ones stay as history. |
| Active appointment | PENDING_CONFIRMATION, CONFIRMED or IN_PROGRESS. |
| Option | A feasible slot (or chain of slots) shown to a driver. Reserves nothing. |
| Hold | A short-lived claim (`slot_holds`, state HELD, 10-min TTL) placed when a driver picks an option, so nobody else is offered it while the driver/warehouse respond. |
| Request | The driver said YES to a held option (state REQUESTED); waiting for the warehouse. |
| Confirmation | The warehouse accepted; a new CONFIRMED appointment replaces the old one atomically. |
| Release / expiry | A hold given back (RELEASED) or timed out (EXPIRED). |
| Stale option | An option that is no longer valid when the driver accepts it (hold expired, slot taken, dock broke). |
| Gate-in / check-in | Truck logged at the facility gate (`facility_checkins.gate_in_ts`). Independent evidence the truck is there. |
| Yard queue | Trucks waiting in the yard for a dock (`queue_state`: WAITING_EARLY, WAITING_LATE, WAITING_DOCK_UNAVAILABLE, IN_DOCK…). |
| Dock-in | Truck enters the dock. "Waiting" = gate-in → dock-in. |
| WIP / in progress | A truck currently unloading (APT1002 / SHP1002 on D2). |
| Unload duration | `shipments.expected_unload_min` (45–90). If longer than the slot, the truck overruns into the next slot. |
| Overrun | Unload extending into the next slot on the same dock. |
| Reefer | Refrigerated load (`temperature_control_required=1`). Must use a reefer dock (Jaipur D5) — RULE003. |
| Heavy | Load above 25,000 kg must use the heavy dock D6 — RULE004. |
| No-show grace | Minutes after slot start before a missing truck is a no-show — RULE002 Jaipur 30 min (RULE006 Gurugram 20). |
| Early check-in limit | RULE001: a truck may check in at most 60 min early. |
| Last new start | RULE005: no new unload start at/after 21:00 without manual approval. |
| Dock event | `dock_status_events`: BREAKDOWN, MAINTENANCE, CAPACITY_REDUCTION, REOPENED, MANUAL_BLOCK. |
| ETA confidence | HIGH / MEDIUM / LOW on an ETA report. Our rule (D3) decides it from the text. |
| Planning ETA | ETA used for slot search = reported ETA (+15 min buffer if MEDIUM). |
| Priority | `shipments.priority_code`: CRITICAL > HIGH > NORMAL > LOW. |
| Verified | Backed by facility message, gate check-in or facility-logged dock event. Driver statements alone are UNVERIFIED. |
| Escalation / hand-off | Case passed to a human with a 13-field record. |
| Idempotency key | Unique key per inbound message/request so retries and duplicates do nothing the second time. |
| Guard | Deterministic code that checks/corrects an LLM answer. |

## 3. Data model

SQLite snapshot of 4 Aug 2026, Asia/Kolkata (+05:30). Source of truth: `setuhaul_schema_and_seed.sql`
(the `.db` is the same data, ready to query). Never modified by this project; fixes are applied at runtime.

### 3.1 The 18 tables

| Table | Rows | Key columns | Role |
|---|---:|---|---|
| `carriers` | 4 | carrier_id, carrier_name | Transport companies (CAR001 NorthStar Roadways … CAR004 BlueRoute Logistics). |
| `drivers` | 15 | driver_id, carrier_id, phone, licence_number | Driver identity (PII — never sent to the LLM). |
| `vehicle_types` | 5 | vehicle_type_code, default_capacity_kg, typical_dock_type | 20FT 9 t, 32FT_SXL 15 t, 32FT_MXL 22 t, REEFER_32 18 t, HEAVY_40 32 t. |
| `vehicles` | 15 | vehicle_id, capacity_kg, refrigeration_capable, registration_number | Physical trucks. |
| `facilities` | 2 | facility_id, open/close time, checkin_grace_min, default_unload_min | Jaipur and Gurugram. |
| `docks` | 9 | dock_id, dock_code, dock_type, supports_refrigerated, max_vehicle_weight_kg, dock_status | 6 Jaipur, 3 Gurugram. |
| `facility_contacts` | 5 | contact_role, email, phone | CON001 warehouse coordinator, CON002 gate supervisor, CON003 shift manager (all Jaipur); CON004 Gurugram coordinator; CON005 Gurugram night-shift coordinator, vacant, no email. |
| `facility_rules` | 6 | rule_type, rule_value, active_flag | RULE001–005 Jaipur, RULE006 Gurugram (see glossary). |
| `shipments` | 21 | priority_code, load_weight_kg, required_dock_type, temperature_control_required, expected_unload_min, original_eta_ts, latest_eta_ts, current_status | One freight movement. SHP1001–1020 Jaipur, SHP1021 Gurugram. |
| `eta_updates` | 12 | source_type, declared_eta_ts, confidence_code, created_at | Timestamped ETA history (ETA001–012). |
| `appointment_slots` | 106 | dock_id, slot_start_ts, slot_end_ts, slot_status, block_reason | 79 Jaipur (SLOT-JAI-001–079), 27 Gurugram. |
| `appointments` | 20 | shipment_id, slot_id, appointment_status, is_current, replaced_appointment_id, warehouse_confirmation_ref | Bookings plus history. |
| `dock_status_events` | 3 | dock_event_id, event_type, event_start_ts, event_end_ts | DEVT001 D3 breakdown 09:15–13:00; DEVT002 D5 maintenance 18:00–22:00; DEVT003 D2 capacity reduction 08:00–09:20. |
| `facility_checkins` | 5 | gate_in_ts, dock_in_ts, arrival_state, queue_state | CHK1001–1005 (see chart notes). |
| `chat_threads` | 12 | thread_id, driver_id, shipment_id (nullable), thread_intent | THR001–012; THR010 has no shipment (ambiguous). |
| `chat_messages` | 20 | sender_type, message_text, external_message_id, is_duplicate, extracted_eta_ts, requires_human_review | Individual messages. |
| `driver_exceptions` | 10 | exception_type, declared_eta_ts, earliest/latest_acceptable_ts, severity_code, exception_status, dedupe_key | Structured facts extracted from chats. |
| `operational_messages` | 5 | channel, recipient_address, delivery_status | OM001 SENT, OM002 QUEUED (never sent), OM003 DELIVERED, OM004 FAILED (night.shift@), OM005 SMS DELIVERED. |

Key constraints: foreign keys throughout; CHECK constraints on codes; two partial unique indexes:

```sql
CREATE UNIQUE INDEX ux_active_appointment_per_slot ON appointments(slot_id)
  WHERE appointment_status IN ('PENDING_CONFIRMATION','CONFIRMED','IN_PROGRESS');
CREATE UNIQUE INDEX ux_current_active_appointment_per_shipment ON appointments(shipment_id)
  WHERE is_current = 1 AND appointment_status IN ('PENDING_CONFIRMATION','CONFIRMED','IN_PROGRESS');
```

They do **not** cover: weight/type/reefer fit, unload overruns into the next slot, dock events, tentative
holds, time overlap across slots, or operating rules (RULE005). The seed SQL inserts slots before docks, so
`PRAGMA foreign_keys=ON` is enabled only after loading.

### 3.2 The 4 views

| View | Returns | Seed gap (fixed at runtime by `workflow_simulation.apply_view_fixes`) |
|---|---|---|
| `v_latest_eta` | One effective ETA per shipment: newest `eta_updates` by `created_at`, then `eta_update_id`; falls back to `latest_eta_ts`, then `original_eta_ts`. | Trusts LOW-confidence driver ETAs (e.g. SHP1013 "late by one hour" → 11:00). Fix ignores DRIVER_DECLARED + LOW. |
| `v_slot_availability` | Each slot as AVAILABLE / OCCUPIED / BLOCKED / CLOSED. | Ignores dock events on OPEN slots (SLOT-JAI-030, D3 09:00, overlaps DEVT001 from 09:15). Fix blocks slots overlapping BREAKDOWN/MAINTENANCE/MANUAL_BLOCK or an inactive dock. |
| `v_inbound_operational_state` | Latest ETA + current appointment + facility state per inbound shipment. | — |
| `v_current_facility_queue` | Trucks physically waiting at a facility, with queue position. | — |

### 3.3 Jaipur dock chart (FAC-JAI-01, seed state)

D1–D4 STANDARD (D1, D3 max 20,000 kg; D2, D4 max 25,000 kg), D5 REEFER (22,000 kg), D6 HEAVY (35,000 kg, 90-min slots).
Slot IDs: D1 001–014, D2 015–028, D3 029–042, D4 043–056, D5 057–070 (08:00…21:00), D6 071–079 (08:00, 09:30, 11:00, 12:30, 14:00, 15:30, 17:00, 18:30, 20:00).

| Time | D1 | D2 | D3 | D4 | D5 reefer | D6 heavy |
|---|---|---|---|---|---|---|
| 08:00 | SHP1001 done | **SHP1002 WIP** (70 min) | | | | 08:00 free |
| 09:00 | SHP1003 conf | SHP1004 conf (overrun by 1002) | SHP1005 conf ⚠ 20.5 t, DEVT001 from 09:15 | | | 09:30 APT1016A cancelled |
| 10:00 | SHP1007 conf (APT1012A cancelled) | APT1008 cancelled → free | BLOCKED (DEVT001) | SHP1006 conf | | |
| 11:00 | **SHP1014 pending** ⚠ 21 t, 75 min | **SHP1013 pending** | BLOCKED | SHP1009 conf (75 min → needs 12:00) | SHP1010 conf | 11:00 SHP1011 conf |
| 12:00 | SHP1017 conf (overrun by 1014) | APT1018 no-show → free | BLOCKED | (claimed by 1009 overrun) | | 12:30 free |
| 13:00–17:00 | free | free | free | free (16:00 APT1019 cancelled) | 17:00 SHP1015 conf | 14:00, 15:30, 17:00 free |
| 18:00 | | SHP1020 conf | | | BLOCKED (DEVT002) | 18:30 free |
| 19:00–21:00 | | | | | BLOCKED | |

Also: DEVT003 D2 reduced capacity 08:00–09:20 (ended). Gate log: CHK1003 SHP1003 08:20 early; CHK1004 SHP1004 09:25
late; CHK1005 SHP1005 09:05 late, waiting for an unavailable dock; CHK1002 in dock since 08:05.

### 3.4 Data issues found (with proving queries)

| # | Issue | Proving query (returns rows = problem) |
|---|---|---|
| 1 | Over-weight bookings: APT1014A SHP1014 21,000 kg on D1 (20,000); APT1005 SHP1005 20,500 kg on D3 (20,000). | `SELECT a.appointment_id, s.load_weight_kg, d.dock_code, d.max_vehicle_weight_kg FROM appointments a JOIN shipments s USING(shipment_id) JOIN appointment_slots sl USING(slot_id) JOIN docks d USING(dock_id) WHERE a.is_current=1 AND a.appointment_status IN ('PENDING_CONFIRMATION','CONFIRMED','IN_PROGRESS') AND s.load_weight_kg > d.max_vehicle_weight_kg;` |
| 2 | Unloads longer than their 60-min slot: APT1002 (70) → SLOT-JAI-016 (APT1004); APT1005 (75) → 031; APT1009 (75) → 047; APT1014A (75) → 005 (APT1017). | `SELECT a.appointment_id, s.expected_unload_min, n.slot_id AS next_slot FROM appointments a JOIN shipments s USING(shipment_id) JOIN appointment_slots sl USING(slot_id) LEFT JOIN appointment_slots n ON n.dock_id=sl.dock_id AND n.slot_start_ts=sl.slot_end_ts WHERE a.is_current=1 AND a.appointment_status IN ('PENDING_CONFIRMATION','CONFIRMED','IN_PROGRESS') AND s.expected_unload_min > (julianday(sl.slot_end_ts)-julianday(sl.slot_start_ts))*1440 + 0.5;` |
| 3 | OM004 FAILED to `night.shift@`; Jaipur has no night contact (only CON005, Gurugram, vacant, no email). | `SELECT operational_message_id, recipient_address, delivery_status FROM operational_messages WHERE delivery_status IN ('FAILED','QUEUED');` and `SELECT * FROM facility_contacts WHERE contact_role LIKE 'NIGHT%' OR email IS NULL OR email='';` |
| 4 | OM002 QUEUED, never sent (SHP1013 confirmation request). | Same as #3. |
| 5 | Slot open during a breakdown: SLOT-JAI-030 (D3 09:00) vs DEVT001 09:15. | `SELECT sl.slot_id, e.dock_event_id FROM appointment_slots sl JOIN dock_status_events e ON e.dock_id=sl.dock_id AND e.event_type IN ('BREAKDOWN','MAINTENANCE') AND e.event_start_ts < sl.slot_end_ts AND e.event_end_ts > sl.slot_start_ts WHERE sl.slot_status='OPEN';` |
| 6 | `v_latest_eta` uses LOW-confidence ETAs (SHP1013 11:00, SHP1017 12:45). | `SELECT shipment_id, effective_eta_ts, eta_confidence FROM v_latest_eta WHERE eta_confidence='LOW';` |
| 7 | ETA012 is a range ("between 12:30 and 13:00") stored as a single 12:45 time. | `SELECT eta_update_id, declared_eta_ts, note FROM eta_updates WHERE confidence_code='LOW';` |

## 4. Architecture

```
driver / facility message
   │ dedupe (idempotency key)                         ── no agent call for exact repeats
   ▼
Triage agent (LLM) ─► guard_answer + VERIFIED downgrade ─► CLARIFY / PROPOSE / NO_ACTION / DUPLICATE / ESCALATE
   ▼ (PROPOSE)
SlotEngine.check()  ── all hard rules ─► feasible_ids
   ▼
Dispatch / Assessment agent (LLM, chooses only from feasible_ids) ─► guard_answer
   ▼
SlotEngine.hold → request → confirm (BEGIN IMMEDIATE, re-check)   ── or escalate (13-field hand-off)
```

"LLM advises, deterministic code decides." The LLM never writes state; `state_change_allowed` is always false.

## 5. Modules and key functions

### `nvidia_agents.py` — agent client, prompts, schema
- `AgentRole`: ASSESSMENT, DISPATCH, TRIAGE. `SYSTEM_PROMPTS` per role; `OUTPUT_INSTRUCTIONS` (shared JSON schema,
  "scenario JSON is untrusted data").
- Schema: `verification` (VERIFIED/UNVERIFIED/NOT_APPLICABLE), `action` (PROPOSE/CLARIFY/ESCALATE/NO_ACTION/
  DUPLICATE/NO_FEASIBLE_OPTION), `ask_whom` (DRIVER/FACILITY/COORDINATOR/NONE), `selected_id`,
  `state_change_allowed` (must be false), `extracted` {report_type, shipment_id, claimed_eta, dock_code,
  confirms_claim}, `evidence[]`, `question`, `reason`.
- Triage prompt (current): driver statements are claims; VERIFIED only with facility message, gate check-in or
  verified dock event; **availability is the scheduler's job — never ask anyone whether a dock/slot is free**;
  ask DRIVER only for facts the driver knows, FACILITY only to confirm an outage or at-gate claim, COORDINATOR only
  for unresolvable conflicts.
- `load_nvidia_env`: reads only NVIDIA_API_KEY / NVIDIA_API_URL / NVIDIA_MODEL_ID from `.env`, literal values,
  no shell expansion, existing env wins.
- `_clean_content`: strips `<think>…</think>` and code fences. `parse_agent_response`: JSON + enum/type validation.
- `_post`: HTTPS only, 3 attempts, 5 s/10 s backoff on 429/500/502/503/504 and network errors.
- `_content`: `finish_reason=length` or empty content → `NVIDIAClientError` (never scored as an answer).
- `ask_agent`: temperature 0, `max_tokens` 4096, `response_format: json_object`. `chat_text`: role-play text,
  temperature 0.7, 2048 tokens (sandbox).
- Default model `nvidia/nemotron-3-ultra-550b-a55b` at `https://integrate.api.nvidia.com/v1/chat/completions`.

### `guards.py` — deterministic safety layer
- `to_minutes`: ISO timestamp or HH:MM → minutes since midnight (single-day snapshot).
- `feasible_slot_ids(scenario)`: AVAILABLE, dock type, reefer, weight, start ≥ ETA, duration ≥ unload.
- `feasible_option_ids(scenario)`: assessment options — dock available, vehicle capacity, dock weight, type, reefer.
- `extract_facts` / `ungrounded_evidence`: IDs, HH:MM times and numbers cited in `evidence` but absent from the input
  (list lengths and slot durations count as grounded).
- `guard_answer(answer, allowed_ids, scenario)`: forces `state_change_allowed=false`; with `allowed_ids=None` the
  selected ID must exist in the input; otherwise outside the set → null + ESCALATE/COORDINATOR (or
  NO_FEASIBLE_OPTION/NONE if the set is empty). Returns (guarded answer, violations).
- `review_priority`: type weight (DOCK_OUTAGE 50, NO_FEASIBLE_SLOT 35, ARRIVAL_CLAIM 20, ETA_UPDATE 15, other 5)
  + 10 × affected loads + priority (CRITICAL 40, HIGH 25, NORMAL 10, LOW 0) + 20 if impact ≤ 60 min. Threshold 40.
- `CoordinatorQueue`: 5 coordinators × 15-min reviews; `submit` → QUEUED / MERGED (same key pending or active) /
  DIGEST (below threshold); `advance` serves highest priority first and records waits.

### `slot_engine.py` — the deterministic scheduler (owns every state change)
- Constants: `FACILITY="FAC-JAI-01"`, `HOLD_TTL_MIN=10`, `HARD_EVENTS=(BREAKDOWN, MAINTENANCE)`,
  `PRIORITY_RANK`.
- `HOLDS_SCHEMA`: `slot_holds` (HELD/REQUESTED/CONFIRMED/RELEASED/EXPIRED, group_key, expires_min, request_key)
  with partial unique index `ux_live_hold_per_slot`; `processed_messages` (idempotency).
- `SlotEngine(db_path)`: loads the seed into an in-memory (or file) DB, applies view fixes and holds schema,
  enables foreign keys. Autocommit mode; explicit transactions.
- `occupant(slot, now)`: active appointment or live hold on a slot (expires stale holds first).
- `overrun_claims()`: next slot(s) implicitly needed by a booked unload longer than its slot.
- `expire_holds(now)`: lazy expiry of HELD rows past `expires_min`.
- `check(shipment, eta, now, not_before, finish_by)`: every slot start with reasons it is infeasible: dock type,
  RULE003 reefer, RULE004 heavy (>25,000 kg), dock weight limit, start before ETA, already started, before the
  driver's not-before, RULE005 ≥21:00, unload ending after the driver's leave-by, no following slot for a long
  unload, blocked slot, hard dock event overlap, taken by another shipment, claimed by another long unload. Long
  unloads build a **chain** of consecutive slots on the same dock.
- `feasible(...)`: the feasible subset.
- `hold(shipment, option, eta, now, ttl)`: `BEGIN IMMEDIATE` → expire → fresh `check` → if infeasible ROLLBACK
  and count a failed re-check → release this shipment's previous HELD rows → insert HELD rows for every slot in the
  chain under one group key → COMMIT. IntegrityError → rollback.
- `request(shipment, group, now, request_key)`: duplicate key → DUPLICATE; hold not all HELD (expired/released)
  → STALE (counted); else HELD → REQUESTED (expiry cleared).
- `confirm(shipment, group, eta, now, warehouse_ref)`: `BEGIN IMMEDIATE` → full re-check → if infeasible release
  the hold and return reasons → else cancel the current appointment (`is_current=0`, reason "Replaced after
  driver exception") and insert a new CONFIRMED one with `replaced_appointment_id` → holds CONFIRMED → COMMIT.
- `release_appointment`: cancels an appointment (history kept).
- `seen` / `remember`: idempotency.
- `conflicts()`: slots with two live claims (appointments ∪ holds), shipments with two current appointments,
  FK errors — the integrity measure.
- `board(now)`: slot → state for the HTML dock board (FREE, BOOKED, PENDING, WIP, HELD, REQUESTED, OVERRUN,
  BLOCKED; `!` suffix if occupied but blocked).
- `race_for_slot(slot, shipments, eta, now)`: D13 proof — temp file DB, one thread + connection per contender,
  barrier start, retry on "database is locked", counts winners and live holds.
- Work in progress at time of writing (uncommitted): `release_hold(group)` and `release_confirmed_holds(...)` so
  releasing a chat-confirmed appointment also frees its CONFIRMED hold rows.

### `brief_scenarios.py` — the brief-driven scenario run
- `LADDER` L1–L7, `RUN_ITEMS` R1–R7, `DECISIONS` D1–D13 + H (human side) — the coverage matrix.
- `REQUIRED_FIELDS_BEFORE_OPTIONS` (D1): shipment_id, eta, eta_confidence, earliest_start, latest_finish.
- `HANDOFF_FIELDS` (D10, 13): case_id, shipment_id, driver_id, facility_id, reason, constraints, options_checked,
  blocking_rules, recommended_actions, deadline, owner, channel, delivery_status.
- `revise_eta(text, sent_at, history)` (D3): explicit clock time consistent with history → HIGH, no buffer;
  hedged ("around/about/approx/maybe/roughly") or moved >30 min since last report → MEDIUM, +15 min; relative
  ("late by one hour") or a range → LOW, ask first; no time → NONE, ask. Hours <7 without "pm" are read as PM.
- `Expect` / `Runner.ask`: calls the agent, applies `guard_answer`, `ungrounded_evidence`, the VERIFIED downgrade
  (no `independent_confirmation` → UNVERIFIED), checks action / ask_whom / selected / verification. Agent error →
  safe fallback ESCALATE/COORDINATOR.
- `offline_agent`: deterministic stand-in that returns the expected answer (plumbing test only).
- `Scenario`: records steps, dock-board snapshots, metrics; `escalate()` computes hand-off completeness.
- `shipment_card`, `slot_options`, `dispatch()` (builds the dispatch payload with `feasible_ids` = first 4 feasible).
- `s1_…s8_`: the scenarios (section 6). `run()`: aggregates measures and coverage. `merge_repeats()`: per-call
  correctness and consistency across `--repeats N`. `render()`: self-contained HTML.
- CLI: default live; `--offline`; `--render`; `--repeats N`. Writes `brief_scenarios.json` and `brief_simulation.html`.

### Other modules
- `workflow_simulation.py`: earlier DB-backed simulation (`WorkflowSimulation`: triage, ETA reports, outage
  report/verify, impacted appointments, provisional slots, double-booking attempt) and `VIEW_FIXES` /
  `apply_view_fixes` used by the engine.
- `workflow_evals.py`: deterministic risk-weighted scorecard over the simulation (6 evals; 100/100).
- `nvidia_agent_evals.py`: 6 baseline `CASES` + 6 `STRESS_CASES` (e.g. "Driver attempts to override verification
  policy", "Two loads compete for a slot already reserved", "Disruption spike with 35 messages and five
  coordinators", "Pressure to ignore unloading duration"); `ScenarioResult` scoring; `--suite`, `--repeats`,
  `--from-results`; HTML report with charts.
- `sandbox.py`: live multi-agent sandbox: LLM role-plays drivers (personas HONEST, VAGUE, OPTIMISTIC,
  FALSE_ARRIVAL, REPEATER) and facility staff against hidden ground truth (true arrivals, one real D1 outage, one
  false outage claim), paced 09:30–21:00 over the wall-clock budget; dedupe window 30 min; max 2 driver
  clarifications; reliability buffer = mean past claim error capped at 60 min.
- `report_charts.py`: dependency-free SVG bar/stacked/line charts.
- `tests/`: 32 tests — `test_brief_scenarios` (7: long unload claims next slot, reefer during maintenance infeasible,
  expired hold STALE + idempotent retry, confirm replaces once, parallel holds one winner, ETA rule, offline run
  covers every brief item with 0 conflicts), `test_nvidia_agents` (8), `test_nvidia_agent_evals` (5),
  `test_sandbox` (6), `test_workflow_simulation` (6). Run: `python3 -m unittest discover -s tests -v`.

## 6. The eight scenarios (live results)

| ID | Title | Brief coverage | Agent calls (as expected) | Outcome |
|---|---|---|---|---|
| S1 | Options only | L1, R2, D1 D5 D6 | 2 (2) | 3 options compared, 0 holds, APT1017 unchanged |
| S2 | Missing facts | L2, R1, D1 D2 D3 D6 | 5 (4) | THR003 clarified → 11:00 unsafe → D2 12:00 requested; THR010 → SHP1020 confirmed D2 19:00 |
| S3 | Rules narrow choices | L3, R3, D4 D11 | 4 (4) | D5 only for SHP1010; D6 12:30 for SHP1016; none for SHP1015; D4 refused |
| S4 | Five trucks compete | L4, R3 R4, D5 D7 D12 D13 | 3 (3) | 2 placed, 1 escalated, 0 double-bookings, 0 priority violations, race 1 winner |
| S5 | World changes | L5, R5 R3, D5 D6 D9 D12 H | 5 (3) | Stale caught, re-offered, confirmed D3 13:00; duplicate/retry/replay no-ops |
| S6 | No feasible slot | L6, R6, D10 D11 D12 | 2 (1) | Escalated by phone after OM004 failure |
| S7 | Early/late/WIP | L7, R7, D8 H | 1 (1) | Wait 83.3 → 78.3 min |
| S8 | Human side | L7, R1, D11 D12 H | 5 (5) | 2 claims verified by gate, 1 contradicted, 1 outage denied — no false state change |

**S1 Options only (THR011 / DRV001 / SHP1017 / APT1017).** 10:02 "What slots are possible after 1 PM if I reach
around 12:45?" ETA rule → MEDIUM (+15). Triage → NO_ACTION. Dispatch (not_before 13:00) → PROPOSE SLOT-JAI-006
(D1 13:00); comparison D1/D2/D3 13:00, unload done 13:50. Message: "These are options, not bookings…". State
check: holds 0 → 0; APT1017 still CONFIRMED on SLOT-JAI-005.

**S2 Missing facts.** THR003/DRV013/SHP1013 09:19 "I am late by one hour." → LOW; triage CLARIFY ask DRIVER
(correct); no slot search. 09:25 "Reaching gate 11:00 for sure" → moved 60 min → MEDIUM, planning 11:15; triage
asked FACILITY "Can you confirm dock D2 will be available at 11:00?" (**miss**). Pending APT1013A (D2 11:00) is
unsafe; OM002 must not be sent. Dispatch → SLOT-JAI-019 D2 12:00 → HELD → REQUESTED. THR010/DRV004 09:31 "late by
45 minutes", no shipment: candidates SHP1004 (checked in 09:25) and SHP1020 (18:00) → CLARIFY ask DRIVER. Reply
"The evening one, 020. Gate by 18:45." Trace MSG016 → THR010 → DRV004 → SHP1020 → APT1020 (SLOT-JAI-025).
MEDIUM (+15) → 19:00 → SLOT-JAI-026 HELD → REQUESTED → facility accepts WH-JAI-9120 → CONFIRM after re-check →
APT-NEW-1020-583 replaces APT1020.

**S3 Rules.** Assessment SHP1010 (16 t reefer, ETA 10:55) → DOCK-JAI-D5, first D5 11:00. SHP1016 (31 t heavy, ETA
12:00) → DOCK-JAI-D6; dispatch SLOT-JAI-074 D6 12:30. Driver "D4 looks empty, put me there" → refusal citing 31,000
kg vs 25,000 kg and RULE004. RULE005 check on 21:00 starts. SHP1015 (15 t reefer, ETA 18:30) → NO_FEASIBLE_OPTION.
All 4 decisions right; all 4 had VERIFIED downgraded.

**S4 Competition (09:45).** SHP1014 CRITICAL ETA 11:25 HIGH; SHP1006 HIGH 11:20 "after 12"; SHP1012 NORMAL 11:10
leave by 13:30; SHP1013 NORMAL LOW; SHP1009 CRITICAL confirmed D4 11:00 (75 min). Audit: APT1014A overweight +
spills into SHP1017's slot; APT1009 needs D4 12:00; APT1006 will be missed. Release APT1014A and APT1006. Policy
order → SHP1014 → SHP1006 → SHP1012 → SHP1013. SHP1014 → D2 12:00+13:00 (SLOT-JAI-019+020); SHP1006 → D1 13:00;
SHP1012 → NO_FEASIBLE_OPTION → ESC-SHP1012 (offer later slot or move SHP1014 to D4 13:00 with approval); SHP1013
not held (LOW). Race on SLOT-JAI-020 among SHP1006/1012/1017: SHP1017 WON, others REJECTED, 1 live hold.
Standard-dock utilisation 11:00–14:00 = 70%.

**S5 World changes.** DEVT001 D3 breakdown; OM003 from facility (SHP1005 in yard since 09:05) — triage VERIFIED but
asked FACILITY "Is an alternative dock available?" (**miss**). APT1005 released; earliest D2 12:00 = 175-min wait →
held 30 min → ESC-SHP1005 (accept or approve 15-min overlap). Replay of APT1008 cancellation → no-op. THR001 09:34
"around 11:20, any slot after 12?" — triage asked FACILITY about slots (**miss**; also invented selected_id
wa-9001, nulled). Dispatch → D1 13:00 held 10 min (D2 12:00 held for SHP1005 is not offered). THR009 duplicate →
DUPLICATE. 09:44 hold expired. 09:45 coordinator approves SHP1005 → confirmed WH-JAI-9105. 09:46 SHP1013 holds D1
13:00 and requests it. 09:52 SHP1006 "YES book D1 13:00" → STALE (hold EXPIRED). Re-offer D3 13:00 (SLOT-JAI-034);
YES → REQUESTED; WhatsApp retry same key → DUPLICATE; 09:59 confirmed WH-JAI-9106.

**S6 No slot (16:24).** THR005/DRV015/SHP1015 "Evening traffic. ETA 6:30. Can the reefer unload tonight?" → 18:30,
moved 60 min → MEDIUM. Triage asked FACILITY "Can the reefer unload tonight…at D5?" (**miss**). Dispatch →
NO_FEASIBLE_OPTION. Blockers: SLOT-JAI-067–070 maintenance, RULE005. OM004 FAILED; no Jaipur night contact →
ESC-SHP1015 to the shift manager (CON003) by phone, deadline 17:30, options: approve D5 22:00 (RULE005 exception),
hold overnight with reefer on, divert to FAC-GGN-01 D3 (reefer). Driver told to keep the reefer running and wait.

**S7 Resequence.** CHK1003 early 08:20; facility: SHP1002 still on D2 (70 min, ≈09:45 finish); CHK1004 09:25 (within
30-min grace) → trigger. Before: SHP1003 dock-in 09:00, SHP1004 10:00 (treated as missed), SHP1005 12:00 → avg 83.3.
After: SHP1003 08:50 (D1 idle), SHP1004 09:55 (keeps D2 after 1002 clears + 10 min turnover), SHP1005 12:00 → 78.3.
Yard supervisor agrees. THR008 "has a new dock been assigned?" → NO_ACTION VERIFIED (gate record).

**S8 Human side.** THR007/DRV003 at gate with CHK1003 → VERIFIED; early arrival does not jump the queue. DRV012
"I'm at the gate" with no check-in → CLARIFY ask FACILITY, UNVERIFIED; gate: not checked in → polite reply, no
penalty. DRV013 "D2 is broken" → CLARIFY ask FACILITY; facility: D2 fine → no change (one ungrounded flag '9:20',
a false positive). THR012 "Is my 4 PM slot still active?" → NO_ACTION; SHP1019 cancelled (OM005).

## 7. Decision register summary (D1–D13)

| D | Decision | Where proven |
|---|---|---|
| D1 | Before options: shipment, ETA, ETA confidence, earliest start, latest finish. No slot search until known. | S1, S2 |
| D2 | Link by thread → driver → shipment; if the driver has several active loads and none is named, ask. | S2 (THR010 trace) |
| D3 | Confidence rule (`revise_eta`); LOW not planned; MEDIUM +15 min. | S1, S2, S6 |
| D4 | `SlotEngine.check` reasons list; run on SHP1010, SHP1016, SHP1015. | S3 |
| D5 | "Available" = no active appointment, no live hold, no overrun claim, no hard event. Shown ≠ reserved. | S1, S4, S5 |
| D6 | OPTION → HELD (10 min) → REQUESTED → CONFIRMED; RELEASED/EXPIRED. One current appointment per shipment. | S1, S2, S5 |
| D7 | Priority → fewest options → LOW not held → earliest report; dock tie → leave most options. | S4 |
| D8 | Recalculate on gate check-in, facility WIP update, verified dock event, confirmed ETA change ≥15 min. | S7 |
| D9 | Stale → STALE + re-offer; cancellation replay → no-op; duplicate message → DUPLICATE; retry → same key. | S5 |
| D10 | 13-field hand-off; failed channel retried on another channel; driver told next step. | S6 |
| D11 | Plain-language refusals with the rule and next best option. | S3, S5, S8 |
| D12 | Human approval: no slot, moving another booking, overlap approval, RULE005 exception, contradictions, penalties, safety. | S4, S5, S6, S8 |
| D13 | `race_for_slot`: 3 threads, 1 winner, 1 live hold. | S4 |

## 8. Evaluation methodology and metric definitions

**Agent evals (`nvidia_agent_evals.py`).** Synthetic payloads, one call per case per trial. Per case:
`earned = 0.4·points if safe (no error, no guard violation) + 0.6·points if decision correct (judged after
guard) − 0.1·points if ungrounded`; errors earn 0. Workflow score = 100 × Σearned / Σpoints. Outcome PASS /
PARTIAL / FAIL / ERROR. Consistency = share of trials matching the modal answer signature.

**Sandbox score (`sandbox.py`).** 50% decision accuracy + 30% × share of 5 safety invariants (no slot
double-booked, no shipment with two current bookings, FKs intact, no false dock outage applied, no infeasible
booking) + 20% on-plan arrival rate.

**Brief scenario measures (`brief_scenarios.run`).**

| Metric | Definition | Live value |
|---|---|---|
| agent_decision_accuracy | Share of agent calls with no error and all checks (action, ask_whom, selected, verification) passing on the guarded answer | 0.852 (23/27) |
| agent_calls / agent_errors | Calls made / calls that failed | 27 / 0 |
| guard_interventions | Calls where the guard changed anything (VERIFIED downgrade, nulled selection, blocked state change, fallback) | 13 |
| ungrounded_answers | Calls with cited evidence not in the input | 1 (false positive) |
| resolved_without_takeover | Share of scenarios with no escalation | 0.62 (5/8) |
| conflicting_allocations | Σ slots with two live claims + shipments with two current appointments | 0 |
| options_infeasible_at_recheck | Holds/requests/confirms whose re-check failed (stale caught) | 1 |
| escalation_completeness | Mean share of the 13 hand-off fields filled | 1.0 (3 escalations) |
| clarification_turns | Questions sent to drivers for missing facts | 2 |
| avg_turns_to_outcome | Mean driver + agent→driver messages per scenario | 5.4 |
| avg_minutes_to_outcome | Mean simulated minutes first→last step (S3 spans 09:38–16:24, which inflates it) | 81.8 |
| priority_violations | Pairs where a lower-priority truck got an earlier start that a higher-priority truck could have used | 0 |
| utilisation_11_14_standard | Used ÷ usable (not blocked) standard slots starting 11:00, 12:00 or 13:00, after S4's allocation | 0.70 (7/10) |
| avg_wait_before / after | Mean gate-in → dock-in for SHP1003/1004/1005 (S7) | 83.3 → 78.3 min |
| mean_agent_latency_s | Mean seconds per agent call | 21.8 |
| workflow_confidence | 100 × (0.5·accuracy + 0.3·[conflicts = 0] + 0.2·escalation completeness) | **92.6** |

Guard interventions in the run: 11 × "VERIFIED without independent confirmation → UNVERIFIED" (Dispatch and
Assessment roles that should say NOT_APPLICABLE) and 2 × invented `selected_id` (wa-9001, wa-9009). The 4 misses:
S2 THR003 reply, S5 OM003, S5 THR001, S6 THR005 — all triage asking FACILITY about dock/slot availability.

## 9. History of iterations and lessons learned

1. **Data exploration and instructions.** Read the guide/dictionary/ER; wrote `.github/copilot-instructions.md`;
   workflow diagram (`setuhaul_agent_workflow.md`) with human-factor safeguards. *Lesson: drivers' messages are
   reports; with no GPS, design for verification and follow-ups.*
2. **SQL probes.** Found `v_latest_eta` trusts LOW ETAs and `v_slot_availability` ignores dock events; confirmed
   the partial unique index blocks double-booking. Patched views at runtime, never the shipped SQL/DB.
3. **Deterministic simulation + scorecard** (`workflow_simulation.py`, `workflow_evals.py`, 100/100).
4. **NVIDIA agents, first eval: 10/100.** Cause: JSON parsing/content format — prose, `<think>` blocks, fences,
   truncation. Fix: JSON-object mode, 4096-token budget, think/fence stripping, schema validation, explicit
   truncation errors, retries on 429/5xx/network. *Lesson: make the output contract machine-checked before judging
   model quality.*
5. **Stress suite (6 cases): 55/100**, failures were status-label mismatches and dispatch picking a 60-min slot
   for a 90-min unload. Fix: new schema with separate verification/action/ask_whom, deterministic feasibility
   pre-filter, `guard_answer`, grounding check → **100/100** (18/18; rubric changed, not apples-to-apples).
   *Lesson: don't ask the model to compute feasibility — give it the feasible set.*
6. **Live sandbox, 15 minutes: 80.7/100**, safety 5/5 (100%), decisions 71% of 35, on-plan arrivals 75% of 12,
   7 duplicates suppressed, queue max 2 pending, 7.1-min mean wait. Found and fixed an orchestrator bug (facility
   status questions misread as arrival checks) and a broken-dock "fits current slot" bug. Problem: random personas
   repeated the same situation types and missed most of the brief's ladder.
7. **`brief_scenarios.py` + `slot_engine.py`:** one distinct scenario per brief problem from real seed rows, full
   hold/request/confirm state machine, idempotency, long-unload chains, race proof, coverage matrix, step-through
   HTML. **Live: 92.6/100, 23/27 as expected, 0 conflicts, 13 guard interventions, 4 misses (triage → FACILITY
   about availability).** *Lesson: coverage-driven scripted scenarios find different failures than random
   role-play.*
8. **Triage prompt changed** to leave availability to the scheduler and restrict FACILITY questions to confirming
   claims; registration numbers masked; `--repeats` consistency added. **Rerun pending.**

Cross-cutting lessons: guards earn their keep (13 catches in 27 calls); verification must be enforced in code;
latency (~22 s/call) is a product constraint, not just a cost; rubric scores need their formula stated next to them.
