# 0003：Foundation 1A 的进程内文本历史

- **状态**：已采纳
- **日期**：2026-07-15

## 要解决的问题

Foundation 0 的 REPL 可以连续接收输入，但每次 `AgentLoop.run(prompt)` 都把 prompt 当作独立请求。因此第二条输入不知道第一条 user/assistant 交互，也无法验证后续真正的模型—工具因果链需要的有序上下文。

Foundation 1A 只建立最小文本历史：谁保存消息、何时提交、下一次 provider 看见什么，以及 provider 失败时历史如何保持一致。

## 决策

引入不可变的 `TextMessage(role, text)`，其中 `role` 仅允许 `user` 或 `assistant`。provider seam 改为：

```text
ConversationProvider.respond(history: tuple[TextMessage, ...]) -> str
```

`AgentLoop` 作为唯一的进程内历史所有者：

```text
run(prompt)
  → 创建 prospective user message
  → 将 completed history + user message 交给 provider
  → provider 成功返回文本后
  → 同时提交 user message 与 assistant message
  → 返回 assistant 文本给 CLI/REPL
```

同一 REPL 启动只创建一个 `AgentLoop`，因此后续输入能看到此前成功完成的 user/assistant 对。一次性 `prompt` 命令每次都创建新 loop，因此保持一轮、空历史的外部行为。

历史以只读 tuple snapshot 暴露给测试；不会生成 ID、时间戳、文件、JSONL、session 或任何持久化格式。

## 原子失败语义

provider 调用失败时：

1. 原始异常直接向上传播；
2. prospective user message 不提交；
3. 不构造 assistant error message；
4. 已完成历史保持在最后一组 user/assistant 对；
5. 用户之后重试时，失败 prompt 不会重复出现在 provider history。

这保证每个已提交的 user message 都紧跟真实 assistant 文本，不产生半个 turn 或伪造因果链。

## Scripted fake provider

`ScriptedFakeProvider` 是本切片的确定性测试替身：

- 每次记录 provider 收到的完整 immutable history snapshot；
- 无 script 时保持 CLI 兼容输出：`Fake response: <最后一条 user 文本>`；
- 有 script 时按调用顺序返回文本或抛出预设异常；
- script 耗尽时产生固定、可断言的错误。

它不模拟模型理解、不会生成真实答案，也不读取凭据、环境变量、网络或文件。

## 模块职责

```text
cli      输入、显示、局部命令、REPL 生命周期
agent    有序 history、provider 调用、成功后 commit
core     TextMessage 与 ConversationProvider contract
providers  history-aware deterministic fake
```

REPL 不拥有或重建历史；它只把普通输入交给 loop。`/help`、`/history <count>`、`/exit`、`/quit`、空输入、EOF、Ctrl-C 与 Tab 补全都不是对话消息。`/history <count>` 只读取已完成的 user/assistant 对，按时间顺序显示最近 `count` 个回合；它不调用 provider，也不改变历史。

## 参考与差异

本切片借鉴 Coding Agent 需要保留 user 与 assistant 因果顺序的一般原则，但不从学习子模块导入或复制运行时代码。它比完整 agent transcript 更小：尚无 system message、tool use、tool result、streaming block 或持久化 session。

## 明确不做的内容

Foundation 1A 不包含：

- 真实模型 SDK、API、认证、网络、模型选择或 streaming；
- system prompt、assistant content block、thinking、cache、compaction 或重试；
- 工具、workspace、文件、Bash、审批或权限策略；
- session identity、JSONL transcript、resume、长期记忆或磁盘持久化；
- `/clear`、`/session`、`/resume` 或任何未实际实现的 REPL 命令。

## 验证证据

测试必须证明：

1. 第一轮 provider 只收到 user message；
2. 第二轮 provider 收到完整有序的前一 user/assistant 对加新 user message；
3. 每轮成功后只有一个完整 user/assistant 对被提交；
4. 异常不会污染 history，重试不会重复失败输入；
5. fake provider 的 received history 是不可被后续调用改变的 snapshot；
6. REPL 局部命令、空行和退出路径不会产生消息；
7. 一次性 `prompt`、别名与模块入口保持原有输出与退出码。
