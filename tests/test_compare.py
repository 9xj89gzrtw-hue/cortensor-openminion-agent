from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from cortensor_canary.client import Target
from cortensor_canary.compare import (
    DEFAULT_GATES,
    compare_runs,
    load_comparison,
    render_verdict,
    save_comparison,
)
from cortensor_canary.runner import CaseResult, Run, compute_metrics
from cortensor_canary.suites import Case, Suite


def make_run(
    name: str = "run",
    passed: tuple[bool, ...] = (True, True),
    latency: float = 100.0,
    errors: int = 0,
    suite_name: str = "smoke",
) -> Run:
    cases = []
    for index, ok in enumerate(passed):
        cases.append(
            CaseResult(
                id=f"case-{index}",
                prompt="",
                expected={},
                passed=ok,
                score=1.0 if ok else 0.0,
                latency_ms=latency,
                prompt_tokens=10,
                completion_tokens=5,
                error="failed" if (not ok and index < errors) else "",
            )
        )
    suite = Suite(
        name=suite_name,
        description="",
        version="1.0",
        cases=tuple(
            Case(id=f"case-{index}", prompt="", expect={"type": "exact", "value": ""})
            for index in range(len(passed))
        ),
    )
    target = Target(name=name, base_url="http://localhost/v1", model="m", label=name)
    return Run(
        run_id=f"{name}-id",
        target=target,
        suite=suite,
        cases=cases,
        started_at="",
        finished_at="",
        duration_ms=0.0,
        metrics=compute_metrics(cases),
    )


class CompareTest(unittest.TestCase):
    def test_identical_is_pass(self):
        comparison = compare_runs(make_run("base"), make_run("cand"))
        self.assertEqual(comparison["status"], "PASS")
        self.assertEqual(comparison["deltas"]["pass_rate_pp"], 0.0)
        self.assertIn("Recommendation: promote", comparison["verdict_text"])

    def test_quality_drop_fails(self):
        comparison = compare_runs(make_run(passed=(True, True)), make_run(passed=(True, False)))
        self.assertEqual(comparison["status"], "FAIL")
        self.assertEqual(comparison["deltas"]["pass_rate_pp"], -50.0)
        self.assertIn("case-1", comparison["cases"]["regressed"])
        self.assertIn("pass rate dropped", comparison["verdict_text"])

    def test_quality_within_gate_passes(self):
        base = make_run(passed=tuple([True] * 100))
        cand = make_run(passed=tuple([True] * 99 + [False]))
        comparison = compare_runs(base, cand)
        self.assertEqual(comparison["status"], "PASS")

    def test_latency_warn(self):
        comparison = compare_runs(make_run(latency=100), make_run(latency=130))
        self.assertEqual(comparison["status"], "WARN")
        self.assertIn("latency", comparison["reasons"][0])

    def test_latency_hard_fail(self):
        comparison = compare_runs(make_run(latency=100), make_run(latency=200))
        self.assertEqual(comparison["status"], "FAIL")

    def test_error_rate_fails(self):
        comparison = compare_runs(
            make_run(passed=(True, True)),
            make_run(passed=(False, True), errors=1),
        )
        self.assertEqual(comparison["status"], "FAIL")
        self.assertEqual(comparison["deltas"]["error_rate_pp"], 50.0)

    def test_suite_mismatch_fails(self):
        comparison = compare_runs(make_run(suite_name="smoke"), make_run(suite_name="support-ru"))
        self.assertEqual(comparison["status"], "FAIL")
        self.assertTrue(any("suite mismatch" in reason for reason in comparison["reasons"]))

    def test_custom_gates(self):
        comparison = compare_runs(
            make_run(passed=(True, True)),
            make_run(passed=(True, False)),
            gates={"max_quality_drop_pp": 60.0},
        )
        self.assertEqual(comparison["status"], "PASS")

    def test_save_and_load(self):
        comparison = compare_runs(make_run(), make_run())
        with tempfile.TemporaryDirectory() as tmp:
            path = save_comparison(comparison, tmp)
            loaded = load_comparison(path)
            raw = json.loads(Path(path).read_text(encoding="utf-8"))
        self.assertEqual(loaded["status"], "PASS")
        self.assertEqual(raw["schema"], "cortensor-canary/comparison/v1")

    def test_render_verdict_mentions_both_targets(self):
        comparison = compare_runs(make_run("portal"), make_run("router"))
        text = render_verdict(comparison)
        self.assertIn("portal", text)
        self.assertIn("router", text)

    def test_default_gates_sane(self):
        self.assertEqual(DEFAULT_GATES["max_quality_drop_pp"], 2.0)
        self.assertEqual(DEFAULT_GATES["max_error_rate_pct"], 2.0)


if __name__ == "__main__":
    unittest.main()
