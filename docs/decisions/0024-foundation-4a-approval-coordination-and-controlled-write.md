# 0024：Foundation 4A Approval Coordination、Runtime Integration与Controlled `write_file`

- 状态：已接受
- 日期：2026-07-23
- 范围：Foundation 4A Slice 5–9

## 问题

ADR 0022固定了纯`PermissionGate`，ADR 0023固定了exact action identity、single-use approval grant与durable audit，但这些Host-only契约尚未回答真实模型工具请求如何经过policy、human approval、durable start barrier和executor，也没有定义CLI在交互与非交互环境中的安全行为。

第一个写工具还会引入比只读工具更严格的问题：模型不能可信地自报create或overwrite，approval必须绑定审批时观察到的目标状态，执行前必须重新检查stale/conflict，文件安装必须尽量failure-atomic，写入已经可见但cleanup或directory fsync失败时又不能谎称“没有写”。这些语义必须先于Bash、patch/edit、delete或mkdir落地。

## 决策

### Central ActionCoordinator固定唯一编排顺序

每个permission-eligible action都经过同一Host coordinator：

```text
action_requested
→ permission_decided
→ deny / ask / allow
→ ask时调用human approval handler
→ accept时签发并消费exact single-use grant
→ action_execution_started append+fsync
→ executor
→ action_execution_finished append+fsync
```

`deny`不询问也不执行；`reject`和`cancel`保存对应approval record并返回structured error `ToolResult`。`allow`使用`policy-allow` authorization，accepted `ask`使用与完整`ActionIdentity`绑定的`approval-grant` authorization。Executor只有在durable start成功后才能产生副作用。

普通executor异常转换为安全失败结果并完成audit；但副作用已经返回后若final audit append失败，异常必须向上传播，不能伪装rollback、重新执行或把未durable finish写成成功。Read-only actions也经过同一coordinator，以保持一致的policy与audit路径。

### Prepared-turn ActionLease在automatic compaction后建立并固定continuation

`PreparedAgentTurn`在所有pre-turn automatic compaction完成后绑定一个`ActionLease`。Lease包含当前Session、不可重建lease UUID、runtime generation与已提交Effective Context ID。同一user turn内的所有provider continuation、tool result与approval使用同一lease、runtime、system prompt和Effective Context snapshot。

一旦turn持有lease就不得rebase；ProjectSession lock覆盖完整provider/approval turn，因此approval等待期间不能切换runtime、resume到另一Session、替换context或并发开始另一turn。执行前若lease、runtime或context不再精确匹配，turn失败且不继续调用provider。

### CLI把capability ceiling与interaction mode作为正交参数

全局CLI参数为：

```text
--permission-mode read-only | workspace-write | danger-full-access
--approval ask | auto
```

默认是`read-only + ask`。`permission-mode`决定能力上限，`approval`只决定能力范围内的风险动作是询问还是自动继续；`auto`从不绕过workspace、symlink、UTF-8、size、conflict、causality、audit或durability hard bounds。

One-shot `prompt`遇到`ask`时fail-safe cancel，且绝不从stdin偷读approval，以免脚本输入被误当确认。REPL才提供terminal confirmation：显示trusted action class、workspace-relative path与UTF-8 byte count，不显示完整content；`y/yes`接受，空输入或`n/no`拒绝，`c/cancel`取消，EOF与Ctrl-C取消，三次非法输入后也取消。

### `write_file`只暴露完整内容，不让模型选择执行语义

第四个model-visible tool固定为：

```json
{"name":"write_file","input":{"path":"workspace-relative path","content":"complete UTF-8 content"}}
```

Canonical顺序现在是`read_file, glob, grep, write_file`。四种工具共享每个user turn最多三次resolved/executed calls；第四次请求只收到现有structured limit result，不创建action lifecycle，模型若继续请求工具则turn不提交并确定性停止。Provider仍只能一次返回final text或一个tool call，compact-summary request继续不暴露任何tool，parallel tool calls继续关闭。

模型不能传`overwrite`、expected hash、mkdir、delete、patch或approval字段。Host根据真实目标状态分类：

```text
目标不存在                 → workspace-create + path-absent
现有UTF-8普通文件           → workspace-overwrite + expected-state-sha256
```

Malformed argument、unsafe path、symlink、缺失parent、特殊文件、oversized或non-UTF-8 existing target在permission eligibility之前被hard reject。它们作为普通error `ToolResult`返回模型并消耗共享tool budget，但不会创建虚假的action audit lifecycle。

### Workspace与content hard bounds不可被approval绕过

