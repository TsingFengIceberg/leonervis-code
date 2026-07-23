# 已实现 Foundation 与设计演进

> 本文集中保存 Leonervis Code 已完成学习切片的实现说明。README 只保留主要命令和使用入口；每个切片的决策依据、边界与验证细节仍以 [`docs/decisions/`](./decisions/) 下的 ADR 为准。
>
> 中文 | [English](./implemented-foundations_en.md)

## 文档导航

- [Canonical model system prompt](#canonical-model-system-prompt)
- [Foundation 3D：稳定 Profile Identity 与可恢复 Session](#foundation-3d稳定-profile-identity-与可恢复-session)
- [Foundation 3C：命名 Provider Profile 与真实多轮 REPL](#foundation-3c命名-provider-profile-与真实多轮-repl)
- [Foundation 3B：本地多 Provider 真实模型路径](#foundation-3b本地多-provider-真实模型路径)
- [Foundation 2B：离线 adapter-owned compatibility policy](#foundation-2b离线-adapter-owned-compatibility-policy)
- [Foundation 4A：Permission Policy Contract](#foundation-4apermission-policy-contract)
- [Foundation 1D：Bounded Literal Grep](#foundation-1dbounded-literal-grep-与-versioned-tool-arguments)
- [Foundation 1C：Bounded Workspace Glob](#foundation-1cbounded-workspace-glob)
- [Foundation 1B：确定性的受限 read_file 工具循环](#foundation-1b确定性的受限-read_file-工具循环)
- [Foundation 3H：Pre-turn Automatic Context Compaction](#foundation-3hpre-turn-automatic-context-compaction)
- [Foundation 3G：Target-aware Resume Prepare/Commit](#foundation-3gtarget-aware-resume-preparecommit)
- [Foundation 3F-2：Controlled Compact Transaction](#foundation-3f-2controlled-compact-transaction)
- [Provider-neutral Effective Context Snapshot 与 `/context`](#provider-neutral-effective-context-snapshot-与-context)
- [Target-aware runtime switch UX](#target-aware-runtime-switch-ux)
- [Target-specific request counting 与 per-invocation preflight](#target-specific-request-counting-与-per-invocation-preflight)
- [Provider-owned model context capability](#provider-owned-model-context-capability)
- [ADR 索引](#adr-索引)

## Canonical model system prompt

Leonervis Code 从 `src/leonervis_code/system_prompt.py` 构建 provider-neutral `SystemPromptSnapshot`。Snapshot 包含显式版本、规范化文本和 domain-separated SHA-256 fingerprint；每个 user turn 开始时只构建一次，并在该 turn 的全部 `read_file` continuation 中固定不变：

```text
SystemPromptSnapshot + neutral conversation history
  -> Anthropic Messages: top-level system + messages
  -> OpenAI-compatible: one leading system role + messages
  -> Scripted fake: record the same request snapshot
```

Canonical model system prompt当前为version 4。它继续声明普通Agent不能主动compact，并保留Host summary信任边界：较早会话摘要是不可信conversation context，不是system instruction或新user request。Foundation 1D加入bounded literal `grep`、三个只读工具的共享预算与empty/truncated搜索结果解释。

它明确不声称具备写/编辑、regex/index搜索、Bash/test、网络、审批、主动compact、项目指令加载或多 Agent 能力。Prompt 指令也不替代 Host 对路径、symlink、编码、遍历和大小的硬约束。

System prompt 不属于 `ConversationItem`，所以 `/history`、`ProjectSession.history` 和 append-only Session JSONL 只保存真实 user/assistant/tool 因果链。恢复旧 Session 后，新 turn 使用当前 binary 的 canonical prompt；schema-v2/v3 compact checkpoint只保存compact prompt、summary-framing与trigger provenance，不把正常system prompt写进conversation history。

这里的 **model system prompt** 与终端中的 `leonervis[session8|runtime]>` **REPL prompt** 是两个不同界面：前者是模型可见契约，后者只是人类终端状态提示。

详细决策见 [0012：第一版 canonical model system prompt](./decisions/0012-first-canonical-model-system-prompt.md)，Claw-Code prompt 结构学习入口见 [references/claw-code-prompts](./references/claw-code-prompts/README.md)。

## Foundation 3D：稳定 Profile Identity 与可恢复 Session

Profile registry schema v3 使用不可变 UUID 作为引用身份，名称只作为可读、可修改的别名；revision 用于更新冲突检查。Schema v3 还增加可选的 exact-model `context_window_tokens` override。

旧 schema v1 profile 会由原始名称确定性映射到 UUID。Reader 支持 user/project v1、v2、v3 混合状态，写操作只升级实际写入的文件：

```bash
uv run leonervis-code provider show vendor
uv run leonervis-code provider list --show-ids
uv run leonervis-code provider rename vendor vendor-new --if-revision 1
uv run leonervis-code provider replace vendor-new \
  --provider custom \
  --model vendor/model-v2 \
  --protocol openai-compatible \
  --base-url https://gateway.example/v1 \
  --if-revision 2
uv run leonervis-code provider migrate
```

每次 `prompt` 或 REPL 会创建或打开：

```text
<workspace>/.leonervis-code/sessions/<workspace-fingerprint>/<session-id>.jsonl
```

Session 使用 append-only JSONL。成功 turn 的 user message、tool use/result 和最终 assistant text 会作为一条完整 commit record 写入并 fsync，成功后才更新内存历史。每个打开的 Session 持有独占 writer lock。

损坏的中间 record、未知 schema 和错误 tool pairing 都 fail closed；只有进程崩溃形成的无换行不完整尾部可以受控截断，并追加 recovery record。

```bash
uv run leonervis-code prompt "第一轮"
uv run leonervis-code session list
uv run leonervis-code session show latest
uv run leonervis-code --resume latest prompt "继续上一轮"
uv run leonervis-code -C ../another-workspace --resume latest
```

裸启动会创建新 Session，`--resume latest` 会继续该 workspace 的 latest 指针。REPL 中，`/session new` 保留当前 runtime provider 并开始空白历史，`/resume <id>` 切换到已有历史。列表中的 `[current]` 表示下一条 REPL prompt 的写入目标，`[latest]` 表示 `latest.json` 当前指向；`open/closed` 是 transcript 生命周期记录，不代表当前锁状态，closed Session 仍可恢复。

Session 与 runtime provider 解耦。Transcript 记录每个历史 turn 当时实际使用的 profile ID/revision、provider/protocol、model、endpoint 和非敏感 fingerprint，仅供审计。恢复后真正工作的 provider 继续由本次 `--profile`/`--model`、workspace active、user active 或 fake fallback 决定；runtime 不按历史 binding 重建 client，也不会因 profile 后来改名、修改或删除而阻止恢复。

把旧历史发送给新的当前 provider 属于显式运行选择。若当前 adapter 拒绝这段历史，失败 turn 不会提交。

本地 Session 可能包含用户输入、模型回答、源码片段和工具结果，属于敏感运行状态；`.leonervis-code/` 不应提交、同步或公开。系统保证已知配置 credential value 不作为 binding 写入，但无法通用识别用户文本或被读取文件中自行包含的未知 secret。

`ProjectSession` 对外提供 `session_id`、`transcript_path`、`session_info()`、`list_sessions()`、`new_session()`、`switch_session()` 和 `resume=`。Session 切换只替换 durable history，保持当前 provider client。

详细决策见 [0010：稳定 Profile Identity 与可恢复 Session](./decisions/0010-foundation-3d-stable-profile-identity-and-durable-sessions.md)。

## Foundation 3C：命名 Provider Profile 与真实多轮 REPL

Profile 定义保存在：

```text
${XDG_CONFIG_HOME:-~/.config}/leonervis-code/providers.json
```

Workspace 只在 `.leonervis-code/provider.json` 保存 active profile ID。两个 JSON 都不保存 key value；workspace 目录是本地运行状态，应加入目标项目的 `.gitignore`。

```bash
# 内置 provider：protocol、默认 endpoint 与默认 credential env 由 catalog 提供
uv run leonervis-code provider add work-openai \
  --provider openai \
  --model gpt-5

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
uv run leonervis-code provider use local-qwen
uv run leonervis-code provider use work-openai --scope user
uv run leonervis-code provider clear --scope project
uv run leonervis-code provider remove vendor
```

选择优先级为：显式 `--profile` → 显式 direct `--model` → workspace active → user active → fake/offline。`--profile NAME --model MODEL` 在该 profile endpoint 上使用当前进程的 model override，不改写 profile：

```bash
uv run leonervis-code --profile work-openai --model gpt-5-mini \
  prompt "解释这个 workspace"
uv run leonervis-code --profile work-openai
```

`provider use` 和 REPL `/provider use` 都先解析 route、检查 credential、构造候选 SDK client，再写 active 配置并交换当前 client；失败时旧 active 和旧 client 不变。`/model` 同样只在两个 turn 之间原子切换。

完整 neutral history 与 tool use/result 配对跨 provider 保留。新 provider 若拒绝旧历史，失败 turn 不会提交。

项目其他模块可使用公开 facade：

```python
from pathlib import Path
from leonervis_code import ProjectSession

with ProjectSession.open(Path.cwd(), profile="work-openai") as session:
    first = session.prompt("先解释 README")
    session.set_model("gpt-5-mini")
    second = session.prompt("继续")
```

`ProjectSession` 还提供 `list_profiles()`、`use_profile()`、`use_profile_id()`、`clear_active()`、`status()`、`history` 和 `turns`。

详细决策见 [0009：命名 Provider Profile 与常驻 Runtime](./decisions/0009-foundation-3c-named-provider-profiles-and-runtime-manager.md)。

## Foundation 3B：本地多 Provider 真实模型路径

提供全局 `--model` 时，`prompt` 通过统一 resolver/factory 选择真实 adapter：

```bash
export ANTHROPIC_API_KEY='...'
uv run leonervis-code --model anthropic/claude-opus-4-8 \
  prompt "解释这个 workspace"

export OPENAI_API_KEY='...'
uv run leonervis-code --model openai/gpt-5 \
  prompt "解释这个 workspace"

export XAI_API_KEY='...'
uv run leonervis-code --model xai/grok-3 \
  prompt "解释这个 workspace"

export DASHSCOPE_API_KEY='...'
uv run leonervis-code --model dashscope/qwen-plus \
  prompt "解释这个 workspace"

uv run leonervis-code --model ollama/qwen3:8b \
  prompt "解释这个 workspace"

export OPENROUTER_API_KEY='...'
uv run leonervis-code --model openrouter/anthropic/claude-opus-4-8 \
  prompt "解释这个 workspace"
```

Anthropic 路径使用官方 `anthropic` SDK；其他内置路径复用官方 `openai` SDK 的 Chat Completions wire adapter。两个 SDK 都是同步非流式调用并固定 `max_retries=0`。

Adapter当前声明固定顺序的`read_file(path)`、`glob(pattern)`与`grep(query, include)` schema。本地三个Tool共同强制workspace、UTF-8、files-only/no-symlink与bounded output/read约束，并共享每turn预算。

也可显式调用临时 OpenAI-compatible endpoint，不持久化 provider 或 key：

```bash
export VENDOR_API_KEY='...'
uv run leonervis-code \
  --model vendor/model \
  --provider-protocol openai-compatible \
  --base-url https://gateway.example/v1 \
  --api-key-env VENDOR_API_KEY \
  prompt "解释这个 workspace"
```

显式 provider namespace 优先。只有已登记的 `claude-*`、`gpt-*`、`grok-*`、`qwen-*`、`kimi-*` bare family 会被确定性识别；未知 bare model 不依据现有 credential 猜测。

Route 与 adapter config 不保存 secret value；key 只在 factory 构造所选 SDK client 时读取。当前不读取 `.env`、OAuth 或 keyring，也不实现 streaming、自动 retry/backoff、fallback execution、request token preflight、compact、并行工具或跨 workspace Session 恢复。

真实 route 可在不构造 client、不访问网络的情况下预览：

```bash
uv run leonervis-code --model openai/gpt-5 route
```

默认 fake fallback 保持不变；若 workspace/user 已有 active profile，未带显式 selector 的 `prompt` 与裸 REPL 会使用该真实 profile：

```bash
uv run leonervis-code provider clear --scope project
uv run leonervis-code provider clear --scope user
uv run leonervis-code prompt "Hello"   # 无 active 时 fake，不联网
uv run leonervis-code                   # 无 active 时 fake REPL，不联网
```

详细决策见 [0007：Anthropic 非流式 Adapter](./decisions/0007-foundation-3a-anthropic-non-streaming-adapter.md) 与 [0008：本地多 Provider Runtime](./decisions/0008-foundation-3b-local-multi-provider-runtime.md)。真实 smoke test 只应在用户明确愿意使用自己的 credential、endpoint 和 API 费用时手动运行。

## Foundation 2B：离线 adapter-owned compatibility policy

`route` 是确定性的 control-plane 与 adapter-policy 边界诊断入口：

```bash
uv run leonervis-code route

uv run leonervis-code route \
  --model beta \
  --max-output-tokens 32 \
  --fallback-model default

uv run leonervis-code route \
  --model beta \
  --temperature 0.2
```

Route resolver 负责**硬**准入规则：有效 provider/model 选择、enabled 状态、所需 tool-use/streaming capability、canonical option 类型与范围、fallback 有效性，以及 Harness-owned field 保护。

选定 adapter 负责 provider-native wire name 和有文档依据的**软**兼容行为。Fake `beta` model 用于证明这种区别：请求的 `temperature` 会作为已知 fixed-sampling incompatibility 被省略，`route` 显示该决定，而不是静默改变请求或错误 hard fail。

Provider-specific extension 当前只有受控 Python API 路径；它不能覆盖 `model`、messages、tools、streaming、token-limit fields 或 adapter-generated parameter fields。CLI 暂不接受任意 JSON body override。

`route` 的 Foundation 2B 子命令形式完全离线：不构造 provider client、不读取环境变量、不访问网络，也不显示 credential reference/value。带全局 `--model` 的 route 使用真实 resolver 展示 provider、protocol、wire model、base URL 来源和 `configured/missing/not required` 状态，但仍不构造 client 或发送请求。成功 preview 不代表远端 provider 必然接受请求。

详细决策见 [0005：Provider-neutral Model Routing](./decisions/0005-foundation-2a-provider-neutral-model-routing.md) 与 [0006：Adapter-owned Compatibility Policy](./decisions/0006-foundation-2b-adapter-owned-compatibility-policy.md)。

## Foundation 4A：Permission Policy Contract

在暴露任何写工具前，Host现在先拥有一个无状态、无I/O的纯`PermissionGate` policy kernel。能力上限固定为`read-only | workspace-write | danger-full-access`，交互模式固定为`ask | auto`，两者正交；结果固定为`allow | ask | deny`并携带stable machine-readable reason。Policy action class是`workspace-read | workspace-create | workspace-overwrite | dangerous | unknown`，其中unknown在所有配置下fail closed。

当前`read_file`、`glob`与`grep`统一归类为`workspace-read`，因此在所有mode/approval组合下allow且不会要求terminal confirmation。Workspace create/overwrite在`read-only`下deny，在更高能力模式下由`ask | auto`决定ask或allow；dangerous action只有`danger-full-access`可进入ask/allow。PermissionGate不读取CLI、Session、provider、credential或filesystem，不执行Tool，不创建approval token，也不能绕过workspace、symlink、size、timeout、conflict、causality或durability hard bounds。

作为该边界的前置修复，`read_file`现在拒绝最终和中间的所有symlink component，包括指向workspace内部的link与broken link；普通nested UTF-8读取和32 KiB bound保持不变。当前local single-user v0仍不声称消除检查与open之间的hostile concurrent TOCTOU。

本slice尚未把PermissionGate接入AgentLoop、CLI、Session或provider projection，也没有新增用户配置、approval UI或model-visible tool。因此canonical system prompt保持v4及既有fingerprint，adapter contract保持v5，ToolArguments保持v1，new `turn_committed`保持schema v2，`context_compacted`继续v2/v3 replay，Effective Context保持ctx-v1/v2。下一步是exact action identity与single-use approval grant，之后才进入durable audit、approval coordinator、AgentLoop/CLI integration与create-only write。

完整决策见[0022：Foundation 4A Permission Policy Contract](./decisions/0022-foundation-4a-permission-policy-contract.md)。

## Foundation 1D：Bounded Literal Grep 与 Versioned Tool Arguments

模型可见只读工具面扩展为固定顺序的`read_file, glob, grep`。`grep(query, include)`使用与glob相同的portable workspace-relative selector选择non-symlink regular files，再在strict UTF-8 logical lines内执行case-sensitive literal substring search；每个matching line只输出一次compact JSONL，包含POSIX relative path、1-based line number与完整line text。它不支持regex、index、Unicode normalization、`.gitignore`、multiple patterns或context windows。

Grep具有明确hard bounds：最多1,000个candidates、每file 1 MiB、aggregate 16 MiB、200个matching lines和32 KiB model-visible output，并继续受selector的entry/directory/depth bounds约束。Unreadable、oversized、NUL或invalid-UTF-8 selected file均为whole-call safe error；只有match/output cap返回complete JSON records的stable prefix与`{"truncated":true}` sentinel。No-match仅在bounded candidate set被完整搜索时为空成功。读取时再次执行regular/non-symlink与descriptor identity检查，同时保留local single-user TOCTOU边界。

为表达grep的两个参数，in-memory `ToolUse`改用immutable `ToolArguments` v1 canonical JSON object。新`turn_committed`使用record-local schema v2保存`arguments_version + arguments`；legacy schema-v1 read/glob records在replay时转换为同一generic representation，旧JSONL不重写，resume后只append v2。其他Session records仍v1，`context_compacted`仍兼容v2/v3，Effective Context representation仍为ctx-v1/v2。

三个工具继续共享每user turn三次顺序execution预算，AgentLoop和ProjectSession仍显式composition/dispatch而非dynamic registry。Anthropic与OpenAI-compatible ordinary count/create按相同catalog投影exact three schemas，compact summary仍no-tools，parallel calls仍关闭。Adapter contract升级为v5；canonical model system prompt升级为v4并声明literal grep、no-match/truncation解释及仍不可用的write/Bash/regex能力。Generic arguments、prompt与catalog会按设计改变current-binary context IDs，但不重写历史checkpoint。

完整设计见[0021：Foundation 1D Bounded Literal Grep](./decisions/0021-foundation-1d-bounded-literal-grep.md)。

## Foundation 1C：Bounded Workspace Glob

模型可见只读工具面现在包含固定顺序的`read_file`与`glob(pattern)`。`glob`使用workspace-relative、`/`分隔的portable pattern，支持component `*`、`?`、bracket class与whole-component `**`；裸pattern不隐式递归，hidden component必须显式以`.`匹配，也不读取`.gitignore`。结果只包含non-symlink regular files，使用POSIX relative path与deterministic UTF-8 lexical order；目录、special files和所有symlink都不返回或遍历。

搜索有多重hard bounds：pattern最多4096 characters/bytes与64 components，最多200个matches、32 KiB output、10,000个scanned entries、1,000个directories和32层深度。Match/output cap返回stable prefix与`[truncated]`；traversal/depth bound因无法证明完整性而返回安全error，不泄露absolute workspace或raw OS failure。实现只使用stdlib `os.scandir`与component `fnmatchcase`，没有shell或新增dependency；local single-user TOCTOU边界保持诚实可见。

两个工具共享每个user turn三次顺序execution预算。AgentLoop仍显式dispatch，未知工具和limit都形成structured result，provider failure或durable commit failure不会提交candidate turn。一个窄的canonical catalog固定`read_file, glob`顺序，同时驱动Effective Context identity及Anthropic/OpenAI-compatible ordinary count/create schemas；compact summary继续no-tools，parallel calls继续关闭。

Foundation 1C当时为保持append-only兼容，曾以schema-v1 `ToolUse.path`作为read/glob single-string seam，adapter分别投影`{"path":...}`与`{"pattern":...}`；它让旧read-only Session与mixed glob/read turn无需重写即可resume和compact。该临时seam现已由Foundation 1D的`ToolArguments`与record-local turn schema v2取代，但legacy v1 decoder继续兼容。Foundation 1C当时的adapter v4、prompt v3和两工具context identity仍作为历史设计事实保留。

完整设计见[0020：Foundation 1C Bounded Workspace Glob](./decisions/0020-foundation-1c-bounded-workspace-glob.md)。

## Foundation 1B：确定性的受限 read_file 工具循环

REPL 和 `prompt` 命令完成以下最小、可测路径：

```text
终端输入 → AgentLoop（固定 canonical system prompt snapshot + 有序因果上下文）
  → ScriptedFakeProvider → 在当前 workspace 内可选 read_file
  → 结构化 tool result → ScriptedFakeProvider → 最终文本输出
```

Provider 的一次响应只能是最终 assistant 文本或一个 `read_file` 请求。Loop 只有在 provider 结束后才返回最终文本，并且只有该成功发生后，才提交本次尝试中的完整 user 输入、可能的 tool request/result 和最终 assistant 文本。

每个 user turn 最多允许三次文件读取。超额请求会收到结构化上限错误；如果 provider 随后仍再次请求工具，loop 会确定性停止。

`read_file` 只接受解析后仍在当前 workspace 内的相对路径。它拒绝绝对路径、`..` 或符号链接逃逸、缺失路径、目录、不可读文件和无效 UTF-8；最多返回 32 KiB UTF-8 文本并携带截断标记。它不能写入、重命名、删除、执行命令、搜索或访问网络。

默认 `ScriptedFakeProvider` 保持可见回显行为，不会自行请求工具。其 scripted 形式为测试提供确定性工具循环；`demo-read <path>` 将同一条固定链路公开为手动终端验证入口。

`prompt` 是一次性命令，但每次成功 turn 都会自动保存。同一 REPL 中，`/history <count>` 只显示当前 Session 已完成的 user/final-assistant 回合，不显示内部工具数据。

Foundation 1B 原始切片只验证了进程内原子历史；Foundation 3D 进一步将完整 turn 持久化到 workspace JSONL。若在非交互终端中直接运行 `leonervis-code`，程序会提示使用 `leonervis-code prompt "..."` 并以非零状态退出，避免管道或 CI 意外卡住。

详细决策见 [0001：单轮 Loop](./decisions/0001-foundation-0-single-turn-loop.md)、[0002：确定性 REPL](./decisions/0002-foundation-0-deterministic-repl.md)、[0003：内存文本历史](./decisions/0003-foundation-1a-in-memory-text-history.md) 和 [0004：受限 read_file 工具循环](./decisions/0004-foundation-1b-bounded-read-file-tool-loop.md)。

## Foundation 3H：Pre-turn Automatic Context Compaction

普通one-shot与REPL prompt现在会在发送新turn前评估exact initial request：current Effective Context + pending user message + requested output reserve。Known `FITS`且`(input + reserve) * 100 >= window * 80`时最多尝试一次proactive `high_water` compact；known `CONTEXT_EXCEEDED`时最多尝试一次mandatory `overflow` compact。`UNKNOWN`不猜测、不生成summary，fake runtime保持无请求无噪声，`MODEL_OUTPUT_EXCEEDED`则因compact无法修复reserve而直接拒绝。

`PreparedAgentTurn`在history mutation前固定唯一pending `UserMessage`和committed context snapshot。Pending item进入source与candidate assessment，因此判断覆盖真正将发送的request；它不进入summary source、checkpoint、context identity或durable history。Checkpoint成功后prepared turn只rebase committed snapshot，仍以同一个pending tuple发送一次，并且只有完整普通turn成功后才持久化。

Automatic与manual `/compact`共用3F-2的prepare → runtime work → revalidate/commit/install transaction：至少4个完整effective turns、保留最近2个turns、summary更早complete turns、known comparable count、candidate known `FITS`且严格减少pending-inclusive input、checkpoint append+fsync后才安装memory。一个`provider_for_turn()` lease固定provider/route/capability/status/generation，覆盖initial assessment、summary、candidate assessment和完整tool loop，同时阻止switch、另一turn、manual compact、resume transition与close。

每个prompt只有一次automatic attempt，不递归compact，也不在tool continuation或provider error后重试。Proactive failure若仍是安全precommit且原request known `FITS`，会发warning后继续原turn；mandatory failure则保留原overflow rejection并且不发送普通generation。Stale或checkpoint durability不确定时不能继续旧request；若checkpoint已经durable commit而后续generation失败，checkpoint保留，pending turn不提交。

新的`context_compacted`使用closed schema v3，持久化`trigger = manual | high_water | overflow`，并且只有`high_water`携带固定`high_water_percent = 80`。Schema-v2 checkpoint继续按legacy manual provenance replay；trigger只作审计和`/context`展示，不进入`ctx-v2` identity，也不持久化token count、fit report或pending prompt。Typed prompt events只报告安全计量、context ID、turn counts、checkpoint sequence和reason code；one-shot事件写stderr，stdout仍只有model response。

Canonical model system prompt已审阅：automatic timing完全由Host控制，模型仍不能请求compact，既有untrusted Host-summary framing已覆盖compact后的模型输入。因此version 2、exact text与fingerprint不变。完整设计见[0019：Pre-turn Automatic Context Compaction](./decisions/0019-pre-turn-automatic-context-compaction.md)。

## Foundation 3G：Target-aware Resume Prepare/Commit

Startup `--resume` 与 REPL `/resume` 现在先prepare target、构造candidate Effective Context并用当前runtime screening，之后才durable commit。Known context/model-output overflow在任何resume audit、tail repair或latest pointer写入前拒绝；`UNKNOWN`以warning fail open，fake runtime明确screening unavailable且不发provider request。恢复仍只恢复Session state，不按历史binding重建runtime。

`SessionStore.prepare_resume()`是物理只读的一次性独占lease：它要求既有root/directory lock/target lock/latest/transcript，使用`O_NOFOLLOW` retained descriptor重放，并把incomplete final crash tail只记录为pending recovery。Transcript stale token包含device/inode/size/mtime/ctime和exact-byte SHA-256；`latest` selector另有pointer token。Commit在第一笔写前验证transcript、pathname、target lock与latest CAS，因此append、same-size replacement、inode/symlink/lock swap以及count期间latest移动都作为retryable conflict拒绝。显式UUID/path忽略无关latest移动；same-current selector直接返回无写入no-op。

Commit先candidate-replay proposed records，再按`Recovery`（若需要）→`SessionResumed`→atomic latest update执行。`Recovery`允许紧跟`SessionClosed`但保持closed，只有后续`SessionResumed`重新打开。Prepared descriptor/lock在成功后转移给`SessionWriter`，普通append也通过descriptor并校验pathname identity，消除revalidate/reopen TOCTOU。

`SessionResumed`的fsync是语义commit point。Typed result区分precommit/stale、recovery-only、durability unknown、resume-applied/latest-failed和latest-replaced/directory-fsync-unknown；commit point后的错误不再声称“全部未变”或做不可靠rollback。Top-level `--resume ... prompt`把resume evidence写stderr，使stdout只保留最终model response；known reject以exit 2和空stdout结束。

Manager的context-transition lease固定current provider/route/capability/status/generation，并阻止switch、turn、compact和close。Screen使用candidate loop的`effective_context_snapshot()`，所以compacted Session只按summary + retained suffix计量；下一次真实invocation仍执行完整preflight。Canonical model system prompt已审阅：本切片没有模型可见变化，保持version 2、exact text与fingerprint不变。完整设计见[0018：Target-aware Resume Prepare/Commit](./decisions/0018-target-aware-resume-prepare-commit.md)。

## Foundation 3F-2：Controlled Compact Transaction

REPL `/compact` 现在能在保留完整 append-only transcript 与 `/history` 的同时，手动缩短 provider-visible effective context。Foundation 3F-2的固定policy要求至少4个完整effective turns，保留最近2个turns原文，并用当前真实provider对更早projection生成一次summary；fake runtime不可用，该原始切片本身不自动触发，也不重试原user turn。Foundation 3H随后在新turn发送前按known evidence调用同一transaction，但仍不做failed-turn retry。

Compact generation使用独立版本化 prompt和专用 no-tools request。Anthropic native body省略`tools`，OpenAI-compatible同时省略`tools`与`parallel_tool_calls`；count与generation共享同一input projection。只接受正常结束的非空文本，tool call、refusal、truncation与malformed response全部fail closed。

Summary不属于`ConversationItem`或真实turn。Effective state是`Host summary + retained complete-turn suffix`，adapter以明确的untrusted continuation framing投影summary。Normal Agent canonical system prompt升级为v2，说明Host summary是早期conversation context而不是system instruction或新user request。无summary context仍沿用原`ctx-v1` identity；summary-bearing context使用`ctx-v2`。

Session不重写旧行：普通records继续是schema v1，legacy Foundation 3F-2 `context_compacted`是schema v2，当前manual与automatic checkpoint写schema v3。V3增加trigger provenance与可选high-water percentage；mixed replay接受v2/v3并把v2解释为manual，从所有`TurnCommitted`重建full history，从latest checkpoint重建summary/retained suffix，让后续turn同时追加到full/effective。Checkpoint append复用candidate replay validation、O_APPEND、flush/fsync，然后才安装内存effective state。

Transaction在generation前冻结writer/session/sequence、loop、full/effective state与source context ID；generation和candidate assessment结束后重新检查这些事实。Candidate必须与source使用可比较的known count、known `FITS`且严格减少input tokens。任何precommit、stale或persistence failure都不写`TurnFailed`，也不改变effective memory。

`/context`在compact后显示checkpoint source、summary presence、retained real turns与checkpoint sequence，而summary不计入transcript turn/item。完整设计见[0017：Controlled Compact Transaction](./decisions/0017-controlled-compact-transaction.md)。

## Provider-neutral Effective Context Snapshot 与 `/context`

`AgentLoop` 现在明确区分 append-only transcript 派生的 full history、provider-visible effective history 和单次 invocation request。3F-1 中 full/effective history 在 restore、成功 commit 与 resume 后仍完全相等；真实 turn 的初始请求和每次 tool continuation 都从同一个 `EffectiveContextSnapshot` 加上当前 pending suffix 派生，因此没有模型行为变化，但 future compact 不再需要改写 `/history` 或 durable transcript truth。

完整 committed history 使用统一的 strict validator，只接受 `UserMessage, (ToolUse, matching ToolResult)*, AssistantText`；tool pair 必须相邻、ID 匹配且全局唯一。Session replay、loop restore 与 effective-context construction共享该因果规则，同时保留各自的 schema、大小与 provider invocation terminal validation。

Snapshot 对 current system prompt、neutral `read_file` contract 与完整 effective turns做 canonical JSON + domain-separated SHA-256，得到稳定 `ctx-v1-...` content identity。Identity不包含 Session/runtime/provider/audit/token metadata，不持久化到 JSONL，也不声称 transcript tamper-proof。

REPL `/context` 在 `ProjectSession` facade lock 内冻结 context 与 target，显示 source、context ID、full/effective turn/item counts、exact/estimated/unknown input、reserve、两类模型限制、fit与known remaining capacity。该命令不调用 generation/tool、不写 transcript或audit，也不修改 history/runtime。Fake runtime明确 unavailable；OpenAI-compatible使用本地 estimate；official Anthropic exact inspection可能调用 count-only `messages.count_tokens`，但不调用 `messages.create`。

Session schema继续为v1，不保存 effective context/checkpoint/count。详细决策见 [0016：Provider-neutral Effective Context Snapshot](./decisions/0016-provider-neutral-effective-context-snapshot.md)。Canonical model system prompt已审阅；Host-only inspection和full-history passthrough不改变模型可见能力，因此version 1与fingerprint不变。

## Target-aware runtime switch UX

长生命周期 runtime 的 `/provider use`、`/model` 与对应 `ProjectSession` API 现在会在提交 candidate 前，对当前 committed conversation context 做 destination-specific screening。`AgentLoop` 构造当前 canonical system prompt 与 exact committed causal history 的只读 snapshot；空 Session 保持 `history=()`，不会为了计量伪造 user message。

Adapter 的计量路径接受空历史或以 `AssistantText` 结束的完整 committed history，但真实 `respond()` 仍严格要求以 `UserMessage` 或 `ToolResult` 结束的 invocation history。Anthropic/OpenAI-compatible 的 count 与 create 因而继续共享同一 native projection，又不会放宽真实发送的因果验证。

Manager 使用已经准备好的同一个 provider/route/capability candidate：

- known context/model-output overflow 在 active selection 与 client 交换前抛 `RuntimeSwitchContextError`，关闭 candidate，旧 runtime、selection 与 generation 不变；
- `FITS` 提交并返回 count method/value、reserve 与 window；
- `UNKNOWN` fail open，但 REPL 以 warning 明确 compatibility 未确认、没有删除历史、下一次真实 invocation 仍会 full preflight；
- fake destination 不需要 compatibility report。

`ProjectSession` 在 facade lock 内冻结 history、执行 screening/commit，再追加既有 schema-v1 `RuntimeChanged`。若 runtime 已切换但 audit append 失败，会抛携带已生效结果的 `RuntimeSwitchAuditError`，不误报为未切换，也不做不可靠 rollback。Transcript binding 现在保存真实 runtime generation。Rejected switch 不写 conversation、`TurnFailed` 或 runtime-change record。

Foundation 3E 的原始切片不处理 `/resume`/`--resume` 的切换前判断；该边界现已由 Foundation 3G 的只读 prepare、current-runtime screening 与 durable commit transaction 补齐。Runtime switch 本身仍不实现 compact、历史删除或自动新 Session。

详细决策见 [0015：Target-aware Runtime Switch UX](./decisions/0015-target-aware-runtime-switch-ux.md)。Canonical model system prompt 已审阅；这仍是 Host-side runtime control，version 1 与 fingerprint 不变。

## Target-specific request counting 与 per-invocation preflight

Runtime 现在会把 provider client、exact route、context/model-output capability 与 redacted status 固定为完整 turn snapshot。Snapshot 是唯一的 provider invocation 入口，因此初始请求、每次 `read_file` continuation 和工具上限后的最终请求都会重新 preflight。

判断明确区分三个概念：context window、模型最大输出，以及当前 route 的 requested output reserve。`input + reserve == window` 允许；已知 `>` 时在发送前抛出 typed local error；任一必要事实 unknown 时不猜测并允许 provider 最终裁决。失败 turn 不提交 conversation history，只追加安全的 `TurnFailed` audit record。

Anthropic official endpoint 使用官方 SDK `messages.count_tokens` 对与 create 共用的 model/system/messages/tools projection 做 exact count；失败安全退化为 compact UTF-8 JSON 的 `ceil(bytes / 4)` estimate。OpenAI-compatible Chat Completions 始终使用同形 local estimate，不盲调其他协议的 count endpoint。

Profile registry schema v4 增加 `model_max_output_tokens` override；private discovery cache schema v2 可逐字段保存 context 与 model-output positive limits。`route`、`/status` 和 `/provider current` 展示两个限制与 requested reserve，但不记录成功请求的 last-token meter。Foundation 3H现在消费新turn发送前的fit report决定是否compact；每次真实invocation的preflight仍是最终gate。

详细决策见 [0014：Target-specific Request Counting 与 Preflight](./decisions/0014-target-specific-request-counting-and-preflight.md)。Canonical model system prompt 已审阅；本切片只增加 Host 发送前控制，没有模型可见能力变化，因此保持 version 1 与原 fingerprint。

## Provider-owned model context capability

Runtime 现在能在不伪造未知限制的前提下解析当前 exact endpoint/model 的 context window。解析优先级固定为：

1. 命名 profile 的 exact override；
2. 只匹配官方 provider/endpoint/exact model 的 built-in catalog；
3. fresh private XDG discovery cache；
4. provider-owned live discovery；
5. `unknown`。

Anthropic 官方 endpoint 复用同一个官方 SDK client 的 Models API。Generic OpenAI-compatible `/models` 不存在统一 context metadata contract，因此不会被盲目探测。

```bash
uv run leonervis-code provider add local-qwen \
  --provider custom \
  --model Qwen/Qwen3.5 \
  --protocol openai-compatible \
  --base-url http://127.0.0.1:11434 \
  --context-window-tokens 131072
uv run leonervis-code provider show local-qwen
uv run leonervis-code --profile local-qwen route
```

`provider show` 将用户配置标为 `context window override`；离线 `route` 和 runtime `/status` 显示 resolved value 与 source。成功 discovery 只进入：

```text
${XDG_CACHE_HOME:-~/.cache}/leonervis-code/model-context-capabilities.json
```

Cache 不保存 credential value、raw provider body 或 Session 内容。Profile registry schema v3 reader 兼容 v1/v2/v3，写操作只升级实际写入层，`provider migrate` 可显式升级。

这一切片只建立容量事实，尚不计算当前请求 token、不阻止超限请求，也不自动 compact。详细设计见 [0013：Provider-owned Model Context Capability](./decisions/0013-provider-owned-model-context-capabilities.md)。

## ADR 索引

1. [0001：Foundation 0 单轮 Loop](./decisions/0001-foundation-0-single-turn-loop.md)
2. [0002：Foundation 0 确定性 REPL](./decisions/0002-foundation-0-deterministic-repl.md)
3. [0003：Foundation 1A 内存文本历史](./decisions/0003-foundation-1a-in-memory-text-history.md)
4. [0004：Foundation 1B 受限 read_file 工具循环](./decisions/0004-foundation-1b-bounded-read-file-tool-loop.md)
5. [0005：Foundation 2A Provider-neutral Model Routing](./decisions/0005-foundation-2a-provider-neutral-model-routing.md)
6. [0006：Foundation 2B Adapter-owned Compatibility Policy](./decisions/0006-foundation-2b-adapter-owned-compatibility-policy.md)
7. [0007：Foundation 3A Anthropic 非流式 Adapter](./decisions/0007-foundation-3a-anthropic-non-streaming-adapter.md)
8. [0008：Foundation 3B 本地多 Provider Runtime](./decisions/0008-foundation-3b-local-multi-provider-runtime.md)
9. [0009：Foundation 3C 命名 Provider Profile 与 Runtime Manager](./decisions/0009-foundation-3c-named-provider-profiles-and-runtime-manager.md)
10. [0010：Foundation 3D 稳定 Profile Identity 与可恢复 Session](./decisions/0010-foundation-3d-stable-profile-identity-and-durable-sessions.md)
11. [0011：解耦 REPL 展示与 Slash Dispatch](./decisions/0011-decoupled-repl-presentation-and-slash-dispatch.md)
12. [0012：第一版 Canonical Model System Prompt](./decisions/0012-first-canonical-model-system-prompt.md)
13. [0013：Provider-owned Model Context Capability](./decisions/0013-provider-owned-model-context-capabilities.md)
14. [0014：Target-specific Request Counting 与 Per-invocation Preflight](./decisions/0014-target-specific-request-counting-and-preflight.md)
15. [0015：Target-aware Runtime Switch UX](./decisions/0015-target-aware-runtime-switch-ux.md)
16. [0016：Provider-neutral Effective Context Snapshot](./decisions/0016-provider-neutral-effective-context-snapshot.md)
17. [0017：Controlled Compact Transaction](./decisions/0017-controlled-compact-transaction.md)
18. [0018：Target-aware Resume Prepare/Commit](./decisions/0018-target-aware-resume-prepare-commit.md)
19. [0019：Pre-turn Automatic Context Compaction](./decisions/0019-pre-turn-automatic-context-compaction.md)
20. [0020：Foundation 1C Bounded Workspace Glob](./decisions/0020-foundation-1c-bounded-workspace-glob.md)
21. [0021：Foundation 1D Bounded Literal Grep](./decisions/0021-foundation-1d-bounded-literal-grep.md)
22. [0022：Foundation 4A Permission Policy Contract](./decisions/0022-foundation-4a-permission-policy-contract.md)
