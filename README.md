# SetuHaul driver-exception service (FDE challenge)

This project is a prototype for the SetuHaul Logistics FDE challenge, described in
`21182914-SetuHaul_Logistics_FDE_Challenge_Project_Brief.pdf` (checklist: [docs/brief_checklist.md](docs/brief_checklist.md)).
It is a conversational driver-exception service for Jaipur DC. It works over a SQLite freight-operations snapshot
(4 Aug 2026, Asia/Kolkata). Drivers report delays and ask for dock slots. The service asks for missing facts, offers only
feasible slots, holds, requests, confirms and releases them without double-booking, and escalates cases with no safe
outcome to a human.

## Architecture: LangGraph routes, deterministic code decides

```
driver / facility message
  → duplicate suppression (exact resend never reaches an agent)
  → LangGraph safety gate (driver emergencies bypass scheduling and escalate)
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
  long-unload overrun. Holds, requests and confirmations re-check inside `BEGIN IMMEDIATE`. Request idempotency is
  transactionally tied to the hold and rejects a reused key for another operation.
- **Conversation graph (`conversation_graph.py`)** uses LangGraph to route safety emergencies away from slot planning.
  The service stores the offered option list and rechecks the driver's selection before holding and requesting it.
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
| `conversation_graph.py` | LangGraph safety routing for driver messages |
| `dashboard.html` | Control-tower wireframe for dock status, shipment activity, events and five training roleplays |
| `deployment_dashboard.html` | Deployment health view, EKS target profile, and explicitly unobserved AWS rollout status |
| `training_agents.py` | Four deterministic driver personas and one facility persona; training only, no operational side effects |
| `service.py` | HTTP API, message workflow, SQLite persistence and audit log |
| `guards.py` | Feasibility filters, answer guard, grounding check, `CoordinatorQueue` |
| `nvidia_agents.py` | Agent roles, prompts, response schema, NVIDIA client, `.env` loader |
| `workflow_simulation.py` | Seeded workflow simulation and `apply_view_fixes` |
| `brief_scenarios.py` | Scenario run S1-S8 over the brief's ladder; writes `brief_scenarios.json` and `brief_simulation.html` |
| `sandbox.py` | Live multi-agent sandbox with LLM role-played drivers and facility staff; writes `sandbox_report.html/.json` |
| `nvidia_agent_evals.py` | Synthetic agent evals and stress suite; writes `nvidia_agent_report.html`/`nvidia_stress_report.html` |
| `workflow_evals.py` | Risk-weighted scorecard for the deterministic workflow |
| `report_charts.py` | Dependency-free inline SVG charts for the reports |
| `tests/` | `unittest` suites, including HTTP end-to-end message and booking tests |
| `deploy/k8s/`, `scripts/deploy-eks.sh` | Single-replica EKS manifests and ECR deployment script |
| `docs/` | Brief checklist, data audit, and the [EKS deployment guide](docs/deploy_eks.md) |

## Quickstart

Run these from the repository root.

```bash
# Install the LangGraph dependency in a virtual environment
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt

# Unit and local HTTP end-to-end tests
.venv/bin/python -m unittest discover -s tests -v

# Open the operations tower and agent training room
.venv/bin/python service.py --port 8080
# Visit http://127.0.0.1:8080/dashboard
# Deployment health and EKS target view: http://127.0.0.1:8080/deployment

# Scenario run with the deterministic stand-in agent (no network)
.venv/bin/python brief_scenarios.py --offline

# Scenario run with live NVIDIA agents, repeated 3 times for consistency
.venv/bin/python brief_scenarios.py --repeats 3

# Seeded workflow simulation
.venv/bin/python workflow_simulation.py

# Live multi-agent sandbox (paces 09:30-21:00 over 15 wall-clock minutes)
.venv/bin/python sandbox.py --minutes 15

# Database integrity: expect "ok" and no foreign-key rows
sqlite3 setuhaul_freight_operations.db "PRAGMA integrity_check; PRAGMA foreign_key_check;"
# Active-slot uniqueness: expect no rows
sqlite3 setuhaul_freight_operations.db "SELECT slot_id, COUNT(*) FROM appointments WHERE appointment_status IN ('PENDING_CONFIRMATION','CONFIRMED','IN_PROGRESS') GROUP BY slot_id HAVING COUNT(*) > 1;"
```

`.venv/bin/python brief_scenarios.py --render` re-renders `brief_simulation.html` from the saved JSON without calling the API.

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
| Python | 3.14 (local tested runtime); container uses `python:3.14-slim` |
| Python libraries | `langgraph==1.2.23`; remaining application code uses the Python standard library |
| SQLite | Bundled with Python (`python -c "import sqlite3;print(sqlite3.sqlite_version)"`) |
| LLM | `nvidia/nemotron-3-ultra-550b-a55b` via `https://integrate.api.nvidia.com/v1/chat/completions` |

### Container and AWS deployment

Build and exercise the container locally:

```bash
docker build --platform linux/amd64 -t setuhaul-service:e2e .
docker run --rm setuhaul-service:e2e python -m unittest discover -s tests -v
```

Deploy to an **existing** EKS cluster with `AWS_REGION` and `EKS_CLUSTER_NAME` set using
[docs/deploy_eks.md](docs/deploy_eks.md). The deployment is intentionally single-replica and private
because this prototype uses SQLite and does not yet include caller authentication or TLS.

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
