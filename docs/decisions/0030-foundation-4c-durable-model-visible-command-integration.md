# 0030：Foundation 4C Durable Model-visible Command Integration

- 状态：已接受
- 日期：2026-07-24
- 范围：Foundation 4C Slice 7–9

## 问题

内部executor本身还不能形成coding-agent loop。`run_command`必须经过与写入工具相同的permission、approval、prepared-turn lease、durable Action Audit和tool-use/result因果边界；同时provider schema、system prompt、Effective Context identity与CLI展示必须一起变化，否则模型看到的合同、Host实际执行和可恢复审计会不一致。

Command还有一个比文件写入更强的failure-atomicity问题：spawn之后可能立刻产生外部副作用。若`action_execution_started`尚未持久化就启动进程，crash recovery会看不到一次真实执行；若execution finish持久化失败，又不能把已运行命令伪装成从未发生。

## 决策

### 复用ActionCoordinator并固定spawn commit point

ProjectSession将prepared command映射为现有`PermissionAction.DANGEROUS`并交给中央ActionCoordinator。顺序保持：

```text
prepare exact request
→ durable action_requested
→ durable permission_decided
→ optional durable approval_resolved
→ cwd/lease revalidation and single-use grant consume
→ durable action_execution_started
→ subprocess spawn and execution
→ durable action_execution_finished
→ provider continuation
→ atomic turn_committed
```

只有`action_execution_started` append+fsync成功后executor才可调用`Popen`。Executor还会在紧贴spawn的边界再次检查workspace root/cwd；若此时失效则持久记录`command_cwd_invalid`且不启动进程。若此前任何audit、approval、lease或revalidation步骤失败，进程同样绝不启动。若spawn后`action_execution_finished`无法持久化，turn不提交；严格replay仍会把started-without-finish导出为`outcome-unknown`，诚实保留“可能已有副作用”的事实。

Command复用Action Audit schema v1与ActionIdentity v1，不新增命令专用record。Exact identity已经绑定canonical argv/cwd/timeout、workspace fingerprint、prepared-turn lease、runtime generation和Effective Context。Command没有可移植的项目树CAS，因此precondition保持`none`，但cwd在execution-start记录前重新检查。

### CLI审批与脱敏审计

交互REPL的`approval=ask`显示完整argv、相对cwd和timeout，供用户决定是否运行；one-shot模式继续fail-safe cancel且不读取stdin。`approval=auto`只省略确认，不绕过danger-full-access、准备、审计、lease、timeout、output或cleanup边界。

`session actions`和`/actions`只显示executable、额外参数数量、相对cwd、timeout、permission/approval与lifecycle/result code。普通审计展示不回显完整argv，避免把诸如token argument直接暴露在终端摘要；append-only Session仍保存exact ActionIdentity以支持因果和重放验证。

### 第六个model-visible工具

Canonical tool order固定为：

```text
read_file, glob, grep, write_file, edit_file, run_command
```

六个工具继续共享每个user turn最多三次顺序执行，不为command增加独立预算，也不允许parallel tool calls。Anthropic与OpenAI-compatible的ordinary count/create请求投影相同的closed schema；compact-summary请求继续不暴露任何工具。

Provider adapter contract从v7升级为v8。Canonical model system prompt从v6升级为v7，明确direct argv而非shell source、danger-full-access要求、无OS sandbox/rollback保证、有界输出与partial cleanup语义，并禁止模型在timeout、signal或cleanup uncertainty后自动retry。

Prompt/tool catalog变化会自然改变当前binary生成的Effective Context fingerprint；empty full-context golden更新为`ctx-v1-e6b5274ea57642fd614842c58dfa74def0b6f0c1319b2c312b7c54d61b834ce3`。Identity算法与representation仍是`ctx-v1`/`ctx-v2`，历史checkpoint不重写。

### Schema与兼容性

本切片不改变ToolArguments v1、`turn_committed` schema v2、ActionIdentity v1、Action Audit schema v1、普通Session record schema v1或`context_compacted` v2/v3 replay。新工具参数中的array与integer仍通过同一个bounded canonical JSON object持久化；legacy transcript按既有reader继续重放。

Resume只恢复历史与audit provenance，不重建进程、不重跑started command，也不根据旧Session provider binding恢复runtime。Compaction继续原子保留tool-use/result pair；run_command输出与其他不可信tool result一样只能作为conversation data进入summary。

## 不变量

- Model请求command不等于Host应执行；PermissionGate与approval始终在spawn之前。
- `action_execution_started`的durable fsync是允许spawn的语义commit point。
- Tool result必须紧跟并匹配唯一`tool_use_id`；最终assistant text和turn durable commit之前candidate history不可见。
- 第四次共享tool request仍返回结构化limit result，再次请求会停止且不提交turn。
- Resume和compaction绝不重放、复制或拆开已经发生的command action。
- CLI展示可脱敏，但持久identity、permission与execution lifecycle必须足以严格重放。
- Provider adapter只做native projection/parsing，不拥有permission、Session或process execution。

## 不在范围

本切片不增加Bash source string、interactive terminal、streaming command output、command history rerun、repair/retry、per-executable policy、network policy、resource sandbox、后台任务、并行工具、delete、mkdir或rename。
