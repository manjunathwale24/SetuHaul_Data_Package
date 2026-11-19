"""Deterministic slot engine: feasibility, holds, requests, confirmations and idempotency.

The LLM agents only advise. Every state change goes through this module, which
re-checks feasibility inside a write transaction so a stale option can never be
committed. Times are minutes since midnight on 4 Aug 2026 (Asia/Kolkata).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
import sqlite3
import tempfile
import threading
from typing import Any
from uuid import uuid4

from guards import to_minutes
from workflow_simulation import DEFAULT_SEED, apply_view_fixes


FACILITY = "FAC-JAI-01"
DAY = "2026-08-04"
HOLD_TTL_MIN = 10
REQUEST_TTL_MIN = 15
HARD_EVENTS = ("BREAKDOWN", "MAINTENANCE")
ACTIVE_APPOINTMENTS = ("CONFIRMED", "PENDING_CONFIRMATION", "IN_PROGRESS")
PRIORITY_RANK = {"CRITICAL": 0, "HIGH": 1, "NORMAL": 2, "LOW": 3}

HOLDS_SCHEMA = """
CREATE TABLE IF NOT EXISTS slot_holds (
    hold_id INTEGER PRIMARY KEY,
    slot_id TEXT NOT NULL REFERENCES appointment_slots(slot_id),
    shipment_id TEXT NOT NULL REFERENCES shipments(shipment_id),
    group_key TEXT NOT NULL,
    state TEXT NOT NULL CHECK (state IN ('HELD','REQUESTED','CONFIRMED','RELEASED','EXPIRED')),
    created_min INTEGER NOT NULL,
    expires_min INTEGER,
    request_key TEXT
);
CREATE UNIQUE INDEX IF NOT EXISTS ux_live_hold_per_slot
    ON slot_holds(slot_id) WHERE state IN ('HELD','REQUESTED','CONFIRMED');
