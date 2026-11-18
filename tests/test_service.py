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
        self.assertEqual(self.call("GET", "/health")[1]["status"], "ok")


if __name__ == "__main__":
    unittest.main()
