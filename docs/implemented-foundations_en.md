# Implemented Foundations and Design Evolution

> This document preserves the implementation narrative for Leonervis Code's completed learning slices. The README is intentionally limited to primary commands and usage entry points. The ADRs under [`docs/decisions/`](./decisions/) remain the authoritative records for each slice's rationale, boundaries, and verification evidence.
>
> [中文](./implemented-foundations.md) | English

## Contents

- [Canonical model system prompt](#canonical-model-system-prompt)
- [Foundation 3D: stable profile identity and durable Sessions](#foundation-3d-stable-profile-identity-and-durable-sessions)
- [Foundation 3C: named provider profiles and a real multi-turn REPL](#foundation-3c-named-provider-profiles-and-a-real-multi-turn-repl)
- [Foundation 3B: local multi-provider real-model path](#foundation-3b-local-multi-provider-real-model-path)
- [Foundation 2B: offline adapter-owned compatibility policy](#foundation-2b-offline-adapter-owned-compatibility-policy)
- [Foundation 1B: deterministic bounded read_file tool loop](#foundation-1b-deterministic-bounded-read_file-tool-loop)
- [Target-specific request counting and per-invocation preflight](#target-specific-request-counting-and-per-invocation-preflight)
- [Provider-owned model context capability](#provider-owned-model-context-capability)
- [ADR index](#adr-index)

## Canonical model system prompt

Leonervis Code builds a provider-neutral `SystemPromptSnapshot` from `src/leonervis_code/system_prompt.py`. The snapshot contains an explicit version, normalized text, and a domain-separated SHA-256 fingerprint. It is built once at the beginning of each user turn and remains pinned across every `read_file` continuation in that turn:

```text
SystemPromptSnapshot + neutral conversation history
  -> Anthropic Messages: top-level system + messages
  -> OpenAI-compatible: one leading system role + messages
  -> Scripted fake: record the same request snapshot
```

The first prompt is a stable model-visible contract. It contains no absolute workspace path, date, Session ID, provider/model/profile, endpoint, or credential. It declares only capabilities the Harness actually provides: selective access to one workspace-relative UTF-8 text file, bounded or truncated tool results, and evidence-based answers.

It explicitly does not claim write/edit, glob/grep, Bash/tests, network, approval, compaction, project-instruction loading, or multi-agent capabilities. Prompt instructions also do not replace the Host's hard path, encoding, and size constraints.

The system prompt is not a `ConversationItem`, so `/history`, `ProjectSession.history`, and append-only Session JSONL contain only the user/assistant/tool causal chain. A new turn after resume uses the current binary's canonical prompt. Session schema v1 does not yet record the exact historical prompt version/fingerprint; that audit requirement is reserved for a separate schema migration.

The **model system prompt** and the human-facing `leonervis[session8|runtime]>` **REPL prompt** are different interfaces: the former is a model-visible contract, while the latter is only a terminal status cue.

See [0012: first canonical model system prompt](./decisions/0012-first-canonical-model-system-prompt.md) for the detailed decision and [references/claw-code-prompts](./references/claw-code-prompts/README.md) for the Claw-Code prompt-structure study map.

## Foundation 3D: stable profile identity and durable Sessions

Profile-registry schema v3 uses an immutable UUID as reference identity, while each name remains a readable, mutable alias and each revision supports update-conflict checks. Schema v3 also adds an optional exact-model `context_window_tokens` override.

Legacy schema-v1 profiles deterministically map their original names to UUIDs. The reader accepts mixed v1, v2, and v3 user/project files, and a write upgrades only the file it actually changes:

```bash
uv run leonervis-code provider show vendor
uv run leonervis-code provider list --show-ids
uv run leonervis-code provider rename vendor vendor-new --if-revision 1
uv run leonervis-code provider replace vendor-new \
  --provider custom \
  --model vendor/model-v2 \
  --protocol openai-compatible \
  --base-url https://gateway.example/v1 \
  --if-revision 2
uv run leonervis-code provider migrate
```

Every `prompt` or REPL invocation creates or opens:

```text
<workspace>/.leonervis-code/sessions/<workspace-fingerprint>/<session-id>.jsonl
```

A Session uses append-only JSONL. A successful turn's user message, tool-use/result pairs, and final assistant text are written and fsynced as one complete commit record before in-memory history changes. Each open Session holds an exclusive writer lock.

Corrupt middle records, unknown schemas, and invalid tool pairing fail closed. Only an incomplete, unterminated crash tail can be truncated under controlled recovery, which also appends a recovery record.

```bash
uv run leonervis-code prompt "First turn"
uv run leonervis-code session list
uv run leonervis-code session show latest
uv run leonervis-code --resume latest prompt "Continue the previous turn"
uv run leonervis-code -C ../another-workspace --resume latest
```

A bare launch creates a new Session, while `--resume latest` continues the workspace's latest pointer. Inside the REPL, `/session new` starts empty history without changing the current runtime provider, and `/resume <id>` switches to existing history. `[current]` marks the destination of the next REPL prompt, `[latest]` marks the current `latest.json` target, and `open/closed` describes transcript lifecycle rather than lock ownership; a closed Session remains resumable.

Sessions and runtime providers are decoupled. The transcript records the profile ID/revision, provider/protocol, model, endpoint, and non-secret fingerprints actually used for each historical turn solely as audit provenance. After resume, the working provider still comes from this invocation's `--profile`/`--model`, workspace active selection, user active selection, or fake fallback. The runtime never reconstructs a client from historical binding metadata, and later profile rename, replacement, or deletion does not block resume.

Sending old history to a newly selected provider is an explicit runtime choice. If the current adapter rejects that history, the failed turn is not committed.

A local Session can contain user input, model responses, source excerpts, and tool results, so `.leonervis-code/` is sensitive runtime state and should not be committed, synchronized, or published. Known configured credential values are never written as binding data, but the system cannot generally detect an unknown secret that appears in user text or a file read by a tool.

`ProjectSession` exposes `session_id`, `transcript_path`, `session_info()`, `list_sessions()`, `new_session()`, `switch_session()`, and `resume=`. Switching Sessions replaces only durable history and preserves the current provider client.

See [0010: stable profile identity and durable Sessions](./decisions/0010-foundation-3d-stable-profile-identity-and-durable-sessions.md) for the detailed decision.

## Foundation 3C: named provider profiles and a real multi-turn REPL

Profile definitions live at:

```text
${XDG_CONFIG_HOME:-~/.config}/leonervis-code/providers.json
```

A workspace stores only its active profile ID in `.leonervis-code/provider.json`. Neither JSON file stores key values. The workspace directory is local runtime state and should be added to the target project's `.gitignore`.

```bash
# Built-in provider: protocol, default endpoint, and credential env come from the catalog
uv run leonervis-code provider add work-openai \
  --provider openai \
  --model gpt-5

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
uv run leonervis-code provider use local-qwen
uv run leonervis-code provider use work-openai --scope user
uv run leonervis-code provider clear --scope project
uv run leonervis-code provider remove vendor
```

Selection precedence is explicit `--profile` → explicit direct `--model` → workspace active → user active → fake/offline. `--profile NAME --model MODEL` uses a process-local model override on that profile endpoint without rewriting the profile:

```bash
uv run leonervis-code --profile work-openai --model gpt-5-mini \
  prompt "Explain this workspace"
uv run leonervis-code --profile work-openai
```

Both `provider use` and REPL `/provider use` resolve the route, validate the credential, and construct a candidate SDK client before writing active configuration and swapping the current client. On failure, the old active selection and client remain intact. `/model` is likewise atomic and allowed only between turns.

Complete neutral history and tool-use/result pairs survive a provider switch. If the new provider rejects old history, the failed turn is not committed.

Other project modules can use the public facade directly:

```python
from pathlib import Path
from leonervis_code import ProjectSession

with ProjectSession.open(Path.cwd(), profile="work-openai") as session:
    first = session.prompt("Explain the README first")
    session.set_model("gpt-5-mini")
    second = session.prompt("Continue")
```

`ProjectSession` also exposes `list_profiles()`, `use_profile()`, `use_profile_id()`, `clear_active()`, `status()`, `history`, and `turns`.

See [0009: named provider profiles and the runtime manager](./decisions/0009-foundation-3c-named-provider-profiles-and-runtime-manager.md) for the detailed decision.

## Foundation 3B: local multi-provider real-model path

With global `--model`, `prompt` resolves a real adapter through the shared resolver/factory:

```bash
export ANTHROPIC_API_KEY='...'
uv run leonervis-code --model anthropic/claude-opus-4-8 \
  prompt "Explain this workspace"

export OPENAI_API_KEY='...'
uv run leonervis-code --model openai/gpt-5 \
  prompt "Explain this workspace"

export XAI_API_KEY='...'
uv run leonervis-code --model xai/grok-3 \
  prompt "Explain this workspace"

export DASHSCOPE_API_KEY='...'
uv run leonervis-code --model dashscope/qwen-plus \
  prompt "Explain this workspace"

uv run leonervis-code --model ollama/qwen3:8b \
  prompt "Explain this workspace"

export OPENROUTER_API_KEY='...'
uv run leonervis-code --model openrouter/anthropic/claude-opus-4-8 \
  prompt "Explain this workspace"
```

The Anthropic path uses the official `anthropic` SDK. Every other built-in route reuses the official `openai` SDK through the Chat Completions wire adapter. Both clients are synchronous, non-streaming, and configured with `max_retries=0`.

Adapters declare only the current `read_file(path)` tool. Local `ReadFileTool` continues to enforce workspace containment, UTF-8, the 32 KiB cap, and the per-turn tool budget.

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

Explicit provider namespaces win. Only registered bare `claude-*`, `gpt-*`, `grok-*`, `qwen-*`, and `kimi-*` families are inferred deterministically; an unknown bare model is never guessed from installed credentials.

Route and adapter configuration contain no secret value. A key is read only when the factory constructs the selected SDK client. The runtime does not read `.env`, OAuth, or keyrings, and it does not implement streaming, automatic retries/backoff, fallback execution, request token preflight, compaction, parallel tools, or cross-workspace Session resume.

A real route can be previewed without constructing a client or accessing the network:

```bash
uv run leonervis-code --model openai/gpt-5 route
```

The fake fallback remains unchanged. If a workspace/user active profile exists, `prompt` and the bare REPL use that real profile even without an explicit selector:

```bash
uv run leonervis-code provider clear --scope project
uv run leonervis-code provider clear --scope user
uv run leonervis-code prompt "Hello"   # fake with no active profile; no network
uv run leonervis-code                   # fake REPL with no active profile; no network
```

See [0007: non-streaming Anthropic adapter](./decisions/0007-foundation-3a-anthropic-non-streaming-adapter.md) and [0008: local multi-provider runtime](./decisions/0008-foundation-3b-local-multi-provider-runtime.md) for the detailed decisions. Run live smoke checks only when the user explicitly chooses their own credentials, endpoints, and API budget.

## Foundation 2B: offline adapter-owned compatibility policy

`route` is a deterministic diagnostic surface for the control-plane and adapter-policy boundary:

```bash
uv run leonervis-code route

uv run leonervis-code route \
  --model beta \
  --max-output-tokens 32 \
  --fallback-model default

uv run leonervis-code route \
  --model beta \
  --temperature 0.2
```

The route resolver owns **hard** admission rules: valid provider/model selection, enabled status, required tool-use/streaming capabilities, canonical option types and ranges, fallback validity, and Harness-owned field protection.

A selected adapter owns provider-native wire names and documented **soft** compatibility behavior. The fake `beta` model demonstrates the distinction: its requested `temperature` is omitted as a known fixed-sampling incompatibility, and `route` reports that decision instead of silently changing the request or issuing a false hard error.

Provider-specific extensions currently have a controlled Python API path only. They cannot override `model`, messages, tools, streaming, token-limit fields, or adapter-generated parameter fields. The CLI intentionally does not accept arbitrary JSON body overrides.

The Foundation 2B form of `route` is completely offline: it constructs no provider client, reads no environment variables, makes no network call, and reveals no credential reference/value. A global-`--model` route uses the real resolver to show provider, protocol, wire model, base-URL source, and `configured/missing/not required` status, while still constructing no client and sending no request. A successful preview is not proof that the remote provider will accept a request.

See [0005: provider-neutral model routing](./decisions/0005-foundation-2a-provider-neutral-model-routing.md) and [0006: adapter-owned compatibility policy](./decisions/0006-foundation-2b-adapter-owned-compatibility-policy.md) for the detailed decisions.

## Foundation 1B: deterministic bounded read_file tool loop

The REPL and `prompt` command complete this minimal, testable path:

```text
terminal input → AgentLoop (one pinned canonical system-prompt snapshot + ordered causal context)
  → ScriptedFakeProvider → optional read_file within the current workspace
  → structured tool result → ScriptedFakeProvider → final text output
```

A provider response is either final assistant text or one `read_file` request. The loop returns final text only after the provider finishes, and commits the whole attempted turn—user input, any tool request/result, and final assistant text—only after that success.

Each user turn permits at most three file reads. A further request receives a structured limit error; another tool request after it stops deterministically.

`read_file` accepts only a relative path whose resolved target remains inside the current workspace. It rejects absolute paths, `..` or symlink escapes, missing paths, directories, unreadable files, and invalid UTF-8. It returns at most 32 KiB of UTF-8 text with a truncation marker. It cannot write, rename, delete, execute commands, search, or access the network.

The default `ScriptedFakeProvider` retains visible echo behavior and does not request tools by itself. Its scripted form provides deterministic tool-loop evidence in tests, while `demo-read <path>` exposes the same fixed cycle for manual terminal verification.

The `prompt` command remains one-shot, but every successful turn is auto-saved. Within one REPL, `/history <count>` shows only completed user/final-assistant turns from the current Session, never internal tool data.

Foundation 1B originally proved only process-local atomic history. Foundation 3D now persists each complete turn to workspace JSONL. A bare `leonervis-code` invocation in a noninteractive terminal explains that automation should use `leonervis-code prompt "..."` and exits nonzero, avoiding accidental hangs in pipes or CI.

See [0001: single-turn loop](./decisions/0001-foundation-0-single-turn-loop.md), [0002: deterministic REPL](./decisions/0002-foundation-0-deterministic-repl.md), [0003: in-memory text history](./decisions/0003-foundation-1a-in-memory-text-history.md), and [0004: bounded read_file tool loop](./decisions/0004-foundation-1b-bounded-read-file-tool-loop.md) for the detailed decisions.

## Target-specific request counting and per-invocation preflight

The runtime now pins the provider client, exact route, context/model-output capability, and redacted status in one immutable turn snapshot. That snapshot is the only provider-invocation entry point, so the initial request, every `read_file` continuation, and the final invocation after the tool limit are all preflighted again.

The decision keeps three concepts distinct: context window, model maximum output, and the current route's requested output reserve. `input + reserve == window` is allowed; a known `>` is rejected locally before sending; if any required fact is unknown, the Host does not guess and lets the provider remain the final authority. A rejected turn commits no conversation history and appends only a safe `TurnFailed` audit record.

For the official Anthropic endpoint, the official SDK's `messages.count_tokens` counts the same model/system/messages/tools projection shared with create. A failure safely degrades to a compact UTF-8 JSON `ceil(bytes / 4)` estimate. OpenAI-compatible Chat Completions always uses the matching local estimate rather than calling a count endpoint belonging to a different protocol.

Provider-profile schema v4 adds a `model_max_output_tokens` override, while private discovery-cache schema v2 can store positive context and model-output limits independently. `route`, `/status`, and `/provider current` show both limits and the requested reserve, but no successful last-request token meter is persisted and no automatic compaction occurs.

See [0014: target-specific request counting and preflight](./decisions/0014-target-specific-request-counting-and-preflight.md). The canonical model system prompt was reviewed: this slice adds Host-side send control without changing model-visible capabilities, so prompt version 1 and its fingerprint remain unchanged.

## Provider-owned model context capability

The runtime can resolve the current exact endpoint/model context window without fabricating unknown limits. Resolution follows a fixed precedence:

1. the named profile's exact override;
2. an exact official provider/endpoint/model built-in entry;
3. a fresh private XDG discovery cache entry;
4. provider-owned live discovery;
5. `unknown`.

The official Anthropic endpoint reuses the same official SDK client for the Models API. Generic OpenAI-compatible `/models` responses do not share a context-metadata contract and are therefore not probed blindly.

```bash
uv run leonervis-code provider add local-qwen \
  --provider custom \
  --model Qwen/Qwen3.5 \
  --protocol openai-compatible \
  --base-url http://127.0.0.1:11434 \
  --context-window-tokens 131072
uv run leonervis-code provider show local-qwen
uv run leonervis-code --profile local-qwen route
```

`provider show` labels user configuration as a `context window override`; offline `route` and runtime `/status` show the resolved value and source. Successful discovery is stored only at:

```text
${XDG_CACHE_HOME:-~/.cache}/leonervis-code/model-context-capabilities.json
```

The cache contains no credential value, raw provider body, or Session content. Profile-registry schema v3 reads v1/v2/v3, upgrades only the layer written, and supports explicit `provider migrate`.

This slice establishes capacity facts only. It does not count current request tokens, reject oversized requests, or compact history. See [0013: provider-owned model context capability](./decisions/0013-provider-owned-model-context-capabilities.md) for the detailed design.

## ADR index

1. [0001: Foundation 0 single-turn loop](./decisions/0001-foundation-0-single-turn-loop.md)
2. [0002: Foundation 0 deterministic REPL](./decisions/0002-foundation-0-deterministic-repl.md)
3. [0003: Foundation 1A in-memory text history](./decisions/0003-foundation-1a-in-memory-text-history.md)
4. [0004: Foundation 1B bounded read_file tool loop](./decisions/0004-foundation-1b-bounded-read-file-tool-loop.md)
5. [0005: Foundation 2A provider-neutral model routing](./decisions/0005-foundation-2a-provider-neutral-model-routing.md)
6. [0006: Foundation 2B adapter-owned compatibility policy](./decisions/0006-foundation-2b-adapter-owned-compatibility-policy.md)
7. [0007: Foundation 3A non-streaming Anthropic adapter](./decisions/0007-foundation-3a-anthropic-non-streaming-adapter.md)
8. [0008: Foundation 3B local multi-provider runtime](./decisions/0008-foundation-3b-local-multi-provider-runtime.md)
9. [0009: Foundation 3C named provider profiles and runtime manager](./decisions/0009-foundation-3c-named-provider-profiles-and-runtime-manager.md)
10. [0010: Foundation 3D stable profile identity and durable Sessions](./decisions/0010-foundation-3d-stable-profile-identity-and-durable-sessions.md)
11. [0011: decoupled REPL presentation and slash dispatch](./decisions/0011-decoupled-repl-presentation-and-slash-dispatch.md)
12. [0012: first canonical model system prompt](./decisions/0012-first-canonical-model-system-prompt.md)
13. [0013: provider-owned model context capability](./decisions/0013-provider-owned-model-context-capabilities.md)
14. [0014: target-specific request counting and per-invocation preflight](./decisions/0014-target-specific-request-counting-and-preflight.md)
