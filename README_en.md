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

> **Current status: Foundation 0 is complete.** The project now runs one deterministic local `prompt` command. It proves the first CLI → AgentLoop → provider control flow with a fake provider; it is not yet a runtime that can perform real agent tasks.

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

# 3. Run the Foundation 0 deterministic prompt
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

## Foundation 0: single deterministic loop

The current command completes only this minimal, testable path:

```text
prompt command → AgentLoop → DeterministicFakeProvider → text output
```

Every `prompt` invocation performs exactly one provider call and displays its returned text unchanged. The default fake provider is stable and reproducible, making it suitable for first verifying Harness control flow and error propagation boundaries.

This slice makes **no** model API call, credential or environment-variable read, network request, filesystem/tool action, session write, or workspace access. It has no REPL, approval, or persistence. Real providers, tool loops, and other runtime capabilities will arrive only in separately designed, implemented, and tested slices.

The learning design record is available in [the Foundation 0 decision](./docs/decisions/0001-foundation-0-single-turn-loop.md).

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
- the `PromptProvider` contract, deterministic fake provider, and one-turn `AgentLoop`;
- an end-to-end Foundation 0 path through the `prompt` command; and
- a minimal `pytest` and `ruff` quality toolchain.

The next slice can introduce explicit assistant-content contracts and a bounded multi-turn/tool loop while retaining the provider boundary established here. Real model integration, file tools, write approvals, sessions, and controlled Bash each still need their own learning slice.

MCP, plugins, remote/server forms, multi-agent coordination, RAG, and background work are not permanently ruled out. They will be introduced only after a concrete need, boundary design, and test plan exist.

## Repository layout

```text
src/leonervis_code/
  core/                 # neutral contracts; currently PromptProvider only
  agent/                # bounded, one-turn AgentLoop
  providers/            # deterministic fake provider only for now
  cli/                  # command parsing, composition, and terminal output
tests/                  # unit, integration, security, and end-to-end tests will grow here
docs/                   # architecture decisions, learning notes, and security design
scripts/                # reproducible local/CI maintenance commands, added when needed
learning-submodules/    # read-only learning references
```

Repositories under `learning-submodules/` are read-only study materials. They are not runtime dependencies, and product code must never import them. When their design informs an implementation, Leonervis Code will document both the inspiration and its own deliberate differences.
