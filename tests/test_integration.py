from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from cortensor_canary.client import OpenAIClient, Target
from cortensor_canary.mock import MockServer
from cortensor_canary.report import render_html, render_markdown, write_reports
from cortensor_canary.compare import compare_runs
from cortensor_canary.pipeline import Workspace, run_gate
from cortensor_canary.runner import run_suite
from cortensor_canary.suites import load_suite

ROOT = Path(__file__).resolve().parents[1]


def target(server: MockServer, name: str = "mock") -> Target:
    return Target(
        name=name,
        base_url=server.base_url,
        model="mock-good",
        label=name,
        api_key_env="CANARY_MOCK_KEY",
        timeout=10,
    )


class MockEndToEndTest(unittest.TestCase):
    def test_good_profile_passes_smoke(self):
        with MockServer("good") as server:
            run = run_suite(target(server), load_suite("smoke"), retries=0)
        self.assertEqual(run.metrics["pass_rate"], 1.0, [c.detail for c in run.cases if not c.passed])
        self.assertEqual(run.metrics["errors"], 0)

    def test_degraded_profile_fails_instruction_case(self):
        with MockServer("degraded") as server:
            run = run_suite(target(server), load_suite("smoke"), retries=0)
        self.assertLess(run.metrics["pass_rate"], 1.0)
        failed = {case.id for case in run.cases if not case.passed}
        self.assertIn("instruction-three-colors", failed)

    def test_degraded_profile_produces_errors(self):
        with MockServer("degraded") as server:
            run = run_suite(target(server), load_suite("structured"), retries=0)
        self.assertGreaterEqual(run.metrics["errors"], 1)

    def test_client_lists_models(self):
        with MockServer("good") as server:
            models = OpenAIClient(target(server)).list_models()
        self.assertEqual(models, ["mock-good"])

    def test_gate_artifacts(self):
        with MockServer("good") as base_server, MockServer("degraded") as cand_server:
            with tempfile.TemporaryDirectory() as tmp:
                workspace = Workspace(Path(tmp))
                gate = run_gate(
                    target(base_server, "base"),
                    target(cand_server, "cand"),
                    load_suite("smoke"),
                    workspace,
                    progress=lambda *args: None,
                    retries=0,
                )
                comparison = gate["comparison"]
                self.assertEqual(comparison["status"], "FAIL")
                self.assertTrue(Path(gate["paths"]["report_html"]).is_file())
                self.assertTrue(Path(gate["paths"]["report_md"]).is_file())
                self.assertTrue(Path(gate["paths"]["comparison"]).is_file())
                report = Path(gate["paths"]["report_html"]).read_text(encoding="utf-8")
                self.assertIn("FAIL", report)
                self.assertIn("instruction-three-colors", report)


class ReportTest(unittest.TestCase):
    def _comparison(self):
        with MockServer("good") as base_server, MockServer("degraded") as cand_server:
            base = run_suite(target(base_server, "base"), load_suite("smoke"), retries=0)
            cand = run_suite(target(cand_server, "cand"), load_suite("smoke"), retries=0)
        return base, cand, compare_runs(base, cand)

    def test_markdown_contains_verdict_and_tables(self):
        base, cand, comparison = self._comparison()
        md = render_markdown(base, cand, comparison)
        self.assertIn("# Cortensor Canary report", md)
        self.assertIn("## Gates", md)
        self.assertIn("instruction-three-colors", md)

    def test_html_is_self_contained(self):
        base, cand, comparison = self._comparison()
        html = render_html(base, cand, comparison)
        self.assertIn("<!doctype html>", html)
        self.assertIn("<style>", html)
        self.assertNotIn("<script", html)
        self.assertIn("Why this verdict", html)

    def test_html_escapes_labels(self):
        base, cand, comparison = self._comparison()
        base.target = Target(
            name="x",
            base_url="http://localhost/v1",
            model="m",
            label="<script>alert(1)</script>",
        )
        html = render_html(base, cand, comparison)
        self.assertNotIn("<script>alert(1)</script>", html)
        self.assertIn("&lt;script&gt;", html)

    def test_write_reports(self):
        base, cand, comparison = self._comparison()
        with tempfile.TemporaryDirectory() as tmp:
            paths = write_reports(base, cand, comparison, tmp)
            self.assertTrue(Path(paths["report_html"]).is_file())
            self.assertTrue(Path(paths["report_md"]).is_file())


class CliDemoTest(unittest.TestCase):
    def test_demo_runs_offline(self):
        with tempfile.TemporaryDirectory() as tmp:
            env = {**os.environ, "PYTHONPATH": str(ROOT / "src")}
            proc = subprocess.run(
                [sys.executable, "-m", "cortensor_canary", "demo", "--workspace", tmp],
                cwd=ROOT,
                env=env,
                capture_output=True,
                text=True,
                timeout=120,
            )
            self.assertEqual(proc.returncode, 0, proc.stderr)
            self.assertIn("Cortensor Canary verdict: FAIL", proc.stdout)
            reports = list(Path(tmp, "reports").glob("*/report.html"))
            self.assertEqual(len(reports), 1)
            self.assertIn("FAIL", reports[0].read_text(encoding="utf-8"))

    def test_suites_command(self):
        env = {**os.environ, "PYTHONPATH": str(ROOT / "src")}
        proc = subprocess.run(
            [sys.executable, "-m", "cortensor_canary", "suites"],
            cwd=ROOT,
            env=env,
            capture_output=True,
            text=True,
            timeout=60,
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("support-ru", proc.stdout)


if __name__ == "__main__":
    unittest.main()
