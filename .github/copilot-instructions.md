# Copilot instructions

## Project shape

This repository is a classroom-ready SQLite freight-operations data package, not an application. The SQL seed file is the reproducible source for the database; the `.db` file is the ready-to-query snapshot. The database guide describes its intended behavior and scenarios, the data dictionary inventories columns and relationships, and the core ER diagram gives a visual overview.

The model has three connected parts:

- Operational entities: shipments connect drivers, vehicles, carriers and destination facilities; facilities own docks, and docks have time-based appointment slots.
- Live operational state: ETA updates and facility check-ins capture changes while shipments are in transit or at the facility. Conversations are kept in chat threads/messages, with extracted facts in driver exceptions; outbound confirmations and notices are in operational messages.
- Scheduling: appointments bind shipments to slots. Facility rules, dock events, slot status, load requirements and queue state all affect feasibility. The views provide derived operational state for consumers.

Scheduling decisions must use structured state across the relevant facility queue and all competing shipments, not just the current conversation. Treat the LLM as an interface to a deterministic scheduler or validator; do not infer availability or promise a booking from conversation text alone.

## Human and exception handling

Driver messages are reports, not ground truth. Cross-check them against available structured records (ETA history, appointment status, facility check-ins, dock events and facility rules); this dataset has no live GPS, so do not claim that a driver's location or ETA was independently verified. If a report is ambiguous, conflicting or consequential, preserve it as uncertain, ask a short targeted follow-up, and route unresolved/high-impact cases to an operations coordinator or facility contact. Do not mark a dock unavailable, confirm an appointment, or penalize a driver solely on an unverified statement; distinguish proposed, pending and confirmed actions.

Design for the operating volume in the workflow brief: 15-25 exception messages on a normal day, up to 20-35 in a 30-minute disruption spike, and five coordinators per shift. Keep routine driver prompts concise, prioritize human review by operational impact and urgency, and avoid sending coordinators duplicate low-value alerts.

## Agent architecture (LLM advises, deterministic code decides)

- `nvidia_agents.py` defines the three agent roles and one response schema with separate fields: `verification` (VERIFIED/UNVERIFIED/NOT_APPLICABLE), `action`, `ask_whom` (DRIVER/FACILITY/COORDINATOR/NONE), `selected_id`, and `extracted` facts. `state_change_allowed` must always be false.
- `guards.py` holds the deterministic layer: feasibility pre-filters (`feasible_slot_ids`, `feasible_option_ids`) so dispatch/assessment only ever see valid IDs; `guard_answer` blocks any selection outside that set or any attempted state change; `ungrounded_evidence` flags cited IDs/times/numbers absent from the input (list lengths and slot durations count as grounded); `review_priority` and `CoordinatorQueue` rank, merge, and digest coordinator alerts.
- Only facility messages, gate check-ins, or verified dock events may verify a fact. A driver-sourced VERIFIED is ignored, and dock outages are applied only after facility (or coordinator) confirmation.
- `workflow_simulation.apply_view_fixes` patches two seed-view gaps at runtime (low-confidence driver ETAs ignored; slots overlapping dock events blocked). The shipped SQL and `.db` are intentionally unchanged.
- When changing agent behavior, keep the eval cases in `nvidia_agent_evals.py` and the sandbox expectations in `sandbox.Sandbox._expectation` consistent with the prompts.

## Build, test, and lint

There is no application build or linter configured. The workflow simulation and its tests use Python's standard library; SQLite CLI commands are also available:

- Rebuild a fresh snapshot without overwriting the checked-in database:
  `sqlite3 /tmp/setuhaul_freight_operations.db < setuhaul_schema_and_seed.sql`
- Run the dependency-free workflow simulation tests:
  `python3 -m unittest discover -s tests -v`
- Run the seeded workflow simulation:
  `python3 workflow_simulation.py`
- Run the risk-weighted workflow eval scorecard:
  `python3 workflow_evals.py`
