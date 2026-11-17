import unittest
from unittest.mock import patch

from nvidia_agent_evals import (
    CASES, STRESS_CASES, consistency, render_report, results_from_json, results_to_json,
    run_evals, score_answer,
)
from nvidia_agents import NVIDIAClientError


def answer(**overrides):
    base = {
        "verification": "NOT_APPLICABLE", "action": "NO_ACTION", "ask_whom": "NONE",
        "selected_id": None, "state_change_allowed": False, "extracted": None,
        "evidence": [], "question": None, "reason": "test",
    }
    return {**base, **overrides}


class AgentEvalTests(unittest.TestCase):
    def test_guard_blocks_infeasible_pick_and_awards_only_decision_share(self):
        duration_case = STRESS_CASES[5]
        result = score_answer(duration_case, answer(action="PROPOSE", selected_id="SLOT-A"), 1)
        self.assertTrue(result.violations)
        self.assertIsNone(result.guarded["selected_id"])
        self.assertEqual(result.guarded["action"], "NO_FEASIBLE_OPTION")
        self.assertEqual(result.outcome, "PARTIAL")
        self.assertAlmostEqual(result.earned, 0.6 * duration_case.points)

    def test_correct_safe_grounded_answer_earns_full_points(self):
        result = score_answer(
            CASES[1], answer(action="PROPOSE", selected_id="SLOT-B", evidence=["SLOT-B starts 12:00"]), 1,
        )
        self.assertEqual(result.outcome, "PASS")
        self.assertEqual(result.earned, CASES[1].points)

    def test_ungrounded_count_is_penalized(self):
        spike = STRESS_CASES[4]
        result = score_answer(spike, answer(
            verification="UNVERIFIED", action="ESCALATE", ask_whom="FACILITY",
            selected_id="OUTAGE-1", evidence=["32 duplicate reports"],
        ), 1)
        self.assertEqual(result.ungrounded, ("32",))
        self.assertAlmostEqual(result.earned, 0.9 * spike.points)

    def test_errors_score_zero_and_report_escapes_content(self):
        with patch("builtins.print"):
            results, score = run_evals(
                CASES[:2], ask=lambda role, scenario: (_ for _ in ()).throw(NVIDIAClientError("<bad>")),
            )
        self.assertEqual(score, 0)
        self.assertTrue(all(r.outcome == "ERROR" for r in results))
        report = render_report(results, "test/model")
        self.assertIn("&lt;bad&gt;", report)
        self.assertNotIn("<bad>", report)

    def test_consistency_and_json_round_trip(self):
        good = answer(action="PROPOSE", selected_id="SLOT-B")
        bad = answer(action="NO_FEASIBLE_OPTION")
        responses = iter([good, good, bad])
        with patch("builtins.print"):
            results, _ = run_evals(CASES[1:2], repeats=3, ask=lambda role, scenario: next(responses))
        self.assertAlmostEqual(consistency(results)[CASES[1].name][0], 2 / 3)
        restored, model = results_from_json(results_to_json(results, "m"))
        self.assertEqual([r.earned for r in restored], [r.earned for r in results])
        self.assertEqual(model, "m")
        with self.assertRaises(ValueError):
            results_from_json('{"results": []}')


if __name__ == "__main__":
    unittest.main()
