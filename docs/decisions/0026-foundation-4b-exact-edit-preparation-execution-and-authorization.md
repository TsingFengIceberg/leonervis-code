# 0026：Foundation 4B Exact Edit Preparation、Execution与Authorization Composition

- 状态：已接受
- 日期：2026-07-24
- 范围：Foundation 4B Slice 0–3

## 问题

`write_file(path, content)`已经能安全创建或替换完整文件，但小改动仍要求模型重写整个文件。直接加入patch、按行编辑或Bash会同时扩大匹配语义、权限边界和失败模式，也可能绕过现有的workspace containment、symlink rejection、exact precondition、durable start与Action Audit。

在把编辑能力暴露给模型前，Host需要先证明一个更窄的内部合同：给定现有UTF-8文件、唯一的旧文本和替换文本，能否在不产生prepare副作用的前提下构造确定候选内容，并通过现有`workspace-overwrite`权限、approval grant、stale-state revalidation与failure-atomic replacement安全执行。

## 决策

### 使用唯一exact replacement

内部工具合同为：

```text
edit_file(path, old_text, new_text)
```

`path`必须是portable workspace-relative path，目标必须已经存在且是最多1 MiB的strict UTF-8普通文件。所有intermediate与final symlink继续拒绝，parent必须已经存在；本工具不创建文件或目录。

`old_text`必须非空，`old_text`和`new_text`各自最多4096 characters且4096 UTF-8 bytes。匹配按Python字符串的exact code-point sequence执行，不做regex、Unicode normalization、换行转换或模糊匹配。`old_text`必须恰好出现一次；第二次查找从首次匹配的下一个起始位置开始，因此重叠出现也算多匹配，例如`aa`在`aaa`中被拒绝。`new_text`可为空以支持精确删除。相同old/new、零匹配、多匹配以及最终内容不变都在permission eligibility前拒绝。

修改后的完整候选内容仍限制为最多1 MiB。Prepare严格读取并验证源文件、构造完整candidate bytes、绑定源SHA-256，但不创建temporary file、不修改目标，也不产生Action Audit。

### 复用受控overwrite执行边界

Exact edit复用`write_file`已经验证的现有文件观察与原子替换边界，而不复制一套可能漂移的文件系统实现。执行前重新观察approved digest；写入使用同目录exclusive temporary file、保留原mode、完整写入并fsync temporary、再次校验digest/device/inode、`os.replace`安装，然后fsync parent directory。

任何replace前错误或stale/conflicting source都返回`edit_not_applied`并保持目标不变。成功返回deterministic JSON，包含result byte count、`operation: edited`、relative path和`replacements: 1`。Replace已经可见但directory fsync失败时返回`partial`与`edited_durability_unknown`，明确要求检查workspace且不得自动重试。

### 复用既有permission、approval、identity与audit

Exact edit固定归类为`PermissionAction.WORKSPACE_OVERWRITE`，不新增permission action或policy matrix。Prepared edit携带原始immutable `ToolUse`、relative path、完整candidate bytes和`expected-state-sha256` precondition。`ActionIdentity`继续包含原始canonical `path/old_text/new_text` arguments、prepared-turn lease、workspace fingerprint与precondition。

现有`ActionCoordinator`保持固定顺序：durable request、permission decision、可选human resolution、precondition revalidation、single-use grant consumption、durable execution start、executor、durable known outcome。确定性组合测试证明read-only deny、ask accept、reject、cancel、auto allow以及approval等待期间源文件变化后的stale rejection；成功、拒绝和放弃状态均可从append-only transcript严格重放。CLI Action Audit仍只显示tool、trusted action class、relative path、permission/approval与结果，不显示`old_text`或`new_text`。

### Slice 0–3不改变模型契约

本阶段只建立内部exact-edit engine及其与既有授权/审计边界的可组合性。`edit_file`尚未加入canonical tool catalog、provider count/create projection、AgentLoop dispatch或ProjectSession runtime dispatch，真实provider与普通CLI prompt都不能请求它。

因此model-visible顺序仍是`read_file, glob, grep, write_file`，共享每个user turn最多三次顺序调用；canonical system prompt保持v5，provider adapter contract保持v6。`ToolArguments`保持v1，new `turn_committed`保持schema v2，action audit records保持schema v1，`context_compacted`继续支持v2/v3 replay，Effective Context representation保持`ctx-v1`/`ctx-v2`。Slice 4必须在同一变更中审阅并更新tool schema/order、provider parity、system prompt、Effective Context identity goldens、AgentLoop/Session dispatch和相关ADR后，才能声明`edit_file`为model-visible功能。

## 明确不做

- 在Slice 0–3中把`edit_file`暴露给模型或CLI；
- regex、patch hunks、line-number edit、fuzzy matching或Unicode normalization；
- 多处替换、多文件事务、create、delete或mkdir；
- Bash、shell/test execution或任意命令审批；
- portable hostile-concurrency filesystem CAS或sandbox声明；
- 修改Session schema、action-audit schema、permission matrix或existing `write_file`模型合同。

## 验证

Deterministic tests覆盖side-effect-free prepare、exact Unicode/newline行为、精确删除、closed arguments、字符/byte/result bounds、portable path、missing/symlink/special/binary/oversized target、零/多/重叠匹配、no-op、mode preservation、stale conflict、replace前失败清理和directory-fsync partial outcome。独立组合测试使用真实`SessionWriter`与`ActionCoordinator`验证deny、ask accept/reject/cancel、auto、accepted-then-stale、strict audit replay和Action Audit redaction。完整pytest、Ruff、format、lock与diff检查构成release gate；本slice不需要也不会使用真实provider、credential、network或API费用。
