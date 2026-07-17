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

> **当前状态：Foundation 3D 的稳定 Profile Identity 与可恢复 Session 已完成。** Provider profile 现在具有不可变 UUID、revision 和配置/route fingerprint，旧 schema v1 可安全读取并按写入范围升级。每个 workspace 的对话以 append-only JSONL 自动保存，完整 turn 在 fsync 成功后才进入内存历史，支持跨进程 `--resume`、Session 查看/列举和 REPL 内 `/resume`。Session 只记录每个历史 turn 的实际 provider provenance；当前运行 provider 始终由本次 CLI/active 配置选择，不会被历史 Session 强绑定。两个真实 SDK 仍固定 `max_retries=0`。

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

启动后会显示彩色 LEO 标志、版本、当前 workspace、脱敏 runtime 状态、稳定 Session ID、transcript 路径和自动保存状态，然后显示动态 prompt：

```text
leonervis[3fe4bb27|fake]>
leonervis[3fe4bb27|deepseek-test]>
leonervis[3fe4bb27|direct:openai]>
```

方括号内第一项是当前 Session UUID 的前 8 位，只用于视觉辨识；第二项是当前 runtime identity：命名 profile 名、`direct:<provider>` 或 `fake`。model、workspace 和 turn 数不会塞进 prompt；完整信息由 `/status`、`/provider current` 和 `/session show` 查看。动态字段会做终端安全投影和定长截断，短 ID 不能替代完整 Session selector。

输入任意非空文本可得到确定性结果：

```text
leonervis> 解释 Harness 边界
Fake response: 解释 Harness 边界
```

REPL 内提供持久历史、Session 和 provider runtime 控制命令。输入 `/session` 或 `/provider` 可查看对应命令组帮助：

```text
/help                         查看控制说明
/history <count>              显示当前 Session 最近 count 个完整回合
/session show                 显示当前 Session ID、路径和 turn 数
/session list                 列出 Session，并标记 current/latest 与 open/closed
/session new                  保持当前 runtime provider，开始空白 Session
/resume <latest|id>           切换 Session，保持当前 runtime provider 不变
/status                       显示脱敏后的当前 runtime 状态
/provider list                列出命名 profile
/provider current             显示当前 profile/provider/model
/provider use <name>          为当前 workspace 切换并持久化 active profile
/model <model>                仅覆盖当前进程模型，不修改 profile
/exit 或 /quit                正常退出
Ctrl-D / EOF / Ctrl-C         正常退出
```

终端语义提示采用传统颜色：绿色表示成功，黄色表示 usage、warning 或 fake/offline，红色表示失败，蓝色表示普通信息和 real runtime/Session 上下文；模型最终回答保持原样。颜色只在 TTY 中启用，设置 `NO_COLOR=1` 可完全关闭。

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

## Foundation 3D：稳定 Profile Identity 与可恢复 Session

profile registry schema v2 使用不可变 UUID 作为引用身份，名称只作为可读、可修改的别名；revision 用于更新冲突检查。旧 schema v1 profile 会由原始名称确定性映射到 UUID，reader 支持 user/project v1/v2 混合状态，写操作只升级实际写入的文件：

```bash
uv run leonervis-code provider show vendor       # 显示 profile ID 与 revision
uv run leonervis-code provider list --show-ids
uv run leonervis-code provider rename vendor vendor-new --if-revision 1
uv run leonervis-code provider replace vendor-new \
  --provider custom --model vendor/model-v2 \
  --protocol openai-compatible --base-url https://gateway.example/v1 \
  --if-revision 2
uv run leonervis-code provider migrate
```

每次 `prompt` 或 REPL 会创建/打开：

```text
<workspace>/.leonervis-code/sessions/<workspace-fingerprint>/<session-id>.jsonl
```

Session 采用 append-only JSONL；成功 turn 的 user、tool use/result 和最终 assistant text 作为一条完整 commit record写入并fsync，成功后才更新内存历史。每个打开的Session持有独占writer lock；损坏的中间record、未知schema和错误tool pairing都fail closed，只有进程崩溃形成的无换行不完整尾部可以受控截断并写 recovery record。

```bash
uv run leonervis-code prompt "第一轮"
uv run leonervis-code session list
uv run leonervis-code session show latest
uv run leonervis-code --resume latest prompt "继续上一轮"
uv run leonervis-code -C ../another-workspace --resume latest
```

日常使用时，裸启动会创建新 Session，`--resume latest` 会继续该 workspace 的 latest 指针；`session list` 和 `session show latest` 用于查找与检查历史。在 REPL 中，`/session new` 会保留当前 runtime provider 并开始空白历史，`/resume <id>` 则切换到已有历史。列表中的 `[current]` 表示下一条 REPL prompt 的写入目标，`[latest]` 表示 `latest.json` 当前指向；`open/closed` 是 transcript 生命周期记录而不是锁状态，closed Session 仍可恢复。

