"""Command line interface for Cortensor Canary."""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any

from . import __version__
from .agent import (
    AgentUnavailable,
    format_agent_stats,
    gate_summary,
    openminion_available,
    run_agent,
    write_mock_config,
)
from .client import OpenAIClient, Target, load_targets
from .compare import compare_runs, save_comparison
from .pipeline import Workspace, run_gate, run_single
from .report import write_reports
from .runner import load_run
from .suites import SuiteError, list_suites, load_suite


def project_root() -> Path:
    env = os.environ.get("CANARY_HOME")
    if env:
        return Path(env)
    cwd = Path.cwd()
    if (cwd / "config" / "targets.json").is_file():
        return cwd
    for parent in Path(__file__).resolve().parents:
        if (parent / "config" / "targets.json").is_file():
            return parent
    return cwd


def default_targets_file() -> Path:
    return Path(os.environ.get("CANARY_TARGETS") or project_root() / "config" / "targets.json")


def _load_targets(path: str | None) -> dict[str, Target]:
    target_path = Path(path) if path else default_targets_file()
    if not target_path.is_file():
        raise SystemExit(f"targets file not found: {target_path}\nCopy config/targets.example.json first.")
    try:
        return load_targets(target_path)
    except (OSError, ValueError) as exc:
        raise SystemExit(f"cannot load targets: {exc}") from exc


def _workspace(path: str | None) -> Workspace:
    return Workspace(Path(path) if path else Path("artifacts"))


def _print_table(rows: list[list[str]], headers: list[str]) -> None:
    widths = [len(header) for header in headers]
    for row in rows:
        for index, cell in enumerate(row):
            widths[index] = max(widths[index], len(str(cell)))
    line = "  ".join(header.ljust(widths[index]) for index, header in enumerate(headers))
    print(line)
    print("  ".join("-" * width for width in widths))
    for row in rows:
        print("  ".join(str(cell).ljust(widths[index]) for index, cell in enumerate(row)))


# --------------------------------------------------------------------- commands
def cmd_targets(args: argparse.Namespace) -> int:
    targets = _load_targets(args.targets)
    rows = []
    for target in targets.values():
        probe = ""
        if args.probe:
            result = OpenAIClient(target, retries=0).probe()
            probe = (
                f"ok {result['latency_ms']:.0f} ms / {result.get('reply', '')!r}"
                if result["ok"]
                else f"FAIL {result.get('error', '')[:50]}"
            )
        rows.append([target.name, target.model, target.base_url, target.api_key_env or "—", probe])
    _print_table(rows, ["name", "model", "base_url", "api key env", "probe"])
    if not args.probe:
        print("\nRun with --probe to make one small live request per target.")
    return 0


def cmd_suites(args: argparse.Namespace) -> int:
    rows = []
    for item in list_suites():
        rows.append([item["name"], str(item["cases"]), item.get("version", ""), item.get("description", "")[:70]])
    _print_table(rows, ["suite", "cases", "version", "description"])
    return 0


def cmd_run(args: argparse.Namespace) -> int:
    targets = _load_targets(args.targets)
    if args.target not in targets:
        raise SystemExit(f"unknown target {args.target!r}; choose: {', '.join(targets)}")
    suite = load_suite(args.suite)
    workspace = _workspace(args.workspace)
    run = run_single(
        targets[args.target],
        suite,
        workspace,
        retries=args.retries,
        limit=args.limit,
    )
    m = run.metrics
    print(
        f"\n{run.run_id}: {m['passed']}/{m['cases']} passed "
        f"({m['pass_rate'] * 100:.1f}%), p95 {m['latency_ms']['p95']:.0f} ms, {m['errors']} errors"
    )
    print(f"Artifact: {workspace.run_path(run.run_id)}")
    return 0


