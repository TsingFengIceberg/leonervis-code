# 0022：Foundation 4A Permission Policy Contract

- 状态：已接受
- 日期：2026-07-23
- 范围：Foundation 4A Slice 1–2

## 问题

Foundation 1D已经提供三个有界只读工具，但Host仍没有统一回答“一个模型请求的action是否在当前能力上限内，以及是否需要用户批准”。如果直接在`write_file`、CLI或AgentLoop中散落条件分支，permission mode、approval交互、workspace hard bounds、durable audit与failure behavior会互相耦合；未来增加overwrite或Bash时还会出现多个不一致的policy实现。

Permission也不能等同于安全执行。一个policy result为`allow`，只表示action处于当前配置允许的能力范围；它不能放宽workspace containment、no-symlink、file/output size、timeout、conflict detection、tool causality、runtime lease或durability要求。相反，terminal confirmation是Host与human之间的交互，provider adapter和Tool都不应拥有它。

因此在暴露任何写工具前，先固定permission vocabulary、decision matrix和纯策略边界，并让后续slice分别增加action identity、durable audit、approval coordinator、AgentLoop/CLI integration与真实副作用。

## 决策

### Permission mode与approval mode正交

能力上限固定为：

```text
permission_mode:
  read-only
  workspace-write
  danger-full-access
```

交互策略固定为：

```text
approval:
  ask
  auto
```

Policy结果固定为：

```text
allow
ask
deny
```

`permission_mode`回答“此类action是否在能力上限内”；`approval`只回答“在能力范围内的受控action是否需要human确认”。`auto`减少交互，但不提升能力上限，也不绕过Tool executor与Host transaction的hard constraints。

### 第一版action classes

纯policy只理解能力类别，不理解provider-native payload、终端文案或文件系统状态：

```text
workspace-read
workspace-create
workspace-overwrite
dangerous
unknown
```

`workspace-read`覆盖当前`read_file`、`glob`与`grep`。`workspace-create`和`workspace-overwrite`为后续write slices预留明确且不同的policy语义；classification必须由Host在执行前根据真实目标状态完成，不能由模型通过参数自行声称。`dangerous`只表示未来需要`danger-full-access`能力上限的action，不表示Bash或workspace外写入已经实现。无法识别或无法可信分类的action必须映射为`unknown`并fail closed。

### Deterministic decision matrix

| Action | `read-only` | `workspace-write` + `ask` | `workspace-write` + `auto` | `danger-full-access` + `ask` | `danger-full-access` + `auto` |
| --- | --- | --- | --- | --- | --- |
| `workspace-read` | `allow` | `allow` | `allow` | `allow` | `allow` |
| `workspace-create` | `deny` | `ask` | `allow` | `ask` | `allow` |
| `workspace-overwrite` | `deny` | `ask` | `allow` | `ask` | `allow` |
| `dangerous` | `deny` | `deny` | `deny` | `ask` | `allow` |
| `unknown` | `deny` | `deny` | `deny` | `deny` | `deny` |

当前三个read-only tools在所有mode和approval组合下都auto-allow，不产生terminal confirmation。`read-only`是未来默认配置候选，但CLI默认值和持久配置来源要到integration slice再决定；本ADR不提前增加flags或profile fields。

### Stable machine-readable reasons

每个result同时包含decision与stable reason code。第一版reason code固定为：

```text
allowed_workspace_read
allowed_workspace_create_auto
allowed_workspace_overwrite_auto
allowed_dangerous_auto
approval_required_workspace_create
approval_required_workspace_overwrite
approval_required_dangerous
denied_read_only_mode
denied_workspace_write_mode
denied_unknown_action
```

Reason code用于测试、后续audit与Host分支，不直接等同于model-visible ToolResult或human-facing terminal sentence。CLI可把同一reason渲染为更友好的文案；未来若向模型返回denial，必须另行审阅stable model-visible wording、system prompt和Effective Context identity。

### Pure `PermissionGate`

`PermissionGate`是无状态、无I/O的Host policy component：

```text
PermissionRequest(permission_mode, approval_mode, action)
  -> PermissionGate.evaluate(...)
  -> PermissionResult(decision, reason)
```

输入与输出使用frozen closed data structures。非法enum/type是Host programming/configuration error并在构造边界拒绝；已被可信classifier标记为`unknown`的action得到确定性`deny`，不会抛给provider或进入executor。

PermissionGate不得：

