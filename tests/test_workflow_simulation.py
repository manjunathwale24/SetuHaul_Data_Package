import unittest

from workflow_simulation import WorkflowSimulation


class WorkflowSimulationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.simulation = WorkflowSimulation()

    def tearDown(self) -> None:
        self.simulation.close()

    def test_seeded_duplicate_ambiguous_and_incomplete_threads(self) -> None:
        self.assertEqual(self.simulation.triage_thread("THR009"), "DUPLICATE")
        self.assertEqual(self.simulation.triage_thread("THR010"), "HUMAN_REVIEW")
        self.assertEqual(self.simulation.triage_thread("THR001"), "NEEDS_FOLLOW_UP")

    def test_unverified_eta_does_not_change_effective_eta(self) -> None:
        before = self.simulation.effective_eta("SHP1006")
        report = self.simulation.report_driver_eta(
            "SHP1006", "2026-08-04T23:59:00+05:30", "HIGH"
        )

        self.assertEqual(report.status, "UNVERIFIED")
        self.assertEqual(self.simulation.effective_eta("SHP1006"), before)

    def test_dock_outage_requires_verification_and_blocks_overlapping_slot(self) -> None:
        eta = self.simulation.report_driver_eta(
            "SHP1006", "2026-08-04T11:20:00+05:30", "MEDIUM"
        )
        options = self.simulation.provisional_slots(eta.report_id)
        self.assertTrue(options)
        slot = options[0]
        outage = self.simulation.report_dock_outage(
            self.simulation.db.execute(
                "SELECT dock_id FROM appointment_slots WHERE slot_id = ?",
                (slot["slot_id"],),
            ).fetchone()[0],
            slot["slot_start_ts"],
            slot["slot_end_ts"],
        )

        self.assertEqual(self.simulation.slot_status(slot["slot_id"]), "AVAILABLE")
        with self.assertRaises(ValueError):
            self.simulation.verify_dock_outage(outage.report_id, "DRIVER")

        self.simulation.verify_dock_outage(outage.report_id, "OPERATIONS")
        self.assertEqual(self.simulation.slot_status(slot["slot_id"]), "BLOCKED")
        self.assertNotIn(
            slot["slot_id"],
            {option["slot_id"] for option in self.simulation.provisional_slots(eta.report_id)},
        )

    def test_verified_outage_flags_overlapping_active_appointment(self) -> None:
        outage = self.simulation.report_dock_outage(
            "DOCK-JAI-D3",
            "2026-08-04T09:15:00+05:30",
            "2026-08-04T13:00:00+05:30",
        )
        with self.assertRaises(ValueError):
            self.simulation.impacted_appointments(outage.report_id)

        self.simulation.verify_dock_outage(outage.report_id, "OPERATIONS")
        impacted = self.simulation.impacted_appointments(outage.report_id)
        appointment = next(
            item for item in impacted if item["appointment_id"] == "APT1005"
        )

        self.assertEqual(
            appointment["overlap_start_ts"], "2026-08-04T09:15:00+05:30"
        )
        self.assertEqual(
            appointment["overlap_end_ts"], "2026-08-04T10:00:00+05:30"
        )

    def test_reefer_maintenance_leaves_no_provisional_slot(self) -> None:
        report = self.simulation.report_driver_eta(
            "SHP1015", "2026-08-04T18:30:00+05:30", "HIGH"
        )
        self.assertEqual(self.simulation.provisional_slots(report.report_id), [])

    def test_partial_unique_index_blocks_second_active_booking(self) -> None:
        active = self.simulation.db.execute(
            """
            SELECT slot_id FROM appointments
            WHERE appointment_status IN ('PENDING_CONFIRMATION', 'CONFIRMED', 'IN_PROGRESS')
            LIMIT 1
            """
        ).fetchone()
        unbooked = self.simulation.db.execute(
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

        self.assertFalse(
            self.simulation.try_second_active_booking(
                unbooked["shipment_id"], active["slot_id"]
            )
        )


if __name__ == "__main__":
    unittest.main()