Session 与 runtime provider 解耦：transcript 记录每个历史 turn 当时实际使用的 profile ID/revision、provider/protocol、model、endpoint和非敏感fingerprint，仅供审计。恢复后真实工作的provider继续由本次`--profile`/`--model`、workspace active、user active或fake fallback决定；不会按历史binding重建client，也不会因profile后来改名、修改或删除而阻止恢复。把旧历史发送给新的当前provider属于显式运行选择，当前adapter若拒绝该历史，失败turn不会提交。

本地Session可能包含用户输入、模型回答、源码片段和工具结果，属于敏感运行状态；`.leonervis-code/`不应提交、同步或公开。系统保证已知配置credential value不会作为binding写入，但无法通用识别用户文本或被读取文件中自行包含的未知secret。

`ProjectSession` 现在提供 `session_id`、`transcript_path`、`session_info()`、`list_sessions()`、`new_session()`、`switch_session()` 和 `resume=`；Session切换只替换durable history，保持当前provider client。

## Foundation 3C：命名 Provider Profile 与真实多轮 REPL

profile 定义保存在 `${XDG_CONFIG_HOME:-~/.config}/leonervis-code/providers.json`；workspace 只在 `.leonervis-code/provider.json` 保存 active profile ID。两个 JSON 都不保存 key value；项目目录是本地运行状态，应加入目标项目的 `.gitignore`。

```bash
# 内置 provider：protocol、默认 endpoint 与默认 credential env 由 catalog 提供
uv run leonervis-code provider add work-openai \
  --provider openai --model gpt-5

# 受控 custom OpenAI-compatible endpoint：只保存 key 的环境变量名
uv run leonervis-code provider add local-qwen \
  --provider custom \
  --model Qwen/Qwen3.5 \
  --protocol openai-compatible \
  --base-url http://127.0.0.1:11434

uv run leonervis-code provider add vendor \
  --provider custom \
  --model vendor/model \
  --protocol openai-compatible \
  --base-url https://gateway.example/v1 \
  --api-key-env VENDOR_API_KEY

uv run leonervis-code provider list
uv run leonervis-code provider show vendor
uv run leonervis-code provider use local-qwen              # 默认 workspace scope
uv run leonervis-code provider use work-openai --scope user
uv run leonervis-code provider clear --scope project
uv run leonervis-code provider remove vendor
```

选择优先级是：显式 `--profile` > 显式 direct `--model` > workspace active > user active > fake/offline。`--profile NAME --model MODEL` 在该 endpoint 上使用当前进程的 model override，不改写 profile：

```bash
uv run leonervis-code --profile work-openai --model gpt-5-mini prompt "解释这个 workspace"
uv run leonervis-code --profile work-openai       # 真实多轮 REPL，client 跨 turn 复用
```

`provider use` 和 REPL 的 `/provider use` 都先解析 route、检查 credential、构造候选 SDK client，再写 active 配置并交换当前 client；失败时旧 active 和旧 client 不变。`/model` 同样只在两个 turn 之间原子切换。完整 neutral history 与 tool use/result 配对跨 provider 保留；新 provider 若拒绝旧历史，失败 turn 不会提交。

项目其他模块可直接使用公开 facade：

```python
from pathlib import Path
from leonervis_code import ProjectSession

with ProjectSession.open(Path.cwd(), profile="work-openai") as session:
    first = session.prompt("先解释 README")
    session.set_model("gpt-5-mini")
    second = session.prompt("继续")
```

`ProjectSession` 还提供 `list_profiles()`、`use_profile()`、`use_profile_id()`、`clear_active()`、`status()`、`history` 与 `turns`。Foundation 3D 在此 facade 上增加稳定Session identity、JSONL自动保存和resume。

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

显式 provider namespace 优先；只有已登记的 `claude-*`、`gpt-*`、`grok-*`、`qwen-*`、`kimi-*` bare family 会被确定性识别，未知 bare model 不依据现有 credential 猜测。route 和 adapter config 不保存 secret value；key 只在 factory 构造所选 SDK client 时读取。当前不读取 `.env`、OAuth 或 keyring，也不实现 streaming、自动 retry/backoff、fallback execution、live discovery、并行工具或跨 workspace Session 恢复。

真实 route 可在不构造 client、不中断网络的情况下预览：

```bash
uv run leonervis-code --model openai/gpt-5 route
```

默认 fake fallback 保持不变；但已设置 workspace/user active profile 时，未带显式 selector 的 `prompt` 与裸 REPL 会使用该真实 profile：

```bash
uv run leonervis-code provider clear --scope project
uv run leonervis-code provider clear --scope user
uv run leonervis-code prompt "Hello"   # 无 active 时 fake，不联网
uv run leonervis-code                   # 无 active 时 fake REPL，不联网
uv run leonervis-code route             # Foundation 2B fake policy preview，不联网
```

