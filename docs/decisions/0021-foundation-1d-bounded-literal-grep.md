# 0021：Foundation 1D Bounded Literal Grep

- 状态：已接受
- 日期：2026-07-23
- 范围：Foundation 1D

## 问题

Foundation 1C让模型能按path pattern发现文件，再读取已知文件，但仍无法回答“某个literal text出现在哪些文件和行”。直接代理shell grep、递归库或无界索引会绕过workspace、symlink、encoding、result size、file size、traversal cost与durable causality边界。

Grep还首次需要两个provider-visible参数。ADR 0020刻意保留的schema-v1 `ToolUse.path`只能表达一个string，继续复用会把query/include塞入ad-hoc encoding，破坏strict schema、provider projection与append-only replay。因此本切片必须同时引入真正的versioned generic arguments，并保持旧transcript无重写兼容。

## 决策

### 新增固定的literal `grep(query, include)`

模型可见input为closed object，两个字段都required：

```json
{"query":"ToolUse(","include":"src/**/*.py"}
```

`query`是case-sensitive literal UTF-8 string，不是regex；不跨line、不normalize Unicode、不case-fold。它必须非空，可由空格/tab组成，但拒绝NUL、CR、LF、超过4096 characters或4096 UTF-8 bytes。

`include`与glob共享同一portable component grammar和walker：workspace-relative `/` path，component内支持`*`、`?`、bracket class，`**`只能是完整component；hidden name要求explicit leading dot；不读取`.gitignore`，不支持brace/extglob、multiple/negative patterns或shell expansion。共享代码只负责pattern、bounded traversal、files-only/no-symlink selection与stable UTF-8 lexical order；glob和grep各自保留output、limit和diagnostic contract。

### 输出为deterministic JSON Lines

Candidate按relative path UTF-8 bytes排序，每个file按1-based logical line排序。LF、CRLF与lone CR都终止line，terminator不进入text；final unterminated line仍搜索，trailing terminator不新增empty final line。一个line即使包含query多次也只返回一个record：

```json
{"path":"src/app.py","line":17,"text":"class ToolUse:"}
```

JSON固定key order为`path,line,text`，使用compact UTF-8 encoding。No-match为`content=""`的成功结果。Match-count或output-byte cap返回complete records的stable prefix，加：

```json
{"truncated":true}
```

并设置`ToolResult.truncated=True`。不拆JSON record、source line或UTF-8 sequence。若单个matching-line record本身无法在32 KiB budget内与sentinel共存，则whole-call error并建议`read_file`，不制造partial line/window。

### 所有搜索资源都有hard bounds

固定第一版：

- 最多1000个candidate files；
- 每file最多1 MiB；
- 每execution最多读取16 MiB aggregate bytes；
- 最多200个matching lines；
- model-visible output最多32 KiB；
- selector继续限制4096 pattern characters/bytes、64 components、10000 entries、1000 directories与32 depth。

Candidate cap、traversal/depth/permission、oversized/unreadable/non-UTF-8/NUL file和aggregate cap都表示无法证明完整搜索，因此返回脱敏whole-call error，不返回partial matches。只有match/output cap是明确的truncated success。达到truncation后允许停止读取，因为结果已经声明不完整。

### Files-only、descriptor revalidation与安全边界

Selector不返回或进入任何symlink，也不返回directory、socket、FIFO或device。Content read前重新`lstat`，使用平台支持的`O_NOFOLLOW`打开，`fstat`确认regular file及device/inode identity，并按descriptor实际读取bytes限制growth。Raw `OSError`、errno和absolute workspace不进入model-visible result。

这仍不是hostile concurrent filesystem sandbox。Local process可在多个检查之间replacement；真正multi-user threat surface需要descriptor-relative directory traversal或OS isolation，超出当前local single-user v0边界。

### ToolArguments v1结束single-string seam

`ToolUse`改为`tool_use_id + name + ToolArguments`。`ToolArguments`是immutable versioned canonical JSON object：v1按sorted keys、compact separators、UTF-8和`allow_nan=False`编码，最大16 KiB；内部保存canonical string，projection返回fresh mapping，避免frozen dataclass持有mutable dict。

