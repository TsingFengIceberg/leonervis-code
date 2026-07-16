# 0005：Foundation 2A 的 provider-neutral 模型路由与请求规划

- **状态**：已采纳
- **日期**：2026-07-16

## 要解决的问题

Foundation 1B 的 `ConversationProvider` 已经让 AgentLoop 不依赖某个模型 SDK，但 CLI 仍固定构造 `ScriptedFakeProvider`。因此系统还不能回答一组不同于 conversation history 的问题：用户选择的 provider/model 是什么、别名如何消歧、模型是否具备当前 Agent 所需能力、同一语义参数怎样映射到不同 API，以及将来的失败是否可能重试或改走 fallback。

本切片建立这些问题的离线控制平面，不把它伪装成已接入真实模型：没有 HTTP、SDK、网络、环境变量、`.env`、配置文件、keyring 或凭据读取。

```text
route 输入 / 注入配置 / 静态 catalog
  → 解析 provider/model selector
  → 验证 provider、capability、canonical parameter 与 fallback chain
  → 输出已脱敏的 provider-native request plan
  → （未来）由 adapter factory 构造真正 ConversationProvider
  → 现有 AgentLoop
```

## 决策

### 路由属于 composition-time control plane

`AgentLoop` 继续只拥有 `UserMessage → ToolUse → ToolResult → AssistantText` 的因果 history、工具调用上限和原子提交。模型选择、认证来源、wire parameter 和传输错误不进入 `ConversationItem`；它们在创建一个 loop/session 所需的 provider adapter 前被解析和验证。

这避免 provider 细节污染可重放的 Agent causal chain，也允许未来每个 adapter 绑定一个已验证的 route。

### 最小不可变 contract

`core/orchestration.py` 定义 frozen records：

```text
SecretRef                    # opaque reference，不是 secret value
ProviderProfile              # provider 是否可用、adapter key、可选 SecretRef
CapabilitySet                # tool_use / streaming / system_messages
ParameterSpec                # canonical name → native wire name + type/range
ModelDefinition              # provider/model、aliases、capability、parameter specs
RouteRequest / ResolvedRoute # primary + ordered fallbacks
ProviderRequestPlan          # provider/model + ordered native parameters
ProviderFailure              # future adapter 的安全归一化失败
```

数组均用 tuple，保证 route plan 和测试顺序稳定。`SecretResolver` 与 `ProviderAdapterFactory` 仅作为未来扩展的 Protocol；此切片不会创建或调用它们。

### selector、capability 与 parameter 规则

- `provider/model-id` 是精确选择；已知 provider 前缀后面的 model ID 原样保留，可包含更多 `/`；
- 无 provider 前缀的 alias 只能匹配一个 enabled provider/model，否则要求用户使用 qualified selector；
- primary 和每个 fallback 都必须来自 enabled provider，且同时满足 caller 要求的 tool use、streaming 和 system-message capability；
- canonical option 当前仅有 `max_output_tokens` 与 `temperature`；catalog 指定每个模型是否支持、native wire name 与取值范围；
- 不支持、类型错误或越界一律 fail closed；不静默删除参数、clamp 值、替换默认值或降低 capability；
- fallback 是显式有序链，在启动前一次验证；重复候选或任何无效 fallback 让整个 route 不可用。

静态 catalog 故意只含两个 fake provider/model：同一个 canonical token limit 映射为 `max_tokens` 或 `max_output_tokens`，其中一个模型不支持 `temperature`。这以确定性方式证明参数兼容层的必要性，而不编造尚未验证的真实 vendor 行为。

### credentials 与显示边界

`ProviderProfile` 最多携带 `SecretRef`。它不允许存放 API key、OAuth token、Bearer value 或任意 plaintext secret。route 的输出只显示 `credential: configured` 或 `credential: not configured`，绝不显示 reference name 或 value。

未来的真实 adapter 才可在最窄的调用边界向 `SecretResolver` 请求凭据。route inspector、测试、错误和 transcript 不能解析或记录秘密。

### 归一化失败与 future retry policy

真实 adapter 将来必须将 SDK/HTTP 的**已知 typed error** 映射为：

```text
authentication / authorization / invalid_request / model_unavailable
rate_limited / timeout / transport / provider_unavailable
response_invalid / content_refusal
```

