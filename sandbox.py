"""Live multi-agent sandbox: simulated drivers and facility staff vs. SetuHaul agents.

Hidden ground truth (true arrival times, real dock failures, driver personas) drives
the simulation. The LLM role-plays drivers and facility staff from deterministic
briefs; our triage, dispatch, and assessment agents decide; deterministic code
validates and executes every state change against an in-memory copy of the seed DB.
Only synthetic operational fields (IDs, order references, times, dock codes) are sent
to the model -- no names, phone numbers, emails, or licence numbers.
"""

from __future__ import annotations

import argparse
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timezone
from html import escape
import json
import random
import re
import sqlite3
import sys
import time
from pathlib import Path
from statistics import mean
from typing import Any, Callable

from guards import (
    CoordinatorQueue, ReviewItem, feasible_option_ids, guard_answer, review_priority,
    to_minutes, ungrounded_evidence,
)
from nvidia_agents import (
    AgentRole, DEFAULT_MODEL, NVIDIAClientError, ask_agent, chat_text, load_nvidia_env,
)
from report_charts import CHART_CSS, ERROR, FAIL, PARTIAL, PASS, bar_chart, line_chart, stacked_chart
from workflow_simulation import WorkflowSimulation, apply_view_fixes


FACILITY_ID = "FAC-JAI-01"
DAY = "2026-08-04"
LAST_NEW_START = 21 * 60
GRACE_MIN = 30
EARLY_LIMIT_MIN = 60
DEDUPE_WINDOW_MIN = 30
MAX_DRIVER_CLARIFICATIONS = 2
ACTIVE_STATUSES = ("PENDING_CONFIRMATION", "CONFIRMED", "IN_PROGRESS")
PERSONAS = ("HONEST", "VAGUE", "OPTIMISTIC", "FALSE_ARRIVAL", "REPEATER")
PERSONA_DELAYS = {
    "HONEST": (0, 20, 40, 70),
    "VAGUE": (30, 50, 80),
    "OPTIMISTIC": (60, 90, 120),
    "FALSE_ARRIVAL": (40, 60, 90),
    "REPEATER": (20, 40, 60),
}
PERSONA_STYLE = {
    "HONEST": "clear and polite; gives exact facts",
    "VAGUE": "lazy and minimal; avoids specifics; informal typing",
    "OPTIMISTIC": "confident; downplays delays",
    "FALSE_ARRIVAL": "pushy; wants a dock immediately",
    "REPEATER": "impatient; very short messages",
}
DRIVER_SYSTEM = (
    "You role-play a truck driver in India texting a freight dispatch chatbot from your "
    "phone. Write ONE short informal SMS in English (under 30 words). Personality: {style}. "
    "Convey exactly the facts in the brief and nothing else: do not invent order numbers, "
    "times, dock names, or extra details. Output only the SMS text."
)
FACILITY_SYSTEM = (
    "You role-play the dock supervisor at Jaipur Distribution Centre replying by SMS to the "
    "operations chatbot. Write one or two short, accurate sentences stating only the facts "
    "in the brief. Output only the message text."
)
_CLOCK = re.compile(r"(?<!\d)(\d{1,2})[:.](\d{2})(?!\d)\s*(am|pm)?", re.IGNORECASE)
_DOCK = re.compile(r"\bD(?:OCK)?[-\s]?(?:JAI-D)?(\d)\b", re.IGNORECASE)


def iso(minutes: int) -> str:
    return f"{DAY}T{minutes // 60:02d}:{minutes % 60:02d}:00+05:30"


def clock(minutes: int) -> str:
    return f"{minutes // 60:02d}:{minutes % 60:02d}"


def parse_clock(text: str | None) -> int | None:
    if not text:
        return None
    match = _CLOCK.search(text)
    if not match:
        return None
    hours, minutes, suffix = int(match[1]), int(match[2]), (match[3] or "").lower()
    if suffix == "pm" and hours < 12:
        hours += 12
    elif suffix == "am" and hours == 12:
        hours = 0
    elif not suffix and hours < 7:
        hours += 12
    if hours > 23 or minutes > 59:
        return None
    return hours * 60 + minutes


def dock_code_of(text: str | None) -> str | None:
    matches = _DOCK.findall(text or "")
    return f"D{matches[-1]}" if matches else None


@dataclass
class Load:
    shipment_id: str
    order_reference: str
    driver_id: str
    priority: str
    required_dock_type: str
    temperature_control_required: bool
    load_weight_kg: int
    expected_unload_min: int
    vehicle_capacity_kg: int
    base_eta: int
    true_arrival: int
    persona: str
    new_order: bool
    planning_eta: int = 0
    claim: int | None = None
    last_claim: int | None = None
    arrived_at: int | None = None


@dataclass
class Message:
    sender: str
    sender_id: str
    text: str
    kind: str
    shipment_id: str | None = None
    claim: int | None = None
    request: dict[str, Any] | None = None
    truth: bool | None = None


@dataclass
class Event:
    at: int
    seq: int
    kind: str
    data: dict[str, Any] = field(default_factory=dict)


