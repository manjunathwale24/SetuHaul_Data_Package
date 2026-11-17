import json
import os
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch
from urllib.request import Request

from nvidia_agents import (
    AgentRole,
    NVIDIAClientError,
    ask_agent,
    load_nvidia_env,
    parse_agent_response,
)


class NVIDIAAgentTests(unittest.TestCase):
    def test_env_loads_literal_values_without_overriding_environment(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / ".env"
            path.write_text(
                '# local settings\nNVIDIA_API_KEY="test-key"\n'
                'NVIDIA_MODEL_ID=file/model\n', encoding="utf-8",
            )
            with patch.dict(os.environ, {"NVIDIA_MODEL_ID": "shell/model"}, clear=True):
                load_nvidia_env(path)
                self.assertEqual(os.environ["NVIDIA_API_KEY"], "test-key")
                self.assertEqual(os.environ["NVIDIA_MODEL_ID"], "shell/model")

    def test_invalid_env_does_not_echo_secret_or_partially_load(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / ".env"
            path.write_text(
                "NVIDIA_API_KEY=test-secret\ninvalid-secret-line\n", encoding="utf-8",
            )
            with patch.dict(os.environ, {}, clear=True):
                with self.assertRaises(NVIDIAClientError) as error:
                    load_nvidia_env(path)
                self.assertNotIn("secret", str(error.exception))
                self.assertNotIn("NVIDIA_API_KEY", os.environ)

    def test_parse_agent_response_requires_complete_typed_json(self) -> None:
        answer = {
            "verification": "UNVERIFIED",
            "action": "CLARIFY",
            "ask_whom": "DRIVER",
            "selected_id": None,
            "state_change_allowed": False,
            "extracted": {
                "report_type": "ETA_UPDATE", "shipment_id": None, "claimed_eta": None,
                "dock_code": None, "confirms_claim": None,
            },
            "evidence": ["shipment id is missing"],
            "question": "Which shipment do you mean?",
            "reason": "Two assignments are possible.",
        }
        self.assertEqual(parse_agent_response(json.dumps(answer)), answer)

    def test_parse_agent_response_rejects_non_json_output(self) -> None:
        with self.assertRaises(NVIDIAClientError):
            parse_agent_response("I think the driver is probably late.")

    def test_parse_strips_reasoning_and_fences_but_rejects_unknown_values(self) -> None:
        answer = {
            "verification": "UNVERIFIED", "action": "CLARIFY", "ask_whom": "FACILITY",
            "selected_id": None, "state_change_allowed": False,
            "extracted": {"report_type": "DOCK_OUTAGE", "dock_code": "D2"},
            "evidence": [], "question": "Is D2 down?", "reason": "Unconfirmed.",
        }
        wrapped = "<think>long reasoning</think>```json\n" + json.dumps(answer) + "\n```"
        parsed = parse_agent_response(wrapped)
        self.assertEqual(parsed["extracted"]["dock_code"], "D2")
        self.assertIsNone(parsed["extracted"]["confirms_claim"])
        with self.assertRaisesRegex(NVIDIAClientError, "ask_whom"):
            parse_agent_response(json.dumps({**answer, "ask_whom": "MANAGER"}))

    def test_ask_agent_requires_key_without_making_a_request(self) -> None:
        with patch.dict(os.environ, {}, clear=True):
            with self.assertRaisesRegex(NVIDIAClientError, "NVIDIA_API_KEY"):
                ask_agent(AgentRole.TRIAGE, {"report": "synthetic"})

    def test_request_uses_synthetic_payload_and_configured_model(self) -> None:
        answer = {
            "verification": "NOT_APPLICABLE",
            "action": "CLARIFY",
            "ask_whom": "DRIVER",
            "selected_id": None,
            "state_change_allowed": False,
            "extracted": None,
            "evidence": [],
            "question": "Which shipment?",
            "reason": "Shipment is ambiguous.",
        }

        class FakeResponse:
            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return None

            def read(self):
                return json.dumps(
                    {"choices": [{"message": {"content": json.dumps(answer)}}]}
                ).encode()

        captured: dict[str, object] = {}

        def fake_urlopen(request: Request, timeout: float):
            captured["url"] = request.full_url
            captured["body"] = json.loads(request.data)
            captured["timeout"] = timeout
            return FakeResponse()

        with patch.dict(
            os.environ,
            {
                "NVIDIA_API_KEY": "test-key-not-sent-to-network",
                "NVIDIA_MODEL_ID": "test/model",
                "NVIDIA_API_URL": "https://example.invalid/v1/chat/completions",
            },
            clear=True,
        ):
            with patch("nvidia_agents.urlopen", fake_urlopen):
                result = ask_agent(
                    AgentRole.TRIAGE, {"report": {"message": "synthetic"}}
                )

        self.assertEqual(result, answer)
        self.assertEqual(captured["url"], "https://example.invalid/v1/chat/completions")
        self.assertEqual(captured["body"]["model"], "test/model")
        self.assertEqual(captured["body"]["response_format"], {"type": "json_object"})
        self.assertEqual(captured["body"]["max_tokens"], 4096)
        self.assertIn("synthetic", captured["body"]["messages"][1]["content"])
        self.assertNotIn("test-key-not-sent-to-network", str(captured["body"]))

    def test_truncated_and_reasoning_only_responses_are_explicit_errors(self) -> None:
        responses = (
            (
                {"finish_reason": "length", "message": {"content": '{"decision":'}},
                "token limit",
            ),
            (
                {
                    "finish_reason": "stop",
                    "message": {"content": None, "reasoning_content": "not an answer"},
                },
                "no text answer",
            ),
        )
        for choice, expected in responses:
            with self.subTest(choice=choice):
                with patch.dict(os.environ, {"NVIDIA_API_KEY": "test-key"}, clear=True):
                    with patch("nvidia_agents.urlopen") as mock_open:
                        mock_open.return_value.__enter__.return_value.read.return_value = (
                            json.dumps({"choices": [choice]}).encode()
                        )
                        with self.assertRaisesRegex(NVIDIAClientError, expected):
                            ask_agent(AgentRole.TRIAGE, {"report": "synthetic"})


if __name__ == "__main__":
    unittest.main()
