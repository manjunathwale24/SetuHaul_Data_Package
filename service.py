"""Conversational driver-exception service with dock-slot hold, confirm and release.

A stdlib JSON API around one SlotEngine. The clock is simulated: every request carries
"now" as HH:MM on the 4 Aug 2026 snapshot day. Messages only ever produce options,
clarification questions or escalations; slot state changes only through the explicit
/holds and /appointments endpoints, each re-checked by the deterministic engine and
written to service_audit_log.

Usage:
    python3 service.py --port 8080 [--db path] [--live]
"""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import re
import sqlite3
import threading
from typing import Any, Callable
from urllib.parse import parse_qs, unquote, urlparse

from conversation_graph import build_message_graph
from brief_scenarios import HANDOFF_FIELDS, revise_eta, shipment_card
from guards import guard_answer, to_minutes
from nvidia_agents import AgentRole, NVIDIAClientError, ask_agent, load_nvidia_env
from slot_engine import HOLD_TTL_MIN, HOLDS_SCHEMA, SlotCheck, SlotEngine, hhmm, ts
from training_agents import AGENTS as TRAINING_AGENTS, respond as training_agent_respond


AgentFn = Callable[[AgentRole, dict[str, Any]], dict[str, Any]]
MAX_OPTIONS = 3
INACTIVE_SHIPMENTS = ("COMPLETED", "CANCELLED")
TRAINING_FACILITY_ID = "FAC-JAI-01"

SERVICE_SCHEMA = """
CREATE TABLE IF NOT EXISTS service_audit_log (
    audit_id INTEGER PRIMARY KEY,
    sim_ts TEXT NOT NULL,
    recorded_at TEXT NOT NULL,
    actor TEXT NOT NULL,
    action TEXT NOT NULL,
    payload TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS service_messages (
    external_id TEXT PRIMARY KEY,
    thread_key TEXT NOT NULL,
    sender TEXT NOT NULL,
    received_min INTEGER NOT NULL,
    response TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS service_threads (
    thread_key TEXT PRIMARY KEY,
    sender TEXT NOT NULL,
    shipment_id TEXT,
    awaiting TEXT,
    pending_text TEXT,
    not_before INTEGER,
    finish_by INTEGER,
    offered_options TEXT,
    updated_min INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS service_etas (
    eta_id INTEGER PRIMARY KEY,
    shipment_id TEXT NOT NULL REFERENCES shipments(shipment_id),
    reported_min INTEGER NOT NULL,
    eta_min INTEGER NOT NULL,
    planning_eta_min INTEGER NOT NULL,
    confidence TEXT NOT NULL,
    source TEXT NOT NULL,
    external_id TEXT,
    not_before INTEGER,
    finish_by INTEGER
);
CREATE TABLE IF NOT EXISTS service_holds (
    group_key TEXT PRIMARY KEY,
    shipment_id TEXT NOT NULL REFERENCES shipments(shipment_id),
    eta_min INTEGER NOT NULL,
    created_min INTEGER NOT NULL,
    not_before INTEGER,
    finish_by INTEGER,
    eta_id INTEGER
);
CREATE TABLE IF NOT EXISTS service_escalations (
    case_id TEXT PRIMARY KEY,
    shipment_id TEXT NOT NULL REFERENCES shipments(shipment_id),
    created_min INTEGER NOT NULL,
    record TEXT NOT NULL
);
"""

_TIME_TAIL = r"(\d{1,2})(?:[:.](\d{2}))?\s*(am|pm)?\b"
_NOT_BEFORE = re.compile(r"\bafter\s+" + _TIME_TAIL, re.I)
_FINISH_BY = re.compile(r"\b(?:leave|done|finish(?:ed)?|out|go)\s+(?:by|before)\s+" + _TIME_TAIL, re.I)
_SHIPMENT_ID = re.compile(r"\bSHP\d{4}\b")


class ApiError(Exception):
    def __init__(self, http_status: int, error: str, **extra: Any):
        super().__init__(error)
        self.http_status, self.body = http_status, {"error": error, **extra}


def _clock(h: str, m: str | None, ampm: str | None) -> int:
    hour, minute = int(h), int(m or 0)
    if ampm and ampm.lower() == "pm" and hour < 12:
        hour += 12
    elif not ampm and hour < 7:
        hour += 12
    return hour * 60 + minute


def parse_constraints(text: str) -> tuple[int | None, int | None, str]:
    """(not_before, finish_by, text without those phrases) so revise_eta only sees the arrival time."""
    not_before = finish_by = None
    if m := _FINISH_BY.search(text):
        finish_by = _clock(*m.groups())
        text = text[:m.start()] + text[m.end():]
    if m := _NOT_BEFORE.search(text):
        not_before = _clock(*m.groups())
        text = text[:m.start()] + text[m.end():]
    return not_before, finish_by, text


def parse_now(value: Any) -> int:
    if not isinstance(value, str) or not re.fullmatch(r"\d{1,2}:\d{2}", value):
        raise ApiError(400, "now must be HH:MM")
    minutes = to_minutes(value)
    if not 0 <= minutes < 24 * 60:
        raise ApiError(400, "now must be HH:MM")
    return minutes


def _option(i: int, c: SlotCheck, eta: int) -> dict[str, Any]:
    return {"option": i + 1, "slot_id": c.slot_ids[0], "slot_ids": c.slot_ids, "dock": c.dock,
            "start": hhmm(c.start), "unload_ends": hhmm(c.end), "minutes_after_eta": c.start - eta}


