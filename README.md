<div align="center">

<img src="./docs/assets/leo-mark.png" alt="LEO mark" width="240">

# Leonervis Code

[English](./README_en.md) | 中文

[![Python](https://img.shields.io/badge/Python-3.12%E2%80%933.13-3776AB?logo=python&logoColor=white)](./pyproject.toml)
[![uv](https://img.shields.io/badge/uv-managed-DE5FE9?logo=uv&logoColor=white)](https://docs.astral.sh/uv/)
[![pytest](https://img.shields.io/badge/pytest-8%2B-0A9EDC?logo=pytest&logoColor=white)](./pyproject.toml)
[![Ruff](https://img.shields.io/badge/Ruff-0.9%2B-D7FF64?logo=ruff&logoColor=black)](./pyproject.toml)

</div>

Leonervis Code 是一个面向本地单用户使用、以学习为先的 Coding Agent CLI 原型。模型负责决策，Host 在明确的 workspace 边界内执行受控工具，并把结构化结果写回模型。

> **当前状态：** 已支持命名 provider profile、真实/离线 runtime、可恢复 Session、受限 `read_file` 工具循环、provider-owned 模型限制，以及每次 provider invocation 前的 target-specific request counting/preflight。尚未实现 compact、写工具、Bash 或审批流程。

## 目录

- [快速开始](#快速开始)
- [主要命令](#主要命令)
  - [执行任务与启动 REPL](#执行任务与启动-repl)
  - [配置 Provider](#配置-provider)
  - [检查 Route 与 Context Window](#检查-route-与-context-window)
  - [管理 Session](#管理-session)
  - [REPL 命令](#repl-命令)
- [配置与本地状态](#配置与本地状态)
- [开发与验证](#开发与验证)
- [详细文档](#详细文档)
- [当前范围与下一步](#当前范围与下一步)

## 快速开始

要求 Python 3.12 或 3.13、最新稳定版 [uv](https://docs.astral.sh/uv/) 和 Git。项目使用 `uv.lock` 管理可复现环境。

```bash
cd leonervis-code
uv sync
uv run leonervis-code
```

裸命令会在真实终端中启动 REPL。未选择真实 provider 时使用确定性的 fake provider，不访问网络：

```text
leonervis[3fe4bb27|fake]>
```

正式命令为 `leonervis-code`，`leonervis` 是简写；也可使用模块入口：

```bash
uv run leonervis --version
uv run python -m leonervis_code --help
```

## 主要命令

完整参数始终以命令自身帮助为准：

```bash
uv run leonervis-code --help
uv run leonervis-code provider --help
uv run leonervis-code session --help
```

### 执行任务与启动 REPL

| 用途 | 命令 |
| --- | --- |
| 启动新 Session 的 REPL | `uv run leonervis-code` |
| 恢复当前 workspace 的最新 Session | `uv run leonervis-code --resume latest` |
| 执行一次 prompt | `uv run leonervis-code prompt "解释这个 workspace"` |
| 在指定 workspace 执行 | `uv run leonervis-code -C ../project prompt "解释项目结构"` |
| 使用命名 profile | `uv run leonervis-code --profile work prompt "解释 README"` |
| 临时覆盖 profile 的 model | `uv run leonervis-code --profile work --model model-v2 prompt "继续"` |
| 使用直接 model route | `uv run leonervis-code --model anthropic/claude-opus-4-8 prompt "解释 README"` |
| 查看版本 | `uv run leonervis-code --version` |

`prompt` 用于脚本和一次性任务；裸命令用于有状态多轮 REPL。成功 turn 会自动保存到 workspace Session transcript。

### 配置 Provider

内置 provider 使用 catalog 中的 protocol、默认 endpoint 和 credential 环境变量名：

```bash
export ANTHROPIC_API_KEY='...'
uv run leonervis-code provider add work \
  --provider anthropic \
  --model claude-opus-4-8
```

自定义 OpenAI-compatible endpoint 必须显式给出 protocol 和 base URL。Profile 只保存 credential 的环境变量名，不保存 key value：

```bash
export VENDOR_API_KEY='...'
uv run leonervis-code provider add vendor \
  --provider custom \
  --model vendor/model \
  --protocol openai-compatible \
  --base-url https://gateway.example/v1 \
  --api-key-env VENDOR_API_KEY \
  --context-window-tokens 1000000
```

常用 profile 管理命令：

```bash
uv run leonervis-code provider list
uv run leonervis-code provider show vendor
uv run leonervis-code provider use vendor              # workspace scope
uv run leonervis-code provider use vendor --scope user
uv run leonervis-code provider clear --scope project
uv run leonervis-code provider rename vendor vendor-new --if-revision 1
uv run leonervis-code provider remove vendor-new
uv run leonervis-code provider migrate
```

选择优先级为：显式 `--profile` → 显式 direct `--model` → workspace active → user active → fake/offline。`provider use` 会在候选 route、credential 和 client 准备成功后才原子切换；失败时保留旧配置与旧 client。

### 检查 Route 与 Context Window

`route` 是离线诊断命令：不构造 provider client，不读取 key value，也不发起网络请求。

```bash
uv run leonervis-code --profile vendor route
uv run leonervis-code --model openai/gpt-5 route
```

命名 profile 可为 exact endpoint/model 配置上下文窗口：

```bash
uv run leonervis-code provider replace vendor \
  --provider custom \
  --model vendor/model \
  --protocol openai-compatible \
  --base-url https://gateway.example/v1 \
  --api-key-env VENDOR_API_KEY \
  --context-window-tokens 1000000 \
  --if-revision 1

uv run leonervis-code provider show vendor
uv run leonervis-code --profile vendor route
```

Runtime 按字段独立解析 context window 与 model max output：profile exact override → exact built-in catalog → fresh private discovery cache → provider-owned live discovery → `unknown`。每次 provider invocation（包括工具 continuation）都会用当前 requested output reserve 做 preflight；Anthropic official endpoint 优先 exact count，OpenAI-compatible 使用明确标记的 deterministic estimate，unknown 时不猜测并允许 provider 最终裁决。该能力不会自动 compact。

### 管理 Session

```bash
uv run leonervis-code prompt "第一轮"
uv run leonervis-code session list
uv run leonervis-code session show latest
uv run leonervis-code --resume latest prompt "继续上一轮"
uv run leonervis-code --resume <session-uuid>
```

Session 绑定 workspace，并使用 append-only JSONL 保存完整成功 turn。恢复 Session 只恢复历史；当前 provider 仍由本次 CLI selector 或 active profile 决定。

### REPL 命令

| 命令 | 作用 |
| --- | --- |
| `/help` | 查看控制命令 |
| `/history <count>` | 显示当前 Session 最近的完整回合 |
| `/status` | 显示脱敏 runtime、model 和 context-window 状态 |
| `/provider list` | 列出命名 profile |
| `/provider current` | 显示当前 profile/provider/model |
| `/provider use <name>` | 为当前 workspace 原子切换 active profile |
| `/model <model>` | 仅覆盖当前进程 model，不修改 profile |
| `/session show` | 显示当前 Session |
| `/session list` | 列出 workspace Session |
| `/session new` | 保持当前 runtime，开始空白 Session |
| `/resume <latest\|id>` | 保持当前 runtime，切换 Session |
| `/exit`、`/quit` | 正常退出 |

Ctrl-D、EOF 或在等待输入时按 Ctrl-C 也会正常退出。终端颜色只在 TTY 中启用；设置 `NO_COLOR=1` 可关闭。

用于观察受限工具循环的确定性演示命令：

```bash
uv run leonervis-code demo-read README.md
uv run leonervis-code demo-read ../outside.txt   # 验证 workspace 逃逸拒绝
```

`demo-read` 不是实际模型接口，不写文件、不执行 shell，也不访问网络。

## 配置与本地状态

| 路径 | 内容 |
| --- | --- |
| `${XDG_CONFIG_HOME:-~/.config}/leonervis-code/providers.json` | user provider profiles 与 active selection |
| `<workspace>/.leonervis-code/provider.json` | workspace active profile |
| `<workspace>/.leonervis-code/sessions/.../*.jsonl` | Session transcript |
| `${XDG_CACHE_HOME:-~/.cache}/leonervis-code/model-context-capabilities.json` | private context capability discovery cache |

`.leonervis-code/` 可能包含用户输入、模型回答、源码片段和工具结果，应加入目标项目的 `.gitignore`，不要提交、同步或公开。配置和 capability cache 不保存已知 credential value，但系统无法识别用户文本或源码中自行出现的未知 secret。

## 开发与验证

```bash
uv run pytest
uv run ruff check .
uv run ruff format --check .
uv lock --check
git diff --check
```

依赖变化后先执行 `uv lock`，再检查锁文件。Leonervis Code 不为目标 workspace 安装 Node、Rust、Java、Docker、数据库等项目环境。

## 详细文档

- [已实现 Foundation 与设计演进](./docs/implemented-foundations.md)：system prompt、工具循环、route policy、多 provider runtime、profile、Session 和 context capability 的集中说明。
- [架构决策记录](./docs/decisions/)：每个学习切片的完整问题、取舍、边界与验证记录。
- [Target-specific request counting 与 preflight](./docs/decisions/0014-target-specific-request-counting-and-preflight.md)：每次 provider invocation 的 native input 计量、两类限制与 typed local rejection。
- [Provider-owned model context capability](./docs/decisions/0013-provider-owned-model-context-capabilities.md)：context/model-output limit 解析与缓存设计。
- [Canonical model system prompt](./docs/decisions/0012-first-canonical-model-system-prompt.md)：模型可见契约、版本和 fingerprint。
- [Stable profile identity and durable Sessions](./docs/decisions/0010-foundation-3d-stable-profile-identity-and-durable-sessions.md)：profile UUID/revision 与 Session 持久化。
- [Claw-Code prompt 学习入口](./docs/references/claw-code-prompts/README.md)：只读参考结构与 Leonervis 的采用差异。
- [Harness-study](https://github.com/TsingFengIceberg/Harness-study)：相关 Harness 阅读与学习笔记。

## 当前范围与下一步

当前仅提供一个 workspace-bound `read_file` 工具；尚无写/编辑、glob/grep、Bash/test、网络工具、审批、streaming、自动 retry/fallback、并行工具、compact、多 Agent 或远程服务。

下一切片计划实现 target-aware switch UX：在切换 provider/model 前复用当前 counter/fit report 检查目标容量；之后再进入 durable effective context 与 controlled compact。完整范围、开发原则和路线记录在 [CLAUDE.md](./CLAUDE.md) 与各 ADR 中。
