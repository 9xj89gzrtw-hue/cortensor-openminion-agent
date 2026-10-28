"""Deterministic answer scoring.

No LLM judge: every check can be reproduced offline, which is what makes the
canary usable as a release gate. Supported expectations:

    {"type": "exact",    "value": "391"}
    {"type": "contains", "value": ["red", "blue"], "mode": "all"}
    {"type": "regex",    "value": "^\\\\d{3}$"}
    {"type": "label",    "value": ["billing", "tech"]}
    {"type": "json",     "keys": ["order_id", "total"], "equals": {"order_id": "A-1042"}}
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any, Mapping

_EDGE = " \t\r\n.,;:!?\"'«»()[]{}"
_PUNCT_RE = re.compile(r"\s+")


class ExpectationError(ValueError):
    """Malformed expectation inside a suite file."""


@dataclass
class Score:
    passed: bool
    score: float
    detail: str


def normalize(text: Any) -> str:
    value = str(text or "").lower().replace("ё", "е")
    value = _PUNCT_RE.sub(" ", value).strip()
    return value.strip(_EDGE)


def extract_json(text: str) -> Any:
    """Return the first JSON value found in ``text``.

    Handles bare JSON, fenced blocks and the common "prose then JSON" shape
    produced by small models.
    """
    if text is None:
        return None
    raw = str(text).strip()
    if not raw:
        return None

    fence = re.search(r"```(?:json)?\s*(.+?)```", raw, flags=re.S | re.I)
    if fence:
        raw = fence.group(1).strip()

    try:
        return json.loads(raw)
    except ValueError:
        pass

    decoder = json.JSONDecoder()
    for idx, char in enumerate(raw):
        if char not in "{[":
            continue
        try:
            value, _ = decoder.raw_decode(raw[idx:])
        except ValueError:
            continue
        if isinstance(value, (dict, list)):
            return value
    return None


def _as_list(value: Any) -> list[str]:
    if isinstance(value, (list, tuple)):
        return [str(item) for item in value]
    return [str(value)]


def _score_exact(text: str, expected: Any) -> Score:
    options = [normalize(item) for item in _as_list(expected)]
    got = normalize(text)
    if got in options:
        return Score(True, 1.0, f"exact match: {got!r}")
    return Score(False, 0.0, f"expected one of {options}, got {got!r}")


def _score_contains(text: str, expected: Any, mode: str) -> Score:
    needles = [normalize(item) for item in _as_list(expected)]
    haystack = normalize(text)
    found = [item for item in needles if item in haystack]
    if mode == "any":
        passed = bool(found)
    else:
        passed = len(found) == len(needles)
    missing = [item for item in needles if item not in found]
    detail = f"found {found!r}"
    if missing and not passed:
        detail += f", missing {missing!r}"
    return Score(passed, 1.0 if passed else 0.0, detail)


def _score_regex(text: str, pattern: str) -> Score:
    try:
        compiled = re.compile(pattern, flags=re.I | re.S)
    except re.error as exc:
        raise ExpectationError(f"invalid regex {pattern!r}: {exc}") from exc
    match = compiled.search(str(text))
    if match:
        return Score(True, 1.0, f"matched {match.group(0)[:80]!r}")
    return Score(False, 0.0, f"no match for {pattern!r}")


def _score_label(text: str, expected: Any) -> Score:
    labels = [normalize(item) for item in _as_list(expected)]
    got = normalize(text)
    if got in labels:
        return Score(True, 1.0, f"label {got!r}")
    # Accept "billing" inside a short phrase, but never accept two labels at once.
    present = [label for label in labels if re.search(rf"\b{re.escape(label)}\b", got)]
    if len(present) == 1 and len(got.split()) <= 4:
        return Score(True, 1.0, f"label {present[0]!r} in {got!r}")
    if len(present) > 1:
        return Score(False, 0.0, f"ambiguous labels {present!r}")
    return Score(False, 0.0, f"expected one of {labels}, got {got!r}")


def _score_json(text: str, expected: Mapping[str, Any]) -> Score:
    parsed = extract_json(text)
    if parsed is None:
        return Score(False, 0.0, "no JSON value found")
    if not isinstance(parsed, dict):
        return Score(False, 0.0, f"expected a JSON object, got {type(parsed).__name__}")
    keys = [str(k) for k in expected.get("keys", [])]
    missing = [key for key in keys if key not in parsed]
    if missing:
        return Score(False, 0.0, f"missing keys: {missing}")
    equals = dict(expected.get("equals") or {})
    for key, want in equals.items():
        got = parsed.get(key)
        if isinstance(want, list):
            if not isinstance(got, list):
                return Score(False, 0.0, f"{key}: expected a list, got {got!r}")
            got_norm = [normalize(x) for x in got]
            want_norm = [normalize(x) for x in want]
            if expected.get("unordered"):
                ok = sorted(got_norm) == sorted(want_norm)
            else:
                ok = got_norm == want_norm
            if not ok:
                return Score(False, 0.0, f"{key}: expected {want!r}, got {got!r}")
        elif isinstance(want, bool):
            if bool(got) is not want:
                return Score(False, 0.0, f"{key}: expected {want}, got {got!r}")
        elif isinstance(want, (int, float)) and not isinstance(want, bool):
            try:
                delta = abs(float(got) - float(want))
            except (TypeError, ValueError):
                return Score(False, 0.0, f"{key}: expected number {want}, got {got!r}")
            # Relative tolerance: small rounding drift in money amounts is fine,
            # a wrong integer is not.
            tolerance = max(1e-6, abs(float(want)) * 1e-4)
            if delta > tolerance:
                return Score(False, 0.0, f"{key}: expected {want}, got {got!r}")
        elif normalize(got) != normalize(want):
            return Score(False, 0.0, f"{key}: expected {want!r}, got {got!r}")
    return Score(True, 1.0, f"JSON ok: keys={keys}")


def score_answer(text: str, expect: Mapping[str, Any]) -> Score:
    if not isinstance(expect, Mapping):
        raise ExpectationError(f"expectation must be an object, got {type(expect).__name__}")
    kind = str(expect.get("type", "")).strip().lower()
    if kind == "exact":
        return _score_exact(text, expect.get("value", ""))
    if kind == "contains":
        return _score_contains(text, expect.get("value", ""), str(expect.get("mode", "all")).lower())
    if kind == "regex":
        return _score_regex(text, str(expect.get("value", "")))
    if kind == "label":
        return _score_label(text, expect.get("value", []))
    if kind == "json":
        return _score_json(text, expect)
    raise ExpectationError(f"unknown expectation type: {kind!r}")