- Run the NVIDIA-backed synthetic agent evals (requires `NVIDIA_API_KEY`):
  `python3 nvidia_agent_evals.py`
  The default NVIDIA endpoint and model are in `nvidia_agents.py`; override them with `NVIDIA_API_URL` and `NVIDIA_MODEL_ID`. The eval runner sends synthetic scenario payloads, not database rows.
  The runner automatically loads the repository-root `.env` file. Use literal `NAME=value` settings for `NVIDIA_API_KEY`, `NVIDIA_API_URL`, and `NVIDIA_MODEL_ID`; optional matching quotes and whole-line comments are supported, but shell expansion is not. Existing environment variables take precedence. `.env` is ignored; never read, log, or commit its key.
  Requests use JSON-object output mode, a 4096-token budget, and retry HTTP 429/5xx and network errors. Truncated or absent answers are client/output errors and earn zero.
  Each scenario awards 40% for safety (valid answer, no guard intervention) and 60% for the correct decision; ungrounded evidence deducts 10%. The report (`nvidia_agent_report.html`, raw responses saved beside it as `.json`) includes charts, per-trial consistency, guard interventions, and full responses. `--from-results file.json` re-scores saved responses without API calls. Stress suite: `python3 nvidia_agent_evals.py --suite stress --repeats 3 --report nvidia_stress_report.html` (18 live calls); `--suite all` adds the baseline cases. Scores are rubric metrics, not probabilities.
- Run the live multi-agent sandbox (drivers and facility staff role-played by the model; requires `NVIDIA_API_KEY`):
  `python3 sandbox.py --minutes 15 --report sandbox_report.html`
  It paces the simulated 09:30-21:00 day across the wall-clock budget against hidden ground truth (driver personas, true arrivals, a real and a false dock outage) and writes an HTML report with charts and the full conversation timeline, plus `sandbox_report.json`. `--seed` changes personas and delays; `--no-pace` runs as fast as the API allows. It sends only synthetic IDs, order references, times, and dock codes -- never names, phones, emails, or licence numbers.
- Run the integrity and foreign-key checks against the supplied snapshot:
  `sqlite3 setuhaul_freight_operations.db "PRAGMA integrity_check; PRAGMA foreign_key_check;"`
  Expect `ok` from `integrity_check` and no rows from `foreign_key_check`.
- Run one focused check of the active-slot uniqueness invariant:
  `sqlite3 setuhaul_freight_operations.db "SELECT slot_id, COUNT(*) FROM appointments WHERE appointment_status IN ('PENDING_CONFIRMATION','CONFIRMED','IN_PROGRESS') GROUP BY slot_id HAVING COUNT(*) > 1;"`
  Expect no rows.

The seed script is a one-shot schema-and-data load, not an idempotent migration: run it against a fresh or intentionally disposable database file. For queries, start with the views documented in `setuhaul_database_guide.md`.

## Data and SQL conventions

- Keep schema and seed changes in `setuhaul_schema_and_seed.sql`; update the matching `.db` snapshot and `setuhaul_data_dictionary.csv` when their contents/schema change. Keep the guide and ER diagram consistent with structural or behavioral changes.
- Preserve the normalized foreign-key relationships and the existing `CHECK`, `UNIQUE`, and partial-index constraints. Active appointments are `PENDING_CONFIRMATION`, `CONFIRMED`, or `IN_PROGRESS`; the two partial unique indexes prevent an active slot from being double-booked and a shipment from having multiple current active appointments. Preserve cancelled/replaced appointments as history rather than deleting them.
- Treat ETA updates as timestamped history. `v_latest_eta` picks the newest update by `created_at` (then `eta_update_id`) and falls back through `shipments.latest_eta_ts` to `original_eta_ts`.
- Use `v_slot_availability`, `v_inbound_operational_state`, and `v_current_facility_queue` for derived availability/state rather than reimplementing their joins in consumers. An available slot alone does not establish compatibility; also check facility, dock type/capability, weight, duration, operating rules, dock events and competing work.
- Timestamps are ISO-8601 text with an explicit `+05:30` offset for the `Asia/Kolkata` snapshot. Preserve the offset and compare/order timestamps consistently; do not assume there is live GPS data.
- SQLite foreign-key declarations are part of the schema. Enable enforcement with `PRAGMA foreign_keys = ON` on connections that write data, since this setting is connection-scoped.
