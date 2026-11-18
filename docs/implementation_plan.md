# SetuHaul driver-exception service — Implementation Plan

Baseline (from `docs/spec.md`): deterministic `slot_engine.py` with re-checked hold/request/confirm,
guards, three LLM roles, scripted brief run at 92.6/100 (accuracy 85.2 %, 0 conflicts, escalation
completeness 100 %), sandbox 80.7, stress suite 100/100. Gaps: agent advice does not drive the brief
flow, Triage over-asks the facility, holds are not persisted in the real DB, no service/channel,
no coordinator UI, latency 21.8 s/call.

Effort: S ≤ 2 days, M 3-5 days, L 1-2 weeks (one engineer). Priorities: P0 = needed for a credible
shadow pilot; P1 = needed for assisted operation; P2 = scale.

## Summary

| ID | Item | Pri | Effort | Status | Depends on |
|---|---|---|---|---|---|
| P0.1 | Agent decisions drive the flow (closed-loop brief run) | P0 | M | planned | — |
| P0.2 | Measured Triage prompt fix | P0 | S | planned | P0.1 (for measurement) |
| P0.3 | Role-correct verification + ID normalisation in guards | P0 | S | planned | — |
| P0.4 | Single feasibility source for agents | P0 | S | planned | — |
| P0.5 | Engine hardening (request race, release_hold, atomic idempotency) | P0 | S | planned | — |
| P1.1 | `service.py` stdlib HTTP service: hold/request/confirm/release | P1 | M | **in progress (separate agent)** | P0.5 |
| P1.2 | WhatsApp-style inbound webhook + outbound sender | P1 | M | planned | P1.1 |
| P1.3 | Persist holds/idempotency/audit in the real DB (migration) | P1 | M | planned | P0.5 |
| P1.4 | Coordinator console | P1 | L | planned | P1.1, P1.3 |
| P1.5 | PII redaction layer + tests | P1 | S | planned | — |
| P1.6 | Observability (structured logs, metrics, traces) | P1 | M | planned | P1.1 |
| P1.7 | Load test: 35 messages / 30 min | P1 | S | planned | P1.1, P1.2 |
| P2.1 | Real-time ETA feeds (telematics / carrier APIs) | P2 | L | planned | P1.3 |
| P2.2 | Multi-facility, multi-day scheduling | P2 | L | planned | P1.3 |
| P2.3 | Learned ETA confidence | P2 | M | planned | P2.1 |

---

## P0 — make the agent's decisions count and measurable

### P0.1 Closed-loop brief evaluation (agent decisions drive the flow)
Today `brief_scenarios.py` scripts each scenario path; `Runner.ask` scores the agent but the next
step does not depend on the guarded answer. `sandbox.py:_act_on_driver` is closed-loop but books
through `Sandbox._book`, not `SlotEngine`.

Tasks
- Add `brief_scenarios.py:route(guarded, sc, now)` that maps the guarded answer to the engine action:
  `PROPOSE` + `selected_id` → `SlotEngine.hold`; `CLARIFY/DRIVER` → driver question turn (scripted
  reply from the seed thread); `CLARIFY/FACILITY` → facility check step; `ESCALATE` →
  `Scenario.escalate`; `NO_FEASIBLE_OPTION` → escalation with `HANDOFF_FIELDS`; `DUPLICATE` → no-op.
- Keep the scripted path as the expected trajectory; record divergence per step
  (`metrics["trajectory_divergence"]`) and still enforce invariants via `SlotEngine.conflicts`.
- Switch `sandbox.py:_book/_reslot` to `SlotEngine.hold/request/confirm` so both harnesses share the
  state machine.
- Add `--closed-loop` flag; offline mode must reproduce today's outcomes exactly.

Acceptance
- `python3 brief_scenarios.py --offline --closed-loop` yields the same outcomes and coverage as
  today; 0 conflicts.
