# 0028：Foundation 4C Controlled Command Contract与Side-effect-free Preparation

- 状态：已接受
- 日期：2026-07-24
- 范围：Foundation 4C Slice 0–3

## 问题

Foundation 4B之后，模型可以读取、搜索、创建、覆盖和精确编辑workspace文件，但不能运行测试、lint、format check或其他项目验证程序，因此还无法形成“修改—执行—观察—继续修改”的完整coding loop。直接增加`bash(command)`会把shell parsing、管道、重定向、变量展开、命令替换、后台任务与实际程序执行混成一个不透明边界，也容易让`workspace-write`被误解为能够约束普通进程只能写workspace。

运行`pytest`或其他看似只做验证的程序也不天然安全：项目测试可以修改多个文件、读取用户目录、访问credential、联网并启动子进程。没有OS级sandbox时，Host不能承诺进程副作用被限制在workspace，也不能承诺失败后回滚。因此第一阶段必须先固定真实能力、权限分类、请求边界与审批身份，再实现任何`subprocess`调用。

## 决策

### 使用`run_command`而不是伪安全的`run_test`

内部未来工具名确定为：

```text
run_command(argv, cwd, timeout_seconds)
```

首要产品用途仍是运行测试、lint、format check与build verification，但合同使用`run_command`，因为测试本身也是任意本地代码执行。这个命名避免暗示测试具有额外sandbox保证，也能让同一个Host边界覆盖不同语言和构建系统。

本合同接收参数数组，不接收shell source string。后续executor必须直接使用argv并固定`subprocess`的`shell=False`；`|`、`>`, `*`、`$()`等字符只是单个参数中的普通数据，不由Leonervis自动解释。该约束不等同于sandbox：若未来允许并批准显式shell executable，或者被执行程序自身解释参数、读取文件、联网或启动子进程，仍属于`danger-full-access`风险。

### Closed bounded request

请求必须恰好包含：

```json
{
  "argv": ["uv", "run", "pytest"],
  "cwd": ".",
  "timeout_seconds": 60
}
```

`argv`包含1至64个UTF-8字符串，首项必须是非空白executable name。每项最多1024 characters且1024 UTF-8 bytes，全部参数合计最多8192 UTF-8 bytes；只有NUL被禁止，后续空参数和shell metacharacter保持literal。现有`ToolArguments` v1仍先保证整个canonical JSON object不超过16 KiB。

`cwd`必须是`.`或使用`/`分隔的portable workspace-relative目录，最多4096 characters/bytes与64个components。Absolute、Windows drive、backslash、empty/dot/dot-dot/repeated components全部拒绝。Prepare使用`lstat`检查workspace root并逐段检查cwd，确认它们已经存在、是目录且都不是symlink；missing、file、broken link、internal link和external link均在permission eligibility前拒绝。

`timeout_seconds`必须是1至300之间的真实integer，Boolean和float拒绝。Future executor的Host-owned stdout与stderr capture cap分别固定为32 KiB；模型不能通过参数提升timeout或output ceiling，也不能提供environment override。Future executor只从Host环境复制closed allowlist：`HOME`、locale字段、`NO_COLOR`、`PATH`、terminal/temp字段、`UV_CACHE_DIR`、`VIRTUAL_ENV`与XDG目录字段，并按实际cwd生成`PWD`；provider API key、任意project secret和其他环境变量不自动转发。该清理减少意外credential继承，但进程仍可从filesystem或network取得数据，因此不构成credential sandbox。

### 复用既有`dangerous`权限分类

`run_command`固定映射为现有`PermissionAction.DANGEROUS`，而不是新增`workspace-execute`。普通OS进程即使从workspace cwd启动也可能在workspace外产生副作用；新增一个听起来受workspace约束的action会造成错误安全暗示。现有PermissionGate矩阵已经表达所需能力：

| Permission mode | `approval=ask` | `approval=auto` |
| --- | --- | --- |
| `read-only` | `deny` | `deny` |
| `workspace-write` | `deny` | `deny` |
| `danger-full-access` | `ask` | `allow` |

