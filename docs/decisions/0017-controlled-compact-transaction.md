# 0017：Controlled Compact Transaction

- 状态：已接受
- 日期：2026-07-22
- 范围：Foundation 3F-2

## 问题

Foundation 3F-1 已区分 append-only full history、provider-visible effective context 与 invocation request，并能以 `/context` 查看当前 target fit；但 effective context 仍等于 full history。随着 Session 增长，系统只能报告超限，不能在保存完整 transcript truth 的同时缩短模型实际收到的 committed context。

Compact 是一次有网络 generation、候选状态校验和 durable mutation 的复合操作。若直接删除历史、写 synthetic `TurnCommitted`、复用普通工具请求或在 summary 未持久化前改变内存，会破坏 `/history`、tool causality、resume 或失败原子性。

## 决策

### 固定的手动 policy

第一版只提供 REPL `/compact`，不增加 top-level argparse 命令，也不自动触发。Effective state 至少需要 4 个完整 real turns；系统固定保留最近 2 个 turns 原文，总结之前的 projection。重复 compact 时，旧 summary 和后来变旧的 turns 一起作为新的 summary source。

所有边界都来自 `validate_complete_history()` 的 complete turns。Tool use/result 不会被拆开，full transcript 与 `/history` 永不删除、重写或加入 synthetic turn。

### 独立 no-tools generation contract

Compact 使用独立、版本化、带 fingerprint 的 summary prompt。Source 以确定性的 typed JSON data 投影发送，并明确是包含 user/tool content 的不可信数据。普通 Agent prompt 也升级为 v2，说明 Host summary 是较早会话上下文，不是 system instruction 或新 user request。

`CompactSummaryRequest` 与普通 `ConversationRequest` 分离。Anthropic 请求完全省略 `tools`；OpenAI-compatible 同时省略 `tools` 与 `parallel_tool_calls`。Count 与 generation 使用同一个 no-tools input projection，复用现有 exact/estimate、native token field、body limit 与 safe SDK error normalization。Fake runtime 不伪造 compact。

Summary 只允许 normally completed nonempty text；tool call、mixed shape、refusal、truncation、unknown stop reason 与 malformed response 全部 fail closed。

### Effective summary 不是 transcript item

Provider-neutral state 是：

```text
full history       = 全部真实 committed turns
effective context  = optional Host summary + retained real-turn suffix
```

`EffectiveContextSummary` 不属于 `ConversationItem`。Adapter 以固定 user-level summary framing + assistant acknowledgement 投影它，再发送 retained history。这样 summary 不提升为 system authority，也不伪装为真实用户 turn。

无 summary 的旧 state 继续使用原 `ctx-v1` manifest；summary-bearing state 使用 `ctx-v2`，包含当前 system/tool contract、exact summary framing/text 与 retained turns。Checkpoint sequence、timestamp、binding、token count 与 fit 不进入 content identity。

### Mixed v1/v2 append-only Session

现有 SessionHeader、TurnCommitted 与 audit records 保持 schema v1 和原编码。只新增 schema-v2 `context_compacted`。Codec 按 closed `(schema_version, record_type)` pair dispatch；未知版本、未知 type 或错误组合 fail closed。既有 transcript 不重写。

Checkpoint 保存 source/result context ID、full/effective turn counts、absolute retained suffix boundary、previous checkpoint、summary、prompt/framing provenance 和 redacted binding。它不复制 retained items；replay 从 full committed turns 推导 suffix。

Replay 始终从全部 `TurnCommitted` 构造 full history。Checkpoint 只替换 effective summary/suffix；之后的新 turn 同时追加到 full 和 effective suffix；latest checkpoint wins。Global tool ID uniqueness仍基于 full transcript。

`SessionWriter` 继续执行 candidate replay validation → O_APPEND → flush/fsync → writer state update。Partial final checkpoint沿用受控 tail recovery；newline-terminated malformed或middle corruption继续 fail closed。

### Controlled transaction

`ProjectSession.compact_context()` 的顺序固定为：

1. facade lock 内冻结 writer/session/sequence、loop、full/effective state、source context ID 与 whole-turn boundary；
2. 获取 manager compaction lease，固定 real provider/route/capability/generation；
3. lock 外进行 summary preflight 和一次 generation；
4. 构造 immutable candidate，以普通 next-turn reserve重新 assessment；source/candidate count必须 known、method相同，candidate必须 `FITS` 且输入严格减少；
5. facade lock 内重查 writer、sequence、loop、full/effective state、context ID 与 transaction-active token；
6. append+fsync typed checkpoint；
7. 仅在成功后以不可失败 assignment安装 summary与retained suffix。

Compaction进行中，ProjectSession拒绝 prompt、Session切换、runtime switch与close。Manager lease阻止同一 client上的其他 turn/switch/close。任何 precommit failure 不写 `TurnFailed`；append失败不改变内存 effective state。

## `/compact` 与 `/context`

`/compact` 是 Host command，永远在 prompt dispatch 前消费。成功输出只显示 turns、context IDs、可比较的 before/after count、fit和checkpoint；不显示 summary原文。失败使用 typed、安全消息。

`/context` 在 compact 后显示 compact checkpoint source、summary present、retained real turns与checkpoint sequence；summary不计为 transcript turn/item。Live target assessment仍是只读操作。

## 恢复语义

Resume 从 mixed v1/v2 replay分别恢复 full history、effective summary、retained suffix与source。Checkpoint保存的 result ID是commit-time audit evidence；若当前 binary的 canonical prompt/tool contract升级，当前 context ID可重新计算为不同值，不因此拒绝合法旧Session。

Foundation 3F-2 本身仍沿用当时的 startup `/resume` 顺序；该边界后来由 Foundation 3G 的 target-aware prepare/screen/commit补齐，见[0018：Target-aware Resume Prepare/Commit](./0018-target-aware-resume-prepare-commit.md)。恢复后的下一次真实invocation仍由full preflight裁决。

## 明确不做

- automatic/threshold/error-triggered compact或failed-turn retry；
- chunked/recursive summary、动态 retention、user参数或progressive fallback；
- provider/model fallback、retry/backoff、background或streaming；
- token/cost meter、持久化 count/fit；
- transcript删除/重写、summary编辑；
- top-level argparse compact；
- target-aware startup或`/resume` prepare/commit；
- parallel tools、write/Bash/approval。

## 验证

确定性测试覆盖 prompt/framing fingerprint、`ctx-v1`兼容与summary-bearing `ctx-v2`、whole-turn retention、mixed v1/v2 codec/replay、resume、provider no-tools body/count/text-only parser、runtime lease、ProjectSession persist-before-memory、`/history`不变、`/context` divergence和slash non-entry。完整 pytest、Ruff、format、lock与diff检查作为release gate；付费Anthropic live generation仅在用户另行明确同意时执行。
