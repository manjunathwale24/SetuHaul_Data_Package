"""Deterministic guards around LLM agent decisions.

Times are compared as minutes since midnight because the dataset is a single-day
Asia/Kolkata snapshot.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
import json
import re
from typing import Any


_ID = re.compile(r"\b[A-Z][A-Z0-9]*(?:-[A-Z0-9]+)+\b|\b[A-Z]{2,}\d+[A-Z]?\b")
_TIME = re.compile(r"(?<!\d)(\d{1,2}):(\d{2})(?!\d)")
_NUMBER = re.compile(r"(?<![\w:.\-])\d+(?:\.\d+)?(?![\w:])")
_THOUSANDS = re.compile(r"(?<=\d),(?=\d{3}\b)")


def to_minutes(value: str) -> int:
    if "T" in value:
        parsed = datetime.fromisoformat(value)
        return parsed.hour * 60 + parsed.minute
    hours, minutes = value.split(":")
    return int(hours) * 60 + int(minutes)


def feasible_slot_ids(scenario: dict[str, Any]) -> list[str]:
    """Slots that every hard constraint allows for at least one listed shipment."""
    if "shipments" in scenario:
        shipments = scenario["shipments"]
    elif "shipment" in scenario:
        shipments = [scenario["shipment"]]
    else:
        shipments = []
    feasible = []
    for slot in scenario.get("slots", []):
        if slot.get("status") != "AVAILABLE":
            continue
        start, end = to_minutes(slot["start"]), to_minutes(slot["end"])
        for shipment in shipments:
            eta = shipment.get("eta") or shipment.get("eta_report") or shipment.get("planning_eta")
            required = shipment.get("required_dock_type", "ANY")
            if required not in ("ANY", slot.get("dock_type")):
                continue
            if shipment.get("temperature_control_required") and not slot.get(
                "supports_refrigerated", slot.get("dock_type") == "REEFER"
            ):
                continue
            weight, limit = shipment.get("load_weight_kg"), slot.get("max_vehicle_weight_kg")
            if weight is not None and limit is not None and weight > limit:
                continue
            if eta and start < to_minutes(eta):
                continue
            if end - start < shipment.get("expected_unload_min", 0):
                continue
            feasible.append(slot["id"])
            break
    return feasible


def feasible_option_ids(scenario: dict[str, Any]) -> list[str]:
    load = scenario.get("load", {})
    weight = load.get("weight_kg", 0)
    required = load.get("required_dock_type", "ANY")
    feasible = []
    for option in scenario.get("options", []):
        if not option.get("dock_available", True):
            continue
        if option.get("vehicle_capacity_kg", weight) < weight:
            continue
        if option.get("dock_max_weight_kg", weight) < weight:
            continue
        if required not in ("ANY", option.get("dock_type")):
            continue
        if load.get("temperature_control_required") and not option.get("supports_refrigerated"):
            continue
        feasible.append(option["id"])
    return feasible


@dataclass(frozen=True)
class Facts:
    ids: frozenset[str]
    times: frozenset[str]
    numbers: frozenset[str]


def _normalize_time(hours: str, minutes: str) -> str:
    return f"{int(hours)}:{minutes}"


def extract_facts(text: str) -> Facts:
    text = _THOUSANDS.sub("", text)
    return Facts(
        frozenset(_ID.findall(text)),
        frozenset(_normalize_time(h, m) for h, m in _TIME.findall(text)),
        frozenset(str(float(n)).rstrip("0").rstrip(".") for n in _NUMBER.findall(text)),
    )


def _derived_numbers(value: Any) -> set[str]:
    """Counts and slot durations a careful reader can state without guessing."""
    derived: set[str] = set()
    if isinstance(value, list):
        derived.add(str(len(value)))
        for item in value:
            derived |= _derived_numbers(item)
    elif isinstance(value, dict):
        start, end = value.get("start"), value.get("end")
        if isinstance(start, str) and isinstance(end, str):
            try:
                derived.add(str(to_minutes(end) - to_minutes(start)))
            except ValueError:
                pass
        for item in value.values():
            derived |= _derived_numbers(item)
    return derived


def ungrounded_evidence(answer: dict[str, Any], scenario: dict[str, Any]) -> list[str]:
    """IDs, times, and numbers cited as evidence that are absent from the input."""
    source = extract_facts(json.dumps(scenario))
    allowed_numbers = source.numbers | _derived_numbers(scenario)
    missing: list[str] = []
    for item in answer.get("evidence", []):
        cited = extract_facts(item)
        missing.extend(sorted(cited.ids - source.ids))
        missing.extend(sorted(cited.times - source.times))
        missing.extend(sorted(cited.numbers - allowed_numbers))
    return list(dict.fromkeys(missing))


def guard_answer(
    answer: dict[str, Any],
    allowed_ids: list[str] | None,
    scenario: dict[str, Any],
) -> tuple[dict[str, Any], list[str]]:
    """Return a safe copy of the answer and every intervention the guard made.

    allowed_ids=None means the selection only has to reference an ID in the input.
    """
    guarded = dict(answer)
    violations: list[str] = []
    if answer.get("state_change_allowed") is not False:
        violations.append("model tried to authorize a state change")
        guarded["state_change_allowed"] = False

    selected = answer.get("selected_id")
    if selected is None:
        return guarded, violations
    if allowed_ids is None:
        if selected not in extract_facts(json.dumps(scenario)).ids:
            violations.append(f"selected_id {selected} does not appear in the input")
            guarded["selected_id"] = None
        return guarded, violations
    if selected not in allowed_ids:
        violations.append(
            f"selected {selected} outside the deterministic feasible set {sorted(allowed_ids)}"
        )
        guarded["selected_id"] = None
        if allowed_ids:
            guarded["action"], guarded["ask_whom"] = "ESCALATE", "COORDINATOR"
        else:
            guarded["action"], guarded["ask_whom"] = "NO_FEASIBLE_OPTION", "NONE"
    return guarded, violations


TYPE_WEIGHT = {"DOCK_OUTAGE": 50, "ARRIVAL_CLAIM": 20, "NO_FEASIBLE_SLOT": 35, "ETA_UPDATE": 15}
PRIORITY_WEIGHT = {"CRITICAL": 40, "HIGH": 25, "NORMAL": 10, "LOW": 0}
REVIEW_THRESHOLD = 40


def review_priority(
    report_type: str,
    *,
    affected_loads: int = 1,
    shipment_priority: str = "NORMAL",
    minutes_to_impact: int | None = None,
) -> int:
    score = TYPE_WEIGHT.get(report_type, 5)
    score += 10 * max(affected_loads, 0)
    score += PRIORITY_WEIGHT.get(shipment_priority, 10)
    if minutes_to_impact is not None and minutes_to_impact <= 60:
        score += 20
    return score


@dataclass
class ReviewItem:
    key: str
    kind: str
    priority: int
    submitted: int
    payload: dict[str, Any]
    started: int | None = None


@dataclass
class CoordinatorQueue:
    """Five coordinators work the highest-impact review items first."""

    coordinators: int = 5
    review_minutes: int = 15
    threshold: int = REVIEW_THRESHOLD
    pending: list[ReviewItem] = field(default_factory=list)
    active: list[tuple[int, ReviewItem]] = field(default_factory=list)
    waits: list[int] = field(default_factory=list)
    merged: int = 0
    below_threshold: int = 0
    submitted: int = 0
    max_pending: int = 0

    def submit(self, item: ReviewItem) -> str:
        """Returns QUEUED, MERGED (duplicate alert), or DIGEST (below threshold)."""
        if any(existing.key == item.key for existing in self.pending) or any(
            active.key == item.key for _, active in self.active
        ):
            self.merged += 1
            return "MERGED"
        if item.priority < self.threshold:
            self.below_threshold += 1
            return "DIGEST"
        self.pending.append(item)
        self.submitted += 1
        self.max_pending = max(self.max_pending, len(self.pending))
        return "QUEUED"

    def advance(self, now: int) -> list[ReviewItem]:
        finished = [item for done_at, item in self.active if done_at <= now]
        self.active = [(done_at, item) for done_at, item in self.active if done_at > now]
        self.pending.sort(key=lambda item: (-item.priority, item.submitted))
        while self.pending and len(self.active) < self.coordinators:
            item = self.pending.pop(0)
            item.started = now
            self.waits.append(now - item.submitted)
            self.active.append((now + self.review_minutes, item))
        return finished