详细边界见 [Foundation 3A Anthropic adapter 决策](./docs/decisions/0007-foundation-3a-anthropic-non-streaming-adapter.md)、[Foundation 3B 多 Provider runtime 决策](./docs/decisions/0008-foundation-3b-local-multi-provider-runtime.md) 与 [Foundation 3C 命名 profile/常驻 runtime 决策](./docs/decisions/0009-foundation-3c-named-provider-profiles-and-runtime-manager.md)。真实 smoke test 只应在用户明确愿意使用自己的 credential、endpoint 和 API 费用时手动运行。

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

默认 `ScriptedFakeProvider` 保持可见的回显行为，且不会自行请求工具；它的script形式在测试中为工具循环提供确定性证明，而`demo-read <path>`将同一套固定scripted链路公开为可手动验证的终端入口。`prompt`仍是一次性命令，但每次成功turn都会自动保存；同一个运行中的REPL里，`/history <count>`只显示当前Session已完成的user/final assistant对，不显示内部工具数据。

Foundation 1B 原始切片只验证了进程内原子历史；Foundation 3D 现在将完整turn持久化到workspace内JSONL。若在非交互终端中直接运行`leonervis-code`，程序仍会提示改用`leonervis-code prompt "..."`并以非零状态退出，避免管道或CI意外卡住。

详细学习设计记录见：[单轮 Loop 决策](./docs/decisions/0001-foundation-0-single-turn-loop.md)、[确定性 REPL 决策](./docs/decisions/0002-foundation-0-deterministic-repl.md)、[内存文本历史决策](./docs/decisions/0003-foundation-1a-in-memory-text-history.md)、[受限 read-file 工具循环决策](./docs/decisions/0004-foundation-1b-bounded-read-file-tool-loop.md)、[provider-neutral 模型路由决策](./docs/decisions/0005-foundation-2a-provider-neutral-model-routing.md)、[adapter-owned compatibility policy 决策](./docs/decisions/0006-foundation-2b-adapter-owned-compatibility-policy.md)、[Anthropic 非流式 adapter 决策](./docs/decisions/0007-foundation-3a-anthropic-non-streaming-adapter.md)、[本地多 Provider runtime 决策](./docs/decisions/0008-foundation-3b-local-multi-provider-runtime.md)、[命名 provider profile/常驻 runtime 决策](./docs/decisions/0009-foundation-3c-named-provider-profiles-and-runtime-manager.md)、[稳定 profile/可恢复 Session 决策](./docs/decisions/0010-foundation-3d-stable-profile-identity-and-durable-sessions.md) 与 [解耦 REPL 展示/命令分发决策](./docs/decisions/0011-decoupled-repl-presentation-and-slash-dispatch.md)。

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
- 结构化的 `UserMessage` / `AssistantText` / `ToolUse` / `ToolResult` contract、确定性 scripted fake provider、携带原子内存因果历史的 `AgentLoop`、一个受限的 `read_file` 工具、Foundation 2B 离线 route policy，以及支持 Anthropic 与 OpenAI-compatible family 的本地多 Provider runtime；
- 不含credential value且具有stable UUID/revision的命名provider profiles、v1/v2兼容迁移、user/project active precedence、turn间原子client/model切换，以及供其他模块使用的`ProjectSession` API；
- workspace-bound UUID Session、append-only JSONL、完整工具因果链、single-writer lock、tail recovery、跨进程resume和每turn provider provenance；
- 具有彩色启动标志、Tab补全、`/history`、`/session`、`/resume`、provider/status/model控制和持久完整回合历史的本地真实/fake REPL；
- 可通过 `prompt` 命令端到端运行的自动化友好路径；
- `pytest` 与 `ruff` 的基础质量工具链。

下一切片建议在Foundation 3D的稳定Session基础上进入context预算与受控compact，或转向文件写工具与PermissionGate。Streaming、自动retry/fallback、审批和受控Bash仍需各自的学习切片。

MCP、插件、远程/服务端形态、多 Agent、RAG、后台任务等并非被永久排除，但只有出现明确问题、设计边界和测试方案后才会引入。

## 仓库结构

```text
src/leonervis_code/
  core/                 # 中立的 conversation/tool 与 model-orchestration contracts
  agent/                # 维护受限因果历史与工具决策的 AgentLoop
  tools/                # 当前仅有 workspace 受限的 read_file 工具
  providers/            # adapter、route/factory、命名 profile store 与 runtime manager
  session.py            # 组合runtime与可切换durable Session的ProjectSession facade
  session_records.py    # closed JSONL record schema与因果replay验证
  session_store.py      # workspace路径、writer lock、append/latest与恢复
  cli/                  # 命令解析、profile 管理、品牌渲染、REPL 与终端输出
tests/                  # 单元、集成、安全与端到端测试将逐步进入这里
docs/                   # 架构决策、学习笔记与安全设计
scripts/                # 可复现的本地/CI 维护命令（按需加入）
learning-submodules/    # 只读学习参考
```

`learning-submodules/` 内的仓库均为只读学习材料：它们不是运行时依赖，且产品代码不得 import 它们。参考其设计时，会记录借鉴点以及 Leonervis Code 的采用或差异。
