from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from cortensor_canary.client import ChatResult, InferenceError, Target
from cortensor_canary.runner import (
    CaseResult,
    compute_metrics,
    load_run,
    percentile,
    run_suite,
)
from cortensor_canary.suites import Case, Suite


def make_suite() -> Suite:
    cases = (
        Case(id="ok", prompt="say ok", expect={"type": "exact", "value": "ok"}, tags=("basic",)),
        Case(id="math", prompt="2+2", expect={"type": "exact", "value": "4"}),
    )
    return Suite(name="unit", description="", version="1.0", cases=cases)


def make_target() -> Target:
    return Target(name="fake", base_url="http://127.0.0.1:1/v1", model="fake-1", label="Fake")


class FakeClient:
    def __init__(self, answers: dict[str, str], fail: set[str] | None = None, latency: float = 10.0):
        self.answers = answers
        self.fail = fail or set()
        self.latency = latency
        self.calls = 0

    def chat(self, messages, max_tokens=None):
        self.calls += 1
        prompt = messages[-1]["content"]
        key = "math" if "2+2" in prompt else "ok"
        if key in self.fail:
            raise InferenceError("boom", status=503, retryable=False)
        return ChatResult(
            text=self.answers[key],
            latency_ms=self.latency,
            prompt_tokens=10,
            completion_tokens=2,
            model="fake-1",
        )


class RunSuiteTest(unittest.TestCase):
    def test_all_pass(self):
        client = FakeClient({"ok": "ok", "math": "4"})
        run = run_suite(make_target(), make_suite(), client=client)
        self.assertEqual(run.metrics["passed"], 2)
        self.assertEqual(run.metrics["pass_rate"], 1.0)
        self.assertEqual(run.metrics["errors"], 0)
        self.assertEqual(run.metrics["tokens"]["total"], 24)
        self.assertEqual(client.calls, 2)

    def test_partial_and_errors(self):
        client = FakeClient({"ok": "nope", "math": "4"}, fail={"math"})
        run = run_suite(make_target(), make_suite(), client=client)
        self.assertEqual(run.metrics["passed"], 0)
        self.assertEqual(run.metrics["errors"], 1)
        self.assertIn("boom", run.cases[1].error)

    def test_limit(self):
        run = run_suite(make_target(), make_suite(), client=FakeClient({"ok": "ok", "math": "4"}), limit=1)
        self.assertEqual(len(run.cases), 1)

    def test_save_and_load_roundtrip(self):
        run = run_suite(make_target(), make_suite(), client=FakeClient({"ok": "ok", "math": "4"}))
        with tempfile.TemporaryDirectory() as tmp:
            path = run.save(tmp)["run_json"]
            loaded = load_run(path)
        self.assertEqual(loaded.run_id, run.run_id)
        self.assertEqual(loaded.target.name, "fake")
        self.assertEqual(loaded.suite.name, "unit")
        self.assertEqual(len(loaded.cases), 2)
        self.assertEqual(loaded.metrics["pass_rate"], 1.0)

    def test_progress_callback(self):
        seen = []
        run_suite(
            make_target(),
            make_suite(),
            client=FakeClient({"ok": "ok", "math": "4"}),
            progress=lambda i, n, result: seen.append((i, n, result.id)),
        )
        self.assertEqual(seen, [(1, 2, "ok"), (2, 2, "math")])


class MetricsTest(unittest.TestCase):
    def test_empty(self):
        metrics = compute_metrics([])
        self.assertEqual(metrics["cases"], 0)
        self.assertEqual(metrics["pass_rate"], 0.0)
        self.assertEqual(metrics["latency_ms"]["p95"], 0.0)

    def test_percentile(self):
        values = [10.0, 20.0, 30.0, 40.0]
        self.assertEqual(percentile(values, 0.0), 10.0)
        self.assertEqual(percentile(values, 1.0), 40.0)
        self.assertEqual(percentile([], 0.5), 0.0)

    def test_error_rate(self):
        cases = [
            CaseResult(id="a", prompt="", expected={}, passed=True),
            CaseResult(id="b", prompt="", expected={}, passed=False, error="x"),
        ]
        metrics = compute_metrics(cases)
        self.assertEqual(metrics["error_rate"], 0.5)


if __name__ == "__main__":
    unittest.main()