- Live closed-loop run reports accuracy, divergence and conflicts; any wrong agent decision is
  visibly contained (escalation or re-ask), never a conflict.
- New test in `tests/test_brief_scenarios.py`: a deliberately wrong offline answer (e.g. selected ID
  outside feasible set) is guarded and routed to escalation.

Risks: scenario scripts encode human replies tied to the expected path → keep a small reply table
per scenario keyed by question type.

### P0.2 Measured Triage prompt fix
All 4 misses in the live brief run are Triage choosing `CLARIFY/FACILITY` when the input already
holds a facility message (S5 OM003), a verified dock event (S5 DEVT001), or a clear clock-time ETA
(S2, S6).

Tasks
- Add the 4 failing payloads from `brief_scenarios.json` as stress cases in
  `nvidia_agent_evals.py:STRESS_CASES` (synthetic IDs only) — fail first.
- Amend `SYSTEM_PROMPTS[AgentRole.TRIAGE]`: "If the message *is from* the facility, or a verified
  dock event / gate check-in is present, do not ask the facility again; PROPOSE (re-plan) or
  ESCALATE." Add "no-slot reefer with clear ETA → PROPOSE so dispatch can prove infeasibility".
- Update `sandbox.Sandbox._expectation` and `Expect` objects in `brief_scenarios.py` consistently
  (per `.github/copilot-instructions.md`).
- Run `--suite all --repeats 3` before/after; keep both JSON reports.

Acceptance: brief accuracy ≥ 95 % (≥ 26/27) across 3 repeats; stress suite stays 100; no new
facility over-asks in sandbox (`facility_requests_not_needed` ≤ 2).
Effort S. Risk: over-correction (Triage stops verifying driver outage claims) — guarded by existing
S8 cases.

### P0.3 Role-correct verification and ID normalisation
11 of 13 guard interventions are dispatch/assessment returning `VERIFIED`; 2 are lowercase message
IDs (`wa-9001`) rejected by `guards._ID`.

Tasks
- In `guards.guard_answer`, accept a `role` argument: non-triage roles get `verification` coerced to
  `NOT_APPLICABLE` (count as a soft normalisation, not a safety violation).
- Tighten DISPATCH/ASSESSMENT prompts; give payload message IDs as uppercase or extend the ID check
  to exact-match any string ID present in the input.
- Unit tests in `tests/test_nvidia_agents.py` for both.

Acceptance: brief run guard interventions ≤ 2, all of them real safety catches.

### P0.4 Single feasibility source
`guards.feasible_slot_ids` ignores dock events, facility rules and overruns; `SlotEngine.check` is
authoritative.

Tasks: brief/sandbox/service build `deterministic_feasibility.feasible_ids` only from
`SlotEngine.feasible`; keep `guards.feasible_slot_ids` for synthetic eval payloads, documented as such;
add a parity test on seed data for cases both can express.
Acceptance: no code path offers an agent an ID the engine would reject at `hold`.

### P0.5 Engine hardening
Tasks
- `SlotEngine.request`: move the state read inside `BEGIN IMMEDIATE` and use
  `UPDATE … WHERE group_key=? AND state='HELD'`, checking rowcount.
- Write `processed_messages` in the same transaction as the action it guards (request, confirm,
  release), so a crash cannot record a key without the effect or vice versa.
- Add `SlotEngine.release_hold(group, now, reason)` (driver declines / coordinator frees).
- Add a `race_for_request` / `race_for_confirm` variant of `race_for_slot`.

Acceptance: new concurrency tests pass 100 runs in a loop; `conflicts()` all zero.

---

## P1 — a service operators can use

### P1.1 HTTP service (`service.py`) — **in progress, separate agent**
Stdlib `http.server` wrapper over `SlotEngine`.

Expected endpoints
- `GET  /shipments/{id}/options?eta=HH:MM&not_before=&finish_by=` → feasible + top infeasible with
  reasons (no state change).
