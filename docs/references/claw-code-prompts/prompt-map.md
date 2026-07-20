# Prompt Source Map

This map describes Claw-Code at git commit `4ea31c1bc91c4e9bcbd67d51c550c01e127e6d0d`. Descriptions are paraphrased; follow the linked local source for exact current text.

## Active canonical Rust runtime

| Prompt surface | Local source and stable anchor | Responsibility | Classification |
| --- | --- | --- | --- |
| Main system prompt | [`SystemPromptBuilder`](../../../learning-submodules/claw-code/rust/crates/runtime/src/prompt.rs) | Builds ordered identity, general policy, task execution, action safety, environment, project instruction, Git, runtime configuration, output-style, and appended sections. It exposes a boundary between relatively stable scaffolding and dynamic runtime context. | Canonical model-facing prompt |
| Project instructions | [`discover_instruction_files`](../../../learning-submodules/claw-code/rust/crates/runtime/src/prompt.rs) | Discovers bounded instruction content from Claw/Claude/Agents conventions and optionally imported editor-assistant rules, with per-file and aggregate budgets and deduplication. | Dynamic model-facing context |
| Git context | [`GitContext`](../../../learning-submodules/claw-code/rust/crates/runtime/src/git_context.rs) and prompt rendering in [`prompt.rs`](../../../learning-submodules/claw-code/rust/crates/runtime/src/prompt.rs) | Supplies branch, status, diff, history, and repository context to dynamic prompt sections. | Dynamic model-facing context |
| Runtime prompt assembly | [`conversation.rs`](../../../learning-submodules/claw-code/rust/crates/runtime/src/conversation.rs) and [`rusty-claude-cli/src/main.rs`](../../../learning-submodules/claw-code/rust/crates/rusty-claude-cli/src/main.rs) | Carries built prompt sections into provider requests; the CLI also exposes a system-prompt inspection path and section provenance. | Integration layer |
| Built-in tool descriptions | [`ToolSpec` and `mvp_tool_specs`](../../../learning-submodules/claw-code/rust/crates/tools/src/lib.rs) | Defines model-visible names, descriptions, JSON schemas, and permission requirements for the built-in tool set. At the inspected revision the implementation is the authority; older parity counts may differ. | Canonical tool prompt surface |
| Background agent instruction | [`build_agent_system_prompt`](../../../learning-submodules/claw-code/rust/crates/tools/src/lib.rs) | Adds task-scoped instructions for delegated/background agents and selects role-specific tool subsets. | Secondary model-facing system prompt |
| Runtime MCP tools | [`mcp_runtime_tool_definition`](../../../learning-submodules/claw-code/rust/crates/rusty-claude-cli/src/main.rs) | Converts server-provided MCP names, descriptions, and schemas into runtime tool definitions and adds fallback descriptions when needed. | Dynamic extension prompt surface |
| Plugin tools | [`PluginToolDefinition`](../../../learning-submodules/claw-code/rust/crates/plugins/src/lib.rs) | Lets enabled plugins contribute model-visible tool descriptions and schemas through the tool registry. | Dynamic extension prompt surface |
| Compact continuation | [`get_compact_continuation_message`](../../../learning-submodules/claw-code/rust/crates/runtime/src/compact.rs) | Builds the synthetic system continuation after compaction and instructs the model to resume directly. | Model-facing continuation prompt |
| Deterministic compact summary | [`summarize_messages`](../../../learning-submodules/claw-code/rust/crates/runtime/src/compact.rs) | Produces a structured local summary of older messages, tools, pending work, files, current work, and a timeline. It is not an LLM summarization request. | Deterministic synthetic context |
| Summary compression | [`compress_summary`](../../../learning-submodules/claw-code/rust/crates/runtime/src/summary_compression.rs) | Deduplicates, prioritizes, and bounds summary lines under fixed character and line budgets. | Deterministic context transform |
| Trident summaries | [`trident_compact_session`](../../../learning-submodules/claw-code/rust/crates/runtime/src/trident.rs) | Prunes superseded events and emits synthetic collapsed or clustered conversation summaries before standard compaction. | Deterministic synthetic context |
| Permission policy text | [`permissions.rs`](../../../learning-submodules/claw-code/rust/crates/runtime/src/permissions.rs) and [`permission_enforcer.rs`](../../../learning-submodules/claw-code/rust/crates/runtime/src/permission_enforcer.rs) | Determines allow/ask/deny behavior and generates model- or user-readable policy reasons. | Policy/result text, not one system prompt |
| Human approval prompt | [`CliPermissionPrompter`](../../../learning-submodules/claw-code/rust/crates/rusty-claude-cli/src/main.rs) | Renders the terminal approval question, tool input, mode, and default-deny confirmation. This is host-to-human UI and is not sent as the model's system prompt. | Human-facing prompt |

## Separate analog harness

[`claw-analog/src/lib.rs`](../../../learning-submodules/claw-code/rust/crates/claw-analog/src/lib.rs) contains a smaller, separate harness prompt. It varies identity and capability language by permission mode, adds workspace restrictions, presets, language hints, source-grounding and optional retrieval guidance, and builds its own tool descriptions. It should be studied as an alternate harness rather than merged with the canonical CLI prompt map.

## Reminder finding

The main prompt source tells the model that messages or tool results may contain tags resembling `system-reminder`. No active generator for such a tag was found in the inspected Rust runtime. Therefore this map does not claim that Claw-Code has a standalone reminder-injection subsystem.

## Historical metadata, not active prompt source

The companion Python/reference workspace under [`src/`](../../../learning-submodules/claw-code/src/) is not the canonical runtime. Its metadata points to an absent `archive/claude_code_ts_snapshot/src` tree:

- [`archive_surface_snapshot.json`](../../../learning-submodules/claw-code/src/reference_data/archive_surface_snapshot.json) records the former archive's shape;
- [`tools_snapshot.json`](../../../learning-submodules/claw-code/src/reference_data/tools_snapshot.json) lists historical `tools/*/prompt.ts` path hints;
- [`commands_snapshot.json`](../../../learning-submodules/claw-code/src/reference_data/commands_snapshot.json) points to historical compact and permission command paths;
- [`subsystems/constants.json`](../../../learning-submodules/claw-code/src/reference_data/subsystems/constants.json) points to historical prompt/system/output-style modules.

These files preserve names and inventory metadata, not the missing prompt bodies. They must not be cited as active source or reconstructed into purported original prompts.

## Excluded evidence

Tracked transcripts under the Claw-Code workspace are execution artifacts, not normative prompt source. They may contain machine-specific paths, model-generated content, and stale behavior, so this reference neither mines nor republishes them.
