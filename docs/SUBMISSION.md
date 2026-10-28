# Cortensor Hackathon #5 submission — Cortensor Canary

## One line

Cortensor Canary is an OpenMinion agent that decides whether a Cortensor endpoint can replace the
inference provider you run today — using your own task suite and a deterministic release gate, not a
vibe check.

## The problem

Teams evaluating a decentralized inference network need to answer one question before switching
traffic: *is it actually good enough for my workload?* Today that answer comes from a handful of
prompts typed into a playground, or from an LLM judging another LLM — neither is reproducible, and
neither is a decision a release process can use.

Two failure modes make this concrete:

- A model that is fine on prose silently breaks JSON-only output that a downstream service parses.
- A model that is correct but 6× slower at p95 turns a 200 ms endpoint into a timeout incident.

Both are invisible in a chat comparison and obvious in a per-case gate.

## The solution

Canary runs the same suite against baseline and candidate and returns `PASS`, `WARN` or `FAIL`:

| Check | Default gate |
|---|---|
| Pass-rate drop | ≤ 2.0 percentage points |
| p95 latency growth | ≤ 25% for WARN, ≤ 50% for FAIL |
| Candidate error rate | ≤ 2% |
| Suite identity | must match, otherwise FAIL |

The verdict names the regressed case ids, so the failure is actionable rather than a score.

## Why an agent, and why OpenMinion

The measurement itself must not be agentic — a model that can restate a pass rate can also invent one.
So the split is deliberate:

- **Deterministic core** (`runner`, `scoring`, `compare`) computes every number. Standard library only,
  so it runs in a bare CI container with no SDKs and no spend.
- **OpenMinion agent** (`agent.py`) plans and explains: `list_targets → run_eval → run_eval →
  compare_runs → gate_verdict`. It sees numbers only as tool output.

That is what makes the agent trustworthy as a release gate: it can refuse, it can report a tool error,
but it cannot fabricate a result.

Built on OpenMinion 0.1.14 using the public Python SDK: `Agent`, `APIRuntime`, `@tool`,
`AgentRunResult`. No patched internals, no private imports — the config is a plain
`agents.json` written by `agent.py:write_mock_config` and loaded through
`APIRuntime.from_config_path()`.

### Cortensor integration

OpenMinion ships native presets, so no custom adapter is needed:

```bash
openminion setup --provider cortensor-portal --model oss-20b --no-chat
# or, for a local router:
openminion setup --provider cortensor-router --model gpt-oss-20b --no-chat
```

Canary itself talks to any OpenAI-compatible endpoint, which covers `https://api.cortensor.app/v1`,
a local Router gateway, Ollama, vLLM and hosted providers with the same code path. `config/targets.json`
ships three targets: `portal`, `router`, `local`.

## Demo (runs offline, no key, no spend)

```bash
./canary demo --agent
```

Two local mock endpoints, the `smoke` suite, the full gate, and the real OpenMinion tool loop:

```text
                    baseline     candidate       delta
  pass rate           100.0%         83.3%      -16.7pp
  p95                  36.7ms       257.7ms     +602.2%
  errors                   0             0

Cortensor Canary verdict: FAIL
  quality         -16.7pp  (gate -2.0pp)  FAIL
  latency_p95    +602.2%   (gate +25.0%)  FAIL
  errors           +0.0%   (gate +2.0%)  PASS

Regressions: 1 -> instruction-three-colors
[agent] inference steps: 9, tool calls: 5, tool errors: 0, tokens: 5189
```

The numbers are fixture data from the bundled mock, clearly labelled as such in the README. They
demonstrate the gate, not Cortensor's quality — a real verdict requires a real endpoint and a real key.

## Building blocks tested

Full detail with repro steps: [BUILDING_BLOCKS.md](BUILDING_BLOCKS.md).

| Block | Verdict |
|---|---|
| `@tool(min_scope="READ_ONLY")` ×4 | worked first try; JSON returns pass through |
| `Agent(...)` + `run(timeout_seconds=)` | worked; 9 inference steps, 5 tool calls, 0 tool errors |
| `APIRuntime.from_config_path(path, home_root=)` | worked; requires matching `close()` |
| `cortensor-portal` / `cortensor-router` presets | both present; different wire protocols (chat vs completions) |
| Native `cortensor` vendor behaviour | retries forced off on Portal (no idempotency), tool arguments preserved |
| `AgentRunResult.stats` / `.metadata` | both used, types disagree — normalised in `agent_stats()` |