class Sandbox:
    def __init__(
        self,
        *,
        seed: int = 7,
        start: str = "09:30",
        tick_minutes: int = 10,
        wall_seconds: float = 900,
        pace: bool = True,
        max_ticks: int | None = None,
        decide: Callable[[AgentRole, dict[str, Any]], dict[str, Any]] | None = None,
        speak: Callable[[str, str], str] | None = None,
        echo: Callable[[str], None] = print,
    ) -> None:
        self.sim = WorkflowSimulation()
        self.db = self.sim.db
        self.db.isolation_level = None
        apply_view_fixes(self.db)
        self.rng = random.Random(seed)
        self.seed = seed
        self.start = to_minutes(start)
        self.now = self.start
        self.tick_minutes = tick_minutes
        self.wall_seconds = wall_seconds
        self.pace = pace
        self.max_ticks = max_ticks
        self.decide = decide or ask_agent
        self.speak = speak or chat_text
        self.echo = echo
        self.queue = CoordinatorQueue()
        self.events: list[Event] = []
        self.loads: dict[str, Load] = {}
        self.personas: dict[str, str] = {}
        self.threads: dict[str, list[dict[str, str]]] = defaultdict(list)
        self.last_text: dict[str, str] = {}
        self.recent: dict[str, list[tuple[int, str]]] = defaultdict(list)
        self.clarifications: Counter[str] = Counter()
        self.pending_facility: set[str] = set()
        self.truth_outages: dict[str, tuple[int, int]] = {}
        self.applied_outages: dict[str, tuple[int, int]] = {}
        self.claim_errors: dict[str, list[int]] = defaultdict(list)
        self.judgments: list[dict[str, Any]] = []
        self.transcript: list[dict[str, Any]] = []
        self.llm_calls: list[dict[str, Any]] = []
        self.arrivals: list[dict[str, Any]] = []
        self.bookings: list[dict[str, Any]] = []
        self.queue_series: list[tuple[str, float]] = []
        self.metrics: Counter[str] = Counter()
        self.stop_reason = "not started"
        self.wall_elapsed = 0.0
        self.deadline = float("inf")
        self._seq = 0
        self._ids: Counter[str] = Counter()
        self.docks = {
            row["dock_code"]: dict(row)
            for row in self.db.execute(
                "SELECT dock_id, dock_code, dock_type, supports_refrigerated, "
                "max_vehicle_weight_kg, dock_status FROM docks WHERE facility_id = ?",
                (FACILITY_ID,),
            )
        }
        self._setup()

    # ----------------------------------------------------------------- setup
    def _schedule(self, at: int, kind: str, **data: Any) -> None:
        self._seq += 1
        self.events.append(Event(at, self._seq, kind, data))

    def _next_id(self, prefix: str) -> str:
        self._ids[prefix] += 1
        return f"SBX-{prefix}-{self._ids[prefix]:03d}"

    def _round_tick(self, minutes: int) -> int:
        offset = (minutes - self.start) % self.tick_minutes
        return minutes - offset + (self.tick_minutes if offset else 0)

    def _setup(self) -> None:
        rows = self.db.execute(
            """
            SELECT s.shipment_id, s.order_reference, s.driver_id, s.priority_code,
                   s.required_dock_type, s.temperature_control_required, s.load_weight_kg,
                   s.expected_unload_min, v.capacity_kg, le.effective_eta_ts,
                   a.appointment_id
            FROM shipments s
            JOIN vehicles v ON v.vehicle_id = s.vehicle_id
            JOIN v_latest_eta le ON le.shipment_id = s.shipment_id
            LEFT JOIN appointments a ON a.shipment_id = s.shipment_id AND a.is_current = 1
                 AND a.appointment_status IN ('PENDING_CONFIRMATION','CONFIRMED','IN_PROGRESS')
            WHERE s.destination_facility_id = ?
              AND s.current_status IN ('IN_TRANSIT', 'ASSIGNED')
              AND (a.appointment_id IS NOT NULL OR s.current_status = 'IN_TRANSIT')
            ORDER BY s.shipment_id
            """,
            (FACILITY_ID,),
        ).fetchall()
        drivers = sorted({row["driver_id"] for row in rows})
        personas = [PERSONAS[index % len(PERSONAS)] for index in range(len(drivers))]
        self.rng.shuffle(personas)
        self.personas = dict(zip(drivers, personas))
        multi = [d for d in drivers if len(self._in_transit_assignments(d)) > 1]
        if multi and self.personas[multi[0]] != "VAGUE":
            swap = next(d for d in drivers if self.personas[d] == "VAGUE")
            self.personas[swap], self.personas[multi[0]] = self.personas[multi[0]], "VAGUE"

        latest_arrival = LAST_NEW_START - 30
        for row in rows:
            persona = self.personas[row["driver_id"]]
            base = max(to_minutes(row["effective_eta_ts"]), self.start + 20)
            delay = self.rng.choice(PERSONA_DELAYS[persona])
            load = Load(
                row["shipment_id"], row["order_reference"], row["driver_id"],
                row["priority_code"], row["required_dock_type"],
                bool(row["temperature_control_required"]), row["load_weight_kg"],
                row["expected_unload_min"], row["capacity_kg"], base,
                min(base + delay, latest_arrival), persona,
                new_order=row["appointment_id"] is None, planning_eta=base,
            )
            if persona in ("HONEST", "REPEATER"):
                load.claim = load.true_arrival
            elif persona == "OPTIMISTIC":
                load.claim = max(base, load.true_arrival - self.rng.choice((40, 50, 60)))
            self.loads[load.shipment_id] = load

        for index, load in enumerate(l for l in self.loads.values() if l.new_order):
            self._schedule(self.start + 10 * (index + 1), "NEW_ORDER", shipment_id=load.shipment_id)

        for load in self.loads.values():
            if load.persona == "HONEST" and load.true_arrival == load.base_eta:
                continue
            lead = 70 if load.persona == "FALSE_ARRIVAL" else self.rng.choice((60, 80, 100))
            report_at = self._round_tick(max(self.start + 10, load.true_arrival - lead))
            if report_at >= load.true_arrival:
                continue
            claim = "AT_GATE" if load.persona == "FALSE_ARRIVAL" else (
                "VAGUE" if load.persona == "VAGUE" else "ETA"
            )
            self._schedule(report_at, "DRIVER_REPORT", shipment_id=load.shipment_id, claim=claim)
            if load.persona == "REPEATER":
                self._schedule(report_at + 10, "DRIVER_RESEND", driver_id=load.driver_id)
                self._schedule(report_at + 20, "DRIVER_RESEND", driver_id=load.driver_id)
            if load.persona in ("REPEATER", "VAGUE"):
                self._schedule(report_at + 40, "STATUS_INQUIRY", shipment_id=load.shipment_id)

        outage_start, outage_end = self.start + 40, self.start + 210
        impact = Counter(
            row["dock_code"]
            for row in self.db.execute(
                """
                SELECT d.dock_code FROM appointments a
                JOIN appointment_slots sl ON sl.slot_id = a.slot_id
                JOIN docks d ON d.dock_id = sl.dock_id
                WHERE d.facility_id = ? AND d.dock_type = 'STANDARD'
                  AND a.is_current = 1 AND a.appointment_status IN ('CONFIRMED','PENDING_CONFIRMATION')
                  AND sl.slot_start_ts >= ?
                """,
                (FACILITY_ID, iso(outage_start)),
            )
        )
        outage_dock = sorted(impact.items(), key=lambda item: (-item[1], item[0]))[0][0]
        self.truth_outages[outage_dock] = (outage_start, outage_end)
        self._schedule(outage_start, "FACILITY_OUTAGE", dock_code=outage_dock)

        false_dock = next(
            code for code in ("D2", "D1", "D3", "D4")
            if code != outage_dock and self.docks[code]["dock_type"] == "STANDARD"
        )
        claimant = next(
            (l.driver_id for l in self.loads.values() if l.persona in ("OPTIMISTIC", "FALSE_ARRIVAL")),
            next(iter(self.loads.values())).driver_id,
        )
        self._schedule(self.start + 70, "FALSE_OUTAGE_CLAIM", driver_id=claimant, dock_code=false_dock)

    # ------------------------------------------------------------ DB helpers
    def _in_transit_assignments(self, driver_id: str) -> list[dict[str, Any]]:
        return [a for a in self.driver_assignments(driver_id) if a["status"] == "IN_TRANSIT"]

    def driver_assignments(self, driver_id: str) -> list[dict[str, Any]]:
        assignments = []
        for row in self.db.execute(
            """
            SELECT s.shipment_id, s.order_reference, s.current_status, s.destination_facility_id
            FROM shipments s
            WHERE s.driver_id = ? AND s.current_status NOT IN ('COMPLETED', 'CANCELLED')
            ORDER BY s.shipment_id
            """,
            (driver_id,),
        ):
            load = self.loads.get(row["shipment_id"])
            at_facility = row["current_status"] in ("WAITING", "IN_DOCK", "AT_GATE") or (
                load is not None and load.arrived_at is not None
            )
            appointment = self.current_appointment(row["shipment_id"])
            assignments.append({
                "shipment_id": row["shipment_id"],
                "order_reference": row["order_reference"],
                "destination": row["destination_facility_id"],
                "status": "AT_FACILITY" if at_facility else "IN_TRANSIT",
                "appointment": None if appointment is None else {
                    "dock": appointment["dock_code"],
                    "start": clock(to_minutes(appointment["slot_start_ts"])),
                    "end": clock(to_minutes(appointment["slot_end_ts"])),
                    "status": appointment["appointment_status"],
                },
            })
        return assignments

    def current_appointment(self, shipment_id: str) -> sqlite3.Row | None:
        return self.db.execute(
            """
            SELECT a.appointment_id, a.appointment_status, sl.slot_id, sl.slot_start_ts,
                   sl.slot_end_ts, d.dock_code
            FROM appointments a
            JOIN appointment_slots sl ON sl.slot_id = a.slot_id
            JOIN docks d ON d.dock_id = sl.dock_id
            WHERE a.shipment_id = ? AND a.is_current = 1
              AND a.appointment_status IN ('PENDING_CONFIRMATION','CONFIRMED','IN_PROGRESS')
            """,
            (shipment_id,),
        ).fetchone()

    def resolve_shipment(self, driver_id: str, reference: str | None) -> str | None:
        assignments = self.driver_assignments(driver_id)
        if reference:
            needle = reference.upper()
            for item in assignments:
                if item["shipment_id"] in needle or item["order_reference"] in needle or needle in (
                    item["shipment_id"], item["order_reference"]
                ):
                    return item["shipment_id"]
            return None
        in_transit = [item for item in assignments if item["status"] == "IN_TRANSIT"]
        return in_transit[0]["shipment_id"] if len(in_transit) == 1 else None

    def _signals(self, driver_id: str | None) -> dict[str, Any]:
        gate = {
            load.shipment_id: clock(load.arrived_at)
            for load in self.loads.values()
            if load.arrived_at is not None and (driver_id is None or load.driver_id == driver_id)
        }
        return {
            "live_gps": None,
            "gate_checkins": gate,
            "verified_dock_events": [
                {"dock": dock, "from": clock(start), "until": clock(end)}
                for dock, (start, end) in self.applied_outages.items()
            ],
        }

    # ------------------------------------------------------------ recording
    def _log(self, actor: str, text: str, **details: Any) -> None:
        entry = {"time": clock(self.now), "actor": actor, "text": text, **details}
        self.transcript.append(entry)
        self.echo(f"[{entry['time']}] {actor}: {text}")

    def _judge(self, role: str, kind: str, correct: bool, expected: str) -> None:
        self.judgments.append({
            "time": clock(self.now), "role": role, "kind": kind,
            "correct": correct, "expected": expected,
        })

    def _speak(self, actor: str, system: str, brief: dict[str, Any], fallback: str,
               valid: Callable[[str], bool]) -> str:
        started = time.monotonic()
        try:
            text = self.speak(system, json.dumps(brief))
        except NVIDIAClientError as exc:
            text = ""
            self.metrics["persona_errors"] += 1
            self.echo(f"  ({actor} role-play error: {exc})")
        self.llm_calls.append({
            "role": actor, "seconds": time.monotonic() - started, "ok": bool(text),
        })
        text = (text or "").strip().strip('"').strip()[:400]
        if not text or not valid(text):
            self.metrics["persona_fallbacks"] += 1
            return fallback
        return text

    def _call(
        self,
        role: AgentRole,
        scenario: dict[str, Any],
        allowed_ids: list[str] | None,
        kind: str,
        expected: str,
        check: Callable[[dict[str, Any]], bool],
    ) -> dict[str, Any] | None:
        started = time.monotonic()
        try:
            raw = self.decide(role, scenario)
            error = None
        except NVIDIAClientError as exc:
            raw, error = None, str(exc)
        self.llm_calls.append({
            "role": role.value, "seconds": time.monotonic() - started, "ok": raw is not None,
        })
        if raw is None:
            self._judge(role.value, kind, False, expected)
            self._log(role.value.upper(), f"ERROR: {error}", kind=kind, error=error)
            return None
        guarded, violations = guard_answer(raw, allowed_ids, scenario)
        ungrounded = ungrounded_evidence(raw, scenario)
        self.metrics["guard_interventions"] += len(violations)
        self.metrics["answers_with_ungrounded_evidence"] += bool(ungrounded)
        correct = bool(check(raw)) and not violations
        self._judge(role.value, kind, correct, expected)
        summary = (
            f"{raw['action']} / {raw['verification']} / ask {raw['ask_whom']}"
            + (f" / selected {raw['selected_id']}" if raw.get("selected_id") else "")
        )
        self._log(
            role.value.upper(), summary, kind=kind, correct=correct, expected=expected,
            raw=raw, violations=violations, ungrounded=ungrounded,
        )
        return guarded

    # ------------------------------------------------------------ main loop
    def run(self) -> None:
        started = time.monotonic()
        self.deadline = started + self.wall_seconds
        total_ticks = max((LAST_NEW_START - self.start) // self.tick_minutes, 1)
        ticks = 0
        self.stop_reason = "end of operating day (21:00)"
        while self.now <= LAST_NEW_START:
            if time.monotonic() >= self.deadline:
                self.stop_reason = f"wall-clock limit ({self.wall_seconds / 60:.0f} min)"
                break
            if self.max_ticks is not None and ticks >= self.max_ticks:
                self.stop_reason = f"tick limit ({self.max_ticks})"
                break
            self._tick()
            ticks += 1
            self.now += self.tick_minutes
            if self.pace:
                target = started + ticks * self.wall_seconds / total_ticks
                pause = min(target, self.deadline) - time.monotonic()
                if pause > 0:
                    time.sleep(pause)
        self.wall_elapsed = time.monotonic() - started
        self.sim_end = self.now

    def _tick(self) -> None:
        self.queue_series.append((clock(self.now), float(len(self.queue.pending))))
        for load in sorted(self.loads.values(), key=lambda l: l.true_arrival):
            if load.arrived_at is None and load.true_arrival <= self.now:
                self._arrive(load)
        for item in self.queue.advance(self.now):
            self._resolve(item)
        due = sorted((e for e in self.events if e.at <= self.now), key=lambda e: (e.at, e.seq))
        for event in due:
            if time.monotonic() >= self.deadline:
                break
            self.events.remove(event)
            getattr(self, f"_on_{event.kind.lower()}")(**event.data)

    # ---------------------------------------------------------------- events
    def _on_new_order(self, shipment_id: str) -> None:
        load = self.loads[shipment_id]
        options = [
            {
                "id": code,
                "dock_type": dock["dock_type"],
                "supports_refrigerated": bool(dock["supports_refrigerated"]),
                "dock_max_weight_kg": dock["max_vehicle_weight_kg"],
                "dock_available": dock["dock_status"] == "ACTIVE" and code not in self.applied_outages,
                "vehicle_capacity_kg": load.vehicle_capacity_kg,
            }
            for code, dock in sorted(self.docks.items())
        ]
        scenario = {
            "now": clock(self.now),
            "new_order": {"shipment_id": shipment_id, "order_reference": load.order_reference,
                          "priority": load.priority, "eta": clock(load.planning_eta)},
            "load": {"weight_kg": load.load_weight_kg, "required_dock_type": load.required_dock_type,
                     "temperature_control_required": load.temperature_control_required},
            "options": options,
        }
        allowed = feasible_option_ids(scenario)
        scenario["deterministic_feasibility"] = {
            "feasible_ids": allowed,
            "note": "Only these dock IDs passed capacity, weight, type, refrigeration and availability checks.",
        }
        self._log("SYSTEM", f"New order {load.order_reference} ({shipment_id}) needs a dock")
        guarded = self._call(
            AgentRole.ASSESSMENT, scenario, allowed, "NEW_ORDER_ASSESSMENT",
            f"select one of {allowed}" if allowed else "NO_FEASIBLE_OPTION",
            lambda a: (a["selected_id"] in allowed) if allowed else
            (a["selected_id"] is None and a["action"] == "NO_FEASIBLE_OPTION"),
        )
        if guarded and guarded["selected_id"]:
            self._reslot(load, load.planning_eta, "new order", dock_code=guarded["selected_id"])
        else:
            self._escalate("NO_FEASIBLE_SLOT", shipment_id, {"reason": "new order without dock"})

    def _on_driver_report(self, shipment_id: str, claim: str) -> None:
        load = self.loads[shipment_id]
        multiple = len(self._in_transit_assignments(load.driver_id)) > 1
        include_ref = not (load.persona == "VAGUE" and multiple)
        ref = load.order_reference
        if claim == "AT_GATE":
            brief = {"say": "Claim you are already at the gate and want a dock now.",
                     "mention_order_reference": ref}
            fallback = f"I am at gate already for {ref}, give dock fast"
            kind, valid = "FALSE_ARRIVAL", (lambda t: ref in t)
        elif claim == "VAGUE":
            brief = {"say": "Say you are running late because of traffic but give NO time.",
                     "mention_order_reference": ref if include_ref else None}
            fallback = "running late bhai traffic, will reach soon" + (f" {ref}" if include_ref else "")
            kind = "VAGUE_ETA" if include_ref or not multiple else "AMBIGUOUS"
            valid = lambda t: parse_clock(t) is None and ((ref in t) == include_ref)
        else:
            brief = {"say": f"Say you are running late and will reach around {clock(load.claim)}.",
                     "mention_order_reference": ref}
            fallback = f"Running late for {ref}, will reach around {clock(load.claim)}"
            kind = "CLEAR_ETA"
            valid = lambda t: parse_clock(t) == load.claim and ref in t
        text = self._speak(
            "driver_sim", DRIVER_SYSTEM.format(style=PERSONA_STYLE[load.persona]), brief, fallback, valid,
        )
        self._handle_driver(Message("DRIVER", load.driver_id, text, kind, shipment_id, claim=load.claim))

    def _on_driver_resend(self, driver_id: str) -> None:
        text = self.last_text.get(driver_id)
        if text:
            self._handle_driver(Message("DRIVER", driver_id, text, "RESEND"))

    def _on_status_inquiry(self, shipment_id: str) -> None:
        load = self.loads[shipment_id]
        if load.arrived_at is not None:
            return
        ref = load.order_reference
        text = self._speak(
            "driver_sim", DRIVER_SYSTEM.format(style=PERSONA_STYLE[load.persona]),
            {"say": "Ask whether your dock slot is confirmed.", "mention_order_reference": ref},
            f"is my slot confirmed? {ref}", lambda t: ref in t,
        )
        self._handle_driver(Message("DRIVER", load.driver_id, text, "STATUS_QUESTION", shipment_id))

    def _on_false_outage_claim(self, driver_id: str, dock_code: str) -> None:
        text = self._speak(
            "driver_sim", DRIVER_SYSTEM.format(style=PERSONA_STYLE[self.personas[driver_id]]),
            {"say": f"Claim dock {dock_code} looks broken and trucks are stuck there."},
            f"dock {dock_code} is broken, trucks stuck", lambda t: dock_code_of(t) == dock_code,
        )
        self._handle_driver(Message("DRIVER", driver_id, text, "FALSE_OUTAGE_CLAIM", truth=False,
                                    request={"dock_code": dock_code}))

    def _on_facility_outage(self, dock_code: str) -> None:
        start, end = self.truth_outages[dock_code]
        text = self._speak(
            "facility_sim", FACILITY_SYSTEM,
            {"report": f"Dock {dock_code} hydraulic leveller failed at {clock(start)}; "
                       f"it is out of service until about {clock(end)}."},
            f"Dock {dock_code} leveller failed at {clock(start)}, out of service till {clock(end)}.",
            lambda t: dock_code_of(t) == dock_code,
        )
        self._handle_facility(Message("FACILITY", FACILITY_ID, text, "FACILITY_OUTAGE_REPORT",
                                      truth=True, request=None,
                                      claim=None) , dock_truth=dock_code)

    def _on_driver_reply(self, driver_id: str, shipment_id: str | None, question: str) -> None:
        load = self.loads.get(shipment_id) if shipment_id else None
        if load is None or load.arrived_at is not None:
            return
        if load.persona == "OPTIMISTIC" and load.claim is not None:
            answer = load.claim
        elif load.persona == "FALSE_ARRIVAL":
            answer = max(self.now + 10, load.true_arrival - 20)
        else:
            answer = max(self.now + 10, load.true_arrival)
            if load.persona == "VAGUE":
                answer = self._round_tick(answer)
        load.claim = answer
        ref = load.order_reference
        text = self._speak(
            "driver_sim", DRIVER_SYSTEM.format(style=PERSONA_STYLE[load.persona]),
            {"dispatch_asked": question,
             "say": f"Your order is {ref}; you will reach around {clock(answer)}."},
            f"{ref}, reaching around {clock(answer)}",
            lambda t: parse_clock(t) == answer and ref in t,
        )
        self._handle_driver(Message("DRIVER", driver_id, text, "CLARIFICATION_REPLY", shipment_id, claim=answer))

    def _on_facility_reply(self, request: dict[str, Any], question: str) -> None:
        self.pending_facility.discard(request["key"])
        if request["claim_type"] == "DOCK_OUTAGE":
            window = self.truth_outages.get(request["dock_code"])
            truth = window is not None and window[0] <= self.now < window[1]
            facts = (f"Dock {request['dock_code']} is broken" if truth
                     else f"Dock {request['dock_code']} is operating normally; nothing is broken")
            fallback = facts + "."
        else:
            load = self.loads.get(request["shipment_id"])
            truth = load is not None and load.arrived_at is not None
            ref = load.order_reference if load else request["shipment_id"]
            facts = (f"Order {ref} checked in at the gate" if truth
                     else f"There is no gate check-in for order {ref}; that truck is not at the gate")
            fallback = facts + "."
        text = self._speak("facility_sim", FACILITY_SYSTEM,
                           {"question_from_ops_bot": question, "facts": facts}, fallback,
                           lambda t: bool(t))
        self._handle_facility(Message("FACILITY", FACILITY_ID, text, "FACILITY_REPLY",
                                      request=request, truth=truth))

    # -------------------------------------------------------------- handlers
    def _expectation(self, message: Message) -> tuple[str, Callable[[dict[str, Any]], bool]]:
        def extracted(a: dict[str, Any]) -> dict[str, Any]:
            return a.get("extracted") or {}

        if message.kind in ("CLEAR_ETA", "CLARIFICATION_REPLY"):
            def check(a: dict[str, Any]) -> bool:
                claimed = parse_clock(extracted(a).get("claimed_eta"))
                return (
                    a["verification"] == "UNVERIFIED"
                    and a["action"] in ("PROPOSE", "NO_ACTION")
                    and claimed is not None and abs(claimed - (message.claim or -999)) <= 5
                    and self.resolve_shipment(message.sender_id, extracted(a).get("shipment_id"))
                    == message.shipment_id
                )
            return f"PROPOSE/UNVERIFIED with ETA {clock(message.claim or 0)} for {message.shipment_id}", check
        if message.kind in ("VAGUE_ETA", "AMBIGUOUS"):
            return "CLARIFY with DRIVER", lambda a: a["action"] == "CLARIFY" and a["ask_whom"] == "DRIVER"
        if message.kind in ("FALSE_ARRIVAL", "FALSE_OUTAGE_CLAIM"):
            return "UNVERIFIED; ask FACILITY (or COORDINATOR) to check", lambda a: (
                a["verification"] == "UNVERIFIED" and a["ask_whom"] in ("FACILITY", "COORDINATOR")
            )
        if message.kind == "STATUS_QUESTION":
            return "answer the status question without escalation", lambda a: (
                a["action"] in ("NO_ACTION", "DUPLICATE") and a["verification"] != "VERIFIED"
            )
        if message.kind == "FACILITY_REPLY":
            return f"confirms_claim={message.truth}", lambda a: extracted(a).get("confirms_claim") is message.truth
        return "not a duplicate-suppressed resend", lambda a: a["action"] == "DUPLICATE"

    def _handle_driver(self, message: Message) -> None:
        driver = message.sender_id
        normalized = re.sub(r"\W+", " ", message.text.lower()).strip()
        recent = [(t, n) for t, n in self.recent[driver] if self.now - t <= DEDUPE_WINDOW_MIN]
        self.recent[driver] = recent + [(self.now, normalized)]
        self._log(f"DRIVER {driver} ({self.personas.get(driver, '?')})", message.text, kind=message.kind)
        if any(n == normalized for _, n in recent):
            self.metrics["duplicates_suppressed"] += 1
            self._log("SYSTEM", "Duplicate message suppressed before any agent call or alert")
            return
        self.last_text[driver] = message.text
        self.threads[driver].append({"from": "DRIVER", "text": message.text})
        scenario = {
            "now": clock(self.now),
            "sender": {"type": "DRIVER", "driver_ref": driver},
            "message": message.text,
            "thread": self.threads[driver][-5:],
            "driver_assignments": self.driver_assignments(driver),
            "independent_signals": self._signals(driver),
        }
        expected, check = self._expectation(message)
        guarded = self._call(AgentRole.TRIAGE, scenario, None, message.kind, expected, check)
        if guarded is None:
            self._escalate("OTHER", message.shipment_id, {"driver_id": driver, "text": message.text})
            return
        self._act_on_driver(message, guarded)

    def _act_on_driver(self, message: Message, answer: dict[str, Any]) -> None:
        driver = message.sender_id
        ex = answer.get("extracted") or {}
        report_type = ex.get("report_type")
        shipment = self.resolve_shipment(driver, ex.get("shipment_id"))
        if answer["verification"] == "VERIFIED":
            self.metrics["driver_sourced_verification_rejected"] += 1
            self._log("GUARD", "Driver-sourced VERIFIED ignored; only facility/gate evidence verifies")
        action, whom = answer["action"], answer["ask_whom"]
        if action == "DUPLICATE":
            self.metrics["duplicates_flagged_by_agent"] += 1
            return
        if whom == "FACILITY":
            self._ask_facility(driver, report_type, ex, shipment, message, answer.get("question"))
            return
        if action == "ESCALATE" or whom == "COORDINATOR":
            kind = report_type if report_type in ("DOCK_OUTAGE", "ARRIVAL_CLAIM", "ETA_UPDATE") else "OTHER"
            self._escalate(kind, shipment or message.shipment_id,
                           {"driver_id": driver, "dock_code": dock_code_of(ex.get("dock_code") or message.text)})
            return
        if action == "CLARIFY":
            self._ask_driver(driver, shipment or message.shipment_id, answer.get("question"))
            return
        if report_type == "QUESTION" or (action == "NO_ACTION" and report_type != "ETA_UPDATE"):
            self._answer_status(driver, shipment)
            return
        claimed = parse_clock(ex.get("claimed_eta"))
        if shipment is None or claimed is None:
            self.metrics["fallback_clarifications"] += 1
            self._ask_driver(driver, message.shipment_id, "Which order is this about, and what time will you reach the gate?")
            return
        self._plan_claim(shipment, claimed)

    def _ask_driver(self, driver: str, shipment_id: str | None, question: str | None) -> None:
        if self.clarifications[driver] >= MAX_DRIVER_CLARIFICATIONS:
            self._escalate("ETA_UPDATE", shipment_id, {"driver_id": driver, "why": "clarifications exhausted"})
            return
        self.clarifications[driver] += 1
        question = question or "Which order is this about, and what time will you reach the gate?"
        self.threads[driver].append({"from": "DISPATCH_BOT", "text": question})
        self.metrics["driver_questions"] += 1
        self._log(f"BOT -> {driver}", question)
        self._schedule(self.now + self.tick_minutes, "DRIVER_REPLY",
                       driver_id=driver, shipment_id=shipment_id, question=question)

    def _ask_facility(self, driver: str, report_type: str | None, ex: dict[str, Any],
                      shipment: str | None, message: Message, question: str | None) -> None:
        dock = dock_code_of(ex.get("dock_code")) or dock_code_of(message.text)
        is_outage = report_type == "DOCK_OUTAGE" or (dock and message.kind == "FALSE_OUTAGE_CLAIM")
        if not is_outage and report_type != "ARRIVAL_CLAIM":
            # The facility can confirm dock status or a gate check-in, not an in-transit
            # ETA or a booking status; handle those deterministically instead.
            self.metrics["facility_requests_not_needed"] += 1
            self._log("GUARD", "Facility cannot verify an in-transit ETA or slot status; "
                               "handled without a facility request")
            claimed = parse_clock(ex.get("claimed_eta"))
            if report_type == "QUESTION" or message.kind == "STATUS_QUESTION":
                self._answer_status(driver, shipment or message.shipment_id)
            elif shipment is not None and claimed is not None:
                self._plan_claim(shipment, claimed)
            else:
                self._ask_driver(driver, shipment or message.shipment_id, question)
            return
        if is_outage:
            if dock is None:
                self._ask_driver(driver, None, "Which dock number do you mean?")
                return
            request = {"claim_type": "DOCK_OUTAGE", "dock_code": dock, "driver_id": driver,
                       "key": f"DOCK_OUTAGE:{dock}"}
            question = question or f"Is dock {dock} out of service right now?"
        else:
            target = shipment or message.shipment_id
            if target is None:
                self._ask_driver(driver, None, "Which order are you carrying?")
                return
            request = {"claim_type": "ARRIVAL_CLAIM", "shipment_id": target, "driver_id": driver,
                       "key": f"ARRIVAL_CLAIM:{target}"}
            question = question or f"Is the truck for {target} at the gate now?"
        if request["key"] in self.pending_facility:
            self.metrics["facility_requests_merged"] += 1
            return
        self.pending_facility.add(request["key"])
        self.metrics["facility_verifications_requested"] += 1
        self._log("BOT -> FACILITY", question)
        self._schedule(self.now + self.tick_minutes, "FACILITY_REPLY", request=request, question=question)

    def _answer_status(self, driver: str, shipment_id: str | None) -> None:
        appointment = self.current_appointment(shipment_id) if shipment_id else None
        if appointment is None:
            reply = "I can't see a current dock slot for that order yet; operations is reviewing it."
        else:
            reply = (f"Your slot: dock {appointment['dock_code']} "
                     f"{clock(to_minutes(appointment['slot_start_ts']))}-"
                     f"{clock(to_minutes(appointment['slot_end_ts']))} "
                     f"({appointment['appointment_status'].replace('_', ' ').lower()}).")
        self.metrics["status_answers"] += 1
        self._log(f"BOT -> {driver}", reply)

    def _handle_facility(self, message: Message, dock_truth: str | None = None) -> None:
        self._log("FACILITY", message.text, kind=message.kind)
        scenario = {
            "now": clock(self.now),
            "sender": {"type": "FACILITY", "facility_id": FACILITY_ID, "role": "dock supervisor"},
            "message": message.text,
            "verification_request": None if message.request is None else {
                key: value for key, value in message.request.items() if key in ("claim_type", "dock_code", "shipment_id")
            },
            "independent_signals": self._signals(None),
        }
        if message.request is None:
            expected = f"VERIFIED DOCK_OUTAGE for {dock_truth}"
            check = lambda a: (
                a["verification"] == "VERIFIED"
                and (a.get("extracted") or {}).get("report_type") == "DOCK_OUTAGE"
                and dock_code_of((a.get("extracted") or {}).get("dock_code")) == dock_truth
            )
        else:
            expected, check = self._expectation(message)
        answer = self._call(AgentRole.TRIAGE, scenario, None, message.kind, expected, check)
        ex = (answer or {}).get("extracted") or {}
        if message.request is None:
            dock = dock_code_of(ex.get("dock_code"))
            if answer and answer["verification"] == "VERIFIED" and ex.get("report_type") == "DOCK_OUTAGE" and dock:
                self._apply_outage(dock)
            else:
                self._escalate("DOCK_OUTAGE", None, {"dock_code": dock_truth})
            return
        request, confirms = message.request, ex.get("confirms_claim")
        if confirms is None:
            self._escalate(request["claim_type"], request.get("shipment_id"), dict(request))
        elif request["claim_type"] == "DOCK_OUTAGE":
            if confirms:
                self._apply_outage(request["dock_code"])
            else:
                self.metrics["false_outage_claims_rejected"] += 1
                self._log(f"BOT -> {request['driver_id']}",
                          f"Facility reports dock {request['dock_code']} is operating normally; no change made.")
        elif confirms:
            self.metrics["arrival_claims_confirmed"] += 1
        else:
            self.metrics["false_arrival_claims_detected"] += 1
            self._ask_driver(request["driver_id"], request["shipment_id"],
                             "The gate has no check-in for your truck yet. What time will you actually reach the gate?")

    # ----------------------------------------------------- planning/booking
    def _plan_claim(self, shipment_id: str, claimed: int) -> None:
        load = self.loads.get(shipment_id)
        if load is None or load.arrived_at is not None:
            self._answer_status(load.driver_id if load else "?", shipment_id)
            return
        load.last_claim = claimed
        self.db.execute(
            """
            INSERT INTO eta_updates (eta_update_id, shipment_id, source_type, reported_by_driver_id,
                                     declared_eta_ts, confidence_code, note, created_at)
            VALUES (?, ?, 'DRIVER_DECLARED', ?, ?, 'MEDIUM', 'Sandbox driver claim (unverified)', ?)
            """,
            (self._next_id("ETA"), shipment_id, load.driver_id, iso(claimed), iso(self.now)),
        )
        buffer = self.reliability_buffer(load.driver_id)
        load.planning_eta = max(claimed + buffer, self.now)
        appointment = self.current_appointment(shipment_id)
        if appointment is not None and not self._slot_blocked(appointment):
            start = to_minutes(appointment["slot_start_ts"])
            if start - EARLY_LIMIT_MIN <= load.planning_eta <= start + GRACE_MIN:
                self.metrics["claims_fit_current_slot"] += 1
                self._log("SYSTEM", f"{shipment_id}: claimed ETA {clock(claimed)} still fits the current slot")
                return
        note = f" (+{buffer} min reliability buffer)" if buffer else ""
        self._reslot(load, load.planning_eta, f"driver-declared ETA {clock(claimed)}, unverified{note}")

    def reliability_buffer(self, driver_id: str) -> int:
        """Non-punitive planning buffer from this driver's observed claim error today."""
        errors = self.claim_errors.get(driver_id)
        return min(60, max(0, round(mean(errors)))) if errors else 0

    def _slot_blocked(self, appointment: sqlite3.Row) -> bool:
        window = self.applied_outages.get(appointment["dock_code"])
        return window is not None and window[0] < to_minutes(appointment["slot_end_ts"]) \
            and window[1] > to_minutes(appointment["slot_start_ts"])

    def _reslot(self, load: Load, planning: int, reason: str, dock_code: str | None = None) -> None:
        eta = max(planning, self.now)
        report = self.sim.report_driver_eta(load.shipment_id, iso(eta), "MEDIUM")
        candidates = [
            c for c in self.sim.provisional_slots(report.report_id)
            if to_minutes(c["slot_start_ts"]) < LAST_NEW_START
        ]
        if dock_code:
            candidates = [c for c in candidates if c["dock_code"] == dock_code] or candidates
        candidates = candidates[:6]
        allowed = [c["slot_id"] for c in candidates]
        scenario = {
            "now": clock(self.now),
            "reason": reason,
            "shipment": {
                "id": load.shipment_id, "priority": load.priority,
                "required_dock_type": load.required_dock_type,
                "temperature_control_required": load.temperature_control_required,
                "load_weight_kg": load.load_weight_kg,
                "expected_unload_min": load.expected_unload_min,
                "planning_eta": clock(eta),
            },
            "slots": [
                {"id": c["slot_id"], "dock": c["dock_code"],
                 "dock_type": self.docks[c["dock_code"]]["dock_type"],
                 "start": clock(to_minutes(c["slot_start_ts"])),
                 "end": clock(to_minutes(c["slot_end_ts"])), "status": "AVAILABLE"}
                for c in candidates
            ],
            "deterministic_feasibility": {
                "feasible_ids": allowed,
                "note": "Only these slots passed ETA, compatibility, duration, dock-event and occupancy checks.",
            },
        }
        guarded = self._call(
            AgentRole.DISPATCH, scenario, allowed, "DISPATCH",
            f"select one of {allowed[:3]}..." if allowed else "NO_FEASIBLE_OPTION",
            lambda a: (a["selected_id"] in allowed) if allowed else
            (a["selected_id"] is None and a["action"] == "NO_FEASIBLE_OPTION"),
        )
        if guarded and guarded["selected_id"]:
            self._book(load, guarded["selected_id"], reason)
        else:
            self._escalate("NO_FEASIBLE_SLOT", load.shipment_id, {"reason": reason})

    def _book(self, load: Load, slot_id: str, reason: str) -> bool:
        appointment = self.current_appointment(load.shipment_id)
        new_id = self._next_id("APT")
        self.db.execute("SAVEPOINT booking")
        try:
            if appointment is not None:
                self.db.execute(
                    """
                    UPDATE appointments
                    SET appointment_status = 'CANCELLED', is_current = 0, cancelled_at = ?,
                        cancellation_reason = ?, updated_at = ?
                    WHERE appointment_id = ?
                    """,
                    (iso(self.now), f"Sandbox re-plan: {reason}", iso(self.now), appointment["appointment_id"]),
                )
            self.db.execute(
                """
                INSERT INTO appointments (appointment_id, shipment_id, slot_id, appointment_status,
                    booking_source, is_current, booked_at, replaced_appointment_id, updated_at)
                VALUES (?, ?, ?, 'PENDING_CONFIRMATION', 'SCHEDULING_TOOL', 1, ?, ?, ?)
                """,
                (new_id, load.shipment_id, slot_id, iso(self.now),
                 appointment["appointment_id"] if appointment else None, iso(self.now)),
            )
            self.db.execute("RELEASE booking")
        except sqlite3.IntegrityError as exc:
            self.db.execute("ROLLBACK TO booking")
            self.db.execute("RELEASE booking")
            self.metrics["booking_conflicts_blocked"] += 1
            self._log("GUARD", f"Booking blocked by database constraint: {exc}")
            self._escalate("NO_FEASIBLE_SLOT", load.shipment_id, {"reason": "booking conflict"})
            return False
        slot = self.current_appointment(load.shipment_id)
        self.bookings.append({
            "time": clock(self.now), "shipment_id": load.shipment_id, "slot_id": slot_id,
            "dock": slot["dock_code"], "start": clock(to_minutes(slot["slot_start_ts"])),
            "end": clock(to_minutes(slot["slot_end_ts"])), "reason": reason,
            "persona": load.persona, "true_arrival": clock(load.true_arrival),
        })
        self.metrics["bookings_pending_confirmation"] += 1
        self._log(f"BOT -> {load.driver_id}",
                  f"Proposed dock {slot['dock_code']} {self.bookings[-1]['start']}-{self.bookings[-1]['end']} "
                  f"for {load.order_reference}, pending facility confirmation.")
        return True

    def _apply_outage(self, dock_code: str) -> None:
        if dock_code in self.applied_outages or dock_code not in self.docks:
            return
        window = self.truth_outages.get(dock_code)
        if window is None or not (window[0] <= self.now < window[1]):
            self.metrics["false_outages_applied"] += 1
            window = (self.now, self.now + 120)
        start, end = window
        report = self.sim.report_dock_outage(self.docks[dock_code]["dock_id"], iso(start), iso(end))
        self.sim.verify_dock_outage(report.report_id, "FACILITY")
        self.applied_outages[dock_code] = window
        impacted = [
            item for item in self.sim.impacted_appointments(report.report_id)
            if item["appointment_status"] != "IN_PROGRESS"
        ]
        self.metrics["outage_impacted_appointments"] += len(impacted)
        self._log("SYSTEM", f"Verified outage applied to dock {dock_code} {clock(start)}-{clock(end)}; "
                            f"{len(impacted)} appointment(s) affected")
        for item in impacted:
            load = self.loads.get(item["shipment_id"])
            if load is None or load.arrived_at is not None:
                self._escalate("DOCK_OUTAGE", item["shipment_id"], {"dock_code": dock_code})
                continue
            self._reslot(load, max(load.planning_eta, self.now), f"dock {dock_code} outage")

    # ---------------------------------------------------- truth & humans
    def _arrive(self, load: Load) -> None:
        load.arrived_at = load.true_arrival
        if load.last_claim is not None:
            self.claim_errors[load.driver_id].append(load.true_arrival - load.last_claim)
        appointment = self.current_appointment(load.shipment_id)
        if appointment is None:
            outcome = "NO_SLOT"
        else:
            start = to_minutes(appointment["slot_start_ts"])
            if load.arrived_at < start - EARLY_LIMIT_MIN:
                outcome = "EARLY_WAIT"
            elif load.arrived_at <= start + GRACE_MIN:
                outcome = "ON_PLAN"
                if appointment["appointment_status"] == "PENDING_CONFIRMATION":
                    self.db.execute(
                        "UPDATE appointments SET appointment_status = 'CONFIRMED', confirmed_at = ?, "
                        "updated_at = ? WHERE appointment_id = ?",
                        (iso(self.now), iso(self.now), appointment["appointment_id"]),
                    )
            else:
                outcome = "MISSED_SLOT"
        self.arrivals.append({
            "shipment_id": load.shipment_id, "persona": load.persona,
            "arrived": clock(load.arrived_at), "last_claim": clock(load.last_claim) if load.last_claim else None,
            "slot": None if appointment is None else
            f"{appointment['dock_code']} {clock(to_minutes(appointment['slot_start_ts']))}",
            "outcome": outcome,
        })
        self._log("GATE", f"{load.shipment_id} checked in at {clock(load.arrived_at)} -> {outcome}")
        if outcome in ("MISSED_SLOT", "NO_SLOT"):
            load.planning_eta = load.arrived_at
            self._reslot(load, self.now, "truck at gate after its slot (verified gate check-in)")

    def _escalate(self, kind: str, shipment_id: str | None, payload: dict[str, Any]) -> None:
        load = self.loads.get(shipment_id) if shipment_id else None
        appointment = self.current_appointment(shipment_id) if shipment_id else None
        minutes_to_impact = (to_minutes(appointment["slot_start_ts"]) - self.now) if appointment else None
        affected = 1
        if kind == "DOCK_OUTAGE" and payload.get("dock_code"):
            affected = max(1, sum(
                1 for l in self.loads.values()
                if (a := self.current_appointment(l.shipment_id)) and a["dock_code"] == payload["dock_code"]
            ))
        priority = review_priority(
            kind, affected_loads=affected,
            shipment_priority=load.priority if load else "NORMAL",
            minutes_to_impact=minutes_to_impact,
        )
        key = f"{kind}:{shipment_id or payload.get('dock_code')}"
        status = self.queue.submit(ReviewItem(key, kind, priority, self.now, {"shipment_id": shipment_id, **payload}))
        self.metrics[f"review_{status.lower()}"] += 1
        self._log("COORDINATOR QUEUE", f"{kind} for {shipment_id or payload.get('dock_code')} "
                                       f"(priority {priority}) -> {status}")
        if status == "DIGEST" and load is not None and load.arrived_at is None:
            self._human_replan(load, via="low-priority digest follow-up")

    def _resolve(self, item: ReviewItem) -> None:
        self.metrics["coordinator_reviews_completed"] += 1
        shipment_id = item.payload.get("shipment_id")
        load = self.loads.get(shipment_id) if shipment_id else None
        if item.kind == "DOCK_OUTAGE":
            dock = item.payload.get("dock_code")
            window = self.truth_outages.get(dock) if dock else None
            if window and window[0] <= self.now < window[1]:
                self._apply_outage(dock)
            self._log("COORDINATOR", f"Reviewed dock {dock} report "
                                     f"({'confirmed by phone' if window else 'not broken; dismissed'})")
        elif item.kind == "NO_FEASIBLE_SLOT":
            self.metrics["manual_replans"] += 1
            self._log("COORDINATOR", f"Manual re-plan for {shipment_id} (overtime/alternate facility decision)")
        elif load is not None and load.arrived_at is None:
            self._human_replan(load, via="coordinator phone call")
        else:
            self._log("COORDINATOR", f"Closed {item.kind} for {shipment_id}")

    def _human_replan(self, load: Load, via: str) -> None:
        """A human phones the driver and gets the true ETA, then books deterministically."""
        self.metrics["human_replans"] += 1
        load.last_claim = load.true_arrival
        load.planning_eta = max(load.true_arrival, self.now)
        appointment = self.current_appointment(load.shipment_id)
        if appointment is not None and not self._slot_blocked(appointment):
            start = to_minutes(appointment["slot_start_ts"])
            if start - EARLY_LIMIT_MIN <= load.planning_eta <= start + GRACE_MIN:
                self._log("COORDINATOR", f"{load.shipment_id}: {via}; current slot still fits")
                return
        report = self.sim.report_driver_eta(load.shipment_id, iso(load.planning_eta), "HIGH")
        options = [c for c in self.sim.provisional_slots(report.report_id)
                   if to_minutes(c["slot_start_ts"]) < LAST_NEW_START]
        self._log("COORDINATOR", f"{load.shipment_id}: {via}; true ETA {clock(load.planning_eta)}")
        if options:
            self._book(load, options[0]["slot_id"], f"{via}; ETA confirmed by phone")
        else:
            self.metrics["manual_replans"] += 1

    # --------------------------------------------------------------- scoring
    def safety_checks(self) -> dict[str, bool]:
        db = self.db
        double_slots = db.execute(
            "SELECT COUNT(*) FROM (SELECT slot_id FROM appointments WHERE appointment_status IN "
            "('PENDING_CONFIRMATION','CONFIRMED','IN_PROGRESS') GROUP BY slot_id HAVING COUNT(*) > 1)"
        ).fetchone()[0]
        multi_current = db.execute(
            "SELECT COUNT(*) FROM (SELECT shipment_id FROM appointments WHERE is_current = 1 AND "
            "appointment_status IN ('PENDING_CONFIRMATION','CONFIRMED','IN_PROGRESS') "
            "GROUP BY shipment_id HAVING COUNT(*) > 1)"
        ).fetchone()[0]
        foreign_keys = db.execute("PRAGMA foreign_key_check").fetchall()
        infeasible = 0
        for booking in self.bookings:
            row = db.execute(
                """
                SELECT s.required_dock_type, s.temperature_control_required, s.load_weight_kg,
                       s.expected_unload_min, d.dock_type, d.supports_refrigerated,
                       d.max_vehicle_weight_kg, sl.slot_start_ts, sl.slot_end_ts
                FROM appointment_slots sl JOIN docks d ON d.dock_id = sl.dock_id
                JOIN shipments s ON s.shipment_id = ?
                WHERE sl.slot_id = ?
                """,
                (booking["shipment_id"], booking["slot_id"]),
            ).fetchone()
            duration = to_minutes(row["slot_end_ts"]) - to_minutes(row["slot_start_ts"])
            if (
                row["required_dock_type"] not in ("ANY", row["dock_type"])
                or (row["temperature_control_required"] and not row["supports_refrigerated"])
                or row["load_weight_kg"] > row["max_vehicle_weight_kg"]
                or duration < row["expected_unload_min"]
                or to_minutes(row["slot_start_ts"]) >= LAST_NEW_START
            ):
                infeasible += 1
        return {
            "No slot double-booked": double_slots == 0,
            "No shipment with two current bookings": multi_current == 0,
            "Foreign keys intact": not foreign_keys,
            "No false dock outage applied": self.metrics["false_outages_applied"] == 0,
            "No infeasible booking": infeasible == 0,
        }

    def summary(self) -> dict[str, Any]:
        correct = sum(j["correct"] for j in self.judgments)
        decision_accuracy = correct / len(self.judgments) if self.judgments else 0.0
        safety = self.safety_checks()
        safety_rate = sum(safety.values()) / len(safety)
        good = sum(a["outcome"] in ("ON_PLAN", "EARLY_WAIT") for a in self.arrivals)
        on_plan_rate = good / len(self.arrivals) if self.arrivals else None
        weights = {"decisions": 50, "safety": 30, "outcomes": 20 if on_plan_rate is not None else 0}
        weight_total = sum(weights.values())
        score = 100 * (
            weights["decisions"] * decision_accuracy
            + weights["safety"] * safety_rate
            + weights["outcomes"] * (on_plan_rate or 0)
        ) / weight_total
        calls = [c for c in self.llm_calls if c["role"] in {r.value for r in AgentRole}]
        return {
            "score": score,
            "decision_accuracy": decision_accuracy,
            "decisions": len(self.judgments),
            "safety": safety,
            "safety_rate": safety_rate,
            "on_plan_rate": on_plan_rate,
            "arrivals": len(self.arrivals),
            "weights": weights,
            "agent_calls": len(calls),
            "agent_errors": sum(not c["ok"] for c in calls),
            "role_play_calls": len(self.llm_calls) - len(calls),
            "mean_agent_latency_s": mean(c["seconds"] for c in calls) if calls else 0.0,
            "queue_max_pending": self.queue.max_pending,
            "queue_mean_wait_min": mean(self.queue.waits) if self.queue.waits else 0.0,
            "metrics": dict(self.metrics),
        }

    # ---------------------------------------------------------------- report
    def render_report(self, model: str) -> str:
        s = self.summary()
        by_kind: dict[str, list[bool]] = defaultdict(list)
        for j in self.judgments:
            by_kind[f"{j['role']}: {j['kind']}"].append(j["correct"])
        decision_rows = [
            (kind, 100 * sum(v) / len(v), f"({sum(v)}/{len(v)})") for kind, v in sorted(by_kind.items())
        ]
        safety_rows = [(name, 100.0 if ok else 0.0, "pass" if ok else "FAIL") for name, ok in s["safety"].items()]
        outcomes = ("ON_PLAN", "EARLY_WAIT", "MISSED_SLOT", "NO_SLOT")
        persona_rows = []
        for persona in PERSONAS:
            counts = Counter(a["outcome"] for a in self.arrivals if a["persona"] == persona)
            if counts:
                persona_rows.append((persona, tuple(counts[o] for o in outcomes)))
        role_counts = Counter(c["role"] for c in self.llm_calls)
        role_latency = {
            role: mean(c["seconds"] for c in self.llm_calls if c["role"] == role) for role in role_counts
        }
        role_errors = Counter(c["role"] for c in self.llm_calls if not c["ok"])
        latency_rows = "".join(
            f"<tr><td>{escape(role)}</td><td>{count}</td><td>{role_latency[role]:.1f}s</td>"
            f"<td>{role_errors[role]}</td></tr>"
            for role, count in sorted(role_counts.items())
        )
        persona_table = "".join(
            f"<tr><td>{escape(driver)}</td><td>{escape(persona)}</td>"
            f"<td>{', '.join(l.shipment_id for l in self.loads.values() if l.driver_id == driver)}</td></tr>"
            for driver, persona in sorted(self.personas.items())
        )
        metric_rows = "".join(
            f"<tr><td>{escape(name.replace('_', ' '))}</td><td>{value}</td></tr>"
            for name, value in sorted(s["metrics"].items())
        )
        transcript_rows = []
        for entry in self.transcript:
            details = ""
            if "raw" in entry:
                flag = "&#10004; correct" if entry.get("correct") else "&#10008; incorrect"
                extra = ""
                if entry.get("violations"):
                    extra += "<p class='error'>Guard: " + escape("; ".join(entry["violations"])) + "</p>"
                if entry.get("ungrounded"):
                    extra += "<p class='error'>Ungrounded evidence: " + escape(", ".join(entry["ungrounded"])) + "</p>"
                details = (
                    f"<details><summary>{flag} &mdash; expected: {escape(entry.get('expected', ''))}</summary>"
                    f"{extra}<pre>{escape(json.dumps(entry['raw'], indent=2))}</pre></details>"
                )
            css = " class='agent'" if "raw" in entry else ""
            transcript_rows.append(
                f"<tr{css}><td>{entry['time']}</td><td>{escape(entry['actor'])}</td>"
                f"<td>{escape(entry['text'])}{details}</td></tr>"
            )
        on_plan = "n/a" if s["on_plan_rate"] is None else f"{100 * s['on_plan_rate']:.0f}%"
        generated = datetime.now(timezone.utc).isoformat(timespec="seconds")
        return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>SetuHaul live sandbox report</title>
<style>
body {{font:15px/1.55 system-ui,sans-serif;max-width:1180px;margin:32px auto;padding:0 20px;color:#17233b;background:#f4f6fa}}
header,section {{background:white;border:1px solid #d5dce8;border-radius:12px;padding:24px;margin:20px 0}}
.score {{font-size:48px;font-weight:750;color:#164c93}}
table {{width:100%;border-collapse:collapse}} th,td {{padding:8px;text-align:left;border-bottom:1px solid #e3e7ef;vertical-align:top}}
pre {{white-space:pre-wrap;overflow-wrap:anywhere;background:#f4f6fa;padding:12px;font-size:12px}}
tr.agent td {{background:#f8fafd}} .error {{color:#a31726}} .grid {{display:grid;grid-template-columns:1fr 1fr;gap:20px}}
{CHART_CSS}
</style></head><body>
<header><h1>Live multi-agent sandbox: workflow confidence</h1>
<div class="score">{s['score']:.1f}/100</div>
<p>Decision accuracy {100 * s['decision_accuracy']:.0f}% ({s['decisions']} agent decisions) |
Safety checks {sum(s['safety'].values())}/{len(s['safety'])} | On-plan arrivals {on_plan} ({s['arrivals']} arrivals)</p>
<p>Model: {escape(model)} | Seed: {self.seed} | Simulated {clock(self.start)}&ndash;{clock(self.sim_end)} |
Wall time {self.wall_elapsed / 60:.1f} min | Stopped: {escape(self.stop_reason)} | Generated: {generated}</p></header>
<section><h2>How to interpret this score</h2>
<p>Score = {s['weights']['decisions']}% decision accuracy (each triage, dispatch and assessment
decision judged against hidden ground truth) + {s['weights']['safety']}% safety invariants +
{s['weights']['outcomes']}% on-plan arrivals (truck arrives within its slot window or early).
Weights are renormalized if no truck arrived. This is a rubric metric for one seeded run,
not a calibrated probability or production-readiness rating.</p>
<p>Driver and facility messages are written by the LLM role-playing personas from deterministic
briefs; a persona message that drops a required fact is replaced by a template
({s['metrics'].get('persona_fallbacks', 0)} times). Optimistic drivers' false ETAs cannot be
detected without GPS; their effect appears in arrival outcomes. Coordinators are simulated as
humans who learn the truth by phone after a {self.queue.review_minutes}-minute review.</p></section>
<section><h2>Charts</h2>
{bar_chart("Decision accuracy by agent and situation", decision_rows)}
{bar_chart("Safety invariants", safety_rows)}
{stacked_chart("Arrival outcome by driver persona", persona_rows,
               [("On plan", PASS), ("Early wait", PARTIAL), ("Missed slot", FAIL), ("No slot", ERROR)])}
{line_chart("Coordinator review queue (pending items)", self.queue_series, "pending")}
</section>
<div class="grid">
<section><h2>Operations metrics</h2><table><tbody>
<tr><td>Agent calls (errors)</td><td>{s['agent_calls']} ({s['agent_errors']})</td></tr>
<tr><td>Role-play calls</td><td>{s['role_play_calls']}</td></tr>
<tr><td>Mean agent latency</td><td>{s['mean_agent_latency_s']:.1f}s</td></tr>
<tr><td>Coordinator queue max / mean wait</td><td>{s['queue_max_pending']} / {s['queue_mean_wait_min']:.0f} min</td></tr>
{metric_rows}</tbody></table></section>
<section><h2>LLM calls by role</h2><table>
<thead><tr><th>Role</th><th>Calls</th><th>Mean latency</th><th>Errors</th></tr></thead>
<tbody>{latency_rows}</tbody></table>
<h2>Hidden driver personas</h2><table>
<thead><tr><th>Driver</th><th>Persona</th><th>Loads</th></tr></thead><tbody>{persona_table}</tbody></table></section>
</div>
<section><h2>Conversation and decision timeline</h2><table>
<thead><tr><th>Sim time</th><th>Actor</th><th>Message / decision</th></tr></thead>
<tbody>{''.join(transcript_rows)}</tbody></table></section>
</body></html>"""

    def to_json(self, model: str) -> str:
        return json.dumps({
            "model": model, "seed": self.seed, "start": clock(self.start), "end": clock(self.sim_end),
            "wall_seconds": self.wall_elapsed, "stop_reason": self.stop_reason,
            "summary": self.summary(), "personas": self.personas, "judgments": self.judgments,
            "arrivals": self.arrivals, "bookings": self.bookings, "transcript": self.transcript,
            "llm_calls": self.llm_calls,
        }, indent=2, default=str)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--minutes", type=float, default=15, help="Wall-clock run time (default 15)")
    parser.add_argument("--seed", type=int, default=7)
    parser.add_argument("--start", default="09:30", help="Simulated start time, HH:MM")
    parser.add_argument("--report", type=Path, default=Path("sandbox_report.html"))
    parser.add_argument("--no-pace", action="store_true",
                        help="Do not spread the simulated day across the wall-clock budget")
    args = parser.parse_args()
    if args.minutes <= 0:
        parser.error("--minutes must be positive")
    try:
        load_nvidia_env()
    except NVIDIAClientError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    import os
    if not os.environ.get("NVIDIA_API_KEY"):
        print("NVIDIA_API_KEY is not set; add it to .env or the environment.", file=sys.stderr)
        return 2

    model = os.environ.get("NVIDIA_MODEL_ID", DEFAULT_MODEL)
    sandbox = Sandbox(seed=args.seed, start=args.start, wall_seconds=args.minutes * 60,
                      pace=not args.no_pace, echo=lambda line: print(line, flush=True))
    print(f"Sandbox starting: {len(sandbox.loads)} loads, personas {sandbox.personas}", flush=True)
    sandbox.run()
    args.report.write_text(sandbox.render_report(model), encoding="utf-8")
    args.report.with_suffix(".json").write_text(sandbox.to_json(model), encoding="utf-8")
    summary = sandbox.summary()
    print(f"\nStopped: {sandbox.stop_reason}; simulated {clock(sandbox.start)}-{clock(sandbox.sim_end)} "
          f"in {sandbox.wall_elapsed / 60:.1f} min")
    print(f"Workflow confidence (sandbox rubric): {summary['score']:.1f}/100")
    print(f"Decision accuracy: {100 * summary['decision_accuracy']:.0f}% of {summary['decisions']}")
    print(f"Safety: {summary['safety']}")
    print(f"HTML report: {args.report.resolve()}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
