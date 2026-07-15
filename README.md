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

> **当前状态：环境引导完成，Harness 运行时尚未实现。** 现在可以安装项目、检查命令与运行基础质量检查；尚不能执行真实的 Agent 任务。

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

# 3. 查看当前可用的 bootstrap CLI
uv run leonervis-code --help
uv run leonervis --version
```

两个命令名指向同一个入口：`leonervis-code` 是正式命令，`leonervis` 是简写。也可以使用模块入口：

```bash
uv run python -m leonervis_code --help
```

目前直接运行而不带 `--help` 或 `--version` 会明确报告 Harness 运行时尚未实现，并以非零状态退出；这不是可用 Agent 的伪装入口。

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
- `pytest` 与 `ruff` 的基础质量工具链；
- 一个明确表明运行时尚未实现的 bootstrap CLI。

接下来的第一个实现切片会建立最小、可测试的 Harness 基础：核心 contracts、确定性测试替身、受限 Agent loop 和最小 CLI。模型接入、文件工具、写入审批、session 与受控 Bash 等能力都会在各自的学习切片中再加入。

MCP、插件、远程/服务端形态、多 Agent、RAG、后台任务等并非被永久排除，但只有出现明确问题、设计边界和测试方案后才会引入。

## 仓库结构

```text
src/leonervis_code/    # 唯一的生产 Python 包；目前仅有 bootstrap CLI
tests/                 # 单元、集成、安全与端到端测试将逐步进入这里
docs/                  # 架构决策、学习笔记与安全设计
scripts/               # 可复现的本地/CI 维护命令（按需加入）
learning-submodules/   # 只读学习参考
```

`learning-submodules/` 内的仓库均为只读学习材料：它们不是运行时依赖，且产品代码不得 import 它们。参考其设计时，会记录借鉴点以及 Leonervis Code 的采用或差异。
