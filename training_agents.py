"""Deterministic, non-operational driver and facility personas for dashboard training."""

from __future__ import annotations

import re
from typing import Any


AGENTS = {
    "driver_01": {
        "role": "driver",
        "name": "Ravi · Driver 06",
        "badge": "DRIVER ROLEPLAY",
        "scenario": "Late inbound · SHP1006 · ETA 11:20",
        "opening": "Hi, I'm running late on SHP1006. Traffic has slowed us down; I expect to reach around 11:20.",
    },
    "driver_02": {
        "role": "driver",
        "name": "Anika · Driver 04",
        "badge": "DRIVER ROLEPLAY",
        "scenario": "Two-load clarification · SHP1004 / SHP1020",
        "opening": "I have two loads today, SHP1004 and SHP1020. Which one are we discussing?",
    },
    "driver_03": {
        "role": "driver",
        "name": "Dev · Driver 01",
        "badge": "DRIVER ROLEPLAY",
        "scenario": "Slot selection · SHP1017 · after 13:00",
        "opening": "I can reach after 13:00 for SHP1017. If you send options, I'll tell you which one works.",
    },
    "driver_04": {
        "role": "driver",
        "name": "Kiran · Driver 14",
        "badge": "DRIVER ROLEPLAY",
        "scenario": "Roadside breakdown · safety escalation",
        "opening": "My truck has broken down at the roadside. Should I continue to the facility?",
    },
    "facility": {
        "role": "facility",
        "name": "Meera · Jaipur Dock Desk",
        "badge": "FACILITY ROLEPLAY",
        "scenario": "Dock status · D3 unavailable until 13:00",
        "opening": "Dock D3 is still under repair. We expect it back online at 13:00. Please don't send another truck there yet.",
    },
}

_ROLE_DEFAULTS = {"driver": "driver_01", "facility": "facility"}
_SAFETY = re.compile(
    r"\b(emergency|accident|fire|injur(?:y|ed)|brakes?\s+(?:failed|failure)|medical|breakdown|broken\s+down)\b", re.I
)


def respond(agent_id: str, message: str) -> dict[str, Any]:
    """Return one deterministic training turn; never reads or changes operational records."""
    agent_id = _ROLE_DEFAULTS.get(agent_id, agent_id)
    if agent_id not in AGENTS:
        raise ValueError(f"unknown training agent: {agent_id}")
    text = message.strip()
    if not text:
        raise ValueError("message is required")

    profile = AGENTS[agent_id]
    role = profile["role"]
    if _SAFETY.search(text) or agent_id == "driver_04":
        response = (
            "Training cue: pause scheduling. Acknowledge the safety report, direct the driver to a safe place "
            "and local emergency support, and route the case to a human coordinator. Do not promise dispatch "
            "or emergency services were contacted."
        )
        lesson = "Safety first · escalate; never continue slot selection."
    elif agent_id == "driver_01" and re.search(r"\b(eta|when|reach|arrive|time)\b", text, re.I):
        response = "Traffic is moving slowly. My best estimate is 11:20, but I may need another 10 minutes."
        lesson = "ETA is a driver report, not independently verified."
    elif agent_id == "driver_01" and re.search(r"\b(load|shipment|which|reference|order)\b", text, re.I):
        response = "I'm driving SHP1006, order ORD-260804-006. It's the medical-devices load."
        lesson = "Confirm the shipment link before discussing slot options."
    elif agent_id == "driver_02":
        response = "I have two loads, SHP1004 and SHP1020. Please confirm the shipment or order reference before we change either plan."
        lesson = "Resolve an ambiguous driver-to-shipment link before offering or changing a slot."
    elif agent_id == "driver_03" and re.search(r"\b(option|slot|book|dock)\b", text, re.I):
        response = "Option 1 could work for me. Can you confirm it's only held, not booked yet?"
        lesson = "A driver selection may request a hold; only the warehouse confirms a booking."
    elif role == "driver":
        response = "I can check my shipment, share an updated arrival time, or choose from options you've offered."
        lesson = "Ask one clear question; do not invent facts or claim a slot is booked."
    elif role == "facility" and re.search(r"\b(d3|repair|maintenance|outage|broken|status)\b", text, re.I):
        response = "D3 remains unavailable until 13:00. This is a facility report; record it as verified evidence and block overlapping slots."
        lesson = "Facility statements can be independent verification; reflect the time window precisely."
    elif role == "facility" and re.search(r"\b(confirm|booking|appointment|warehouse|hold)\b", text, re.I):
        response = "I can confirm warehouse reference WH-TRAIN-014 for the requested appointment. This is a training-only reference."
        lesson = "A confirmation must match a requested hold and pass the engine's fresh feasibility check."
    else:
        response = "Training desk here. D3 is unavailable until 13:00; tell me the dock and shipment if you need a specific status check."
        lesson = "Capture the affected dock, interval, and source before changing availability."
    return {
        "role": role,
        "agent_id": agent_id,
        "agent": profile["name"],
        "reply": response,
        "lesson": lesson,
        "simulated": True,
    }
