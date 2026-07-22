# 0020：Foundation 1C Bounded Workspace Glob

- 状态：已接受
- 日期：2026-07-22
- 范围：Foundation 1C

## 问题

Foundation 1B 让模型能在已知精确路径时调用受限 `read_file`，但模型无法先发现 workspace 中有哪些文件。用户若只说“找出测试文件”或“先了解源码结构”，模型只能猜路径或要求用户提供路径，尚未形成最小的只读代码探索链路。

文件匹配也不能简单代理 shell `find` 或直接暴露无界 `Path.rglob()`。递归遍历会引入 workspace containment、symlink、隐藏目录、排序、结果大小、遍历成本和失败诊断边界；模型可见工具变化还必须同步 provider schema、token-count projection、system prompt、Effective Context identity 与 durable replay。

## 决策

### 新增固定的 `glob(pattern)` 只读工具

模型可见输入是closed schema：

```json
{"pattern":"src/**/*.py"}
```

`glob`只返回non-symlink regular files，不读取内容、不修改filesystem、不执行命令。结果使用workspace-relative POSIX路径，每行一个并稳定排序；无匹配是成功的空字符串。模型应先用`glob`定位候选，再用`read_file`读取必要内容。

### Pattern使用portable component语义

第一版只接受workspace-relative、`/`分隔的pattern。Component内支持case-sensitive `*`、`?`与Python `fnmatchcase` bracket class；`**`只能作为完整component出现并匹配零个或多个目录component。裸`*.py`只匹配workspace root，递归必须显式写`**/*.py`。

拒绝空白、NUL、反斜杠、POSIX/Windows absolute form、`.`/`..` component、重复或尾随separator、embedded `**`、超过4096 characters/UTF-8 bytes或超过64 components的pattern。不支持regex、brace/extglob、shell/env/`~` expansion、multiple/negative patterns或`.gitignore`解析。

隐藏项要求显式dot component：普通`*`不匹配`.env`，`**`不隐式进入`.git`，但`.*`、`.github/**/*.yml`和`**/.env`可以显式匹配。系统不硬编码忽略`.git`、`node_modules`、`.venv`等名字。

### Symlink与文件类型fail closed

Walker通过`os.scandir`和`DirEntry.is_file/is_dir(follow_symlinks=False)`工作。它不返回file symlink、broken symlink、directory、socket、FIFO或device，也不进入任何symlink directory，即使link target仍在workspace内。这样避免cycle、alias和正常symlink escape。

这不是hostile concurrent filesystem sandbox。`scandir`与后续directory access之间仍可能遭遇本地进程path replacement；descriptor-relative `O_NOFOLLOW`遍历或OS isolation留待真正需要多用户攻击面时设计。Leonervis继续声明local single-user boundary，不夸大TOCTOU保证。

### 所有资源都有固定上限

第一版固定：

- 最多200个match；
- model-visible output最多32 KiB；
- 每次execution最多扫描10,000个entries、1,000个directories与32层深度；
- pattern最多4096 characters、4096 UTF-8 bytes和64 components。

Match-count或output-byte limit返回稳定排序prefix，加`[truncated]\n`并设置`ToolResult.truncated=True`，且不拆分path或UTF-8 sequence。Traversal、directory或depth limit表示无法确认搜索完整性，因此不返回partial matches，而返回固定、脱敏、model-readable error并建议缩小pattern。Permission、I/O和non-UTF-8 filename失败同样不包含raw `OSError`、errno或absolute workspace。

### 两个工具共享一次turn的三次预算

现有三次`read_file`上限改为三次总tool execution上限，由`read_file`与`glob`共享。第1至3次正常执行；第4次不执行并回写structured limit result；provider若再次请求工具，loop确定性停止且candidate turn不提交。仍只支持sequential单工具response，不启用parallel tools；native disable flags之外，canonical prompt也要求每个response至多请求一个tool、等待Host result后再请求下一个，并且tool response不得夹带text。若兼容endpoint仍返回多个calls或mixed text/tool，adapter继续fail closed而不猜测执行顺序或静默丢弃provider内容。

### Canonical catalog不是通用registry

