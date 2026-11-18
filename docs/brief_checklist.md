# SetuHaul Logistics FDE Challenge — brief checklist (source: 21182914-SetuHaul_Logistics_FDE_Challenge_Project_Brief.pdf in repo root)

Ask: a conversational driver-exception service with dock-slot hold, confirm and release, plus concurrency proof,
escalation, scenario run, data audit, decision register and client readout.
Ops volume: 280-360 loads/day, 190-240 receiving appointments/day, 15-25 exception msgs normal day, 20-35 in a 30-min spike,
5 coordinators/shift, no live GPS.

Data: SQLite snapshot 4 Aug 2026 (Asia/Kolkata +05:30), 18 tables (carriers 4, drivers 15, vehicle_types 5, vehicles 15,
shipments 21, eta_updates 12, facilities 2, docks 9, facility_rules 6, appointment_slots 106, appointments 20,
dock_status_events 3, facility_checkins 5, chat_threads 12, chat_messages 20, driver_exceptions 10, operational_messages 5,
facility_contacts 5). Views: v_latest_eta, v_slot_availability, v_inbound_operational_state, v_current_facility_queue.
Partial unique indexes: one active appointment per slot; one current active appointment per shipment. Audit must ask what
these indexes do NOT cover.

Ladder of situations:
L1 Options-only request: THR011, SHP1017 (asking kept separate from booking)
L2 Missing facts: THR003, SHP1013, THR010, DRV004 (ask before tool call)
L3 Rules/compatibility narrow choices: SHP1010, SHP1015, SHP1016, RULE005
L4 Several delayed trucks chase nearby slots: SHP1006, SHP1012, SHP1013, SHP1014, SHP1009 (defensible allocation policy)
L5 World changes mid-conversation: SHP1005, APT1008, THR001, THR009, SHP1002 (recovery, safe retries)
L6 No feasible slot: SHP1015, THR005, OM004 (escalation)
L7 Optional: SHP1002, SHP1003, SHP1004, SHP1006 (early, late, WIP share a facility)

Scenario run must show: R1 delay report + clarification; R2 later options + comparison; R3 several requests vs same
schedule; R4 >=2 competing for same capacity; R5 option changes/disappears; R6 escalation, no safe automatic outcome;
R7 optional new sequence after ETA update / gate check-in.

Six deliverables: 1 Problem Definition Sheet + data audit with a proving query per issue; 2 Decision register;
3 Working prototype with library versions in README; 4 Scenario run; 5 Integrity proof (concurrency test);
6 Client readout. Plus optional scheduling tool.

13 decisions: D1 fields to collect before showing options (+3 example chats); D2 conversation -> driver/shipment/appointment
linking (+ message-to-rows trace); D3 revised ETA & confidence (+ rule applied to 2 messages); D4 slot feasibility
(+ written check run on 3 shipments); D5 what "available" means while another driver decides; D6 hold/request/
reservation/confirmation states; D7 ordering of simultaneous requests (+ policy tested on competing trucks);
D8 optional when schedule recalculates; D9 stale options, cancellations, duplicates, retries (+ one replayed case each);
D10 no-slot escalation message + hand-off record; D11 two plain-language refusal messages; D12 decisions needing human
approval + trigger for each; D13 concurrency test and result.

Measures: turns & time to outcome; share resolved w/o manual takeover; conflicting allocations; options later infeasible
(re-check at confirm); completeness of escalation records; avg waiting gate-in->dock-in; slot utilisation;
priority-policy violations; clarification turns.

Boundaries: agent never decides capacity, vehicle-dock fit, ranking or commit status alone. Human handles safety,
penalties, no-slot cases, contradictory data, regulated loads, emergencies.

Jaipur chart: D1-D4 Standard, D5 Reefer, D6 Heavy. D3 breakdown ~10:00-13:00 (DEVT001 09:15-13:00), D5 maintenance
18:00-22:00, D2 reduced capacity 08:00-09:20. 1018 no-show; 1008, 1016, 1019 cancelled; 1014 and 1013 pending.

Data issues found so far: APT1014A (SHP1014 21,000 kg) pending on D1 (20,000 kg limit); 75-min unloads in 60-min slots
overrun next slot (APT1002->SLOT-JAI-016, APT1005->031, APT1009->047, APT1014A->005); OM004 FAILED to night.shift address,
FAC-JAI-01 has no night contact (CON005 is GGN and vacant); seed views ignore dock events / trust low-confidence ETAs
(patched at runtime by workflow_simulation.apply_view_fixes); OM002 QUEUED never sent; ETA012 is a range (12:30-13:00).
