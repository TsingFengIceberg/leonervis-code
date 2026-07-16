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

> **Current status: Foundation 1B's bounded `read_file` tool loop is complete.** Invoking the command with no subcommand opens a local interactive terminal, and later inputs in the same REPL process see prior completed user/assistant text pairs. The Harness can now represent one safe read-only file request and result structurally, but the default fake provider does not request tools, so this is not yet a runtime that can perform real agent tasks.

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

The REPL currently supports only local controls:

```text
/help              show controls
/history <count>   show the most recent complete conversation turns
/exit or /quit     exit normally
Ctrl-D / EOF       exit normally
Ctrl-C             exit normally
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

The learning design records are [the single-turn loop decision](./docs/decisions/0001-foundation-0-single-turn-loop.md), [the deterministic REPL decision](./docs/decisions/0002-foundation-0-deterministic-repl.md), [the in-memory history decision](./docs/decisions/0003-foundation-1a-in-memory-text-history.md), and [the bounded read-file tool-loop decision](./docs/decisions/0004-foundation-1b-bounded-read-file-tool-loop.md).

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
- the structured `UserMessage` / `AssistantText` / `ToolUse` / `ToolResult` contract, deterministic scripted fake provider, an `AgentLoop` with atomic in-memory causal history, and one bounded `read_file` tool;
- a local REPL with a colored startup mark, minimal controls, Tab completion, `/history`, and ordered process-local completed-turn history;
- an automation-friendly end-to-end path through the `prompt` command; and
- a minimal `pytest` and `ruff` quality toolchain.

The next slice can introduce additional read-only workspace tools or a separately designed real provider adapter while retaining the structured causal chain established here. File writes, approvals, sessions, and controlled Bash each still need their own learning slice.

MCP, plugins, remote/server forms, multi-agent coordination, RAG, and background work are not permanently ruled out. They will be introduced only after a concrete need, boundary design, and test plan exist.

## Repository layout

```text
src/leonervis_code/
  core/                 # neutral structured conversation and tool contracts
  agent/                # AgentLoop with bounded causal history and tool decisions
  tools/                # workspace-confined read_file tool only for now
  providers/            # deterministic scripted fake provider only for now
  cli/                  # command parsing, brand rendering, REPL, and terminal output
tests/                  # unit, integration, security, and end-to-end tests will grow here
docs/                   # architecture decisions, learning notes, and security design
scripts/                # reproducible local/CI maintenance commands, added when needed
learning-submodules/    # read-only learning references
```

Repositories under `learning-submodules/` are read-only study materials. They are not runtime dependencies, and product code must never import them. When their design informs an implementation, Leonervis Code will document both the inspiration and its own deliberate differences.
