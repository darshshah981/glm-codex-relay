# GLM Relay Wrapper

This wrapper manages `codex-relay` for Z.ai GLM-5.2.

It does not store the raw Z.ai key. Set it only in the shell that starts the
relay:

```bash
export ZAI_RAW_KEY="paste-your-zai-raw-key-here"
```

## One-time setup

```bash
work/glm-relay install-relay --python "$HOME/.cache/codex-runtimes/codex-primary-runtime/dependencies/python/bin/python3"
work/glm-relay write-profile
```

This installs `codex-relay` into `work/.venv` and writes:

```text
~/.codex/glm52-relay.config.toml
```

By default, install uses the vendored `third_party/codex-relay` source when Rust
is available and falls back to PyPI's prebuilt wheel otherwise:

```bash
work/glm-relay install-relay --source auto
work/glm-relay install-relay --source vendored
work/glm-relay install-relay --source pypi
```

The profile also points Codex at a local model catalog:

```text
work/glm-model-catalog.json
```

That catalog gives GLM the `slug` metadata Codex expects, so Codex does not
need to parse Z.ai's raw `/models` response as a Codex catalog.

## Run

```bash
work/glm-relay start
work/glm-relay status
work/glm-relay health
work/glm-relay logs
```

For an interactive session where the wrapper keeps JWTs fresh automatically,
prefer:

```bash
work/glm-relay serve
```

Leave that running in a terminal while Codex uses the `glm52-relay` profile.

Use the profile with Codex:

```bash
codex --profile glm52-relay exec --skip-git-repo-check "Say exactly: GLM relay works"
```

## Offline reliability checks

Run the relay and wrapper reliability suite without live Z.ai credentials:

```bash
cargo test --manifest-path third_party/codex-relay/Cargo.toml
python3 -m unittest discover -s tests
```

The Rust tests replay redacted Codex request fixtures and fake GLM SSE streams
through the vendored relay. The Python tests cover wrapper behavior such as JWT
generation, profile writing, install-source selection, default tool denylist,
state metadata, and secret hygiene.

Fixture refresh rules:

```text
keep: protocol shape, minimized tool schemas, representative stream chunks
drop: raw keys, generated JWTs, bearer tokens, logs, relay history, full prompts
```

Use live GLM calls only as a final smoke test after the offline suite passes.

## Live smoke

The live smoke answers a different question than the offline suite:

```text
offline tests -> does our relay logic work for known protocol shapes?
live smoke    -> do current Z.ai auth, endpoint, model, and relay wiring work now?
```

Run it only when you are ready to spend a tiny live request:

```bash
export ZAI_RAW_KEY="paste-your-zai-raw-key-here"
work/glm-relay live-smoke
```

By default it starts the relay if needed, sends one small text request through
`/v1/responses`, verifies output text came back, and stops the smoke relay.
Use the stricter optional tool-call check after the text smoke passes:

```bash
work/glm-relay live-smoke --include-tool-call
```

## Worker lane

The worker lane keeps Codex as planner/reviewer and uses GLM for one bounded
implementation attempt:

```bash
export ZAI_RAW_KEY="paste-your-zai-raw-key-here"
work/glm-relay run-worker "Implement the focused task described here"
work/glm-relay review-worker latest
```

`run-worker` refreshes or starts the relay, writes
`$CODEX_HOME/glm52-relay.config.toml`, and invokes Codex CLI with that profile
and the same `CODEX_HOME`. It does not run `live-smoke` first by default,
because the worker run itself is already a live provider call.

Each attempt writes a local review bundle:

```text
outputs/glm-worker-runs/<timestamp>-<label>/
  prompt.txt
  metadata.json
  stdout.txt
  stderr.txt
  exit_code.txt
  summary.txt
```

`metadata.json` stores non-secret runtime facts, redacts the task prompt from
the captured argv, and records git status snapshots before and after the worker
attempt when the worker cwd is inside a git repository.

Use `review-worker` to inspect a run without restarting the relay or calling
Z.ai:

```bash
work/glm-relay review-worker latest
work/glm-relay review-worker <run-directory-name>
work/glm-relay review-worker latest --json
```

The review command is read-only. It summarizes exit code, model, elapsed time,
cwd, artifact path, relay pid, changed files when available, and bounded
stdout/stderr tails. It does not accept, revert, delete, or test changes. Review
the bundle, the actual git diff, and the relevant tests before trusting the GLM
worker result.

## Trace conversation view

Worker artifacts tell you what the Codex worker process did at the shell level.
Traces tell you what passed through the relay while that worker was running.
The wrapper starts the relay with trace capture enabled and tags relay requests
with the active worker run when `run-worker` is in progress.

Use the default thread view first:

```bash
work/glm-relay traces latest
work/glm-relay traces <run-directory-name>
```

The thread view is a projection of raw trace events. It groups each relay
request into a readable turn: worker prompt, Codex request summary,
system/instructions, user input, GLM thinking, assistant text, tool calls, usage,
and final status.

Use raw or expanded modes when debugging the relay itself:

```bash
work/glm-relay traces <run-directory-name> --raw
work/glm-relay traces latest --full-prompts
work/glm-relay traces latest --follow
```

`--raw` prints JSONL trace events unchanged. `--full-prompts` expands local
prompt/request previews. `--follow` polls for appended trace lines so you can
watch a live worker run without opening a dashboard.

Traces are local sensitive data. They can include prompts, tool output, local
paths, and repo context. Treat `work/.glm-relay/traces/` the same way as relay
history and worker artifacts.

## Auth refresh

Z.ai uses a generated JWT. The wrapper generates a fresh JWT when the relay
starts. Use `serve` for automatic refresh. It checks periodically and restarts
the relay with a fresh JWT before expiry. If you are not using `serve`, call
`refresh` from a scheduler before the token expires:

```bash
work/glm-relay refresh
```

The default token TTL is 1 hour, with a 5 minute refresh margin.

## Local data

The wrapper state file does not store the raw key or the generated JWT. It only
stores runtime metadata such as pid, port, expiry time, and relay settings.

Disk-backed relay history can contain prompts, tool outputs, and conversation
state. Treat this directory as sensitive:

```text
work/.glm-relay/history/
work/.glm-relay/traces/
outputs/glm-worker-runs/
```

## Tool policy

By default, v1 hides subagent / multi-agent runtime tools from GLM:

```text
spawn_agent,wait_agent,close_agent,
multi_agent_v1-spawn_agent,multi_agent_v1-wait_agent,multi_agent_v1-close_agent,
multi_agent_v1-resume_agent,multi_agent_v1-send_input
```

This is intentional. Prompting can steer GLM away from unsupported subagent
calls, but only a runtime smoke test can prove the local Codex daemon can
execute them. Remove the denylist only after that test passes:

```bash
work/glm-relay restart --tool-denylist ""
```

## Files

```text
work/glm-relay                 wrapper CLI
work/glm-model-catalog.json    Codex model catalog entry for glm-5.2
third_party/codex-relay/       vendored codex-relay source, MIT licensed
work/.venv/                    local codex-relay install
work/.glm-relay/relay.pid      relay pid
work/.glm-relay/state.json     non-secret state
work/.glm-relay/history/       optional disk-backed relay history
work/.glm-relay/traces/        local relay JSONL traces
outputs/glm-worker-runs/       local GLM worker review bundles
outputs/glm-relay.log          relay logs
```
