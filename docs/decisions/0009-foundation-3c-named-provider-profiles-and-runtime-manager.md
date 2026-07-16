# 0009：Foundation 3C 的命名 Provider Profiles 与常驻运行时

- **状态**：已采纳
- **日期**：2026-07-16

## 要解决的问题

Foundation 3B 已能用一次性 `--model` 调用多个 provider，但真实 client 每次命令都重新构造，裸 REPL 仍固定使用 fake provider，其他项目模块也只能自行拼 resolver、factory、`AgentLoop` 和 workspace tool。用户需要的是一个可配置、可持续对话、能在 turn 之间切换 endpoint，并可由 CLI 之外代码直接调用的运行时。

## 决策

新增四层职责：

```text
NamedProviderProfile       严格、无 secret 的 endpoint/model 配置
ProviderProfileStore       全局 registry + workspace active override
RuntimeProviderManager     长生命周期 client、原子切换、turn lease
ProjectSession             workspace + AgentLoop + manager 的 public facade
```

`ProjectSession.prompt()` 在一个完整 `user → optional tool use/result → final text` turn 内固定同一个 provider；profile 或 model 只能在两个已完成 turn 之间切换。切换 client 不替换 `AgentLoop`，所以中立的完整 causal history、tool-use ID 和 tool-result ID 原样保留。

## 配置层次与优先级

用户 profile registry 位于：

```text
${XDG_CONFIG_HOME:-~/.config}/leonervis-code/providers.json
```

项目只保存 active override：

```text
<workspace>/.leonervis-code/provider.json
```

选择优先级为：

```text
显式 --profile (+ 可选 --model runtime override)
  > 显式 --model direct selector / custom endpoint
  > workspace active profile
  > user active profile
  > fake/offline
```

项目文件不复制 profile 定义。这样 endpoint policy 只有一份全局事实来源，项目只表达选择。`--model` 与 `--profile` 同用时只覆盖当前进程的 model，不修改 profile；Foundation 3B 的 direct `--model provider/model` 保持兼容。

## Profile Schema 与 Secret Boundary

profile 只保存：名称、provider/protocol、默认 model、可选 base URL、credential 环境变量名、max output tokens 和 temperature。未知字段、未知 schema version、协议不匹配、非法环境变量名、带 credential/query/fragment 的 URL 都 fail closed。

不允许 `api_key`、token、headers 或任意 request JSON。credential value 仍只在 `create_provider()` 构造 SDK client 时从注入 environment 读取。list/show/status/route 只报告 configured、missing 或 not required，不输出 value；一般 status 也不输出 env 名。

## 持久化安全

store 通过同目录独占临时文件写入、flush/fsync 后 `os.replace`，并使用目录 `0700`、文件 `0600`（平台支持时）。目标文件、配置目录或已有路径链中的 symlink 和非普通文件都被拒绝。损坏 JSON、未知字段、未知版本和悬空 active reference 不会被静默忽略或退回 fake。

## 原子运行时切换

`RuntimeProviderManager.use_profile()` 分两阶段：

1. 加载 profile、解析 route、检查 credential 并构造候选 SDK client；该阶段不发网络请求；
2. 原子写入 active 选择，成功后才交换内存中的 client，再关闭旧 client。

任一步失败都保留旧 client 和旧 persisted active。`clear` 也先预览并构造下一层候选，再提交清除。`set_model()` 只交换进程内候选，不写 profile。重选同一 profile 可重建 client，从而显式重载已更改的环境 credential；不做后台轮询或自动重连。

manager 用 turn lease 防止并发 prompt/switch。旧/new client 支持可选 `close()`；manager 负责幂等关闭，关闭异常不会把 SDK 内容暴露给用户。

## 跨 Provider History

历史保持 provider-neutral `ConversationItem`，不扁平化、不改写旧 tool ID、不删除旧内容。切换后新 adapter 按既有 strict serializer 接收完整历史；若新 provider 拒绝历史，错误安全返回，`AgentLoop` 的 candidate-commit 规则确保失败 turn 不进入 history。

## CLI 与 REPL

CLI 增加：

```text
provider add/list/show/use/clear/remove
--profile NAME [--model OVERRIDE]
```

REPL 增加 `/status`、`/provider list`、`/provider current`、`/provider use <name>` 和 `/model <model>`。slash command 由本地处理，不进入模型 history。显式或 active profile 让裸 REPL 使用真实长生命周期 client；没有任何选择时仍完全 fake/offline。

`ProjectSession` 作为项目级 API 导出 `prompt()`、`list_profiles()`、`use_profile()`、`clear_active()`、`set_model()`、`status()`、`history`、`turns` 和 `close()`，避免其他模块依赖 CLI internals。

## Claw-Code 参考与主动差异

采用 Claw-Code 的显式配置优先级、长生命周期 provider client、REPL 中替换 client 并保留 session history 的原则。Leonervis 独立用 Python 实现，并主动补齐 reference 中未形成完整 lifecycle 的命名 profile registry。

不同点：

- 不保存明文 key，只保存 env 名；
- active identity 包含 profile/endpoint provenance，不只保存 model 字符串；
- profile CRUD、项目覆盖、原子写入与两阶段 client switch 都有明确测试；
- 不把 fallback chain 与命名 endpoint profile 混为一谈；
- CLI 只做呈现，runtime 由 `ProjectSession`/manager 提供。

## 明确不做

Foundation 3C 不包含 JSONL transcript/resume（留给 3D）、streaming、retry/backoff/fallback、live discovery、跨进程热更新、OAuth/keyring/`.env` 加载、明文 key、并行工具、托管 gateway、多用户或计费服务。