class Service:
    def __init__(self, db_path: str = ":memory:", *, agent: AgentFn | None = None):
        self.db_path = db_path
        self.engine = SlotEngine(db_path, seed=not self._is_seeded(db_path))
        self.db = self.engine.db
        self.db.executescript(HOLDS_SCHEMA + SERVICE_SCHEMA)
        self._ensure_column("service_threads", "offered_options", "TEXT")
        self._ensure_column("service_holds", "not_before", "INTEGER")
        self._ensure_column("service_holds", "finish_by", "INTEGER")
        self._ensure_column("service_holds", "eta_id", "INTEGER")
        self.db.execute(
            "UPDATE service_holds SET eta_id=COALESCE((SELECT MAX(eta_id) FROM service_etas"
            " WHERE service_etas.shipment_id=service_holds.shipment_id"
            " AND service_etas.reported_min<=service_holds.created_min),0)"
            " WHERE eta_id IS NULL")
        self.agent = agent
        self.lock = threading.RLock()
        self.message_graph = build_message_graph(
            self._process_message,
            lambda body: self._process_message(body, force_emergency=True),
            self._is_driver,
        )

    def _ensure_column(self, table: str, column: str, declaration: str) -> None:
        columns = {row["name"] for row in self.db.execute(f"PRAGMA table_info({table})")}
        if column not in columns:
            self.db.execute(f"ALTER TABLE {table} ADD COLUMN {column} {declaration}")

    def _is_driver(self, sender: str) -> bool:
        return self.db.execute("SELECT 1 FROM drivers WHERE driver_id=?", (sender,)).fetchone() is not None

    @staticmethod
    def _is_seeded(db_path: str) -> bool:
        if db_path == ":memory:" or not Path(db_path).exists():
            return False
        with sqlite3.connect(db_path) as probe:
            return probe.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='shipments'").fetchone() is not None

    # ----- helpers --------------------------------------------------------
    def audit(self, now: int, actor: str, action: str, payload: dict[str, Any]) -> None:
        self.db.execute(
            "INSERT INTO service_audit_log(sim_ts, recorded_at, actor, action, payload) VALUES (?,?,?,?,?)",
            (ts(now), datetime.now(timezone.utc).isoformat(timespec="seconds"), actor, action,
             json.dumps(payload, sort_keys=True)))

    def tick(self, now: int) -> None:
        expired = self.engine.expire_holds(now)
        if expired:
            self.audit(now, "SYSTEM", "HOLDS_EXPIRED", {"rows": expired})

    def shipment(self, sid: str) -> dict[str, Any]:
        try:
            return self.engine.shipment(sid)
        except KeyError:
            raise ApiError(404, f"unknown shipment {sid}") from None

    def latest_eta(self, sid: str) -> dict[str, Any]:
        row = self.db.execute(
            "SELECT * FROM service_etas WHERE shipment_id=? ORDER BY reported_min DESC, eta_id DESC LIMIT 1",
            (sid,)).fetchone()
        if row:
            return {"eta": row["eta_min"], "planning_eta": row["planning_eta_min"], "confidence": row["confidence"],
                    "not_before": row["not_before"], "finish_by": row["finish_by"], "source": row["source"]}
        row = self.db.execute("SELECT * FROM v_latest_eta WHERE shipment_id=?", (sid,)).fetchone()
        eta = to_minutes(row["effective_eta_ts"])
        return {"eta": eta, "planning_eta": eta, "confidence": row["eta_confidence"], "not_before": None,
                "finish_by": None, "source": row["eta_source"]}

    def latest_eta_id(self, sid: str) -> int:
        row = self.db.execute("SELECT MAX(eta_id) AS eta_id FROM service_etas WHERE shipment_id=?", (sid,)).fetchone()
        return row["eta_id"] or 0

    def options(self, sid: str, eta: int, now: int, *, not_before: int | None = None,
                finish_by: int | None = None) -> tuple[list[SlotCheck], list[dict[str, Any]]]:
        checks = self.engine.check(sid, eta, now=now, not_before=not_before, finish_by=finish_by)
        feasible = [c for c in checks if c.feasible][:MAX_OPTIONS]
        return checks, [_option(i, c, eta) for i, c in enumerate(feasible)]

    def escalation(self, sid: str, eta: dict[str, Any], checks: list[SlotCheck], now: int,
                   reason: str) -> dict[str, Any]:
        shp = self.shipment(sid)
        later = [c for c in checks if c.start >= eta["planning_eta"]] or checks
        same_type = {d["dock_code"] for d in self.engine.docks() if d["dock_type"] == shp["required_dock_type"]}
        relevant = [c for c in later if c.dock in same_type] or later
        blocking = list(dict.fromkeys(r for c in relevant for r in c.reasons))[:8]
        actions = [f"Approve exception for {c.dock} {hhmm(c.start)} (only blocked by {c.reasons[0].split()[0]})"
                   for c in relevant if len(c.reasons) == 1 and c.reasons[0].startswith("RULE005")][:1]
        actions += ["Hold the truck safely until the next open compatible slot (coordinator decides)",
                    "Divert to another facility only with customer approval",
                    "Call the driver with the decision; do not let them queue at the gate meanwhile"]
        contact = self.db.execute(
            "SELECT * FROM facility_contacts WHERE facility_id=? AND active_flag=1 AND email IS NOT NULL AND email<>''"
            " ORDER BY CASE contact_role WHEN 'SHIFT_MANAGER' THEN 0 WHEN 'WAREHOUSE_COORDINATOR' THEN 1 ELSE 2 END"
            " LIMIT 1", (shp["destination_facility_id"],)).fetchone()
        deadline = max(now + 15, min(now + 30, eta["planning_eta"] - 30))
        record = {
            "case_id": f"ESC-{sid}-{hhmm(now).replace(':', '')}", "shipment_id": sid,
            "driver_id": shp["driver_id"], "facility_id": shp["destination_facility_id"], "reason": reason,
            "constraints": {"eta": hhmm(eta["eta"]), "planning_eta": hhmm(eta["planning_eta"]),
                            "eta_confidence": eta["confidence"], "weight_kg": shp["load_weight_kg"],
                            "required_dock_type": shp["required_dock_type"],
                            "temperature_control": bool(shp["temperature_control_required"]),
                            "unload_min": shp["expected_unload_min"],
                            "not_before": hhmm(eta["not_before"]) if eta.get("not_before") is not None else None,
                            "finish_by": hhmm(eta["finish_by"]) if eta.get("finish_by") is not None else None},
            "options_checked": len(checks), "blocking_rules": blocking, "recommended_actions": actions,
            "deadline": hhmm(deadline),
            "owner": f"{contact['contact_name']} ({contact['contact_role']})" if contact else "Shift coordinator",
            "channel": f"PHONE {contact['phone']}" if contact else "Coordinator queue",
            "delivery_status": "QUEUED",
        }
        record["missing_fields"] = [f for f in HANDOFF_FIELDS if record.get(f) in (None, "", [])]
        return record

    def triage(self, payload: dict[str, Any], deterministic: dict[str, Any], *, driver: bool) -> dict[str, Any]:
        """Advisory triage. The live agent only annotates; deterministic rules decide the outcome."""
        source, raw, error = "deterministic", deterministic, None
        if self.agent is not None:
            try:
                raw, source = self.agent(AgentRole.TRIAGE, payload), "agent"
            except NVIDIAClientError as exc:
                error = str(exc)
        guarded, violations = guard_answer(raw, None, payload)
        if driver and guarded.get("verification") == "VERIFIED":
            violations.append("driver report cannot be VERIFIED without independent confirmation")
            guarded["verification"] = "UNVERIFIED"
        return {"source": source, "answer": guarded, "violations": violations, "agent_error": error}

    # ----- messages -------------------------------------------------------
    def post_message(self, body: dict[str, Any]) -> tuple[int, dict[str, Any]]:
        for key in ("external_id", "sender", "text"):
            if not isinstance(body.get(key), str) or not body[key].strip():
                raise ApiError(400, f"{key} is required")
        with self.lock:
            result = self.message_graph.invoke({"body": body})
            return 200, result["response"]

    def _process_message(self, body: dict[str, Any], *, force_emergency: bool = False) -> dict[str, Any]:
        now = parse_now(body.get("now"))
        ext, sender, text = body["external_id"], body["sender"].strip(), body["text"].strip()
        thread_id = body.get("thread_id")
        prior = self.db.execute("SELECT response FROM service_messages WHERE external_id=?", (ext,)).fetchone()
        if prior:
            return {**json.loads(prior["response"]), "duplicate": True}
        self.tick(now)
        is_driver = self._is_driver(sender)
        contact = self.db.execute("SELECT * FROM facility_contacts WHERE contact_id=?", (sender,)).fetchone()
        if not is_driver and contact is None and not sender.startswith("FAC-"):
            raise ApiError(400, f"unknown sender {sender}")
        thread_key = thread_id or f"DIRECT-{sender}"
        state = self.db.execute("SELECT * FROM service_threads WHERE thread_key=?", (thread_key,)).fetchone()
        state = dict(state) if state and state["sender"] == sender else {}
        if is_driver:
            response = self._driver_message(
                now, ext, sender, thread_id, text, state, emergency=force_emergency)
        else:
            response = self._facility_message(now, ext, sender, thread_id, text, contact)
        response.update({"external_id": ext, "thread_id": thread_key, "duplicate": False, "holds_created": 0})
        self.db.execute("INSERT INTO service_messages VALUES (?,?,?,?,?)",
                        (ext, thread_key, sender, now, json.dumps(response)))
        self.audit(now, sender, "MESSAGE_PROCESSED",
                   {"external_id": ext, "thread": thread_key, "status": response["status"],
                    "shipment_id": response.get("shipment_id")})
        return response

    def _link(self, sender: str, thread_id: str | None, text: str,
              state: dict[str, Any]) -> tuple[str | None, str, list[dict[str, Any]]]:
        cands = [dict(r) for r in self.db.execute(
            f"SELECT shipment_id, order_reference, current_status FROM shipments WHERE driver_id=? AND current_status"
            f" NOT IN ({','.join('?' * len(INACTIVE_SHIPMENTS))}) ORDER BY original_eta_ts",
            (sender, *INACTIVE_SHIPMENTS))]
        ids = {c["shipment_id"] for c in cands}
        named = [c["shipment_id"] for c in cands if c["shipment_id"] in _SHIPMENT_ID.findall(text)
                 or c["order_reference"] in text
                 or re.search(rf"(?<![\d:]){c['order_reference'][-3:]}(?![\d:])", text)]
        if len(named) == 1:
            return named[0], "named in message", cands
        if state.get("shipment_id") in ids:
            return state["shipment_id"], "earlier in this thread", cands
        if thread_id:
            row = self.db.execute("SELECT driver_id, shipment_id FROM chat_threads WHERE thread_id=?",
                                  (thread_id,)).fetchone()
            if row and row["driver_id"] == sender and row["shipment_id"] in ids:
                return row["shipment_id"], f"chat thread {thread_id}", cands
        if len(cands) == 1:
            return cands[0]["shipment_id"], "driver's only active shipment", cands
        return None, "ambiguous" if cands else "no active shipment", cands

    def _save_thread(self, key: str, sender: str, now: int, **fields: Any) -> None:
        self.db.execute(
            "INSERT INTO service_threads(thread_key, sender, shipment_id, awaiting, pending_text, not_before, finish_by,"
            " offered_options, updated_min)"
            " VALUES (:k,:s,:shipment_id,:awaiting,:pending_text,:not_before,:finish_by,:offered_options,:now)"
            " ON CONFLICT(thread_key) DO UPDATE SET shipment_id=:shipment_id, awaiting=:awaiting,"
            " pending_text=:pending_text, not_before=:not_before, finish_by=:finish_by,"
            " offered_options=:offered_options, updated_min=:now",
            {"k": key, "s": sender, "now": now, "shipment_id": None, "awaiting": None, "pending_text": None,
             "not_before": None, "finish_by": None, "offered_options": None, **fields})

    def _driver_message(self, now: int, ext: str, sender: str, thread_id: str | None, text: str,
                        state: dict[str, Any], *, emergency: bool = False) -> dict[str, Any]:
        thread_key = thread_id or f"DIRECT-{sender}"
        sid, linked_by, cands = self._link(sender, thread_id, text, state)
        message = {"thread_id": thread_key, "sender": sender, "text": text, "sent_at": hhmm(now),
                   "external_id": ext}
        nb, fb, eta_text = parse_constraints(text)
        not_before = nb if nb is not None else state.get("not_before")
        finish_by = fb if fb is not None else state.get("finish_by")
        pending = " ".join(filter(None, [state.get("pending_text"), text]))

        if emergency:
            return self._safety_escalation(now, sender, thread_key, sid, text, cands, linked_by)

        if sid is None:
            cards = [shipment_card(self.engine, c["shipment_id"]) for c in cands]
            if cands:
                listing = " or ".join(
                    f"{c['order_reference']}" + (f" (slot {c['current_appointment']['start']})"
                                                 if c["current_appointment"] else "") for c in cards)
                question = f"You have {len(cands)} loads today: {listing}. Which one is this message about?"
                ask = "DRIVER"
            else:
                question, ask = "No active shipment is linked to you; a coordinator will contact you.", "COORDINATOR"
            triage = self.triage({"message": message, "candidate_shipments": cards, "independent_confirmation": []},
                                 self._answer("CLARIFY", ask, "UNVERIFIED", question, "shipment not identified"),
                                 driver=True)
            self._save_thread(thread_key, sender, now, awaiting="shipment", pending_text=pending,
                              not_before=not_before, finish_by=finish_by)
            return {"status": "CLARIFY", "shipment_id": None, "linked_by": linked_by, "missing": ["shipment_id"],
                    "question": question, "ask_whom": ask, "reply": question, "options": [], "escalation": None,
                    "candidates": [c["shipment_id"] for c in cands], "triage": triage}

        selected = self._select_offered_option(
            now, ext, sender, thread_key, sid, text, state, not_before, finish_by)
        if selected is not None:
            return selected

        prev = self.latest_eta(sid)
        history = [(prev["eta"], prev["confidence"])]
        revised = revise_eta(eta_text, now, history)
        if revised["planning_eta"] is None and state.get("pending_text"):
            revised = revise_eta(parse_constraints(state["pending_text"])[2], now, history)
        card = shipment_card(self.engine, sid)
        if revised["planning_eta"] is None:
            question = ("What clock time will you reach the gate (for example "
                        f"{hhmm(min(prev['eta'] + 60, 23 * 60))}), and is there a time by which you must leave?")
            triage = self.triage({"message": message, "linked": card, "independent_confirmation": []},
                                 self._answer("CLARIFY", "DRIVER", "UNVERIFIED", question, revised["rule"]),
                                 driver=True)
            self._save_thread(thread_key, sender, now, shipment_id=sid, awaiting="eta", pending_text=None,
                              not_before=not_before, finish_by=finish_by)
            return {"status": "CLARIFY", "shipment_id": sid, "linked_by": linked_by, "missing": ["eta"],
                    "eta": revised, "question": question, "ask_whom": "DRIVER", "reply": question, "options": [],
                    "escalation": None, "triage": triage}

        self.db.execute(
            "INSERT INTO service_etas(shipment_id, reported_min, eta_min, planning_eta_min, confidence, source,"
            " external_id, not_before, finish_by) VALUES (?,?,?,?,?,?,?,?,?)",
            (sid, now, revised["eta"], revised["planning_eta"], revised["confidence"], "DRIVER_CHAT", ext,
             not_before, finish_by))
        self.audit(now, sender, "ETA_REVISED", {"shipment_id": sid, "external_id": ext, **revised,
                                                "not_before": not_before, "finish_by": finish_by})
        self._save_thread(thread_key, sender, now, shipment_id=sid, not_before=not_before, finish_by=finish_by)
        eta = {**revised, "not_before": not_before, "finish_by": finish_by}
        payload = {"message": message, "linked": {**card, "planning_eta": hhmm(revised["planning_eta"])},
                   "eta_rule": revised["rule"], "independent_confirmation": []}
        result = self._plan(now, sid, eta, linked_by, payload, actor=sender, persist=True)
        result["eta"] = revised
        return result

    def _safety_escalation(self, now: int, sender: str, thread_key: str, sid: str | None, text: str,
                           candidates: list[dict[str, Any]], linked_by: str) -> dict[str, Any]:
        record: dict[str, Any]
        if sid:
            eta = self.latest_eta(sid)
            checks = self.engine.check(
                sid, eta["planning_eta"], now=now, not_before=eta.get("not_before"),
                finish_by=eta.get("finish_by"))
            record = self.escalation(sid, eta, checks, now, f"Potential driver safety emergency: {text[:160]}")
            self.db.execute("INSERT OR REPLACE INTO service_escalations VALUES (?,?,?,?)",
                            (record["case_id"], sid, now, json.dumps(record)))
            self.audit(now, sender, "SAFETY_ESCALATED",
                       {"case_id": record["case_id"], "shipment_id": sid, "reason": record["reason"]})
        else:
            case_id = f"SAFETY-{sender}-{now}"
            record = {"case_id": case_id, "shipment_id": None, "reason": f"Potential driver safety emergency: {text[:160]}",
                      "candidates": [item["shipment_id"] for item in candidates], "owner": "SetuHaul coordinator",
                      "channel": "DISPATCH", "delivery_status": "QUEUED"}
            self.audit(now, sender, "SAFETY_ESCALATED", {"case_id": case_id, "reason": record["reason"]})

        self._save_thread(thread_key, sender, now, shipment_id=sid)
        reply = ("Safety concern recorded for escalation. Move to a safe location if you can and contact local "
                 "emergency services and SetuHaul dispatch immediately. Do not proceed to the dock.")
        payload = {"message": {"sender": sender, "text": text, "sent_at": hhmm(now)},
                   "linked": shipment_card(self.engine, sid) if sid else None,
                   "independent_confirmation": []}
        triage = self.triage(
            payload, self._answer("ESCALATE", "COORDINATOR", "UNVERIFIED", None, "driver safety emergency"),
            driver=True)
        return {"status": "ESCALATED", "shipment_id": sid, "linked_by": linked_by, "options": [],
                "question": None, "reply": reply, "escalation": record, "triage": triage}

    def _select_offered_option(self, now: int, ext: str, sender: str, thread_key: str, sid: str, text: str,
                               state: dict[str, Any], not_before: int | None,
                               finish_by: int | None) -> dict[str, Any] | None:
        offered = state.get("offered_options")
        if not offered:
            return None
        options = json.loads(offered)
        match = re.fullmatch(r"\s*(?:option\s*)?([1-3])(?:\s*(?:please|thanks|thank you))?[.!]?\s*",
                            text, re.IGNORECASE)
        accepts = re.fullmatch(r"\s*(?:yes|yeah|yep|okay|ok|please)\s*[.!]?\s*", text, re.IGNORECASE)
        if match:
            choice = int(match.group(1))
        elif accepts and len(options) == 1:
            choice = 1
        elif accepts:
            question = f"Which option should I hold? Please reply with a number from 1 to {len(options)}."
            self._save_thread(thread_key, sender, now, shipment_id=sid, not_before=not_before,
                              finish_by=finish_by, offered_options=offered,
                              awaiting="option")
            return {"status": "CLARIFY", "shipment_id": sid, "options": options, "question": question,
                    "reply": question, "ask_whom": "DRIVER", "escalation": None}
        else:
            return None

        if choice > len(options):
            question = f"That option wasn't offered. Please choose a number from 1 to {len(options)}."
            return {"status": "CLARIFY", "shipment_id": sid, "options": options, "question": question,
                    "reply": question, "ask_whom": "DRIVER", "escalation": None}

        eta = self.latest_eta(sid)
        if not_before is not None:
            eta["not_before"] = not_before
        if finish_by is not None:
            eta["finish_by"] = finish_by
        checks, fresh_options = self.options(
            sid, eta["planning_eta"], now, not_before=eta.get("not_before"), finish_by=eta.get("finish_by"))
        slot_id = options[choice - 1]["slot_id"]
        current = next((check for check in checks if check.slot_ids[0] == slot_id and check.feasible), None)
        if current is None:
            refreshed = self._plan(
                now, sid, eta, "previously offered option", {"message": {"thread_id": thread_key,
                "sender": sender, "text": text}, "linked": shipment_card(self.engine, sid),
                "independent_confirmation": []}, actor=sender, persist=True)
            refreshed["reply"] = "That option is no longer available. " + refreshed["reply"]
            return refreshed

        ok, group = self.engine.hold(
            sid, current, eta["planning_eta"], now=now,
            not_before=eta.get("not_before"), finish_by=eta.get("finish_by"))
        if not ok:
            refreshed = self._plan(
                now, sid, eta, "previously offered option", {"message": {"thread_id": thread_key,
                "sender": sender, "text": text}, "linked": shipment_card(self.engine, sid),
                "independent_confirmation": []}, actor=sender, persist=True)
            refreshed["reply"] = "That option was taken before I could hold it. " + refreshed["reply"]
            return refreshed

        self.db.execute(
            "INSERT INTO service_holds(group_key, shipment_id, eta_min, created_min, not_before, finish_by, eta_id)"
            " VALUES (?,?,?,?,?,?,?)",
            (group, sid, eta["planning_eta"], now, eta.get("not_before"), eta.get("finish_by"),
             self.latest_eta_id(sid)))
        self.audit(now, sender, "HOLD_CREATED",
                   {"group": group, "shipment_id": sid, "slot_ids": current.slot_ids, "source": "driver option"})
        status, detail = self.engine.request(sid, group, now=now, request_key=f"driver-message:{ext}")
        if status != "REQUESTED":
            raise ApiError(409, f"could not request selected option: {detail}", status=status)
        self.audit(now, sender, "HOLD_REQUESTED",
                   {"group": group, "shipment_id": sid, "request_key": f"driver-message:{ext}"})
        self._save_thread(thread_key, sender, now, shipment_id=sid, not_before=eta.get("not_before"),
                          finish_by=eta.get("finish_by"))
        reply = (f"Option {choice} is held at {current.dock} {hhmm(current.start)} and marked as requested for "
                 "warehouse approval. It is not confirmed until the warehouse accepts.")
        return {"status": "REQUESTED", "shipment_id": sid, "group": group, "slot_ids": current.slot_ids,
                "dock": current.dock, "start": hhmm(current.start), "unload_ends": hhmm(current.end),
                "options": fresh_options, "question": None, "reply": reply, "escalation": None}

    def _plan(self, now: int, sid: str, eta: dict[str, Any], linked_by: str, payload: dict[str, Any], *,
              actor: str, persist: bool) -> dict[str, Any]:
        checks, opts = self.options(sid, eta["planning_eta"], now, not_before=eta.get("not_before"),
                                    finish_by=eta.get("finish_by"))
        if opts:
            reply = ("These are options, not bookings: " + "; ".join(
                f"{o['option']}) {o['dock']} {o['start']} (unload done {o['unload_ends']})" for o in opts)
                + ". Your current booking stays as it is until you pick one; reply with the option number and I'll "
                  f"hold it for {HOLD_TTL_MIN} minutes while I ask the warehouse.")
            if persist:
                message = payload.get("message", {})
                self._save_thread(
                    message.get("thread_id", f"DIRECT-{actor}"), message.get("sender", actor), now,
                    shipment_id=sid, not_before=eta.get("not_before"), finish_by=eta.get("finish_by"),
                    offered_options=json.dumps(opts))
            triage = self.triage(payload, self._answer("PROPOSE", "NONE", "UNVERIFIED", None,
                                                       "shipment and clock-time ETA known"), driver=True)
            return {"status": "OPTIONS", "shipment_id": sid, "linked_by": linked_by, "options": opts,
                    "options_checked": len(checks), "question": None, "reply": reply, "escalation": None,
                    "triage": triage}
        record = self.escalation(sid, eta, checks, now,
                                 f"No feasible slot for planning ETA {hhmm(eta['planning_eta'])}")
        if persist:
            message = payload.get("message", {})
            self._save_thread(message.get("thread_id", f"DIRECT-{actor}"), message.get("sender", actor), now,
                              shipment_id=sid, not_before=eta.get("not_before"), finish_by=eta.get("finish_by"))
            self.db.execute("INSERT OR IGNORE INTO service_escalations VALUES (?,?,?,?)",
                            (record["case_id"], sid, now, json.dumps(record)))
            self.audit(now, actor, "ESCALATED", {"case_id": record["case_id"], "shipment_id": sid,
                                                 "owner": record["owner"], "channel": record["channel"]})
        reply = (f"I can't find a safe slot for your load from {hhmm(eta['planning_eta'])}. I've recorded an "
                 f"escalation for {record['owner'].split(' (')[0]}. Contact SetuHaul dispatch to coordinate "
                 f"before {record['deadline']}; please don't come to the gate before a coordinator confirms.")
        triage = self.triage(payload, self._answer("ESCALATE", "COORDINATOR", "UNVERIFIED", None,
                                                   "no feasible slot"), driver=True)
        return {"status": "ESCALATED", "shipment_id": sid, "linked_by": linked_by, "options": [],
                "options_checked": len(checks), "question": None, "reply": reply, "escalation": record,
                "triage": triage}

    def _facility_message(self, now: int, ext: str, sender: str, thread_id: str | None, text: str,
                          contact: sqlite3.Row | None) -> dict[str, Any]:
        facility = contact["facility_id"] if contact else sender
        ids = [r["shipment_id"] for r in self.db.execute(
            "SELECT shipment_id, order_reference FROM shipments WHERE destination_facility_id=?", (facility,))
            if r["shipment_id"] in text or r["order_reference"] in text]
        sid = ids[0] if len(ids) == 1 else None
        payload = {"message": {"source": "FACILITY", "id": ext, "sender": sender, "text": text},
                   "linked": shipment_card(self.engine, sid) if sid else None,
                   "independent_confirmation": [f"facility message {ext}"]}
        triage = self.triage(payload, self._answer(
            "NO_ACTION", "COORDINATOR" if sid else "NONE", "VERIFIED", None,
            "facility report recorded; coordinator applies any booking or dock change"), driver=False)
        return {"status": "ACKNOWLEDGED", "shipment_id": sid, "linked_by": "named in facility message" if sid else None,
                "options": [], "question": None, "escalation": None,
                "reply": "Recorded. A coordinator will apply any booking or dock change after review.",
                "triage": triage}

    @staticmethod
    def _answer(action: str, ask: str, verification: str, question: str | None, reason: str) -> dict[str, Any]:
        return {"verification": verification, "action": action, "ask_whom": ask, "selected_id": None,
                "state_change_allowed": False, "extracted": None, "evidence": [], "question": question,
                "reason": reason}

    # ----- options, holds and appointments --------------------------------
    def get_options(self, sid: str, query: dict[str, str]) -> tuple[int, dict[str, Any]]:
        now = parse_now(query.get("now"))
        with self.lock:
            self.shipment(sid)
            self.tick(now)
            eta = self.latest_eta(sid)
            if query.get("eta"):
                eta.update(eta=parse_now(query["eta"]), planning_eta=parse_now(query["eta"]), confidence="REQUESTED")
            for key in ("not_before", "finish_by"):
                if query.get(key):
                    eta[key] = parse_now(query[key])
            payload = {"linked": shipment_card(self.engine, sid)}
            result = self._plan(now, sid, eta, "shipment id", payload, actor="API", persist=False)
            result.pop("triage")
            result["planning_eta"] = hhmm(eta["planning_eta"])
            result["holds_created"] = 0
            return 200, result

    def _held_slots(self, sid: str) -> list[str]:
        return [r["group_key"] for r in self.db.execute(
            "SELECT DISTINCT group_key FROM slot_holds WHERE shipment_id=? AND state='HELD'", (sid,))]

    def post_hold(self, body: dict[str, Any]) -> tuple[int, dict[str, Any]]:
        now = parse_now(body.get("now"))
        sid, slot_id = body.get("shipment_id"), body.get("slot_id")
        if not isinstance(sid, str) or not isinstance(slot_id, str):
            raise ApiError(400, "shipment_id and slot_id are required")
        actor = body.get("actor", "DRIVER")
        with self.lock:
            self.shipment(sid)
            self.tick(now)
            eta = self.latest_eta(sid)
            if body.get("eta"):
                eta.update(planning_eta=parse_now(body["eta"]), not_before=None, finish_by=None)
            planning = eta["planning_eta"]
            checks = self.engine.check(sid, planning, now=now, not_before=eta.get("not_before"),
                                       finish_by=eta.get("finish_by"))
            option = next((c for c in checks if c.slot_ids[0] == slot_id), None)
            reasons = option.reasons if option else [f"{slot_id} is not a slot start for {sid}"]
            if option is None or not option.feasible:
                self.audit(now, actor, "HOLD_REJECTED", {"shipment_id": sid, "slot_id": slot_id, "reasons": reasons})
                raise ApiError(409, "slot not available", status="REJECTED", reasons=reasons)
            replaced = self._held_slots(sid)
            ok, detail = self.engine.hold(
                sid, option, planning, now=now, not_before=eta.get("not_before"), finish_by=eta.get("finish_by"))
            if not ok:
                self.audit(now, actor, "HOLD_REJECTED", {"shipment_id": sid, "slot_id": slot_id, "reasons": [detail]})
                raise ApiError(409, "slot not available", status="REJECTED", reasons=[detail])
            self.db.execute(
                "INSERT OR REPLACE INTO service_holds(group_key, shipment_id, eta_min, created_min,"
                " not_before, finish_by, eta_id) VALUES (?,?,?,?,?,?,?)",
                (detail, sid, planning, now, eta.get("not_before"), eta.get("finish_by"), self.latest_eta_id(sid)))
            self.audit(now, actor, "HOLD_CREATED", {"group": detail, "shipment_id": sid, "slot_ids": option.slot_ids,
                                                    "expires": hhmm(now + HOLD_TTL_MIN), "replaced": replaced})
            return 200, {"status": "HELD", "group": detail, "shipment_id": sid, "slot_ids": option.slot_ids,
                         "dock": option.dock, "start": hhmm(option.start), "unload_ends": hhmm(option.end),
                         "expires_at": hhmm(now + HOLD_TTL_MIN), "released_previous_holds": replaced,
                         "reply": f"I'm holding {option.dock} {hhmm(option.start)} for {HOLD_TTL_MIN} minutes. "
                                  "It is not confirmed until the warehouse accepts."}

    def _group(self, group: str) -> list[sqlite3.Row]:
        rows = self.db.execute("SELECT * FROM slot_holds WHERE group_key=? ORDER BY hold_id", (group,)).fetchall()
        if not rows:
            raise ApiError(404, f"unknown hold {group}")
        return rows

    def request_hold(self, group: str, body: dict[str, Any]) -> tuple[int, dict[str, Any]]:
        now = parse_now(body.get("now"))
        key = body.get("request_key")
        if not isinstance(key, str) or not key:
            raise ApiError(400, "request_key is required")
        with self.lock:
            self.tick(now)
            sid = self._group(group)[0]["shipment_id"]
            status, detail = self.engine.request(sid, group, now=now, request_key=key)
            if status == "DUPLICATE":
                return 200, {"status": status, "group": group, "detail": detail}
            if status == "KEY_REUSED":
                raise ApiError(409, detail, status=status)
            self.audit(now, body.get("actor", "DRIVER"), f"HOLD_{status}",
                       {"group": group, "shipment_id": sid, "request_key": key, "detail": detail})
            if status == "STALE":
                raise ApiError(409, "hold is no longer valid", status=status, detail=detail,
                               next_step=f"GET /shipments/{sid}/options for fresh options")
            return 200, {"status": status, "group": group, "shipment_id": sid,
                         "reply": "Requested from the warehouse; not confirmed until they accept."}

    def confirm_hold(self, group: str, body: dict[str, Any]) -> tuple[int, dict[str, Any]]:
        now = parse_now(body.get("now"))
        ref = body.get("warehouse_ref")
        if not isinstance(ref, str) or not ref:
            raise ApiError(400, "warehouse_ref is required")
        actor = body.get("actor", "FACILITY")
        with self.lock:
            self.tick(now)
            rows = self._group(group)
            sid, first = rows[0]["shipment_id"], rows[0]["slot_id"]
            if all(r["state"] == "CONFIRMED" for r in rows):
                apt = self.db.execute("SELECT appointment_id FROM appointments WHERE shipment_id=? AND slot_id=?"
                                      " AND is_current=1", (sid, first)).fetchone()
                return 200, {"status": "ALREADY_CONFIRMED", "group": group,
                             "appointment_id": apt["appointment_id"] if apt else None}
            meta = self.db.execute("SELECT * FROM service_holds WHERE group_key=?", (group,)).fetchone()
            latest = self.db.execute(
                "SELECT planning_eta_min, not_before, finish_by FROM service_etas"
                " WHERE shipment_id=? AND eta_id>? ORDER BY eta_id DESC LIMIT 1",
                (sid, meta["eta_id"] if meta else 0)).fetchone()
            current_eta = self.latest_eta(sid)
            if latest:
                eta = latest["planning_eta_min"]
                not_before, finish_by = latest["not_before"], latest["finish_by"]
            elif meta:
                eta = meta["eta_min"]
                not_before, finish_by = meta["not_before"], meta["finish_by"]
            else:
                eta = current_eta["planning_eta"]
                not_before, finish_by = current_eta.get("not_before"), current_eta.get("finish_by")
            old = self.db.execute("SELECT appointment_id, slot_id FROM appointments WHERE shipment_id=? AND is_current=1",
                                  (sid,)).fetchone()
            ok, detail = self.engine.confirm(
                sid, group, eta, now=now, warehouse_ref=ref, not_before=not_before, finish_by=finish_by)
            if not ok:
                self.audit(now, actor, "CONFIRM_REJECTED", {"group": group, "shipment_id": sid, "reason": detail})
                raise ApiError(409, "confirmation rejected after re-check", status="REJECTED", reasons=[detail])
            if old:
                self.engine.release_confirmed_holds(sid, old["slot_id"])
            self.audit(now, actor, "APPOINTMENT_CONFIRMED",
                       {"group": group, "shipment_id": sid, "appointment_id": detail, "warehouse_ref": ref,
                        "replaced_appointment_id": old["appointment_id"] if old else None})
            return 200, {"status": "CONFIRMED", "group": group, "shipment_id": sid, "appointment_id": detail,
                         "replaced_appointment_id": old["appointment_id"] if old else None,
                         "reply": f"Confirmed, warehouse ref {ref}."}

    def release_hold(self, group: str, body: dict[str, Any]) -> tuple[int, dict[str, Any]]:
        now = parse_now(body.get("now"))
        with self.lock:
            self.tick(now)
            rows = self._group(group)
            changed = self.engine.release_hold(group, now)
            if changed:
                self.audit(now, body.get("actor", "DRIVER"), "HOLD_RELEASED",
                           {"group": group, "shipment_id": rows[0]["shipment_id"], "rows": changed})
                return 200, {"status": "RELEASED", "group": group}
            state = self._group(group)[0]["state"]
            if state == "CONFIRMED":
                raise ApiError(409, "hold is confirmed; release the appointment instead", status=state)
            return 200, {"status": "ALREADY_RELEASED", "group": group, "state": state}

    def release_appointment(self, apt_id: str, body: dict[str, Any]) -> tuple[int, dict[str, Any]]:
        now = parse_now(body.get("now"))
        reason = body.get("reason")
        if not isinstance(reason, str) or not reason:
            raise ApiError(400, "reason is required")
        with self.lock:
            self.tick(now)
            row = self.db.execute("SELECT * FROM appointments WHERE appointment_id=?", (apt_id,)).fetchone()
            if row is None:
                raise ApiError(404, f"unknown appointment {apt_id}")
            if not self.engine.release_appointment(apt_id, now=now, reason=reason):
                return 200, {"status": "ALREADY_RELEASED", "appointment_id": apt_id,
                             "appointment_status": row["appointment_status"]}
            freed = self.engine.release_confirmed_holds(row["shipment_id"], row["slot_id"])
            self.audit(now, body.get("actor", "COORDINATOR"), "APPOINTMENT_RELEASED",
                       {"appointment_id": apt_id, "shipment_id": row["shipment_id"], "slot_id": row["slot_id"],
                        "reason": reason, "holds_freed": freed})
            return 200, {"status": "RELEASED", "appointment_id": apt_id, "slot_id": row["slot_id"]}

    def board(self, query: dict[str, str]) -> tuple[int, dict[str, Any]]:
        now = parse_now(query.get("now"))
        with self.lock:
            self.tick(now)
            return 200, {"now": hhmm(now), "board": self.engine.board(now)}

    def integrity(self) -> tuple[int, dict[str, Any]]:
        with self.lock:
            conflicts = self.engine.conflicts()
            blocking = {key: value for key, value in conflicts.items()
                        if key != "preexisting_appointment_overlaps"}
            return 200, {"ok": not any(blocking.values()), "conflicts": conflicts}

    def audit_log(self, query: dict[str, str]) -> tuple[int, dict[str, Any]]:
        limit = int(query.get("limit", "200"))
        with self.lock:
            rows = [dict(r) for r in self.db.execute(
                "SELECT * FROM service_audit_log ORDER BY audit_id DESC LIMIT ?", (limit,))]
        for r in rows:
            r["payload"] = json.loads(r["payload"])
        return 200, {"entries": rows[::-1]}

    def health(self) -> tuple[int, dict[str, Any]]:
        return 200, {"status": "ok", "db": self.db_path, "live_agent": self.agent is not None}

    def deployment_status(self) -> tuple[int, dict[str, Any]]:
        health = self.health()[1]
        return 200, {
            "health": "HEALTHY" if health["status"] == "ok" else "UNHEALTHY",
            "runtime": "Kubernetes" if os.environ.get("KUBERNETES_SERVICE_HOST") else "Local",
            "pod_name": os.environ.get("POD_NAME"),
            "namespace": os.environ.get("POD_NAMESPACE"),
            "target_cluster": os.environ.get("EKS_CLUSTER_NAME"),
            "database_mode": "File-backed SQLite" if health["db"] != ":memory:" else "In-memory SQLite",
            "live_agent": health["live_agent"],
            "rollout_status": "NOT_OBSERVED",
            "cluster_api_status": "NOT_CONNECTED",
        }

    def activity(self, query: dict[str, str]) -> tuple[int, dict[str, Any]]:
        now = parse_now(query.get("now", "10:30"))
        with self.lock:
            board = self.engine.board(now)
            docks = []
            for dock in self.engine.docks():
                slots = [slot for slot in self.engine.slots() if slot["dock_id"] == dock["dock_id"]]
                events = self.engine.dock_events(dock["dock_id"])
                docks.append({
                    "dock": dock["dock_code"],
                    "type": dock["dock_type"],
                    "slots": [{
                        "slot_id": slot["slot_id"],
                        "start": to_minutes(slot["slot_start_ts"]),
                        "end": to_minutes(slot["slot_end_ts"]),
                        "state": board[slot["slot_id"]][0].rstrip("!"),
                        "shipment_id": board[slot["slot_id"]][1] or None,
                    } for slot in slots],
                    "events": [{
                        "type": event["event_type"],
                        "start": event["event_start_ts"][11:16],
                        "end": event["event_end_ts"][11:16] if event["event_end_ts"] else None,
                        "reason": event["reason"],
                    } for event in events],
                })

            shipments = []
            for row in self.db.execute(
                """SELECT s.shipment_id, s.order_reference, s.driver_id, s.current_status, s.priority_code,
                          s.destination_facility_id, s.required_dock_type, s.temperature_control_required,
                          s.expected_unload_min, a.appointment_id, a.appointment_status, a.slot_id,
                          d.dock_code, sl.slot_start_ts
                   FROM shipments s
                   LEFT JOIN appointments a ON a.shipment_id=s.shipment_id AND a.is_current=1
                     AND a.appointment_status IN ('PENDING_CONFIRMATION','CONFIRMED','IN_PROGRESS')
                   LEFT JOIN appointment_slots sl ON sl.slot_id=a.slot_id
                   LEFT JOIN docks d ON d.dock_id=sl.dock_id
                   WHERE s.destination_facility_id=? AND s.current_status NOT IN ('COMPLETED','CANCELLED')
                   ORDER BY CASE s.priority_code WHEN 'CRITICAL' THEN 0 WHEN 'HIGH' THEN 1 ELSE 2 END,
                            s.shipment_id""", (TRAINING_FACILITY_ID,)
            ):
                shipment = dict(row)
                eta = self.latest_eta(shipment["shipment_id"])
                shipment["eta"] = hhmm(eta["planning_eta"])
                shipment["eta_confidence"] = eta["confidence"]
                shipment["slot_start"] = (
                    shipment["slot_start_ts"][11:16] if shipment["slot_start_ts"] else None)
                shipment.pop("slot_start_ts", None)
                shipments.append(shipment)

            holds = [dict(row) for row in self.db.execute(
                """SELECT h.group_key, h.shipment_id, h.state, h.created_min, h.expires_min,
                          s.driver_id, s.priority_code, GROUP_CONCAT(h.slot_id) AS slot_ids
                   FROM slot_holds h JOIN shipments s USING (shipment_id)
                   WHERE h.state IN ('HELD','REQUESTED')
                   GROUP BY h.group_key ORDER BY h.created_min DESC""")]
            for hold in holds:
                hold["created_at"] = hhmm(hold.pop("created_min"))
                hold["expires_at"] = hhmm(hold.pop("expires_min")) if hold["expires_min"] is not None else None
                hold["slot_ids"] = (hold["slot_ids"] or "").split(",")

            activities = []
            for row in self.db.execute(
                "SELECT audit_id, sim_ts, actor, action, payload FROM service_audit_log"
                " ORDER BY audit_id DESC LIMIT 20"):
                item = dict(row)
                item["time"] = item.pop("sim_ts")
                item["payload"] = json.loads(item["payload"])
                activities.append(item)
            for row in self.db.execute(
                """SELECT m.chat_message_id, m.message_ts, m.sender_type, m.sender_reference,
                          m.parsed_intent, m.message_text, t.shipment_id
                   FROM chat_messages m JOIN chat_threads t USING (thread_id)
                   WHERE m.is_duplicate=0 AND substr(m.message_ts, 12, 5)<=?
                   ORDER BY m.message_ts DESC LIMIT 20""", (hhmm(now),)):
                activities.append({
                    "audit_id": f"seed-{row['chat_message_id']}",
                    "time": row["message_ts"][11:16],
                    "actor": row["sender_reference"] or row["sender_type"],
                    "action": row["parsed_intent"] or row["sender_type"],
                    "source": "seed transcript",
                    "payload": {"shipment_id": row["shipment_id"], "summary": row["message_text"]},
                })
            activities.sort(key=lambda item: (item["time"], str(item["audit_id"])), reverse=True)

            counts = {
                "inbound": len(shipments),
                "in_transit": sum(item["current_status"] == "IN_TRANSIT" for item in shipments),
                "at_gate": sum(item["current_status"] in {"AT_GATE", "WAITING", "IN_DOCK"} for item in shipments),
                "booked": sum(item["appointment_status"] in {"CONFIRMED", "IN_PROGRESS"} for item in shipments),
                "pending": sum(item["appointment_status"] == "PENDING_CONFIRMATION" for item in shipments),
                "alerts": sum(item["priority_code"] == "CRITICAL" for item in shipments)
                    + sum(item["state"] == "BLOCKED" for dock in docks for item in dock["slots"]),
            }
            return 200, {
                "now": hhmm(now),
                "facility": "FAC-JAI-01",
                "mode": "SIMULATION",
                "counts": counts,
                "docks": docks,
                "shipments": shipments,
                "holds": holds,
                "activity": activities,
                "training_agents": TRAINING_AGENTS,
            }

    def training_agent(self, body: dict[str, Any]) -> tuple[int, dict[str, Any]]:
        role = body.get("role")
        agent_id = body.get("agent_id")
        if agent_id is None:
            agent_id = (
                {"driver": "driver_01", "facility": "facility"}.get(role, role)
                if isinstance(role, str) else role
            )
        message = body.get("message")
        if not isinstance(agent_id, str) or agent_id not in TRAINING_AGENTS:
            raise ApiError(400, "agent_id must identify one of the four drivers or the facility agent")
        profile = TRAINING_AGENTS[agent_id]
        if role is not None and (
            not isinstance(role, str) or role not in {"driver", "facility"} or role != profile["role"]
        ):
            raise ApiError(400, "role must match the selected training agent")
        if not isinstance(message, str) or not message.strip():
            raise ApiError(400, "message is required")
        try:
            return 200, training_agent_respond(agent_id, message)
        except ValueError as exc:
            raise ApiError(400, str(exc)) from exc


