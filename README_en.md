<div align="center">

<img src="./docs/assets/leo-mark.png" alt="LEO mark" width="240">

# Leonervis Code

English | [中文](./README.md)

[![Python](https://img.shields.io/badge/Python-3.12%E2%80%933.13-3776AB?logo=python&logoColor=white)](./pyproject.toml)
[![uv](https://img.shields.io/badge/uv-managed-DE5FE9?logo=uv&logoColor=white)](https://docs.astral.sh/uv/)
[![pytest](https://img.shields.io/badge/pytest-8%2B-0A9EDC?logo=pytest&logoColor=white)](./pyproject.toml)
[![Ruff](https://img.shields.io/badge/Ruff-0.9%2B-D7FF64?logo=ruff&logoColor=black)](./pyproject.toml)

</div>

Leonervis Code is a learning-first coding-agent CLI prototype for local, single-user use. The model makes decisions, the host executes controlled tools within an explicit workspace boundary, and structured results return to the model.

> **Current status:** named provider profiles, real/offline runtimes, resumable Sessions, a bounded `read_file` loop, provider-owned model limits, target-specific preflight, switch-time screening, provider-neutral Effective Context, and manual resumable failure-atomic `/compact` are implemented. Automatic compaction, write tools, Bash, and approval flows are not yet implemented.

## Contents

- [Quick start](#quick-start)
- [Main commands](#main-commands)
  - [Run tasks and start the REPL](#run-tasks-and-start-the-repl)
  - [Configure providers](#configure-providers)
  - [Inspect routes and context windows](#inspect-routes-and-context-windows)
  - [Manage Sessions](#manage-sessions)
  - [REPL commands](#repl-commands)
- [Configuration and local state](#configuration-and-local-state)
- [Development and verification](#development-and-verification)
- [Detailed documentation](#detailed-documentation)
- [Current scope and next step](#current-scope-and-next-step)

## Quick start

Leonervis Code requires Python 3.12 or 3.13, the latest stable [uv](https://docs.astral.sh/uv/), and Git. The project uses `uv.lock` for a reproducible environment.

```bash
cd leonervis-code
uv sync
uv run leonervis-code
```

A bare invocation starts the REPL in a real terminal. Without a selected real provider, it uses the deterministic fake provider and performs no network access:

```text
leonervis[3fe4bb27|fake]>
```

The formal command is `leonervis-code`; `leonervis` is a shorthand. A module entry point is also available:

```bash
uv run leonervis --version
uv run python -m leonervis_code --help
```

## Main commands

The command's own help is always the authoritative parameter reference:

```bash
uv run leonervis-code --help
uv run leonervis-code provider --help
uv run leonervis-code session --help
```

### Run tasks and start the REPL

| Purpose | Command |
| --- | --- |
| Start a REPL with a new Session | `uv run leonervis-code` |
| Resume the workspace's latest Session | `uv run leonervis-code --resume latest` |
| Run one prompt | `uv run leonervis-code prompt "Explain this workspace"` |
| Run in another workspace | `uv run leonervis-code -C ../project prompt "Explain the project structure"` |
| Use a named profile | `uv run leonervis-code --profile work prompt "Explain the README"` |
| Override a profile's model temporarily | `uv run leonervis-code --profile work --model model-v2 prompt "Continue"` |
| Use a direct model route | `uv run leonervis-code --model anthropic/claude-opus-4-8 prompt "Explain the README"` |
| Show the version | `uv run leonervis-code --version` |

Use `prompt` for scripts and one-shot tasks, and the bare command for a stateful multi-turn REPL. Successful turns are automatically saved to the workspace Session transcript.

### Configure providers

A built-in provider gets its protocol, default endpoint, and credential environment-variable name from the catalog:

```bash
export ANTHROPIC_API_KEY='...'
uv run leonervis-code provider add work \
  --provider anthropic \
  --model claude-opus-4-8
```

A custom OpenAI-compatible endpoint requires an explicit protocol and base URL. A profile stores only the credential environment-variable name, never the key value:

```bash
export VENDOR_API_KEY='...'
uv run leonervis-code provider add vendor \
  --provider custom \
  --model vendor/model \
  --protocol openai-compatible \
  --base-url https://gateway.example/v1 \
  --api-key-env VENDOR_API_KEY \
  --context-window-tokens 1000000
```

Common profile-management commands:

```bash
uv run leonervis-code provider list
uv run leonervis-code provider show vendor
uv run leonervis-code provider use vendor              # workspace scope
uv run leonervis-code provider use vendor --scope user
uv run leonervis-code provider clear --scope project
uv run leonervis-code provider rename vendor vendor-new --if-revision 1
uv run leonervis-code provider remove vendor-new
uv run leonervis-code provider migrate
```

Selection precedence is explicit `--profile` → explicit direct `--model` → workspace active → user active → fake/offline. `provider use` prepares the candidate route, credential, and client before atomically switching; failure preserves the old configuration and client.

### Inspect routes and context windows

`route` is an offline diagnostic command. It constructs no provider client, reads no key value, and sends no network request.

```bash
uv run leonervis-code --profile vendor route
uv run leonervis-code --model openai/gpt-5 route
```

A named profile can configure the context window for its exact endpoint/model:

```bash
uv run leonervis-code provider replace vendor \
  --provider custom \
  --model vendor/model \
  --protocol openai-compatible \
  --base-url https://gateway.example/v1 \
  --api-key-env VENDOR_API_KEY \
  --context-window-tokens 1000000 \
  --if-revision 1

uv run leonervis-code provider show vendor
uv run leonervis-code --profile vendor route
```

The runtime resolves the context window and model maximum output independently: exact profile override → exact built-in catalog → fresh private discovery cache → provider-owned live discovery → `unknown`. Every provider invocation, including tool continuations, is preflighted with the current requested output reserve. The official Anthropic endpoint prefers an exact count, OpenAI-compatible routes use an explicitly marked deterministic estimate, and unknown facts are not guessed—the provider remains the final authority. REPL `/provider use` and `/model` also count the current committed history before committing a switch: known overflow preserves the old runtime/selection, while unknown evidence permits the switch with a warning. The next real invocation still runs full preflight. This capability does not compact automatically.

### Manage Sessions

```bash
uv run leonervis-code prompt "First turn"
uv run leonervis-code session list
uv run leonervis-code session show latest
uv run leonervis-code --resume latest prompt "Continue the previous turn"
uv run leonervis-code --resume <session-uuid>
```

A Session is workspace-bound and stores complete successful turns in append-only JSONL. Resuming restores history only; the current provider still comes from this invocation's CLI selector or active profile.

### REPL commands

| Command | Purpose |
| --- | --- |
| `/help` | Show control commands |
| `/history <count>` | Show recent complete turns in the current Session |
| `/status` | Show redacted runtime, model, and context-window status |
| `/context` | Read-only inspection of Effective Context, content ID, count, and target fit |
| `/compact` | Use the current real provider to summarize older complete turns and persist an effective-context checkpoint |
| `/provider list` | List named profiles |
| `/provider current` | Show the current profile/provider/model |
| `/provider use <name>` | Atomically switch the workspace's active profile |
| `/model <model>` | Override this process's model without editing the profile |
| `/session show` | Show the current Session |
| `/session list` | List workspace Sessions |
| `/session new` | Start an empty Session while preserving the runtime |
| `/resume <latest\|id>` | Switch Sessions while preserving the runtime |
| `/exit`, `/quit` | Exit normally |

Ctrl-D, EOF, or Ctrl-C while waiting for input also exits normally. `/context` does not invoke generation, mutate the Session, or write the transcript; after compaction it distinguishes the full transcript, summary, and retained real turns. `/compact` operates only with at least four complete effective turns, retains the latest two, and makes one no-tools summary request through the current real provider. Success appends and fsyncs one typed checkpoint while preserving full `/history`; fake runtime, unknown/non-reducing candidates, and all precommit failures commit nothing. Exact inspection or compact counting on an official Anthropic route may issue a count-only `messages.count_tokens` request, while OpenAI-compatible routes use a local estimate. Terminal colors are enabled only on a TTY; set `NO_COLOR=1` to disable them.

For a deterministic view of the bounded tool loop:

```bash
uv run leonervis-code demo-read README.md
uv run leonervis-code demo-read ../outside.txt   # verify workspace-escape rejection
```

`demo-read` is not a real model interface. It does not write files, execute shell commands, or access the network.

## Configuration and local state

| Path | Contents |
| --- | --- |
| `${XDG_CONFIG_HOME:-~/.config}/leonervis-code/providers.json` | user provider profiles and active selection |
| `<workspace>/.leonervis-code/provider.json` | workspace active profile |
| `<workspace>/.leonervis-code/sessions/.../*.jsonl` | Session transcripts |
| `${XDG_CACHE_HOME:-~/.cache}/leonervis-code/model-context-capabilities.json` | private context-capability discovery cache |

`.leonervis-code/` can contain user input, model responses, source excerpts, and tool results. Add it to the target project's `.gitignore`; do not commit, synchronize, or publish it. Configuration and the capability cache do not store known credential values, but the system cannot detect an unknown secret that appears in user text or source code.

## Development and verification

```bash
uv run pytest
uv run ruff check .
uv run ruff format --check .
uv lock --check
git diff --check
```

After changing dependencies, run `uv lock` before checking the lockfile. Leonervis Code does not install Node, Rust, Java, Docker, databases, or other build environments for a target workspace.

## Detailed documentation

- [Implemented foundations and design evolution](./docs/implemented-foundations_en.md): a consolidated account of the system prompt, tool loop, route policy, multi-provider runtime, profiles, Sessions, and context capability.
- [Architecture decision records](./docs/decisions/): complete problem statements, trade-offs, boundaries, and verification records for each learning slice.
- [Controlled Compact Transaction](./docs/decisions/0017-controlled-compact-transaction.md): manual `/compact`, no-tools summary generation, mixed Session schema, and persist-before-memory atomicity.
- [Provider-neutral Effective Context Snapshot](./docs/decisions/0016-provider-neutral-effective-context-snapshot.md): full/effective context boundaries, stable `ctx-v1` identity, and read-only `/context`.
- [Target-aware runtime switch UX](./docs/decisions/0015-target-aware-runtime-switch-ux.md): committed-context screening before switches, known-reject/unknown-allow behavior, and atomic audit semantics.
- [Target-specific request counting and preflight](./docs/decisions/0014-target-specific-request-counting-and-preflight.md): native-input counting, two distinct limits, and typed local rejection before every provider invocation.
- [Provider-owned model context capability](./docs/decisions/0013-provider-owned-model-context-capabilities.md): context/model-output limit resolution and cache design.
- [Canonical model system prompt](./docs/decisions/0012-first-canonical-model-system-prompt.md): model-visible contract, version, and fingerprint.
- [Stable profile identity and durable Sessions](./docs/decisions/0010-foundation-3d-stable-profile-identity-and-durable-sessions.md): profile UUID/revision and Session persistence.
- [Claw-Code prompt study map](./docs/references/claw-code-prompts/README.md): read-only reference structure and Leonervis-specific differences.
- [Harness-study](https://github.com/TsingFengIceberg/Harness-study): related Harness reading and learning notes.

## Current scope and next step

The only workspace tool today is bounded `read_file`. There are no write/edit, glob/grep, Bash/test, network, approval, streaming, automatic retry/fallback, parallel-tool, automatic-compaction, multi-agent, or remote-service capabilities yet.

The controlled compact slice now provides a manual typed checkpoint, stale-source checks, mixed v1/v2 replay, and failure atomicity. The recommended next slice is target-aware resume prepare/commit so startup and REPL Session switches can screen destination-runtime compatibility before changing resume audit or the latest pointer. [CLAUDE.md](./CLAUDE.md) and the ADRs record the complete scope, principles, and roadmap.