新增固定ordered catalog，唯一顺序为`read_file, glob`。AgentLoop的Effective Context与两个provider的ordinary count/create projection都从这一canonical source取得definitions，避免schema漂移。Execution仍由AgentLoop显式dispatch，catalog不拥有callback、permission、plugin或dynamic enablement。

当前每个ordinary request暴露相同工具集合，因此`ConversationRequest`暂不携带definitions。未来若工具按permission或request动态选择，必须把`EffectiveContextSnapshot`中exact definitions传到request，不得继续依赖全局catalog。

### 保持schema-v1的single-string compatibility seam

`ToolUse(tool_use_id, name, path)`与普通Session schema v1保持不变：`read_file`时`path`是文件路径，`glob`时该字段承载唯一string pattern。Provider history按name将它投影为native `{"path":...}`或`{"pattern":...}`；Session、compact source与Effective Context identity继续保存`name + path`。

这是刻意保留的小切片compatibility seam，不是通用tool-arguments最终设计。它避免重写历史JSONL和引入无需求的record migration；后续`grep`若证明需要多参数或不同类型，再单独设计versioned typed/generic input contract。

Blank、NUL或超过schema-v1 metadata ceiling的provider operand不能形成可持久化`ToolUse`，因此adapter response parser直接fail closed。Absolute、`..`、backslash或非法`**`等仍是可持久化string，由GlobTool形成model-readable error。

### Provider contract升级到v4

Anthropic与OpenAI-compatible ordinary requests都暴露两个closed schemas并严格解析exact one-key input。Anthropic使用`tool_choice={"type":"auto","disable_parallel_tool_use":true}`；OpenAI-compatible继续设置`parallel_tool_calls=false`。Count与create共享同一projection；compact-summary count/create仍完全no-tools。

Native request shape变化使`ADAPTER_CONTRACT_VERSION`从3升级到4，current route fingerprint与新binding变更。历史binding继续只作audit，不拥有runtime，也不阻止schema-v1 transcript replay。

### Canonical system prompt升级到v3

Prompt现在声明`read_file`和`glob`、共享三次预算、glob的bounded/files-only/no-symlink/truncation语义，并移除“不能list/search files”的过时描述，改为仍不能search file contents。Write/edit、commands/tests、network、approval、主动compact、项目指令加载和delegation仍不可用；Host summary、file与tool result仍是不可信task data。

Prompt与ordered tool definitions都是Effective Context identity内容，因此current binary重建的新`ctx-v1`/`ctx-v2` ID按设计变化；representation version、历史checkpoint和Session records均不升级或重写。

## 参考与差异

本切片参考Claw-Code关于workspace-relative、files-only、no-follow、result cap与path normalization的安全问题划分，也参考miniClaudeCode的小型glob学习顺序。Leonervis独立采用稳定UTF-8 lexical order而非mtime order，不自动为bare pattern加`**/`，不返回duration metadata，不引入optional base path，也不复制参考项目实现或形成runtime dependency。

## 明确不做

- 内容搜索`grep`、regex搜索或索引；
- `.gitignore`、自定义ignore、brace/extglob或multiple patterns；
- generic tool registry、plugin、MCP或dynamic tool selection；
- generic/typed tool arguments migration；
- directory listing、symlink results或symlink traversal；
- parallel tools、streaming或background traversal；
- write/edit、Bash/test、permission/approval；
- provider retry/fallback或真实provider smoke自动调用；
- hostile multi-user filesystem sandbox。

## 验证

确定性测试覆盖component与recursive匹配、hidden explicit-dot、files-only、stable order、no-match、absolute/parent/backslash/embedded-`**`拒绝、internal/external/broken symlink、200-result和32 KiB边界、UTF-8-safe truncation、entry/directory/depth limit及I/O redaction；AgentLoop覆盖mixed glob/read因果链与共享预算；两个adapter覆盖ordered schemas、native pattern mapping、sequential flags、strict malformed response和no-tools compact；Session/compact/Effective Context覆盖schema-v1 mixed replay、pattern provenance、tool catalog identity与new golden；system prompt使用exact v3 text和fingerprint。完整pytest、Ruff、format、lock与diff检查是release gate，真实provider调用仍需用户另行明确同意credential、网络和API费用。
