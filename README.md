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

> **当前状态：** 已支持命名 provider profile、真实/离线 runtime、可恢复 Session、受限 `read_file`/`glob` 顺序工具循环、provider-owned 模型限制、target-specific preflight、切换前 screening、provider-neutral Effective Context、手动且可恢复的 `/compact`、target-aware startup/REPL resume prepare/screen/commit，以及固定 80% high-water 与 known overflow 触发的 pre-turn automatic compact。尚未实现内容搜索、写工具、Bash 或审批流程。

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

Runtime 按字段独立解析 context window 与 model max output：profile exact override → exact built-in catalog → fresh private discovery cache → provider-owned live discovery → `unknown`。每次 provider invocation（包括工具 continuation）都会用当前 requested output reserve 做 preflight；Anthropic official endpoint 优先 exact count，OpenAI-compatible 使用明确标记的 deterministic estimate，unknown 时不猜测并允许 provider 最终裁决。普通 prompt 在 exact initial request 的 input + reserve 达到 known window 的 80% 时最多 proactive compact 一次，在 known context overflow 时最多 mandatory compact 一次；unknown不触发，model-output overflow直接拒绝，成功compact后的真实invocation仍执行完整preflight。REPL 的 `/provider use` 与 `/model` 还会在提交切换前计量当前已提交历史：known overflow 保留旧 runtime/selection，unknown 则以 warning 允许切换；下一次真实 invocation 仍执行完整 preflight。

### 管理 Session

```bash
uv run leonervis-code prompt "第一轮"
uv run leonervis-code session list
uv run leonervis-code session show latest
uv run leonervis-code --resume latest prompt "继续上一轮"
uv run leonervis-code --resume <session-uuid>
```

Session 绑定 workspace，并使用 append-only JSONL 保存完整成功 turn。恢复 Session 只恢复历史；当前 provider 仍由本次 CLI selector 或 active profile 决定。Startup `--resume` 与 REPL `/resume` 会先以只读独占 lease 重放目标，并用当前 runtime 检查目标 Effective Context；known context/model-output overflow 在写入 `SessionResumed` 或更新 `latest.json` 前拒绝，`UNKNOWN` 以 warning fail open，fake runtime 明确不执行 provider 请求。Compacted Session 按 Host summary + retained real-turn suffix 计量，而不是按完整 transcript。`/resume latest` 对准备期间的 pointer 变化执行 exact CAS；恢复当前 Session 是不写 record 的 no-op。下一次真实 invocation 仍执行完整 preflight。

### REPL 命令

| 命令 | 作用 |
| --- | --- |
| `/help` | 查看控制命令 |
| `/history <count>` | 显示当前 Session 最近的完整回合 |
| `/status` | 显示脱敏 runtime、model 和 context-window 状态 |
| `/context` | 只读检查当前 Effective Context、内容 ID、计数与 target fit |
| `/compact` | 使用当前真实 provider 手动总结较早完整回合并持久化 effective-context checkpoint |
| `/provider list` | 列出命名 profile |
| `/provider current` | 显示当前 profile/provider/model |
| `/provider use <name>` | 为当前 workspace 原子切换 active profile |
| `/model <model>` | 仅覆盖当前进程 model，不修改 profile |
| `/session show` | 显示当前 Session |
| `/session list` | 列出 workspace Session |
| `/session new` | 保持当前 runtime，开始空白 Session |
| `/resume <latest\|id>` | 保持当前 runtime，切换 Session |
| `/exit`、`/quit` | 正常退出 |

Ctrl-D、EOF 或在等待输入时按 Ctrl-C 也会正常退出。`/context` 不调用模型生成、不修改 Session，也不写 transcript；compact 后它会明确区分完整 transcript、summary、保留的 real turns与latest checkpoint trigger。`/compact` 只在至少 4 个完整 effective turns 时工作，固定保留最近 2 个 turns，使用当前真实 provider 发起一次不暴露工具的 summary 请求；成功时只 append+fsync typed checkpoint，完整 `/history` 不变。普通 one-shot/REPL prompt也复用同一transaction：exact initial request的input + reserve达到known window的80%时最多proactive compact一次，known context overflow时最多mandatory compact一次；pending user参与前后计量但不进入summary/checkpoint。Proactive安全precommit failure会warning后继续原known-fit turn，mandatory failure不发送普通generation；事件不显示pending或summary原文。Fake runtime、unknown/non-reducing candidate 或任何 precommit failure 都不会提交 compact。`/resume` screening 也不调用 generation/tool；known overflow 保持 current Session、runtime、latest 与 target transcript 不变，unknown/fake 则以明确 warning 应用恢复。Anthropic official route 的 exact inspection/compact/resume count 可能发起 count-only `messages.count_tokens` 请求，OpenAI-compatible 使用本地 estimate。终端颜色只在 TTY 中启用；设置 `NO_COLOR=1` 可关闭。

