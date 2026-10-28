"""OpenMinion agent integration test.

Skipped unless ``openminion`` is importable. The test runs the real agent loop
against the deterministic mock endpoint, so no provider credentials or network
access are needed.
"""

from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path

from cortensor_canary.agent import (
    agent_stats,
    openminion_available,
    run_agent,
    write_mock_config,
)
from cortensor_canary.client import Target
from cortensor_canary.mock import MockServer
from cortensor_canary.pipeline import Workspace


def _target(server: MockServer, name: str) -> Target:
    return Target(
        name=name,
        base_url=server.base_url,
        model="mock-good",
        label=name,
        api_key_env="CANARY_MOCK_KEY",
        timeout=10,
    )


@unittest.skipUnless(openminion_available(), "openminion is not installed")
class AgentToolLoopTest(unittest.TestCase):
    def test_agent_runs_full_gate(self):
        os.environ.setdefault("CANARY_MOCK_KEY", "mock")
        with MockServer("good") as baseline_server, MockServer("degraded") as candidate_server:
            with tempfile.TemporaryDirectory() as tmp:
                workspace = Workspace(Path(tmp))
                config = write_mock_config(Path(tmp) / "agents.json", baseline_server.base_url)
                result = run_agent(
                    "Decide whether mock-candidate can replace mock-baseline. "
                    "Run both evaluations, compare them and report the verdict.",
                    workspace,
                    config_path=str(config),
                    targets={
                        "mock-baseline": _target(baseline_server, "mock-baseline"),
                        "mock-candidate": _target(candidate_server, "mock-candidate"),
                    },
                    timeout=180,
                )
                stats = agent_stats(result)
                text = result.text or ""

                artifacts = sorted(Path(tmp, "reports").glob("*/report.html"))
                self.assertTrue(artifacts, "the agent did not produce an HTML report")
                self.assertTrue(artifacts[0].is_file())

        self.assertGreaterEqual(stats["tool_calls"], 5, text)
        self.assertEqual(stats["tool_errors"], 0, text)
        self.assertIn("FAIL", text.upper())
        self.assertIn("mock-baseline", text)

    def test_agent_surfaces_missing_target_error(self):
        """A missing target must surface as a tool error, never as a made-up verdict."""
        os.environ.setdefault("CANARY_MOCK_KEY", "mock")
        with MockServer("good") as server:
            with tempfile.TemporaryDirectory() as tmp:
                workspace = Workspace(Path(tmp))
                config = write_mock_config(Path(tmp) / "agents.json", server.base_url)
                result = run_agent(
                    "Decide whether mock-candidate can replace mock-baseline.",
                    workspace,
                    config_path=str(config),
                    targets={"mock-baseline": _target(server, "mock-baseline")},
                    timeout=120,
                )
                stats = agent_stats(result)
                text = result.text or ""
                self.assertEqual(stats["tool_errors"], 0)
                self.assertIn("error", text.lower())
                self.assertNotIn("Recommendation: promote", text)


class AgentConfigTest(unittest.TestCase):
    def test_mock_config_is_valid_json(self):
        import json

        with tempfile.TemporaryDirectory() as tmp:
            path = write_mock_config(Path(tmp) / "agents.json", "http://127.0.0.1:1234/v1")
            payload = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual(payload["agents"]["canary"]["default_channel"], "console")
        self.assertIn("console", payload["enabled_channels"])
        self.assertEqual(payload["providers"]["openai"]["base_url"], "http://127.0.0.1:1234/v1")

    def test_agent_stats_parses_strings(self):
        class FakeResult:
            stats = {"tool_calls": 3}
            metadata = {"inference_steps": "5", "tool_calls_count": "3", "total_tokens_used": "900"}

        values = agent_stats(FakeResult())
        self.assertEqual(values["inference_steps"], 5)
        self.assertEqual(values["tool_calls"], 3)
        self.assertEqual(values["tokens"], 900)
        self.assertEqual(values["tool_errors"], 0)


if __name__ == "__main__":
    unittest.main()
