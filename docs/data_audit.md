# Problem Definition Sheet and data audit (deliverable 1)

Source: `setuhaul_freight_operations.db` (snapshot 4 Aug 2026, Asia/Kolkata `+05:30`). Every query below was run
with `sqlite3 -header setuhaul_freight_operations.db "<query>"` against the shipped, unpatched snapshot. Outputs are pasted
unchanged.

## Problem definition

**Problem statement.** Delayed, early and uncertain trucks reach Jaipur DC (FAC-JAI-01) through driver chat. Today the
conversation, ETA and dock schedule are only loosely linked. As a result, drivers can be shown options that are already
infeasible, two trucks can contend for the same dock, and exceptions with no safe slot go out through channels that fail
silently. SetuHaul needs a conversational driver-exception service that collects the missing facts, offers only feasible
slots, and holds, requests, confirms and releases them without conflicts. It must also escalate to a human, with a
complete record, whenever no safe automatic outcome exists.

**Users.**

| User | Need |
|---|---|
| Truck drivers (15 in seed, low-typing, WhatsApp-style) | Short questions, a clear answer on where and when to unload, and no false promises. |
| Operations coordinators (5 per shift) | Ranked, de-duplicated alerts that include the evidence, not a stream of chats. |
| Facility staff (warehouse coordinator, gate supervisor, shift manager) | Confirm or reject requests, and report dock outages. |
| Carrier dispatch | Status of their trucks and cancellations. |

**Constraints.**

- Volume: 280-360 loads/day, 190-240 receiving appointments/day, 15-25 exception messages on a normal day, and 20-35 in a 30-minute spike.
- No live GPS. ETAs are original plan, driver-declared, warehouse or ops override. A driver message is a report, not ground truth.
- Physical rules: dock type (standard/reefer/heavy), dock weight limit, reefer to D5 (RULE003), more than 25,000 kg to the heavy dock (RULE004), no new start at or after 21:00 without approval (RULE005), 30-minute no-show grace (RULE002), and unload duration versus slot length.
- The LLM never decides capacity, vehicle-dock fit, ranking or commit status. Deterministic code (`slot_engine.py`, `guards.py`) decides. Safety, penalties, no-slot cases, contradictory data, regulated loads and emergencies go to a human.
- Seed SQL and `.db` are not modified. View gaps are patched at runtime (`workflow_simulation.apply_view_fixes`).

**Success measures** (from the brief). Turns and time to outcome. Share resolved without manual takeover. **Zero**
conflicting allocations. Options found infeasible at re-check are caught before commit. Escalation records are complete.
Average wait from gate-in to dock-in. Slot utilisation. Zero priority-policy violations. Clarification turns per thread.

## Data issues

### 1. Overweight loads booked on docks below their weight limit (APT1014A, and also APT1005)

APT1014A puts SHP1014 (21,000 kg) on D1 (20,000 kg limit). The query also found a second case: APT1005 puts SHP1005
(20,500 kg) on D3 (20,000 kg limit). Neither the schema nor the views compare load weight with dock capacity.
**Impact:** an unsafe or impossible dock assignment could be confirmed. For SHP1014 (CRITICAL), the pending request
would be rejected at the dock.

```sql
SELECT a.appointment_id, a.shipment_id, a.appointment_status, d.dock_code, d.max_vehicle_weight_kg, s.load_weight_kg
FROM appointments a JOIN shipments s USING(shipment_id) JOIN appointment_slots sl USING(slot_id)
JOIN docks d ON d.dock_id = sl.dock_id
WHERE a.is_current = 1 AND a.appointment_status IN ('PENDING_CONFIRMATION','CONFIRMED','IN_PROGRESS')
  AND s.load_weight_kg > d.max_vehicle_weight_kg;
```
```
appointment_id|shipment_id|appointment_status|dock_code|max_vehicle_weight_kg|load_weight_kg
APT1005|SHP1005|CONFIRMED|D3|20000|20500
APT1014A|SHP1014|PENDING_CONFIRMATION|D1|20000|21000
```

### 2. Unloads longer than their 60-minute slot overrun the next slot (APT1002, APT1005, APT1009, APT1014A)

