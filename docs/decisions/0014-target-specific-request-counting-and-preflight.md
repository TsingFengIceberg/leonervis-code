# 0014：Target-specific Request Counting 与 Per-invocation Preflight

- 状态：已接受
- 日期：2026-07-21
- 范围：Foundation 3E-2

## 问题

Foundation 3E-1 能解析 exact provider/endpoint/model 的 context window，但 Agent Loop 仍直接发送请求。Host 不知道完整 provider-native input 的 token 数，也不会在 `read_file` tool result continuation 后重新判断容量。已知超限只能等待远端返回通用错误，而且“上下文窗口”“模型最大输出”“本次输出预留”容易被混为一谈。

## 决策

Leonervis Code 在每一次 provider invocation 前执行 target-specific preflight，包括初始请求、每次工具 continuation 和工具预算耗尽后的最终请求。

Runtime Manager 为完整 turn 固定一个 immutable snapshot：provider client、resolved route、两类模型限制及 redacted status。Snapshot 是唯一的 `respond()` 入口，按以下规则判断：

1. `requested_output_tokens > model_max_output_tokens` 时本地拒绝；
2. context window unknown 时不猜测，允许发送；
3. context 已知时由 adapter 对其 native input projection 计量；
4. exact 或 estimated count 明确满足 `input + requested_output > context_window` 时本地拒绝；
5. count unknown 时 fail open，由 provider 最终裁决；
6. `input + requested_output == context_window` 允许发送。

本地拒绝使用独立的 typed `ContextPreflightError`，区分 `context_window_exceeded` 与 `model_output_exceeded`。错误只包含非敏感 target 和数值，不包含 prompt、history、tool result、credential、raw provider body 或 SDK exception。

## Native 计量责任

Adapter 拥有 native serialization，Agent Loop 和 Manager 不重建 provider payload：

- Anthropic official endpoint 使用官方 Python SDK `client.messages.count_tokens(model=..., system=..., messages=..., tools=...)`。计量和 create 从同一个 input-bearing projection 派生；count 请求不包含 `max_tokens`、`stream` 或 `temperature`。成功标记为 `exact`；count API 失败或返回 malformed value 时，不泄露原始错误并退化为本地 serialized-byte estimate。
- Anthropic custom endpoint 与全部 OpenAI-compatible Chat Completions 路径使用 compact UTF-8 JSON 的 `ceil(bytes / 4)` estimate，明确标记为 `estimated`。Generic gateway 没有统一 tokenizer/count contract，且 Leonervis 发送的是 Chat Completions，因此不调用 Responses token-count API。
- 没有 counter seam 的 fake/injected provider 返回 `unknown`，保持原有离线行为。

这一结构借鉴 Claw-Code 的单窗口 preflight、serialized-byte estimate 和 unknown fail-open；Leonervis 的差异是固定 exact runtime target、优先使用 Anthropic 官方 count endpoint，并独立表达 exact/estimated/unknown 和两类限制。

## Capability 与配置演进

`ModelContextCapability` 独立保存 context window 与 model max output 的 value/source/freshness。两者逐字段按 profile override → exact built-in catalog → fresh private discovery cache → provider-owned live discovery → unknown 补齐，不能因为一个字段命中就跳过另一个字段。

- Profile registry 升为 schema v4，增加 `model_max_output_tokens`；reader 兼容 v1-v4，写操作只升级实际写入层。
- Derived capability cache 升为 closed schema v2，可保存一个或两个 positive limits；旧 schema 安全 miss 后重新发现，不持久迁移，也不做 negative cache。
- Adapter route contract 升为 v3。

`route`、`/status` 与 `/provider current` 分别展示 context window、model max output 和 requested output reserve，但不显示 last-request token meter。

## Session 与失败原子性

Preflight wrapper 覆盖完整 turn。失败时 candidate history 和 turn 都不提交；如果此前已执行文件读取，本次 conversation candidate 仍整体丢弃。Schema-v1 `TurnFailed` 只记录 error class 和安全数值 message，不进入 replay history。本切片不迁移 Session schema，也不持久化成功 count observation。

## System prompt 审阅

已审阅 `src/leonervis_code/system_prompt.py`。本切片新增 Host 发送前控制，不新增模型可调用工具、compact 行为或其他模型可见能力，因此 canonical prompt 保持 version 1 和现有 fingerprint，不制造无语义 diff。

## 明确不做

- response usage、cost meter、成功 count 持久化或 last-request meter；
- target-aware model/provider switch UX；
- 解析 provider raw 400/413 文本或自动 retry/fallback；
- 静默删除历史、checkpoint、`/compact` 或自动摘要；
- streaming、parallel tools、write/Bash/approval。

## 后续

下一切片是 3E-3 target-aware switch UX：复用本 ADR 的 counter/fit report，在 runtime 切换提交前判断目标是否容纳现有 effective context。之后才进入 durable effective context 与 controlled compact。
