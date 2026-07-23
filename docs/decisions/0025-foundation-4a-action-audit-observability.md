# 0025：Foundation 4A Action Audit Observability

- 状态：已接受
- 日期：2026-07-23
- 范围：Foundation 4A Slice 10

## 问题

Foundation 4A Slice 3–9已经把permission decision、human approval、durable execution start与known outcome保存为Host-only append-only action audit，但用户只能在原始Session JSONL或测试中看见这些事实。受控写入已经可能产生`succeeded`、`failed`、`partial`、`abandoned`或`outcome-unknown`等不同结果；如果CLI没有安全的只读入口，用户无法直接回答某个Session里请求过哪些action、为何允许或拒绝、审批是否发生以及Host最终知道什么。

直接打印完整record或`ActionIdentity`不是可接受方案，因为其中可能含完整write content、absolute workspace、request/grant/lease ID、digest、precondition hash和executor message。观察接口还不能为了展示而修复transcript、拿writer lease、写入新record、改变latest pointer，或把Host audit注入模型history。

## 决策

### 提供standalone与REPL两个只读入口

CLI新增：

```text
leonervis-code -C <workspace> session actions [latest|session-id] [--limit N]
```

selector默认`latest`。Standalone inspection只验证已存在的Session root，解析selector并以`allow_repair=False`严格重放JSONL；它不创建Session目录、不拿writer lease、不修复incomplete tail、不更新`latest.json`、不追加record。不存在、损坏、stale或unsafe的Session状态继续通过现有`SessionStoreError`路径fail closed。

REPL新增：

```text
/actions
/actions N
```

它在当前`ProjectSession` lock下读取当前writer已经重放的`ActionAuditState`，不重新打开Session，也不调用provider。Slash command由Host处理，不进入user/model conversation history。

两个入口默认显示最近20条，显式limit必须是ASCII整数`1..100`。被截断时先报告“最近N条/总M条”，记录仍按原始时间顺序显示。空Session明确显示没有action audit。

### 终端只展示脱敏lifecycle摘要

每条记录只显示：

- durable request sequence与tool name；
- trusted action class；
- model argument中的workspace-relative path（存在时）；
- permission decision与stable reason；
- approval outcome或`pending | not recorded | not requested | not required | not reached`；
- derived lifecycle status与可选stable result code。

不显示完整content、executor message、absolute workspace、request/tool-use/grant/lease ID、action digest、workspace fingerprint或precondition hash。Path使用Python string representation，persisted result code转义control characters，避免换行或terminal control sequence改变展示结构。该输出是面向人的redacted摘要，不是portable export或完整forensic dump。

### 观察性不改变runtime contract

本slice只暴露已存在的Host audit state，不新增或修改Session record、audit lifecycle、permission matrix、approval coordination、tool execution或recovery语义。Action audit仍不进入full/effective model history，也不参与compaction summary或checkpoint identity。

Canonical model system prompt已审阅并保持v5；model-visible tool顺序仍为`read_file, glob, grep, write_file`，共享三次顺序预算；provider adapter contract保持v6。`ToolArguments`保持v1，new `turn_committed`保持schema v2，action audit records保持schema v1，`context_compacted`继续v2/v3 replay，Effective Context representation保持`ctx-v1`/`ctx-v2`。因此不更新prompt golden、provider projection或Effective Context identity算法。

## 明确不做

- JSON/CSV export、完整record dump或跨workspace/remote audit；
- 按tool、status、path、时间或decision过滤与搜索；
- transcript repair、retry、re-execution或unknown-outcome推断；
- 展示write content、executor message、absolute path、identity/grant/digest等敏感或内部字段；
- 改变permission/approval、write executor、Session schema、system prompt或provider contract；
- 在本slice加入`edit_file`、Bash、delete、mkdir或parallel actions。

## 验证

Deterministic tests覆盖standalone strict replay、relative-path redaction、content/message/grant/absolute-workspace不泄露、control-character escaping、default/explicit bounds、recent chronological selection、empty state、slash completion/help、`/actions`不进入模型history，以及deny、allow、pending approval、abandoned与outcome-unknown的准确措辞。完整pytest、Ruff、format、lock、diff和offline fake CLI smoke构成release gate；真实provider、credential、network与API费用不需要也不会用于本slice验证。