def cmd_compare(args: argparse.Namespace) -> int:
    workspace = _workspace(args.workspace)
    try:
        baseline = load_run(workspace.resolve_run(args.baseline))
        candidate = load_run(workspace.resolve_run(args.candidate))
    except (OSError, ValueError) as exc:
        raise SystemExit(f"cannot load runs: {exc}") from exc
    gates = {
        "max_quality_drop_pp": args.max_quality_drop,
        "max_p95_latency_increase_pct": args.max_latency_increase,
        "max_error_rate_pct": args.max_error_rate,
    }
    comparison = compare_runs(baseline, candidate, gates)
    out_dir = Path(args.out) if args.out else workspace.reports_dir / f"compare-{args.baseline}-{args.candidate}"
    out_dir.mkdir(parents=True, exist_ok=True)
    save_comparison(comparison, out_dir)
    paths = write_reports(baseline, candidate, comparison, out_dir)
    print(comparison["verdict_text"])
    print(f"\nReport: {paths['report_html']}")
    return 0 if comparison["status"] != "FAIL" else 1


def cmd_canary(args: argparse.Namespace) -> int:
    targets = _load_targets(args.targets)
    for name in (args.baseline, args.candidate):
        if name not in targets:
            raise SystemExit(f"unknown target {name!r}; choose: {', '.join(targets)}")
    suite = load_suite(args.suite)
    workspace = _workspace(args.workspace)
    gate = run_gate(
        targets[args.baseline],
        targets[args.candidate],
        suite,
        workspace,
        gates={
            "max_quality_drop_pp": args.max_quality_drop,
            "max_p95_latency_increase_pct": args.max_latency_increase,
            "max_error_rate_pct": args.max_error_rate,
        },
        retries=args.retries,
        limit=args.limit,
    )
    print(gate_summary(gate))
    return 0 if gate["comparison"]["status"] != "FAIL" else 1


def cmd_demo(args: argparse.Namespace) -> int:
    from .mock import MockServer

    suite_name = args.suite if not args.agent else "smoke"
    suite = load_suite(suite_name)
    workspace = _workspace(args.workspace)
    baseline_server = MockServer("good").start()
    candidate_server = MockServer(args.profile).start()
    try:
        baseline = Target(
            name="mock-baseline",
            label="Mock baseline",
            base_url=baseline_server.base_url,
            model="mock-good",
            api_key_env="CANARY_MOCK_KEY",
        )
        candidate = Target(
            name="mock-candidate",
            label=f"Mock candidate ({args.profile})",
            base_url=candidate_server.base_url,
            model=f"mock-{args.profile}",
            api_key_env="CANARY_MOCK_KEY",
        )
        print(f"Mock baseline:  {baseline.base_url}")
        print(f"Mock candidate: {candidate.base_url}  profile={args.profile}")
        print(f"Suite: {suite.name} ({len(suite.cases)} cases)\n")

        gate = run_gate(
            baseline,
            candidate,
            suite,
            workspace,
            gates={"max_quality_drop_pp": args.max_quality_drop},
            retries=0,
        )
        print(gate_summary(gate))

        if args.agent:
            config_path = write_mock_config(workspace.root / "mock-openminion.json", baseline.base_url)
            os.environ.setdefault("CANARY_MOCK_KEY", "mock")
            objective = (
                "Decide whether mock-candidate can replace mock-baseline for the smoke suite. "
                "Run both evaluations, compare them, and report the deterministic verdict."
            )
            print("\nOpenMinion agent (offline, deterministic mock model):")
            try:
                result = run_agent(
                    objective,
                    workspace,
                    config_path=str(config_path),
                    targets={"mock-baseline": baseline, "mock-candidate": candidate},
                    timeout=args.timeout,
                )
                print(result.text.strip())
                print("\n" + format_agent_stats(result))
            except AgentUnavailable as exc:
                print(f"[agent] unavailable: {exc}")
                return 2
        return 0
    finally:
        baseline_server.stop()
        candidate_server.stop()