`read_file`与`glob`是当前模型可见的两个只读工具，共享每个user turn最多3次顺序执行预算。`glob`接受workspace-relative、`/`分隔pattern，支持component `*`、`?`、bracket class与whole-component `**`；只返回stable sorted的non-symlink regular-file paths，不读取内容、不跟随link，hidden component必须显式以`.`匹配。结果最多200项与32 KiB，超限时明确`[truncated]`；traversal/depth limit则返回安全error。它不读取`.gitignore`，也不等价于无界目录列表或内容搜索。

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

- [已实现 Foundation 与设计演进](./docs/implemented-foundations.md)：system prompt、工具循环、route policy、多 provider runtime、profile、Session、context capability与automatic context compaction的集中说明。
- [架构决策记录](./docs/decisions/)：每个学习切片的完整问题、取舍、边界与验证记录。
- [Bounded Workspace Glob](./docs/decisions/0020-foundation-1c-bounded-workspace-glob.md)：portable pattern、hidden/symlink policy、stable bounds、共享tool budget与schema-v1兼容seam。
- [Pre-turn Automatic Context Compaction](./docs/decisions/0019-pre-turn-automatic-context-compaction.md)：80% high-water、pending-turn隔离、一次尝试、共享runtime lease与schema-v3 trigger provenance。
- [Target-aware Resume Prepare/Commit](./docs/decisions/0018-target-aware-resume-prepare-commit.md)：只读prepare、当前runtime screening、exact stale/CAS与durable partial outcomes。
- [Controlled Compact Transaction](./docs/decisions/0017-controlled-compact-transaction.md)：manual `/compact`、no-tools summary、mixed Session schema 与 persist-before-memory 原子性。
- [Provider-neutral Effective Context Snapshot](./docs/decisions/0016-provider-neutral-effective-context-snapshot.md)：full/effective context边界、稳定 `ctx-v1` identity 与只读 `/context`。
- [Target-aware runtime switch UX](./docs/decisions/0015-target-aware-runtime-switch-ux.md)：切换前 committed-context screening、known reject/unknown allow 与原子审计语义。
- [Target-specific request counting 与 preflight](./docs/decisions/0014-target-specific-request-counting-and-preflight.md)：每次 provider invocation 的 native input 计量、两类限制与 typed local rejection。
- [Provider-owned model context capability](./docs/decisions/0013-provider-owned-model-context-capabilities.md)：context/model-output limit 解析与缓存设计。
- [Canonical model system prompt](./docs/decisions/0012-first-canonical-model-system-prompt.md)：模型可见契约、版本和 fingerprint。
- [Stable profile identity and durable Sessions](./docs/decisions/0010-foundation-3d-stable-profile-identity-and-durable-sessions.md)：profile UUID/revision 与 Session 持久化。
- [Claw-Code prompt 学习入口](./docs/references/claw-code-prompts/README.md)：只读参考结构与 Leonervis 的采用差异。
- [Harness-study](https://github.com/TsingFengIceberg/Harness-study)：相关 Harness 阅读与学习笔记。

## 当前范围与下一步

当前提供workspace-bound、只读且有界的`read_file`与`glob`；尚无内容搜索`grep`、写/编辑、Bash/test、网络工具、审批、streaming、自动retry/fallback、并行工具、多Agent或远程服务。

Foundation 1C已补齐最小文件发现能力：模型可通过portable bounded glob匹配候选regular files，再用`read_file`读取内容；两个工具共享三次顺序调用预算，并继续保留structured causality、Session replay与workspace硬边界。下一步建议把bounded content `grep`作为新的独立vertical slice，先定义line/result limits、encoding、binary与ignore语义；write/Bash/approval继续延后。完整范围、开发原则和路线记录在 [CLAUDE.md](./CLAUDE.md) 与各 ADR 中。
