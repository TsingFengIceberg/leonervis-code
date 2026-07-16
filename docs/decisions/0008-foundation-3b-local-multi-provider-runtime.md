# 0008：Foundation 3B 的本地多 Provider Runtime

- **状态**：已采纳
- **日期**：2026-07-16

## 要解决的问题

Foundation 2A/2B 建立了离线路由与 adapter-owned compatibility policy，Foundation 3A 又证明了 Anthropic Messages 的真实 `read_file` 工具循环，但 CLI 仍直接构造 Anthropic adapter。项目目标不是只支持 `ANTHROPIC_API_KEY`，而是像 Claw-Code 一样，让一个本地 Harness 在不污染 `AgentLoop` 的前提下接入多个 API。

## 决策

采用 Claw-Code 的 provider-client 边界，独立用 Python 实现：

```text
--model selector
  → deterministic runtime resolver
  → non-secret RuntimeProviderRoute
  → provider factory 在 client construction 时解析 credential
  → Anthropic Messages 或 OpenAI-compatible adapter
  → 现有 AgentLoop / ReadFileTool
```

CLI 不再构造 Anthropic 专用 client。没有 `--model` 时仍使用确定性 fake provider，不读取任何 credential 或访问网络。

## 两个 Wire Protocol Family

### Anthropic Messages

使用官方 `anthropic` Python SDK，保留 Foundation 3A 的原生 content block、tool-use ID、严格 stop reason、typed error 与 `max_retries=0`。

### OpenAI-compatible Chat Completions

使用官方 `openai` Python SDK；同一个 adapter 服务于 OpenAI、xAI、DashScope、Ollama/local、OpenRouter 和受控 custom endpoint。中立 contract 映射为：

```text
UserMessage      → role=user
AssistantText    → role=assistant
ToolUse          → assistant.tool_calls[].function(arguments JSON text)
ToolResult       → role=tool + tool_call_id
```

response 只接受完整 text 或恰好一个合法 `read_file` function call。空 choices、多 choices、多工具、未知工具、畸形 JSON、mixed text/tool、截断、拒绝或未知 finish reason 都 fail closed。

## Built-in Provider Definitions

| Provider | Selector | Credential | Base URL |
| --- | --- | --- | --- |
| Anthropic | `anthropic/<model>` / `claude-*` | `ANTHROPIC_API_KEY` | Anthropic official |
| OpenAI | `openai/<model>` / `gpt-*`、`o1/o3/o4*` | `OPENAI_API_KEY` | OpenAI `/v1` |
| xAI | `xai/<model>` / `grok-*` | `XAI_API_KEY` | xAI `/v1` |
| DashScope | `dashscope/<model>` / `qwen-*` / `kimi-*` | `DASHSCOPE_API_KEY` | compatible-mode `/v1` |
| Ollama | `ollama/<model>` | 不要求 | `OLLAMA_HOST` 或 loopback default |
| Local | `local/<model>` | 不要求 | `OPENAI_BASE_URL` 或 loopback default |
| OpenRouter | `openrouter/<vendor/model>` | `OPENROUTER_API_KEY` | OpenRouter `/api/v1` |

显式 namespace 永远优先。只有文档列明的 bare model family 才允许确定性 convenience routing；未知 bare model 即使环境里已有某个 key 也失败，不做 ambient credential guessing。

## Model 与 Endpoint Policy

- Leonervis-owned routing prefix 在 official/local provider 上剥离；
- OpenRouter 保留完整 nested slug；
- custom endpoint 保留用户提供的完整 model ID；
- OpenAI-compatible base URL 必须为无 embedded credential/query/fragment 的绝对 HTTP(S) URL；
- base URL 若还不是 `/v1`、`/api/v1` 或完整 `/chat/completions`，补一个 `/v1`；
- custom endpoint 只由一次 invocation 的 `--provider-protocol openai-compatible --base-url ... [--api-key-env ...]` 开启；
- custom credential 只接受合法 ASCII 环境变量名；不持久化 key。

## Compatibility Policy

参考 Claw-Code 的 request policy，但用 Leonervis 的 pure helpers 和 tests 表达：

- `gpt-5*` 使用 `max_completion_tokens`；普通 compatible model 使用 `max_tokens`；
- `o1/o3/o4/gpt-5` 等 fixed-sampling family 省略 temperature；
- tool schema 为 closed object；
- `parallel_tool_calls=false`，与当前单工具 Loop 契约一致；
- OpenRouter/custom 的 slash model ID 不被误剥离；
- provider-specific request body limit 在 SDK call 前执行。

当前不开放 arbitrary extra body，所以用户无法覆盖 model、messages、stream、tools、tool choice 或 token-limit fields。

## Credential 与 Secret Boundary

`RuntimeProviderRoute` 只保存 credential environment-variable name，不保存 value。factory 才从注入的 environment 中读取选中 provider 的 key并立即构造 SDK client。route preview 只显示 `configured`、`missing` 或 `not required`，不会显示 env 名称或值。

不读取 `.env`、配置文件、OAuth、keyring 或 plaintext stored profile；不把 credential 放入 history、diagnostic、tests、README output 或 provider failure。

## Safe Failure

两个 adapter 共用 `ProviderAdapterError` 与 `ProviderFailureKind`，并只保留 stable code、provider/model、retryability、有限 Retry-After 和 printable request ID。raw SDK error、body、headers、request payload 和 key 均不保留。

本切片仍固定 SDK `max_retries=0`。`retryable` 只是未来 policy 的分类，当前不 retry、不 sleep、不 fallback。

## Claw-Code 参考与主动差异

主要参考：

- `learning-submodules/claw-code/rust/crates/api/src/types.rs`
- `.../api/src/providers/mod.rs`
- `.../api/src/client.rs`
- `.../api/src/providers/anthropic.rs`
- `.../api/src/providers/openai_compat.rs`
- `.../api/src/error.rs`

采用：统一中立消息、双 wire adapter、显式模型路由、compatible endpoint profiles、tool pairing、model compatibility、safe failure。

不复制 Claw-Code 当前缺陷：未接通的 stored provider config、plaintext key output、分散 alias source、credential guessing 覆盖显式选择、只在部分 runtime 生效的 fallback，以及非结构化 provider switch。

## 明确不做

Foundation 3B 不包含：

- streaming/SSE、thinking、usage/cost、cache、token count 或 server tools；
- automatic retry/backoff/fallback、live model discovery 或 provider marketplace；
- named persistent provider profiles、setup wizard、`.env`、OAuth/keyring；
- arbitrary JSON body、多个/并行工具、write/bash 或 session persistence；
- hosted gateway、billing platform、多用户 credential service。

## 验证证据

测试必须证明 built-in selector、显式优先级、base URL、prefix strip/preserve、custom endpoint、missing credential、Anthropic/OpenAI tool causality、strict response parsing、fixed-sampling/token-field policy、request body limit、safe failure、AgentLoop 原子性和所有原 fake/offline CLI 行为。
