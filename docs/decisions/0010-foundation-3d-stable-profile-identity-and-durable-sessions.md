# 0010：Foundation 3D 的稳定 Profile Identity 与可恢复 Session

- **状态**：已采纳
- **日期**：2026-07-17

## 要解决的问题

Foundation 3C 已有命名 profile、workspace active override、长生命周期 provider client 和 provider-neutral 内存历史，但 profile 名同时承担身份与显示职责，退出进程后对话也会丢失。改名、同名删除后重建和跨进程恢复会因此产生错误引用；只把聊天文本写成 JSON 又无法可靠保存工具因果链、运行时 provenance、损坏边界和并发 writer 状态。

## 决策

Foundation 3D 同时引入两组稳定但彼此解耦的状态：

```text
ProviderProfileStore v2
  profile_id + revision + mutable name + non-secret config

SessionStore
  workspace-bound UUID + append-only JSONL + lifetime writer lock
```

Session **不拥有或恢复 runtime provider 配置**。它只记录每个历史 turn 当时实际使用的 profile/route provenance；恢复或在 REPL 中切换 Session 后，当前 runtime 继续服从调用时的 CLI/profile active 选择。历史 profile 被改名、修改或删除不会阻止恢复，也不会按旧 binding 重建 client。若当前 adapter 无法接受旧历史，按正常 provider error 返回，失败 turn 不进入恢复历史。

## Profile Identity 与 Schema v2

`provider_id` 继续表示 catalog/adapter kind。用户创建的 profile 另有不可变 canonical UUID `profile_id`、正整数 `revision` 和可变唯一名称。

- create 使用 UUID4、revision 1；
- rename 保持 ID、revision 增加；
- 内容 replace 保持 ID并增加 revision，完全相同为 no-op；
- remove 后同名 re-add 得到新 ID；
- active selection 持久化 profile ID，不持久化名称引用；
- optional expected revision 提供 compare-and-swap 冲突检测。

旧 schema v1 用固定 namespace 与原始、大小写敏感名称生成 UUID5。reader严格分派v1/v2 closed schema，并支持 user/project 的 v1/v1、v1/v2、v2/v1、v2/v2 四种组合。每次 mutation 只原子升级自己写的文件；显式 `provider migrate` 可升级两层。user registry 与 workspace selection 可能位于不同文件系统，因此只承诺单文件原子和 mixed-schema crash consistency，不声称跨文件 ACID。

profile fingerprint 对规范化配置做 SHA-256，排除名称、身份、revision和credential状态；route fingerprint 对 resolved provider/protocol/model/URL/generation/adapter contract 做 SHA-256，排除credential value和presence。

## Session 格式

默认位置：

```text
<workspace>/.leonervis-code/sessions/<workspace-fingerprint>/
  <session-id>.jsonl
  <session-id>.lock
  latest.json
```

Session ID 使用 UUID4；workspace fingerprint 是 canonical workspace 的 versioned SHA-256。JSONL首行必须是唯一 `session_header`，后续 sequence严格连续。

成功 turn 使用一整行 `turn_committed` 作为 durable commit单元，items完整保存：

```text
UserMessage → optional ToolUse/ToolResult pairs → AssistantText
```

这样工具调用和结果不会被一次进程崩溃拆成两个可恢复记录。其他 audit records 包括 runtime change、turn failure、resume、tail recovery 和 close；它们不进入model history。

每个 committed turn保存实际 runtime provenance：profile ID/revision/name snapshot和fingerprint（如有）、selection source、provider/protocol、selected/wire model、resolved base URL/source、credential env reference、generation参数、adapter contract和route fingerprint。credential value永不保存；这些字段仅用于审计，不参与resume client选择。

## 持久化、恢复与损坏边界

- Session目录为 `0700`，transcript/lock/latest为`0600`（平台支持时）；拒绝symlink、非常规文件和workspace外path。
- 打开的Session在整个生命周期持有nonblocking exclusive writer lock；第二进程不能同时写同一Session，不同Session可并行。
- record append后flush/fsync；latest通过temp+fsync+replace更新，ID碰撞不覆盖。
- reader严格验证UTF-8、closed schema、version、sequence、workspace、ID/文件名、大小和工具因果链。
- 唯一自动修复是无换行且不能形成完整UTF-8 JSON的最后尾部；恢复时截断到最后完整行并写入`recovery`。中间坏行、完整但非法的末行、未知version/type、sequence错误和错误tool pairing全部fail closed。
- close record是生命周期审计边界，不是不可再开的墓碑；后续resume追加`session_resumed`并继续同一历史。

## Persist-before-memory

`AgentLoop`继续在candidate history中完成provider/tool loop，但最终回答产生后先构造完整 committed turn并调用durable callback。只有JSONL append和fsync成功，才替换内存history/turns。

因此本地磁盘失败时：

- provider远端调用无法回滚；
- transcript和内存history都保持上一个committed状态；
- 下一轮不会继承未持久化turn。

失败turn可以写安全audit信息，但不会保存半完成candidate，也不会进入恢复history。

## Runtime 与 Session 切换

`ProjectSession`持有一个RuntimeProviderManager和一个可切换Session writer/AgentLoop：

- create/resume时先按当前CLI/profile active优先级构造runtime，再创建或加载history；历史binding不选择client。
- `/resume`先获得目标writer lock并严格replay到候选loop；成功后才交换current Session并释放旧lock，runtime client保持不变。
- provider/model switch继续遵循manager的candidate-client两阶段规则；随后记录runtime audit。profile文件、client交换和append-only audit不是跨文件事务，audit失败会被明确报告而不伪称回滚。
- slash commands不进入model history。

## CLI 与公共API

新增：

```text
-C, --cwd PATH
--resume latest|SESSION_ID|PATH
session list
session show [latest|ID|PATH]
provider migrate
provider rename
provider replace
provider list --show-ids
--profile-id / provider command --id
```

REPL显示Session ID和transcript位置，并提供`/session show`、`/session list`、`/resume latest|ID`。`prompt` stdout仍只输出最终回答，Session元数据通过单独命令或公共API读取。

公共API导出`ProjectSession`、`SessionInfo`和安全错误类型；文件锁和JSON codec保持内部实现。

## Secret 与隐私边界

profile和Session只保存credential环境变量名，不保存value；factory仍只在client构造时读取value；normalized SDK failure不会写raw body/header。

Transcript仍可能包含用户主动输入的secret或workspace文件中被工具读取的secret。Foundation 3D不声称能通用识别未知敏感文本；文档应将`.leonervis-code`视为本地敏感运行状态，不自动提交、同步或导出。

## Claw-Code参考与主动差异

采用Claw-Code的provider-neutral typed history、workspace session store和provider调用前adapter conversion；不照搬其Session只记录单一model、live resume静默混淆provenance、无profile/endpoint stable identity、缺少writer lock和schema gate、或完整runtime config可能泄露secret的边界。

## 明确不做

本阶段不实现自动compact、fork/branch/export、SQLite、云同步、加密/keyring、retention、跨workspace relocate/resume、多writer协作、fallback/retry、streaming、并行工具、自动profile热更新或按历史binding重建client。Context达到后续预算上限时必须明确失败，不静默删除或拆散tool pair。