CREATE TABLE IF NOT EXISTS processed_messages (
    idempotency_key TEXT PRIMARY KEY,
    first_seen_min INTEGER NOT NULL,
    outcome TEXT NOT NULL,
    request_fingerprint TEXT NOT NULL DEFAULT ''
);
"""


def hhmm(minutes: int) -> str:
    return f"{minutes // 60:02d}:{minutes % 60:02d}"


def ts(minutes: int) -> str:
    return f"{DAY}T{hhmm(minutes)}:00+05:30"


@dataclass
class SlotCheck:
    slot_ids: list[str]
    dock: str
    start: int
    end: int
    reasons: list[str] = field(default_factory=list)

    @property
    def feasible(self) -> bool:
        return not self.reasons

    def as_dict(self) -> dict[str, Any]:
        return {
            "id": self.slot_ids[0],
            "slot_ids": self.slot_ids,
            "dock": self.dock,
            "start": hhmm(self.start),
            "end": hhmm(self.end),
            "feasible": self.feasible,
            "reasons": self.reasons,
        }


class SlotEngine:
    def __init__(self, db_path: str = ":memory:", seed_path: Path = DEFAULT_SEED, *, seed: bool = True):
        self.db = sqlite3.connect(db_path, isolation_level=None, check_same_thread=False, timeout=10)
        self.db.row_factory = sqlite3.Row
        self.db.create_function("to_minutes", 1, to_minutes, deterministic=True)
        if seed:
            self.db.executescript(seed_path.read_text(encoding="utf-8"))
            apply_view_fixes(self.db)
        self.db.executescript(HOLDS_SCHEMA)
        columns = {row["name"] for row in self.db.execute("PRAGMA table_info(processed_messages)")}
        if "request_fingerprint" not in columns:
            self.db.execute(
                "ALTER TABLE processed_messages ADD COLUMN request_fingerprint TEXT NOT NULL DEFAULT ''")
        self.db.execute("PRAGMA foreign_keys = ON")
        self.rechecks_failed = 0

    # ----- reference data -------------------------------------------------
    def shipment(self, shipment_id: str) -> dict[str, Any]:
        row = self.db.execute(
            """SELECT s.*, v.capacity_kg AS vehicle_capacity_kg, v.vehicle_type_code
               FROM shipments s JOIN vehicles v USING (vehicle_id) WHERE shipment_id = ?""",
            (shipment_id,),
        ).fetchone()
        if row is None:
            raise KeyError(shipment_id)
        return dict(row)

    def rule(self, rule_type: str) -> str | None:
        row = self.db.execute(
            "SELECT rule_value FROM facility_rules WHERE facility_id=? AND rule_type=? AND active_flag=1",
            (FACILITY, rule_type),
        ).fetchone()
        return row["rule_value"] if row else None

    def docks(self) -> list[dict[str, Any]]:
        return [dict(r) for r in self.db.execute(
            "SELECT * FROM docks WHERE facility_id=? ORDER BY dock_code", (FACILITY,))]

    def slots(self) -> list[dict[str, Any]]:
        return [dict(r) for r in self.db.execute(
            """SELECT sl.*, d.dock_code, d.dock_type, d.supports_refrigerated, d.max_vehicle_weight_kg
               FROM appointment_slots sl JOIN docks d USING (dock_id)
               WHERE sl.facility_id=? ORDER BY d.dock_code, sl.slot_start_ts""", (FACILITY,))]

    def dock_events(self, dock_id: str) -> list[dict[str, Any]]:
        return [dict(r) for r in self.db.execute(
            "SELECT * FROM dock_status_events WHERE dock_id=?", (dock_id,))]

    # ----- occupancy ------------------------------------------------------
    def occupant(self, slot_id: str, *, now: int) -> tuple[str, str] | None:
        """(state, shipment_id) of whatever currently occupies a slot, or None."""
        row = self.db.execute(
            f"""SELECT appointment_status, shipment_id FROM appointments
                WHERE slot_id=? AND is_current=1 AND appointment_status IN
                ({",".join("?" * len(ACTIVE_APPOINTMENTS))})""",
            (slot_id, *ACTIVE_APPOINTMENTS),
        ).fetchone()
        if row:
            return row["appointment_status"], row["shipment_id"]
        self.expire_holds(now)
        row = self.db.execute(
            "SELECT state, shipment_id FROM slot_holds WHERE slot_id=? AND state IN ('HELD','REQUESTED','CONFIRMED')",
            (slot_id,),
        ).fetchone()
        return (f"HOLD_{row['state']}", row["shipment_id"]) if row else None

    def overrun_claims(self) -> dict[str, tuple[str, str]]:
        """Next slot(s) on the same dock implicitly used by a booked unload longer than its slot."""
        claims: dict[str, tuple[str, str]] = {}
        rows = self.db.execute(
            f"""SELECT a.appointment_id, a.shipment_id, s.expected_unload_min, sl.dock_id,
                       sl.slot_start_ts, sl.slot_end_ts
                FROM appointments a JOIN shipments s USING (shipment_id)
                JOIN appointment_slots sl USING (slot_id)
                WHERE a.is_current=1 AND a.appointment_status IN
                ({",".join("?" * len(ACTIVE_APPOINTMENTS))})""", ACTIVE_APPOINTMENTS).fetchall()
        for row in rows:
            finish = to_minutes(row["slot_start_ts"]) + row["expected_unload_min"]
            if finish <= to_minutes(row["slot_end_ts"]):
                continue
            for nxt in self.db.execute(
                    "SELECT slot_id, slot_start_ts FROM appointment_slots WHERE dock_id=? AND slot_start_ts>=?"
                    " ORDER BY slot_start_ts", (row["dock_id"], row["slot_end_ts"])):
                if to_minutes(nxt["slot_start_ts"]) >= finish:
                    break
                claims[nxt["slot_id"]] = (row["appointment_id"], row["shipment_id"])
        return claims

    def expire_holds(self, now: int) -> int:
        cur = self.db.execute(
            "UPDATE slot_holds SET state='EXPIRED' WHERE state IN ('HELD','REQUESTED') AND expires_min <= ?",
            (now,))
        return cur.rowcount

    # ----- feasibility ----------------------------------------------------
    def check(self, shipment_id: str, eta: int, *, now: int, not_before: int | None = None,
              finish_by: int | None = None) -> list[SlotCheck]:
        """Every slot start with the reasons it is or is not feasible for this shipment.

        A load whose unload is longer than one slot needs the following slot(s) on the
        same dock as well, so long unloads never silently overrun into the next truck.
        """
        shp = self.shipment(shipment_id)
        last_start = to_minutes(self.rule("LAST_NEW_START_TIME") or "23:59")
        heavy_kg = int(self.rule("HEAVY_DOCK_REQUIRED_KG") or 10**9)
        by_dock: dict[str, list[dict[str, Any]]] = {}
        for slot in self.slots():
            by_dock.setdefault(slot["dock_id"], []).append(slot)
        overruns = self.overrun_claims()
        checks = []
        for dock_id, dock_slots in by_dock.items():
            events = self.dock_events(dock_id)
            for i, slot in enumerate(dock_slots):
                start, end = to_minutes(slot["slot_start_ts"]), to_minutes(slot["slot_end_ts"])
                chain = [slot]
                while to_minutes(chain[-1]["slot_end_ts"]) - start < shp["expected_unload_min"]:
                    nxt = i + len(chain)
                    if nxt >= len(dock_slots):
                        break
                    chain.append(dock_slots[nxt])
                finish = start + shp["expected_unload_min"]
                reasons: list[str] = []
                if shp["required_dock_type"] != slot["dock_type"]:
                    reasons.append(f"needs {shp['required_dock_type']} dock, {slot['dock_code']} is {slot['dock_type']}")
                if shp["temperature_control_required"] and not slot["supports_refrigerated"]:
                    reasons.append("RULE003 reefer load needs D5")
                if shp["load_weight_kg"] > heavy_kg and slot["dock_type"] != "HEAVY":
                    reasons.append(f"RULE004 {shp['load_weight_kg']} kg > {heavy_kg} kg needs heavy dock")
                if shp["load_weight_kg"] > slot["max_vehicle_weight_kg"]:
                    reasons.append(f"{shp['load_weight_kg']} kg > {slot['dock_code']} limit {slot['max_vehicle_weight_kg']} kg")
                if start < eta:
                    reasons.append(f"starts {hhmm(start)} before ETA {hhmm(eta)}")
                if start < now:
                    reasons.append("already started")
                if not_before is not None and start < not_before:
                    reasons.append(f"before requested {hhmm(not_before)}")
                if start >= last_start:
                    reasons.append(f"RULE005 no new start at/after {hhmm(last_start)} without approval")
                if finish_by is not None and finish > finish_by:
                    reasons.append(f"unload ends {hhmm(finish)} after driver limit {hhmm(finish_by)}")
                if to_minutes(chain[-1]["slot_end_ts"]) - start < shp["expected_unload_min"]:
                    reasons.append("no following slot for a long unload")
                for part in chain:
                    p_start, p_end = to_minutes(part["slot_start_ts"]), to_minutes(part["slot_end_ts"])
                    if part["slot_status"] != "OPEN":
                        reasons.append(f"{part['slot_id']} {part['slot_status'].lower()}: {part['block_reason']}")
                        continue
                    for ev in events:
                        if ev["event_type"] in HARD_EVENTS and to_minutes(ev["event_start_ts"]) < p_end \
                                and to_minutes(ev["event_end_ts"]) > p_start:
                            reasons.append(f"{ev['dock_event_id']} {ev['event_type'].lower()} on {part['dock_code']}")
                    occ = self.occupant(part["slot_id"], now=now)
                    if occ and occ[1] != shipment_id:
                        reasons.append(f"{part['slot_id']} taken ({occ[0].lower()} {occ[1]})")
                    elif part["slot_id"] in overruns and overruns[part["slot_id"]][1] != shipment_id:
                        apt, other = overruns[part["slot_id"]]
                        reasons.append(f"{part['slot_id']} needed by {apt} ({other}) long unload")
                checks.append(SlotCheck([p["slot_id"] for p in chain], slot["dock_code"], start, finish,
                                        list(dict.fromkeys(reasons))))
        checks.sort(key=lambda c: (c.start, c.dock))
        return checks

    def feasible(self, shipment_id: str, eta: int, *, now: int, **kw: Any) -> list[SlotCheck]:
        return [c for c in self.check(shipment_id, eta, now=now, **kw) if c.feasible]

    # ----- state machine: OPTION -> HELD -> REQUESTED -> CONFIRMED --------
    def hold(self, shipment_id: str, option: SlotCheck, eta: int, *, now: int,
             ttl: int = HOLD_TTL_MIN, not_before: int | None = None,
             finish_by: int | None = None) -> tuple[bool, str]:
        """Atomically re-check and hold every slot in an option. Returns (ok, detail)."""
        group = f"{shipment_id}@{option.slot_ids[0]}@{now}@{uuid4().hex[:8]}"
        self.db.execute("BEGIN IMMEDIATE")
        try:
            self.expire_holds(now)
            fresh = {c.slot_ids[0]: c for c in self.check(
                shipment_id, eta, now=now, not_before=not_before, finish_by=finish_by)}
            current = fresh.get(option.slot_ids[0])
            if current is None or not current.feasible:
                self.db.execute("ROLLBACK")
                self.rechecks_failed += 1
                return False, "; ".join(current.reasons if current else ["slot no longer exists"])
            self.db.execute(
                "UPDATE slot_holds SET state='RELEASED' WHERE shipment_id=? AND state='HELD'", (shipment_id,))
            for slot_id in current.slot_ids:
                self.db.execute(
                    "INSERT INTO slot_holds(slot_id, shipment_id, group_key, state, created_min, expires_min)"
                    " VALUES (?,?,?,?,?,?)", (slot_id, shipment_id, group, "HELD", now, now + ttl))
            self.db.execute("COMMIT")
            return True, group
        except sqlite3.IntegrityError as exc:
            self.db.execute("ROLLBACK")
            return False, f"constraint rejected hold: {exc}"

    def request(self, shipment_id: str, group: str, *, now: int, request_key: str) -> tuple[str, str]:
        """Driver accepted a held option. Idempotent on request_key. Returns (status, detail)."""
        self.db.execute("BEGIN IMMEDIATE")
        try:
            fingerprint = f"{shipment_id}\0{group}"
            prior = self.db.execute(
                "SELECT outcome, request_fingerprint FROM processed_messages WHERE idempotency_key=?",
                (request_key,)).fetchone()
            if prior:
                self.db.execute("COMMIT")
                if prior["request_fingerprint"] and prior["request_fingerprint"] != fingerprint:
                    return "KEY_REUSED", "request_key was already used for a different hold"
                return "DUPLICATE", "same request already processed"

            self.expire_holds(now)
            rows = self.db.execute(
                "SELECT * FROM slot_holds WHERE group_key=? AND shipment_id=? ORDER BY hold_id",
                (group, shipment_id)).fetchall()
            if not rows or any(r["state"] != "HELD" for r in rows):
                self.rechecks_failed += 1
                state = rows[0]["state"] if rows else "MISSING"
                self.db.execute(
                    "INSERT INTO processed_messages VALUES (?,?,?,?)",
                    (request_key, now, "STALE", fingerprint))
                self.db.execute("COMMIT")
                return "STALE", f"hold is {state}; option must be re-checked"

            cur = self.db.execute(
                "UPDATE slot_holds SET state='REQUESTED', expires_min=?, request_key=?"
                " WHERE group_key=? AND shipment_id=? AND state='HELD'",
                (now + REQUEST_TTL_MIN, request_key, group, shipment_id))
            if cur.rowcount != len(rows):
                self.db.execute("ROLLBACK")
                return "STALE", "hold changed while request was being processed"
            self.db.execute(
                "INSERT INTO processed_messages VALUES (?,?,?,?)",
                (request_key, now, "REQUESTED", fingerprint))
            self.db.execute("COMMIT")
            return "REQUESTED", rows[0]["slot_id"]
        except Exception:
            if self.db.in_transaction:
                self.db.execute("ROLLBACK")
            raise

    def confirm(self, shipment_id: str, group: str, eta: int, *, now: int, warehouse_ref: str,
                not_before: int | None = None, finish_by: int | None = None) -> tuple[bool, str]:
        """Facility accepted. Re-check feasibility, then replace the current appointment atomically."""
        self.db.execute("BEGIN IMMEDIATE")
        try:
            self.expire_holds(now)
            rows = self.db.execute(
                "SELECT * FROM slot_holds WHERE group_key=? AND shipment_id=? AND state='REQUESTED'"
                " ORDER BY hold_id", (group, shipment_id)).fetchall()
            if not rows:
                self.db.execute("COMMIT")
                return False, "no requested hold"
            first = rows[0]["slot_id"]
            check = {c.slot_ids[0]: c for c in self.check(
                shipment_id, eta, now=now, not_before=not_before, finish_by=finish_by)}.get(first)
            if check is None or not check.feasible:
                self.db.execute("UPDATE slot_holds SET state='RELEASED' WHERE group_key=?", (group,))
                self.db.execute("COMMIT")
                self.rechecks_failed += 1
                return False, "; ".join(check.reasons if check else ["slot missing"])
            old = self.db.execute(
                "SELECT appointment_id FROM appointments WHERE shipment_id=? AND is_current=1",
                (shipment_id,)).fetchone()
            if old:
                self.db.execute(
                    "UPDATE appointments SET appointment_status='CANCELLED', is_current=0, cancelled_at=?,"
                    " cancellation_reason='Replaced after driver exception', updated_at=? WHERE appointment_id=?",
                    (ts(now), ts(now), old["appointment_id"]))
            new_id = f"APT-NEW-{shipment_id[-4:]}-{now}-{uuid4().hex[:8]}"
            self.db.execute(
                "INSERT INTO appointments(appointment_id, shipment_id, slot_id, appointment_status, booking_source,"
                " is_current, booked_at, confirmed_at, replaced_appointment_id, warehouse_confirmation_ref, updated_at)"
                " VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (new_id, shipment_id, first, "CONFIRMED", "DRIVER_CHAT", 1, ts(now), ts(now),
                 old["appointment_id"] if old else None, warehouse_ref, ts(now)))
            changed = self.db.execute(
                "UPDATE slot_holds SET state='CONFIRMED' WHERE group_key=? AND shipment_id=? AND state='REQUESTED'",
                (group, shipment_id))
            if changed.rowcount != len(rows):
                self.db.execute("ROLLBACK")
                return False, "hold changed while confirmation was being processed"
            self.db.execute("COMMIT")
            return True, new_id
        except sqlite3.IntegrityError as exc:
            self.db.execute("ROLLBACK")
            return False, f"constraint rejected confirmation: {exc}"

    def release_appointment(self, appointment_id: str, *, now: int, reason: str) -> bool:
        cur = self.db.execute(
            "UPDATE appointments SET appointment_status='CANCELLED', is_current=0, cancelled_at=?,"
            " cancellation_reason=?, updated_at=? WHERE appointment_id=? AND is_current=1",
            (ts(now), reason, ts(now), appointment_id))
        return cur.rowcount == 1

    def release_hold(self, group: str, now: int) -> int:
        """Release a HELD/REQUESTED hold group. Idempotent: returns how many rows changed."""
        self.expire_holds(now)
        cur = self.db.execute(
            "UPDATE slot_holds SET state='RELEASED' WHERE group_key=? AND state IN ('HELD','REQUESTED')", (group,))
        return cur.rowcount

    def release_confirmed_holds(self, shipment_id: str, slot_id: str) -> int:
        """Free the CONFIRMED hold group behind a released appointment so the slot shows as free."""
        cur = self.db.execute(
            "UPDATE slot_holds SET state='RELEASED' WHERE state='CONFIRMED' AND shipment_id=? AND group_key IN"
            " (SELECT group_key FROM slot_holds WHERE slot_id=? AND shipment_id=?)", (shipment_id, slot_id, shipment_id))
        return cur.rowcount

    # ----- idempotency ----------------------------------------------------
    def seen(self, key: str) -> bool:
        return self.db.execute(
            "SELECT 1 FROM processed_messages WHERE idempotency_key=?", (key,)).fetchone() is not None

    def remember(self, key: str, now: int, outcome: str) -> bool:
        cur = self.db.execute(
            "INSERT OR IGNORE INTO processed_messages(idempotency_key, first_seen_min, outcome)"
            " VALUES (?,?,?)", (key, now, outcome))
        return cur.rowcount == 1

    # ----- integrity and views -------------------------------------------
    def conflicts(self) -> dict[str, int]:
        q = self.db.execute
        overlaps = q(
            """SELECT
                   SUM(CASE WHEN a.appointment_id LIKE 'APT-NEW-%' OR b.appointment_id LIKE 'APT-NEW-%'
                            THEN 1 ELSE 0 END) AS created,
                   SUM(CASE WHEN a.appointment_id NOT LIKE 'APT-NEW-%'
                                  AND b.appointment_id NOT LIKE 'APT-NEW-%'
                            THEN 1 ELSE 0 END) AS existing
               FROM appointments a
               JOIN appointments b ON a.appointment_id < b.appointment_id
               JOIN appointment_slots sa ON sa.slot_id=a.slot_id
               JOIN appointment_slots sb ON sb.slot_id=b.slot_id AND sb.dock_id=sa.dock_id
               JOIN shipments sha ON sha.shipment_id=a.shipment_id
               JOIN shipments shb ON shb.shipment_id=b.shipment_id
               WHERE a.is_current=1 AND b.is_current=1
                 AND a.appointment_status IN (?, ?, ?) AND b.appointment_status IN (?, ?, ?)
                 AND a.shipment_id!=b.shipment_id
                 AND to_minutes(sa.slot_start_ts) < to_minutes(sb.slot_start_ts) + shb.expected_unload_min
                 AND to_minutes(sb.slot_start_ts) < to_minutes(sa.slot_start_ts) + sha.expected_unload_min""",
            (*ACTIVE_APPOINTMENTS, *ACTIVE_APPOINTMENTS)).fetchone()
        return {
            "slots_with_two_live_claims": q(
                f"""SELECT COUNT(*) FROM (SELECT slot_id FROM (
                      SELECT slot_id, shipment_id FROM appointments WHERE is_current=1 AND appointment_status IN
                        ({",".join("?" * len(ACTIVE_APPOINTMENTS))})
                      UNION SELECT slot_id, shipment_id FROM slot_holds WHERE state IN ('HELD','REQUESTED'))
                    GROUP BY slot_id HAVING COUNT(DISTINCT shipment_id) > 1)""", ACTIVE_APPOINTMENTS).fetchone()[0],
            "shipments_with_two_current": q(
                "SELECT COUNT(*) FROM (SELECT shipment_id FROM appointments WHERE is_current=1 "
                "GROUP BY shipment_id HAVING COUNT(*) > 1)").fetchone()[0],
            "new_appointment_overlaps": overlaps["created"] or 0,
            "preexisting_appointment_overlaps": overlaps["existing"] or 0,
            "foreign_key_errors": len(q("PRAGMA foreign_key_check").fetchall()),
        }

    def board(self, now: int) -> dict[str, list[str]]:
        """slot_id -> [state, shipment] for the dock board in the HTML report."""
        out = {}
        overruns = self.overrun_claims()
        for slot in self.slots():
            start, end = to_minutes(slot["slot_start_ts"]), to_minutes(slot["slot_end_ts"])
            blocked = slot["slot_status"] != "OPEN" or any(
                ev["event_type"] in HARD_EVENTS and to_minutes(ev["event_start_ts"]) < end
                and to_minutes(ev["event_end_ts"]) > start for ev in self.dock_events(slot["dock_id"]))
            occ = self.occupant(slot["slot_id"], now=now)
            if occ:
                state = {"CONFIRMED": "BOOKED", "IN_PROGRESS": "WIP", "PENDING_CONFIRMATION": "PENDING",
                         "HOLD_HELD": "HELD", "HOLD_REQUESTED": "REQUESTED",
                         "HOLD_CONFIRMED": "BOOKED"}[occ[0]]
                out[slot["slot_id"]] = [state + ("!" if blocked else ""), occ[1]]
            elif slot["slot_id"] in overruns:
                out[slot["slot_id"]] = ["OVERRUN" + ("!" if blocked else ""), overruns[slot["slot_id"]][1]]
            else:
                out[slot["slot_id"]] = ["BLOCKED" if blocked else "FREE", ""]
        return out


def race_for_slot(slot_id: str, shipment_ids: list[str], eta: int, now: int) -> dict[str, Any]:
    """Integrity proof: several threads with their own connections try to hold one slot at once."""
    with tempfile.TemporaryDirectory() as tmp:
        path = str(Path(tmp) / "race.db")
        SlotEngine(path).db.close()
        barrier = threading.Barrier(len(shipment_ids))
        results: dict[str, str] = {}

        def attempt(shipment_id: str) -> None:
            engine = SlotEngine(path, seed=False)
            option = next(c for c in engine.check(shipment_id, eta, now=now) if c.slot_ids[0] == slot_id)
            barrier.wait()
            for _ in range(50):
                try:
                    ok, detail = engine.hold(shipment_id, option, eta, now=now)
                    break
                except sqlite3.OperationalError:  # database is locked: retry like a real client
                    threading.Event().wait(0.01)
            else:
                ok, detail = False, "gave up after lock retries"
            results[shipment_id] = "WON" if ok else f"REJECTED ({detail})"
            engine.db.close()

        threads = [threading.Thread(target=attempt, args=(s,)) for s in shipment_ids]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        check = SlotEngine(path, seed=False)
        live = check.db.execute(
            "SELECT COUNT(DISTINCT shipment_id) FROM slot_holds WHERE slot_id=? AND state='HELD'",
            (slot_id,)).fetchone()[0]
        check.db.close()
    return {"slot_id": slot_id, "contenders": shipment_ids, "results": results,
            "winners": sum(v == "WON" for v in results.values()), "live_holds_after": live}