Each appointment books exactly one slot. Four loads need 70-75 minutes in 60-minute slots. SHP1002 needs **70** min, not
75. Two of these overruns collide with a booked truck: APT1002 runs into APT1004 on D2, and APT1014A runs into APT1017 on
D1. The other two spill into slots that the view reports as free or blocked. **Impact:** the next truck waits, and
SLOT-JAI-047 is offered as `AVAILABLE` even though D4 is still busy until 12:15.

```sql
SELECT a.appointment_id, s.expected_unload_min, d.dock_code, substr(sl.slot_start_ts,12,5) AS slot_start,
       substr(sl.slot_end_ts,12,5) AS slot_end, nx.slot_id AS next_slot, COALESCE(na.appointment_id,'-') AS next_slot_booking
FROM appointments a JOIN shipments s USING(shipment_id) JOIN appointment_slots sl USING(slot_id)
JOIN docks d ON d.dock_id = sl.dock_id
JOIN appointment_slots nx ON nx.dock_id = sl.dock_id AND nx.slot_start_ts = sl.slot_end_ts
LEFT JOIN appointments na ON na.slot_id = nx.slot_id AND na.appointment_status IN ('PENDING_CONFIRMATION','CONFIRMED','IN_PROGRESS')
WHERE a.is_current = 1 AND a.appointment_status IN ('PENDING_CONFIRMATION','CONFIRMED','IN_PROGRESS')
  AND CAST(ROUND((julianday(sl.slot_end_ts) - julianday(sl.slot_start_ts)) * 1440) AS INTEGER) < s.expected_unload_min;
```
```
appointment_id|expected_unload_min|dock_code|slot_start|slot_end|next_slot|next_slot_booking
APT1002|70|D2|08:00|09:00|SLOT-JAI-016|APT1004
APT1005|75|D3|09:00|10:00|SLOT-JAI-031|-
APT1009|75|D4|11:00|12:00|SLOT-JAI-047|-
APT1014A|75|D1|11:00|12:00|SLOT-JAI-005|APT1017
```

### 3. OM004 failed, and FAC-JAI-01 has no night-shift contact

The no-slot escalation for SHP1015 was emailed to `night.shift@setuhaul.example`. That address is not in
`facility_contacts`, and the delivery failed. The only night-shift contact, CON005, belongs to FAC-GGN-01, is vacant and
has no email. In addition, APT1015A is still `CONFIRMED` for 17:00, but the ETA is now 18:30. **Impact:** a HIGH-priority
reefer load has no slot and no one received the escalation. A system that only checks the appointment status sees
"confirmed".

```sql
SELECT om.operational_message_id, om.recipient_address, om.delivery_status,
       (SELECT COUNT(*) FROM facility_contacts c WHERE c.email = om.recipient_address) AS matching_contacts
FROM operational_messages om WHERE om.operational_message_id = 'OM004';
```
```
operational_message_id|recipient_address|delivery_status|matching_contacts
OM004|night.shift@setuhaul.example|FAILED|0
```
```sql
SELECT contact_id, facility_id, contact_role, contact_name, COALESCE(email,'NULL') AS email
FROM facility_contacts WHERE contact_role LIKE '%NIGHT%' OR facility_id = 'FAC-JAI-01';
```
```
contact_id|facility_id|contact_role|contact_name|email
CON001|FAC-JAI-01|WAREHOUSE_COORDINATOR|Neha Bansal|neha.bansal@setuhaul.example
CON002|FAC-JAI-01|GATE_SUPERVISOR|Mahesh Verma|mahesh.verma@setuhaul.example
CON003|FAC-JAI-01|SHIFT_MANAGER|Anita Joshi|anita.joshi@setuhaul.example
CON005|FAC-GGN-01|NIGHT_SHIFT_COORDINATOR|Vacant Position|NULL
```
```sql
SELECT a.appointment_id, a.appointment_status, substr(sl.slot_start_ts,12,5) AS slot_start, substr(le.effective_eta_ts,12,5) AS eta
FROM appointments a JOIN appointment_slots sl USING(slot_id) JOIN v_latest_eta le USING(shipment_id)
WHERE a.appointment_id = 'APT1015A';
```
```
appointment_id|appointment_status|slot_start|eta
APT1015A|CONFIRMED|17:00|18:30
```

### 4. Pending requests have no warehouse confirmation, and OM002 was never sent (SHP1013, SHP1014)