`ProviderFailure` 保留 provider/model、稳定 diagnostic code、安全 message、`retryable` 和可选 `retry_after_seconds`，不保存 raw response body 或 credential。Foundation 2A 只定义纯分类：rate limit、timeout、transport 和 provider unavailable 将来可考虑有界同模型重试；model unavailable 可考虑显式 fallback；认证、授权、invalid request、无效 response 与 refusal 默认不自动处理。

本切片不执行 retry、sleep、jitter、circuit breaker、请求 replay 或 fallback。自动 retry 可能重复计费，自动 fallback 可能改变质量、tool 行为和上下文兼容性；必须在有真实 adapter、attempt events 和用户明示 policy 后单独实现。

## CLI 表面

新增仅供诊断的离线入口：

```bash
leonervis-code route
leonervis-code route --model beta --max-output-tokens 32 --fallback-model default
leonervis-code route --model beta --require-streaming  # 安全失败
```

`route` 不构造 agent provider，不读取 credentials，不访问网络。它不能证明 API 可用，只能证明静态 catalog 下的选择、capability 和 parameter mapping 正确。

仍不向 `prompt` 或 REPL 添加 `--model`。它们继续固定使用 `ScriptedFakeProvider`；提前接受 model flag 会错误地承诺该模型已被调用。

## 参考与差异

- `learning-submodules/miniClaudeCode` 直接构造单一 Anthropic client，说明为何 Leonervis 需要 provider routing seam，但它没有可复用的 multi-provider control plane。
- Claw-Code 的 `rust/crates/api/src/providers/mod.rs`、`providers/openai_compat.rs` 与 `error.rs` 展示了 alias/metadata/capability diagnostics、OpenAI-compatible parameter handling 和安全失败分类与 HTTP client 分离的原则。Leonervis 借鉴「catalog data + normalized errors + preflight validation」；不复制 Rust crate、HTTP client、环境读取、retry 或庞大 provider registry。
- 用户点名的 LiteLLM、cc-switch、OpenClaw 与 Hermes 在当前研究环境中未能完整读取，因此没有把其未验证的具体实现或 provider 行为作为本切片事实。

## 明确不做的内容

Foundation 2A 不包含：

- 任何真实 provider（Anthropic、OpenAI-compatible、OpenRouter、Bedrock、Vertex、Azure、Ollama 等）SDK 或 HTTP adapter；
- conversation/tool schema 序列化、provider response 解析、streaming、token count、cache 或 live model discovery；
- 环境变量、`.env`、文件配置、OAuth、keyring、secret-manager 或 API key 读取；
- 真实 credential validation、retry/backoff、fallback execution、health check 或 circuit breaker；
- 对 AgentLoop history、tool budget、workspace boundary、session、transcript 或 permission 的修改；
- 向用户承诺 `prompt --model` 已调用真实模型。

## 后续切片

1. **Foundation 2B**：仅接入一个真实 provider adapter；注入 `SecretResolver`，把现有结构化 history/tool contract 映射到该 provider，使用 transport fake 证明 response/error normalization，再进行明确 opt-in 的 smoke test。
2. **Foundation 2C**：独立引入 OpenAI-compatible adapter family 与 custom base URL；每个服务的 capabilities/parameters 均需依据文档和测试验证，不能假设兼容协议完全一致。
3. **Foundation 2D**：加入可观测、明确 opt-in 的 bounded retry/fallback policy；记录每个 attempt，并禁止在前一尝试可能完成 tool call 后盲目 failover。
4. **Foundation 2E**：真实 adapter 已存在后，再设计配置文件 precedence 和安全 credential UX。

## 验证证据

测试和手动命令必须证明：

1. 默认、qualified selector、alias 消歧、disabled provider、slash model ID 与 fallback 规则稳定；
2. capability、unsupported parameter、type/range 和重复 fallback 均在 API 调用前失败；
3. 同一 canonical option 在 fake providers 上映射到不同 wire names；
4. route output 从不包含 `SecretRef` 名称或 secret 值，且没有 resolver 被调用；
5. `prompt`、`demo-read`、module entry、REPL 和 non-TTY Foundation 1B 行为不变；
6. `pytest`、Ruff formatting/lint 和 lockfile check 通过；
7. production route path 不读取网络、环境变量或任何 credential source。
