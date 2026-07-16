# 0006：Foundation 2B 的离线 adapter-owned compatibility policy

- **状态**：已采纳
- **日期**：2026-07-16

## 要解决的问题

Foundation 2A 已能离线选择 provider/model、验证 fallback 和硬 capability，但它在 route resolver 中直接把 canonical 参数转换成 provider wire name，并把所有不支持参数都当作 route-time error。

真实多 Provider Coding Agent 不适合这样二元处理：某些参数是 Harness 正确运行的硬协议要求，必须拒绝；另一些是 provider/model 已知的软兼容差异，adapter 可以安全省略或重写，但用户必须知道实际发生了什么。

## 决策

采用 Claw-Code 的**政策骨架**，但不用其 Rust 实现、HTTP client、环境推断、OAuth、广泛 model registry 或 retry runtime：

```text
route resolver
  → 选择模型、验证硬 capability、验证 canonical type/range
  → 输出 adapter-ready canonical plan

request transformer
  → 选择 provider-native field name
  → 应用已知软兼容 rule
  → 输出 native preview + structured diagnostic
```

`AgentLoop`、`ConversationItem` 和工具因果链完全不感知 provider wire format，继续保持 Foundation 1B 的原子提交语义。

## Hard 与 soft 的边界

### 必须 fail closed 的 hard rules

- 未知、歧义或 disabled provider/model；
- primary/fallback 重复或无效；
- 当前调用明确要求而模型缺失的 `tool_use`、`streaming`、`system_messages` capability；
- canonical 参数类型或范围无效；
- 模型没有声明的 canonical 参数；
- provider extension 试图覆盖 Harness-owned field；
- 重复或空 extension key。

这些错误在任何 provider-native preview 之前终止；不允许 adapter 或上游 API 猜测修复。

### adapter 可以处理的 known soft rules

如果 model policy 明确声明 `OMIT_WITH_DIAGNOSTIC`，route 仍接受该 canonical 参数，但不将它交给 adapter-native 选项；adapter 输出稳定 diagnostic。

当前 fake `fake-chat/beta/1` 将 `temperature` 作为 fixed-sampling 规则演示：

```text
--temperature 0.2
  → route success
  → canonical adapter input 中省略 temperature
  → native preview 中省略 temperature
  → temperature_omitted_fixed_sampling diagnostic
```

这不是静默降级，也不是“模型不支持”的假 hard error。未知/未声明的参数仍然失败。

## adapter-owned request boundary

`ProviderRequestPlan` 只保留 canonical accepted parameters、它们的 handling、provider adapter key 和 extension data；不再包含 wire parameter。`providers/request_policy.py` 中的 pure fake transformers 才负责：

```text
fake-messages: max_output_tokens → max_tokens
fake-chat:     max_output_tokens → max_output_tokens
```

该 preview 没有 HTTP client、credential、endpoint、history serialization 或 `ConversationProvider`。它只证明未来真实 adapter 的职责边界。

## Provider extension 保护

provider-specific extension 当前只由 Python API 接收，尚无 CLI JSON 输入。它可保留非核心扩展键，但不得覆盖：

```text
model, messages, system, stream, tools, tool_choice,
max_output_tokens, max_tokens, max_completion_tokens, temperature
```

adapter 在生成 native field 后再次验证 collision，形成 defense in depth。未来 real adapter 的 extra body 只能在它构造 core payload 后受此规则合并；extension 不是绕过 Harness 请求协议的途径。

## 可见诊断

`route` 现在分开显示：

```text
canonical parameters
native preview
structured diagnostics
```

diagnostic 包含 stable code、severity、message、action，不包含 credential reference/value、raw payload 或 provider error body。无 adaptation 时显示 `diagnostics: <none>`。

## 参考与差异

Claw-Code 的 OpenAI-compatible request builder 对已知 reasoning/fixed-sampling 模型省略 tuning 参数，并由 provider diagnostics 解释；其 `extra_body` 受保护 field 限制。参考位置：

- `learning-submodules/claw-code/rust/crates/api/src/providers/mod.rs`
- `learning-submodules/claw-code/rust/crates/api/src/providers/openai_compat.rs`
- `learning-submodules/claw-code/rust/crates/api/src/error.rs`

Leonervis 采用「hard protocol gate、adapter-local soft adaptation、visible diagnostics、protected extensions」四项原则；不复制任何运行时代码，也没有实现 Claw-Code 的环境检测、真实 provider、transport、retry、streaming、token preflight、cache、OAuth 或 plugin 机制。

miniClaudeCode 仍只作为只读对照：它直接使用单一 Anthropic SDK，不能提供 multi-provider adapter policy。

## 明确不做的内容

Foundation 2B 不包含：

- 真实 Anthropic/OpenAI-compatible 或其他 provider client、SDK、HTTP、SSE 或 response parser；
- credential/environment/config/keyring/OAuth 读取；
- custom base URL、endpoint/model auto-detection 或 live model discovery；
- actual conversation/tool wire serialization；
- retry/backoff/fallback execution、attempt event 或 circuit breaker；
- CLI arbitrary JSON extension input；
- `prompt` / REPL 的 `--model` 或真实模型调用。

## 后续路径

1. 一个真实 provider adapter：注入受控 credential resolver，序列化当前 neutral history/tool contract，并归一化 typed transport failure；
2. OpenAI-compatible family：明确 adapter profile、custom base URL 和 model compatibility hint，不能仅凭 URL 推断能力；
3. 有界 retry/fallback：在 typed failure、attempt events 和明确 policy 存在后引入；
4. 配置和凭据 UX：仅在真实 adapter 已验证后设计持久配置。

## 验证证据

测试必须证明：

1. hard selector/capability/fallback/type/range/extension 错误仍提前失败；
2. fake transformer 而非 resolver 选择 native parameter name；
3. known `temperature` soft incompatibility 成功、被省略且可见；
4. unknown unsupported option 不会被泛化为静默 omit；
5. extension 不能覆盖核心或 adapter-generated field；
6. diagnostics 和 preview 顺序稳定且不泄露 secret；
7. Foundation 1B prompt/demo-read/REPL/module entry 行为不变；
8. 新生产路径没有网络、SDK、环境读取、凭据读取或 retry sleep。