APT1013A and APT1014A are `PENDING_CONFIRMATION`, with no `confirmed_at` and no `warehouse_confirmation_ref`. OM002, the
request for APT1013A, has been `QUEUED` since 09:32. OM001 is `SENT`, not `DELIVERED`, and has no reply. The pending
rows still occupy slots through the unique index. **Impact:** capacity stays locked indefinitely because nothing expires a
pending request, and a driver could read "pending" as "booked".

```sql
SELECT a.appointment_id, a.shipment_id, a.slot_id, a.booked_at, COALESCE(a.confirmed_at,'NULL') AS confirmed_at,
       COALESCE(a.warehouse_confirmation_ref,'NULL') AS wh_ref, om.operational_message_id, om.delivery_status
FROM appointments a LEFT JOIN operational_messages om USING(appointment_id)
WHERE a.appointment_status = 'PENDING_CONFIRMATION';
```
```
appointment_id|shipment_id|slot_id|booked_at|confirmed_at|wh_ref|operational_message_id|delivery_status
APT1013A|SHP1013|SLOT-JAI-018|2026-08-04T09:32:00+05:30|NULL|NULL|OM002|QUEUED
APT1014A|SHP1014|SLOT-JAI-004|2026-08-04T09:52:00+05:30|NULL|NULL|OM001|SENT
```

### 5. Low-confidence and range ETAs are treated as firm (ETA008, ETA012)

ETA008 ("late by one hour") and ETA012 ("between 12:30 and 13:00", stored as the midpoint 12:45) are `LOW` confidence.
The seed `v_latest_eta` still returns them as the effective ETA, and `shipments.latest_eta_ts` mirrors them. **Impact:**
slots are filtered against a guess. SHP1013's pending 11:00 request rests entirely on this low-confidence ETA.

```sql
SELECT e.eta_update_id, e.shipment_id, e.confidence_code, substr(e.declared_eta_ts,12,5) AS declared, e.note,
       substr(le.effective_eta_ts,12,5) AS view_eta, le.eta_confidence, substr(s.latest_eta_ts,12,5) AS shipments_latest
FROM eta_updates e JOIN v_latest_eta le USING(shipment_id) JOIN shipments s USING(shipment_id)
WHERE e.confidence_code = 'LOW';
```
```
eta_update_id|shipment_id|confidence_code|declared|note|view_eta|eta_confidence|shipments_latest
ETA008|SHP1013|LOW|11:00|Driver said only: late by one hour.|11:00|LOW|11:00
ETA012|SHP1017|LOW|12:45|Driver expects arrival between 12:30 and 13:00.|12:45|LOW|12:45
```

With `apply_view_fixes`, the patched view drops LOW driver claims and falls back to the original plan. SHP1013 then
returns 10:00 `ORIGINAL_PLAN` and SHP1017 returns 12:15 `ORIGINAL_PLAN`, and the claims are sent for clarification.

### 6. `v_slot_availability` ignores `dock_status_events`

None of the seed views reference `dock_status_events`. Outages are visible only where someone also set
`slot_status = 'BLOCKED'` by hand. **Check of the claim in the brief:** no slot overlapping DEVT001 or DEVT003 is
currently shown `AVAILABLE`. They are either manually `BLOCKED` or `OCCUPIED`. The gap is latent but real. SLOT-JAI-030
overlaps the DEVT001 breakdown (09:15-13:00) yet is still `OPEN`, so it would appear `AVAILABLE` as soon as APT1005 is
moved.

