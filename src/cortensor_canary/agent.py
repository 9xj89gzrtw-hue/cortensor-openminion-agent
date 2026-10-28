"""OpenMinion agent integration.

The agent is deliberately thin: every number it reports comes from a tool call
into the deterministic runner. The model plans and explains; it cannot make up
a pass rate.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from . import compare as compare_mod
from .client import Target, load_targets
from .pipeline import Workspace, run_gate, run_single
from .report import write_reports
from .runner import load_run
from .suites import list_suites, load_suite

INSTRUCTIONS = """You are Cortensor Canary, an inference release-gate agent.

Your job: decide whether a candidate inference endpoint can replace the baseline
for a specific workload.

Workflow:
1. Call list_targets to see configured targets and suites.
2. Call run_eval once for the baseline and once for the candidate. Use the SAME suite.
3. Call compare_runs with the two run ids (or artifact paths).
4. Call gate_verdict with the comparison path.
5. Reply with the deterministic verdict, then at most 5 short bullets of context.

Rules:
- Never invent pass rates, latencies, regressions or a verdict. Use only tool output.
- Never change gate thresholds. If the verdict is FAIL, say FAIL.
- If a tool returns an error, report it and stop.
- Keep the final answer under 200 words.
"""

MOCK_API_KEY_ENV = "CANARY_MOCK_KEY"


class AgentUnavailable(RuntimeError):
    """OpenMinion is not installed or no provider is configured."""


def openminion_available() -> bool:
    try:
        import openminion  # noqa: F401
    except Exception:  # noqa: BLE001
        return False
    return True


def write_mock_config(path: str | Path, base_url: str, model: str = "mock-good") -> Path:
    """Minimal OpenMinion config that points at the deterministic mock server."""
    config = {
        "default_agent": "canary",
        "agents": {
            "canary": {
                "name": "canary",
                "provider": "openai",
                "default_channel": "console",
                "model_connections": {
                    "mock": {
                        "provider": "openai",
                        "default": True,
                        "models": [model],
                        "provider_config_overrides": {
                            "api_key_env": MOCK_API_KEY_ENV,
                            "base_url": base_url,
                            "timeout_seconds": 60,
                        },
                    }
                },
            }
        },
        "providers": {
            "openai": {
                "api_key_env": MOCK_API_KEY_ENV,
                "base_url": base_url,
                "model": model,
                "timeout_seconds": 60,
                "tool_call_strategy": "native",
            }
        },
        "enabled_channels": ["console"],
        "channel_authenticity": {
            "allowed_algorithms": ["hmac-sha256"],
            "max_age_seconds": 300,
            "mode": "warn",
            "required_channels": [],
            "secret_env_by_channel": {},
            "trusted_channels": ["console"],
        },
        "channel_policy": {
            "dm_allowlist": [],
            "dm_policy": "pairing",
            "group_allowlist": [],
            "group_policy": "disabled",
            "paired_dm_senders": [],
        },
        "runtime": {"log_level": "ERROR"},
    }
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(config, indent=2) + "\n", encoding="utf-8")
    return target


def _json(payload: dict[str, Any]) -> str:
    return json.dumps(payload, ensure_ascii=False, sort_keys=True)


def make_tools(workspace: Workspace, targets: dict[str, Target] | None = None):
    """Build decorated OpenMinion tools bound to a workspace."""
    try:
        from openminion import tool
    except Exception as exc:  # noqa: BLE001
        raise AgentUnavailable(f"openminion is not installed: {exc}") from exc

    targets_file = workspace.root / "targets.json"
    if targets is None:
        targets = load_targets(targets_file) if targets_file.is_file() else {}

    @tool(min_scope="READ_ONLY")
    def list_targets() -> str:
        """List configured inference targets and available evaluation suites."""
        payload = {
            "targets": [
                {
                    "name": target.name,
                    "label": target.label,
                    "model": target.model,
                    "base_url": target.base_url,
                }
                for target in targets.values()
            ],
            "suites": [item["name"] for item in list_suites()],
        }
        if not targets:
            payload["error"] = f"no targets configured in {targets_file}"
        return _json(payload)

    @tool(min_scope="READ_ONLY")
    def run_eval(target: str, suite: str = "smoke") -> str:
        """Run an evaluation suite against a configured target and return metrics plus the artifact path."""
        if target not in targets:
            return _json({"error": f"unknown target {target!r}", "known": sorted(targets)})
        try:
            suite_obj = load_suite(suite)
            run = run_single(targets[target], suite_obj, workspace)
        except Exception as exc:  # noqa: BLE001
            return _json({"error": f"{type(exc).__name__}: {exc}"})
        return _json(
            {
                "run_id": run.run_id,
                "path": str(workspace.run_path(run.run_id)),
                "target": run.target.name,
                "suite": run.suite.name,
                "cases": run.metrics["cases"],
                "passed": run.metrics["passed"],
                "pass_rate": run.metrics["pass_rate"],
                "p95_latency_ms": run.metrics["latency_ms"]["p95"],
                "errors": run.metrics["errors"],
            }
        )

    @tool(min_scope="READ_ONLY")
    def compare_runs(baseline_run: str, candidate_run: str) -> str:
        """Compare two run artifacts and return gate status, reasons and the comparison path."""
        try:
            baseline = load_run(workspace.resolve_run(baseline_run))
            candidate = load_run(workspace.resolve_run(candidate_run))
            comparison = compare_mod.compare_runs(baseline, candidate)
            out_dir = workspace.reports_dir / f"{baseline_run}--{candidate_run}".replace("/", "_")
            out_dir.mkdir(parents=True, exist_ok=True)
            path = compare_mod.save_comparison(comparison, out_dir)
            reports = write_reports(baseline, candidate, comparison, out_dir)
        except Exception as exc:  # noqa: BLE001
            return _json({"error": f"{type(exc).__name__}: {exc}"})
        return _json(
            {
                "status": comparison["status"],
                "comparison_path": path,
                "report_html": reports["report_html"],
                "report_md": reports["report_md"],
                "deltas": comparison["deltas"],
                "reasons": comparison["reasons"],
                "regressed": comparison["cases"]["regressed"],
            }
        )

    @tool(min_scope="READ_ONLY")
    def gate_verdict(comparison_path: str) -> str:
        """Return the deterministic verdict text from a saved comparison JSON."""
        try:
            comparison = compare_mod.load_comparison(comparison_path)
        except Exception as exc:  # noqa: BLE001
            return f"error: {type(exc).__name__}: {exc}"
        return str(comparison["verdict_text"])

    return [list_targets, run_eval, compare_runs, gate_verdict]


def resolve_config_path(config_path: str | None = None) -> str | None:
    if config_path:
        return config_path
    env = os.environ.get("OPENMINION_CONFIG")
    if env:
        return env
    default = Path.home() / ".openminion" / "agents.json"
    if default.is_file():
        return str(default)
    return None


def run_agent(
    objective: str,
    workspace: Workspace,
    *,
    config_path: str | None = None,
    targets: dict[str, Target] | None = None,
    timeout: float = 900.0,
    instructions: str | None = None,
):
    """Run one OpenMinion agent turn. Returns ``AgentRunResult``."""
    if not openminion_available():
        raise AgentUnavailable(
            "openminion is not installed for this interpreter. "
            "Run: python3.11 -m pip install 'openminion>=0.1.14', then "
            "PYTHON=python3.11 ./canary agent ..."
        )
    from openminion import Agent, APIRuntime

    config = resolve_config_path(config_path)
    runtime = APIRuntime.from_config_path(config, home_root=workspace.root / ".openminion")
    try:
        tools = make_tools(workspace, targets)
        with Agent(
            runtime=runtime,
            name="canary",
            instructions=instructions or INSTRUCTIONS,
            tools=tools,
        ) as agent:
            return agent.run(objective, timeout_seconds=timeout)
    finally:
        runtime.close()


def _as_int(value: Any) -> int | None:
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return None


def agent_stats(result: Any) -> dict[str, int]:
    """Normalize OpenMinion run statistics into stable integers.

    ``AgentRunResult`` exposes both ``stats`` and ``metadata``; depending on the
    version the useful counters live in different places.
    """
    stats = dict(getattr(result, "stats", {}) or {})
    metadata = dict(getattr(result, "metadata", {}) or {})

    def pick(name: str, fallback: str) -> int:
        for value in (stats.get(name), metadata.get(fallback)):
            parsed = _as_int(value)
            if parsed is not None:
                return parsed
        return 0

    return {
        "inference_steps": pick("inference_steps", "inference_steps"),
        "tool_calls": pick("tool_calls", "tool_calls_count"),
        "tool_errors": pick("tool_errors", "tool_errors_count"),
        "tokens": pick("total_tokens", "total_tokens_used"),
    }


def format_agent_stats(result: Any) -> str:
    values = agent_stats(result)
    return (
        f"[agent] inference steps: {values['inference_steps']}, "
        f"tool calls: {values['tool_calls']}, "
        f"tool errors: {values['tool_errors']}, "
        f"tokens: {values['tokens']}"
    )


def gate_summary(gate: dict[str, Any]) -> str:
    """Short terminal summary for ``canary`` runs."""
    comparison = gate["comparison"]
    baseline = gate["baseline"]
    candidate = gate["candidate"]
    b = baseline.metrics
    c = candidate.metrics
    lines = [
        "",
        f"  {'':<14}{'baseline':>12}{'candidate':>14}{'delta':>12}",
        f"  {'pass rate':<14}{b['pass_rate'] * 100:>11.1f}%{c['pass_rate'] * 100:>13.1f}%"
        f"{comparison['deltas']['pass_rate_pp']:>+11.1f}pp",
        f"  {'p95':<14}{b['latency_ms']['p95']:>11.0f}ms{c['latency_ms']['p95']:>12.0f}ms"
        f"{comparison['deltas']['p95_latency_pct']:>+11.1f}%",
        f"  {'errors':<14}{b['errors']:>12}{c['errors']:>14}",
        "",
        comparison["verdict_text"],
        "",
        f"Report: {gate['paths']['report_html']}",
    ]
    return "\n".join(lines)
