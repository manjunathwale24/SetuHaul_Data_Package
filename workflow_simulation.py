"""Deterministic, in-memory simulation of SetuHaul exception triage."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
import sqlite3


ROOT = Path(__file__).resolve().parent
DEFAULT_SEED = ROOT / "setuhaul_schema_and_seed.sql"
@dataclass
class DriverEtaReport:
    report_id: int
    shipment_id: str
    eta_ts: str
    confidence: str
    status: str = "UNVERIFIED"


@dataclass
class DockOutageReport:
    report_id: int
    dock_id: str
    start_ts: str
    end_ts: str | None
    status: str = "UNVERIFIED"


class WorkflowSimulation:
    """Runs triage and slot-feasibility scenarios without changing the shipped DB."""

    def __init__(self, seed_path: Path = DEFAULT_SEED) -> None:
        self.db = sqlite3.connect(":memory:")
        self.db.row_factory = sqlite3.Row
        self.db.executescript(seed_path.read_text(encoding="utf-8"))
        self.db.execute("PRAGMA foreign_keys = ON")
        self._eta_reports: dict[int, DriverEtaReport] = {}
        self._outage_reports: dict[int, DockOutageReport] = {}
        self._next_report_id = 1

    def close(self) -> None:
        self.db.close()

    def effective_eta(self, shipment_id: str) -> str:
        row = self.db.execute(
            "SELECT effective_eta_ts FROM v_latest_eta WHERE shipment_id = ?",
            (shipment_id,),
        ).fetchone()
        if row is None:
            raise ValueError(f"Unknown shipment: {shipment_id}")
        return row["effective_eta_ts"]

    def triage_thread(self, thread_id: str) -> str:
        thread = self.db.execute(
            "SELECT shipment_id FROM chat_threads WHERE thread_id = ?",
            (thread_id,),
        ).fetchone()
        if thread is None:
            raise ValueError(f"Unknown thread: {thread_id}")

        messages = self.db.execute(
            """
            SELECT sender_type, is_duplicate, requires_human_review
            FROM chat_messages
            WHERE thread_id = ?
            """,
            (thread_id,),
        ).fetchall()
        if thread["shipment_id"] is None or any(
            row["requires_human_review"] for row in messages
        ):
            return "HUMAN_REVIEW"

        driver_messages = [row for row in messages if row["sender_type"] == "DRIVER"]
        if driver_messages and all(row["is_duplicate"] for row in driver_messages):
            return "DUPLICATE"

        needs_information = self.db.execute(
            """
            SELECT 1 FROM driver_exceptions
            WHERE thread_id = ? AND exception_status = 'NEEDS_INFORMATION'
            LIMIT 1
            """,
            (thread_id,),
        ).fetchone()
        if needs_information:
            return "NEEDS_FOLLOW_UP"
        return "READY_FOR_VALIDATION"

    def report_driver_eta(
        self, shipment_id: str, eta_ts: str, confidence: str
    ) -> DriverEtaReport:
        self._parse_timestamp(eta_ts)
        if confidence not in {"LOW", "MEDIUM", "HIGH"}:
            raise ValueError(f"Invalid confidence code: {confidence}")
        if not self.db.execute(
            "SELECT 1 FROM shipments WHERE shipment_id = ?", (shipment_id,)
        ).fetchone():
            raise ValueError(f"Unknown shipment: {shipment_id}")

        report = DriverEtaReport(
            self._allocate_report_id(), shipment_id, eta_ts, confidence
        )
        self._eta_reports[report.report_id] = report
        return report

    def report_dock_outage(
        self, dock_id: str, start_ts: str, end_ts: str | None
    ) -> DockOutageReport:
        start = self._parse_timestamp(start_ts)
        end = self._parse_timestamp(end_ts) if end_ts is not None else None
        if end is not None and end <= start:
            raise ValueError("Outage end must be after its start")
        if not self.db.execute(
            "SELECT 1 FROM docks WHERE dock_id = ?", (dock_id,)
        ).fetchone():
            raise ValueError(f"Unknown dock: {dock_id}")

        report = DockOutageReport(
            self._allocate_report_id(), dock_id, start_ts, end_ts
        )
        self._outage_reports[report.report_id] = report
        return report

    def verify_dock_outage(self, report_id: int, verifier: str) -> None:
        report = self._outage_reports[report_id]
        if verifier not in {"FACILITY", "OPERATIONS"}:
            raise ValueError("A dock outage requires facility or operations verification")
        if report.status != "UNVERIFIED":
            raise ValueError(f"Outage report is already {report.status}")

        self.db.execute(
            """
            INSERT INTO dock_status_events
                (dock_event_id, dock_id, event_type, event_start_ts, event_end_ts,
                 reason, created_at)
            VALUES (?, ?, 'BREAKDOWN', ?, ?, ?, ?)
            """,
            (
                f"SIM-DEVT-{report.report_id}",
                report.dock_id,
                report.start_ts,
                report.end_ts,
                f"Verified by {verifier} in workflow simulation",
                self._next_timestamp("dock_status_events", "created_at"),
            ),
        )
        report.status = "VERIFIED"

    def impacted_appointments(self, report_id: int) -> list[dict[str, str]]:
        report = self._outage_reports[report_id]
        if report.status != "VERIFIED":
            raise ValueError("An outage must be verified before checking its impact")

        rows = self.db.execute(
            """
            SELECT a.appointment_id, a.shipment_id, a.appointment_status,
                   sl.slot_start_ts, sl.slot_end_ts
            FROM appointments a
            JOIN appointment_slots sl USING (slot_id)
            WHERE sl.dock_id = ?
              AND a.is_current = 1
              AND a.appointment_status IN
                    ('PENDING_CONFIRMATION', 'CONFIRMED', 'IN_PROGRESS')
              AND sl.slot_start_ts < COALESCE(?, '9999-12-31T23:59:59+05:30')
              AND (sl.slot_end_ts > ?)
            ORDER BY sl.slot_start_ts, a.appointment_id
            """,
            (report.dock_id, report.end_ts, report.start_ts),
        ).fetchall()
        return [
            {
                "appointment_id": row["appointment_id"],
                "shipment_id": row["shipment_id"],
                "appointment_status": row["appointment_status"],
                "slot_start_ts": row["slot_start_ts"],
                "slot_end_ts": row["slot_end_ts"],
                "overlap_start_ts": max(row["slot_start_ts"], report.start_ts),
                "overlap_end_ts": min(row["slot_end_ts"], report.end_ts)
                if report.end_ts is not None
                else row["slot_end_ts"],
            }
            for row in rows
        ]

    def slot_status(self, slot_id: str) -> str:
        slot = self.db.execute(
            """
            SELECT v.availability_status, sl.dock_id, d.dock_status,
                   sl.slot_start_ts, sl.slot_end_ts
            FROM v_slot_availability v
            JOIN appointment_slots sl USING (slot_id)
            JOIN docks d USING (dock_id)
            WHERE v.slot_id = ?
            """,
            (slot_id,),
        ).fetchone()
        if slot is None:
            raise ValueError(f"Unknown slot: {slot_id}")
        if slot["availability_status"] != "AVAILABLE":
            return slot["availability_status"]
        if slot["dock_status"] != "ACTIVE":
            return "BLOCKED"

        event = self.db.execute(
            """
            SELECT 1
            FROM dock_status_events
            WHERE dock_id = ?
              AND event_type IN ('BREAKDOWN', 'MAINTENANCE', 'MANUAL_BLOCK')
              AND event_start_ts < ?
              AND (event_end_ts IS NULL OR event_end_ts > ?)
            LIMIT 1
            """,
            (slot["dock_id"], slot["slot_end_ts"], slot["slot_start_ts"]),
        ).fetchone()
        return "BLOCKED" if event else "AVAILABLE"

    def provisional_slots(self, report_id: int) -> list[dict[str, object]]:
        report = self._eta_reports[report_id]
        shipment = self.db.execute(
            """
            SELECT destination_facility_id, required_dock_type,
                   temperature_control_required, load_weight_kg, expected_unload_min
            FROM shipments
            WHERE shipment_id = ?
            """,
            (report.shipment_id,),
        ).fetchone()

        rows = self.db.execute(
            """
            SELECT v.slot_id, v.dock_code, v.dock_type, v.supports_refrigerated,
                   v.max_vehicle_weight_kg, v.slot_start_ts, v.slot_end_ts
            FROM v_slot_availability v
            JOIN appointment_slots sl USING (slot_id)
            JOIN docks d USING (dock_code, facility_id)
            WHERE v.facility_id = ?
              AND v.availability_status = 'AVAILABLE'
              AND d.dock_status = 'ACTIVE'
              AND v.slot_start_ts >= ?
            ORDER BY v.slot_start_ts, v.dock_code
            """,
            (shipment["destination_facility_id"], report.eta_ts),
        ).fetchall()

        candidates: list[dict[str, object]] = []
        for row in rows:
            if shipment["required_dock_type"] not in {"ANY", row["dock_type"]}:
                continue
            if (
                shipment["temperature_control_required"]
                and not row["supports_refrigerated"]
            ):
                continue
            if shipment["load_weight_kg"] > row["max_vehicle_weight_kg"]:
                continue
            duration = self._parse_timestamp(row["slot_end_ts"]) - self._parse_timestamp(
                row["slot_start_ts"]
            )
            if duration < timedelta(minutes=shipment["expected_unload_min"]):
                continue
            if self.slot_status(row["slot_id"]) != "AVAILABLE":
                continue
            candidates.append(
                {
                    "slot_id": row["slot_id"],
                    "dock_code": row["dock_code"],
                    "slot_start_ts": row["slot_start_ts"],
                    "slot_end_ts": row["slot_end_ts"],
                    "recommendation_status": "PROVISIONAL",
                }
            )
        return candidates

    def try_second_active_booking(self, shipment_id: str, slot_id: str) -> bool:
        created_at = self._next_timestamp("appointments", "booked_at")
        try:
            self.db.execute(
                """
                INSERT INTO appointments
                    (appointment_id, shipment_id, slot_id, appointment_status,
                     booking_source, is_current, booked_at, updated_at)
                VALUES (?, ?, ?, 'PENDING_CONFIRMATION', 'SCHEDULING_TOOL', 1, ?, ?)
                """,
                (f"SIM-APT-{self._next_report_id}", shipment_id, slot_id, created_at, created_at),
            )
        except sqlite3.IntegrityError as exc:
            if "appointments.slot_id" in str(exc):
                return False
            raise
        return True

    def _allocate_report_id(self) -> int:
        report_id = self._next_report_id
        self._next_report_id += 1
        return report_id

    def _next_timestamp(self, table: str, column: str) -> str:
        latest = self.db.execute(
            f"SELECT MAX({column}) FROM {table}"
        ).fetchone()[0]
        if latest is None:
            return "2026-08-04T00:00:00+05:30"
        return (self._parse_timestamp(latest) + timedelta(seconds=1)).isoformat()

    @staticmethod
    def _parse_timestamp(value: str) -> datetime:
        try:
            parsed = datetime.fromisoformat(value)
        except ValueError as exc:
            raise ValueError(f"Invalid ISO-8601 timestamp: {value}") from exc
        if parsed.utcoffset() is None:
            raise ValueError("Timestamp must include a UTC offset")
        return parsed


VIEW_FIXES = """
DROP VIEW v_latest_eta;
CREATE VIEW v_latest_eta AS
WITH ranked AS (
    SELECT
        e.*,
        ROW_NUMBER() OVER (PARTITION BY shipment_id ORDER BY created_at DESC, eta_update_id DESC) AS rn
    FROM eta_updates e
    WHERE NOT (e.source_type = 'DRIVER_DECLARED' AND e.confidence_code = 'LOW')
)
SELECT
    s.shipment_id,
    s.original_eta_ts,
    COALESCE(r.declared_eta_ts, s.original_eta_ts) AS effective_eta_ts,
    COALESCE(r.source_type, 'ORIGINAL_PLAN') AS eta_source,
    COALESCE(r.confidence_code, 'HIGH') AS eta_confidence,
    r.delay_reason_code,
    r.note AS eta_note,
    r.created_at AS eta_updated_at
