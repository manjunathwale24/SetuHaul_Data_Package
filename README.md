# SetuHaul driver-exception service (FDE challenge)

This project is a prototype for the SetuHaul Logistics FDE challenge, described in
`21182914-SetuHaul_Logistics_FDE_Challenge_Project_Brief.pdf` (checklist: [docs/brief_checklist.md](docs/brief_checklist.md)).
It is a conversational driver-exception service for Jaipur DC. It works over a SQLite freight-operations snapshot
(4 Aug 2026, Asia/Kolkata). Drivers report delays and ask for dock slots. The service asks for missing facts, offers only
feasible slots, holds, requests, confirms and releases them without double-booking, and escalates cases with no safe
outcome to a human.

## Architecture: LLM agents advise, deterministic code decides

```
driver / facility message
  → duplicate suppression (exact resend never reaches an agent)
  → Triage agent (NVIDIA LLM)      extracts facts; verification / action / ask_whom
  → guards.py                       feasible-ID pre-filter, selection guard, grounding check, coordinator queue
  → Dispatch / Assessment agent     chooses only from feasible IDs
  → slot_engine.py                  re-checks feasibility in a write transaction; owns every state change
                                    OPTION → HELD (10-min TTL) → REQUESTED → CONFIRMED / RELEASED / EXPIRED
  → driver reply, facility request, or coordinator hand-off
```

- **Agents (`nvidia_agents.py`)** define three roles: triage, dispatch and order/vehicle assessment. All three return one
  JSON schema. `state_change_allowed` is always false, so an agent can recommend but never commit.
- **Guards (`guards.py`)** pre-filter feasible slot and option IDs, and block any selection outside that set or any
  attempted state change. They also flag ungrounded evidence, and rank, merge and digest coordinator alerts.
- **Slot engine (`slot_engine.py`)** checks dock type, reefer, weight, ETA, RULE004 and RULE005, dock events, occupancy and
  long-unload overrun. Holds, requests and confirmations re-check inside `BEGIN IMMEDIATE`. Requests and replays are
  idempotent. `race_for_slot` is the concurrency proof.
- **Runtime view fixes (`workflow_simulation.apply_view_fixes`)**: LOW-confidence driver ETAs are ignored, and slots that
  overlap dock events are blocked. The shipped SQL and `.db` are not modified.
- Driver messages are reports, not ground truth. Only facility messages, gate check-ins or verified dock events can verify a fact.

## File map

| Path | Purpose |
|---|---|
| `setuhaul_schema_and_seed.sql` | Reproducible schema and seed (one-shot load) |
| `setuhaul_freight_operations.db` | Ready-to-query snapshot |
| `setuhaul_database_guide.md`, `setuhaul_data_dictionary.csv`, `setuhaul_er_diagram_core.png` | Data documentation |
| `setuhaul_agent_workflow.md` | End-to-end agent workflow (mermaid) |
| `slot_engine.py` | Deterministic feasibility, hold, request, confirm and release logic, plus `race_for_slot` |
| `guards.py` | Feasibility filters, answer guard, grounding check, `CoordinatorQueue` |
| `nvidia_agents.py` | Agent roles, prompts, response schema, NVIDIA client, `.env` loader |
| `workflow_simulation.py` | Seeded workflow simulation and `apply_view_fixes` |
| `brief_scenarios.py` | Scenario run S1-S8 over the brief's ladder; writes `brief_scenarios.json` and `brief_simulation.html` |
| `sandbox.py` | Live multi-agent sandbox with LLM role-played drivers and facility staff; writes `sandbox_report.html/.json` |
| `nvidia_agent_evals.py` | Synthetic agent evals and stress suite; writes `nvidia_agent_report.html`/`nvidia_stress_report.html` |
| `workflow_evals.py` | Risk-weighted scorecard for the deterministic workflow |
| `report_charts.py` | Dependency-free inline SVG charts for the reports |
| `tests/` | `unittest` suites (slot engine, scenarios, guards and agents, sandbox, workflow) |
| `docs/` | Brief checklist, data audit and the deliverable documents listed below |

## Quickstart

Run these from the repository root.