def cmd_agent(args: argparse.Namespace) -> int:
    targets = _load_targets(args.targets) if args.targets or default_targets_file().is_file() else {}
    workspace = _workspace(args.workspace)
    try:
        result = run_agent(
            args.objective,
            workspace,
            config_path=args.config,
            targets=targets or None,
            timeout=args.timeout,
        )
    except AgentUnavailable as exc:
        print(f"OpenMinion agent unavailable: {exc}", file=sys.stderr)
        if args.baseline and args.candidate:
            print("Falling back to the deterministic gate.\n", file=sys.stderr)
            suite = load_suite(args.suite)
            gate = run_gate(targets[args.baseline], targets[args.candidate], suite, workspace)
            print(gate_summary(gate))
            return 0 if gate["comparison"]["status"] != "FAIL" else 1
        return 2
    print(result.text)
    print("\n" + format_agent_stats(result))
    return 0


def cmd_version(args: argparse.Namespace) -> int:
    print(f"cortensor-canary {__version__}")
    print(f"openminion available: {openminion_available()}")
    return 0


# ----------------------------------------------------------------------- parser
def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="canary",
        description="Cortensor Canary — release gate for LLM endpoints.",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    def common(p: argparse.ArgumentParser) -> None:
        p.add_argument("--workspace", default=None, help="artifact directory (default: ./artifacts)")
        p.add_argument("--targets", default=None, help="targets JSON (default: config/targets.json)")

    p = sub.add_parser("targets", help="list configured endpoints")
    common(p)
    p.add_argument("--probe", action="store_true", help="send one small live request per target")
    p.set_defaults(func=cmd_targets)

    p = sub.add_parser("suites", help="list bundled evaluation suites")
    p.set_defaults(func=cmd_suites)

    p = sub.add_parser("run", help="run one suite against one target")
    common(p)
    p.add_argument("--target", required=True)
    p.add_argument("--suite", default="smoke")
    p.add_argument("--limit", type=int, default=None)
    p.add_argument("--retries", type=int, default=2)
    p.set_defaults(func=cmd_run)

    p = sub.add_parser("compare", help="compare two run artifacts")
    common(p)
    p.add_argument("baseline")
    p.add_argument("candidate")
    p.add_argument("--out", default=None)
    p.add_argument("--max-quality-drop", type=float, default=2.0)
    p.add_argument("--max-latency-increase", type=float, default=25.0)
    p.add_argument("--max-error-rate", type=float, default=2.0)
    p.set_defaults(func=cmd_compare)

    p = sub.add_parser("canary", help="run baseline + candidate and apply gates")
    common(p)
    p.add_argument("--baseline", required=True)
    p.add_argument("--candidate", required=True)
    p.add_argument("--suite", default="smoke")
    p.add_argument("--limit", type=int, default=None)
    p.add_argument("--retries", type=int, default=2)
    p.add_argument("--max-quality-drop", type=float, default=2.0)
    p.add_argument("--max-latency-increase", type=float, default=25.0)
    p.add_argument("--max-error-rate", type=float, default=2.0)
    p.set_defaults(func=cmd_canary)

    p = sub.add_parser("demo", help="offline end-to-end demo with mock endpoints")
    common(p)
    p.add_argument("--suite", default="smoke")
    p.add_argument("--profile", default="degraded", choices=["degraded", "slow", "good"])
    p.add_argument("--agent", action="store_true", help="also run the OpenMinion agent")
    p.add_argument("--timeout", type=float, default=300.0)
    p.add_argument("--max-quality-drop", type=float, default=2.0)
    p.set_defaults(func=cmd_demo)

    p = sub.add_parser("agent", help="run the OpenMinion release-gate agent")
    common(p)
    p.add_argument("objective", nargs="?", default="Decide whether the candidate endpoint can replace the baseline. Run the evaluation and report the verdict.")
    p.add_argument("--config", default=None, help="OpenMinion agents.json")
    p.add_argument("--timeout", type=float, default=900.0)
    p.add_argument("--baseline", default=None, help="fallback baseline target if OpenMinion is missing")
    p.add_argument("--candidate", default=None, help="fallback candidate target if OpenMinion is missing")
    p.add_argument("--suite", default="smoke")
    p.set_defaults(func=cmd_agent)

    p = sub.add_parser("version", help="print version and OpenMinion availability")
    p.set_defaults(func=cmd_version)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return int(args.func(args))
    except SuiteError as exc:
        print(f"suite error: {exc}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        print("\ninterrupted", file=sys.stderr)
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
