# 0016：Provider-neutral Effective Context Snapshot 与只读 `/context`

- 状态：已接受
- 日期：2026-07-21
- 范围：Foundation 3F-1

## 问题

Foundation 3E 已能在真实 provider invocation 和 runtime switch 前执行 target-specific context assessment，但 transcript truth、provider-visible committed context 和一次 invocation request 仍共享同一个 history 概念。未来若直接在这个耦合点实现 compact，容易让 `/history` 与 durable transcript 被误改、拆散 tool pair，或让 `/context`、switch screening 和真实 preflight 使用不同投影。

系统还缺少一个只读入口来回答：当前发送给模型的 committed context 是什么来源、包含多少完整 turns/items、provider-neutral 内容身份是什么，以及在当前 target 下是 exact/estimated/unknown、fits 还是 exceeded。

## 决策

### 三层状态边界

Leonervis Code 明确区分：

```text
full history       = append-only Session transcript 派生的完整 conversation truth
effective history  = 下一次 provider invocation 可见的 committed context
ConversationRequest = effective history + 当前未提交 turn suffix 的一次调用 DTO
```

`AgentLoop` 分别维护 `_full_history` 与 `_effective_history`。3F-1 中二者在 restore、成功 commit 和 resume 后始终相等；失败 turn 不进入任何一方。这是行为中立的 future-compaction seam，不是 compact 实现。

`EffectiveContextSnapshot` 一次冻结当前 canonical system prompt、neutral tool definition、full/effective histories 与 source。真实 turn 的每个 tool continuation 都从同一个 snapshot 加上当前 pending suffix 派生 `ConversationRequest`，因此 system/tool/context snapshot 在 turn 内固定。Runtime switch screening 与 `/context` 也消费同一 effective-context seam。

### 严格完整历史

共享 validator 只接受：

```text
UserMessage, (ToolUse, matching ToolResult)*, AssistantText
```

Tool use/result 必须相邻且 ID 匹配；ID 在完整历史内全局唯一。未知 item、错误 scalar type、半个 pair、交错/并行样式 tool sequence 都 fail closed。`AgentLoop` restore、effective-context construction 与 Session replay 使用同一个 complete-history规则；Session codec继续叠加 schema、closed-field、大小、NUL、timestamp 与 binding 约束。

Adapter serializers仍拥有不同职责：实际 invocation 可以以 `UserMessage` 或 `ToolResult` 结束并等待 assistant，因此不以 complete-history validator 替代 adapter send validation。

### Stable context identity

每个 snapshot 可计算：

```text
ctx-v1-<domain-separated SHA-256>
```

Canonical manifest 使用 compact sorted UTF-8 JSON，包含：

- effective-context representation version；
- exact current system prompt version/text/fingerprint；
- current neutral `read_file` definition exact content；
- validated effective turns、turn boundaries 与每个 item 的 closed projection，包括 tool result flags。

明确排除 Session ID/path、full-only transcript history、record sequence/timestamp、audit/binding、provider/profile/model/route/runtime generation、token count/limit/decision/diagnostic。Context ID 是 provider-neutral model-visible content identity，不是 tamper-proof transcript proof，也不持久化到 JSONL。

`read_file` identity 与 adapter schema 都从现有 `read_file_model_definition()` 生成，不维护第二份 tool contract。

## 当前 target assessment

`RuntimeProviderManager.assess_current_context()` 在 manager lock 下固定 current provider/route/capability/status 并执行只读 assessment。它：

- 复用 `assess_context_fit()` 与 adapter-owned `count_input_tokens()`；
- 不调用 generation，不设置 `_turn_active`，不增加 generation，不改变 selection/runtime；
- fake runtime 返回明确 unavailable/unknown，不伪造 reserve、limits 或 count；
- model-output limit precedence、boundary、counter failure redaction 与 unknown policy保持不变。

Official Anthropic target 在 context limit known 时可能调用官方 `messages.count_tokens`。这是 count-only 网络 I/O，不调用 `messages.create`；失败按既有规则安全退化为 serialized estimate。OpenAI-compatible 路径继续使用本地 deterministic estimate。

## ProjectSession 与 `/context`

`ProjectSession.inspect_context()` 在 facade `RLock` 内冻结 Session、effective context 与 current target，完成 count/fit 后返回 immutable inspection。锁覆盖可能的 Anthropic count-only I/O，以保证观察一致。

REPL `/context` 显示：

- source 与 `ctx-v1` identity；
- full/effective turn/item counts；
- exact/estimated/unknown/unavailable input；
- requested output reserve、context window、model max output；
- fit decision 与 operands known 时的 remaining capacity；
- sanitized diagnostic。

`FITS` 使用 info，`UNKNOWN`/fake unavailable 使用 warning，known context/model-output exceeded 使用 error。命令在 prompt dispatch 前被 Host 消费，不进入模型历史，不执行 tool，不追加 conversation/audit record，不修改 latest pointer、history、runtime 或 Session。

本切片只增加 REPL `/context`，不增加 top-level argparse 子命令。

## Session schema 与恢复

Session schema 保持 v1：不新增 record type、binding field、context ID、effective history、count、limit 或 fit observation。Resume 从现有 `ReplayState.history` 重建 full/effective histories；相同 binary 的 current prompt/tool contract 与相同 effective history会得到相同 context ID。

共享 validator 对旧 replay 中理论可接受、但当前 sequential loop 与 adapters 都无法产生的交错 tool sequence收紧为 fail closed。这是 causality validator 对齐，不是 wire schema migration；合法 schema-v1 records 的编码不变。

## System prompt 审阅

已审阅 `src/leonervis_code/system_prompt.py`。`/context` 是 Host-only inspection，effective history仍等于 full history，没有新增 compact、summary、工具或其他模型可见能力，因此 canonical prompt保持 version 1 与现有 fingerprint。

## 明确不做

- `/compact`、automatic compaction 或 overflow retry；
- deterministic/LLM summary、retention policy、continuation prompt；
- effective-context checkpoint 或 Session schema migration；
- 持久化 context ID、token count 或 fit observation；
- target-aware startup resume 或 `/resume` prepare/commit；
- history删除/重写、cost meter、provider retry/fallback；
- streaming、parallel tools、write/Bash/approval。

## 验证

确定性测试覆盖 empty/plain/tool context、golden identity、identity inclusion/exclusion、strict tool causality、full/effective atomic update、manager read-only assessment、Session/transcript不变、fake unavailable、CLI presentation/help/completion与 slash non-entry。真实 CLI observation使用隔离临时 workspace，验证 repeated `/context` identity稳定、普通 turn后identity变化、custom OpenAI-compatible local estimate，以及 transcript byte-for-byte不变。

## 后续

下一切片才设计 controlled compact transaction：冻结 source context ID、生成 candidate summary、验证 resulting context、重新检查 source identity、append/fsync typed checkpoint，然后原子替换 in-memory effective history。它需要独立 Session schema migration，不能复用free-form audit reason或 synthetic conversation turn。