因此PermissionAction、PermissionReason和Session action-audit vocabulary不扩展，policy code保持不变。`auto`只减少确认交互，不能改变argv/cwd/timeout、未来process/output bounds、prepared-turn lease、audit或causality要求。

### Side-effect-free preparation与exact identity

`RunCommandTool.prepare()`只解析immutable `ToolArguments`、检查bounds并以只读方式验证cwd，不导入或调用`subprocess`，不解析PATH，不查找executable，不创建文件，不写Action Audit，也不启动进程。结果是frozen `PreparedRunCommand`，携带exact `ToolUse`、immutable argv tuple、canonical relative cwd、timeout、`dangerous`分类与`ActionPrecondition.none()`。

Command没有像单文件写入那样可合理绑定的唯一目标state。Approval identity绑定的是exact request、workspace fingerprint、prepared-turn lease与Effective Context，而不是executable binary、整个项目树或测试代码内容。改变argv、cwd、timeout、runtime generation或context都会改变现有ActionIdentity v1 digest；项目文件在批准前后可能变化，这是coding workflow的预期行为，不能伪装成portable full-workspace CAS。

Prepare后仍提供只读workspace root与cwd revalidation boundary。后续coordinator必须在durable execution start前重新拒绝missing、non-directory或symlink workspace/cwd；future executor还必须在实际spawn边界再次检查并诚实记录剩余local single-user TOCTOU限制。

### Future execution contract已经固定但尚未实现

后续Slice 4–6必须在不改变上述request meaning的前提下实现：

- argv-based `subprocess`与`shell=False`；
- Host-owned environment，模型无environment override；
- stdout/stderr分别有界捕获与确定性截断；
- exit code、signal、timeout、cancel与spawn failure的结构化结果；
- 新process group/session、timeout后的process-tree termination与bounded cleanup；
- durable execution-start先于spawn，known outcome后再追加finish；
- started但没有durable finish的replay状态继续是`outcome-unknown`；
- 已启动命令的副作用不可声称自动回滚，也不得因未知结果自动retry。

本ADR不声称禁网、credential隔离、filesystem sandbox、container/VM isolation或portable hostile-concurrency安全。这些能力若未来需要，必须作为独立OS isolation slice设计和验证。

### Slice 0–3不改变模型可见合同

`run_command`尚未加入canonical tool catalog、provider count/create projection、AgentLoop、ProjectSession dispatch、CLI approval presentation或Action Audit写入路径。普通CLI prompt与真实provider都不能请求或运行它。

因此model-visible顺序仍为`read_file, glob, grep, write_file, edit_file`并共享每user turn最多三次顺序execution；canonical system prompt保持v6，provider adapter contract保持v7，Effective Context representation与golden identity不变。ToolArguments保持v1，ActionIdentity保持v1，new `turn_committed`保持schema v2，Action Audit保持schema v1，`context_compacted`继续支持v2/v3 replay，`ctx-v1`/`ctx-v2`不变。

## 明确不做

- 在Slice 0–3中启动任何subprocess；
- 将`run_command`暴露给模型、fake provider、真实provider或CLI；
- 增加Bash source-string、pipe、redirection、command substitution或background语法；
- 声称测试、allowlist、workspace cwd或`danger-full-access`构成sandbox；
- 增加environment overrides、network policy或credential forwarding contract；
- 持久化command Action Audit或修改Session schema；
- 改变system prompt、provider contract、Effective Context identity或共享工具预算。

## 验证

确定性测试覆盖closed input、wrong tool name、argv item/count/byte bounds、literal shell metacharacters、timeout type/range、portable root/nested cwd、missing/file/symlink workspace与cwd、prepare无workspace副作用、prepare result immutability、workspace/cwd revalidation、closed environment allowlist、完整dangerous permission matrix以及argv/cwd/timeout/lease/context对ActionIdentity digest的绑定。完整pytest、Ruff、format、lock与diff检查构成release gate；本阶段不使用真实provider、credential、network或API费用，也不运行prepared command。
