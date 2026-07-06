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
```

The wrapper does not store the raw Z.ai key or generated JWT. Set the raw key
only in the shell that starts the relay:

```bash
export ZAI_RAW_KEY="api_id.secret"
```

## Setup

Install `codex-relay` into a local virtual environment:

```bash
work/glm-relay install-relay
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
export ZAI_RAW_KEY="api_id.secret"
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