FROM shipments s
LEFT JOIN ranked r ON r.shipment_id = s.shipment_id AND r.rn = 1;
DROP VIEW v_slot_availability;
CREATE VIEW v_slot_availability AS
SELECT
    sl.slot_id,
    sl.facility_id,
    d.dock_code,
    d.dock_type,
    d.supports_refrigerated,
    d.max_vehicle_weight_kg,
    sl.slot_start_ts,
    sl.slot_end_ts,
    CASE
        WHEN sl.slot_status <> 'OPEN' THEN sl.slot_status
        WHEN a.appointment_id IS NOT NULL THEN 'OCCUPIED'
        WHEN d.dock_status <> 'ACTIVE' OR EXISTS (
            SELECT 1 FROM dock_status_events de
            WHERE de.dock_id = sl.dock_id
              AND de.event_type IN ('BREAKDOWN', 'MAINTENANCE', 'MANUAL_BLOCK')
              AND de.event_start_ts < sl.slot_end_ts
              AND (de.event_end_ts IS NULL OR de.event_end_ts > sl.slot_start_ts)
        ) THEN 'BLOCKED'
        ELSE 'AVAILABLE'
    END AS availability_status,
    a.appointment_id,
    a.shipment_id,
    a.appointment_status
FROM appointment_slots sl
JOIN docks d ON d.dock_id = sl.dock_id
LEFT JOIN appointments a
    ON a.slot_id = sl.slot_id
   AND a.appointment_status IN ('PENDING_CONFIRMATION','CONFIRMED','IN_PROGRESS');
