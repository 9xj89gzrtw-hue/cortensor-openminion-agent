# Architecture

## Problem

Replacing an inference endpoint is a release. The candidate may return worse JSON, misclassify Russian
intents, or be 5× slower at p95. Prompt spot checks miss this, and LLM-as-judge scoring is too noisy to
gate a deploy. Canary measures the change on a fixed task suite and returns a verdict a CI job can act on.

## Pipeline

```
suites/*.json
     │
     ▼
  suites.py ──► runner.py ──► runs/<run-id>.json
                   │                  │
        client.py (OpenAI-compatible) │
                   │                  ▼
   Cortensor / Router /           compare.py ──► PASS | WARN | FAIL
   Ollama / vLLM / mock               │
                                      ▼
                              report.py ──► report.md + report.html
```

Baseline and candidate runs use the **same suite and the same case order**. Comparison is by case id, so
two runs remain comparable even if one of them failed midway.

## Modules

| Module | Responsibility | Depends on |
|---|---|---|
| `client.py` | Chat-completions client, retries, token accounting, `probe` | stdlib `urllib` |
| `suites.py` | Load/validate suite JSON into `Suite` / `Case` | `scoring` |
| `scoring.py` | Deterministic expectations: exact, contains, regex, label, json | stdlib `re`, `json` |
| `runner.py` | Execute a suite, per-case results, metrics, run artifacts | `client`, `scoring`, `suites` |
| `compare.py` | Deltas, gate checks, regressions, verdict text | `runner` |
| `report.py` | Markdown and self-contained HTML reports | `runner`, `compare` |
| `pipeline.py` | Workspace layout, `run_gate`, `run_single` | all of the above |
| `mock.py` | Deterministic OpenAI-compatible mock + scripted tool loop | stdlib `http.server` |
| `agent.py` | OpenMinion agent, tools, mock config writer | `pipeline`, `suites` |
| `cli.py` | Argument parsing and terminal output | all of the above |

Core has **no third-party dependencies**. OpenMinion is optional and imported lazily, so `canary demo`
works in a bare container.

## Data contracts

### Run artifact — `cortensor-canary/run/v1`

```json
{
  "schema": "cortensor-canary/run/v1",
  "run_id": "20261028T112728Z-portal-smoke",
  "target": {"name": "portal", "label": "Cortensor Portal", "model": "oss-20b", "base_url": "..."},
  "suite": {"name": "smoke", "version": "1.0", "cases": 6},
  "metrics": {
    "cases": 6, "passed": 5, "failed": 1, "errors": 0,
    "pass_rate": 0.8333, "score": 0.8333, "error_rate": 0.0,
    "latency_ms": {"p50": 240.0, "p95": 255.0, "mean": 245.0, "min": 230.0, "max": 260.0},
    "tokens": {"prompt": 900, "completion": 180, "total": 1080}
  },
  "cases": [{"id": "math-17x23", "passed": true, "score": 1.0, "latency_ms": 240.0, "...": "..."}],
  "meta": {"tool": "cortensor-canary/0.1.0", "python": "3.11.9", "platform": "..."}
}
```

Only `api_key_env` is serialized — never a resolved credential, never a URL with a query token.

### Comparison artifact — `cortensor-canary/comparison/v1`

Contains `status`, `gates`, `baseline`/`candidate` summaries, `deltas`, `checks`, `reasons`,
`cases.{regressed,fixed,still_failing,only_baseline,only_candidate}` and a pre-rendered `verdict_text`.

## Gate semantics

| Check | PASS | WARN | FAIL |
|---|---|---|---|
| `quality` (Δ pass rate, pp) | `Δ ≥ -max_quality_drop_pp` | — | `Δ < -max_quality_drop_pp` |
| `latency_p95` (Δ %) | `Δ ≤ limit` | `limit < Δ ≤ 2·limit` | `Δ > 2·limit` |
| `errors` (candidate error rate, %) | `≤ limit/2` | `limit/2 < rate ≤ limit` | `> limit` |
| `suite` | same suite name | — | names differ |

Overall status is the worst individual status. A suite mismatch is always FAIL because a comparison
across different suites has no meaning.

Quality is measured in **percentage points**, not percent: 100% → 83.3% is `-16.7 pp`. Percent-of-percent
would report `-16.7%` for a 5-of-6 result and overstate it.

## Mock server

`mock.py` serves `/v1/chat/completions` and `/v1/models` from a fixture table keyed by prompt substrings.
Three profiles: `good` (all correct), `slow` (correct, +0.7 s), `degraded` (one wrong instruction answer
and one hard 503 on the structured suite).

It also implements a **scripted tool loop**: when tools are present it returns a `tool_calls` payload,
reads the tool results back, and advances the same five-step sequence the agent instructions describe.
That is what makes `canary demo --agent` a real end-to-end test of the OpenMinion integration rather
than a mock of it.

The mock is explicitly **not** a quality simulator for Cortensor. It exists so CI and the demo are
reproducible and free.

## OpenMinion integration

```python
runtime = APIRuntime.from_config_path(config, home_root=workspace.root / ".openminion")
tools = make_tools(workspace, targets)          # 4 read-only tools
with Agent(runtime=runtime, name="canary", instructions=INSTRUCTIONS, tools=tools) as agent:
    return agent.run(objective, timeout_seconds=timeout)
```

Design constraints learned from OpenMinion 0.1.14:

- Tools default to `WRITE_SAFE`, which triggers an interactive approval prompt. All canary tools are
  read-only, so they declare `@tool(min_scope="READ_ONLY")` and the loop runs unattended.
- The agent's `default_channel` must be `console`, and `console` must be in `enabled_channels` and
  `channel_authenticity.trusted_channels`; otherwise the run is denied as `invalid_channel`.
- Tool results arrive wrapped in `{"outputs": {"data": ...}}`. The mock unwraps this before parsing, so
  scripted steps see the real `run_id` / `comparison_path`.
- Tool-call ids must be unique per session or the runtime raises an idempotency conflict. The mock derives
  ids from the tool name plus a hash of the arguments.

`write_mock_config()` emits a minimal `agents.json` with all of the above, which is why the offline agent
demo needs no setup.

## Failure handling

- HTTP 408/409/425/429/5xx and network errors are retried with exponential backoff.
- A single failing case never aborts the run: it is recorded with `error` and counts toward `error_rate`.
- Errors are truncated to 500 characters so one HTML page never blows up on a stack trace.
- `canary compare`/`canary canary` return exit code 1 on FAIL and 0 on PASS/WARN.

## Extension points

| Want | Do |
|---|---|
| New expectation | add a branch in `scoring.score_answer` |
| New suite | drop a JSON file into `src/cortensor_canary/data/suites/` |
| New provider | add a target with `base_url` + `model` to `config/targets.json` |
| New gate | extend `DEFAULT_GATES` and `compare_runs` |
| Different report | `report.render_html` is one pure function over `Run` + comparison |
