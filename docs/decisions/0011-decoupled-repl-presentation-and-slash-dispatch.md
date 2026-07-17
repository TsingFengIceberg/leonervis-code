# 0011：解耦的 REPL 展示与 Slash Command 分发

- **状态**：已采纳
- **日期**：2026-07-18

## 要解决的问题

Foundation 3D 已有持久 Session、runtime 切换和 REPL 控制命令，但固定 `leonervis> ` 无法持续提示当前 Session/runtime，而展示、参数解析、状态操作和输入循环集中在一个 `run_repl()` 中。未来加入权限、compact 和工具事件时，继续堆叠分支会使终端边缘逻辑反向牵制核心能力。

## 决策

CLI 采用单向依赖：

```text
Session / provider public snapshots
                 ↓
        presentation + slash dispatcher
                 ↓
             REPL streams
```

核心 Session、provider、agent 和 tool 模块不 import CLI。展示层只结构化读取 `SessionInfo`、`RuntimeStatus` 等公开、脱敏字段；slash dispatcher 只调用 `ProjectSession` facade，不读取 writer、manager、transcript record 或 SDK client 内部状态。

## 动态 Prompt

默认逻辑格式：

```text
leonervis[<session8>|<runtime>]>
```

- `session8` 是当前 canonical Session UUID 的前 8 位，只用于视觉辨识，不成为 selector；
- 命名 profile 的 runtime 标签使用当前 profile name；
- direct route 使用 `direct:<provider_id>`；
- fake/offline 使用 `fake`；
- model、workspace、turn count 和历史 binding 不进入 prompt。完整信息仍通过 `/status`、`/provider current` 和 `/session show` 获取。

Prompt 每轮从当前公开 snapshot 重建。`/session new` 与 `/resume` 只改变 session 字段，`/provider use` 改变 runtime 字段，`/model` 不改变 runtime identity 字段。历史 Session binding 不能覆盖当前 runtime 展示。

动态文本只允许受控 ASCII 字符并定长截断，其他字符替换为 `?`，防止 ANSI、控制字符、换行、delimiter 或双向文本影响终端结构。

## 语义颜色与 Readline

LEO 标志继续使用品牌 truecolor；交互消息使用无依赖的传统 ANSI 颜色：

- 红：失败；
- 绿：成功；
- 黄：usage、warning、fake/offline；
- 蓝：信息、Session 与 real runtime 上下文。

颜色不是唯一语义载体，模型最终回答不着色。仅在 stdout 是 TTY 且未设置 `NO_COLOR` 时启用。

真实 `input()`/readline prompt 的 ANSI 非打印序列使用 `\001`/`\002` 标记，避免光标移动和换行宽度错误；注入 stream 不接收这些 marker。

## Slash Command 边界

`cli/slash.py` 返回结构化 `SlashResult`，不直接写 stream 或 ANSI。`cli/presentation.py` 负责纯格式化，`cli/repl.py` 只负责：刷新 snapshot、读取输入、dispatch、渲染 result 或调用模型。

`/session` 与 `/provider` 提供命令组帮助。已识别命令族的缺失、多余或未知参数返回针对性 usage；真正未知的顶层命令才回退 `/help`。Slash commands 始终不进入模型历史。

## 明确不做

本切片不实现 TUI、二级动态补全、持久 readline history、多行编辑、Session 短 ID selector、Session 标题、Ctrl-C 模型/工具取消、权限提示或工具事件渲染，也不增加第三方终端依赖。未来这些能力通过公开 snapshot/event 扩展 presentation/dispatcher，而不是让核心 import 终端代码。