"""


def apply_view_fixes(db: sqlite3.Connection) -> None:
    """Runtime fixes for two seed-view gaps; the shipped SQL and .db stay unchanged.

    v_latest_eta ignores LOW-confidence driver claims (and the seed's latest_eta_ts,
    which can mirror them); v_slot_availability blocks slots overlapping dock events.
    """
    db.executescript(VIEW_FIXES)


def main() -> None:
    simulation = WorkflowSimulation()
    try:
        for thread_id in ("THR001", "THR009", "THR010"):
            print(f"{thread_id}: {simulation.triage_thread(thread_id)}")

        eta = simulation.report_driver_eta(
            "SHP1006", "2026-08-04T11:20:00+05:30", "MEDIUM"
        )
        print(
            f"SHP1006 report={eta.status}; "
            f"authoritative ETA remains {simulation.effective_eta('SHP1006')}"
        )
        slots = simulation.provisional_slots(eta.report_id)
        print(f"SHP1006 provisional compatible slots: {len(slots)}")

        reefer = simulation.report_driver_eta(
            "SHP1015", "2026-08-04T18:30:00+05:30", "HIGH"
        )
        print(
            f"SHP1015 provisional compatible slots after ETA: "
            f"{len(simulation.provisional_slots(reefer.report_id))}"
        )
    finally:
        simulation.close()


if __name__ == "__main__":
    main()
