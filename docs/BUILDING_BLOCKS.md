# Building blocks tested — OpenMinion 0.1.14

Environment: `openminion==0.1.14` from PyPI, Python 3.12.14, macOS 26 (arm64), no Portal key.
Everything below was executed in this repository; each claim names the file that proves it.

`make demo-agent` runs the real agent loop against the bundled deterministic mock model, so the
whole finding list is reproducible offline with no credentials and no spend.

```text
$ CANARY_MOCK_KEY=mock PYTHONPATH=src python -m cortensor_canary demo --workspace artifacts/demo-run --agent
...
[agent] inference steps: 9, tool calls: 5, tool errors: 0, tokens: 5211
```

## What we used

| Block | Where | Verdict |
|---|---|---|
| `@tool(min_scope="READ_ONLY")` | `src/cortensor_canary/agent.py` — 4 tools | Worked first try. JSON-string returns are passed through unchanged. |
| `Agent(runtime, name, instructions, tools)` + `agent.run(objective, timeout_seconds=)` | `agent.py:241` | Worked. `run()` returned `metadata.tool_loop_termination_reason == "model_final"` after 5 tool calls. |
| `APIRuntime.from_config_path(path, home_root=)` | `agent.py:238` | Worked. Needs a matching `runtime.close()`; we close in `finally`. |
| Provider presets (`openminion setup --list-providers`) | `config/targets.json`, docs | `cortensor-portal` → `https://api.cortensor.app/v1`, model `oss-20b`. `cortensor-router` → `http://127.0.0.1:8080/api/v2/completions`. |
| Native `cortensor` service vendor | SDK, hostname-detected for `api.cortensor.app` | Not a plain OpenAI alias: retries are forced off and portal tool arguments are preserved. |
| `AgentRunResult.stats` / `.metadata` | `agent.py:agent_stats` | Both exist and disagree in type; see finding 2. |
| `channel_authenticity` / `channel_policy` config blocks | `agent.py:write_mock_config` | Optional for a headless run — verified by deleting both and re-loading the config. |
| `providers` config block | same | Required. Removing it fails at runtime, not at load time; see finding 4. |

## Findings

### 1. The two Cortensor presets speak different wire protocols, and `--json` hides it

The text listing is correct and explicit:

```text
$ openminion setup --list-providers
cortensor-portal: Cortensor Portal
  API format: openai-compatible (OpenAI-compatible Portal API)
  Endpoint: https://api.cortensor.app/v1
cortensor-router: Cortensor Router (direct)
  API format: cortensor-completions (Cortensor router completions)
  Endpoint: http://127.0.0.1:8080/api/v2/completions
```

The machine-readable listing drops the field:

```text
$ openminion setup --list-providers --json
{"id": "cortensor-router", "base_url": "http://127.0.0.1:8080/api/v2/completions",
 "label": "Cortensor Router (direct)", "recommended_models": ["gpt-oss-20b"], ...}
                                                       # no api_format / api_format_id
```

Impact: the JSON form is the one a setup script reads (it is generated right after the text form), and
it cannot distinguish a Portal chat endpoint from a Router completions endpoint. Someone automating
`openminion setup` will wire `/api/v2/completions` into a chat client and get a 404 that looks like a
network fault.

Credit where due: the adapter itself handles both correctly — `_resolve_api_mode()` detects the
`/completions` suffix and routes to the completions transport, and `_resolve_completion_url()`
normalises the base URL first. The gap is only in the JSON contract.

Suggestion: include `api_format_id` in the JSON payload (same value the text listing prints).

### 2. Run counters live in two places, with two types

From one real turn in 0.1.14:

```python
result.stats     # {"input_tokens": 3985, "output_tokens": 100, "llm_calls": 1,
                 #  "tool_calls": 5, "tool_errors": 0, "duration_ms": 971}   -> int
result.metadata  # {"inference_steps": "9", "tool_calls_count": "5",
                 #  "total_tokens_used": "4085", ...}                        -> str
```

`inference_steps` exists only in `metadata`, and every metadata counter is a string. Anything that
prints or aggregates these numbers has to normalise, or `sum()`/`>` comparisons raise or silently
compare strings.

Impact: Canary carries `agent_stats()` (`agent.py:266`) purely to reconcile this, and the CLI prints
`tokens: 5211` rather than `4085` because the two totals cover different spans.

Suggestion: expose a single typed `RunStats` dataclass on `AgentRunResult`, and keep `metadata` for
provenance only.

### 3. `metadata.model` reports the role, not the configured model

The agent above is configured with model `mock-good`, and `metadata` reports
`"model": "brain-orchestrator"`. In a multi-connection setup that string cannot be used to identify
which upstream served the run.

Suggestion: add `provider` and `model` for the connection that was actually used, and keep the
brain-role name under a separate key.

### 4. Missing provider credentials are detected at run time, not at config load

```python
APIRuntime.from_config_path("config-without-providers.json")   # succeeds
agent.run("...")                                              # ProviderError: OpenAI provider selected
                                                              # but API key is missing
```

Impact: a CI job or a `doctor` check that only loads the config reports green, then the first real
turn fails. Canary's `./canary version` therefore checks `import openminion` availability only and
does not claim a provider is reachable.

Suggestion: make `openminion doctor` (or a `validate=True` flag on `from_config_path`) resolve
credentials eagerly, and print the exact env var and config path that failed.

### 5. Python floor is 3.11 while the rest of the ecosystem is not

`openminion` declares `Requires-Python >=3.11`. A stdlib-only core (`cortensor_canary.runner`,
`scoring`, `compare`) runs on 3.9, so the CI matrix runs the deterministic half on 3.9–3.14 and the
agent tests only on 3.11+ (they skip cleanly elsewhere — `tests/test_agent.py`).

Impact: not a bug, but it split our test suite in two and forced the demo command to be
interpreter-aware. A `openminion agent-check`-style preflight that prints the floor would save a
confusing traceback.

## What we did not test

No Portal key was available, so nothing here is a statement about Cortensor model quality, latency or
uptime. Also untouched: memory, skills, tasks, delegation, scheduling, MCP bridge, room/channel
transports, ACP, sandboxing and the browser tools. A verdict from Canary about a real endpoint
requires a real key and is a separate exercise from the SDK feedback above.
