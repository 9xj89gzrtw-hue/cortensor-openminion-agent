"""Suite execution and run artifacts."""

from __future__ import annotations

import json
import platform
import statistics
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterable

from . import __version__
from .client import ChatResult, InferenceError, OpenAIClient, Target
from .scoring import ExpectationError, score_answer
from .suites import Suite

SCHEMA = "cortensor-canary/run/v1"


def utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def make_run_id(target: Target, suite: Suite, stamp: datetime | None = None) -> str:
    moment = stamp or datetime.now(timezone.utc)
    safe_target = "".join(ch if ch.isalnum() or ch in "-_" else "-" for ch in target.name)
    return f"{moment.strftime('%Y%m%dT%H%M%SZ')}-{safe_target}-{suite.name}"


@dataclass
class CaseResult:
    id: str
    prompt: str
    expected: dict[str, Any]
    response: str = ""
    passed: bool = False
    score: float = 0.0
    detail: str = ""
    latency_ms: float = 0.0
    prompt_tokens: int = 0
    completion_tokens: int = 0
    attempts: int = 0
    error: str = ""
    tags: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "tags": self.tags,
            "prompt": self.prompt,
            "expected": self.expected,
            "response": self.response,
            "passed": self.passed,
            "score": round(self.score, 4),
            "detail": self.detail,
            "latency_ms": round(self.latency_ms, 1),
            "prompt_tokens": self.prompt_tokens,
            "completion_tokens": self.completion_tokens,
            "attempts": self.attempts,
            "error": self.error,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "CaseResult":
        return cls(
            id=str(data["id"]),
            prompt=str(data.get("prompt", "")),
            expected=dict(data.get("expected") or {}),
            response=str(data.get("response", "")),
            passed=bool(data.get("passed")),
            score=float(data.get("score", 0.0)),
            detail=str(data.get("detail", "")),
            latency_ms=float(data.get("latency_ms", 0.0)),
            prompt_tokens=int(data.get("prompt_tokens", 0)),
            completion_tokens=int(data.get("completion_tokens", 0)),
            attempts=int(data.get("attempts", 0)),
            error=str(data.get("error", "")),
            tags=[str(tag) for tag in (data.get("tags") or [])],
        )


