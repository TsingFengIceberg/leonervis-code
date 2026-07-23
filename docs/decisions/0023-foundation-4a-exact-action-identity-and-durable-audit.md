# 0023：Foundation 4A Exact Action Identity、Single-use Approval Grant与Durable Action Audit

- 状态：已接受
- 日期：2026-07-23
- 范围：Foundation 4A Slice 3–4

## 问题

ADR 0022已经固定纯`PermissionGate`的`allow | ask | deny`策略，但策略结果本身不能证明“用户批准的动作”和“稍后真正执行的动作”完全相同，也不能回答进程崩溃后一个有副作用的动作究竟尚未开始、已经完成，还是结果未知。

如果approval只绑定tool name或path，模型参数、workspace、Session、provider runtime、Effective Context或目标文件前置状态在等待期间发生变化，旧批准可能被错误复用。如果Session只保存最终成功/失败，Host还可能在崩溃恢复时把“已经开始但没有写下结果”误报为“没有执行”，或者为了补审计而危险地重试副作用。

因此在接入CLI、AgentLoop或真实write executor前，必须先固定exact action identity、一次性approval grant、append-only action lifecycle与durable start/finish语义。

## 决策

### ActionIdentity v1绑定一个不可替换的exact action

Host为每个resolved action request生成`ActionIdentity` v1。Canonical manifest包含：

```text
request_id
provider tool_use_id
exact tool_name
ToolArguments v1 canonical object + arguments_version
trusted PermissionAction classification
workspace fingerprint
action lease
trusted execution precondition
identity version
```

`request_id`是Host生成的canonical UUID4。Identity使用sorted compact UTF-8 JSON，并以domain-separated SHA-256生成：

```text
act-v1-<64 lowercase hex>
domain = "leonervis-code-action-identity-v1\\0"
```

任一字段变化都会改变digest。Provider只能提供tool call和arguments；`request_id`、trusted action classification、workspace fingerprint、lease与precondition都由Host建立，不能接受模型自报。

### ActionLease绑定prepared turn且不能在resume后重建

第一版lease包含：

```text
session_id
lease_id
runtime_generation
context_id
```

`lease_id`是为一个prepared turn生成的不可重建UUID4，而不是transcript sequence。Audit append会推进sequence，所以sequence不能稳定代表同一prepared turn；resume也不能制造原lease ID，因此旧grant会自然stale。`runtime_generation`绑定当前runtime，`context_id`只接受当前`ctx-v1`或`ctx-v2` Effective Context identity。

本slice定义identity contract，但尚未把lease创建接入AgentLoop。后续coordinator/integration必须保证同一prepared turn的continuation固定使用同一lease、runtime与context snapshot。

### Precondition是审批所绑定的可信目标状态

Precondition采用closed vocabulary：

```text
none
path-absent
expected-state-sha256
```

`path-absent`用于未来create-only action；`expected-state-sha256`为未来controlled overwrite固定expected state；`none`只用于不需要目标状态绑定的action。Precondition只表达identity，不替代executor在执行前重新检查workspace、symlink、file type和conflict。

### ApprovalGrant是Host内存中的一次性能力，不是bearer token

Grant只可在以下条件全部满足时签发：

- ActionIdentity的trusted action与PermissionRequest一致；
- PermissionResult严格等于当前纯PermissionGate对该request的确定性结果；
- decision为`ask`。

`allow`和`deny`不能伪造grant。Grant不是model-visible字符串，也不写入tool arguments；它是Host持有的内存对象。消费时必须匹配完整ActionIdentity、lease与precondition，且用lock保证并发消费最多一次成功。Stable rejection codes为：

```text
action_identity_mismatch
stale_lease
stale_precondition
already_consumed
```

Session只审计grant ID和lifecycle，不尝试在resume后重建可消费grant；旧进程中的grant随进程/lease结束而失效。

### Durable lifecycle使用五种schema-v1 audit records

Append-only Session新增：

```text
action_requested
permission_decided
approval_resolved
action_execution_started
action_execution_finished
```

每个reference record同时重复`action_request_id + action_digest`并在replay时精确匹配。`action_requested`保存exact identity、当前redacted binding、permission mode与approval mode；`permission_decided`必须等于PermissionGate重算结果；accepted approval必须有全局唯一grant ID，reject/cancel不得有grant；execution start必须使用`policy-allow`或该action的exact `approval-grant`授权。

Replay派生状态机：

