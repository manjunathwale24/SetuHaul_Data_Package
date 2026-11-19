import json
import threading
import unittest
from urllib.error import HTTPError
from urllib.parse import quote
from urllib.request import Request, urlopen

from brief_scenarios import HANDOFF_FIELDS
from service import Service, make_server


class ServiceApiTests(unittest.TestCase):
    def setUp(self):
        self.service = Service()
        self.server = make_server(self.service, port=0)
        self.base = f"http://127.0.0.1:{self.server.server_port}"
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()

    def call(self, method, path, body=None):
        data = json.dumps(body).encode() if body is not None else None
        req = Request(self.base + path, data=data, method=method, headers={"Content-Type": "application/json"})
        try:
            with urlopen(req, timeout=10) as resp:
                return resp.status, json.loads(resp.read())
        except HTTPError as exc:
            with exc:
                return exc.code, json.loads(exc.read())

    def holds(self):
        return self.service.db.execute("SELECT COUNT(*) FROM slot_holds").fetchone()[0]

    def hold(self, shipment, slot, now, eta=None):
        body = {"shipment_id": shipment, "slot_id": slot, "now": now}
        if eta:
            body["eta"] = eta
        return self.call("POST", "/holds", body)

    def test_options_only_message_creates_no_hold_and_is_idempotent(self):
        msg = {"external_id": "wa-11", "sender": "DRV001", "thread_id": "THR011", "now": "10:02",
               "text": "What slots are possible after 1 PM if I reach around 12:45?"}
        status, body = self.call("POST", "/messages", msg)
        self.assertEqual((status, body["status"], body["shipment_id"]), (200, "OPTIONS", "SHP1017"))
        self.assertTrue(1 <= len(body["options"]) <= 3)
        self.assertTrue(all(o["start"] >= "13:00" for o in body["options"]))
        self.assertFalse(body["triage"]["answer"]["state_change_allowed"])
        self.assertEqual(self.holds(), 0)
        _, again = self.call("POST", "/messages", msg)
        self.assertTrue(again["duplicate"])
        self.assertEqual(again["options"], body["options"])
        status, opts = self.call("GET", "/shipments/SHP1017/options?now=10:02&eta=12:45")
        self.assertEqual((status, opts["holds_created"]), (200, 0))
        self.assertEqual(self.holds(), 0)

    def test_driver_with_two_loads_gets_clarification_then_options(self):
        status, body = self.call("POST", "/messages", {
            "external_id": "wa-10a", "sender": "DRV004", "thread_id": "THR010", "now": "09:31",
            "text": "I will be late by 45 minutes."})
        self.assertEqual((status, body["status"], body["shipment_id"]), (200, "CLARIFY", None))
        self.assertEqual(set(body["candidates"]), {"SHP1004", "SHP1020"})
        self.assertIn("ORD-260804-020", body["question"])
        _, reply = self.call("POST", "/messages", {
            "external_id": "wa-10b", "sender": "DRV004", "thread_id": "THR010", "now": "09:35",
            "text": "The evening one, 020. Gate by 18:45."})
        self.assertEqual((reply["status"], reply["shipment_id"]), ("OPTIONS", "SHP1020"))

    def test_driver_option_reply_holds_and_requests_selected_slot_end_to_end(self):
        _, options = self.call("POST", "/messages", {
            "external_id": "wa-options-1", "sender": "DRV001", "thread_id": "THR-options",
            "now": "10:02", "text": "What slots are possible after 1 PM if I reach around 12:45?"})
        self.assertEqual(options["status"], "OPTIONS")
        selected = options["options"][0]

        _, response = self.call("POST", "/messages", {
            "external_id": "wa-options-2", "sender": "DRV001", "thread_id": "THR-options",
            "now": "10:03", "text": "option 1 please"})
        self.assertEqual((response["status"], response["slot_ids"][0]), ("REQUESTED", selected["slot_id"]))
        rows = self.service.db.execute(
            "SELECT state FROM slot_holds WHERE group_key=?", (response["group"],)).fetchall()
        self.assertTrue(rows)
        self.assertTrue(all(row["state"] == "REQUESTED" for row in rows))

        group = quote(response["group"], safe="")
        status, confirmed = self.call("POST", f"/holds/{group}/confirm", {
            "now": "10:05", "warehouse_ref": "WH-option-1"})
        self.assertEqual((status, confirmed["status"]), (200, "CONFIRMED"))

    def test_safety_emergency_is_escalated_without_offering_slots(self):
        status, response = self.call("POST", "/messages", {
            "external_id": "wa-emergency-1", "sender": "DRV001", "thread_id": "THR-emergency",
            "now": "10:02", "text": "Emergency: brakes failed. ETA 15:00"})
        self.assertEqual((status, response["status"]), (200, "ESCALATED"))
        self.assertEqual(response["options"], [])
        self.assertIn("safe location", response["reply"])
        self.assertEqual(self.holds(), 0)
        self.assertEqual(self.service.db.execute(
            "SELECT COUNT(*) FROM service_escalations WHERE shipment_id=?",
            (response["shipment_id"],)).fetchone()[0], 1)

    def test_hold_request_confirm_happy_path_and_duplicate_request(self):
        status, held = self.hold("SHP1020", "SLOT-JAI-026", "09:35", eta="19:00")
        self.assertEqual((status, held["status"], held["expires_at"]), (200, "HELD", "09:45"))
        group = quote(held["group"], safe="")
        status, req = self.call("POST", f"/holds/{group}/request", {"now": "09:37", "request_key": "wa-yes-1"})
        self.assertEqual((status, req["status"]), (200, "REQUESTED"))
        status, dup = self.call("POST", f"/holds/{group}/request", {"now": "09:38", "request_key": "wa-yes-1"})
        self.assertEqual((status, dup["status"]), (200, "DUPLICATE"))
        status, conf = self.call("POST", f"/holds/{group}/confirm", {"now": "09:45", "warehouse_ref": "WH-JAI-9120"})
        self.assertEqual((status, conf["status"], conf["replaced_appointment_id"]), (200, "CONFIRMED", "APT1020"))
        _, again = self.call("POST", f"/holds/{group}/confirm", {"now": "09:46", "warehouse_ref": "WH-JAI-9120"})
        self.assertEqual(again["status"], "ALREADY_CONFIRMED")
        actions = [e["action"] for e in self.call("GET", "/audit")[1]["entries"]]
        self.assertEqual(actions, ["HOLD_CREATED", "HOLD_REQUESTED", "APPOINTMENT_CONFIRMED"])
        _, board = self.call("GET", "/board?now=09:46")
        self.assertEqual(board["board"]["SLOT-JAI-026"], ["BOOKED", "SHP1020"])

    def test_expired_hold_is_stale_on_request(self):
        _, held = self.hold("SHP1006", "SLOT-JAI-020", "09:34", eta="11:20")
        group = quote(held["group"], safe="")
        status, body = self.call("POST", f"/holds/{group}/request", {"now": "09:50", "request_key": "late-yes"})
        self.assertEqual((status, body["status"]), (409, "STALE"))
        self.assertIn("EXPIRED", body["detail"])

    def test_release_frees_slot_for_another_shipment_and_is_idempotent(self):
        _, held = self.hold("SHP1006", "SLOT-JAI-020", "09:34", eta="11:20")
        status, body = self.hold("SHP1012", "SLOT-JAI-020", "09:35", eta="11:10")
        self.assertEqual(status, 409)
        self.assertTrue(any("taken" in r for r in body["reasons"]))
        group = quote(held["group"], safe="")
        self.assertEqual(self.call("POST", f"/holds/{group}/release", {"now": "09:36"})[1]["status"], "RELEASED")
        self.assertEqual(self.call("POST", f"/holds/{group}/release", {"now": "09:36"})[1]["status"],
                         "ALREADY_RELEASED")
        status, other = self.hold("SHP1012", "SLOT-JAI-020", "09:37", eta="11:10")
        self.assertEqual((status, other["status"]), (200, "HELD"))
        status, rel = self.call("POST", "/appointments/APT1006/release", {"now": "09:38", "reason": "ETA after slot"})
        self.assertEqual(rel["status"], "RELEASED")
        _, rel = self.call("POST", "/appointments/APT1006/release", {"now": "09:39", "reason": "ETA after slot"})
        self.assertEqual(rel["status"], "ALREADY_RELEASED")

    def test_concurrent_holds_on_one_slot_have_one_winner(self):
        barrier = threading.Barrier(2)
        results = {}

        def attempt(shipment):
            barrier.wait()
            results[shipment] = self.hold(shipment, "SLOT-JAI-020", "09:45", eta="11:20")[0]

        threads = [threading.Thread(target=attempt, args=(s,)) for s in ("SHP1006", "SHP1012")]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(sorted(results.values()), [200, 409])
        live = self.service.db.execute(
            "SELECT COUNT(*) FROM slot_holds WHERE slot_id='SLOT-JAI-020' AND state='HELD'").fetchone()[0]
        self.assertEqual(live, 1)

    def test_no_feasible_slot_escalates_with_complete_handoff(self):
        status, body = self.call("POST", "/messages", {
            "external_id": "wa-05", "sender": "DRV015", "thread_id": "THR005", "now": "16:24",
            "text": "Evening traffic. ETA 18:30. Can the reefer unload tonight?"})
        self.assertEqual((status, body["status"], body["shipment_id"]), (200, "ESCALATED", "SHP1015"))
        record = body["escalation"]
        self.assertEqual([f for f in HANDOFF_FIELDS if record.get(f) in (None, "", [])], [])
        self.assertEqual(self.holds(), 0)
        stored = self.service.db.execute("SELECT COUNT(*) FROM service_escalations").fetchone()[0]
        self.assertEqual(stored, 1)

    def test_integrity_has_zero_conflicts_after_activity(self):
        self.hold("SHP1006", "SLOT-JAI-020", "09:34", eta="11:20")
        self.hold("SHP1012", "SLOT-JAI-020", "09:34", eta="11:10")
        self.hold("SHP1012", "SLOT-JAI-019", "09:34", eta="11:10")
        status, body = self.call("GET", "/integrity")
        self.assertEqual(status, 200)
        self.assertTrue(body["ok"], body)
        self.assertEqual(body["conflicts"]["new_appointment_overlaps"], 0)
        self.assertGreater(body["conflicts"]["preexisting_appointment_overlaps"], 0)
        self.assertEqual(self.call("GET", "/health")[1]["status"], "ok")

    def test_dashboard_wireframe_and_activity_api(self):
        with urlopen(self.base + "/dashboard", timeout=10) as response:
            self.assertEqual(response.status, 200)
            self.assertIn("text/html", response.headers["Content-Type"])
            html = response.read().decode()
        self.assertIn("Operations tower", html)
        self.assertIn("Agent training room", html)
        self.assertIn("TRAINING SIMULATION", html)

        status, activity = self.call("GET", "/activity?now=12:45")
        self.assertEqual(status, 200)
        self.assertEqual(activity["mode"], "SIMULATION")
        self.assertEqual(len(activity["docks"]), 6)
        self.assertGreater(activity["counts"]["inbound"], 0)
        self.assertIn("SHP1017", {item["shipment_id"] for item in activity["shipments"]})
        self.assertEqual(set(activity["training_agents"]), {
            "driver_01", "driver_02", "driver_03", "driver_04", "facility"})
        self.assertTrue(any(event["source"] == "seed transcript" for event in activity["activity"]))

        with urlopen(self.base + "/deployment", timeout=10) as response:
            self.assertEqual(response.status, 200)
            deployment_html = response.read().decode()
        self.assertIn("Deployment control", deployment_html)
        self.assertIn("ROLLOUT NOT OBSERVED", deployment_html)
        status, deployment = self.call("GET", "/deployment/status")
        self.assertEqual(status, 200)
        self.assertEqual(deployment["health"], "HEALTHY")
        self.assertEqual(deployment["rollout_status"], "NOT_OBSERVED")
        self.assertEqual(deployment["cluster_api_status"], "NOT_CONNECTED")

    def test_training_driver_and_facility_agents_do_not_change_booking_state(self):
        before = self.holds()
        _, driver = self.call("POST", "/training/agent", {
            "role": "driver", "message": "What is my ETA for the shipment?"})
        _, facility = self.call("POST", "/training/agent", {
            "role": "facility", "message": "What is the status of D3?"})
        _, safety = self.call("POST", "/training/agent", {
            "role": "driver", "message": "Emergency: brakes failed"})
        _, ambiguous_load = self.call("POST", "/training/agent", {
            "role": "driver", "agent_id": "driver_02", "message": "Which shipment do you mean?"})
        _, slot_choice = self.call("POST", "/training/agent", {
            "role": "driver", "agent_id": "driver_03", "message": "Can I book option 1?"})
        _, breakdown = self.call("POST", "/training/agent", {
            "role": "driver", "agent_id": "driver_04", "message": "Can I continue to the facility?"})
        self.assertTrue(driver["simulated"])
        self.assertIn("11:20", driver["reply"])
        self.assertIn("D3", facility["reply"])
        self.assertIn("Safety first", safety["lesson"])
        self.assertIn("SHP1004 and SHP1020", ambiguous_load["reply"])
        self.assertIn("held, not booked", slot_choice["reply"])
        self.assertIn("Safety first", breakdown["lesson"])
        self.assertEqual(
            {driver["agent_id"], ambiguous_load["agent_id"], slot_choice["agent_id"], breakdown["agent_id"]},
            {"driver_01", "driver_02", "driver_03", "driver_04"},
        )
        self.assertEqual(self.holds(), before)
        self.assertEqual(self.service.db.execute("SELECT COUNT(*) FROM service_audit_log").fetchone()[0], 0)

    def test_training_agent_rejects_mismatched_role_and_agent(self):
        status, body = self.call("POST", "/training/agent", {
            "role": "facility", "agent_id": "driver_01", "message": "Hello"})
        self.assertEqual(status, 400)
        self.assertIn("role must match", body["error"])

    def test_confirmation_rechecks_latest_driver_eta(self):
        status, held = self.hold("SHP1006", "SLOT-JAI-020", "09:34", eta="11:20")
        self.assertEqual(status, 200)
        group = quote(held["group"], safe="")
        status, requested = self.call("POST", f"/holds/{group}/request", {
            "now": "09:35", "request_key": "eta-recheck-request"})
        self.assertEqual((status, requested["status"]), (200, "REQUESTED"))

        _, updated = self.call("POST", "/messages", {
            "external_id": "wa-eta-recheck", "sender": "DRV006", "thread_id": "THR-eta-recheck",
            "now": "09:40", "text": "SHP1006 ETA 15:00"})
        self.assertIn(updated["status"], {"OPTIONS", "ESCALATED"})

        status, rejected = self.call("POST", f"/holds/{group}/confirm", {
            "now": "09:45", "warehouse_ref": "WH-stale-eta"})
        self.assertEqual(status, 409)
        self.assertEqual(rejected["status"], "REJECTED")
        state = self.service.db.execute(
            "SELECT state FROM slot_holds WHERE group_key=? LIMIT 1", (held["group"],)).fetchone()["state"]
        self.assertEqual(state, "RELEASED")

    def test_confirmation_preserves_eta_explicitly_used_for_hold(self):
        _, reported = self.call("POST", "/messages", {
            "external_id": "wa-eta-before-hold", "sender": "DRV006", "thread_id": "THR-explicit-eta",
            "now": "09:30", "text": "SHP1006 ETA 18:00"})
        self.assertIn(reported["status"], {"OPTIONS", "ESCALATED"})

        status, held = self.hold("SHP1006", "SLOT-JAI-020", "09:30", eta="12:00")
        self.assertEqual(status, 200)
        group = quote(held["group"], safe="")
        status, _ = self.call("POST", f"/holds/{group}/request", {
            "now": "09:31", "request_key": "explicit-eta-request"})
        self.assertEqual(status, 200)
        status, confirmed = self.call("POST", f"/holds/{group}/confirm", {
            "now": "09:35", "warehouse_ref": "WH-explicit-eta"})
        self.assertEqual((status, confirmed["status"]), (200, "CONFIRMED"))

    def test_request_key_cannot_be_reused_for_another_hold(self):
        _, first = self.hold("SHP1006", "SLOT-JAI-020", "09:34", eta="11:20")
        _, second = self.hold("SHP1012", "SLOT-JAI-006", "09:34", eta="11:20")
        first_group, second_group = quote(first["group"], safe=""), quote(second["group"], safe="")
        status, _ = self.call("POST", f"/holds/{first_group}/request", {
            "now": "09:35", "request_key": "reused-key"})
        self.assertEqual(status, 200)
        status, response = self.call("POST", f"/holds/{second_group}/request", {
            "now": "09:35", "request_key": "reused-key"})
        self.assertEqual(status, 409)
        self.assertEqual(response["status"], "KEY_REUSED")


if __name__ == "__main__":
    unittest.main()
