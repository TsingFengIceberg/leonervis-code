# 0027：Foundation 4B Model-visible Exact Edit Integration

- 状态：已接受
- 日期：2026-07-24
- 范围：Foundation 4B Slice 4

## 问题

Foundation 4B Slice 0–3已经证明内部`edit_file(path, old_text, new_text)`能够无副作用prepare、通过现有`workspace-overwrite`授权边界执行，并在stale、failure与partial durability情形下保持准确结果和durable Action Audit。但只存在内部executor仍不是完整产品能力：模型看不到schema，provider不能解析请求，Effective Context不包含该contract，`ProjectSession`也不会把普通tool call分派到exact-edit路径。

如果只修改其中一个接入点，会形成危险的半接入状态：模型可能被告知存在工具但provider不投影；provider可能接受调用但Session返回unknown；恢复或compaction可能使用与当前工具面不一致的context identity；system prompt也可能错误描述权限、冲突或重试语义。

## 决策

### 将exact edit加入固定canonical工具面

Model-visible工具顺序变为：

```text
read_file, glob, grep, write_file, edit_file
```

五个工具继续共享每个user turn最多三次顺序execution预算。仍只允许provider每次返回final text或一个纯tool call；multiple或mixed calls继续fail closed。

`edit_file`公开schema要求且只允许`path`、`old_text`与`new_text`三个string字段。`path`必须非空，`old_text`必须非空但可以只由空白组成，`new_text`可以为空以表示精确删除；每个输入string继续受4096 characters和4096 UTF-8 bytes的catalog边界约束。更严格的existing-file、unique-match、1 MiB source/result、no-symlink和no-op检查仍由`EditFileTool.prepare`执行，不能由provider schema或approval代替。

### Provider、prompt与Effective Context同步升级

Anthropic Messages与OpenAI-compatible普通count/create projection都按相同顺序公开第五个closed schema，并把native tool call还原为同一个immutable `ToolArguments`。Compact-summary请求仍然不携带tools。Provider adapter contract升级为v7。

Canonical model system prompt升级为v6，说明exact unique replacement、零/多匹配拒绝、overwrite permission/approval、stale source和visible-partial结果；同时明确`edit_file`适合已有文件中的一个小型唯一锚定修改，`write_file`仍负责create或完整内容替换。

Prompt与tool catalog内容变化会自然改变当前binary计算出的Effective Context ID及golden，但representation仍为`ctx-v1` full history与`ctx-v2` compacted context。已有Session transcript和checkpoint不重写；resume后的新turn使用当前binary的prompt和五工具contract。`ToolArguments`保持v1，new `turn_committed`保持schema v2，Action Audit records保持schema v1，`context_compacted`继续支持v2/v3 replay。

### 通过既有Host action boundary执行

`ProjectSession`在permission eligibility前调用`EditFileTool.prepare`。Malformed、missing target、zero/multiple match、no-op、symlink、invalid UTF-8或size错误直接作为model-visible Tool error返回，不创建Action Audit。合法prepared edit固定映射到`workspace-overwrite`，随后复用现有`ActionCoordinator`顺序：durable request、permission decision、可选human approval、source precondition refresh、single-use grant consumption、durable execution start、executor与durable known outcome。

执行结果映射保持准确：成功为`succeeded / edited`，replace前失败为`failed / edit_not_applied`，replace可见但directory durability未知为`partial / edited_durability_unknown`。Provider continuation或turn commit在文件效果之后失败时，文件与Action Audit事实保留，candidate conversation turn仍不提交，并通过既有`turn_failed`恢复语义避免谎报原子回滚。

## 明确不做

- regex、fuzzy、line-number、hunk或multi-replacement patch；
- create、delete、rename、mkdir或多文件事务；
- Bash、test runner、network、parallel tool calls或automatic retry；
- 提高每轮三次共享tool预算；
- 修改Session、Action Audit、ToolArguments或Effective Context representation version；
- 因工具面变化而重写旧transcript或checkpoint。

## 验证

确定性测试覆盖canonical顺序与closed schema、空`new_text`和空白`old_text`、Anthropic/OpenAI-compatible schema与parser parity、system prompt exact text/fingerprint、Effective Context identity golden、read-only denial、ask accept、hard match rejection、approval等待期间source变更后的stale rejection、完整tool causality、durable Action Audit与既有exact-edit executor边界。完整pytest、Ruff、format、lock、diff及三个offline fake CLI入口与resume/blank-prompt smoke构成release gate；本slice不需要也不会使用真实provider、credential、network或API费用。