```text
requested
  -> authorized | awaiting-approval | denied
awaiting-approval
  -> approved | rejected | cancelled
authorized | approved
  -> executing
executing
  -> succeeded | failed | partial
```

`partial`由后续Slice 8–9为“目标效果已可见但cleanup或directory durability不完整”的已知terminal outcome补充；它不同于缺少durable finish的`outcome-unknown`。

当前sequential Harness最多存在一个unresolved action lifecycle。`turn_committed`、`runtime_changed`、`context_compacted`和clean `session_closed`不得跨越它，避免Session、runtime、context或turn commit与未决副作用交错。

### Durable start是副作用前闸门，finish失败必须报告真实partial outcome

`SessionWriter.action_execution_started()`沿用现有append+fsync路径。未来coordinator必须先成功写入并fsync start record，才可调用executor。若start append失败，副作用不得开始。

Executor返回已知结果后，Host追加并fsync`action_execution_finished`。如果副作用已经返回，但final audit append失败，`ActionOutcomeAuditError`必须保留action request ID、digest、known execution outcome与result code，并链接原始storage error。Host不得把它降级为普通“执行失败”，不得假装finish durable，也不得重试副作用来补记录。

### Resume与turn failure诚实派生中断状态

当durable`session_resumed`或`turn_failed`跨过尚未start的未决action，replay将其标记为`abandoned`。当它跨过已有durable start但无finish的action，replay将其标记为`outcome-unknown`：副作用可能已经发生，系统没有足够证据断言成功、失败或未执行。

`Recovery`本身只描述tail repair；语义中断点仍由随后durable的`session_resumed`建立。Outcome-unknown action不得自动重试，也不会恢复旧approval grant。

### Audit不进入模型上下文，也不被compaction删除

五种action records属于Host audit。Replay把它们保存在`ReplayState.action_audits`，但不加入`history`、`effective_history`或provider request。Compaction只替换Effective Context view，不删除、总结或重写完整transcript中的action records；legacy `turn_committed` v1与当前v2可与action audit混合replay而不改写旧prefix。

### Model-visible与既有版本保持不变

Slice 3–4尚未接入CLI、AgentLoop、provider projection、真实executor或model-visible tool。Canonical system prompt已审阅并保持v4；ordinary tool order仍为`read_file, glob, grep`，共享三次execution预算不变；adapter contract保持v5；ToolArguments保持v1；new `turn_committed`保持schema v2；`context_compacted`继续v2/v3 replay；Effective Context保持`ctx-v1`/`ctx-v2`。新增action audit records使用各自schema v1，不升级无关record。

## 后续切片

1. Host approval coordinator：注入human handler，严格编排ask/accept/reject/cancel、grant签发/消费与durable records，但仍不接provider或真实executor UI。
2. AgentLoop/CLI integration：为prepared turn建立lease，加入terminal confirmation、non-interactive behavior与resolved action request budget，并同步审阅system prompt、tool results、provider projection和Effective Context identity。
3. Create-only `write_file`：增加第一个model-visible write schema，使用`path-absent`、atomic create和既有workspace/no-symlink hard bounds。
4. Controlled overwrite：使用`expected-state-sha256`和failure-atomic replacement，拒绝lost update。

Bash继续延后，不能与首个write slice合并。

## 明确不做

- CLI permission flags、approval terminal UX或non-TTY default；
- AgentLoop interception、prepared-turn lease integration或tool budget变化；
- `write_file`、overwrite、delete、edit、Bash/test或任何真实副作用；
- 将grant暴露给模型、持久化可重放bearer token或resume旧grant；
- 用permission/approval绕过workspace、symlink、size、timeout、conflict、causality或durability hard bounds；
- parallel action execution、multi-agent approval、remote approval service或OS sandbox声明；
- real-provider smoke、credential、network或API费用。

## 验证

Deterministic tests覆盖canonical identity/digest与每字段敏感性、closed decoding、v1/v2 context identity、grant issuance限制、stale/mismatch/single-use/thread-safe consumption、五种record codec、allow/ask/deny/reject/cancel lifecycle、exact reference与policy replay、duplicate IDs、Session/workspace/runtime binding、unresolved boundary、legacy mixed replay、resume/turn-failure的`abandoned`/`outcome-unknown`、writer reopen，以及known executor outcome后final audit失败的truthful exception。完整pytest、Ruff、format、lock、diff和fake CLI smoke仍是release gate。
