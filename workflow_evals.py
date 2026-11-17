"""Risk-weighted evals for the deterministic workflow simulation, not an LLM."""

from __future__ import annotations

from dataclasses import dataclass
import sys

from workflow_simulation import WorkflowSimulation


@dataclass(frozen=True)
class EvalResult:
    name: str
    points: int
    passed: bool
    detail: str


def _eval_unverified_eta(sim: WorkflowSimulation) -> tuple[bool, str]:
    before = sim.effective_eta("SHP1006")
    report = sim.report_driver_eta(
        "SHP1006", "2026-08-04T23:59:00+05:30", "HIGH"
    )
    after = sim.effective_eta("SHP1006")
    passed = report.status == "UNVERIFIED" and after == before
    return passed, f"report={report.status}; effective ETA unchanged={after == before}"


def _eval_outage_verification(sim: WorkflowSimulation) -> tuple[bool, str]:
    eta = sim.report_driver_eta("SHP1006", "2026-08-04T11:20:00+05:30", "MEDIUM")
    slot = sim.provisional_slots(eta.report_id)[0]
    dock_id = sim.db.execute(
        "SELECT dock_id FROM appointment_slots WHERE slot_id = ?",
        (slot["slot_id"],),
    ).fetchone()["dock_id"]
    outage = sim.report_dock_outage(
        dock_id, slot["slot_start_ts"], slot["slot_end_ts"]
    )

    before = sim.slot_status(slot["slot_id"])
    try:
        sim.verify_dock_outage(outage.report_id, "DRIVER")
        driver_rejected = False
    except ValueError:
        driver_rejected = True
    remains_open = sim.slot_status(slot["slot_id"]) == "AVAILABLE"
    sim.verify_dock_outage(outage.report_id, "OPERATIONS")
    blocked_after_verification = sim.slot_status(slot["slot_id"]) == "BLOCKED"
    passed = (
        before == "AVAILABLE"
        and driver_rejected
        and remains_open
        and blocked_after_verification
    )
    return passed, (
        f"unverified={before}; driver verification rejected={driver_rejected}; "
        f"verified outage blocks slot={blocked_after_verification}"
    )


def _eval_booked_appointment_impact(sim: WorkflowSimulation) -> tuple[bool, str]:
    outage = sim.report_dock_outage(
        "DOCK-JAI-D3",
        "2026-08-04T09:15:00+05:30",
        "2026-08-04T13:00:00+05:30",
    )
    try:
        sim.impacted_appointments(outage.report_id)
        unverified_rejected = False
    except ValueError:
        unverified_rejected = True
    sim.verify_dock_outage(outage.report_id, "OPERATIONS")
    impacted = sim.impacted_appointments(outage.report_id)
    appointment = next(
        (item for item in impacted if item["appointment_id"] == "APT1005"), None
    )
    passed = (
        unverified_rejected
        and appointment is not None
        and appointment["overlap_start_ts"] == "2026-08-04T09:15:00+05:30"
        and appointment["overlap_end_ts"] == "2026-08-04T10:00:00+05:30"
    )
    return passed, (
        f"APT1005 flagged={appointment is not None}; "
        f"impact={appointment['overlap_start_ts']}-{appointment['overlap_end_ts']}"
        if appointment
        else "APT1005 was not flagged"
    )


def _eval_human_triage(sim: WorkflowSimulation) -> tuple[bool, str]:
    statuses = {
        "duplicate": sim.triage_thread("THR009"),
        "ambiguous": sim.triage_thread("THR010"),
        "incomplete": sim.triage_thread("THR001"),
    }
    passed = statuses == {
        "duplicate": "DUPLICATE",
        "ambiguous": "HUMAN_REVIEW",
        "incomplete": "NEEDS_FOLLOW_UP",
    }
    return passed, str(statuses)


def _eval_slot_feasibility(sim: WorkflowSimulation) -> tuple[bool, str]:
    late = sim.report_driver_eta("SHP1006", "2026-08-04T11:20:00+05:30", "MEDIUM")
    late_slots = sim.provisional_slots(late.report_id)
    reefer = sim.report_driver_eta("SHP1015", "2026-08-04T18:30:00+05:30", "HIGH")
    reefer_slots = sim.provisional_slots(reefer.report_id)
    passed = bool(late_slots) and not reefer_slots
    earliest = late_slots[0]["slot_start_ts"] if late_slots else "none"
    return passed, (
        f"late shipment options={len(late_slots)} (earliest={earliest}); "
        f"reefer options={len(reefer_slots)}"
    )


def _eval_booking_race(sim: WorkflowSimulation) -> tuple[bool, str]:
    active = sim.db.execute(
        """
        SELECT slot_id FROM appointments
        WHERE appointment_status IN ('PENDING_CONFIRMATION', 'CONFIRMED', 'IN_PROGRESS')
        LIMIT 1
        """
    ).fetchone()
    unbooked = sim.db.execute(
        """
        SELECT s.shipment_id FROM shipments s
        WHERE NOT EXISTS (
            SELECT 1 FROM appointments a
            WHERE a.shipment_id = s.shipment_id
              AND a.is_current = 1
              AND a.appointment_status IN
                ('PENDING_CONFIRMATION', 'CONFIRMED', 'IN_PROGRESS')
        )
        LIMIT 1
        """
    ).fetchone()
    accepted = sim.try_second_active_booking(
        unbooked["shipment_id"], active["slot_id"]
    )
    return not accepted, f"second active booking accepted={accepted}"


EVALS = (
    ("Unverified ETA cannot overwrite state", 25, _eval_unverified_eta),
    ("Dock outage requires trusted verification", 25, _eval_outage_verification),
    ("Outage detects affected active appointments", 20, _eval_booked_appointment_impact),
    ("Duplicate, ambiguous, and incomplete triage", 15, _eval_human_triage),
    ("Recovery respects slot feasibility", 10, _eval_slot_feasibility),
    ("Concurrent booking is rejected", 5, _eval_booking_race),
)


def run_evals() -> list[EvalResult]:
    results = []
    for name, points, evaluate in EVALS:
        simulation = WorkflowSimulation()
        try:
            passed, detail = evaluate(simulation)
        except Exception as exc:
            passed, detail = False, f"{type(exc).__name__}: {exc}"
        finally:
            simulation.close()
        results.append(EvalResult(name, points, passed, detail))
    return results


def main() -> int:
    results = run_evals()
    score = sum(result.points for result in results if result.passed)
    for result in results:
        status = "PASS" if result.passed else "FAIL"
        print(f"{status} [{result.points:>2} pts] {result.name}: {result.detail}")
    print(f"Workflow simulation eval score: {score}/100")
    print("Scope: deterministic simulation behavior; no pretrained agent is connected.")
    return 0 if all(result.passed for result in results) else 1


if __name__ == "__main__":
    sys.exit(main())
