# 0007：Foundation 3A 的 Anthropic 非流式真实 Adapter

- **状态**：已采纳
- **日期**：2026-07-16

## 要解决的问题

Foundation 2B 已把 provider-neutral route 与 adapter-owned request policy 分开，但所有可执行路径仍使用 `ScriptedFakeProvider`。项目需要用一个足够窄的真实 API 切片证明：现有中立因果历史可以经过真实 provider adapter 往返，同时不把 Leonervis 宣称为已经完成的通用多 Provider 平台。

## 决策

首个真实 adapter 仅接入官方 Anthropic Python SDK 的 Messages API，并只由显式命令启动：

```bash
uv run leonervis-code --model <明确模型 ID> prompt "<prompt>"
```

数据流为：

```text
显式 `--model ... prompt`
  → 仅在该命令边界读取 ANTHROPIC_API_KEY
  → AnthropicConversationProvider 序列化中立 history
  → 官方 SDK messages.create（stream=False、max_retries=0）
  → 解析最终文本或一个 read_file tool_use
  → 现有 AgentLoop / ReadFileTool 执行工具
  → tool_result 回传后获得最终文本并原子提交本地 turn
```

默认 `prompt`、裸 REPL、`demo-read` 与 `route` 均保持 fake/offline，不会因为安装了 SDK 而访问网络。

## 为什么先选 Anthropic Messages

当前中立 contract 的 `UserMessage → ToolUse → ToolResult → AssistantText` 与 Anthropic Messages 的原生 content blocks 直接对应，尤其可以原样保留 provider 生成的 `tool_use_id` 和后续 `tool_result.tool_use_id`。若首先实现 OpenAI-compatible family，则还需同时解决 JSON 字符串 arguments、assistant tool calls、tool-role message 配对、不同 gateway 方言和兼容提示，无法保持本切片的小而完整。

因此 Foundation 3A 的“只做 Anthropic”是验证 provider seam，而不是放弃 Foundation 2B 的多 Provider 方向。OpenAI-compatible adapter family 仍作为后续独立切片。

## Adapter 边界

`AnthropicConversationProvider` 负责：

- 将当前全部中立因果历史序列化为 Anthropic native messages；
- 声明唯一工具 `read_file(path: string)`，并用 `additionalProperties: false` 关闭额外输入；
- 调用同步非流式 `messages.create`；
- 只接受当前 `AgentLoop` 可表达的 response shape；
- 将 SDK error 归一化为安全的 `ProviderFailure`。

`AgentLoop` 继续负责：

- workspace 工具执行；
- 每个 user turn 最多三次实际文件读取；
- unknown tool 与工具失败的 model-visible result；
- 只有获得最终 `AssistantText` 后才提交完整候选历史。

adapter 不直接读文件，也不能放宽 `ReadFileTool` 的相对路径、符号链接、UTF-8 与 32 KiB 上限。

## Native 因果映射

| Leonervis contract | Anthropic Messages |
| --- | --- |
| `UserMessage(text)` | `role=user` 的 text block |
| `AssistantText(text)` | `role=assistant` 的 text block |
| `ToolUse(id, read_file, path)` | `role=assistant` 的 `tool_use` block，原样保留 ID |
| `ToolResult(id, content, is_error)` | `role=user` 的 `tool_result` block，原样关联 ID |

序列化在 SDK 调用前 fail closed：空 history、顺序错误、未知工具、空 tool ID、tool result ID 不匹配或未闭合因果链都不会交给远端猜测修复。

## Response 限制

Foundation 3A 只接受：

1. 一个或多个 text blocks，且 `stop_reason=end_turn`，确定性拼接为一个 `AssistantText`；或
2. 恰好一个 `read_file` tool-use，输入必须严格等于一个字符串 `path`，且 `stop_reason=tool_use`。

`stop_reason=max_tokens` 代表不完整输出，会归一化为 `RESPONSE_INVALID` 而不提交；`stop_reason=refusal` 会归一化为 `CONTENT_REFUSAL`。空 response、thinking/server-tool 等未知 block、text 与 tool 混合、多 tool use、未知工具、额外/缺失/非字符串 path 或其他 unsupported stop reason 均 fail closed。不静默丢弃任何 block。

