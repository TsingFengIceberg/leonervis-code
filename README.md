<div align="center">

<img src="./docs/assets/leo-mark.png" alt="LEO mark" width="240">

# Leonervis Code

[English](./README_en.md) | 中文

[![Python](https://img.shields.io/badge/Python-3.12%E2%80%933.13-3776AB?logo=python&logoColor=white)](./pyproject.toml)
[![uv](https://img.shields.io/badge/uv-managed-DE5FE9?logo=uv&logoColor=white)](https://docs.astral.sh/uv/)
[![pytest](https://img.shields.io/badge/pytest-8%2B-0A9EDC?logo=pytest&logoColor=white)](./pyproject.toml)
[![Ruff](https://img.shields.io/badge/Ruff-0.9%2B-D7FF64?logo=ruff&logoColor=black)](./pyproject.toml)

</div>

Leonervis Code 是一个面向本地单用户使用、以学习为先的 Coding Agent CLI 原型。它将逐步实现一个可理解、可验证的 Harness：模型做出决策，Host 在明确的 workspace 与权限边界内执行受控工具，再把结构化结果写回模型。

> **当前状态：Foundation 3B 的本地多 Provider runtime 已完成。** 显式 `leonervis-code --model <SELECTOR> prompt ...` 可通过同一个 provider resolver/factory 运行 Anthropic Messages，或通过 OpenAI-compatible adapter 接入 OpenAI、xAI、DashScope/Qwen/Kimi、Ollama/local、OpenRouter 与受控 custom endpoint。未提供 `--model` 的默认 `prompt`、裸 REPL 与 `demo-read` 仍使用确定性 fake provider，不会读取 credential 或访问网络；两个真实 SDK 均固定 `max_retries=0`。

## 项目定位

本项目不尝试复刻、替代或承诺兼容 Claude Code 或其他现有产品。第一阶段关注受控模型调用、工具执行、workspace 边界、权限确认、结构化事件与确定性测试这些 Harness 基础能力。

实现会以小而完整的学习切片逐步推进：每次只引入当前功能确实需要的代码和依赖，并同时记录设计原因、数据流、参考差异、已知边界和测试证据。不会为了“看起来完整”而预建 MCP、插件、多 Agent、服务端、RAG 或后台任务等空壳。

相关 Harness 阅读与学习笔记位于 [Harness-study](https://github.com/TsingFengIceberg/Harness-study)。

## 环境要求

| 项目 | 当前要求 | 说明 |
| --- | --- | --- |
| Python | **3.12** 为开发/测试基线；允许 3.13 | 项目声明 `>=3.12,<3.14`。 |
| [uv](https://docs.astral.sh/uv/) | 最新稳定版 | 唯一的 Python 包、虚拟环境和锁定文件管理工具。 |
| Git | 任意当前稳定版 | 用于版本管理；不是 Python 依赖。 |
| pytest | `>=8.3` | 当前确定性测试工具。 |
| Ruff | `>=0.9` | 当前静态检查与格式化工具。 |

仓库根目录的 [`.python-version`](./.python-version) 默认选择 Python 3.12。若机器没有该解释器，`uv` 会在同步时提示；也可使用 `uv python install 3.12` 安装。

## 快速开始

```bash
# 1. 克隆仓库后进入项目目录
cd leonervis-code

# 2. 创建 .venv、安装依赖，并依据 uv.lock 同步环境
uv sync

# 3. 启动本地交互终端（需要真实终端）
uv run leonervis-code
```

启动后会显示彩色 LEO 标志、版本、当前目录（Foundation 1B 的 workspace root）和 Foundation 1B 状态，然后显示：

```text
leonervis>
```

输入任意非空文本可得到确定性结果：

```text
leonervis> 解释 Harness 边界
Fake response: 解释 Harness 边界
```

REPL 内目前只提供本地控制命令：

```text
/help                 查看控制说明
/history <count>      按时间顺序显示最近 count 个完整对话回合
/exit 或 /quit        正常退出
Ctrl-D / EOF          正常退出
Ctrl-C                正常退出
```

如需可见、确定性的 Foundation 1B 工具循环演示，可运行：

```bash
uv run leonervis-code demo-read README.md
```

该命令会明确显示 scripted provider 请求、受 workspace 约束的 `read_file` 结果和 scripted 最终回应。它只是验证辅助入口，不是实际模型接口；不会写入文件、执行命令或访问网络。可用越出 workspace 的路径验证失败边界：

```bash
uv run leonervis-code demo-read ../outside.txt
```

以脚本或自动化方式执行一次 prompt 时，使用显式子命令：

```bash
uv run leonervis-code prompt "解释 Harness 边界"
# Fake response: 解释 Harness 边界
```

两个命令名指向同一个入口：`leonervis-code` 是正式命令，`leonervis` 是简写。也可以使用模块入口：

```bash
uv run leonervis prompt "Hello"
uv run python -m leonervis_code prompt "Hello"
```

`--help` 与 `--version` 也可使用：

```bash
uv run leonervis-code --help
uv run leonervis --version
```

## Foundation 3B：本地多 Provider 真实模型路径

提供全局 `--model` 时，`prompt` 会经过统一 resolver/factory 选择真实 adapter：

```bash
export ANTHROPIC_API_KEY='...'
uv run leonervis-code --model anthropic/claude-opus-4-8 prompt "解释这个 workspace"

export OPENAI_API_KEY='...'
uv run leonervis-code --model openai/gpt-5 prompt "解释这个 workspace"

export XAI_API_KEY='...'
uv run leonervis-code --model xai/grok-3 prompt "解释这个 workspace"

export DASHSCOPE_API_KEY='...'
uv run leonervis-code --model dashscope/qwen-plus prompt "解释这个 workspace"

uv run leonervis-code --model ollama/qwen3:8b prompt "解释这个 workspace"

export OPENROUTER_API_KEY='...'
uv run leonervis-code --model openrouter/anthropic/claude-opus-4-8 prompt "解释这个 workspace"
```

Anthropic 路径使用官方 `anthropic` SDK；其他内置路径复用官方 `openai` SDK 的 Chat Completions wire adapter。两个 SDK 均为同步非流式调用并固定 `max_retries=0`。它们只声明现有的 `read_file(path)`，本地 `ReadFileTool` 继续强制 workspace containment、UTF-8、32 KiB 上限和每 turn 工具预算。

也可显式调用一个临时的 OpenAI-compatible endpoint，不持久化 provider 或 key：

```bash
export VENDOR_API_KEY='...'
uv run leonervis-code \
  --model vendor/model \
  --provider-protocol openai-compatible \
  --base-url https://gateway.example/v1 \
  --api-key-env VENDOR_API_KEY \
  prompt "解释这个 workspace"
```

显式 provider namespace 优先；只有已登记的 `claude-*`、`gpt-*`、`grok-*`、`qwen-*`、`kimi-*` bare family 会被确定性识别，未知 bare model 不依据现有 credential 猜测。route 和 adapter config 不保存 secret value；key 只在 factory 构造所选 SDK client 时读取。当前不读取 `.env`、持久配置、OAuth 或 keyring，也不实现 streaming、自动 retry/backoff、fallback execution、live discovery、并行工具、session 或持久化。

真实 route 可在不构造 client、不中断网络的情况下预览：

```bash
uv run leonervis-code --model openai/gpt-5 route
```

默认 fake 路径保持不变：

```bash
uv run leonervis-code prompt "Hello"   # fake，不联网
uv run leonervis-code                   # fake REPL，不联网
uv run leonervis-code route             # Foundation 2B fake policy preview，不联网
```

详细边界见 [Foundation 3A Anthropic adapter 决策](./docs/decisions/0007-foundation-3a-anthropic-non-streaming-adapter.md) 与 [Foundation 3B 多 Provider runtime 决策](./docs/decisions/0008-foundation-3b-local-multi-provider-runtime.md)。真实 smoke test 只应在用户明确愿意使用自己的 credential、endpoint 和 API 费用时手动运行。

## Foundation 2B：离线 adapter-owned compatibility policy

`route` 是为未来真实 provider adapter 准备的确定性 control-plane 与 adapter-policy 边界诊断入口：

```bash
uv run leonervis-code route
# primary: fake-messages/alpha
#   credential: configured
#   canonical parameters: <none>
#   native preview: <none>
#   diagnostics: <none>

uv run leonervis-code route --model beta --max-output-tokens 32 --fallback-model default
# fake-chat 预览 max_output_tokens；fake-messages 预览 max_tokens

uv run leonervis-code route --model beta --temperature 0.2
# 显示固定采样省略参数的可见 diagnostic
```

route resolver 负责**硬**准入规则：有效的 provider/model 选择、enabled 状态、所需 tool-use/streaming capability、canonical option 类型/范围、fallback 有效性以及 Harness-owned field 保护。选定 adapter 负责 provider-native wire name 与有文档依据的**软**兼容行为。fake `beta` model 用于证明这种区别：请求的 `temperature` 被当作已知 fixed-sampling incompatibility 省略，`route` 会报告该决定，而不是静默改变请求或错误地 hard fail。

provider-specific extension 暂时只提供受控的 Python API 路径；它们不能覆盖 `model`、messages、tools、streaming、token-limit fields 或 adapter-generated parameter fields。这预先建立了安全边界；命令暂不接受任意 JSON body override。

`route` 的 Foundation 2B 子命令形式仍完全离线：不会构造 provider client、读取环境变量或访问网络，也不会显示 credential reference/value。带全局 `--model` 的 `route` 则使用 Foundation 3B resolver 展示真实 route 的 provider、protocol、wire model、base URL 来源和 `configured/missing/not required` 状态；它仍不构造 client或发起请求。成功 preview 不代表远端 provider 必然接受请求。

## Foundation 1B：确定性的受限 `read_file` 工具循环

当前 REPL 和 `prompt` 命令现在完成以下最小、可测路径：

```text
终端输入 → AgentLoop（有序内存因果上下文）
  → ScriptedFakeProvider → 在当前 workspace 内可选 read_file
  → 结构化 tool result → ScriptedFakeProvider → 最终文本输出
```

provider 的一次响应只能是最终 assistant 文本或一个 `read_file` 请求。Loop 只有在 provider 结束后才返回最终文本，并且只有该成功发生后，才提交本次尝试中的完整 user 输入、可能的 tool request/result 与最终 assistant 文本。每个 user turn 最多允许三次文件读取；后续请求会收到结构化上限错误，之后如果仍再次请求工具，loop 会确定性停止。

`read_file` 只接受解析后仍在当前工作目录内的相对路径；本切片中当前工作目录就是 workspace root。它拒绝绝对路径、`..` 或符号链接逃逸、缺失路径、目录、不可读文件和无效 UTF-8；最多返回 32 KiB UTF-8 文本并带截断标记。它不能写入、重命名、删除、执行命令、搜索或访问网络。

默认 `ScriptedFakeProvider` 保持可见的回显行为，且不会自行请求工具；它的 script 形式在测试中为工具循环提供确定性证明，而 `demo-read <path>` 将同一套固定 scripted 链路公开为可手动验证的终端入口。`prompt` 仍是一次性命令，每次新启动的 REPL 都从空历史开始；同一个运行中的 REPL 里，`/history <count>` 只显示已完成的 user/final assistant 对，不显示内部工具数据。

这些状态只在当前进程内，不写入磁盘；它不是 session、transcript、恢复或长期记忆。本切片**不会**调用真实模型 API、读取凭据或环境变量、访问网络、执行 Bash、执行写入操作、进行审批决策、写入 session 或持久化。若在非交互终端中直接运行 `leonervis-code`，程序会提示改用 `leonervis-code prompt "..."` 并以非零状态退出，避免管道或 CI 意外卡住。

详细学习设计记录见：[单轮 Loop 决策](./docs/decisions/0001-foundation-0-single-turn-loop.md)、[确定性 REPL 决策](./docs/decisions/0002-foundation-0-deterministic-repl.md)、[内存文本历史决策](./docs/decisions/0003-foundation-1a-in-memory-text-history.md)、[受限 read-file 工具循环决策](./docs/decisions/0004-foundation-1b-bounded-read-file-tool-loop.md)、[provider-neutral 模型路由决策](./docs/decisions/0005-foundation-2a-provider-neutral-model-routing.md)、[adapter-owned compatibility policy 决策](./docs/decisions/0006-foundation-2b-adapter-owned-compatibility-policy.md)、[Anthropic 非流式 adapter 决策](./docs/decisions/0007-foundation-3a-anthropic-non-streaming-adapter.md) 与 [本地多 Provider runtime 决策](./docs/decisions/0008-foundation-3b-local-multi-provider-runtime.md)。

## 开发与验证

所有命令均通过 `uv run` 在锁定的项目环境中执行：

```bash
# 运行确定性测试
uv run pytest

# 静态检查
uv run ruff check .

# 检查格式；实际格式化时移除 --check
uv run ruff format --check .
```

依赖变化后更新并检查锁文件：

```bash
uv lock
uv lock --check
```

## Leonervis Code 自身环境与目标 workspace 环境

Leonervis Code 当前自身只依赖 Python、uv、Git 和由 `uv.lock` 锁定的 Python 包。它**不会**替被操作的项目安装其构建环境。

例如，将来 Agent 在某个 Node 项目执行 `npm test` 时，Node/npm 属于该**目标 workspace**的要求；Rust/Cargo、Java、Docker 或项目数据库也同理。它们不是启动 Leonervis Code 的前置条件。

因此，目前不需要 Docker、Docker Compose、Node.js、npm、pnpm、Rust、Java、Go、数据库、Redis、消息队列、Web server、反向代理或 Makefile。

## 当前范围与后续方向

目前已建立：

- Python 3.12–3.13、uv 与可复现 `uv.lock` 的项目环境；
- `leonervis-code` / `leonervis` 安装入口及 `python -m leonervis_code` 模块入口；
- 结构化的 `UserMessage` / `AssistantText` / `ToolUse` / `ToolResult` contract、确定性 scripted fake provider、携带原子内存因果历史的 `AgentLoop`、一个受限的 `read_file` 工具、Foundation 2B 离线 route policy，以及支持 Anthropic 与 OpenAI-compatible provider family 的显式本地多 Provider runtime；
- 具有彩色启动标志、最小控制命令、Tab 补全、`/history` 与有序进程内完整回合历史的本地 REPL；
- 可通过 `prompt` 命令端到端运行的自动化友好路径；
- `pytest` 与 `ruff` 的基础质量工具链。

下一切片可在这里已验证的多 Provider seam 上设计安全的 named provider profile/config provenance，或回到本地 Harness 路线引入新的只读工具。Streaming、自动 retry/fallback、文件写入、审批、session 与受控 Bash 都仍需各自的学习切片。

MCP、插件、远程/服务端形态、多 Agent、RAG、后台任务等并非被永久排除，但只有出现明确问题、设计边界和测试方案后才会引入。

## 仓库结构

```text
src/leonervis_code/
  core/                 # 中立的 conversation/tool 与 model-orchestration contracts
  agent/                # 维护受限因果历史与工具决策的 AgentLoop
  tools/                # 当前仅有 workspace 受限的 read_file 工具
  providers/            # 确定性 fake provider 与离线 route planning
  cli/                  # 命令解析、品牌渲染、REPL 与终端输出
tests/                  # 单元、集成、安全与端到端测试将逐步进入这里
docs/                   # 架构决策、学习笔记与安全设计
scripts/                # 可复现的本地/CI 维护命令（按需加入）
learning-submodules/    # 只读学习参考
```

`learning-submodules/` 内的仓库均为只读学习材料：它们不是运行时依赖，且产品代码不得 import 它们。参考其设计时，会记录借鉴点以及 Leonervis Code 的采用或差异。