```sql
SELECT COUNT(*) AS views_referencing_dock_events FROM sqlite_master WHERE type='view' AND sql LIKE '%dock_status_events%';
```
```
views_referencing_dock_events
0
```
```sql
SELECT ev.dock_event_id, ev.event_type, va.slot_id, va.dock_code, substr(va.slot_start_ts,12,5) AS slot_start,
       va.availability_status, COALESCE(va.appointment_id,'-') AS appointment_id
FROM dock_status_events ev
JOIN appointment_slots sl ON sl.dock_id = ev.dock_id AND ev.event_start_ts < sl.slot_end_ts AND ev.event_end_ts > sl.slot_start_ts
JOIN v_slot_availability va ON va.slot_id = sl.slot_id ORDER BY ev.dock_event_id, va.slot_start_ts;
```
```
dock_event_id|event_type|slot_id|dock_code|slot_start|availability_status|appointment_id
DEVT001|BREAKDOWN|SLOT-JAI-030|D3|09:00|OCCUPIED|APT1005
DEVT001|BREAKDOWN|SLOT-JAI-031|D3|10:00|BLOCKED|-
DEVT001|BREAKDOWN|SLOT-JAI-032|D3|11:00|BLOCKED|-
DEVT001|BREAKDOWN|SLOT-JAI-033|D3|12:00|BLOCKED|-
DEVT002|MAINTENANCE|SLOT-JAI-067|D5|18:00|BLOCKED|-
DEVT002|MAINTENANCE|SLOT-JAI-068|D5|19:00|BLOCKED|-
DEVT002|MAINTENANCE|SLOT-JAI-069|D5|20:00|BLOCKED|-
DEVT002|MAINTENANCE|SLOT-JAI-070|D5|21:00|BLOCKED|-
DEVT003|CAPACITY_REDUCTION|SLOT-JAI-015|D2|08:00|OCCUPIED|APT1002
DEVT003|CAPACITY_REDUCTION|SLOT-JAI-016|D2|09:00|OCCUPIED|APT1004
```
```sql
SELECT sl.slot_id, sl.slot_status, ev.dock_event_id, ev.event_type FROM appointment_slots sl
JOIN dock_status_events ev ON ev.dock_id = sl.dock_id AND ev.event_start_ts < sl.slot_end_ts AND ev.event_end_ts > sl.slot_start_ts
WHERE sl.slot_status = 'OPEN' AND ev.event_type IN ('BREAKDOWN','MAINTENANCE');
```
```
slot_id|slot_status|dock_event_id|event_type
SLOT-JAI-030|OPEN|DEVT001|BREAKDOWN
```

### 7. Active appointments sit on slots that overlap dock events (APT1005 on D3, and APT1002/APT1004 on D2)

APT1005 is still `CONFIRMED` on D3 during the DEVT001 breakdown, while its truck waits with
`WAITING_DOCK_UNAVAILABLE`. On D2, DEVT003 (a capacity reduction from 08:00 to 09:20) overlaps APT1002, which is in
progress, and APT1004, which is confirmed. No constraint links bookings to dock events. **Impact:** the schedule says
"confirmed" for a dock that cannot be used, and the WAITING truck needs reassignment (OM003 asked for one).

```sql
SELECT a.appointment_id, a.shipment_id, a.appointment_status, ev.dock_event_id, ev.event_type,
       substr(ev.event_start_ts,12,5)||'-'||substr(ev.event_end_ts,12,5) AS event_window, fc.queue_state
FROM appointments a JOIN appointment_slots sl USING(slot_id)
JOIN dock_status_events ev ON ev.dock_id = sl.dock_id AND ev.event_start_ts < sl.slot_end_ts AND ev.event_end_ts > sl.slot_start_ts
LEFT JOIN facility_checkins fc ON fc.shipment_id = a.shipment_id
WHERE a.is_current = 1 AND a.appointment_status IN ('PENDING_CONFIRMATION','CONFIRMED','IN_PROGRESS');
```
```
appointment_id|shipment_id|appointment_status|dock_event_id|event_type|event_window|queue_state
APT1005|SHP1005|CONFIRMED|DEVT001|BREAKDOWN|09:15-13:00|WAITING_DOCK_UNAVAILABLE
APT1002|SHP1002|IN_PROGRESS|DEVT003|CAPACITY_REDUCTION|08:00-09:20|IN_DOCK
APT1004|SHP1004|CONFIRMED|DEVT003|CAPACITY_REDUCTION|08:00-09:20|WAITING_LATE
```

### 8. What the two partial unique indexes do NOT cover

`ux_active_appointment_per_slot` allows at most one active row per `slot_id`. `ux_current_active_appointment_per_shipment`
allows at most one current active row per `shipment_id`. There are no triggers. Neither index checks the following:

| Gap | Evidence |
|---|---|
| Dock weight and type fit | Issue 1. In-memory demo below: a 31,000 kg HEAVY load is accepted on standard D4. |
| Multi-slot (overrunning) unloads | Issue 2. The index sees SLOT-JAI-004 and SLOT-JAI-005 as two separate slots. |
| Dock events versus bookings | Issues 6 and 7. |
| Holds and in-flight requests | There is no hold table, so an option shown to driver A can still be booked by driver B. |
| Cancelled shipments with a current appointment | Not enforced. The current data is clean (query 8a). |
| Cross-facility booking (slot facility versus shipment destination) | Not enforced. The current data is clean (query 8b). |
| Pending rows with no expiry | Issue 4. |
| Duplicate exceptions | `dedupe_key` is not unique (issue 10). |