`path`必须是portable、workspace-relative、`/`分隔的文件路径；拒绝absolute path、Windows drive、backslash、NUL、`.`、`..`、空component、重复`/`和尾随`/`。Parent必须已存在且所有intermediate/final components都不是internal、external或broken symlink；工具不自动mkdir。

新content同时限制为最多4096 characters和4096 UTF-8 bytes。Overwrite只接受现有UTF-8 regular file，source observation最多1 MiB，并记录SHA-256、device、inode与mode。执行前重新观察目标；create若不再absent，或overwrite的digest/inode不再匹配approved state，则拒绝lost update。Overwrite保留原mode。

这些检查面向local single-user v0，不声称构成跨所有不合作外部writer的portable filesystem CAS，也不声称提供OS sandbox。

### Create与overwrite采用same-directory atomic target installation

Create执行：

```text
same-directory exclusive temp
→ write all
→ fsync temp
→ hard-link temp到仍不存在的target
→ unlink temp
→ fsync parent directory
```

Hard-link安装避免覆盖执行期间新出现的目标。Overwrite执行：

```text
观察digest + device/inode/mode
→ same-directory exclusive temp
→ preserve mode
→ write all + fsync temp
→ exact digest/inode recheck
→ os.replace target
→ fsync parent directory
```

成功结果是deterministic JSON：

```json
{"bytes_written":N,"operation":"created|overwritten","path":"..."}
```

### Visible partial effect必须诚实记录且禁止自动retry

目标安装后，temporary cleanup或parent-directory fsync仍可能失败。此时target变化可能已经可见，不能返回普通“未写入”失败。Executor返回error `ToolResult`，audit使用`partial` outcome与stable result code：

```text
created_durability_unknown
created_with_temporary_cleanup_failure
created_cleanup_and_durability_unknown
overwritten_durability_unknown
```

消息要求用户inspect workspace且不得自动retry。ADR 0023的lifecycle因此由`executing -> succeeded | failed`扩展为`executing -> succeeded | failed | partial`；`partial`是已知的terminal audit outcome，不等同于没有finish的`outcome-unknown`。

### Stale与turn failure保持因果和已发生事实

Auto action在durable start前identity变化时抛出`ActionIdentityChangedError`；accepted approval的precondition或lease stale时抛出`ApprovalGrantError`。这些错误终止当前turn、追加`turn_failed`并把未开始lifecycle派生为`abandoned`，不会伪装成可供provider继续推理的普通stale result。

若write已发生后provider continuation失败或`turn_committed` durable append失败，文件效果和action audit保留，candidate turn不提交，并尝试追加`turn_failed`。Host不得为了补turn或补audit自动重放副作用。Action audit仍只属于Host transcript state，不进入full/effective model history，也不被compaction删除或总结。

### Model-visible与持久化版本

新增`write_file`及permission/approval framing改变了model-visible contract，因此canonical system prompt升级到v5，provider adapter contract升级到v6。Current ordinary provider count/create projection必须都暴露相同的四工具closed schema与顺序；compact-summary projection仍为空工具集。

`ToolArguments`表示保持v1；new `turn_committed`保持record-local schema v2；五种action audit records保持schema v1；`context_compacted`继续支持v2/v3 replay。Effective Context representation继续使用`ctx-v1` full history与`ctx-v2` compacted context，因为identity算法和数据形状未改变；prompt/tool snapshot变化只会自然产生新的current-binary context ID，不重写历史checkpoint。

## 明确不做

- Bash、shell/test execution、network tool或credential use；
- patch/edit、delete、rename、mkdir或自动创建parent directory；
- model-controlled overwrite flag、expected hash、permission mode、approval decision或grant token；
- parallel actions、multi-agent approval、remote approval service或background execution；
- portable full filesystem CAS、hostile concurrent writer消除或OS sandbox声明；
- provider retry/fallback、stale action自动重建、partial/outcome-unknown自动重试；
- 将action audit注入模型history或用compaction改写完整transcript。

## 验证

Deterministic tests覆盖coordinator的allow/deny/ask accept/reject/cancel顺序、single-use grant、durable start barrier、executor/final-audit failure；prepared-turn lease与compaction/continuation固定；CLI defaults、one-shot fail-safe、REPL展示和输入边界；四工具provider schema/order/parsing；create/overwrite、size/path/UTF-8/symlink/parent/file-type/mode/conflict边界；partial visible outcomes；action record round-trip/replay与terminal lifecycle；hard-rejected write无action audit；provider continuation和turn commit失败后的truthful effect/audit preservation。完整pytest、Ruff、format、lock、diff与deterministic fake CLI/resume/write smoke构成release gate；真实provider smoke仍需单独授权且不属于正确性前提。
