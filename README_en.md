<div align="center">

<img src="./docs/assets/leo-mark.png" alt="LEO mark" width="240">

# Leonervis Code

English | [中文](./README.md)

[![Python](https://img.shields.io/badge/Python-3.12%E2%80%933.13-3776AB?logo=python&logoColor=white)](./pyproject.toml)
[![uv](https://img.shields.io/badge/uv-managed-DE5FE9?logo=uv&logoColor=white)](https://docs.astral.sh/uv/)
[![pytest](https://img.shields.io/badge/pytest-8%2B-0A9EDC?logo=pytest&logoColor=white)](./pyproject.toml)
[![Ruff](https://img.shields.io/badge/Ruff-0.9%2B-D7FF64?logo=ruff&logoColor=black)](./pyproject.toml)

</div>

Leonervis Code is a learning-first coding-agent CLI prototype for local, single-user use. It will incrementally build an understandable and verifiable Harness: a model makes decisions, the host executes controlled tools within explicit workspace and permission boundaries, and structured results return to the model.

> **Current status: Foundation 3C named provider profiles and the persistent runtime are complete.** Anthropic, OpenAI, xAI, DashScope/Qwen/Kimi, Ollama/local, OpenRouter, and controlled custom endpoints can now be used through one-shot `--model` selection or saved as global profiles containing no secret value, with a workspace choosing its active profile. `ProjectSession` gives other modules the same long-lived client, neutral multi-turn history, and atomic between-turn switching API. The bare REPL becomes a real conversation when an explicit/active profile exists and remains deterministic fake/offline when no real selection exists. Both real SDK clients use `max_retries=0`.

## Project positioning

This project does not attempt to reproduce, replace, or promise compatibility with Claude Code or any other existing product. Its first phase focuses on Harness fundamentals: controlled model calls, tool execution, workspace boundaries, approval, structured events, and deterministic tests.

Implementation progresses through small, complete learning slices. Each slice adds only the code and dependencies it needs, and records its design rationale, data flow, reference differences, known boundary, and test evidence. It will not create empty early shells for MCP, plugins, multi-agent systems, servers, RAG, or background tasks merely to appear complete.

Related Harness reading and learning notes are available in [Harness-study](https://github.com/TsingFengIceberg/Harness-study).

## Requirements

| Item | Current requirement | Notes |
| --- | --- | --- |
| Python | **3.12** development/test baseline; 3.13 allowed | The project declares `>=3.12,<3.14`. |
| [uv](https://docs.astral.sh/uv/) | Latest stable version | The sole manager for Python packages, virtual environments, and lockfiles. |
| Git | Any current stable version | Used for version control; not a Python dependency. |
| pytest | `>=8.3` | Current deterministic test runner. |
| Ruff | `>=0.9` | Current static-checking and formatting tool. |

The root [`.python-version`](./.python-version) selects Python 3.12 by default. If that interpreter is unavailable locally, `uv` will prompt during synchronization; alternatively, install it with `uv python install 3.12`.

## Quick start

```bash
# 1. Enter the repository after cloning it
cd leonervis-code

# 2. Create .venv, install dependencies, and synchronize from uv.lock
uv sync

# 3. Launch the local interactive terminal (requires a real terminal)
uv run leonervis-code
```

The command shows a colored LEO mark, version, current directory (the Foundation 1B workspace root), and Foundation 1B status before displaying:

```text
leonervis>
```

Enter any nonblank text for a deterministic result:

```text
leonervis> Explain the Harness boundary
Fake response: Explain the Harness boundary
```

The REPL provides process-local history and provider-runtime controls:

```text
/help                         show controls
/history <count>              show the most recent complete turns
/status                       show redacted current runtime status
/provider list                list named profiles
/provider current             show the active profile/provider/model
/provider use <name>          switch and persist this workspace's active profile
/model <model>                override only this process; do not edit the profile
/exit or /quit                exit normally
Ctrl-D / EOF / Ctrl-C         exit normally
```

For a visible, deterministic Foundation 1B demonstration of the tool loop, run:

```bash
uv run leonervis-code demo-read README.md
```

This command visibly reports a scripted provider request, the workspace-confined `read_file` result, and the scripted final response. It is a verification aid, not a real model interface; it never writes files, executes commands, or accesses the network. Try a failure boundary with a path that escapes the workspace:

```bash
uv run leonervis-code demo-read ../outside.txt
```

For one prompt in scripts or automation, use the explicit subcommand:

```bash
uv run leonervis-code prompt "Explain the Harness boundary"
# Fake response: Explain the Harness boundary
```

The two command names point to the same entry point: `leonervis-code` is the formal command and `leonervis` is its shorthand. A module entry point is also available:

```bash
uv run leonervis prompt "Hello"
uv run python -m leonervis_code prompt "Hello"
```

`--help` and `--version` remain available:

```bash
uv run leonervis-code --help
uv run leonervis --version
```

## Foundation 3C: named provider profiles and a real multi-turn REPL

Profile definitions live in `${XDG_CONFIG_HOME:-~/.config}/leonervis-code/providers.json`; a workspace stores only its active profile name in `.leonervis-code/provider.json`. Neither JSON file stores key values. The workspace directory is local runtime state and should be added to the target project's `.gitignore`.

```bash
# Built-in provider: protocol, default endpoint, and credential env come from the catalog
uv run leonervis-code provider add work-openai \
  --provider openai --model gpt-5

# Controlled custom OpenAI-compatible endpoint; store only the key's env-variable name
uv run leonervis-code provider add local-qwen \
  --provider custom \
  --model Qwen/Qwen3.5 \
  --protocol openai-compatible \
  --base-url http://127.0.0.1:11434

uv run leonervis-code provider add vendor \
  --provider custom \
  --model vendor/model \
  --protocol openai-compatible \
  --base-url https://gateway.example/v1 \
  --api-key-env VENDOR_API_KEY

uv run leonervis-code provider list
uv run leonervis-code provider show vendor
uv run leonervis-code provider use local-qwen              # workspace scope by default
uv run leonervis-code provider use work-openai --scope user
uv run leonervis-code provider clear --scope project
uv run leonervis-code provider remove vendor
```

Selection precedence is explicit `--profile` > explicit direct `--model` > workspace active > user active > fake/offline. `--profile NAME --model MODEL` uses a process-local model override on that endpoint without rewriting the profile:

```bash
uv run leonervis-code --profile work-openai --model gpt-5-mini prompt "Explain this workspace"
uv run leonervis-code --profile work-openai       # real multi-turn REPL; client reused across turns
```

Both `provider use` and REPL `/provider use` resolve the route, validate the credential, and construct a candidate SDK client before writing active configuration and swapping the current client. On failure, the old active selection and client remain intact. `/model` is likewise atomic and allowed only between turns. Complete neutral history and tool-use/result pairs survive a provider switch; if the new provider rejects old history, the failed turn is not committed.

Other project modules can use the public facade directly:

```python
from pathlib import Path
from leonervis_code import ProjectSession

with ProjectSession.open(Path.cwd(), profile="work-openai") as session:
    first = session.prompt("Explain the README first")
    session.set_model("gpt-5-mini")
    second = session.prompt("Continue")
```

`ProjectSession` also exposes `list_profiles()`, `use_profile()`, `clear_active()`, `status()`, `history`, and `turns`. Foundation 3C history remains process-local; JSONL transcripts and resume are deferred to Foundation 3D.

## Foundation 3B: local multi-provider real-model path

With global `--model`, `prompt` resolves a real adapter through the shared resolver/factory:

```bash
export ANTHROPIC_API_KEY='...'
uv run leonervis-code --model anthropic/claude-opus-4-8 prompt "Explain this workspace"

export OPENAI_API_KEY='...'
uv run leonervis-code --model openai/gpt-5 prompt "Explain this workspace"

export XAI_API_KEY='...'
uv run leonervis-code --model xai/grok-3 prompt "Explain this workspace"

export DASHSCOPE_API_KEY='...'
uv run leonervis-code --model dashscope/qwen-plus prompt "Explain this workspace"

uv run leonervis-code --model ollama/qwen3:8b prompt "Explain this workspace"

export OPENROUTER_API_KEY='...'
uv run leonervis-code --model openrouter/anthropic/claude-opus-4-8 prompt "Explain this workspace"
```

The Anthropic path uses the official `anthropic` SDK. Every other built-in route reuses the official `openai` SDK through the Chat Completions wire adapter. Both clients are synchronous, non-streaming, and configured with `max_retries=0`. They declare only the current `read_file(path)` tool; local `ReadFileTool` continues to enforce workspace containment, UTF-8, the 32 KiB cap, and the per-turn tool budget.

A one-shot controlled OpenAI-compatible endpoint can also be supplied without persisting a provider or key:

```bash
export VENDOR_API_KEY='...'
uv run leonervis-code \
  --model vendor/model \
  --provider-protocol openai-compatible \
  --base-url https://gateway.example/v1 \
  --api-key-env VENDOR_API_KEY \
  prompt "Explain this workspace"
```

Explicit provider namespaces win. Only registered bare `claude-*`, `gpt-*`, `grok-*`, `qwen-*`, and `kimi-*` families are inferred deterministically; an unknown bare model is never guessed from installed credentials. Route and adapter configuration contain no secret value. A key is read only when the factory constructs the selected SDK client. The runtime does not read `.env`, OAuth, or keyrings, and it does not implement streaming, automatic retries/backoff, fallback execution, live discovery, parallel tools, or disk-backed session resume.

A real route can be previewed without constructing a client or accessing the network:

```bash
uv run leonervis-code --model openai/gpt-5 route
```

The fake fallback remains unchanged, but when a workspace/user active profile exists, `prompt` and the bare REPL use that real profile even without an explicit selector:

```bash
uv run leonervis-code provider clear --scope project
uv run leonervis-code provider clear --scope user
uv run leonervis-code prompt "Hello"   # fake when no active profile exists; no network
uv run leonervis-code                   # fake REPL when no active profile exists; no network
uv run leonervis-code route             # Foundation 2B fake policy preview; no network
```

See the [Foundation 3A Anthropic-adapter decision](./docs/decisions/0007-foundation-3a-anthropic-non-streaming-adapter.md), [Foundation 3B multi-provider-runtime decision](./docs/decisions/0008-foundation-3b-local-multi-provider-runtime.md), and [Foundation 3C named-profile/persistent-runtime decision](./docs/decisions/0009-foundation-3c-named-provider-profiles-and-runtime-manager.md). Run live smoke checks only when the user explicitly chooses their own credentials, endpoints, and API budget.

## Foundation 2B: offline adapter-owned compatibility policy

`route` is a deterministic diagnostic surface for the control plane and adapter-policy boundary that a future real provider adapter will use:

```bash
uv run leonervis-code route
# primary: fake-messages/alpha
#   credential: configured
#   canonical parameters: <none>
#   native preview: <none>
#   diagnostics: <none>

uv run leonervis-code route --model beta --max-output-tokens 32 --fallback-model default
# fake-chat previews max_output_tokens; fake-messages previews max_tokens

uv run leonervis-code route --model beta --temperature 0.2
# shows a visible fixed-sampling omission diagnostic
```

The route resolver owns **hard** admission rules: valid provider/model selection, enabled status, required tool-use/streaming capabilities, canonical option types/ranges, fallback validity, and Harness-owned field protection. A selected adapter owns provider-native wire names and documented **soft** compatibility behavior. The fake `beta` model demonstrates the distinction: its requested `temperature` is omitted as a known fixed-sampling incompatibility, and `route` reports that decision instead of silently changing the request or issuing a false hard error.

Provider-specific extensions have a controlled API-level path only for now. They cannot override `model`, messages, tools, streaming, token-limit fields, or adapter-generated parameter fields. This mirrors the future security boundary; the command intentionally does not yet accept arbitrary JSON body overrides.

The Foundation 2B subcommand form of `route` remains completely offline: it constructs no provider client, reads no environment variables, makes no network call, and reveals no credential reference/value. A global-`--model` `route` uses the Foundation 3B resolver to show the real provider, protocol, wire model, base-URL source, and `configured/missing/not required` status, while still constructing no client and sending no request. A successful preview is not proof that the remote provider will accept a request.

## Foundation 1B: deterministic bounded `read_file` tool loop

The current REPL and `prompt` command now complete this minimal, testable path:

```text
terminal input → AgentLoop (ordered in-memory causal context)
  → ScriptedFakeProvider → optional read_file within the current workspace
  → structured tool result → ScriptedFakeProvider → final text output
```

A provider response is either final assistant text or one `read_file` request. The loop returns final text only after the provider finishes, and commits the whole attempted turn—user input, any tool request/result, and final assistant text—only after that success. Each user turn permits at most three file reads; a further request receives a structured limit error, and another tool request after it stops deterministically.

`read_file` accepts only a relative path whose resolved target remains in the current working directory, which is the workspace root for this slice. It rejects absolute paths, `..` or symlink escapes, missing paths, directories, unreadable files, and invalid UTF-8. It returns at most 32 KiB of UTF-8 text with a truncation marker. It cannot write, rename, delete, execute commands, search, or access the network.

The default `ScriptedFakeProvider` retains the visible echo behavior and does not request tools by itself. Its scripted form provides deterministic proof of the tool cycle in tests, while `demo-read <path>` exposes the same fixed scripted cycle for manual terminal verification. The `prompt` command remains one-shot; each newly launched REPL starts with empty history. Within one running REPL, `/history <count>` shows only completed user/final-assistant pairs, never internal tool data.

This state exists only in the current process and is not written to disk. It is not a session, transcript, resume mechanism, or long-term memory. The slice makes **no** real model API call, credential or environment-variable read, network request, Bash execution, write operation, approval decision, session write, or persistence. A bare `leonervis-code` invocation in a noninteractive terminal explains that automation should use `leonervis-code prompt "..."` and exits nonzero, avoiding accidental hangs in pipes or CI.

The learning design records are [the single-turn loop decision](./docs/decisions/0001-foundation-0-single-turn-loop.md), [the deterministic REPL decision](./docs/decisions/0002-foundation-0-deterministic-repl.md), [the in-memory history decision](./docs/decisions/0003-foundation-1a-in-memory-text-history.md), [the bounded read-file tool-loop decision](./docs/decisions/0004-foundation-1b-bounded-read-file-tool-loop.md), [the provider-neutral model-routing decision](./docs/decisions/0005-foundation-2a-provider-neutral-model-routing.md), [the adapter-owned compatibility-policy decision](./docs/decisions/0006-foundation-2b-adapter-owned-compatibility-policy.md), [the non-streaming Anthropic-adapter decision](./docs/decisions/0007-foundation-3a-anthropic-non-streaming-adapter.md), [the local multi-provider-runtime decision](./docs/decisions/0008-foundation-3b-local-multi-provider-runtime.md), and [the named-profile/persistent-runtime decision](./docs/decisions/0009-foundation-3c-named-provider-profiles-and-runtime-manager.md).

## Development and verification

Run every project command through `uv run` so it uses the locked environment:

```bash
# Deterministic tests
uv run pytest

# Static checks
uv run ruff check .

# Check formatting; remove --check to apply formatting
uv run ruff format --check .
```

When dependencies change, update and verify the lockfile:

```bash
uv lock
uv lock --check
```

## Leonervis Code environment vs. target-workspace environment

Leonervis Code itself currently requires only Python, uv, Git, and the Python packages locked in `uv.lock`. It does **not** install build environments for projects it will eventually work on.

For example, when a future agent runs `npm test` in a Node project, Node/npm belongs to that **target workspace**. The same distinction applies to Rust/Cargo, Java, Docker, and a project's database. They are not prerequisites for launching Leonervis Code.

Accordingly, Docker, Docker Compose, Node.js, npm, pnpm, Rust, Java, Go, databases, Redis, message queues, web servers, reverse proxies, and Makefiles are not needed today.

## Current scope and future direction

The repository now includes:

- a reproducible Python 3.12–3.13 and uv environment with `uv.lock`;
- installable `leonervis-code` / `leonervis` entry points plus `python -m leonervis_code`;
- the structured `UserMessage` / `AssistantText` / `ToolUse` / `ToolResult` contract, deterministic scripted fake provider, an `AgentLoop` with atomic in-memory causal history, one bounded `read_file` tool, the Foundation 2B offline route policy, and a local multi-provider runtime covering Anthropic and the OpenAI-compatible family;
- named provider profiles with no credential value, user/project active precedence, atomic between-turn client/model switching, and a public `ProjectSession` API for other modules;
- a local real/fake REPL with a colored startup mark, Tab completion, `/history`, provider/status/model controls, and ordered process-local completed-turn history;
- an automation-friendly end-to-end path through the `prompt` command; and
- a minimal `pytest` and `ruff` quality toolchain.

The recommended next slice is Foundation 3D: add an append-only JSONL transcript, stable session identity, and resume on top of the verified `ProjectSession` and provider provenance while preserving tool causality as pairs. Streaming, automatic retry/fallback, file writes, approvals, and controlled Bash still require separate learning slices.

MCP, plugins, remote/server forms, multi-agent coordination, RAG, and background work are not permanently ruled out. They will be introduced only after a concrete need, boundary design, and test plan exist.

## Repository layout

```text
src/leonervis_code/
  core/                 # neutral conversation/tool and model-orchestration contracts
  agent/                # AgentLoop with bounded causal history and tool decisions
  tools/                # workspace-confined read_file tool only for now
  providers/            # adapters, route/factory, named profile store, and runtime manager
  session.py            # ProjectSession facade for other project modules
  cli/                  # command parsing, profile management, branding, REPL, and output
tests/                  # unit, integration, security, and end-to-end tests will grow here
docs/                   # architecture decisions, learning notes, and security design
scripts/                # reproducible local/CI maintenance commands, added when needed
learning-submodules/    # read-only learning references
```

Repositories under `learning-submodules/` are read-only study materials. They are not runtime dependencies, and product code must never import them. When their design informs an implementation, Leonervis Code will document both the inspiration and its own deliberate differences.
