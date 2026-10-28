"""The end-to-end gate: run baseline, run candidate, compare, report."""

from __future__ import annotations

import json
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from . import compare as compare_mod
from .client import OpenAIClient, Target
from .report import write_reports
from .runner import CaseResult, Run, run_suite
from .suites import Suite, load_suite


@dataclass
class Workspace:
    root: Path

    def __post_init__(self) -> None:
        self.root = Path(self.root)
        self.runs_dir.mkdir(parents=True, exist_ok=True)
        self.reports_dir.mkdir(parents=True, exist_ok=True)

    @property
    def runs_dir(self) -> Path:
        return self.root / "runs"

    @property
    def reports_dir(self) -> Path:
        return self.root / "reports"

    def run_path(self, run_id: str) -> Path:
        return self.runs_dir / f"{run_id}.json"

    def resolve_run(self, ref: str) -> Path:
        candidate = Path(ref)
        if candidate.is_file():
            return candidate
        path = self.run_path(ref)
        if path.is_file():
            return path
        matches = sorted(self.runs_dir.glob(f"*{ref}*.json"))
        if len(matches) == 1:
            return matches[0]
        raise FileNotFoundError(f"run artifact not found: {ref}")


ProgressFn = Callable[[str, int, int, CaseResult], None]


def _default_progress(stage: str, index: int, total: int, result: CaseResult) -> None:
    mark = "PASS" if result.passed else ("ERR " if result.error else "FAIL")
    print(f"  [{stage}] {index:02d}/{total:02d} {result.id:<26} {mark} {result.latency_ms:6.0f} ms")


def _stamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def run_gate(
    baseline: Target,
    candidate: Target,
    suite: Suite,
    workspace: Workspace,
    *,
    gates: dict[str, float] | None = None,
    progress: ProgressFn | None = None,
    retries: int = 2,
    limit: int | None = None,
) -> dict[str, Any]:
    progress = progress or _default_progress
    print(f"Baseline:  {baseline.name} ({baseline.label}, {baseline.model})")
    base_run = run_suite(
        baseline,
        suite,
        client=OpenAIClient(baseline, retries=retries),
        limit=limit,
        progress=lambda i, n, r: progress("baseline", i, n, r),
    )
    print(f"Candidate: {candidate.name} ({candidate.label}, {candidate.model})")
    cand_run = run_suite(
        candidate,
        suite,
        client=OpenAIClient(candidate, retries=retries),
        limit=limit,
        progress=lambda i, n, r: progress("candidate", i, n, r),
    )

    comparison = compare_mod.compare_runs(base_run, cand_run, gates)

    out_dir = workspace.reports_dir / f"{_stamp()}-{suite.name}"
    out_dir.mkdir(parents=True, exist_ok=True)
    base_path = base_run.save(workspace.runs_dir)["run_json"]
    cand_path = cand_run.save(workspace.runs_dir)["run_json"]
    comparison_path = compare_mod.save_comparison(comparison, out_dir)
    report_paths = write_reports(base_run, cand_run, comparison, out_dir)

    return {
        "baseline": base_run,
        "candidate": cand_run,
        "comparison": comparison,
        "paths": {
            "baseline_run": base_path,
            "candidate_run": cand_path,
            "comparison": comparison_path,
            **report_paths,
            "report_dir": str(out_dir),
        },
    }


def run_single(
    target: Target,
    suite: Suite,
    workspace: Workspace,
    *,
    progress: ProgressFn | None = None,
    retries: int = 2,
    limit: int | None = None,
) -> Run:
    progress = progress or _default_progress
    run = run_suite(
        target,
        suite,
        client=OpenAIClient(target, retries=retries),
        limit=limit,
        progress=lambda i, n, r: progress("run", i, n, r),
    )
    run.meta["artifact"] = str(workspace.run_path(run.run_id))
    run.save(workspace.runs_dir)
    return run


def comparison_json(comparison: dict[str, Any]) -> str:
    return json.dumps(comparison, ensure_ascii=False, indent=2)


def wait_for_port(host: str, port: int, timeout: float = 5.0) -> bool:
    import socket

    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with socket.create_connection((host, port), timeout=0.4):
                return True
        except OSError:
            time.sleep(0.05)
    return False