- `POST /holds` `{shipment_id, slot_id, eta}` → `201 {group_key, expires}` | `409 {reasons}`.
- `POST /holds/{group}/request` `{request_key}` → `REQUESTED` | `DUPLICATE` (200) | `STALE` (409).
- `POST /holds/{group}/confirm` `{eta, warehouse_ref}` → `{appointment_id}` | `409 {reasons}`.
- `POST /appointments/{id}/release` `{reason, idempotency_key}`; `GET /board`, `GET /health`,
  `GET /integrity` (→ `conflicts()`).

Acceptance: integration tests hit every endpoint, including concurrent `POST /holds` on one slot →
exactly one 201; all keys idempotent; JSON errors never leak stack traces.
Risk: `ThreadingHTTPServer` + one shared connection — use a connection per request/thread.

### P1.2 Messaging webhook (WhatsApp-style)
Tasks: `POST /webhook/messages` (verify signature header, dedupe on provider message ID via
`processed_messages`, ack in < 2 s, enqueue); worker: link (D2) → `revise_eta` → Triage → guards →
engine → reply template; outbound sender with delivery receipts updating `delivery_status`;
template library for D11 refusals and D10 driver message.
Acceptance: replayed webhook is a no-op; reply contains only engine-validated slots; failed
delivery triggers fallback channel.
Risk: provider template approval lead time; message ordering — process per-driver serially.

### P1.3 Persist holds, idempotency and audit in the real DB
Tasks
- Migration `migrations/001_slot_holds.sql`: `slot_holds`, `ux_live_hold_per_slot`,
  `processed_messages`, new `audit_log(event_id, ts, actor, entity, entity_id, from_state,
  to_state, idempotency_key, detail_json)`, `escalations` table with the 13 `HANDOFF_FIELDS`.
- Convert holds from minutes-since-midnight to ISO-8601 `+05:30` timestamps.
- Fold the two `apply_view_fixes` patches into real view definitions in the migration (seed file
  stays as the original snapshot per repo convention); update `setuhaul_data_dictionary.csv`,
  database guide and ER diagram.
- `SlotEngine(db_path=…, seed=False)` must work against the migrated `.db`.
Acceptance: `PRAGMA integrity_check` ok, `foreign_key_check` empty, active-slot uniqueness query
empty; engine tests pass against the migrated file DB.
Risk: migration on a live DB — run as a forward-only, transactional script with backup.

### P1.4 Coordinator console
Tasks: server-rendered pages from `service.py` — ranked queue (`CoordinatorQueue` persisted),
case view showing hand-off record, evidence, agent raw vs guarded answer, dock board; actions:
approve hold, approve rule exception (RULE005), move truck, divert, close; digest view for
below-threshold items; SLA timers.
Acceptance: coordinator can resolve S4/S5/S6 escalations end-to-end; every action writes
`audit_log`; approvals required by PRD FR9 cannot be bypassed via API.
Risk: scope creep → start with read-only queue + approve/reject.

### P1.5 PII redaction
Tasks: allow-list payload builder (`ids, order_reference, times, dock codes, weights, flags`);
regex redaction for phones/emails/licence numbers in free text before `ask_agent`; test that fails
if any seed driver name/phone/email reaches the payload.
Acceptance: redaction test over all 20 seed chat messages and sandbox transcripts.

### P1.6 Observability
Tasks: JSON logs with correlation ID per message; metrics — agent latency p50/p95, errors, guard
interventions by type, ungrounded answers, stale rechecks, conflicts (must stay 0), queue depth and
wait, takeover rate; daily scorecard reusing `brief_scenarios` measure names; alert on any
`conflicts() > 0` or agent error rate > 5 %.
Acceptance: dashboard shows PRD §7 metrics from live traffic.