```sql
-- 8a
SELECT a.appointment_id, s.shipment_id, s.current_status, a.appointment_status, a.is_current
FROM appointments a JOIN shipments s USING(shipment_id) WHERE s.current_status = 'CANCELLED';
```
```
appointment_id|shipment_id|current_status|appointment_status|is_current
APT1008|SHP1008|CANCELLED|CANCELLED|0
APT1019|SHP1019|CANCELLED|CANCELLED|0
```
```sql
-- 8b
SELECT COUNT(*) AS cross_facility_bookings FROM appointments a JOIN shipments s USING(shipment_id)
JOIN appointment_slots sl USING(slot_id) WHERE sl.facility_id <> s.destination_facility_id;
```
```
cross_facility_bookings
0
```
```sql
-- 8c
SELECT name, sql FROM sqlite_master WHERE type='trigger';
```
```
(no rows)
```

The next demo runs on an in-memory copy, so the snapshot is not modified. Both indexes accept a 31,000 kg HEAVY load on
standard D4 (25,000 kg limit). The slot it lands on, SLOT-JAI-047, is the same one APT1009's unload overruns:

```
sqlite3 :memory: ".restore setuhaul_freight_operations.db" "PRAGMA foreign_keys=ON;" \
 "INSERT INTO appointments(appointment_id,shipment_id,slot_id,appointment_status,booking_source,is_current,booked_at,updated_at)
  VALUES ('APT-DEMO','SHP1016','SLOT-JAI-047','CONFIRMED','DRIVER_CHAT',1,'2026-08-04T10:00:00+05:30','2026-08-04T10:00:00+05:30');" \
 "SELECT a.appointment_id, s.load_weight_kg, s.required_dock_type, d.dock_code, d.dock_type, d.max_vehicle_weight_kg
  FROM appointments a JOIN shipments s USING(shipment_id) JOIN appointment_slots sl USING(slot_id)
  JOIN docks d ON d.dock_id = sl.dock_id WHERE a.appointment_id='APT-DEMO';"
```
```
APT-DEMO|31000|HEAVY|D4|STANDARD|25000
```

### 9. THR010 thread has no shipment

THR010 and its exception EXC010 have `shipment_id` NULL. DRV004 has two open shipments, SHP1004 and SHP1020.
**Impact:** any slot lookup would have to guess the shipment. The agent must ask the driver first (MSG017 does this).

```sql
SELECT t.thread_id, t.driver_id, COALESCE(t.shipment_id,'NULL') AS thread_shipment, e.exception_id,
       COALESCE(e.shipment_id,'NULL') AS exc_shipment,
       (SELECT group_concat(shipment_id) FROM shipments s WHERE s.driver_id = t.driver_id
          AND s.current_status NOT IN ('COMPLETED','CANCELLED')) AS candidate_shipments
FROM chat_threads t LEFT JOIN driver_exceptions e USING(thread_id) WHERE t.shipment_id IS NULL;
```
```
thread_id|driver_id|thread_shipment|exception_id|exc_shipment|candidate_shipments
THR010|DRV004|NULL|EXC010|NULL|SHP1004,SHP1020
```

### 10. Duplicate message MSG015 created a second thread and exception

MSG015 has the same text as MSG001. It arrived 62 s later in a new thread (THR009) with a different `external_message_id`.
It is flagged `is_duplicate=1`, but EXC009 was still created with the same `dedupe_key` as EXC001. The only unique index on
`driver_exceptions` is the primary key. **Impact:** without deduplication before triage, one delay creates two alerts and
two slot searches.

```sql
SELECT m.chat_message_id, m.thread_id, m.external_message_id, m.message_ts, m.is_duplicate, e.exception_id,
       e.exception_status, e.dedupe_key
FROM chat_messages m JOIN driver_exceptions e USING(thread_id)
WHERE m.sender_type = 'DRIVER' AND m.message_text = (SELECT message_text FROM chat_messages WHERE chat_message_id = 'MSG015');
```
```
chat_message_id|thread_id|external_message_id|message_ts|is_duplicate|exception_id|exception_status|dedupe_key
MSG001|THR001|wa-9001|2026-08-04T09:34:00+05:30|0|EXC001|NEEDS_INFORMATION|DRV006-SHP1006-20260804-0934
MSG015|THR009|wa-9009|2026-08-04T09:35:02+05:30|1|EXC009|DUPLICATE|DRV006-SHP1006-20260804-0934
```
```sql
SELECT name FROM sqlite_master WHERE tbl_name = 'driver_exceptions' AND type = 'index';
```
```
name
sqlite_autoindex_driver_exceptions_1
ix_exceptions_status_time
```

