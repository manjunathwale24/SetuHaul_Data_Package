import tempfile
import threading
import unittest
from pathlib import Path

from brief_scenarios import offline_agent, revise_eta, run
from slot_engine import SlotEngine, race_for_slot, to_minutes


class SlotEngineTests(unittest.TestCase):
    def setUp(self):
        self.e = SlotEngine()

    def test_long_unload_claims_the_next_slot(self):
        claims = self.e.overrun_claims()
        self.assertEqual(claims["SLOT-JAI-047"][1], "SHP1009")  # 75-min unload from D4 11:00
        starts = [c.dock + c.slot_ids[0] for c in self.e.feasible("SHP1006", 660, now=585)]
        self.assertNotIn("D4SLOT-JAI-047", starts)

    def test_reefer_during_maintenance_has_no_feasible_slot(self):
        self.assertEqual(self.e.feasible("SHP1015", to_minutes("18:30"), now=to_minutes("16:24")), [])

    def test_expired_hold_is_stale_and_retry_is_idempotent(self):
        option = self.e.feasible("SHP1006", 680, now=574)[0]
        ok, group = self.e.hold("SHP1006", option, 680, now=574)
        self.assertTrue(ok)
        self.assertEqual(self.e.request("SHP1006", group, now=590, request_key="k1")[0], "STALE")
        ok, group = self.e.hold("SHP1006", option, 680, now=590)
        self.assertEqual(self.e.request("SHP1006", group, now=591, request_key="k2")[0], "REQUESTED")
        self.assertEqual(self.e.request("SHP1006", group, now=591, request_key="k2")[0], "DUPLICATE")

    def test_confirm_replaces_current_appointment_once(self):
        option = self.e.feasible("SHP1020", to_minutes("19:00"), now=575)[0]
        _, group = self.e.hold("SHP1020", option, 1140, now=575)
        self.e.request("SHP1020", group, now=576, request_key="y")
        ok, _ = self.e.confirm("SHP1020", group, 1140, now=580, warehouse_ref="WH-1")
        self.assertTrue(ok)
        self.assertEqual(self.e.conflicts()["shipments_with_two_current"], 0)

    def test_requested_hold_expires_when_warehouse_does_not_confirm(self):
        option = self.e.feasible("SHP1006", 680, now=574)[0]
        ok, group = self.e.hold("SHP1006", option, 680, now=574)
        self.assertTrue(ok)
        self.assertEqual(self.e.request("SHP1006", group, now=575, request_key="warehouse-timeout")[0],
                         "REQUESTED")
        self.assertEqual(self.e.expire_holds(590), len(option.slot_ids))
        ok, detail = self.e.confirm("SHP1006", group, 680, now=590, warehouse_ref="WH-late")
        self.assertFalse(ok)
        self.assertEqual(detail, "no requested hold")
        self.assertEqual(self.e.db.execute(
            "SELECT DISTINCT state FROM slot_holds WHERE group_key=?", (group,)).fetchone()["state"], "EXPIRED")

    def test_concurrent_same_key_requests_are_atomic_and_idempotent(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path = str(Path(tmp) / "race.sqlite")
            first = SlotEngine(db_path)
            option = first.feasible("SHP1006", 680, now=574)[0]
            ok, group = first.hold("SHP1006", option, 680, now=574)
            self.assertTrue(ok)

            engines = [SlotEngine(db_path, seed=False), SlotEngine(db_path, seed=False)]
            barrier = threading.Barrier(2)
            statuses = []

            def attempt(engine):
                barrier.wait()
                statuses.append(engine.request("SHP1006", group, now=576, request_key="race-key")[0])

            threads = [threading.Thread(target=attempt, args=(engine,)) for engine in engines]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join()
            self.assertEqual(sorted(statuses), ["DUPLICATE", "REQUESTED"])
            self.assertEqual(first.db.execute(
                "SELECT COUNT(*) FROM processed_messages WHERE idempotency_key='race-key'").fetchone()[0], 1)
            for engine in engines:
                engine.db.close()
            first.db.close()

    def test_concurrent_confirmation_only_commits_once(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path = str(Path(tmp) / "confirm-race.sqlite")
            first = SlotEngine(db_path)
            option = first.feasible("SHP1020", 1140, now=575)[0]
            ok, group = first.hold("SHP1020", option, 1140, now=575)
            self.assertTrue(ok)
            self.assertEqual(first.request("SHP1020", group, now=576, request_key="confirm-race")[0],
                             "REQUESTED")

            engines = [SlotEngine(db_path, seed=False), SlotEngine(db_path, seed=False)]
            barrier = threading.Barrier(2)
            results = []

            def attempt(engine):
                barrier.wait()
                results.append(engine.confirm(
                    "SHP1020", group, 1140, now=580, warehouse_ref="WH-race")[0])

            threads = [threading.Thread(target=attempt, args=(engine,)) for engine in engines]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join()
            self.assertEqual(sorted(results), [False, True])
            self.assertEqual(first.db.execute(
                "SELECT COUNT(*) FROM appointments WHERE shipment_id='SHP1020' AND is_current=1").fetchone()[0], 1)
            for engine in engines:
                engine.db.close()
            first.db.close()

    def test_parallel_holds_on_one_slot_have_one_winner(self):
        race = race_for_slot("SLOT-JAI-020", ["SHP1006", "SHP1012", "SHP1017"], 680, 585)
        self.assertEqual((race["winners"], race["live_holds_after"]), (1, 1))


class BriefScenarioTests(unittest.TestCase):
    def test_eta_confidence_rule(self):
        self.assertEqual(revise_eta("I am late by one hour.", 559, [])["confidence"], "LOW")
        self.assertEqual(revise_eta("Reaching around 11:20", 574, [(650, "MEDIUM")])["planning_eta"], 695)

    def test_offline_run_covers_every_brief_item_without_conflicts(self):
        report = run(offline_agent, "offline", "offline")
        gaps = [k for items in report["coverage"].values() for k, v in items.items() if not v["scenarios"]]
        self.assertEqual(gaps, [])
        self.assertEqual(report["measures"]["conflicting_allocations"], 0)
        self.assertGreaterEqual(report["measures"]["options_infeasible_at_recheck"], 1)
        self.assertEqual(report["measures"]["escalation_completeness"], 1.0)


if __name__ == "__main__":
    unittest.main()