```bash
# Unit tests (no network)
python3 -m unittest discover -s tests -v

# Scenario run with the deterministic stand-in agent (no network)
python3 brief_scenarios.py --offline

# Scenario run with live NVIDIA agents, repeated 3 times for consistency
python3 brief_scenarios.py --repeats 3

# Seeded workflow simulation
python3 workflow_simulation.py

# Live multi-agent sandbox (paces 09:30-21:00 over 15 wall-clock minutes)
python3 sandbox.py --minutes 15

# Database integrity: expect "ok" and no foreign-key rows
sqlite3 setuhaul_freight_operations.db "PRAGMA integrity_check; PRAGMA foreign_key_check;"
# Active-slot uniqueness: expect no rows
sqlite3 setuhaul_freight_operations.db "SELECT slot_id, COUNT(*) FROM appointments WHERE appointment_status IN ('PENDING_CONFIRMATION','CONFIRMED','IN_PROGRESS') GROUP BY slot_id HAVING COUNT(*) > 1;"
```

`python3 brief_scenarios.py --render` re-renders `brief_simulation.html` from the saved JSON without calling the API.

### NVIDIA API key

The live runs (`brief_scenarios.py` without `--offline`, `sandbox.py`, `nvidia_agent_evals.py`) need an NVIDIA API key.
Create a `.env` file in the repository root that contains literal `NAME=value` lines:

```
NVIDIA_API_KEY=your-key-here
# optional overrides
NVIDIA_MODEL_ID=nvidia/nemotron-3-ultra-550b-a55b
NVIDIA_API_URL=https://integrate.api.nvidia.com/v1/chat/completions
```

Matching quotes and whole-line comments are allowed. Shell expansion is not supported. Variables already set in the
environment take precedence. `.env` is listed in `.gitignore`, so **never commit it**. Live runs send only synthetic IDs,
order references, times and dock codes. They never send names, phone numbers, emails or licence numbers.

## Library versions

| Component | Version |
|---|---|
| Python | 3.15.2 |
| Python libraries | Standard library only (`sqlite3`, `urllib`, `threading`, `json`, among others). No `pip install` needed. |
| SQLite (bundled with Python) | 3.54.1 (`python3 -c "import sqlite3;print(sqlite3.sqlite_version)"`) |
| LLM | `nvidia/nemotron-3-ultra-550b-a55b` via `https://integrate.api.nvidia.com/v1/chat/completions` |

## Deliverables map

| # | Brief deliverable | Where |
|---|---|---|
| 1 | Problem Definition Sheet and data audit, with a proving query per issue | [docs/data_audit.md](docs/data_audit.md) |
| 2 | Decision register (D1-D13) | [docs/decision_register.md](docs/decision_register.md) |
| 3 | Working prototype with library versions | `slot_engine.py`, `guards.py`, `nvidia_agents.py`, `workflow_simulation.py`, `brief_scenarios.py`, `sandbox.py`, `tests/`; versions are listed above |
| 4 | Scenario run (R1-R7) | [brief_simulation.html](brief_simulation.html) (from `brief_scenarios.py`; data in `brief_scenarios.json`) |
| 5 | Integrity proof (D13 concurrency test) | `slot_engine.race_for_slot`. It runs in S4 of `brief_scenarios.py` and in `tests/test_brief_scenarios.py::SlotEngineTests::test_parallel_holds_on_one_slot_have_one_winner`, where 3 threads with separate connections compete for one slot and exactly 1 wins. |
| 6 | Client readout | [docs/client_readout.md](docs/client_readout.md) |

Supporting documents: [PRD](docs/PRD.md), [spec](docs/spec.md), [interview whys](docs/interview_whys.md),
[demo script](docs/demo_script.md), [knowledge base](docs/knowledge_base.md),
[implementation plan](docs/implementation_plan.md).

> Note: several linked documents are being written in parallel and may not exist yet: `docs/PRD.md`, `docs/spec.md`,
> `docs/decision_register.md`, `docs/client_readout.md`, `docs/interview_whys.md`, `docs/demo_script.md`,
> `docs/knowledge_base.md` and `docs/implementation_plan.md`.