def percentile(values: list[float], fraction: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    if len(ordered) == 1:
        return float(ordered[0])
    index = max(0, min(len(ordered) - 1, int(round(fraction * (len(ordered) - 1)))))
    return float(ordered[index])


def compute_metrics(cases: Iterable[CaseResult]) -> dict[str, Any]:
    items = list(cases)
    total = len(items)
    passed = sum(1 for item in items if item.passed)
    errors = sum(1 for item in items if item.error)
    latencies = [item.latency_ms for item in items if item.latency_ms > 0]
    prompt_tokens = sum(item.prompt_tokens for item in items)
    completion_tokens = sum(item.completion_tokens for item in items)
    return {
        "cases": total,
        "passed": passed,
        "failed": total - passed,
        "errors": errors,
        "pass_rate": round(passed / total, 4) if total else 0.0,
        "score": round(sum(item.score for item in items) / total, 4) if total else 0.0,
        "error_rate": round(errors / total, 4) if total else 0.0,
        "latency_ms": {
            "p50": round(percentile(latencies, 0.50), 1),
            "p95": round(percentile(latencies, 0.95), 1),
            "mean": round(statistics.fmean(latencies), 1) if latencies else 0.0,
            "min": round(min(latencies), 1) if latencies else 0.0,
            "max": round(max(latencies), 1) if latencies else 0.0,
        },
        "tokens": {
            "prompt": prompt_tokens,
            "completion": completion_tokens,
            "total": prompt_tokens + completion_tokens,
        },
    }


@dataclass
class Run:
    run_id: str
    target: Target
    suite: Suite
    cases: list[CaseResult]
    started_at: str
    finished_at: str
    duration_ms: float
    metrics: dict[str, Any]
    meta: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": SCHEMA,
            "run_id": self.run_id,
            "created_at": self.finished_at,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "duration_ms": round(self.duration_ms, 1),
            "target": self.target.public_dict(),
            "suite": self.suite.to_dict(),
            "metrics": self.metrics,
            "cases": [case.to_dict() for case in self.cases],
            "meta": self.meta,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Run":
        target = Target.from_mapping(str(data["target"].get("name") or "loaded"), data["target"])
        suite_data = data.get("suite") or {}
        suite = Suite(
            name=str(suite_data.get("name") or "unknown"),
            description=str(suite_data.get("description") or ""),
            version=str(suite_data.get("version") or "1.0"),
            cases=(),
            path=str(suite_data.get("path") or ""),
        )
        return cls(
            run_id=str(data["run_id"]),
            target=target,
            suite=suite,
            cases=[CaseResult.from_dict(item) for item in data.get("cases", [])],
            started_at=str(data.get("started_at") or data.get("created_at") or ""),
            finished_at=str(data.get("finished_at") or data.get("created_at") or ""),
            duration_ms=float(data.get("duration_ms") or 0.0),
            metrics=dict(data.get("metrics") or {}),
            meta=dict(data.get("meta") or {}),
        )

    def save(self, directory: str | Path) -> dict[str, str]:
        out = Path(directory)
        out.mkdir(parents=True, exist_ok=True)
        path = out / f"{self.run_id}.json"
        path.write_text(json.dumps(self.to_dict(), ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        return {"run_json": str(path)}


def load_run(path: str | Path) -> Run:
    payload = json.loads(Path(path).read_text(encoding="utf-8"))
    if payload.get("schema") != SCHEMA:
        raise ValueError(f"{path}: not a {SCHEMA} file")
    return Run.from_dict(payload)


def run_suite(
    target: Target,
    suite: Suite,
    *,
    client: OpenAIClient | None = None,
    limit: int | None = None,
    progress: Callable[[int, int, CaseResult], None] | None = None,
    retries: int = 2,
) -> Run:
    client = client or OpenAIClient(target, retries=retries)
    cases = list(suite.cases)
    if limit is not None:
        cases = cases[: max(0, limit)]
    results: list[CaseResult] = []
    started_at = utc_now()
    started = time.perf_counter()

    for index, case in enumerate(cases, start=1):
        messages: list[dict[str, str]] = []
        if case.system:
            messages.append({"role": "system", "content": case.system})
        messages.append({"role": "user", "content": case.prompt})

        result = CaseResult(
            id=case.id,
            prompt=case.prompt,
            expected=case.expect,
            tags=list(case.tags),
        )
        try:
            chat: ChatResult = client.chat(messages, max_tokens=case.max_tokens)
            result.response = chat.text
            result.latency_ms = chat.latency_ms
            result.prompt_tokens = chat.prompt_tokens
            result.completion_tokens = chat.completion_tokens
            result.attempts = chat.attempts
            scored = score_answer(chat.text, case.expect)
            result.passed = scored.passed
            result.score = scored.score
            result.detail = scored.detail
        except (InferenceError, ExpectationError) as exc:
            result.error = str(exc)[:500]
            result.detail = "request failed"
        except Exception as exc:  # noqa: BLE001 - one bad case must not kill the run
            result.error = f"{type(exc).__name__}: {exc}"[:500]
            result.detail = "unexpected error"

        results.append(result)
        if progress:
            progress(index, len(cases), result)

    duration_ms = (time.perf_counter() - started) * 1000
    return Run(
        run_id=make_run_id(target, suite),
        target=target,
        suite=suite,
        cases=results,
        started_at=started_at,
        finished_at=utc_now(),
        duration_ms=duration_ms,
        metrics=compute_metrics(results),
        meta={
            "tool": f"cortensor-canary/{__version__}",
            "python": sys.version.split()[0],
            "platform": platform.platform(),
        },
    )
