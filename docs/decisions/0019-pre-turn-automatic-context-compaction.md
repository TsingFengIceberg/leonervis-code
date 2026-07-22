# 0019：Pre-turn Automatic Context Compaction

- 状态：已接受
- 日期：2026-07-22
- 范围：Foundation 3H

## 问题

Foundation 3F-2 已提供失败原子的手动 `/compact`：它保留完整 append-only transcript 与 `/history`，只把 provider-visible context 改为 `Host summary + retained complete-turn suffix`。Foundation 3G 又让该 effective state 能被 target-aware resume 正确恢复和 screening。但用户仍需主动观察 `/context` 并提前执行 `/compact`；当新 prompt 的 exact initial request 已接近或超过 known context window 时，Host 尚不能自动维护 context 生命周期。

自动化不能实现为“等待远端报错、删除历史、再重试”。这种做法会混淆 committed history 与 failed turn，可能拆散 tool causality，也无法证明第二次请求仍使用同一 provider/runtime。自动 compact 必须在普通 generation 之前，根据 adapter 对 exact request 的现有计量证据做一次有界、可审计的 Host transaction。

## 决策

### 自动触发只发生在 turn 发送前

每个普通 prompt 先构造 exact initial request：当前 Effective Context、一个 pending user message、canonical system prompt、tool contract 与当前 requested output reserve。Adapter 继续拥有 native projection 和 count/estimate；自动策略不另写 token estimator。

固定 high-water policy 为：

```text
(input_tokens + requested_output_tokens) * 100
  >= context_window_tokens * 80
```

等于 80% 即触发。Decision matrix 为：

- known `FITS` 且低于 80%：直接执行 turn；
- known `FITS` 且达到 80%：尝试一次 proactive `high_water` compact；
- known `CONTEXT_EXCEEDED`：尝试一次 mandatory `overflow` compact；
- `MODEL_OUTPUT_EXCEEDED`：compact 无法修复 output reserve，直接拒绝；
- `UNKNOWN`：不猜测、不生成 summary，保留最终 invocation preflight；
- fake runtime：不伪造 count 或 summary，也不产生自动 compact 噪声。

### Pending user turn 必须冻结、计量但不进入 summary

`PreparedAgentTurn` 在修改任何 history 前冻结唯一的 `UserMessage`、committed `EffectiveContextSnapshot` 与 pending tuple。该 pending item 同时进入 source 和 candidate fit assessment，因此 high-water/overflow 判断覆盖真正将发送的 initial request；但 summary source只包含 previous durable summary 与较早的 committed complete turns。

Checkpoint 的 source/result context ID、summary 原文和 retained suffix都不包含 pending prompt。若 compact 成功，Prepared turn只替换 committed snapshot，继续持有同一个 pending user object/tuple；普通 provider initial request发送它一次，且只有完整 turn成功后才持久化一次。Compact、provider或持久化失败不会把 pending prompt写入 conversation history。

### 每个 prompt 最多一次 automatic compact

自动路径不递归，也不在 tool continuation、provider error或失败 turn后重试：

| Trigger | Compact 未提交 | Prompt 行为 |
| --- | --- | --- |
| `high_water` | 安全的 precommit、summary 或 candidate failure | 原 initial request 已 known `FITS`，发出 warning 后继续该 request |
| `overflow` | 不可 compact、summary failure、candidate unknown/non-fitting/non-reducing | 保留原 known overflow rejection，不发送普通 generation |

Stale/conflict 表示 prepared source 已失效，不能继续旧 request。Checkpoint append/fsync failure可能具有不确定 durability，proactive 路径也不得把它降级为可忽略 warning。若 checkpoint 已 durable commit并安装，之后普通 generation失败，checkpoint保留而 pending turn不提交。

### Count、summary、candidate 与 response 共享 turn runtime lease

一个 `provider_for_turn()` lease 固定 provider、route、capability、redacted status与runtime generation，并覆盖 initial assessment、可选 summary assessment/generation、candidate assessment及完整普通 tool loop。期间阻止其他 turn、runtime/profile/model switch、manual compact、resume transition与manager close；Exception和BaseException都通过同一 lifecycle释放 ownership。

最终 `respond()` 仍在每次 initial/continuation invocation前执行完整 preflight。自动 compact只消费发送前的已知证据，不削弱 provider invocation gate，也不把远端错误识别为可重试的 context signal。

### 复用 Controlled Compact Transaction

Manual `/compact` 与 automatic compact共用 Foundation 3F-2 的 prepare → runtime work → revalidate/commit/install transaction。第一版继续要求至少 4 个完整 effective turns、保留最近 2 个turns原文并summary更早turns；tool use/result不会被拆分。

Source和candidate必须具有可比较的known count method；candidate必须 known `FITS`且严格减少pending-inclusive input。Checkpoint先经candidate replay validation，再append+fsync，最后才安装内存summary与retained suffix。完整transcript与`/history`始终不删除、不重写。

### Schema v3、兼容 replay 与 trigger provenance

新的 `context_compacted` checkpoint使用closed schema v3，并增加：

- `trigger = manual | high_water | overflow`；
- `high_water_percent = 80` 仅对 `high_water` 合法；
- `manual` 与 `overflow` 的 threshold必须为 `null`。

既有schema-v2 checkpoint继续按legacy manual provenance读取，不重写旧行。Mixed replay从latest checkpoint恢复effective state；trigger只用于审计与`/context`展示，不进入`ctx-v2`内容identity。Token count、fit report、pending prompt/hash与failure diagnostic都不持久化。

### CLI 与公开 API 事件

Prompt可通过窄的core-owned sink接收 `AutoCompactionStarted`、`AutoCompactionCommitted` 与 `AutoCompactionNotApplied`。事件只包含trigger、context ID、计量方法和值、reserve/window、turn counts、checkpoint sequence和安全reason code，不包含pending prompt、summary原文、tool内容、credential或raw provider error。

One-shot `prompt`把事件写stderr，stdout继续只包含最终model response；REPL在最终response前显示独立状态消息。普通sink `Exception`只表示展示失败，不改变checkpoint或turn语义，也不触发重试；`BaseException`保留取消语义与已经发生的durable state。

## System prompt 审阅

自动 compaction 由 Host 在普通 turn发送前决定，模型仍不能主动请求或控制compact。Compacted context继续通过既有untrusted Host-summary framing提供给模型，因此本切片不改变模型可见tool、authority、workspace或summary contract。Canonical model system prompt保持version 2、exact text与golden fingerprint不变。

## 明确不做

- tool continuation中途compact或保留uncommitted tool suffix；
- 识别远端context error、字符串匹配或provider request retry；
- 一个prompt内多次、progressive、recursive或chunked compact；
- dynamic retention或用户可配置threshold；
- background compact、streaming或parallel-tool协调；
- 自动model/provider fallback或自动新Session；
- transcript删除/重写、summary查看或编辑；
- 持久化token/cost meter或成功fit observation；
- write/Bash/approval或其他tool surface变化。

## 验证

确定性测试覆盖79.x%不触发与exact 80%触发、reserve参与计算、known overflow、unknown/model-output/fake路径、四回合保留两回合、pending prompt不进入summary且只发送一次、proactive failure继续、mandatory failure不调用普通generation、assessment/summary/response共用一个runtime lease、checkpoint先于内存安装、schema-v2/v3 mixed replay与非法trigger组合、event内容脱敏及one-shot stdout/stderr边界。完整pytest、Ruff、format、lock与diff检查是release gate；真实provider smoke仍只在用户另行明确同意credential、网络与API费用时执行。
