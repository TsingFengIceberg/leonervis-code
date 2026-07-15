# 0002：Foundation 0 的确定性 REPL

- **状态**：已采纳
- **日期**：2026-07-15

## 要解决的问题

Foundation 0 已证明一次性 `prompt` 命令能够完成 CLI → AgentLoop → fake provider 的控制流，但用户还不能像使用终端型 Coding Agent 一样直接启动一个交互工作台。

本切片只补充终端输入体验，不提前把 REPL 误实现成真实模型会话、session 存储或工具运行时。

## 决策

不带子命令启动时进入本地 REPL：

```text
leonervis-code
  → 确认 stdin/stdout 都是交互终端
  → 输出 LEO 标志、版本、Foundation 0 状态和当前目录
  → 读取一行输入
  → AgentLoop.run(prompt)
  → DeterministicFakeProvider.respond(prompt)
  → 显示文本结果
  → 重复，直到退出
```

一次性自动化接口保持不变：

```text
leonervis-code prompt "..."
```

REPL 使用同一个 `AgentLoop` 和 `DeterministicFakeProvider`。它只是在同一进程中重复接收输入；每条非空输入仍独立完成一次 provider 调用，**不保存任何对话历史**。

## 终端呈现

启动 banner 使用 Leonervis 自己的 LEO 像素标记与既有配色：

```text
L：深琥珀色（尾部）
E：金橙色（躯干）
O：浅金色（头部）
```

banner 显示产品版本、`Foundation 0 · deterministic local provider` 和当前目录。它不声称真实模型、上下文窗口、计费、workspace 或会话能力。

颜色仅在 stdout 是 TTY 且未设置 `NO_COLOR` 时输出。每个带色字符之后立即重置 ANSI 样式；非彩色路径保持相同的文字和图形形状，不包含 ANSI 转义序列。

当前目录只用于展示。家目录下的路径显示为 `~` 前缀，但这不是 workspace 解析、边界约束或持久化 session。

## 本地控制命令和退出语义

只实现当前真实存在的本地控制：

```text
/help           显示控制说明
/exit、/quit    正常退出
Ctrl-D / EOF    正常退出
Ctrl-C          正常退出
```

空行或仅包含空白的输入被忽略。未知 `/` 命令显示简短提示后继续。不会把当前不存在的 `/model`、`/session`、`/resume`、`/permissions`、`/tools` 或 `/clear` 做成空壳。

在非交互 stdin/stdout 下直接运行裸命令会快速失败，stderr 指向 `leonervis-code prompt "..."`。此策略避免管道、CI 和 subprocess 捕获输出时等待输入或混入终端控制字符。

## 模块边界

- `cli.brand`：纯标记、ANSI 色彩、路径显示和 banner 渲染；
- `cli.repl`：输入、显示、控制命令、EOF/Ctrl-C 处理；
- `cli.main`：argparse 分发与 `AgentLoop(DeterministicFakeProvider())` composition root；
- `agent.loop`：仍然 UI 无关、无状态，只执行一次 provider 调用；
- `core` 与 `providers.fake`：保持 Foundation 0 原有边界。

`banner_sample.py` 与 README PNG 生成脚本复用 package-owned 标记数据，避免终端样例、README 标志和运行时 banner 漂移。

## 参考与差异

本切片参考终端型 Coding Agent 的“裸命令进入交互环境、启动时展示状态”的使用体验，但不复制任何其他产品的图形、文案、模型信息或服务声明。Leonervis 使用自己的 LEO 标记，并如实标注确定性 fake provider。

## 明确不做的内容

本切片不新增：

- 真实模型 API、SDK、凭据、网络或模型选择；
- 流式生成、assistant content blocks、多轮模型 history 或 prompt caching；
- 工具、文件、Bash、workspace、审批或权限策略；
- session ID、transcript、JSONL、恢复、长期记忆或持久化；
- TUI、Web/IDE 表面、MCP、插件、多 Agent、服务端、RAG 或后台任务。

## 验证证据

测试和手动验证应证明：

1. banner 的行数、颜色与无色 fallback 可预测；
2. REPL 只对每条有效输入调用一次 loop；
3. 空输入、帮助、未知命令和所有退出路径可诊断；
4. 裸命令在非 TTY 下不会挂起；
5. `prompt`、别名和 `python -m` 的一次性接口保持不变；
6. README、生成资产与终端标记来自同一套 glyph/palette 定义。