# ---------------------------------------------------------------------------
# HTTP layer
# ---------------------------------------------------------------------------
ROUTES = [
    ("POST", re.compile(r"/messages"), lambda s, m, b, q: s.post_message(b)),
    ("POST", re.compile(r"/training/agent"), lambda s, m, b, q: s.training_agent(b)),
    ("POST", re.compile(r"/holds"), lambda s, m, b, q: s.post_hold(b)),
    ("POST", re.compile(r"/holds/([^/]+)/request"), lambda s, m, b, q: s.request_hold(m[0], b)),
    ("POST", re.compile(r"/holds/([^/]+)/confirm"), lambda s, m, b, q: s.confirm_hold(m[0], b)),
    ("POST", re.compile(r"/holds/([^/]+)/release"), lambda s, m, b, q: s.release_hold(m[0], b)),
    ("POST", re.compile(r"/appointments/([^/]+)/release"), lambda s, m, b, q: s.release_appointment(m[0], b)),
    ("GET", re.compile(r"/shipments/([^/]+)/options"), lambda s, m, b, q: s.get_options(m[0], q)),
    ("GET", re.compile(r"/board"), lambda s, m, b, q: s.board(q)),
    ("GET", re.compile(r"/health"), lambda s, m, b, q: s.health()),
    ("GET", re.compile(r"/deployment/status"), lambda s, m, b, q: s.deployment_status()),
    ("GET", re.compile(r"/integrity"), lambda s, m, b, q: s.integrity()),
    ("GET", re.compile(r"/activity"), lambda s, m, b, q: s.activity(q)),
    ("GET", re.compile(r"/audit"), lambda s, m, b, q: s.audit_log(q)),
]