- 执行Tool或访问文件系统；
- 读取terminal、环境变量、credential、Session或provider；
- 创建或消费approval token；
- 生成provider-native schema；
- 持久化decision；
- 根据工具参数自行推断create/overwrite；
- 修改AgentLoop history或tool budget。

这些职责分别属于后续action identity、audit、approval coordinator、AgentLoop/CLI和executor slices。

### Hard constraints永远不可被permission绕过

Policy evaluation只发生在Host已经拥有可信action classification之后。即使结果为`allow`，执行层仍必须独立验证workspace containment、no-symlink、regular-file type、size/output/time limits、expected-state conflict、causal tool pairing与durable transaction。`danger-full-access + auto`也不构成OS sandbox、root authorization或忽略审计的承诺。

Foundation 4A之前先修复`read_file`契约偏差：最终或中间symlink无论指向workspace内外都拒绝；这证明read permission不会放宽既有symlink hard bound。Local single-user v0仍诚实保留检查与open之间的TOCTOU边界，不声称hostile concurrent filesystem isolation。

### Future tool-action budget

当前运行时继续使用三个read-only tool executions的既有预算，Foundation 4A Slice 1–2不接入AgentLoop，因此不改变行为或prompt。未来integration slice应把限制明确为每user turn最多三个resolved tool action requests：

- 一个`ask -> accept -> execute`是同一个request，只计一次；
- `deny`、user reject与cancel都会终结并计入该request；
- 模型再次请求同一或不同action是新的request；
- 第四个request收到structured limit result，再继续请求则停止且turn不commit。

这一变化接入时必须同步审阅system prompt、tool loop tests、model-visible limit result与Effective Context identity；本ADR不提前修改它们。

### Model-visible与durable contracts保持不变

Slice 1–2只新增tracked ADR、纯Host policy module与unit tests。当前model-visible工具仍严格按`read_file, glob, grep`排序；ToolArguments保持v1，new `turn_committed`保持schema v2，`context_compacted`保持v2/v3 replay，adapter contract保持v5，canonical system prompt保持v4及既有fingerprint，Effective Context保持`ctx-v1`/`ctx-v2`。

Pure PermissionGate尚未接入CLI、AgentLoop、provider projection或Session，因此模型仍被准确告知不能approve actions，用户也没有permission flags或confirmation flow。README必须明确区分“policy kernel已实现”和“approval/write capability仍未实现”。

## 后续切片

1. Action identity与single-use approval grant：绑定exact canonical arguments、workspace、runtime lease与precondition，防止replay和stale approval。
2. Durable action audit records：定义request/decision/approval/execution lifecycle、fsync commit point、replay/resume与truthful partial outcomes。
3. Host approval coordinator：通过注入的human handler协调ask/accept/reject/cancel，不接provider或executor UI。
4. AgentLoop与CLI integration：固定prepared turn/runtime lease，增加terminal confirmation并更新tool-action budget契约。
5. Create-only `write_file`：首次加入model-visible write schema，只原子创建不存在的workspace regular file。
6. Controlled overwrite：使用expected-state conflict detection，不以裸`overwrite=true`绕过lost-update保护。

## 参考与差异

本设计参考其他coding harness中policy、approval UI和executor hard bounds分离的通用原则，但PermissionGate、action classes、reason codes和后续durability transaction均按Leonervis当前单用户CLI、append-only Session与sequential AgentLoop独立设计。Reference repositories保持只读学习材料，不成为runtime dependency，也不复制其permission implementation或prompt。

## 明确不做

- CLI permission/approval flags、interactive prompt或non-TTY policy；
- action digest、approval token、lease或single-use consumption；
- Session permission/action records、replay recovery或compaction integration；
- AgentLoop dispatch interception或tool budget behavior change；
- `write_file`、edit/patch/delete、Bash/test或workspace外操作；
- dynamic tool registry、plugin、MCP、parallel approval或multi-agent delegation；
- OS/VM/container sandbox或hostile multi-user filesystem guarantee；
- real-provider smoke、credential、network或API费用。

## 验证

Deterministic unit tests必须覆盖全部action/mode/approval matrix、stable enum/reason values、frozen request/result、unknown fail-closed与invalid construction。Existing read-file tests必须覆盖最终和中间symlink指向workspace内外以及broken symlink，并证明普通nested UTF-8 file读取不回归。完整pytest、Ruff、format、lock、diff和fake CLI smoke仍是release gate。
