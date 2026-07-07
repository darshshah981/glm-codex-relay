# glm-codex-relay

Use Z.ai GLM-5.2 from Codex by running a local Responses-to-Chat Completions
relay.

This repo wraps [`codex-relay`](https://github.com/MetaFARS/codex-relay) for
the GLM coding endpoint:

```text
Codex Responses request
        |
        v
local relay profile on http://127.0.0.1:4453/v1
        |
        v
codex-relay
        |
        v
Z.ai GLM-5.2 Chat Completions
```

## What is included

```text
work/glm-relay              Wrapper CLI for installing, starting, refreshing, and stopping the relay
work/glm-relay.md           Operational notes
work/glm-model-catalog.json Local Codex model catalog entry for glm-5.2
third_party/codex-relay/    Vendored codex-relay source code
```

`third_party/codex-relay` is copied from
[`MetaFARS/codex-relay`](https://github.com/MetaFARS/codex-relay) and keeps its
upstream MIT license in `third_party/codex-relay/LICENSE`.

The wrapper does not store the raw Z.ai key or generated JWT. Set the raw key
only in the shell that starts the relay:

```bash
export ZAI_RAW_KEY="paste-your-zai-raw-key-here"
```

## Setup

Install `codex-relay` into a local virtual environment:

```bash
work/glm-relay install-relay
```

By default, `install-relay` uses the vendored source when Rust is available and
falls back to PyPI's prebuilt wheel otherwise. You can choose explicitly:

```bash
work/glm-relay install-relay --source vendored
work/glm-relay install-relay --source pypi
```

If you are running this from Codex Desktop and want to use the bundled Python:

```bash
work/glm-relay install-relay --python "$HOME/.cache/codex-runtimes/codex-primary-runtime/dependencies/python/bin/python3"
```

Write the Codex profile:

```bash
work/glm-relay write-profile
```

This creates:

```text
~/.codex/glm52-relay.config.toml
```

## Run

Start the relay with automatic JWT refresh:

```bash
export ZAI_RAW_KEY="paste-your-zai-raw-key-here"
work/glm-relay serve
```

In another terminal, use the profile:

```bash
codex --profile glm52-relay exec --skip-git-repo-check "Say exactly: GLM relay works"
```

Useful commands:

```bash
work/glm-relay status
work/glm-relay health
work/glm-relay logs
work/glm-relay stop
```

## Offline reliability checks

The default reliability suite is fully offline. It uses redacted Codex request
fixtures, a fake GLM Chat Completions upstream, and wrapper unit tests:

```bash
cargo test --manifest-path third_party/codex-relay/Cargo.toml
python3 -m unittest discover -s tests
```

GitHub Actions runs the same offline gates on pull requests.

These checks prove:

```text
Codex Responses request -> GLM Chat Completions request
GLM SSE stream          -> Codex Responses SSE events
normal tool call        -> function_call_output follow-up
wrapper config          -> JWT/profile/denylist/state behavior
```

They do not call Z.ai and do not require `ZAI_RAW_KEY`.

### Fixture maintenance

GLM relay fixtures live under:

```text
third_party/codex-relay/tests/fixtures/codex_glm_current/
```

When refreshing fixtures after a Codex upgrade, keep only the smallest shape
needed to exercise the protocol behavior. Do not commit raw Z.ai keys,
generated JWTs, bearer tokens, relay history, logs, full user prompts, or
unredacted local home paths.

Live GLM checks are still useful as a final smoke test, but they are not the
foundation for reliability because they depend on credentials, provider
availability, and account balance.

Run a live smoke only after the offline suite is green:

```bash
export ZAI_RAW_KEY="paste-your-zai-raw-key-here"
work/glm-relay live-smoke
```

This starts the relay if needed, makes one real text request through GLM-5.2,
and stops the smoke relay afterward. It proves current auth, endpoint, model,
and relay wiring. To also ask GLM for a normal tool call, run:

```bash
work/glm-relay live-smoke --include-tool-call
```

The tool-call check is stricter and more provider-behavior-sensitive, so keep
the text smoke as the first live gate.

## Tool policy

By default, v1 hides Codex subagent / multi-agent runtime tools from GLM:

```text
spawn_agent,wait_agent,close_agent,
multi_agent_v1-spawn_agent,multi_agent_v1-wait_agent,multi_agent_v1-close_agent,
multi_agent_v1-resume_agent,multi_agent_v1-send_input
```

This keeps the first version focused on normal Codex tool calls. Prompting can
steer GLM away from unsupported subagent calls, but only a runtime smoke test
can prove the local Codex daemon can execute returned subagent tool calls.

## Local data

The wrapper state file stores only runtime metadata such as pid, port, JWT
expiry time, and relay settings.

Disk-backed relay history can contain prompts, tool outputs, and conversation
state. Treat it as sensitive:

```text
work/.glm-relay/history/
```
