# 0004：Foundation 1B 的受限 `read_file` 工具循环

- **状态**：已采纳
- **日期**：2026-07-16

## 要解决的问题

Foundation 1A 已建立有序的 user/assistant 文本历史，但 provider 只能返回最终字符串，无法表示「需要 Host 执行一个动作，再根据结果继续回答」。因此也尚未存在 workspace 权限边界、tool request/result 的因果关联或受控停止条件。

Foundation 1B 只增加一条最小、可验证的路径：provider 请求读取一个 workspace 内的文件，`AgentLoop` 执行受限只读工具，把结构化结果交回 provider，随后得到最终 assistant 文本。

## 决策

核心 contract 使用不可变记录：

```text
UserMessage(text)
AssistantText(text)
ToolUse(tool_use_id, name, path)
ToolResult(tool_use_id, content, is_error, truncated)
```

`ConversationProvider.respond(history)` 每次只会返回两种 provider response 之一：

```text
AssistantText  # 本轮完成
ToolUse        # 请求一个动作
```

完整因果链保留在 `AgentLoop.history` 中：

```text
UserMessage
→ ToolUse(read_file)
→ ToolResult(同一 tool_use_id)
→ AssistantText
```

`ToolResult.tool_use_id` 必须与对应 `ToolUse.tool_use_id` 完全相同。错误也通过 `ToolResult(is_error=True)` 返回给 provider，不把用户提供的路径错误变成 loop 级异常。

## 一轮执行和提交语义

```text
run(prompt)
  → 构造 prospective UserMessage
  → provider response
  → ToolUse 时执行并追加 ToolResult，再调用 provider
  → AssistantText 时才提交整条 candidate context
  → 返回最终文本
```

`AgentLoop` 是 canonical history 的唯一所有者。每个输入的 user、tool use、tool result 和最终 assistant text 先保留在局部 candidate context；只有获得最终 `AssistantText` 后才一次提交。因此 provider 在工具结果之后失败时，之前已完成的对话保持不变，失败输入不会在重试时重复出现。

另保留只读 `turns` snapshot，其中仅有 `(UserMessage, AssistantText)` 完整回合，用于 REPL `/history <count>`。工具路径、文件内容、tool ID、内部错误不会显示为用户对话历史。

每个 user turn 最多执行三个工具调用。第 4 个 `ToolUse` 不会执行，而会得到固定的预算错误 `ToolResult`；provider 可据此返回最终文本。若它仍请求工具，loop 抛出确定性 `ToolLoopLimitError`，且 candidate context 不提交。此限制避免无限循环，也让 provider 获得可读的停止原因。

## `read_file` workspace 边界

Foundation 1B 只提供 `read_file(path)`：

- `AgentLoop` 从 CLI composition root 接收 `ReadFileTool`；裸命令和 `prompt` 命令的当前目录既是 banner 展示路径，也是唯一 workspace root；
- path 必须是相对路径；绝对路径立即拒绝；
- 将 root-relative path 和其中的符号链接 resolve 后，解析结果必须仍在已解析 workspace root 内；因此 `..`、兄弟目录和最终/中间符号链接逃逸都会被拒绝；
- 缺失路径、目录、非普通文件、不可读文件和无效 UTF-8 都生成结构化错误结果；
- 正常读取最多回传 32 KiB UTF-8 字节。超过上限时在有效 UTF-8 边界截断，加入固定 `[truncated]` 标记，并标记 `truncated=True`；
- 不写入、删除、重命名、执行命令、访问网络或搜索目录。

此实现防止常规 traversal 和 symlink escape，但不是多用户对抗性 sandbox：`resolve()` 与随后打开文件之间仍可能存在本地 symlink-swap TOCTOU race。future slice 若要处理不可信并发 workspace，需采用 descriptor-relative/no-follow 打开策略或 OS/容器隔离。

## 模块职责

```text
core          最小结构化 conversation/tool contract
agent         candidate context、工具循环、回合预算与原子 commit
tools         workspace-confined read_file 执行和内容限制
providers     记录 snapshot 的 ScriptedFakeProvider
cli           workspace composition、输入、显示、REPL 本地命令
```

`ScriptedFakeProvider` 的 script 可依次提供 `AssistantText`、`ToolUse` 或异常；默认模式仍返回 `AssistantText("Fake response: <最后 user 文本>")`，保持自动化命令可见输出兼容。它不读取文件、环境变量、凭据、网络或时钟，也不模拟模型理解。

## 参考与差异

此切片借鉴 Coding Agent 中 assistant tool request 与 host tool result 必须成对、有序且可诊断的原则，但不从学习子模块导入或复制运行时代码。它比完整 agent tool protocol 更小：一次 response 只有一个 tool request，尚无 assistant 多 block、并行调用、通用 JSON 参数、streaming、thinking、system prompt、registry、policy gate、审批或 transcript。

## 明确不做的内容

Foundation 1B 不包含：

- 真实模型 SDK、认证、网络、模型选择、streaming 或 prompt caching；
- Bash、glob、grep、写入、编辑、删除、重命名或 approval；
- 通用工具注册表、权限模式、并行工具或重试；
- session ID、JSONL transcript、resume、长期记忆、持久化或 context compaction；
- 新的 CLI workspace 选项、调试命令或向用户暴露 fake-provider script；
- 多用户安全隔离、descriptor-relative 文件打开或 OS sandbox。

## 验证证据

测试必须证明：

1. `UserMessage → ToolUse → ToolResult → AssistantText` 顺序与 tool ID 精确配对；
2. 下一次 provider 调用能看到此前完整的结构化因果链；
3. provider 在工具结果后失败时，history 和 displayable turns 都不污染；
4. 未知工具、文件读取错误和预算错误作为可恢复 ToolResult 返回；
5. 第四次请求不执行，第五次工具请求触发受控停止；
6. `read_file` 拒绝绝对路径、`..`、目录、缺失路径和最终/中间 symlink escape；
7. 文件读取不写入，且无效 UTF-8、32 KiB 截断和多字节边界行为稳定；
8. `/history` 只显示完整 user/final assistant 回合；
9. 默认 prompt、别名、模块入口和非 TTY 行为维持兼容。
