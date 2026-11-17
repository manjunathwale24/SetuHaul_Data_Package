"""Brief-driven scenario run: one distinct scenario per problem in the SetuHaul brief.

Each scenario replays real threads, rules and dock events from the seed data. The
NVIDIA agents advise (triage, assessment, dispatch); guards and the slot engine
decide. Output: brief_scenarios.json and brief_simulation.html.

Usage:
    python3 brief_scenarios.py            # live NVIDIA agents (needs NVIDIA_API_KEY in .env)
    python3 brief_scenarios.py --offline  # deterministic stand-in agent, no network
    python3 brief_scenarios.py --render   # re-render HTML from brief_scenarios.json
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass, field
import json
import os
from pathlib import Path
import re
import sys
import time
from typing import Any, Callable

from guards import feasible_option_ids, guard_answer, to_minutes, ungrounded_evidence
from nvidia_agents import AgentRole, NVIDIAClientError, ask_agent, load_nvidia_env
from slot_engine import PRIORITY_RANK, SlotCheck, SlotEngine, hhmm, race_for_slot


ROOT = Path(__file__).resolve().parent
JSON_OUT = ROOT / "brief_scenarios.json"
HTML_OUT = ROOT / "brief_simulation.html"
AgentFn = Callable[[AgentRole, dict[str, Any]], dict[str, Any]]

LADDER = {
    "L1": "Options-only request (THR011, SHP1017)",
    "L2": "Missing facts (THR003, SHP1013, THR010, DRV004)",
    "L3": "Rules and compatibility narrow choices (SHP1010, SHP1015, SHP1016, RULE005)",
    "L4": "Several delayed trucks chase nearby slots (SHP1006, SHP1012, SHP1013, SHP1014, SHP1009)",
    "L5": "World changes mid-conversation (SHP1005, APT1008, THR001, THR009, SHP1002)",
    "L6": "No feasible slot (SHP1015, THR005, OM004)",
    "L7": "Optional: early, late and in-progress share one facility (SHP1002, SHP1003, SHP1004, SHP1006)",
}
RUN_ITEMS = {
    "R1": "Delay report with clarification questions",
    "R2": "Request for later options and comparison",
    "R3": "Several requests against the same facility schedule",
    "R4": "At least two requests competing for the same capacity",
    "R5": "An option that changes or disappears",
    "R6": "Escalation because no safe automatic outcome exists",
    "R7": "Optional: scheduler proposes a new sequence after ETA update / gate check-in",
}
DECISIONS = {
    "D1": "Fields collected before showing options",
    "D2": "Linking a conversation to driver, shipment and appointment",
    "D3": "Revised ETA and confidence rule",
    "D4": "Slot feasibility check",
    "D5": "What 'available' means while another driver decides",
    "D6": "Hold, request, reservation and confirmation states",
    "D7": "Ordering of simultaneous requests",
    "D8": "Optional: when the schedule recalculates",
    "D9": "Stale options, cancellations, duplicates and retries",
    "D10": "No-slot escalation message and hand-off record",
    "D11": "Plain-language refusal messages",
    "D12": "Decisions needing human approval",
    "D13": "Concurrency test and result",
    "H": "Human side: driver claims are reports, not facts",
}
REQUIRED_FIELDS_BEFORE_OPTIONS = ("shipment_id", "eta", "eta_confidence", "earliest_start", "latest_finish")
HANDOFF_FIELDS = ("case_id", "shipment_id", "driver_id", "facility_id", "reason", "constraints",
                  "options_checked", "blocking_rules", "recommended_actions", "deadline", "owner",
                  "channel", "delivery_status")


# ---------------------------------------------------------------------------
# D3: revised ETA and confidence
# ---------------------------------------------------------------------------
def revise_eta(text: str, sent_at: int, history: list[tuple[int, str]]) -> dict[str, Any]:
    """Planning ETA rule. Clock time -> HIGH, unless it moved >30 min from the last report
    (MEDIUM, +15 min buffer). Relative ('late by one hour') or a range -> LOW: the agent asks
    before planning. history = [(eta_min, confidence)] oldest first."""
    clock = re.findall(r"\b(\d{1,2})[:.](\d{2})\b", text)
    rng = re.search(r"between\s+\d{1,2}[:.]\d{2}\s+and\s+\d{1,2}[:.]\d{2}", text, re.I)
    relative = re.search(r"late by\s+(one|an|\d+)\s*(hour|hr|minutes?|min)", text, re.I)
    if clock and not rng:
        h, m = map(int, clock[-1])
        if h < 7 and "pm" not in text.lower():
            h += 12
        eta = h * 60 + m
        if re.search(r"\b(around|about|approx|maybe|roughly)\b", text, re.I):
            return {"eta": eta, "confidence": "MEDIUM", "planning_eta": eta + 15,
                    "rule": f"hedged clock time ('around {hhmm(eta)}') -> MEDIUM, +15 min buffer"}
        if history and abs(eta - history[-1][0]) > 30:
            return {"eta": eta, "confidence": "MEDIUM", "planning_eta": eta + 15,
                    "rule": f"clock time {hhmm(eta)} moved {abs(eta - history[-1][0])} min since last report -> MEDIUM, +15 min buffer"}
        return {"eta": eta, "confidence": "HIGH", "planning_eta": eta,
                "rule": f"explicit clock time {hhmm(eta)}, consistent with history -> HIGH, no buffer"}
    if relative or rng:
        return {"eta": None, "confidence": "LOW", "planning_eta": None,
                "rule": "relative delay or range: ambiguous base time -> LOW, ask before any slot search"}
    return {"eta": None, "confidence": "NONE", "planning_eta": None, "rule": "no time stated -> ask"}


# ---------------------------------------------------------------------------
# Agent harness
# ---------------------------------------------------------------------------
@dataclass
class Expect:
    actions: set[str]
    ask_whom: set[str] | None = None
    selected: set[str] | None = None  # None: not checked; empty set: must be null
    verification: set[str] | None = None


@dataclass
class Runner:
    agent: AgentFn | None
    mode: str
    calls: list[dict[str, Any]] = field(default_factory=list)

    def ask(self, role: AgentRole, payload: dict[str, Any], allowed: list[str] | None,
            expect: Expect) -> dict[str, Any]:
        started = time.monotonic()
        error, raw = None, None
        try:
            raw = (self.agent or ask_agent)(role, payload)
        except NVIDIAClientError as exc:
            error = str(exc)
        seconds = round(time.monotonic() - started, 1)
        if raw is None:
            guarded = {"action": "ESCALATE", "ask_whom": "COORDINATOR", "selected_id": None,
                       "verification": "UNVERIFIED", "state_change_allowed": False, "evidence": [],
                       "reason": f"agent unavailable: {error}", "question": None, "extracted": None}
            violations, ungrounded = ["agent error -> safe fallback to coordinator"], []
        else:
            guarded, violations = guard_answer(raw, allowed, payload)
            ungrounded = ungrounded_evidence(raw, payload)
            if guarded.get("verification") == "VERIFIED" and not payload.get("independent_confirmation"):
                violations.append("VERIFIED without independent confirmation -> UNVERIFIED")
                guarded["verification"] = "UNVERIFIED"
        checks = {"action": guarded.get("action") in expect.actions}
        if expect.ask_whom is not None:
            checks["ask_whom"] = guarded.get("ask_whom") in expect.ask_whom
        if expect.selected is not None:
            sel = guarded.get("selected_id")
            checks["selected"] = (sel is None) if not expect.selected else sel in expect.selected
        if expect.verification is not None:
            checks["verification"] = guarded.get("verification") in expect.verification
        record = {
            "role": role.value, "payload": payload, "raw": raw, "guarded": guarded, "error": error,
            "violations": violations, "ungrounded": ungrounded, "allowed": allowed, "seconds": seconds,
            "expect": {"actions": sorted(expect.actions),
                       "ask_whom": sorted(expect.ask_whom) if expect.ask_whom else None,
                       "selected": sorted(expect.selected) if expect.selected is not None else None,
                       "verification": sorted(expect.verification) if expect.verification else None},
            "checks": checks, "correct": error is None and all(checks.values()),
        }
        self.calls.append(record)
        return record


def offline_agent(role: AgentRole, payload: dict[str, Any]) -> dict[str, Any]:
    """Deterministic stand-in that answers the way the policy expects (for tests/no network)."""
    hint = payload.get("_offline_expect", {})
    return {
        "verification": hint.get("verification", "UNVERIFIED"),
        "action": hint.get("action", "NO_ACTION"),
        "ask_whom": hint.get("ask_whom", "NONE"),
        "selected_id": hint.get("selected"),
        "state_change_allowed": False,
        "extracted": None,
        "evidence": [],
        "question": hint.get("question"),
        "reason": "offline stand-in",
    }


# ---------------------------------------------------------------------------
# Scenario recorder
# ---------------------------------------------------------------------------
class Scenario:
    def __init__(self, sid: str, title: str, covers: dict[str, list[str]], why: str, runner: Runner):
        self.id, self.title, self.covers, self.why = sid, title, covers, why
        self.runner = runner
        self.engine = SlotEngine()
        self.steps: list[dict[str, Any]] = []
        self.metrics: dict[str, Any] = {"driver_turns": 0, "agent_turns": 0, "clarification_turns": 0,
                                        "manual_takeover": False, "escalations": [], "stale_caught": 0}
        self.outcome = ""
        self.start_min: int | None = None
        self.end_min: int | None = None
        self._last_board: dict[str, Any] | None = None

    def step(self, now: int, actor: str, text: str, *, tags: list[str] | None = None,
             data: Any = None, agent: dict[str, Any] | None = None) -> None:
        self.start_min = now if self.start_min is None else min(self.start_min, now)
        self.end_min = now if self.end_min is None else max(self.end_min, now)
        board = self.engine.board(now)
        changed = board != self._last_board
        self._last_board = board
        if actor == "DRIVER":
            self.metrics["driver_turns"] += 1
        if actor == "AGENT→DRIVER":
            self.metrics["agent_turns"] += 1
        self.steps.append({"t": hhmm(now), "actor": actor, "text": text, "tags": tags or [],
                           "data": data, "agent": agent, "board": board if changed else None})

    def ask(self, now: int, role: AgentRole, payload: dict[str, Any], allowed: list[str] | None,
            expect: Expect, note: str, tags: list[str] | None = None) -> dict[str, Any]:
        payload = dict(payload)
        payload["_offline_expect"] = {
            "action": sorted(expect.actions)[0] if len(expect.actions) == 1 else _first(expect.actions),
            "ask_whom": _first(expect.ask_whom) if expect.ask_whom else "NONE",
            "selected": _first(expect.selected) if expect.selected else None,
            "verification": _first(expect.verification) if expect.verification else "UNVERIFIED",
        } if self.runner.mode == "offline" else {}
        if not payload["_offline_expect"]:
            payload.pop("_offline_expect")
        record = self.runner.ask(role, payload, allowed, expect)
        g = record["guarded"]
        label = {"triage": "TRIAGE", "dispatch": "DISPATCH", "order_vehicle_assessment": "ASSESSMENT"}[role.value]
        summary = f"{note} → {g.get('action')}"
        if g.get("selected_id"):
            summary += f" {g['selected_id']}"
        if g.get("ask_whom") not in (None, "NONE"):
            summary += f", ask {g['ask_whom']}"
        summary += f" [{g.get('verification')}]"
        self.step(now, label, summary, tags=tags, agent=record)
        return record

    def say(self, now: int, text: str, tags: list[str] | None = None) -> None:
        self.step(now, "AGENT→DRIVER", text, tags=tags)

    def escalate(self, now: int, record: dict[str, Any], tags: list[str] | None = None) -> None:
        missing = [f for f in HANDOFF_FIELDS if record.get(f) in (None, "", [])]
        record["completeness"] = round(1 - len(missing) / len(HANDOFF_FIELDS), 2)
        record["missing_fields"] = missing
        self.metrics["escalations"].append(record)
        self.metrics["manual_takeover"] = True
        self.step(now, "ESCALATION", f"Hand-off {record['case_id']} to {record['owner']} via {record['channel']} "
                  f"({int(record['completeness'] * 100)}% complete)", tags=tags, data=record)

    def to_dict(self) -> dict[str, Any]:
        calls = [s["agent"] for s in self.steps if s["agent"]]
        self.metrics.update({
            "agent_calls": len(calls),
            "agent_correct": sum(c["correct"] for c in calls),
            "guard_interventions": sum(bool(c["violations"]) for c in calls),
            "minutes_to_outcome": (self.end_min or 0) - (self.start_min or 0),
            "turns_to_outcome": self.metrics["driver_turns"] + self.metrics["agent_turns"],
            "integrity": self.engine.conflicts(),
            "rechecks_failed": self.engine.rechecks_failed,
        })
        return {"id": self.id, "title": self.title, "covers": self.covers, "why": self.why,
                "outcome": self.outcome, "metrics": self.metrics, "steps": self.steps}


def _first(values: set[str] | None) -> str | None:
    return sorted(values)[0] if values else None


def shipment_card(e: SlotEngine, sid: str, **extra: Any) -> dict[str, Any]:
    s = e.shipment(sid)
    card = {"shipment_id": sid, "order_reference": s["order_reference"], "driver_id": s["driver_id"],
            "load_weight_kg": s["load_weight_kg"], "required_dock_type": s["required_dock_type"],
            "temperature_control_required": bool(s["temperature_control_required"]),
            "priority": s["priority_code"], "expected_unload_min": s["expected_unload_min"],
            "status": s["current_status"]}
    apt = e.db.execute(
        "SELECT a.appointment_id, a.appointment_status, sl.slot_id, d.dock_code, substr(sl.slot_start_ts,12,5) AS start"
        " FROM appointments a JOIN appointment_slots sl USING(slot_id) JOIN docks d USING(dock_id)"
        " WHERE a.shipment_id=? AND a.is_current=1", (sid,)).fetchone()
    card["current_appointment"] = dict(apt) if apt else None
    card.update(extra)
    return card


def slot_options(checks: list[SlotCheck], feasible_n: int = 4, infeasible_n: int = 3) -> list[dict[str, Any]]:
    good = [c.as_dict() for c in checks if c.feasible][:feasible_n]
    bad = [c.as_dict() for c in checks if not c.feasible and c.start >= (checks[0].start if checks else 0)]
    return good + bad[:infeasible_n]


def dispatch(sc: Scenario, now: int, sid: str, eta: int, *, request: str, expect_ids: set[str] | None,
             tags: list[str], **kw: Any) -> tuple[list[SlotCheck], dict[str, Any]]:
    checks = sc.engine.check(sid, eta, now=now, **kw)
    feasible = [c for c in checks if c.feasible]
    near = [c for c in checks if c.start >= eta - 60][:40]
    payload = {"request": request, "now": hhmm(now),
               "shipment": shipment_card(sc.engine, sid, planning_eta=hhmm(eta)),
               "slots": slot_options(near), "deterministic_feasibility": {"feasible_ids": [c.slot_ids[0] for c in feasible[:4]]}}
    allowed = payload["deterministic_feasibility"]["feasible_ids"]
    if allowed:
        earliest = {c.slot_ids[0] for c in feasible[:4] if c.start == feasible[0].start}
        expect = Expect({"PROPOSE"}, selected=expect_ids or earliest)
    else:
        expect = Expect({"NO_FEASIBLE_OPTION", "ESCALATE"}, selected=set())
    rec = sc.ask(now, AgentRole.DISPATCH, payload, allowed, expect, f"Dispatch {sid} ({request})", tags)
    return feasible, rec


# ---------------------------------------------------------------------------
# Scenarios
# ---------------------------------------------------------------------------
def s1_options_only(r: Runner) -> Scenario:
    sc = Scenario("S1", "Options only: driver asks, nothing is booked",
                  {"ladder": ["L1"], "run": ["R2"], "decisions": ["D1", "D5", "D6"]},
                  "Proves asking is kept separate from booking: options are listed and compared, "
                  "the current APT1017 is untouched and no hold is placed until the driver chooses.", r)
    e, now = sc.engine, to_minutes("10:02")
    text = "What slots are possible after 1 PM if I reach around 12:45?"
    sc.step(now, "DRIVER", f"[THR011 / DRV001] {text}", tags=["L1"])
    eta = revise_eta(text, now, [(to_minutes("12:15"), "PLANNER"), (to_minutes("12:45"), "LOW")])
    sc.step(now, "ENGINE", f"D3 ETA rule: {eta['rule']}. Linked THR011 → SHP1017 → APT1017 (D1 12:00, now behind ETA).",
            tags=["D2", "D3"], data=eta)
    sc.ask(now, AgentRole.TRIAGE, {
        "message": {"thread_id": "THR011", "sender": "DRV001", "text": text, "sent_at": "10:02"},
        "linked": shipment_card(e, "SHP1017"),
        "eta_history": [{"id": "ETA012", "eta": "12:45", "confidence": "LOW", "note": "between 12:30 and 13:00"}],
        "independent_confirmation": [],
    }, None, Expect({"PROPOSE", "NO_ACTION"}, verification={"UNVERIFIED", "NOT_APPLICABLE"}),
        "Triage THR011", ["H"])
    before = e.db.execute("SELECT COUNT(*) FROM slot_holds").fetchone()[0]
    feasible, rec = dispatch(sc, now, "SHP1017", to_minutes("12:45"), request="OPTIONS_ONLY - compare, do not book",
                             expect_ids=None, tags=["D4"], not_before=to_minutes("13:00"))
    top = feasible[:3]
    comparison = [{"option": i + 1, "dock": c.dock, "start": hhmm(c.start), "unload_ends": hhmm(c.end),
                   "minutes_after_eta": c.start - to_minutes("12:45")} for i, c in enumerate(top)]
    sc.step(now, "ENGINE", "Comparison table (options only, no hold created)", tags=["R2"], data=comparison)
    sc.say(now, "These are options, not bookings: " + "; ".join(
        f"{o['option']}) {o['dock']} {o['start']} (unload done {o['unload_ends']})" for o in comparison)
        + ". Your current 12:00 D1 slot stays as it is until you pick one; reply with the option number "
          "and I'll hold it for 10 minutes while I ask the warehouse.", tags=["D5", "D6"])
    sc.step(now + 3, "DRIVER", "Ok, will decide after lunch.")
    after = e.db.execute("SELECT COUNT(*) FROM slot_holds").fetchone()[0]
    apt = e.db.execute("SELECT appointment_status, slot_id FROM appointments WHERE appointment_id='APT1017'").fetchone()
    sc.step(now + 3, "ENGINE", f"State check: holds before={before}, after={after}; APT1017 still "
            f"{apt['appointment_status']} on {apt['slot_id']}. Nothing was booked.", tags=["D6"])
    sc.outcome = f"OPTIONS_SHARED: {len(comparison)} compared, 0 holds, APT1017 unchanged"
    return sc


def s2_missing_facts(r: Runner) -> Scenario:
    sc = Scenario("S2", "Missing facts: ask before any tool call",
                  {"ladder": ["L2"], "run": ["R1"], "decisions": ["D1", "D2", "D3", "D6"]},
                  "Two incomplete messages: a relative delay (THR003) and a driver with two loads who names "
                  "neither (THR010). The agent must ask first; then a full hold → request → confirm cycle.", r)
    e = sc.engine
    now = to_minutes("09:19")
    t1 = "I am late by one hour."
    sc.step(now, "DRIVER", f"[THR003 / DRV013] {t1}", tags=["L2"])
    eta = revise_eta(t1, now, [(to_minutes("10:00"), "PLANNER")])
    sc.step(now, "ENGINE", f"D3 ETA rule: {eta['rule']}. Missing before options: eta, latest_finish.",
            tags=["D1", "D3"], data={"required": REQUIRED_FIELDS_BEFORE_OPTIONS, "known": ["shipment_id"]})
    sc.ask(now, AgentRole.TRIAGE, {
        "message": {"thread_id": "THR003", "sender": "DRV013", "text": t1, "sent_at": "09:19"},
        "linked": shipment_card(e, "SHP1013"), "original_eta": "10:00", "independent_confirmation": [],
    }, None, Expect({"CLARIFY"}, ask_whom={"DRIVER"}), "Triage THR003", ["D1"])
    sc.metrics["clarification_turns"] += 1
    sc.say(now, "Does one hour mean you will reach the gate at 11:00, or could the delay grow? "
                "And is there a time by which you must leave?", tags=["R1"])
    sc.step(now, "ENGINE", "No slot search run: required fields missing (tool call withheld).", tags=["D1"])
    t2 = "Reaching gate 11:00 for sure, no hurry after that."
    sc.step(now + 6, "DRIVER", f"[THR003] {t2}")
    eta2 = revise_eta(t2, now + 6, [(to_minutes("10:00"), "PLANNER")])
    sc.step(now + 6, "ENGINE", f"D3 ETA rule: {eta2['rule']}.", tags=["D3"], data=eta2)
    sc.ask(now + 6, AgentRole.TRIAGE, {
        "message": {"thread_id": "THR003", "sender": "DRV013", "text": t2, "sent_at": hhmm(now + 6)},
        "linked": shipment_card(e, "SHP1013"), "previous_question": "new arrival time?",
        "independent_confirmation": [],
    }, None, Expect({"PROPOSE"}, verification={"UNVERIFIED"}), "Triage THR003 reply", ["H"])
    pending = next(c for c in e.check("SHP1013", eta2["planning_eta"], now=now + 6) if c.slot_ids[0] == "SLOT-JAI-018")
    sc.step(now + 6, "ENGINE", f"Pending APT1013A (D2 11:00) at planning ETA {hhmm(eta2['planning_eta'])}: "
            + ("still feasible" if pending.feasible else "UNSAFE — " + "; ".join(pending.reasons))
            + ". OM002 (QUEUED, never sent) must not be sent as-is.", tags=["D4", "D6"])
    feasible13, rec13 = dispatch(sc, now + 6, "SHP1013", eta2["planning_eta"], request="REPLAN after clarified ETA",
                                 expect_ids=None, tags=["D4"])
    pick13 = next((c for c in feasible13 if c.slot_ids[0] == rec13["guarded"].get("selected_id")), feasible13[0])
    ok, g13 = e.hold("SHP1013", pick13, eta2["planning_eta"], now=now + 6)
    sc.say(now + 6, f"Thanks. Your ETA has moved an hour since the plan, so I allow a 15-minute buffer and the "
                    f"11:00 slot becomes too tight; I won't push it to the warehouse. I'm holding {pick13.dock} {hhmm(pick13.start)} for 10 minutes — reply YES "
                    "to request it. It is not confirmed until the warehouse accepts.", tags=["D6"])
    sc.step(now + 8, "DRIVER", "[THR003] YES")
    status, _ = e.request("SHP1013", g13, now=now + 8, request_key="wa-THR003-yes")
    sc.step(now + 8, "ENGINE", f"REQUEST → {status}; APT1013A stays PENDING until the new slot is confirmed "
            "(one current appointment per shipment).", tags=["D6"])

    now = to_minutes("09:31")
    t3 = "I will be late by 45 minutes."
    sc.step(now, "DRIVER", f"[THR010 / DRV004] {t3}", tags=["L2"])
    cands = [shipment_card(e, "SHP1004", gate_check_in="CHK1004 09:25 (in yard)"), shipment_card(e, "SHP1020")]
    sc.step(now, "ENGINE", "D2 linking: THR010 has no shipment. DRV004 → SHP1004 (already checked in 09:25) "
            "and SHP1020 (18:00). Ambiguous → ask, don't guess.", tags=["D2"], data=cands)
    sc.ask(now, AgentRole.TRIAGE, {
        "message": {"thread_id": "THR010", "sender": "DRV004", "text": t3, "sent_at": "09:31"},
        "candidate_shipments": cands, "independent_confirmation": [],
    }, None, Expect({"CLARIFY"}, ask_whom={"DRIVER"}), "Triage THR010", ["D2"])
    sc.metrics["clarification_turns"] += 1
    sc.say(now, "You have two loads today. Is this ORD-260804-004 (you're already at the gate) or "
                "the evening ORD-260804-020?", tags=["R1", "D2"])
    t4 = "The evening one, 020. Gate by 18:45."
    sc.step(now + 4, "DRIVER", f"[THR010] {t4}")
    eta4 = revise_eta(t4, now + 4, [(to_minutes("18:00"), "PLANNER")])
    sc.step(now + 4, "ENGINE", f"Trace: MSG016 → THR010 → DRV004 → SHP1020 → APT1020 (SLOT-JAI-025 D2 18:00). "
            f"D3: {eta4['rule']}.", tags=["D2", "D3"], data=eta4)
    feasible, rec = dispatch(sc, now + 4, "SHP1020", eta4["planning_eta"], request="REPLAN after driver ETA",
                             expect_ids=None, tags=["D4"])
    pick = next((c for c in feasible if c.slot_ids[0] == rec["guarded"].get("selected_id")), feasible[0])
    ok, group = e.hold("SHP1020", pick, eta4["planning_eta"], now=now + 4)
    sc.step(now + 4, "ENGINE", f"HOLD {pick.slot_ids[0]} ({pick.dock} {hhmm(pick.start)}) for 10 min: {ok}",
            tags=["D6"])
    sc.say(now + 4, f"I can hold {pick.dock} at {hhmm(pick.start)} for you for 10 minutes. Reply YES to request it.")
    sc.step(now + 6, "DRIVER", "YES")
    status, detail = e.request("SHP1020", group, now=now + 6, request_key="wa-THR010-yes")
    sc.step(now + 6, "ENGINE", f"REQUEST → {status}; warehouse asked to confirm", tags=["D6"])
    sc.step(now + 12, "FACILITY", "Warehouse: accepted, ref WH-JAI-9120.")
    ok, apt = e.confirm("SHP1020", group, eta4["planning_eta"], now=now + 12, warehouse_ref="WH-JAI-9120")
    sc.step(now + 12, "ENGINE", f"CONFIRM after re-check → {ok}: {apt} replaces APT1020", tags=["D6"])
    sc.say(now + 12, f"Confirmed: {pick.dock} {hhmm(pick.start)} today, ref WH-JAI-9120.")
    sc.outcome = ("THR003 clarified → pending 11:00 found unsafe, new slot requested; THR010 clarified → "
                  "SHP1020 HELD → REQUESTED → CONFIRMED")
    return sc


def s3_rules(r: Runner) -> Scenario:
    sc = Scenario("S3", "Rules and compatibility narrow the choices",
                  {"ladder": ["L3"], "run": ["R3"], "decisions": ["D4", "D11"]},
                  "The written feasibility check run on three shipments (reefer, heavy, reefer during "
                  "maintenance) plus RULE005; the assessment agent can only pick what passes.", r)
    e = sc.engine
    rows = []

    def assess(sid: str, eta_s: str, now_s: str) -> None:
        s, eta, now = e.shipment(sid), to_minutes(eta_s), to_minutes(now_s)
        checks = e.check(sid, eta, now=now)
        options = []
        for d in e.docks():
            dock_checks = [c for c in checks if c.dock == d["dock_code"] and c.start >= eta]
            free = next((c for c in dock_checks if c.feasible), None)
            options.append({"id": d["dock_id"], "dock_type": d["dock_type"],
                            "supports_refrigerated": bool(d["supports_refrigerated"]),
                            "dock_max_weight_kg": d["max_vehicle_weight_kg"],
                            "vehicle_capacity_kg": s["vehicle_capacity_kg"],
                            "dock_available": free is not None,
                            "first_feasible_start": hhmm(free.start) if free else None})
        payload = {"load": {"shipment_id": sid, "weight_kg": s["load_weight_kg"],
                            "required_dock_type": s["required_dock_type"],
                            "temperature_control_required": bool(s["temperature_control_required"]),
                            "eta": eta_s}, "options": options}
        allowed = feasible_option_ids(payload)
        payload["deterministic_feasibility"] = {"feasible_ids": allowed}
        exp = Expect({"PROPOSE"}, selected=set(allowed)) if allowed else Expect({"NO_FEASIBLE_OPTION", "ESCALATE"}, selected=set())
        sc.step(now, "SYSTEM", f"Assess {sid}: {s['load_weight_kg']} kg, {s['required_dock_type']}, "
                f"reefer={bool(s['temperature_control_required'])}, ETA {eta_s}", tags=["L3"])
        sc.ask(now, AgentRole.ASSESSMENT, payload, allowed, exp, f"Assessment {sid}", ["D4"])
        first = next((c for c in checks if c.feasible), None)
        sample = [c.as_dict() for c in checks if c.start >= eta][:12]
        rows.append({"shipment": sid, "feasible_docks": [o["id"][-2:] for o in options if o["id"] in allowed],
                     "first_feasible": f"{first.dock} {hhmm(first.start)}" if first else "NONE",
                     "sample_rejections": [f"{c['dock']} {c['start']}: {c['reasons'][0]}" for c in sample if c["reasons"]][:4]})
        sc.step(now, "ENGINE", f"D4 check {sid}: first feasible = {rows[-1]['first_feasible']}", tags=["D4"], data=rows[-1])

    assess("SHP1010", "10:55", "09:38")
    assess("SHP1016", "12:00", "09:39")
    sc.step(to_minutes("09:39"), "DRIVER", "[THR006 / DRV007] Heavy trailer ETA 12:00. Original heavy-bay slot will be missed. What is next?")
    dispatch(sc, to_minutes("09:39"), "SHP1016", to_minutes("12:00"), request="REPLAN heavy load", expect_ids={"SLOT-JAI-074"}, tags=["D4"])
    sc.say(to_minutes("09:39"), "The next heavy bay is D6 at 12:30 (12:30–14:00). D6 11:00 is taken by another heavy "
                                "truck and starts before your ETA. Shall I hold 12:30 for you?")
    sc.step(to_minutes("09:41"), "DRIVER", "D4 looks empty, put me there, I'll be quick.", tags=["H"])
    sc.say(to_minutes("09:41"), "I can't book D4: your load is 31,000 kg and D4 takes up to 25,000 kg; site rule RULE004 "
                                "sends anything over 25,000 kg to the heavy dock. D6 12:30 is the earliest safe option.",
           tags=["D11"])
    late = [c.as_dict() for c in e.check("SHP1017", to_minutes("20:30"), now=to_minutes("10:00")) if c.start >= to_minutes("21:00")][:2]
    sc.step(to_minutes("10:00"), "ENGINE", "RULE005 check: any 21:00 start is rejected without manual approval.",
            tags=["D4", "D12"], data=late)
    assess("SHP1015", "18:30", "16:24")
    sc.outcome = "D5 only for SHP1010; D6 12:30 for SHP1016; no dock for SHP1015 at 18:30; D4 refused for heavy load"
    sc.metrics["feasibility_table"] = rows
    return sc


def s4_competition(r: Runner) -> Scenario:
    sc = Scenario("S4", "Five delayed trucks chase the same slots",
                  {"ladder": ["L4"], "run": ["R3", "R4"], "decisions": ["D5", "D7", "D12", "D13"]},
                  "All five want the 11:00–13:00 standard capacity; D2 12:00 is the only slot several can use. "
                  "A written ordering policy allocates them; a thread race proves one slot cannot be double-held.", r)
    e, now = sc.engine, to_minutes("09:45")
    trucks = {
        "SHP1014": {"eta": "11:25", "conf": "HIGH", "reported": "09:44", "msg": "THR004: ETA 11:25. Urgent, first possible dock?"},
        "SHP1006": {"eta": "11:20", "conf": "HIGH", "reported": "09:34", "msg": "THR001: Reaching around 11:20. Any slot after 12?", "not_before": "12:00"},
        "SHP1012": {"eta": "11:10", "conf": "HIGH", "reported": "09:29", "msg": "THR002: Reach 11:10, must leave before 1:30.", "finish_by": "13:30"},
        "SHP1013": {"eta": "11:00", "conf": "LOW", "reported": "09:19", "msg": "THR003: late by one hour (pending D2 11:00)"},
        "SHP1009": {"eta": "10:50", "conf": "HIGH", "reported": "09:10", "msg": "Confirmed D4 11:00, 75-min unload"},
    }
    for sid, t in trucks.items():
        sc.step(now, "DRIVER" if sid != "SHP1009" else "SYSTEM", f"[{sid}] {t['msg']}", tags=["L4"])
    audit = []
    for sid, apt, slot, issue in (
            ("SHP1014", "APT1014A", "SLOT-JAI-004", "pending on D1 (20,000 kg limit) but load is 21,000 kg; 75-min unload would also spill into SHP1017's D1 12:00"),
            ("SHP1009", "APT1009", "SLOT-JAI-046", "75-min unload in a 60-min slot needs D4 12:00 too (implicit claim)"),
            ("SHP1006", "APT1006", "SLOT-JAI-045", "D4 10:00 will be missed (ETA 11:20); driver's statement is against own interest → release")):
        audit.append({"shipment": sid, "appointment": apt, "slot": slot, "issue": issue})
    sc.step(now, "ENGINE", "Pre-allocation audit of current bookings", tags=["D4"], data=audit)
    e.release_appointment("APT1014A", now=now, reason="Infeasible: load exceeds D1 weight limit")
    e.release_appointment("APT1006", now=now, reason="Driver reported ETA after slot")
    sc.step(now, "ENGINE", "Released APT1014A (infeasible) and APT1006 (will be missed). SHP1009 keeps D4 11:00 + 12:00.")
    policy = ("1) CRITICAL > HIGH > NORMAL > LOW. 2) Same priority: fewer feasible options first. "
              "3) LOW-confidence ETA is not held until clarified. 4) Then earliest report. "
              "Each truck gets its earliest feasible start; ties go to the dock that leaves others most options.")
    sc.step(now, "ENGINE", "D7 ordering policy: " + policy, tags=["D7"])

    def kw(t: dict[str, Any]) -> dict[str, Any]:
        out = {}
        if t.get("not_before"):
            out["not_before"] = to_minutes(t["not_before"])
        if t.get("finish_by"):
            out["finish_by"] = to_minutes(t["finish_by"])
        return out

    queue = [s for s in trucks if s != "SHP1009"]
    queue.sort(key=lambda s: (PRIORITY_RANK[e.shipment(s)["priority_code"]], trucks[s]["conf"] == "LOW",
                              len(e.feasible(s, to_minutes(trucks[s]["eta"]), now=now, **kw(trucks[s]))),
                              trucks[s]["reported"]))
    sc.step(now, "ENGINE", "Processing order: " + " → ".join(queue), tags=["D7"])
    allocation, unplaced = {}, []
    for sid in queue:
        t = trucks[sid]
        eta = to_minutes(t["eta"])
        if t["conf"] == "LOW":
            sc.step(now, "ENGINE", f"{sid}: LOW-confidence ETA → keep pending APT1013A, no new hold; clarify first.", tags=["D3", "D7"])
            continue
        feas = e.feasible(sid, eta, now=now, **kw(t))
        if not feas:
            unplaced.append(sid)
            dispatch(sc, now, sid, eta, request="REPLAN under contention", expect_ids=set(), tags=["D7"], **kw(t))
            continue
        earliest = [c for c in feas if c.start == feas[0].start]
        others = [s for s in queue if s not in allocation and s != sid and trucks[s]["conf"] != "LOW"]

        def remaining(option: SlotCheck) -> int:
            taken = set(option.slot_ids)
            return sum(len([c for c in e.feasible(o, to_minutes(trucks[o]["eta"]), now=now, **kw(trucks[o]))
                            if not taken & set(c.slot_ids)]) for o in others)

        best = max(earliest, key=remaining)
        _, rec = dispatch(sc, now, sid, eta, request="REPLAN under contention", expect_ids={best.slot_ids[0]},
                          tags=["D7"], **kw(t))
        ok, group = e.hold(sid, best, eta, now=now, ttl=30)
        allocation[sid] = {"dock": best.dock, "start": hhmm(best.start), "slots": best.slot_ids, "held": ok}
        sc.step(now, "ENGINE", f"HOLD {sid} → {best.dock} {hhmm(best.start)} ({'+'.join(best.slot_ids)}): {ok}", tags=["D5", "D6"])
    for sid in unplaced:
        t = trucks[sid]
        alt = e.feasible(sid, to_minutes(t["eta"]), now=now)
        late_by = alt[0].end - to_minutes(t["finish_by"]) if alt and t.get("finish_by") else None
        sc.escalate(now, {
            "case_id": f"ESC-{sid}", "shipment_id": sid, "driver_id": e.shipment(sid)["driver_id"], "facility_id": "FAC-JAI-01",
            "reason": "No slot meets the driver's 13:30 leave-by after higher-priority trucks were placed",
            "constraints": {"eta": t["eta"], "finish_by": t.get("finish_by")},
            "options_checked": len(e.check(sid, to_minutes(t["eta"]), now=now)),
            "blocking_rules": ["D2 12:00 held by CRITICAL SHP1014", "unload 45 min"],
            "recommended_actions": [f"Offer {alt[0].dock} {hhmm(alt[0].start)} (unload ends {hhmm(alt[0].end)}, {late_by} min past driver limit)" if alt else "none",
                                    "Or move CRITICAL SHP1014 from D2 12:00 to D4 13:00 to free D2 12:00 — needs approval"],
            "deadline": "10:15", "owner": "Shift coordinator", "channel": "Coordinator queue", "delivery_status": "QUEUED",
        }, tags=["D12", "R6"])
    sc.step(now, "ENGINE", "Final allocation", tags=["D7"], data=allocation)
    violations = 0
    placed = {s: a for s, a in allocation.items()}
    for hi in placed:
        for lo in placed:
            if PRIORITY_RANK[e.shipment(hi)["priority_code"]] < PRIORITY_RANK[e.shipment(lo)["priority_code"]] \
                    and to_minutes(placed[lo]["start"]) < to_minutes(placed[hi]["start"]) \
                    and to_minutes(placed[lo]["start"]) >= to_minutes(trucks[hi]["eta"]):
                violations += 1
    race = race_for_slot("SLOT-JAI-020", ["SHP1006", "SHP1012", "SHP1017"], to_minutes("11:20"), now)
    sc.step(now, "ENGINE", f"D13 concurrency proof: 3 threads, separate connections, same slot SLOT-JAI-020 → "
            f"{race['winners']} winner, {race['live_holds_after']} live hold", tags=["D13"], data=race)
    std = [s for s in e.slots() if s["dock_type"] == "STANDARD" and to_minutes("11:00") <= to_minutes(s["slot_start_ts"]) < to_minutes("14:00")]
    board = e.board(now)
    usable = [s for s in std if board[s["slot_id"]][0] != "BLOCKED"]
    used = [s for s in usable if board[s["slot_id"]][0] != "FREE"]
    sc.metrics.update({"priority_violations": violations, "race": race,
                       "utilisation_11_14_standard": round(len(used) / len(usable), 2), "allocation": allocation})
    sc.outcome = (f"{len(allocation)} placed, {len(unplaced)} escalated, 0 double-bookings, "
                  f"{violations} priority violations, race winners={race['winners']}")
    return sc


def s5_world_changes(r: Runner) -> Scenario:
    sc = Scenario("S5", "The world changes mid-conversation",
                  {"ladder": ["L5"], "run": ["R5", "R3"], "decisions": ["D5", "D6", "D9", "D12", "H"]},
                  "D3 breaks down under a waiting truck; a duplicate message arrives; a held option expires "
                  "and is taken; the driver's late 'yes' is caught as stale and re-offered; a retry and a "
                  "replayed cancellation are no-ops.", r)
    e = sc.engine
    now = to_minutes("09:21")
    sc.step(to_minutes("09:16"), "SYSTEM", "DEVT001: D3 hydraulic leveller failure 09:15–13:00 (logged by facility).", tags=["L5"])
    sc.step(now, "FACILITY", "[OM003] SHP1005 is at the yard and cannot use D3 because of the leveller breakdown. Please advise reassignment.")
    sc.ask(now, AgentRole.TRIAGE, {
        "message": {"source": "FACILITY", "id": "OM003", "text": "SHP1005 is at the yard and cannot use D3 because of the leveller breakdown."},
        "dock_event": {"id": "DEVT001", "dock": "D3", "type": "BREAKDOWN", "start": "09:15", "end": "13:00", "logged_by": "facility"},
        "linked": shipment_card(e, "SHP1005", gate_check_in="CHK1005 09:05"),
        "independent_confirmation": ["DEVT001 facility log", "OM003 facility email", "CHK1005 gate check-in"],
    }, None, Expect({"PROPOSE", "ESCALATE"}, verification={"VERIFIED"}), "Triage OM003 (facility source)", ["H"])
    e.release_appointment("APT1005", now=now, reason="D3 breakdown DEVT001")
    feas = e.feasible("SHP1005", now, now=now)
    sc.step(now, "ENGINE", f"APT1005 released. SHP1005 (20,500 kg, 75 min) earliest: {feas[0].dock} {hhmm(feas[0].start)} "
            f"— a {feas[0].start - to_minutes('09:05')}-min yard wait for a HIGH load at the gate.", tags=["D4"])
    sc.step(now, "ENGINE", "Replay of cancellation APT1008 (customer cancelled 08:20): "
            f"first release={e.remember('cancel:APT1008', now, 'APPLIED')}, replay applied={e.remember('cancel:APT1008', now, 'APPLIED')} "
            "→ idempotent, SLOT-JAI-017 (D2 10:00) is free but too short for a 75-min unload before D2 11:00.", tags=["D9"])
    ok, g5 = e.hold("SHP1005", feas[0], now, now=now, ttl=30)
    sc.escalate(now, {
        "case_id": "ESC-SHP1005", "shipment_id": "SHP1005", "driver_id": "DRV005", "facility_id": "FAC-JAI-01",
        "reason": "Truck at gate since 09:05; D3 down; earliest safe dock means >3 h wait",
        "constraints": {"weight_kg": 20500, "unload_min": 75, "docks": "D2/D4 only"},
        "options_checked": len(e.check("SHP1005", now, now=now)), "blocking_rules": ["DEVT001", "D1/D3 20,000 kg limit"],
        "recommended_actions": [f"Accept held {feas[0].dock} {hhmm(feas[0].start)}", "Approve 15-min overlap at D2 10:00 before SHP1013 (pending, LOW confidence)"],
        "deadline": "09:51", "owner": "Shift coordinator (Anita Joshi)", "channel": "Coordinator queue", "delivery_status": "QUEUED",
    }, tags=["D12"])

    now = to_minutes("09:34")
    t = "Traffic after Shahpura. Reaching around 11:20. Any slot after 12?"
    sc.step(now, "DRIVER", f"[THR001 / DRV006 · wa-9001] {t}", tags=["L5"])
    e.remember("wa-9001", now, "PROCESSED")
    sc.ask(now, AgentRole.TRIAGE, {
        "message": {"thread_id": "THR001", "sender": "DRV006", "text": t, "external_id": "wa-9001"},
        "linked": shipment_card(e, "SHP1006"), "independent_confirmation": [],
    }, None, Expect({"PROPOSE"}, verification={"UNVERIFIED"}), "Triage THR001", ["H"])
    e.release_appointment("APT1006", now=now, reason="Driver reported ETA after slot")
    feasible, rec = dispatch(sc, now, "SHP1006", to_minutes("11:20"), request="REPLAN after 12:00",
                             expect_ids=None, tags=["D5"], not_before=to_minutes("12:00"))
    sc.step(now, "ENGINE", f"D5: D2 12:00 is HELD for SHP1005 (coordinator deciding) → not offered as available.", tags=["D5"])
    pick = next((c for c in feasible if c.slot_ids[0] == rec["guarded"].get("selected_id")), feasible[0])
    ok, g6 = e.hold("SHP1006", pick, to_minutes("11:20"), now=now)
    sc.say(now, f"Earliest after 12: {pick.dock} {hhmm(pick.start)}. Held for 10 minutes (until {hhmm(now + 10)}). Reply YES to request it.", tags=["D6"])

    dup = "Traffic after Shahpura. Reaching around 11:20. Any slot after 12?"
    sc.step(now + 1, "DRIVER", f"[THR009 / DRV006 · wa-9009, is_duplicate=1] {dup}", tags=["D9"])
    sc.ask(now + 1, AgentRole.TRIAGE, {
        "message": {"thread_id": "THR009", "sender": "DRV006", "text": dup, "external_id": "wa-9009"},
        "already_processed": {"thread_id": "THR001", "external_id": "wa-9001", "same_text": True, "seconds_apart": 62,
                              "dedupe_key": "DRV006-SHP1006-20260804-0934"},
        "independent_confirmation": [],
    }, None, Expect({"DUPLICATE"}), "Triage THR009", ["D9"])
    sc.step(now + 1, "ENGINE", "Dedupe key DRV006-SHP1006-20260804-0934 already processed → no second hold, no second alert.", tags=["D9"])

    sc.step(now + 10, "ENGINE", f"{hhmm(now + 10)}: SHP1006 hold expired (no reply): {e.expire_holds(now + 10) or 'already'}", tags=["D6"])
    sc.step(now + 11, "COORDINATOR", "Anita Joshi approves SHP1005 on the held D2 12:00 (ESC-SHP1005 closed).", tags=["D12"])
    sc.metrics["coordinator_action"] = "approved held option"
    e.request("SHP1005", g5, now=now + 11, request_key="coord-SHP1005")
    e.confirm("SHP1005", g5, now, now=now + 11, warehouse_ref="WH-JAI-9105")
    sc.step(now + 12, "DRIVER", "[THR003 / DRV013 · SHP1013] Reaching gate 11:00, no hurry after that.")
    eta13 = revise_eta("Reaching gate 11:00", now + 12, [(to_minutes("10:00"), "PLANNER")])["planning_eta"]
    taker = e.feasible("SHP1013", eta13, now=now + 12)
    opt = next((c for c in taker if set(c.slot_ids) & set(pick.slot_ids)), taker[0])
    _, g13 = e.hold("SHP1013", opt, eta13, now=now + 12)
    sc.step(now + 12, "ENGINE", f"SHP1013 (planning ETA {hhmm(eta13)}) is offered and holds its earliest option "
            f"{opt.dock} {hhmm(opt.start)} — " + ("the same slot SHP1006 was shown, now free after expiry." if opt.slot_ids == pick.slot_ids
                                                    else "a different slot."), tags=["R5", "D5"])
    sc.step(now + 13, "DRIVER", "[THR003 / DRV013] YES")
    sc.step(now + 13, "ENGINE", "SHP1013 request → " + e.request("SHP1013", g13, now=now + 13, request_key="wa-THR003-yes")[0],
            tags=["D6"])

    sc.step(now + 18, "DRIVER", f"[THR001 · wa-9013] YES book {pick.dock} {hhmm(pick.start)}", tags=["R5"])
    status, detail = e.request("SHP1006", g6, now=now + 18, request_key="wa-9013")
    sc.metrics["stale_caught"] += status == "STALE"
    sc.step(now + 18, "ENGINE", f"REQUEST re-check → {status} ({detail})", tags=["D9"])
    feasible2, rec2 = dispatch(sc, now + 18, "SHP1006", to_minutes("11:20"), request="RE-OFFER after stale option",
                               expect_ids=None, tags=["D9"], not_before=to_minutes("12:00"))
    pick2 = next((c for c in feasible2 if c.slot_ids[0] == rec2["guarded"].get("selected_id")), feasible2[0])
    ok, g6b = e.hold("SHP1006", pick2, to_minutes("11:20"), now=now + 18)
    sc.say(now + 18, f"Sorry — {pick.dock} {hhmm(pick.start)} was released when the 10-minute hold ran out and another "
                     f"truck has requested it. Next option: {pick2.dock} {hhmm(pick2.start)}, held until {hhmm(now + 28)}. Reply YES.",
           tags=["D11", "D9"])
    sc.step(now + 19, "DRIVER", "[wa-9014] YES")
    s1, _ = e.request("SHP1006", g6b, now=now + 19, request_key="wa-9014")
    sc.step(now + 19, "DRIVER", "[wa-9014 retry by WhatsApp] YES", tags=["D9"])
    s2, _ = e.request("SHP1006", g6b, now=now + 19, request_key="wa-9014")
    sc.step(now + 19, "ENGINE", f"First YES → {s1}; retried YES → {s2} (idempotent, one request).", tags=["D9"])
    sc.step(now + 25, "FACILITY", "Warehouse accepts SHP1006, ref WH-JAI-9106.")
    ok, apt = e.confirm("SHP1006", g6b, to_minutes("11:20"), now=now + 25, warehouse_ref="WH-JAI-9106")
    sc.step(now + 25, "ENGINE", f"CONFIRM after re-check → {ok} ({apt})", tags=["D6"])
    sc.say(now + 25, f"Confirmed: {pick2.dock} {hhmm(pick2.start)}, ref WH-JAI-9106.")
    sc.outcome = f"Stale option caught at request; re-offered and confirmed {pick2.dock} {hhmm(pick2.start)}; duplicate + retry + cancel replay were no-ops"
    return sc


def s6_no_slot(r: Runner) -> Scenario:
    sc = Scenario("S6", "No feasible slot: escalate with a complete hand-off",
                  {"ladder": ["L6"], "run": ["R6"], "decisions": ["D10", "D12", "D11"]},
                  "Reefer SHP1015 now arrives 18:30 while D5 (the only reefer dock) is in maintenance 18:00–22:00 "
                  "and RULE005 bars new starts from 21:00. The original OM004 escalation email FAILED.", r)
    e, now = sc.engine, to_minutes("16:24")
    t = "Evening traffic. ETA 6:30. Can the reefer unload tonight?"
    sc.step(now, "DRIVER", f"[THR005 / DRV015] {t}", tags=["L6"])
    eta = revise_eta(t, now, [(to_minutes("17:30"), "PLANNER")])
    sc.step(now, "ENGINE", f"D3: {eta['rule']}", tags=["D3"], data=eta)
    sc.ask(now, AgentRole.TRIAGE, {
        "message": {"thread_id": "THR005", "sender": "DRV015", "text": t},
        "linked": shipment_card(e, "SHP1015"), "independent_confirmation": [],
    }, None, Expect({"PROPOSE", "ESCALATE"}, verification={"UNVERIFIED"}), "Triage THR005", ["H"])
    feasible, _ = dispatch(sc, now, "SHP1015", eta["planning_eta"], request="REPLAN reefer", expect_ids=set(), tags=["D4"])
    blockers = sorted({rsn for c in e.check("SHP1015", eta["planning_eta"], now=now) if c.dock == "D5" and c.start >= 1080 for rsn in c.reasons})
    sc.step(now, "ENGINE", "No safe automatic outcome. D5 blockers: " + "; ".join(blockers), tags=["R6"], data=blockers)
    sc.step(now + 1, "SYSTEM", "OM004 to night.shift@setuhaul.example: delivery_status=FAILED. Audit: FAC-JAI-01 has no "
            "night-shift contact (the only one, CON005, is FAC-GGN-01 and vacant) → retry to CON003 shift manager by phone.",
            tags=["D10"])
    phone = dict(e.db.execute("SELECT * FROM facility_contacts WHERE contact_id='CON003'").fetchone())
    sc.escalate(now + 1, {
        "case_id": "ESC-SHP1015", "shipment_id": "SHP1015", "driver_id": "DRV015", "facility_id": "FAC-JAI-01",
        "reason": "Reefer load ETA 18:30; only reefer dock D5 under maintenance 18:00–22:00 (DEVT002); RULE005 bars starts ≥21:00",
        "constraints": {"eta": "18:30", "temperature_control": True, "weight_kg": 15000, "unload_min": 60},
        "options_checked": len(e.check("SHP1015", eta["planning_eta"], now=now)),
        "blocking_rules": ["RULE003 reefer → D5", "DEVT002 D5 maintenance 18:00–22:00", "RULE005 no start ≥21:00"],
        "recommended_actions": ["Approve D5 22:00 start (RULE005 exception, reefer kept running)",
                                "Or hold truck with reefer unit on until 08:00 next day",
                                "Or divert to FAC-GGN-01 D3 (reefer) — needs customer approval"],
        "deadline": "17:30 (before D5 maintenance)", "owner": f"{phone['contact_name']} ({phone['contact_role']})",
        "channel": f"PHONE {phone['phone']} (email OM004 failed)",
        "delivery_status": "SENT",
    }, tags=["D10", "D12"])
    sc.say(now + 1, "I can't find a safe reefer slot tonight: the only cold dock, D5, is in maintenance from 18:00 to 22:00. "
                    "I've passed this to the Jaipur shift manager with your details — they'll call you before 17:30. "
                    "Please keep the reefer unit running and don't come to the gate before you hear from them.",
           tags=["D10", "D11"])
    sc.outcome = "ESCALATED (no feasible slot); failed email retried by phone; driver told what happens next"
    return sc


def s7_resequence(r: Runner) -> Scenario:
    sc = Scenario("S7", "Early, late and in-progress trucks share one facility",
                  {"ladder": ["L7"], "run": ["R7"], "decisions": ["D8", "H"]},
                  "The scheduler recalculates on a gate check-in or a facility update, not on every message. "
                  "SHP1002 overruns D2, SHP1004 checks in late, SHP1003 came early.", r)
    e = sc.engine
    now = to_minutes("09:25")
    sc.step(to_minutes("08:20"), "SYSTEM", "CHK1003: SHP1003 gate-in 08:20 (40 min early for D1 09:00).", tags=["L7"])
    sc.step(to_minutes("09:10"), "FACILITY", "CHK1002 update: SHP1002 still unloading on D2 (started 08:08, 70 min planned); "
            "yard estimates finish ≈09:45.", tags=["D8"])
    sc.step(to_minutes("09:10"), "ENGINE", "Facility WIP update is a recalculation trigger: APT1002's overrun covers "
            "SLOT-JAI-016 (SHP1004's D2 09:00).", tags=["D8"])
    sc.step(now, "SYSTEM", "CHK1004: SHP1004 gate-in 09:25 — 25 min after its D2 09:00 slot (RULE002 grace 30 min).", tags=["D8"])
    triggers = ["gate check-in", "facility WIP update", "verified dock event", "confirmed ETA change ≥15 min"]
    sc.step(now, "ENGINE", "D8 recalculation trigger fired: gate check-in CHK1004. Triggers: " + ", ".join(triggers)
            + ". Unverified driver messages do not trigger a recalculation.", tags=["D8"])
    d2_free = to_minutes("09:45") + 10
    plan_before = [
        {"shipment": "SHP1003", "gate_in": "08:20", "dock_in": "09:00", "dock": "D1", "note": "waits for booked slot"},
        {"shipment": "SHP1004", "gate_in": "09:25", "dock_in": "10:00", "dock": "D2", "note": "naive: treated as missed, next free D2 slot (SLOT-017, freed by APT1008)"},
        {"shipment": "SHP1005", "gate_in": "09:05", "dock_in": "12:00", "dock": "D2", "note": "D3 down; earliest D2/D4 pair (see S5)"},
    ]
    plan_after = [
        {"shipment": "SHP1003", "gate_in": "08:20", "dock_in": "08:50", "dock": "D1", "note": "D1 idle after SHP1001 left 08:50; nobody else waiting or due"},
        {"shipment": "SHP1004", "gate_in": "09:25", "dock_in": hhmm(d2_free), "dock": "D2",
         "note": "within RULE002 grace: keeps own booking; called when SHP1002 clears + 10 min turnover"},
        {"shipment": "SHP1005", "gate_in": "09:05", "dock_in": "12:00", "dock": "D2", "note": "unchanged"},
    ]

    def avg_wait(plan: list[dict[str, str]]) -> float:
        return round(sum(to_minutes(p["dock_in"]) - to_minutes(p["gate_in"]) for p in plan) / len(plan), 1)

    sc.step(now, "ENGINE", "Proposed new sequence (needs facility OK before any truck moves)", tags=["R7", "D12"],
            data={"before": plan_before, "after": plan_after, "avg_wait_before": avg_wait(plan_before), "avg_wait_after": avg_wait(plan_after)})
    sc.step(now + 1, "FACILITY", "Yard supervisor: agreed — call SHP1004 to D2 as soon as SHP1002 clears.")
    t = "I reached late and am in the yard. Has a new dock been assigned?"
    sc.step(now + 2, "DRIVER", f"[THR008 / DRV004] {t}", tags=["H"])
    sc.ask(now + 2, AgentRole.TRIAGE, {
        "message": {"thread_id": "THR008", "sender": "DRV004", "text": t},
        "linked": shipment_card(e, "SHP1004"),
        "gate_check_in": {"id": "CHK1004", "gate_in": "09:25", "queue_state": "WAITING_LATE"},
        "facility_update": "SHP1002 finishing on D2 about 09:45; SHP1004 keeps D2 booking",
        "independent_confirmation": ["CHK1004 gate check-in 09:25", "yard supervisor update"],
    }, None, Expect({"PROPOSE", "NO_ACTION"}, verification={"VERIFIED"}), "Triage THR008 (claim + gate record)", ["H"])
    sc.say(now + 2, f"You keep your D2 booking. D2 is finishing the previous truck; expected call-in around {plan_after[1]['dock_in']}. "
                    "Please stay in the yard queue.")
    sc.metrics.update({"avg_wait_before": avg_wait(plan_before), "avg_wait_after": avg_wait(plan_after)})
    sc.outcome = f"Resequenced on check-in: avg gate-in→dock-in {avg_wait(plan_before)} → {avg_wait(plan_after)} min"
    return sc


def s8_human_side(r: Runner) -> Scenario:
    sc = Scenario("S8", "Human side: claims, pressure and refusals",
                  {"ladder": ["L7"], "run": ["R1"], "decisions": ["D11", "D12", "H"]},
                  "Drivers may exaggerate. Verified claims (gate records) are acted on; unverified ones are checked "
                  "with the facility; nothing is penalised or booked on a claim alone.", r)
    e = sc.engine
    now = to_minutes("08:22")
    t = "I am already at the gate. Can I get a dock now instead of waiting for 9?"
    sc.step(now, "DRIVER", f"[THR007 / DRV003] {t}", tags=["H"])
    sc.ask(now, AgentRole.TRIAGE, {
        "message": {"thread_id": "THR007", "sender": "DRV003", "text": t},
        "linked": shipment_card(e, "SHP1003"),
        "gate_check_in": {"id": "CHK1003", "gate_in": "08:20", "queue_state": "WAITING_EARLY"},
        "independent_confirmation": ["CHK1003 gate check-in 08:20"],
    }, None, Expect({"NO_ACTION", "PROPOSE", "CLARIFY"}, verification={"VERIFIED"}), "Triage THR007 (claim + gate record)", ["H"])
    sc.say(now, "You're checked in at 08:20, thanks. Arriving early doesn't move you ahead of trucks already booked or "
                "waiting; if a dock is idle with no one due, the yard will call you in earlier.", tags=["D11"])

    now = to_minutes("10:00")
    t = "I'm at the gate now, give me a dock."
    sc.step(now, "DRIVER", f"[DRV012 / SHP1012] {t}", tags=["H"])
    sc.ask(now, AgentRole.TRIAGE, {
        "message": {"sender": "DRV012", "text": t, "sent_at": "10:00"},
        "linked": shipment_card(e, "SHP1012"),
        "eta_history": [{"id": "ETA007", "eta": "11:10", "confidence": "HIGH", "created": "09:30", "note": "Tyre repaired"}],
        "gate_check_in": None, "independent_confirmation": [],
    }, None, Expect({"CLARIFY"}, ask_whom={"FACILITY"}, verification={"UNVERIFIED"}), "Triage arrival claim (no check-in)", ["H"])
    sc.step(now + 3, "FACILITY", "Gate: no truck WB23GT4112 (VEH012) at the gate.")
    sc.ask(now + 3, AgentRole.TRIAGE, {
        "message": {"source": "FACILITY", "text": "Gate: no truck WB23GT4112 (VEH012) at the gate."},
        "claim": {"sender": "DRV012", "text": t},
        "independent_confirmation": ["gate log via facility"],
    }, None, Expect({"CLARIFY", "NO_ACTION"}, verification={"VERIFIED"}), "Triage facility reply", ["H"])
    sc.say(now + 3, "The gate hasn't logged your truck yet, so I can't call you to a dock. When you reach the gate, "
                    "check in there and I'll pick it up straight away. Your last ETA was 11:10.", tags=["D11"])
    sc.step(now + 3, "ENGINE", "Claim recorded as UNVERIFIED/contradicted; no state change and no penalty. "
            "Repeated contradicted claims go to the coordinator digest, not to the driver.", tags=["D12", "H"])

    now = to_minutes("10:05")
    t = "D2 is broken, nobody is unloading, reassign everyone to D4."
    sc.step(now, "DRIVER", f"[DRV013 / SHP1013] {t}", tags=["H"])
    sc.ask(now, AgentRole.TRIAGE, {
        "message": {"sender": "DRV013", "text": t}, "dock_events_for_D2": ["DEVT003 capacity reduction 08:00–09:20 (ended)"],
        "independent_confirmation": [],
    }, None, Expect({"CLARIFY"}, ask_whom={"FACILITY"}, verification={"UNVERIFIED"}), "Triage outage claim", ["H"])
    sc.step(now + 4, "FACILITY", "D2 is working normally.")
    sc.step(now + 4, "ENGINE", "Outage not applied (facility denied). D2 bookings unchanged.", tags=["H"])

    now = to_minutes("10:12")
    sc.step(now, "DRIVER", "[THR012 / DRV008] Is my 4 PM slot still active?")
    sc.ask(now, AgentRole.TRIAGE, {
        "message": {"thread_id": "THR012", "sender": "DRV008", "text": "Is my 4 PM slot still active?"},
        "linked": shipment_card(e, "SHP1019"), "operational_message": {"id": "OM005", "text": "Shipment SHP1019 and its 4 PM appointment are cancelled. Do not travel to the facility.", "status": "DELIVERED"},
        "independent_confirmation": ["OM005 dispatch SMS"],
    }, None, Expect({"NO_ACTION"}), "Triage THR012 status", ["D11"])
    sc.say(now, "That load (SHP1019) and its 4 PM slot were cancelled by dispatch at 10:10. Please don't travel to the "
                "facility; contact BlueRoute dispatch for your next job.", tags=["D11"])
    sc.outcome = "2 claims verified by gate records, 1 arrival claim contradicted, 1 outage claim denied — no false state change"
    return sc


SCENARIOS = (s1_options_only, s2_missing_facts, s3_rules, s4_competition, s5_world_changes, s6_no_slot,
             s7_resequence, s8_human_side)


# ---------------------------------------------------------------------------
# Run, score, render
# ---------------------------------------------------------------------------
def run(agent: AgentFn | None, mode: str, model: str) -> dict[str, Any]:
    runner = Runner(agent, mode)
    started = time.monotonic()
    scenarios = []
    for fn in SCENARIOS:
        print(f"Running {fn.__name__} ...", flush=True)
        scenarios.append(fn(runner).to_dict())
    calls = runner.calls
    esc = [x for s in scenarios for x in s["metrics"]["escalations"]]
    measures = {
        "agent_decision_accuracy": round(sum(c["correct"] for c in calls) / max(len(calls), 1), 3),
        "agent_calls": len(calls),
        "agent_errors": sum(c["error"] is not None for c in calls),
        "guard_interventions": sum(bool(c["violations"]) for c in calls),
        "ungrounded_answers": sum(bool(c["ungrounded"]) for c in calls),
        "resolved_without_takeover": round(sum(not s["metrics"]["manual_takeover"] for s in scenarios) / len(scenarios), 2),
        "conflicting_allocations": sum(s["metrics"]["integrity"]["slots_with_two_live_claims"] + s["metrics"]["integrity"]["shipments_with_two_current"] for s in scenarios),
        "options_infeasible_at_recheck": sum(s["metrics"]["rechecks_failed"] for s in scenarios),
        "escalation_completeness": round(sum(x["completeness"] for x in esc) / max(len(esc), 1), 2),
        "escalations": len(esc),
        "clarification_turns": sum(s["metrics"]["clarification_turns"] for s in scenarios),
        "avg_turns_to_outcome": round(sum(s["metrics"]["turns_to_outcome"] for s in scenarios) / len(scenarios), 1),
        "avg_minutes_to_outcome": round(sum(s["metrics"]["minutes_to_outcome"] for s in scenarios) / len(scenarios), 1),
        "priority_violations": sum(s["metrics"].get("priority_violations", 0) for s in scenarios),
        "utilisation_11_14_standard": next((s["metrics"]["utilisation_11_14_standard"] for s in scenarios if "utilisation_11_14_standard" in s["metrics"]), None),
        "avg_wait_before": next((s["metrics"]["avg_wait_before"] for s in scenarios if "avg_wait_before" in s["metrics"]), None),
        "avg_wait_after": next((s["metrics"]["avg_wait_after"] for s in scenarios if "avg_wait_after" in s["metrics"]), None),
        "mean_agent_latency_s": round(sum(c["seconds"] for c in calls) / max(len(calls), 1), 1),
    }
    safety = 1.0 if measures["conflicting_allocations"] == 0 else 0.0
    measures["workflow_confidence"] = round(100 * (0.5 * measures["agent_decision_accuracy"] + 0.3 * safety
                                                   + 0.2 * measures["escalation_completeness"]), 1)
    coverage = {}
    for group, items in (("ladder", LADDER), ("run", RUN_ITEMS), ("decisions", DECISIONS)):
        coverage[group] = {k: {"label": v, "scenarios": [s["id"] for s in scenarios if k in s["covers"].get(group, []) or
                                                         any(k in st["tags"] for st in s["steps"])]}
                           for k, v in items.items()}
    for s in scenarios:
        for st in s["steps"]:
            if st["agent"]:
                st["agent"]["payload"].pop("_offline_expect", None)
    return {"model": model, "mode": mode, "seconds": round(time.monotonic() - started, 1),
            "measures": measures, "coverage": coverage, "scenarios": scenarios,
            "slots": [{"id": s["slot_id"], "dock": s["dock_code"], "start": s["slot_start_ts"][11:16], "end": s["slot_end_ts"][11:16]}
                      for s in SlotEngine().slots()]}


def render(report: dict[str, Any]) -> str:
    data = json.dumps(report, ensure_ascii=False).replace("</", "<\\/")
    return HTML_TEMPLATE.replace("__DATA__", data)


HTML_TEMPLATE = r"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><title>SetuHaul brief scenarios — agent simulation</title>
<meta name="viewport" content="width=device-width, initial-scale=1">
<style>
:root{--bg:#0f172a;--panel:#111827;--ink:#e5e7eb;--mut:#94a3b8;--ok:#22c55e;--bad:#ef4444;--warn:#f59e0b;--acc:#38bdf8;--line:#1f2937}
*{box-sizing:border-box}body{margin:0;font:14px/1.45 system-ui,-apple-system,Segoe UI,Roboto,sans-serif;background:var(--bg);color:var(--ink)}
header{padding:20px 28px;border-bottom:1px solid var(--line)}h1{margin:0 0 4px;font-size:22px}h2{font-size:17px;margin:22px 0 10px}
.mut{color:var(--mut)}.wrap{padding:0 28px 40px}
.kpis{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:10px;margin-top:14px}
.kpi{background:var(--panel);border:1px solid var(--line);border-radius:10px;padding:10px 12px}.kpi b{font-size:22px;display:block}
table{border-collapse:collapse;width:100%;font-size:13px}td,th{border-bottom:1px solid var(--line);padding:5px 8px;text-align:left;vertical-align:top}
th{color:var(--mut);font-weight:600}.chip{display:inline-block;padding:1px 7px;border-radius:999px;background:#1e293b;margin:1px 3px 1px 0;font-size:12px;cursor:pointer}
.chip.ok{background:#14532d}.chip.miss{background:#7f1d1d}
.grid3{display:grid;grid-template-columns:repeat(3,1fr);gap:14px}@media(max-width:1100px){.grid3{grid-template-columns:1fr}}
.tabs{display:flex;flex-wrap:wrap;gap:6px;margin:12px 0}.tab{padding:6px 10px;border-radius:8px;background:var(--panel);border:1px solid var(--line);cursor:pointer}
.tab.on{border-color:var(--acc);color:var(--acc)}
.scn{display:grid;grid-template-columns:minmax(380px,1fr) minmax(420px,1.1fr);gap:16px}@media(max-width:1100px){.scn{grid-template-columns:1fr}}
.card{background:var(--panel);border:1px solid var(--line);border-radius:12px;padding:14px}
.ctrl button{background:#1e293b;color:var(--ink);border:1px solid #334155;border-radius:6px;padding:5px 10px;margin-right:4px;cursor:pointer}
.feed{max-height:640px;overflow:auto;padding-right:6px}
.msg{margin:8px 0;padding:8px 10px;border-radius:10px;border-left:4px solid #334155;background:#0b1220;opacity:.25;transition:opacity .2s}
.msg.shown{opacity:1}.msg.cur{outline:1px solid var(--acc)}
.msg .who{font-size:11px;font-weight:700;letter-spacing:.04em;margin-bottom:2px}
.DRIVER{border-color:#f59e0b}.FACILITY{border-color:#a78bfa}.SYSTEM{border-color:#64748b}.ENGINE{border-color:#38bdf8}
.TRIAGE,.DISPATCH,.ASSESSMENT{border-color:#22c55e}.AGENT\2192DRIVER{border-color:#10b981;background:#062e25}
.ESCALATION{border-color:#ef4444;background:#2a0b0b}.COORDINATOR{border-color:#ec4899}
.verdict{float:right;font-size:11px;padding:1px 6px;border-radius:5px}.verdict.ok{background:#14532d}.verdict.bad{background:#7f1d1d}
details{margin-top:4px}summary{cursor:pointer;color:var(--mut);font-size:12px}pre{white-space:pre-wrap;word-break:break-word;font-size:11.5px;background:#020617;padding:8px;border-radius:6px;max-height:300px;overflow:auto}
.tag{font-size:10.5px;color:var(--acc);margin-left:4px}
.board{display:grid;grid-template-columns:34px repeat(28,1fr);gap:1px;font-size:10px;margin-top:8px}
.board .h{color:var(--mut);text-align:center}.board .d{color:var(--mut);font-weight:700;display:flex;align-items:center}
.cell{height:30px;border-radius:3px;display:flex;align-items:center;justify-content:center;overflow:hidden;white-space:nowrap;font-size:9px;transition:background .25s}
.FREE{background:#1e293b}.BOOKED{background:#2563eb}.PENDING{background:#7c3aed}.HELD{background:#f59e0b;color:#111}.REQUESTED{background:#fb923c;color:#111}
.WIP{background:#0891b2}.BLOCKED{background:repeating-linear-gradient(45deg,#3f1d1d,#3f1d1d 4px,#552222 4px,#552222 8px)}.OVERRUN{background:#1d4ed8;opacity:.55}
.conf{outline:2px solid var(--bad)}.chg{box-shadow:0 0 0 2px #fff inset}
.legend span{display:inline-block;margin-right:10px;font-size:11px}.legend i{display:inline-block;width:11px;height:11px;border-radius:2px;margin-right:4px;vertical-align:-1px}
.why{color:var(--mut);margin:4px 0 8px}
</style></head><body>
<header><h1>SetuHaul brief scenarios — how the agents handled each problem</h1>
<div class="mut" id="sub"></div><div class="kpis" id="kpis"></div></header>
<div class="wrap">
<h2>Coverage against the brief</h2><div class="mut">Each brief item mapped to the scenario(s) that exercise it. Click a chip to open the scenario.</div>
<div class="grid3" id="cov"></div>
<h2>Measures</h2><div class="card"><table id="meas"></table></div>
<h2>Scenario simulation</h2><div class="tabs" id="tabs"></div><div id="scn"></div>
</div>
<script>
const R = __DATA__;
const $ = s => document.querySelector(s), h = (t, a = {}, ...c) => { const e = document.createElement(t); for (const [k, v] of Object.entries(a)) k === 'class' ? e.className = v : k.startsWith('on') ? e[k] = v : e.setAttribute(k, v); for (const x of c.flat()) e.append(x?.nodeType ? x : document.createTextNode(x ?? '')); return e };
const M = R.measures;
$('#sub').textContent = `Model: ${R.model} · mode: ${R.mode} · ${R.scenarios.length} distinct scenarios · ${M.agent_calls} agent calls · run ${R.seconds}s`;
const pct = x => x == null ? '—' : Math.round(x * 100) + '%';
[['Workflow confidence', M.workflow_confidence + '/100'], ['Agent decision accuracy', pct(M.agent_decision_accuracy)], ['Conflicting allocations', M.conflicting_allocations],
 ['Stale options caught', M.options_infeasible_at_recheck], ['Escalation completeness', pct(M.escalation_completeness)], ['Guard interventions', M.guard_interventions],
 ['Priority violations', M.priority_violations], ['Agent errors', M.agent_errors]].forEach(([k, v]) => $('#kpis').append(h('div', {class: 'kpi'}, h('b', {}, String(v)), h('span', {class: 'mut'}, k))));
const names = {ladder: 'Ladder of situations', run: 'Scenario-run items', decisions: 'Decisions (+ human side)'};
for (const [g, items] of Object.entries(R.coverage)) {
  const t = h('table', {}, h('tr', {}, h('th', {}, 'Item'), h('th', {}, 'Scenarios')));
  for (const [k, v] of Object.entries(items)) t.append(h('tr', {}, h('td', {}, h('b', {}, k + ' '), v.label),
    h('td', {}, v.scenarios.length ? v.scenarios.map(s => h('span', {class: 'chip ok', onclick: () => show(s)}, s)) : h('span', {class: 'chip miss'}, 'MISSING'))));
  $('#cov').append(h('div', {class: 'card'}, h('b', {}, names[g]), t));
}
const mrows = [['Turns to outcome (avg)', M.avg_turns_to_outcome], ['Sim minutes to outcome (avg)', M.avg_minutes_to_outcome],
 ['Resolved without manual takeover', pct(M.resolved_without_takeover)], ['Conflicting allocations', M.conflicting_allocations],
 ['Options found infeasible at re-check', M.options_infeasible_at_recheck], ['Escalations / completeness', `${M.escalations} / ${pct(M.escalation_completeness)}`],
 ['Avg waiting gate-in → dock-in (S7)', `${M.avg_wait_before} → ${M.avg_wait_after} min`], ['Std-dock utilisation 11:00–14:00 (S4)', pct(M.utilisation_11_14_standard)],
 ['Priority-policy violations', M.priority_violations], ['Clarification turns', M.clarification_turns], ['Ungrounded agent evidence', M.ungrounded_answers],
 ['Mean agent latency', M.mean_agent_latency_s + ' s']];
mrows.forEach(([k, v]) => $('#meas').append(h('tr', {}, h('th', {}, k), h('td', {}, String(v)))));
R.scenarios.forEach(s => $('#tabs').append(h('div', {class: 'tab', id: 'tab-' + s.id, onclick: () => show(s.id)}, `${s.id} · ${s.title}`)));
const docks = [...new Set(R.slots.map(s => s.dock))].sort();
const toMin = t => +t.slice(0, 2) * 60 + +t.slice(3);
let timer = null;
function boardAt(s, i) { for (let j = i; j >= 0; j--) if (s.steps[j].board) return [s.steps[j].board, j]; return [null, -1]; }
function drawBoard(el, s, i) {
  const [b, j] = boardAt(s, i), [prev] = j > 0 ? boardAt(s, j - 1) : [null];
  el.innerHTML = ''; el.append(h('div'));
  for (let m = 8 * 60; m < 22 * 60; m += 30) el.append(h('div', {class: 'h'}, m % 60 ? '' : String(m / 60).padStart(2, '0')));
  for (const d of docks) {
    el.append(h('div', {class: 'd'}, d));
    const row = R.slots.filter(x => x.dock === d);
    let col = 0;
    for (const sl of row) {
      const span = Math.round((toMin(sl.end) - toMin(sl.start)) / 30), st = b?.[sl.id] || ['FREE', ''];
      const base = st[0].replace('!', ''), c = h('div', {class: `cell ${base}${st[0].endsWith('!') ? ' conf' : ''}${prev && JSON.stringify(prev[sl.id]) !== JSON.stringify(st) ? ' chg' : ''}`,
        title: `${sl.id} ${sl.start}-${sl.end} ${st[0]} ${st[1]}`}, st[1] ? st[1].replace('SHP', '') : '');
      c.style.gridColumn = `${2 + (toMin(sl.start) - 480) / 30} / span ${span}`; c.style.gridRow = 2 + docks.indexOf(d);
      el.append(c); col += span;
    }
  }
}
function show(id) {
  clearInterval(timer);
  document.querySelectorAll('.tab').forEach(t => t.classList.toggle('on', t.id === 'tab-' + id));
  const s = R.scenarios.find(x => x.id === id), root = $('#scn'); root.innerHTML = '';
  const feed = h('div', {class: 'feed'}), board = h('div', {class: 'board'}), info = h('div', {class: 'mut'});
  let i = 0;
  const msgs = s.steps.map((st, k) => {
    const a = st.agent, el = h('div', {class: `msg ${st.actor}`},
      h('div', {class: 'who'}, `${st.t} · ${st.actor}`, ...st.tags.map(t => h('span', {class: 'tag'}, t)),
        a ? h('span', {class: 'verdict ' + (a.correct ? 'ok' : 'bad')}, a.correct ? 'as expected' : (a.error ? 'agent error' : 'deviated')) : ''),
      h('div', {}, st.text));
    if (a) {
      const g = a.guarded;
      el.append(h('div', {class: 'mut', style: 'font-size:12px'}, `Reason: ${g.reason || ''}${g.question ? ' · Q: ' + g.question : ''}`));
      if (a.violations.length) el.append(h('div', {style: 'color:var(--warn);font-size:12px'}, 'Guard: ' + a.violations.join('; ')));
      if (a.ungrounded.length) el.append(h('div', {style: 'color:var(--warn);font-size:12px'}, 'Ungrounded evidence: ' + a.ungrounded.join(', ')));
      el.append(h('details', {}, h('summary', {}, `agent I/O · ${a.seconds}s · expected ${a.expect.actions.join('/')}${a.expect.selected ? ' sel∈' + (a.expect.selected.join(',') || 'null') : ''}`),
        h('pre', {}, 'INPUT\n' + JSON.stringify(a.payload, null, 1)), h('pre', {}, 'RAW AGENT OUTPUT\n' + JSON.stringify(a.raw ?? a.error, null, 1)), h('pre', {}, 'AFTER GUARDS\n' + JSON.stringify(g, null, 1))));
    }
    if (st.data) el.append(h('details', {}, h('summary', {}, 'data'), h('pre', {}, JSON.stringify(st.data, null, 1))));
    el.onclick = () => go(k); feed.append(el); return el;
  });
  function go(k) { i = Math.max(0, Math.min(k, msgs.length - 1)); msgs.forEach((m, n) => { m.classList.toggle('shown', n <= i); m.classList.toggle('cur', n === i) });
    msgs[i].scrollIntoView({block: 'nearest'}); drawBoard(board, s, i); info.textContent = `Step ${i + 1}/${msgs.length} · board at ${s.steps[i].t}`; }
  const ctrl = h('div', {class: 'ctrl'}, h('button', {onclick: () => go(0)}, '⏮'), h('button', {onclick: () => go(i - 1)}, '◀'),
    h('button', {onclick: () => { clearInterval(timer); timer = setInterval(() => { if (i >= msgs.length - 1) clearInterval(timer); else go(i + 1) }, 1100) }}, '▶ play'),
    h('button', {onclick: () => clearInterval(timer)}, '⏸'), h('button', {onclick: () => go(i + 1)}, '▶'), h('button', {onclick: () => go(msgs.length - 1)}, '⏭'));
  const m = s.metrics, cov = Object.entries(s.covers).map(([g, v]) => v.join(' ')).join(' · ');
  const left = h('div', {class: 'card'}, h('b', {}, `${s.id} · ${s.title}`), h('div', {class: 'why'}, s.why), h('div', {class: 'mut'}, 'Covers: ' + cov), h('br'), ctrl, feed);
  const legend = h('div', {class: 'legend'}, ...[['FREE', 'free'], ['BOOKED', 'confirmed'], ['PENDING', 'pending'], ['HELD', 'held'], ['REQUESTED', 'requested'], ['WIP', 'in progress'], ['OVERRUN', 'long-unload overrun'], ['BLOCKED', 'blocked/event']]
    .map(([c, l]) => h('span', {}, h('i', {class: c}), l)));
  const right = h('div', {class: 'card'}, h('b', {}, 'Jaipur DC dock board (FAC-JAI-01)'), legend, info, board,
    h('h2', {}, 'Outcome'), h('div', {}, s.outcome),
    h('table', {}, ...[['Agent calls (as expected)', `${m.agent_calls} (${m.agent_correct})`], ['Guard interventions', m.guard_interventions], ['Driver / agent turns', `${m.driver_turns} / ${m.agent_turns}`],
      ['Clarification turns', m.clarification_turns], ['Sim minutes to outcome', m.minutes_to_outcome], ['Manual takeover', m.manual_takeover ? 'yes' : 'no'],
      ['Stale options caught', m.rechecks_failed], ['Integrity', Object.entries(m.integrity).map(([k, v]) => `${k}=${v}`).join(', ')]]
      .map(([k, v]) => h('tr', {}, h('th', {}, k), h('td', {}, String(v))))));
  root.append(h('div', {class: 'scn'}, left, right)); go(0);
}
show(R.scenarios[0].id);
</script></body></html>
"""


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--offline", action="store_true", help="use the deterministic stand-in agent")
    parser.add_argument("--render", action="store_true", help="re-render HTML from the saved JSON")
    args = parser.parse_args()
    if args.render:
        report = json.loads(JSON_OUT.read_text(encoding="utf-8"))
    else:
        load_nvidia_env()
        if not args.offline and not os.environ.get("NVIDIA_API_KEY"):
            print("NVIDIA_API_KEY missing: add it to .env or use --offline", file=sys.stderr)
            return 2
        model = "offline-stand-in" if args.offline else os.environ.get("NVIDIA_MODEL_ID", "nvidia/nemotron-3-ultra-550b-a55b")
        report = run(offline_agent if args.offline else None, "offline" if args.offline else "live", model)
        JSON_OUT.write_text(json.dumps(report, indent=1, ensure_ascii=False), encoding="utf-8")
    HTML_OUT.write_text(render(report), encoding="utf-8")
    m = report["measures"]
    print(f"Workflow confidence {m['workflow_confidence']}/100 · agent accuracy {m['agent_decision_accuracy']:.0%} · "
          f"conflicts {m['conflicting_allocations']} · stale caught {m['options_infeasible_at_recheck']}")
    missing = [f"{g}:{k}" for g, items in report["coverage"].items() for k, v in items.items() if not v["scenarios"]]
    print("Coverage gaps: " + (", ".join(missing) if missing else "none"))
    print(f"Wrote {HTML_OUT.name} and {JSON_OUT.name}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
