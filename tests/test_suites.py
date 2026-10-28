from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from cortensor_canary.suites import SuiteError, list_suites, load_suite


class BundledSuitesTest(unittest.TestCase):
    def test_all_bundled_suites_load(self):
        items = list_suites()
        self.assertGreaterEqual(len(items), 3)
        names = {item["name"] for item in items}
        self.assertEqual(names, {"smoke", "support-ru", "structured"})
        for item in items:
            self.assertGreater(item["cases"], 0)

    def test_smoke_cases_are_unique(self):
        suite = load_suite("smoke")
        ids = [case.id for case in suite.cases]
        self.assertEqual(len(ids), len(set(ids)))
        self.assertEqual(suite.version, "1.0")

    def test_suite_has_expectations(self):
        for name in ("smoke", "support-ru", "structured"):
            suite = load_suite(name)
            for case in suite.cases:
                self.assertIn("type", case.expect)
                self.assertGreater(case.max_tokens, 0)

    def test_unknown_suite_lists_available(self):
        with self.assertRaises(SuiteError) as ctx:
            load_suite("does-not-exist")
        self.assertIn("smoke", str(ctx.exception))


class CustomSuiteTest(unittest.TestCase):
    def test_load_from_path(self):
        payload = {
            "name": "mini",
            "version": "2.0",
            "cases": [{"id": "one", "prompt": "Say 1", "expect": {"type": "exact", "value": "1"}}],
        }
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "mini.json"
            path.write_text(json.dumps(payload), encoding="utf-8")
            suite = load_suite(path)
        self.assertEqual(suite.name, "mini")
        self.assertEqual(len(suite.cases), 1)
        self.assertEqual(suite.cases[0].max_tokens, 256)

    def test_duplicate_ids_rejected(self):
        payload = {
            "name": "broken",
            "cases": [
                {"id": "same", "prompt": "a", "expect": {"type": "exact", "value": "a"}},
                {"id": "same", "prompt": "b", "expect": {"type": "exact", "value": "b"}},
            ],
        }
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "broken.json"
            path.write_text(json.dumps(payload), encoding="utf-8")
            with self.assertRaises(SuiteError):
                load_suite(path)

    def test_missing_prompt_rejected(self):
        payload = {"name": "broken", "cases": [{"id": "x", "expect": {"type": "exact", "value": "a"}}]}
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "broken.json"
            path.write_text(json.dumps(payload), encoding="utf-8")
            with self.assertRaises(SuiteError):
                load_suite(path)

    def test_bad_expectation_rejected(self):
        payload = {
            "name": "broken",
            "cases": [{"id": "x", "prompt": "a", "expect": {"type": "telepathy"}}],
        }
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "broken.json"
            path.write_text(json.dumps(payload), encoding="utf-8")
            with self.assertRaises(SuiteError):
                load_suite(path)


if __name__ == "__main__":
    unittest.main()
