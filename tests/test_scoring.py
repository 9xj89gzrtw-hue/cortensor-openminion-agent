from __future__ import annotations

import unittest

from cortensor_canary.scoring import ExpectationError, extract_json, normalize, score_answer


class NormalizeTest(unittest.TestCase):
    def test_lowercases_and_collapses_space(self):
        self.assertEqual(normalize("  Hello   WORLD.\n"), "hello world")

    def test_yo_folds_to_e(self):
        self.assertEqual(normalize("Ещё"), "еще")


class ExactTest(unittest.TestCase):
    def test_accepts_case_and_trailing_punctuation(self):
        result = score_answer("Canberra.", {"type": "exact", "value": "canberra"})
        self.assertTrue(result.passed)

    def test_accepts_one_of_list(self):
        expect = {"type": "exact", "value": ["391", "392"]}
        self.assertTrue(score_answer("391", expect).passed)
        self.assertFalse(score_answer("390", expect).passed)


class ContainsTest(unittest.TestCase):
    def test_all_mode(self):
        expect = {"type": "contains", "value": ["red", "blue", "yellow"]}
        self.assertTrue(score_answer("red, blue, yellow", expect).passed)
        self.assertFalse(score_answer("red and blue", expect).passed)

    def test_any_mode(self):
        expect = {"type": "contains", "value": ["refund", "billing"], "mode": "any"}
        self.assertTrue(score_answer("billing", expect).passed)


class RegexTest(unittest.TestCase):
    def test_matches(self):
        result = score_answer("red, blue, yellow", {"type": "regex", "value": r"^red.*yellow$"})
        self.assertTrue(result.passed)

    def test_invalid_regex_raises(self):
        with self.assertRaises(ExpectationError):
            score_answer("x", {"type": "regex", "value": "("})


class LabelTest(unittest.TestCase):
    LABELS = ["billing", "tech", "account", "delivery", "refund", "other"]

    def test_exact_label(self):
        result = score_answer("Billing", {"type": "label", "value": self.LABELS})
        self.assertTrue(result.passed)

    def test_label_in_short_phrase(self):
        result = score_answer("категория: refund", {"type": "label", "value": self.LABELS})
        self.assertTrue(result.passed)

    def test_ambiguous_labels_fail(self):
        result = score_answer("not billing, it is tech", {"type": "label", "value": self.LABELS})
        self.assertFalse(result.passed)

    def test_unknown_label_fails(self):
        result = score_answer("finance", {"type": "label", "value": self.LABELS})
        self.assertFalse(result.passed)


class JsonTest(unittest.TestCase):
    def test_plain_json(self):
        result = score_answer(
            '{"order_id": "A-1042", "total": 12990}',
            {"type": "json", "keys": ["order_id", "total"], "equals": {"order_id": "a-1042", "total": 12990}},
        )
        self.assertTrue(result.passed, result.detail)

    def test_fenced_json(self):
        text = '```json\n{"order_id": "4821"}\n```'
        result = score_answer(text, {"type": "json", "keys": ["order_id"], "equals": {"order_id": "4821"}})
        self.assertTrue(result.passed)

    def test_json_wrapped_in_prose(self):
        text = 'Sure! Here is the result: {"sentiment": "negative", "confidence": 0.9}'
        result = score_answer(text, {"type": "json", "keys": ["sentiment"], "equals": {"sentiment": "negative"}})
        self.assertTrue(result.passed)

    def test_missing_key(self):
        result = score_answer('{"order_id": "1"}', {"type": "json", "keys": ["order_id", "total"]})
        self.assertFalse(result.passed)
        self.assertIn("total", result.detail)

    def test_no_json(self):
        result = score_answer("no json here", {"type": "json", "keys": []})
        self.assertFalse(result.passed)

    def test_numeric_tolerance(self):
        result = score_answer('{"amount": 1290.0001}', {"type": "json", "equals": {"amount": 1290}})
        self.assertTrue(result.passed)

    def test_unordered_list(self):
        expect = {
            "type": "json",
            "keys": ["tags"],
            "equals": {"tags": ["billing", "payment"]},
            "unordered": True,
        }
        self.assertTrue(score_answer('{"tags": ["payment", "billing"]}', expect).passed)

    def test_boolean(self):
        expect = {"type": "json", "keys": ["escalate"], "equals": {"escalate": True}}
        self.assertTrue(score_answer('{"escalate": true}', expect).passed)
        self.assertFalse(score_answer('{"escalate": false}', expect).passed)

    def test_extract_json_prefers_fence(self):
        self.assertEqual(extract_json('text ```json\n{"a": 1}\n``` more'), {"a": 1})

    def test_extract_json_none(self):
        self.assertIsNone(extract_json("nothing"))


class UnknownExpectationTest(unittest.TestCase):
    def test_unknown_type(self):
        with self.assertRaises(ExpectationError):
            score_answer("x", {"type": "vibes"})


if __name__ == "__main__":
    unittest.main()