当前known inputs为：

- `read_file`: `{"path":...}`；
- `glob`: `{"pattern":...}`；
- `grep`: `{"query":...,"include":...}`。

Canonical catalog仍不是generic registry：固定顺序升级为`read_file, glob, grep`，只提供definitions、known exact-input validation与history projection；AgentLoop继续explicit dispatch，catalog不拥有callback、permission、plugin或dynamic enablement。

### `turn_committed` record-local schema v2

新turn只在`turn_committed` record上使用schema v2，tool use item保存：

```json
{"item_type":"tool_use","tool_use_id":"grep-1","name":"grep","arguments_version":1,"arguments":{"include":"src/**/*.py","query":"ToolUse("}}
```

其他Session record继续schema v1；`context_compacted`继续v2/v3。Replay按record type/version dispatch：legacy v1 read path转为`{"path":...}`，glob path转为`{"pattern":...}`，unknown historical name保留为`{"path":...}`以免codec层破坏旧record。V1无法表达grep，不能新写grep-v1。

旧JSONL不迁移、不rewrite；resume后的新turn只append v2，mixed v1/v2 transcript在内存中统一为generic arguments，并继续参与causality validation、provider reprojection、Effective Context、preflight、resume/switch和compaction。Historical binding仍只作audit。

### 三工具共享既有顺序预算

`read_file`、`glob`与`grep`共享每user turn三次execution。第4次请求不执行并收到既有structured limit result；provider若第5次仍请求工具，candidate turn不commit并确定性停止。Native sequential flags、prompt中的one-tool-only rule、pinned Prepared turn/runtime和failure-atomic commit语义保持不变。

### Provider contract v5与system prompt v4

Anthropic与OpenAI-compatible ordinary count/create都从canonical catalog投影exact three schemas和相同order，strict parser只接受一个known exact input；mixed text/tool、multiple calls、unknown/malformed/extra/missing fields继续fail closed。Anthropic保留`disable_parallel_tool_use=true`，OpenAI-compatible保留`parallel_tool_calls=false`，compact summary始终no-tools。

Native shape与generic history projection变化使adapter contract升级到v5。Canonical system prompt升级到v4，声明literal grep、shared budget、empty/truncated解释，移除“不能搜索文件内容”，但继续禁止regex/unrestricted search、write/edit、commands/tests、network、approval、主动compact、project instruction loading与delegation。

Prompt、ordered definitions与structured arguments都会按设计改变current-binary `ctx-v1`/`ctx-v2` content IDs，但Effective Context representation version不变，也不重写historical checkpoint。

## 参考与差异

本切片继续参考Claw-Code对glob/grep的workspace、symlink、output cap与tool-result问题划分，但独立采用literal-only、required include、strict UTF-8、whole-line JSONL、no-ignore、whole-call completeness errors和record-local migration；不复制其实现或形成runtime dependency。

## 明确不做

- regex、case-insensitive mode、context lines、multiple queries；
- content index、cache、watcher、streaming或background search；
- `.gitignore`、custom ignore、negative/multiple include patterns；
- generic execution registry、plugin、MCP或dynamic tools；
- parallel tool calls；
- write/edit、Bash/test、permission/approval；
- hostile multi-user filesystem sandbox；
- provider retry/fallback或未经另行授权的real-provider smoke。

## 验证

确定性测试覆盖literal/line semantics、stable JSONL与escaping、hidden/no-ignore、no-symlink、UTF-8/NUL、candidate/file/aggregate/match/output/traversal bounds、whole-call errors与truncation；Glob regressions证明共享selector不改变Foundation 1C行为。Session tests覆盖immutable arguments、v2 closed codec、legacy v1 conversion、mixed replay与append-only prefix preservation；AgentLoop、ProjectSession、adapters、Effective Context、compaction、prompt、resolver与CLI覆盖三工具integration、v5/v4 identities和failure atomicity。完整pytest、Ruff、format、lock、diff与fake public CLI smoke仍是release gate。