def make_handler(service: Service) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        server_version = "SetuHaulService/1.0"

        def _serve_dashboard(self) -> None:
            page = Path(__file__).with_name("dashboard.html").read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(page)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(page)

        def _serve_deployment_dashboard(self) -> None:
            page = Path(__file__).with_name("deployment_dashboard.html").read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(page)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(page)

        def _dispatch(self, method: str) -> None:
            url = urlparse(self.path)
            query = {k: v[-1] for k, v in parse_qs(url.query).items()}
            try:
                body: dict[str, Any] = {}
                if method == "POST":
                    raw = self.rfile.read(int(self.headers.get("Content-Length") or 0))
                    body = json.loads(raw or b"{}")
                    if not isinstance(body, dict):
                        raise ApiError(400, "body must be a JSON object")
                for verb, pattern, handler in ROUTES:
                    match = pattern.fullmatch(url.path.rstrip("/") or "/")
                    if verb == method and match:
                        status, payload = handler(service, [unquote(g) for g in match.groups()], body, query)
                        break
                else:
                    raise ApiError(404, f"no route {method} {url.path}")
            except ApiError as exc:
                status, payload = exc.http_status, exc.body
            except json.JSONDecodeError:
                status, payload = 400, {"error": "invalid JSON"}
            except (sqlite3.OperationalError, ValueError) as exc:
                status, payload = 503 if isinstance(exc, sqlite3.OperationalError) else 400, {"error": str(exc)}
            except Exception as exc:  # never drop the connection without a JSON answer
                status, payload = 500, {"error": f"{type(exc).__name__}: {exc}"}
            finally:
                with service.lock:
                    if service.db.in_transaction:
                        service.db.execute("ROLLBACK")
            data = json.dumps(payload).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def do_GET(self) -> None:  # noqa: N802
            path = urlparse(self.path).path
            if path in {"/", "/dashboard"}:
                try:
                    self._serve_dashboard()
                except OSError:
                    self.send_error(500, "dashboard asset unavailable")
                return
            if path == "/deployment":
                try:
                    self._serve_deployment_dashboard()
                except OSError:
                    self.send_error(500, "deployment dashboard asset unavailable")
                return
            self._dispatch("GET")

        def do_POST(self) -> None:  # noqa: N802
            self._dispatch("POST")

        def log_message(self, fmt: str, *args: Any) -> None:
            pass

    return Handler


def make_server(service: Service, host: str = "127.0.0.1", port: int = 8080) -> ThreadingHTTPServer:
    return ThreadingHTTPServer((host, port), make_handler(service))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--port", type=int, default=8080)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--db", default=":memory:", help="SQLite file (seeded if new); default in-memory seed")
    parser.add_argument("--live", action="store_true", help="use the NVIDIA triage agent when NVIDIA_API_KEY is set")
    args = parser.parse_args()
    agent = None
    if args.live:
        load_nvidia_env()
        if os.environ.get("NVIDIA_API_KEY"):
            agent = ask_agent
        else:
            print("--live ignored: NVIDIA_API_KEY not set; using deterministic triage")
    server = make_server(Service(args.db, agent=agent), args.host, args.port)
    print(f"SetuHaul service on http://{args.host}:{server.server_port} (db={args.db}, live={agent is not None})")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