### P1.7 Load test — 35 messages / 30 minutes
Tasks: replay generator from sandbox personas at 35 msgs/30 min (plus a burst of 10 in 1 min);
mocked LLM with 22 s latency and real LLM run; N workers configurable.
Acceptance: p95 time-to-first-reply ≤ 60 s with ≥ 2 workers at 22 s/call; 0 conflicts; coordinator
queue max pending ≤ 5; duplicates merged.
Risk: NVIDIA rate limits → backoff budget and a smaller triage model.

---

## P2 — scale and accuracy

### P2.1 Real-time ETA feeds
Ingest carrier/telematics ETAs into `eta_updates` with `source_type`; treat feed ETAs as
independent evidence with their own confidence; reconcile against driver reports (conflict →
UNVERIFIED + ask). Acceptance: claim-vs-feed contradictions surfaced; no ETA treated as GPS-verified
without a feed. Effort L. Risk: feed coverage and latency vary by carrier.

### P2.2 Multi-facility, multi-day
Remove `FACILITY`/`DAY` constants; facility-scoped rules; absolute timestamps; cross-midnight
chains; diversion proposals (FAC-GGN-01) as coordinator-approved actions. Acceptance: S6 diversion
option becomes a computed, approval-gated alternative. Effort L.

### P2.3 Learned ETA confidence
Replace regex buckets in `revise_eta` with a calibrated model per carrier/driver/route from
`eta_updates` vs actual gate-in history; keep rule as fallback; never used to penalise drivers.
Acceptance: calibrated buffer reduces stale holds and missed slots vs rule baseline in backtest.
Effort M. Risk: small, biased history → require minimum sample size per segment.

---

## Milestones

1. **Week 1-2**: P0.1-P0.5 → brief run ≥ 95 workflow confidence over 3 live repeats, closed loop.
2. **Week 3-5**: P1.1 (in progress), P1.3, P1.5, P1.2 → shadow pilot on one facility.
3. **Week 6-8**: P1.4, P1.6, P1.7 → assisted operation gate (PRD rollout phase 2).
4. **Quarter 2**: P2.x.

---

## Service API

`service.py` is a stdlib `http.server` JSON API around one `SlotEngine`. Run
`python3 service.py --port 8080 [--db path] [--live]` (in-memory seed by default; a new `--db` file is
seeded once and reused). Every request carries a simulated clock `"now": "HH:MM"` (4 Aug 2026, +05:30).

| Endpoint | Behaviour |
|---|---|
| `POST /messages` `{external_id, sender, thread_id?, text, now}` | Idempotent on `external_id`. Links sender → shipment (asks which load if a driver has several active, e.g. DRV004), applies `revise_eta`, then returns `CLARIFY` (question), `OPTIONS` (≤3 feasible, nothing held) or `ESCALATED` (hand-off record with every `HANDOFF_FIELDS` entry). |
| `POST /holds` `{shipment_id, slot_id, now, eta?}` | Re-check, then 10-min hold; `409` with reasons if infeasible or taken. |
| `POST /holds/{group}/request` `{now, request_key}` | Idempotent on `request_key` (`DUPLICATE`); expired hold → `409 STALE`. |
| `POST /holds/{group}/confirm` `{now, warehouse_ref}` | Re-check, then confirm (replaces the current appointment); repeat → `ALREADY_CONFIRMED`. |
| `POST /holds/{group}/release`, `POST /appointments/{id}/release` `{now, reason}` | Idempotent release (`ALREADY_RELEASED`). |
| `GET /shipments/{id}/options?now=&eta=`, `GET /board?now=`, `GET /integrity`, `GET /health`, `GET /audit` | Read-only views; `/integrity` returns `SlotEngine.conflicts()`. |

Triage is deterministic unless `--live` and `NVIDIA_API_KEY` are set; the agent only annotates and its output
always passes `guards.guard_answer` (driver reports are never `VERIFIED`). All engine access is serialised by
one lock, so two simultaneous holds on a slot yield exactly one `200`. Every state change (ETA revision, hold,
request, confirm, release, expiry, escalation, rejections) is appended to `service_audit_log`. Tests:
`tests/test_service.py`.