Five concrete findings are written up with expected vs actual behaviour in
[BUILDING_BLOCKS.md](BUILDING_BLOCKS.md):

1. `setup --list-providers --json` omits `api_format`, so an automated setup cannot tell a Portal
   chat endpoint from a Router completions endpoint.
2. Run counters live in both `stats` (ints) and `metadata` (strings), and `inference_steps` exists
   only in `metadata`.
3. `metadata.model` reports the brain role (`brain-orchestrator`), not the configured model.
4. Missing provider credentials are detected at `agent.run()`, not when the config is loaded.
5. `Requires-Python >=3.11` splits a stdlib-only test matrix in two.

## What we learned

The gate is only as trustworthy as its numbers, and OpenMinion 0.1.14 gives an agent two ways to
print the same number. That is the single change we would prioritise: a typed run-stats object, so a
downstream release gate can log one number per fact and compare runs across SDK versions.

The second lesson is about Cortensor specifically. The SDK already knows Portal cannot be retried
safely (no idempotency key), so it disables retries rather than duplicating a charge. That is the
right default, and it changes how a benchmark must be designed: with retries off, a single 429 is a
real error, so a quality gate must report error rate separately from pass rate instead of hiding
transient failures behind a retry. Canary's gate does exactly that.

Third: the adapter layer is more mature than the setup layer. Wire-protocol detection, URL
normalisation and the completion/chat split are handled defensively; the coarse edges are in what the
CLI *tells* the user (JSON listing, eager credential validation, model identity in metadata).

## What is in the repository

```
src/cortensor_canary/
├── client.py      OpenAI-compatible client, retries, token accounting
├── scoring.py     deterministic expectations (exact/contains/regex/label/json)
├── suites.py      suite loading + validation
├── runner.py      execution, metrics, run artifacts (cortensor-canary/run/v1)
├── compare.py     deltas, gates, verdict (cortensor-canary/comparison/v1)
├── report.py      Markdown + self-contained HTML report
├── pipeline.py    workspace, run_gate
├── mock.py        deterministic mock endpoint + scripted tool loop
├── agent.py       OpenMinion agent, 4 read-only tools, mock config
└── cli.py         8 commands, CI-friendly exit codes
```

- 3 bundled suites (smoke 6, support-ru 9, structured 5), plain JSON, user-extensible.
- 65 tests, no network required; the 4 agent tests skip cleanly when OpenMinion is absent
  (they run for real — `Ran 65 tests ... OK` — when `openminion==0.1.14` is installed).
- GitHub Actions CI on Python 3.9/3.11/3.12/3.14.
- Dockerfile and docker-compose for a one-command demo.

## Verification

```bash
make test           # Ran 65 tests ... OK
./canary demo       # exit 0, artifacts/reports/*/report.html
./canary demo --agent
```

Determinism check: the `degraded` profile always fails `instruction-three-colors` and always returns
503 on the escalation case, so the same verdict is produced on every run and on every machine.

## Honest limitations

- Bundled suites are small and synthetic. They prove the pipeline; they do not prove a provider is good
  for a specific production workload. The intended workflow is to commit your own suite.
- Open-ended prose cannot be scored deterministically. Adding an LLM judge would make the gate
  non-reproducible, which defeats its purpose.
- Latency from a single host is not a global latency benchmark; baseline and candidate must be measured
  in the same run from the same machine.
- `cortensor-portal` can be slow or unstable. Retries are bounded, and a FAIL caused by timeouts is a
  real release signal rather than a flaw in the gate.

## Links

- Repository: https://github.com/9xj89gzrtw-hue/cortensor-openminion-agent
- Demo video (65 s, real run): [artifacts/demo/demo.mp4](https://github.com/9xj89gzrtw-hue/cortensor-openminion-agent/blob/main/artifacts/demo/demo.mp4)
- Building blocks tested / findings: [BUILDING_BLOCKS.md](BUILDING_BLOCKS.md)
- Architecture: [docs/ARCHITECTURE.md](ARCHITECTURE.md)
- OpenMinion: https://github.com/openminion/openminion
- Cortensor: https://cortensor.network

## License

Apache-2.0.