## Credential 与网络边界

- 只读取 `ANTHROPIC_API_KEY`；空白或缺失时，在构造 SDK client 前返回安全配置错误；
- key 只传给官方 SDK constructor，不进入 route plan、config dataclass、history、diagnostic、测试快照或文档输出；
- 不读取 `.env`、配置文件、keyring、OAuth、`ANTHROPIC_AUTH_TOKEN` 或 custom base URL；
- SDK 固定 `max_retries=0`，因此本切片没有自动重试、sleep、replay 或 fallback execution；
- CLI 必须显式提供 model ID，不自动选择或探测真实模型。

## 安全失败归一化

SDK typed errors 被映射为现有 `ProviderFailureKind`：认证、授权、无效请求、模型不可用、限流、超时、连接、provider unavailable 与 response invalid。安全 failure 可携带稳定 diagnostic code、retryable 分类、经验证的 `Retry-After` 秒数和可打印 request ID。

它不会保留或显示 raw exception 文本、raw body、请求 headers、序列化请求或 credential。`retryable=True` 只是分类，不代表当前会重试。

## 本地原子性与远端现实

provider 在首次或工具结果回传后的请求中失败时，`AgentLoop` 不提交候选 turn，因此本地 history 仍保持原子性。但已经发出的远端请求可能已经计费、留下 provider request log，或完成其他远端处理；本地 rollback 不能撤销这些远端事实。本切片没有把“本地未提交”等同于“远端未发生”。

## 参考与差异

采用 Claw-Code 的以下原则，独立用 Python 实现，不 import 或复制其 Rust runtime：

- adapter 拥有 provider request/response wire boundary；
- tool-use ID 与 tool-result 因果关系保持结构化；
- credential 只在窄 client-construction boundary 出现；
- provider error 转成安全、typed domain metadata。

主要只读参考位置：

- `learning-submodules/claw-code/rust/crates/api/src/providers/anthropic.rs`
- `learning-submodules/claw-code/rust/crates/api/src/types.rs`
- `learning-submodules/claw-code/rust/crates/api/src/error.rs`

Leonervis 明确不采用本切片不需要的 Claw-Code 功能：自制 HTTP/SSE client、OAuth、`.env`、base URL 推断、prompt cache、count-token preflight、streaming、retry/backoff、provider auto-detection 和大型 model registry。

miniClaudeCode 只作为“直接调用单一 Anthropic SDK”的只读对照；Leonervis 保留自己的 provider-neutral contract、adapter seam、安全错误和 recording-client 测试。

## 明确不做的内容

Foundation 3A 不包含：

- OpenAI-compatible、OpenRouter、xAI、Azure、Bedrock、Vertex、Ollama 或通用 provider menu；
- custom gateway/base URL、持久 profile、配置文件、`.env`、OAuth 或 keyring；
- streaming、thinking blocks、usage/cost 展示、prompt cache、token counting、Files API 或 server tools；
- parallel/multiple tools、generic tool schema、write/edit/bash、permission gate、session 或持久化；
- retry/backoff、fallback execution、model discovery 或自动模型选择；
- 将默认 REPL 或 `prompt` 改为付费/联网路径。

## 验证证据

确定性测试必须证明：

1. 全部当前 history item 的 native 映射与 tool ID 保持；
2. 无效因果链在 SDK 调用前失败；
3. text-only 与单 `read_file` response 正确解析，其他 shape fail closed；
4. recording client 只收到明确的 model、max_tokens、messages、tools 与 `stream=False`；
5. SDK typed exception 被安全分类，不泄露 raw body/key；
6. provider 在 read 后失败时，本地 history/turns 仍为空；
7. 缺少 key 与 model 的 CLI 错误安全且 nonzero；
8. 原有 fake/offline CLI 路径保持不变。

真实 smoke test 仅在用户明确选择使用自己的 API credential 和费用时手动执行，不属于自动测试。
