# 0015：Target-aware Runtime Switch UX

- 状态：已接受
- 日期：2026-07-21
- 范围：Foundation 3E-3

## 问题

Foundation 3E-2 已在每次真实 provider invocation 前执行 target-specific preflight，但长生命周期 runtime 的 `/provider use`、`/model` 与 `ProjectSession` 切换仍可能先提交一个更小的目标，直到下一条用户消息才发现当前 Session 历史明确超限。用户会看到“切换成功”，随后才得到本地拒绝，而且不知道旧 runtime 是否仍然有效。

## 决策

Leonervis Code 在长生命周期 runtime 切换提交前，对当前 **committed conversation context** 执行 destination-specific screening：

1. `AgentLoop.committed_context_request()` 构造当前 canonical system prompt 与 exact committed causal history 的只读 snapshot；
2. snapshot 不追加 empty、placeholder 或 synthetic user message，不运行模型，不执行工具，也不修改 history/turns；
3. Manager 使用已经准备好的同一个 candidate provider、route 与 capability 做检查，不为 preview 再构造第二个 client；
4. `FITS` 与 `UNKNOWN` 允许提交，known `CONTEXT_EXCEEDED` 或 `MODEL_OUTPUT_EXCEEDED` 在 active selection/client 交换前拒绝；
5. 下一次真实 invocation 仍执行 3E-2 的完整 preflight，因为 switch probe 只证明“当前 committed context + 当前 reserve”在检查时的容量状态，不能预测下一条 user message 或后续 tool result。

空 Session 使用 `history=()` 的真实 native projection。Anthropic count endpoint 若不接受空 messages 或 count 失败，adapter 按既有规则安全退化为 serialized estimate；Host 绝不伪造消息来满足 endpoint。

## Adapter 终态契约

Anthropic 与 OpenAI-compatible adapter 共享各自已有的 native history serialization，但区分两种终态：

- actual `respond()` / `build_request()` 仍只接受等待 assistant response 的 invocation history，即以真实 `UserMessage` 或 `ToolResult` 结束；
- `count_input_tokens()` 额外接受空 history 或以 `AssistantText` 结束的完整 committed history。

因此计量与 create 继续使用同一 model/system/messages/tools projection，而不会放宽真实发送路径的因果验证。

## Switch-specific contract

新增三个 immutable/public contract：

- `RuntimeSwitchResult(status, fit_report)`：只在切换已经提交后返回；fake destination 的 `fit_report` 为 `None`；
- `RuntimeSwitchContextError(report)`：只表示提交前的 known overflow，明确当前 runtime 与 profile selection 未改变；
- `RuntimeSwitchAuditError(result)`：表示 Manager 已提交切换，但 Session `runtime_changed` append/fsync 失败。错误携带已生效结果，不能误报“切换没有发生”，也不做不可靠 client rollback。

`UNKNOWN` fail open，但 REPL 用 warning 明确 compatibility 未确认、没有删除历史、下一次 invocation 仍会 preflight。`FITS` 显示 input count method/value、reserve 与 window。known rejection 显示安全数字，并建议保留旧 runtime 或先 `/session new`；不展示尚未实现的 `/compact`。

## 原子性与审计

Manager 保留 prepare-outside-lock、generation CAS、profile identity/revision revalidation 与 store transaction：

- known rejection 或 stale candidate 会关闭 candidate provider；旧 provider、route、capability、generation、model override 与 active selection 不变；
- clear 若暴露 lower-priority real profile，则检查实际 effective destination；clear 到 fake 不需要 compatibility report；
- capability discovery cache 可保留 candidate preparation 的 derived side effect，因为它不含 Session 内容且不是 authoritative selection commit。

`ProjectSession` 的 facade `RLock` 覆盖 committed snapshot、可能的 count I/O、Manager commit 与 audit append，保证被计量的 history 与切换提交时一致。成功 fits/unknown 继续只追加既有 schema-v1 `RuntimeChanged`；rejected attempt 不写 conversation record、`TurnFailed` 或 runtime audit。Compatibility evidence 本切片不持久化。

`RuntimeStatus` 增加真实 runtime generation，`binding_from_status()` 将其写入 transcript provenance，避免 production binding 永远固定为 generation 0。

## 明确 defer

本切片不改变 `/resume`、启动 `--resume` 或 `ProjectSession.switch_session()`。这些操作先改变 durable Session 状态；要在兼容性拒绝时保持原子，需要后续把 `SessionStore.open()` 拆成 read-only prepare 与 commit。恢复后的第一次真实 prompt 仍受 3E-2 保护。

同样不实现自动 compact、历史删除、自动创建新 Session、provider retry/fallback、raw 400/413 解析、streaming、parallel tools、write/Bash/approval。

Standalone `provider use/clear` 没有 ProjectSession history，继续只做 configuration activation 与 candidate client validation，不声称 conversation compatibility。

## System prompt 审阅

已审阅 `src/leonervis_code/system_prompt.py`。本切片只增加 Host-side runtime control 与终端反馈，不新增模型工具、compact 或模型可见命令，因此 canonical prompt 保持 version 1 与现有 fingerprint。

## 验证

确定性测试覆盖 committed empty/complete/tool history、无 synthetic user、adapter count/send 终态分离、exact/estimated/unknown、boundary 与 known overflow、model-output precedence、candidate close/旧状态保留、Session generation/audit failure，以及 REPL success/warning/rejection 文案。真实 CLI observation 使用临时 workspace 与无网络 custom OpenAI-compatible profiles，避免 credential 与 API 消耗。

## 后续

下一切片进入 durable effective context 与 controlled compaction：先定义可审计的 effective-context representation、tool pair 保留与 explicit compact transaction，再讨论触发 UX。Resume compatibility prepare/commit 仍作为独立原子性切片处理。
