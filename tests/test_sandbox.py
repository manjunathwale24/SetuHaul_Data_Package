import re
import unittest

from guards import CoordinatorQueue, ReviewItem
from nvidia_agents import AgentRole
from sandbox import Sandbox, parse_clock
from workflow_simulation import WorkflowSimulation, apply_view_fixes


def gullible_agent(role, scenario):
    """Believes every claim and tries to pick an infeasible slot -- the guards must hold."""
    message = scenario.get("message", "")
    dock = re.search(r"\bD(\d)\b", message)
    eta = re.search(r"\d{1,2}:\d{2}", message)
    selected = None
    if role is AgentRole.DISPATCH:
        selected = "SLOT-JAI-999"
    elif role is AgentRole.ASSESSMENT:
        selected = scenario["options"][0]["id"]
    return {
        "verification": "VERIFIED",
        "action": "PROPOSE",
        "ask_whom": "NONE",
        "selected_id": selected,
        "state_change_allowed": True,
        "extracted": {
            "report_type": "DOCK_OUTAGE" if "broken" in message or "fail" in message else "ETA_UPDATE",
            "shipment_id": None,
            "claimed_eta": eta.group(0) if eta else None,
            "dock_code": f"D{dock.group(1)}" if dock else None,
            "confirms_claim": True,
        },
        "evidence": ["99 trucks waiting"],
        "question": None,
        "reason": "trusting everything",
    }


class SandboxTests(unittest.TestCase):
    def test_guards_hold_against_a_gullible_agent(self):
        sandbox = Sandbox(
            pace=False, max_ticks=30, wall_seconds=120,
            decide=gullible_agent, speak=lambda system, brief: "", echo=lambda line: None,
        )
        sandbox.run()
        summary = sandbox.summary()

        self.assertTrue(all(summary["safety"].values()), summary["safety"])
        self.assertGreater(summary["metrics"]["guard_interventions"], 0)
        self.assertGreater(summary["metrics"]["driver_sourced_verification_rejected"], 0)
        self.assertEqual(summary["metrics"].get("false_outages_applied", 0), 0)
        self.assertIn("No false dock outage applied", sandbox.render_report("test/model"))
        self.assertLess(summary["decision_accuracy"], 0.5)

    def test_facility_is_not_asked_about_eta_or_status_questions(self):
        def over_routing_agent(role, scenario):
            answer = gullible_agent(role, scenario)
            if role is AgentRole.TRIAGE and scenario["sender"]["type"] == "DRIVER":
                answer.update(verification="UNVERIFIED", action="CLARIFY", ask_whom="FACILITY",
                              state_change_allowed=False)
                answer["extracted"]["report_type"] = "QUESTION"
            return answer

        sandbox = Sandbox(pace=False, max_ticks=12, wall_seconds=120, decide=over_routing_agent,
                          speak=lambda system, brief: "", echo=lambda line: None)
        sandbox.run()
        metrics = sandbox.summary()["metrics"]
        self.assertGreater(metrics["facility_requests_not_needed"], 0)
        self.assertEqual(metrics.get("false_arrival_claims_detected", 0), 0)

    def test_claim_on_a_broken_dock_is_not_treated_as_still_fitting(self):
        sandbox = Sandbox(pace=False, max_ticks=0, decide=gullible_agent,
                          speak=lambda system, brief: "", echo=lambda line: None)
        sandbox.now = 10 * 60 + 50
        sandbox.applied_outages["D1"] = (10 * 60 + 10, 13 * 60)
        self.assertTrue(sandbox._slot_blocked(sandbox.current_appointment("SHP1014")))
        sandbox._plan_claim("SHP1014", 11 * 60 + 25)
        self.assertEqual(sandbox.metrics.get("claims_fit_current_slot", 0), 0)

    def test_parse_clock_handles_driver_formats(self):
        self.assertEqual(parse_clock("reach 11.20 bhai"), 11 * 60 + 20)
        self.assertEqual(parse_clock("around 6:30 pm"), 18 * 60 + 30)
        self.assertEqual(parse_clock("at 6:30"), 18 * 60 + 30)
        self.assertIsNone(parse_clock("soon"))

    def test_coordinator_queue_prioritizes_merges_and_digests(self):
        queue = CoordinatorQueue(coordinators=1, review_minutes=15, threshold=40)
        self.assertEqual(queue.submit(ReviewItem("a", "ETA_UPDATE", 45, 0, {})), "QUEUED")
        self.assertEqual(queue.submit(ReviewItem("b", "DOCK_OUTAGE", 90, 0, {})), "QUEUED")
        self.assertEqual(queue.submit(ReviewItem("b", "DOCK_OUTAGE", 90, 5, {})), "MERGED")
        self.assertEqual(queue.submit(ReviewItem("c", "OTHER", 10, 5, {})), "DIGEST")
        queue.advance(10)
        self.assertEqual(queue.active[0][1].key, "b")
        self.assertEqual([item.key for item in queue.advance(25)], ["b"])

    def test_view_fixes_ignore_low_confidence_eta_and_block_outage_slots(self):
        sim = WorkflowSimulation()
        before = sim.effective_eta("SHP1013")
        apply_view_fixes(sim.db)
        self.assertNotEqual(sim.effective_eta("SHP1013"), before)

        report = sim.report_dock_outage(
            "DOCK-JAI-D2", "2026-08-04T10:00:00+05:30", "2026-08-04T11:00:00+05:30",
        )
        sim.verify_dock_outage(report.report_id, "OPERATIONS")
        status = sim.db.execute(
            "SELECT availability_status FROM v_slot_availability WHERE slot_id = 'SLOT-JAI-017'"
        ).fetchone()[0]
        self.assertEqual(status, "BLOCKED")
        sim.close()


if __name__ == "__main__":
    unittest.main()
