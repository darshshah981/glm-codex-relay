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
outputs/glm-relay.log          relay logs
```
