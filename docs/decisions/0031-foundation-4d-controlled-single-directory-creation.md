# 0031：Foundation 4D Controlled Single-directory Creation

- 状态：已接受
- 日期：2026-07-24
- 范围：Foundation 4D Slice 0–4

## 问题

`write_file`和`edit_file`都故意不创建缺失的parent directory，因此模型即使获得`workspace-write`能力，也无法完成“先建立一个新目录，再在其中写文件”的普通项目操作。虽然`run_command(["mkdir", ...])`理论上可以产生类似效果，但它固定属于`dangerous`，需要`danger-full-access`，还会扩大到任意进程副作用；这不能替代一个参数封闭、路径受限、可审计的目录工具。

目录创建也不能只包装`Path.mkdir(parents=True)`。Host必须在permission之前确定准确目标，拒绝逃逸和symlink，绑定“目标不存在”前置条件，并在approval等待后重新检查。创建目录一旦对filesystem可见，后续provider或turn commit失败也不能假装动作未发生；目录已创建但fsync失败同样必须报告partial，而不是自动重试。

## 决策

### 独立的单目录合同

新增model-visible工具：

```text
mkdir(path)
```

它一次只创建一个缺失的workspace-relative目录。`path`必须是有界、合法UTF-8、portable `/`分隔路径；绝对路径、Windows drive、反斜杠、空组件、`.`、`..`、NUL、超过4096 characters、超过4096 UTF-8 bytes、超过64 components或单component超过255 bytes都会在准备阶段拒绝。

目标的所有parent必须已经存在、是real directory且不是symlink。目标若已经是文件、目录、symlink或其他filesystem entry，也在准备阶段直接拒绝，不进入PermissionGate或Action Audit。工具不递归创建parent，不覆盖、不合并，也不把existing directory视为幂等成功。

### Side-effect-free prepare与workspace-create权限

准备阶段只验证参数和filesystem状态，返回immutable `PreparedMkdir`：规范化相对路径、固定`PermissionAction.WORKSPACE_CREATE`和`ActionPrecondition.path_absent()`，不产生目录或其他副作用。

PermissionGate继续使用现有矩阵：

- `read-only`拒绝；
- `workspace-write`和`danger-full-access`在`approval=ask`时逐次询问；
- 两种可写模式在`approval=auto`时自动允许；
- approval不绕过路径、parent、symlink、stale、lease、audit或durability约束。

交互审批只显示tool、trusted action class和workspace-relative path。One-shot ask仍安全cancel且不读取stdin。

### Revalidation、执行与durability结果

ProjectSession通过现有ActionCoordinator执行：

```text
prepare exact absent target
→ durable action_requested
→ durable permission_decided
→ optional durable approval_resolved
→ lease and target-absence revalidation
→ durable action_execution_started
→ create exactly one directory
→ fsync new directory and parent directory
→ durable action_execution_finished
→ provider continuation
→ atomic turn_committed
```

如果approval等待期间目标出现，新的precondition不再等于批准时的`path_absent`，single-use grant以`stale_precondition`拒绝，executor不会运行。执行边界还会再次检查路径与目标；`FileExistsError`等create race稳定映射为未创建失败。

结果固定为：

- 成功：`succeeded / directory_created`，model result为`{"operation":"created","path":"..."}` JSON；
- 创建前失败：`failed / directory_not_created`；
- 目录已经可见但新目录或parent fsync失败：`partial / directory_created_durability_unknown`。

系统不自动重试，也不声称能回滚一个已经可见的目录。如果provider continuation或`turn_committed`失败，目录和durable Action Audit继续保留，candidate conversation turn不提交。若execution-finished audit持久化失败，既有started-without-finish recovery仍如实导出unknown outcome。

路径实现通过每次准备、approval revalidation和紧贴创建边界的`lstat`检查拒绝观察到的symlink与非目录parent；与现有本地单用户文件工具一样，它不宣称提供OS sandbox或敌对并发下的完整portable filesystem transaction。Permission或approval不能把这一边界解释成workspace外访问授权。

### Model-visible与版本影响

Canonical tool order固定为：

```text
read_file, glob, grep, write_file, edit_file, run_command, mkdir
```

七个工具仍共享每个user turn最多三次顺序执行；第四次得到现有limit result，再次请求停止且不提交turn。Anthropic和OpenAI-compatible ordinary count/create都投影同一closed `mkdir` schema，compact-summary请求仍不暴露工具。

Provider adapter contract从v8升级为v9。Canonical model system prompt从v7升级为v8，说明`mkdir`只创建一个目录、parent必须存在、需要缺失parent时应先mkdir再write，并继续禁止递归parent creation。Prompt/tool catalog变化更新当前binary的Effective Context golden为`ctx-v1-12b7d8f648ac4909132c0176de74297f8d00805b887e190d51767b6fc1e2c986`，但identity algorithm和representation仍是`ctx-v1`/`ctx-v2`。

ToolArguments保持v1；新`turn_committed`保持schema v2；ActionIdentity与Action Audit保持v1；普通Session records保持v1；`context_compacted`继续支持v2/v3 replay。旧transcript和checkpoint不重写，resume或compaction不会重新执行mkdir。

## 不变量

- 模型请求mkdir不等于Host应执行；PermissionGate和approval始终位于副作用之前。
- Hard preparation rejection不创建Action Audit，因为请求从未成为permission-eligible action。
- `action_execution_started`成功持久化之前不得创建目录。
- 目标absence、workspace-relative path和no-symlink检查不能被auto approval绕过。
- Tool result必须紧跟并匹配唯一`tool_use_id`；完整turn只在最终assistant text和durable commit后可见。
- Action Audit只显示相对路径和permission lifecycle，不泄露绝对workspace路径或内部fingerprint。
- Resume、compaction和runtime切换不会重放目录副作用，也不会从Session provenance重建旧runtime。

## 明确不做

- `mkdir -p`或递归parent creation；
- 已存在目录的幂等成功；
- rename、file delete、empty-directory removal或recursive delete；
- 通过mkdir提供任意mode、owner、ACL或platform-specific attribute控制；
- 把`run_command(["mkdir", ...])`自动降级为workspace-create；
- OS/VM/container filesystem sandbox或跨平台敌对并发事务保证。

## 后续

下一独立方向应优先设计受控non-overwrite rename：明确source/destination双路径身份、跨filesystem拒绝、目标存在策略、approval后双端stale检查和rename已发生但durability未知的partial语义。之后再分别设计file-only delete与empty-directory removal；不应直接引入递归删除。

## 验证证据

确定性release gate于2026-07-24通过：

```text
675 tests passed
ruff check passed
ruff format --check passed
uv lock --check passed
git diff --check passed
```

三个public fake CLI入口均输出`Fake response: Hello`并退出0；resume smoke最终报告`turns: 2`；空prompt退出2、stdout为空且stderr给出argparse校验错误。未使用credential、网络或真实provider费用。
