"""Baseline vs candidate comparison and release gates."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .runner import Run

SCHEMA = "cortensor-canary/comparison/v1"
DEFAULT_GATES: dict[str, float] = {
    "max_quality_drop_pp": 2.0,
    "max_p95_latency_increase_pct": 25.0,
    "max_error_rate_pct": 2.0,
}
SEVERITY = {"pass": 0, "warn": 1, "fail": 2}


def _pct_change(baseline: float, candidate: float) -> float:
    if baseline <= 0:
        return 0.0 if candidate <= 0 else 100.0
    return (candidate - baseline) / baseline * 100.0


def _check(name: str, status: str, value: float, gate: float, message: str, unit: str) -> dict[str, Any]:
    return {
        "name": name,
        "status": status,
        "value": round(value, 2),
        "gate": gate,
        "unit": unit,
        "message": message,
    }


def compare_runs(baseline: Run, candidate: Run, gates: dict[str, float] | None = None) -> dict[str, Any]:
    limits = {**DEFAULT_GATES, **(gates or {})}
    checks: list[dict[str, Any]] = []
    reasons: list[str] = []

    quality_delta_pp = (candidate.metrics["pass_rate"] - baseline.metrics["pass_rate"]) * 100
    quality_limit = -abs(float(limits["max_quality_drop_pp"]))
    if candidate.metrics["cases"] == 0:
        checks.append(_check("quality", "fail", 0.0, quality_limit, "candidate has no cases", "pp"))
        reasons.append("candidate run contains no cases")
    elif quality_delta_pp < quality_limit:
        status = "fail"
        message = f"pass rate dropped {abs(quality_delta_pp):.1f} pp (limit {abs(quality_limit):.1f})"
        reasons.append(message)
    else:
        status = "pass"
        message = f"pass rate change {quality_delta_pp:+.1f} pp"
    checks.append(_check("quality", status, quality_delta_pp, quality_limit, message, "pp"))

    p95_delta_pct = _pct_change(
        float(baseline.metrics["latency_ms"]["p95"]),
        float(candidate.metrics["latency_ms"]["p95"]),
    )
    latency_limit = abs(float(limits["max_p95_latency_increase_pct"]))
    if p95_delta_pct > latency_limit * 2:
        status = "fail"
        message = f"p95 latency grew {p95_delta_pct:+.1f}% (hard limit {latency_limit * 2:.0f}%)"
        reasons.append(message)
    elif p95_delta_pct > latency_limit:
        status = "warn"
        message = f"p95 latency grew {p95_delta_pct:+.1f}% (limit {latency_limit:.0f}%)"
        reasons.append(message)
    else:
        status = "pass"
        message = f"p95 latency change {p95_delta_pct:+.1f}%"
    checks.append(
        _check("latency_p95", status, p95_delta_pct, latency_limit, message, "%")
    )

    error_rate_pct = float(candidate.metrics["error_rate"]) * 100
    error_limit = abs(float(limits["max_error_rate_pct"]))
    if error_rate_pct > error_limit:
        status = "fail"
        message = f"{error_rate_pct:.1f}% of requests failed (limit {error_limit:.1f}%)"
        reasons.append(message)
    elif error_rate_pct > error_limit / 2:
        status = "warn"
        message = f"{error_rate_pct:.1f}% of requests failed (limit {error_limit:.1f}%)"
        reasons.append(message)
    else:
        status = "pass"
        message = f"error rate {error_rate_pct:.1f}%"
    checks.append(_check("errors", status, error_rate_pct, error_limit, message, "%"))

    suite_mismatch = baseline.suite.name != candidate.suite.name
    if suite_mismatch:
        checks.append(
            _check(
                "suite",
                "fail",
                0.0,
                0.0,
                f"suite mismatch: {baseline.suite.name!r} vs {candidate.suite.name!r}",
                "",
            )
        )
        reasons.append(
            f"suite mismatch: {baseline.suite.name!r} vs {candidate.suite.name!r}; "
            "comparison is not meaningful"
        )

    statuses = [check["status"] for check in checks]
    overall = "fail" if "fail" in statuses else "warn" if "warn" in statuses else "pass"

    base_cases = {case.id: case for case in baseline.cases}
    cand_cases = {case.id: case for case in candidate.cases}
    common = [case_id for case_id in base_cases if case_id in cand_cases]
    regressed = [case_id for case_id in common if base_cases[case_id].passed and not cand_cases[case_id].passed]
    fixed = [case_id for case_id in common if not base_cases[case_id].passed and cand_cases[case_id].passed]
    still_failing = [case_id for case_id in common if not base_cases[case_id].passed and not cand_cases[case_id].passed]
    only_baseline = sorted(set(base_cases) - set(cand_cases))
    only_candidate = sorted(set(cand_cases) - set(base_cases))

    comparison: dict[str, Any] = {
        "schema": SCHEMA,
        "created_at": datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z"),
        "status": overall.upper(),
        "gates": {key: float(value) for key, value in limits.items()},
        "baseline": _run_summary(baseline),
        "candidate": _run_summary(candidate),
        "deltas": {
            "pass_rate_pp": round(quality_delta_pp, 2),
            "score_pp": round((candidate.metrics["score"] - baseline.metrics["score"]) * 100, 2),
            "p50_latency_pct": round(
                _pct_change(baseline.metrics["latency_ms"]["p50"], candidate.metrics["latency_ms"]["p50"]), 2
            ),
            "p95_latency_pct": round(p95_delta_pct, 2),
            "error_rate_pp": round(
                (candidate.metrics["error_rate"] - baseline.metrics["error_rate"]) * 100, 2
            ),
            "tokens_total_pct": round(
                _pct_change(baseline.metrics["tokens"]["total"], candidate.metrics["tokens"]["total"]), 2
            ),
        },
        "checks": checks,
        "reasons": reasons,
        "cases": {
            "common": len(common),
            "regressed": regressed,
            "fixed": fixed,
            "still_failing": still_failing,
            "only_baseline": only_baseline,
            "only_candidate": only_candidate,
        },
    }
    comparison["verdict_text"] = render_verdict(comparison)
    return comparison


def _run_summary(run: Run) -> dict[str, Any]:
    return {
        "run_id": run.run_id,
        "target": run.target.name,
        "target_label": run.target.label,
        "model": run.target.model,
        "suite": run.suite.name,
        "metrics": run.metrics,
    }


def render_verdict(comparison: dict[str, Any]) -> str:
    baseline = comparison["baseline"]
    candidate = comparison["candidate"]
    icons = {"pass": "PASS", "warn": "WARN", "fail": "FAIL"}
    lines = [
        f"Cortensor Canary verdict: {comparison['status']}",
        f"Baseline:  {baseline['target']} / {baseline['model']} / {baseline['suite']} "
        f"({baseline['metrics']['cases']} cases)",
        f"Candidate: {candidate['target']} / {candidate['model']} / {candidate['suite']} "
        f"({candidate['metrics']['cases']} cases)",
        "",
    ]
    for check in comparison["checks"]:
        value = check["value"]
        gate = check["gate"]
        lines.append(
            f"  {check['name']:<12} {value:+8.1f}{check['unit']:<3} "
            f"(gate {gate:+.1f}{check['unit']})  {icons[check['status']]}"
        )
    regressed = comparison["cases"]["regressed"]
    fixed = comparison["cases"]["fixed"]
    lines.append("")
    lines.append(f"Regressions: {len(regressed)}" + (f" -> {', '.join(regressed)}" if regressed else ""))
    if fixed:
        lines.append(f"Fixed:       {len(fixed)} -> {', '.join(fixed)}")
    if comparison["reasons"]:
        lines.append("")
        lines.append("Reasons:")
        for reason in comparison["reasons"]:
            lines.append(f"  - {reason}")

    lines.append("")
    if comparison["status"] == "PASS":
        lines.append("Recommendation: promote the candidate. Quality, latency and error rate are within gates.")
    elif comparison["status"] == "WARN":
        lines.append("Recommendation: promote with monitoring, or rerun on a larger suite before switching traffic.")
    else:
        lines.append("Recommendation: do not promote. Keep the baseline and fix the failing cases first.")
    return "\n".join(lines)


def save_comparison(comparison: dict[str, Any], directory: str | Path, stem: str = "comparison") -> str:
    out = Path(directory)
    out.mkdir(parents=True, exist_ok=True)
    path = out / f"{stem}.json"
    path.write_text(json.dumps(comparison, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return str(path)


def load_comparison(path: str | Path) -> dict[str, Any]:
    payload = json.loads(Path(path).read_text(encoding="utf-8"))
    if payload.get("schema") != SCHEMA:
        raise ValueError(f"{path}: not a {SCHEMA} file")
    return payload
