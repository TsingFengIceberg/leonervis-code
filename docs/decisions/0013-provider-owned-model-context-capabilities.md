# 0013：Provider-owned model context capability

- 状态：Adopted
- 日期：2026-07-20

## 问题

Leonervis Code 已允许在一个 durable Session 中切换 provider/model，但 runtime 不知道目标模型的 context window。当前 route 只有每次请求的 `max_output_tokens`，没有模型容量、来源或 freshness；同一 causal history 在切换到较小窗口模型时可能直到 provider 返回通用 400 才暴露问题。

在实现 token counting、每次 invocation preflight 和 compact 前，需要先建立一个更小的事实层：由 provider 模块统一回答“这个 exact deployment/model 的 context window 是多少”，并把 unknown 保留为合法状态。

## 决策

### 中立能力与解析优先级

`providers/model_context.py` 定义 immutable target/capability/discovery contract。能力解析顺序固定为：

1. 命名 profile 对其 exact model 的显式 `context_window_tokens` override；
2. built-in exact catalog；
3. fresh persistent discovery cache；
4. provider-owned live discovery；
5. unknown。

第一版只记录 `context_window_tokens`，沿用 Claw-Code 的单窗口语义；当前 route 的 `max_output_tokens` 仍表示请求输出预留。真正出现 provider 需要前，不提前引入 combined/separate accounting 抽象。

Unknown 不是配置错误。它表示 Host 暂时无法本地证明容量；后续 request preflight slice 会允许 provider 作最终裁决，而不是猜测窗口。

### 与 Claw-Code 的采用与差异

Claw-Code 当前在 provider 模块用 hard-coded `model_token_limit()` 保存 model context/output 限制，unknown 返回 `None`；这个“能力事实属于 provider”的方向被采用。

它先解析 alias，再只匹配 model 最后一个 `/` 后的 basename。Leonervis 不采用这一点：built-in catalog 必须同时 exact 匹配 provider ID、官方 normalized endpoint 和 exact wire model。自定义 gateway 即使使用同名模型，也不能继承官方容量。

本切片尚不实现 Claw-Code 的本地 request estimate、Anthropic count-tokens、累计 usage auto compact 或 overflow progressive retry。这些分别属于后续 counting/preflight 与 controlled compact slice。

### Profile schema v3 override

Provider profile schema v3 增加可选 `context_window_tokens`，它是用户对 exact profile endpoint/model 的显式事实声明，并进入 profile fingerprint/revision。

Reader 支持 v1/v2/v3：旧 schema 解码为无 override；写操作只升级实际写入的文件；`provider migrate` 显式将可读旧文件升级为 v3；各版本保持 closed schema，future version fail closed。`/model` process-local override 不继承 profile 原模型的窗口。

### Built-in catalog

Catalog 只包含有官方依据、经审阅的 exact Anthropic official-endpoint/model 条目，并记录复核日期。它不使用 prefix/family fallback，也不适用于 Anthropic-protocol custom endpoint。

硬编码表是随 binary 发布、由 Git/tests 审阅的事实；runtime query 结果不会修改源码表。查询结果若未来经人工复核，可在新的 release 中晋升为 built-in entry。

### Discovery cache

成功的 live discovery 保存到独立 XDG cache：

```text
${XDG_CACHE_HOME:-~/.cache}/leonervis-code/model-context-capabilities.json
```

Cache schema v1 是 private、closed、bounded 的派生数据；key 包含 provider/protocol/base URL/exact wire model/credential env **名称**，不含 credential value/presence。只缓存 positive result，内部 TTL 为 24 小时；unknown/failure 不 negative-cache，过期值不作为权威 fallback。

Cache 拒绝 symlink/非普通文件，使用 private directory/file、跨进程 lock、temp + fsync + replace。读取不安全/损坏视为 miss并产生脱敏 diagnostic；live result 的 cache write 失败不丢弃已经得到的能力值。

### Provider-owned discovery

Anthropic adapter 复用同一个官方 SDK owner 的 `messages` 与 `models` resource。只有 built-in `anthropic` provider 且 endpoint 精确为 `https://api.anthropic.com` 时，resolver 才允许调用 `models.retrieve(exact_model)`；返回 ID 必须与请求 exact wire model 一致，`max_input_tokens` 必须为正整数。

Generic OpenAI Chat Completions `/models` 不提供统一 context metadata，OpenAI-compatible endpoint 更不构成 discovery 契约，因此当前显式 unsupported。未来 provider 有独立可信 API 时，在该 provider-owned adapter 内单独实现。

Discovery authentication/transport/rate-limit/malformed result 都安全降级为 unknown，不阻止 conversation runtime，不 retry，不保留 raw SDK body/error。

### Runtime snapshot 与锁边界

`RuntimeProviderManager` 同时持有 provider client、route 和 immutable capability snapshot。初始化、profile/clear/model switch 都为 candidate exact route 重新解析能力；`RuntimeStatus` 公开脱敏后的 tokens/source/freshness/diagnostic，`status()` 只读且永不联网。

Provider client construction、cache 和 Models API I/O 在 manager/store lock 与 profile transaction 外完成。切换采用 prepare/commit：锁外准备 candidate，再回锁验证 runtime generation 与 profile revision/selection 未改变，随后才持久化 active selection 并交换 client/capability；冲突关闭 stale candidate，旧 runtime 保持不变。

### CLI 可见性

- `provider add/replace --context-window-tokens N` 配置 override；`provider show` 明确显示 override；
- real `route` 保持离线，只显示 override/built-in/unknown 及 discovery eligibility；
- runtime `/status`、`/provider current` 与启动状态显示已解析的 `Context window` 和 source；
- compact REPL prompt 仍只显示 Session/runtime identity，不加入 model/token meter。

## System prompt 审阅

本切片只增加 Host/runtime metadata，不改变模型工具、行动或 model-visible context policy。Canonical system prompt 已审阅：保持 version 1 和现有 fingerprint，不新增 context/compact 声明。Leonervis 仍未执行 token tracking、request preflight 或 compact，因此 prompt 中“不具备 compact 能力”的边界保持真实。

## 非目标

- request token estimate/count、provider usage capture；
- 每次 provider invocation preflight 或 typed context overflow；
- 模型切换兼容性询问；
- `/compact`、自动 compact、LLM summary；
- full transcript/effective context 分离；
- Session schema 或 BindingSnapshot 变化；
- background refresh、stale-cache authority、generic compatible `/models` probing。

## 后续顺序

1. Provider-native request counting 与每次 invocation preflight；
2. target-aware model/provider/session switch UX；
3. append-only effective-context checkpoint 与三种 controlled compact：regular maintenance、manual `/compact`、model-switch/provider-overflow recovery。

Compact 将尽量沿用 Claw-Code 的近期消息优先、tool causality 和 `4 → 2 → 1 → 0` 有界恢复；Leonervis 已决定的改进包括最低保留量内按 token budget 向前多留原文、正式使用 bounded LLM semantic summary、完整 transcript 永不删除，以及 checkpoint/switch 的原子提交。

## 验证

确定性测试覆盖：

- override/catalog/cache/live/unknown precedence；
- exact provider/endpoint/model matching；
- TTL、no-negative-cache、cache permissions/corruption/symlink/redaction；
- profile v1/v2/v3 mixed read/migration/fingerprint；
- Anthropic exact Models API mapping与 shared owner；
- runtime switch/model override capability refresh及原子失败；
- offline route/runtime status/profile override 显示；
- fake、Session schema v1、tool loop 和 provider switch 行为不回归。
