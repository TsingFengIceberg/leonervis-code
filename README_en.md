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

> **Current status: Foundation 1A in-memory text history is complete.** Invoking the command with no subcommand opens a local interactive terminal. Later inputs in the same REPL process see prior completed user/assistant text pairs; this is not yet a runtime that can perform real agent tasks.

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

The command shows a colored LEO mark, version, current directory, and Foundation 1A status before displaying:

```text
leonervis>
```

Enter any nonblank text for a deterministic result:

```text
leonervis> Explain the Harness boundary
Fake response: Explain the Harness boundary
```

The REPL currently supports only local controls:

```text
/help              show controls
/history <count>   show the most recent complete conversation turns
/exit or /quit     exit normally
Ctrl-D / EOF       exit normally
Ctrl-C             exit normally
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

## Foundation 1A: deterministic prompt, REPL, and in-memory history

The current REPL and `prompt` command complete only this minimal, testable path:

```text
terminal input → AgentLoop (ordered in-memory history) → ScriptedFakeProvider → text output
```

Every nonblank input performs exactly one provider call and displays its returned text unchanged. Within one running REPL process, the loop retains ordered text history: the second provider call receives the first successful user/assistant pair plus the new user input. The `prompt` command remains one-shot and begins with empty history on every invocation; each newly launched REPL also begins empty.

This history exists only in the current process. The REPL can display complete turns through `/history <count>`, but it is not written to disk and is not a session, transcript, resume mechanism, or long-term memory.

This slice makes **no** model API call, credential or environment-variable read, network request, filesystem/tool action, session write, or workspace access. It has no real model, approval, or persistence. A bare `leonervis-code` invocation in a noninteractive terminal explains that automation should use `leonervis-code prompt "..."` and exits nonzero, avoiding accidental hangs in pipes or CI.

The learning design records are [the single-turn loop decision](./docs/decisions/0001-foundation-0-single-turn-loop.md) and [the deterministic REPL decision](./docs/decisions/0002-foundation-0-deterministic-repl.md).

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
- the `TextMessage` / `ConversationProvider` contract, deterministic scripted fake provider, and an `AgentLoop` with in-memory history;
- a local REPL with a colored startup mark, minimal controls, Tab completion, and ordered process-local text history;
- an automation-friendly end-to-end path through the `prompt` command; and
- a minimal `pytest` and `ruff` quality toolchain.

The next slice can introduce richer assistant-content contracts and a bounded tool loop while retaining the text causal chain established here. Real model integration, file tools, write approvals, sessions, and controlled Bash each still need their own learning slice.

MCP, plugins, remote/server forms, multi-agent coordination, RAG, and background work are not permanently ruled out. They will be introduced only after a concrete need, boundary design, and test plan exist.

## Repository layout

```text
src/leonervis_code/
  core/                 # neutral text contracts: TextMessage and ConversationProvider
  agent/                # AgentLoop with ordered in-memory text history
  providers/            # deterministic scripted fake provider only for now
  cli/                  # command parsing, brand rendering, REPL, and terminal output
tests/                  # unit, integration, security, and end-to-end tests will grow here
docs/                   # architecture decisions, learning notes, and security design
scripts/                # reproducible local/CI maintenance commands, added when needed
learning-submodules/    # read-only learning references
```

Repositories under `learning-submodules/` are read-only study materials. They are not runtime dependencies, and product code must never import them. When their design informs an implementation, Leonervis Code will document both the inspiration and its own deliberate differences.