### 11. In-transit shipments with no active appointment

SHP1012 and SHP1016 had their appointments cancelled after their ETAs moved, and no replacement exists. SHP1018's
appointment is `NO_SHOW`, yet the shipment is still `ASSIGNED`. SHP1021 never had an appointment. **Impact:** these trucks
are invisible to any availability view that starts from slots. They must be found from the shipment side.

```sql
SELECT s.shipment_id, s.current_status, le.eta_source, substr(le.effective_eta_ts,12,5) eta
FROM shipments s JOIN v_latest_eta le USING(shipment_id)
LEFT JOIN appointments a ON a.shipment_id = s.shipment_id AND a.is_current = 1
     AND a.appointment_status IN ('PENDING_CONFIRMATION','CONFIRMED','IN_PROGRESS')
WHERE a.appointment_id IS NULL AND s.current_status NOT IN ('COMPLETED','CANCELLED');
```
```
shipment_id|current_status|eta_source|eta
SHP1012|IN_TRANSIT|DRIVER_DECLARED|11:10
SHP1016|IN_TRANSIT|DRIVER_DECLARED|12:00
SHP1018|ASSIGNED|ORIGINAL_PLAN|11:50
SHP1021|ASSIGNED|ORIGINAL_PLAN|14:00
```

## Summary

| # | Issue | Severity | How the prototype handles it |
|---|---|---|---|
| 1 | Overweight on D1 (APT1014A) and D3 (APT1005) | High | `slot_engine.SlotEngine.check` rejects any slot with `load_weight_kg > max_vehicle_weight_kg` and applies the RULE004 heavy-dock rule. |
| 2 | 70-75-min unloads overrun 60-min slots | High | `SlotEngine.check` chains the following slot(s) for a long unload, and `overrun_claims` marks SLOT-JAI-016/031/047/005 as taken. |
| 3 | OM004 failed, no FAC-JAI-01 night contact, stale CONFIRMED APT1015A | High | S6 in `brief_scenarios.py` finds no feasible slot and escalates by phone to CON003 with a hand-off record. In the sandbox, `guards.review_priority`/`CoordinatorQueue` rank such alerts. |
| 4 | Pending requests unconfirmed; OM002 never sent | Medium | Holds expire after `HOLD_TTL_MIN` (10 min). REQUESTED is separate from CONFIRMED, and `confirm` re-checks feasibility before writing. |
| 5 | LOW and range ETAs used as firm | Medium | `workflow_simulation.apply_view_fixes` ignores LOW driver ETAs in `v_latest_eta`. The agent asks a clarifying question first (S2, S4). |
| 6 | Views ignore dock events | High | `apply_view_fixes` blocks slots that overlap BREAKDOWN, MAINTENANCE or MANUAL_BLOCK. `SlotEngine.check` also tests `HARD_EVENTS` directly. |
| 7 | Bookings on slots that overlap dock events | High | `SlotEngine.check` and `board` flag them (the board shows `BOOKED!`). Outages apply only after facility confirmation (`guards`). CAPACITY_REDUCTION is advisory only. |
| 8 | Index gaps (fit, multi-slot, holds, events) | High | The `slot_holds` table and `ux_live_hold_per_slot`, plus a `BEGIN IMMEDIATE` re-check in `hold` and `confirm`. `race_for_slot` (D13) proves one winner. |
| 9 | THR010 has no shipment | Medium | Triage returns CLARIFY/DRIVER, and no tool call is made until the shipment is linked (S2). |
| 10 | Duplicate MSG015 / non-unique `dedupe_key` | Low | Exact duplicates are suppressed before any agent call. `processed_messages` makes requests idempotent (S5). `CoordinatorQueue` merges repeat alerts. |
| 11 | In-transit shipments with no appointment | Medium | Shipment-side lookup through `SlotEngine.shipment` and `check`. The competition policy (S4) ranks them with other open exceptions. |
