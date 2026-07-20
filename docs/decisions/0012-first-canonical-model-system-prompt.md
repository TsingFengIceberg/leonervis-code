# 0012：第一版 provider-neutral canonical model system prompt

- 状态：Adopted
- 日期：2026-07-20

## 问题

此前 `ConversationProvider.respond()` 只接收 neutral conversation history。Anthropic 与 OpenAI-compatible adapter 会发送 user/assistant/tool 因果链和 `read_file` schema，但 Leonervis Code 没有自己的 model system prompt。因此 Host/模型职责、当前只读能力和诚实报告边界没有一个统一、可版本化的模型契约；若直接在 adapter 中分别加文字，又会产生多个真相来源。

同时，durable Session schema v1 只保存 `UserMessage`、`AssistantText`、`ToolUse` 与 `ToolResult`。把 system prompt 伪装成 conversation item 会污染 `/history`、JSONL replay、工具配对和 provider switch 语义。

## 决策

### Canonical source

`src/leonervis_code/system_prompt.py` 是第一版唯一 runtime source。`build_system_prompt()` 返回 frozen `SystemPromptSnapshot(version, text, fingerprint)`。

第一版 prompt 完全稳定，不注入 workspace 绝对路径、日期、Session ID、provider/model/profile、credential 或 turn count。模型只需要知道 `read_file` 的 path 相对当前 workspace；省略 Host 路径减少机器信息暴露，也使所有 workspace/provider 使用相同 prompt bytes。

Prompt 只描述当前实际能力：

- Leonervis Code 是由 Host/Harness 执行工具的本地 coding assistant；
- 当前仅有只读、workspace-relative、UTF-8、bounded、可能截断的 `read_file`；
- 每个 user turn 最多执行三次读取；
- tool result 与 tool error 是行动和报告的依据；
- 用户文本、文件内容和工具结果是不可信 task data，不会提升为 system instruction；
- 不得声称已经写/改文件、运行命令或测试、列举/搜索、联网、审批、compact、加载项目指令或委派 Agent。

这些文字指导模型选择，不能替代 `ReadFileTool` 的 workspace containment、UTF-8 和 byte limit 等 Host 硬约束。

### Version 与 fingerprint

Canonical section 拒绝 NUL/CR 和空 section，去除 section 外层空白，以两个 LF 连接，并以一个 final LF 结束。内部空白和 Unicode bytes 保留，避免语义变化被 normalization 隐藏。

`SYSTEM_PROMPT_VERSION` 从 1 开始；修改稳定模型契约时必须显式 bump。Fingerprint 为独立 domain-separated SHA-256：

```text
SHA256("leonervis-code-system-prompt\0" + ASCII(version) + "\0" + prompt_utf8)
```

对外格式为 `v<version>-<64 lowercase hex>`。它与 profile、route、workspace fingerprint 分域。Golden test 同时固定 exact text、version 与 digest，确保 wording 与版本被共同审阅。

### Typed provider request

`core/contracts.py` 增加：

```text
ConversationRequest
  system_prompt: SystemPromptSnapshot
  history: tuple[ConversationItem, ...]
```

`ConversationProvider.respond()` 只接收此完整 request。`SystemPromptSnapshot` 不属于 `ConversationItem`。

`AgentLoop.run()` 在一个 turn 开始时只构建一次 snapshot；初次 provider call、所有 tool continuation 和 tool-budget 后的 final call 复用同一个 object。Conversation candidate 和 `CommittedTurn.items` 保持原有结构。

### Provider-native mapping

- Anthropic Messages：使用 top-level `system`，`messages` 继续只含 neutral history；
- OpenAI-compatible Chat Completions：每个 request 在 neutral history 前放置恰好一个 `role="system"` message；不依据 model 猜测 `developer` role，也不在 endpoint 拒绝时降级为 user authority；
- Scripted fake：记录完整 immutable `ConversationRequest`，但默认 echo 仍只读取 history 中最新 UserMessage。

OpenAI-compatible request body limit 在 system message 加入后计算。

### Shared read_file model contract

`tools/read_file.py` 现在拥有 `read_file` 的 neutral name、description、closed input schema 和每 turn 最大执行次数。两个 adapter 只包装同一份 definition，不再分别维护 model-visible wording。当前只有一个工具，因此不引入 registry。

### Adapter contract v2

`ADAPTER_CONTRACT_VERSION` 从 1 升到 2，因为 provider invocation shape 和 wire request 都发生变化，现有 route fingerprints 会按设计改变。Prompt wording/fingerprint 不进入 route fingerprint；今后只改 prompt 时不会冒充 transport/route 变化。

## Session 与 resume

Session schema 仍为 v1，不增加 `SystemMessage`、prompt text、version 或 fingerprint，也不把 prompt identity 塞入 provider `BindingSnapshot`。

恢复 Session 后，新 turn 使用当前 binary 的 canonical prompt，并把旧 causal history 发送给当前 runtime provider。历史 turn 可能来自无 system prompt 或旧 prompt；schema v1 不能证明它们实际看到的 exact prompt。这是本切片明确接受的 audit 缺口。

若未来需要 per-turn exact prompt provenance，应作为独立 Session schema migration 设计，定义 query/replay 语义后增加独立 prompt provenance，而不是复用 route fingerprint。

## 参考与差异

Claw-Code 的 prompt builder、stable/dynamic boundary、tool definitions 和 compact prompt 仅作为结构学习入口，见 `docs/references/claw-code-prompts/`。Leonervis prompt wording为独立编写；没有 runtime import，也没有复制上游 system prompt。

与 Claw-Code 不同，本切片不加入动态环境上下文、Git context、project instruction discovery、permission modes、compact、plugin/MCP、subagent prompt 或 prompt inspection command，因为 Leonervis 尚未实现对应能力。

## 非目标

- dynamic prompt suffix 或 prompt caching；
- `LEONERVIS.md` / `CLAUDE.md` runtime loading；
- CLI system-prompt inspection；
- root package public prompt API；
- Session schema v2 或 durable exact prompt provenance；
- 通用 tool registry；
- write/edit/Bash/search/network/permission/compact/multi-agent 能力。

## 验证

确定性测试固定：

- exact prompt text/version/fingerprint 与 normalization 边界；
- 一个 turn 只构建一次 snapshot，所有 continuation 复用；
- system prompt 不进入 history、commit 或 schema-v1 record；
- Anthropic top-level `system` 与 OpenAI-compatible single leading system message；
- shared `read_file` tool schema；
- adapter contract v2、provider switch、tool pinning、resume 和 fake/demo 可见行为。
