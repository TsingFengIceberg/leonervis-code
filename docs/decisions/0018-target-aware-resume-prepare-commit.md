# 0018：Target-aware Resume Prepare/Commit

- 状态：已接受
- 日期：2026-07-22
- 范围：Foundation 3G

## 问题

Foundation 3E 已在真实 provider invocation 与 runtime switch 前检查 effective context，Foundation 3F 又让 compacted Session 能以 `Host summary + retained complete-turn suffix` 恢复。但原来的 startup `--resume` 与 REPL `/resume` 直接调用 mutating `SessionStore.open()`：它先修复 crash tail、追加并 fsync `SessionResumed`、更新 `latest.json`，ProjectSession 之后才取得历史。若目标 effective context 对本次选择的 runtime 已知超限，系统只能在下一次 prompt preflight 拒绝，此时 target transcript 与 latest pointer 已改变。

Resume 还跨越 transcript、target writer lease、runtime counting、内存 Session swap 与 latest pointer。单纯捕获异常或尝试 byte rollback 无法诚实表示 fsync、replace 与目录持久化之间的部分成功。

## 决策

### Resume 是 prepare → screen → commit transaction

`SessionStore.prepare_resume(selector)` 只读解析目标并返回独占、一次性的 `PreparedSessionResume`。Startup 与 live resume 均先从 prepared replay state 预构造 candidate `AgentLoop` 和 `EffectiveContextSnapshot`，再使用本次已选择的当前 runtime 做 compatibility screening。只有 `FITS`、`UNKNOWN` 或 fake-unavailable 才允许 durable commit；known `CONTEXT_EXCEEDED` 与 `MODEL_OUTPUT_EXCEEDED` 在第一笔写入前拒绝。

恢复只恢复 Session state。历史 binding 继续只作审计，不重建、不切换 provider client；当前 runtime 仍服从本次 CLI selector、active profile 或 fake fallback。

### Prepare 必须物理只读

Prepare 不调用会 mkdir/chmod/repair/update latest 的 helper。它要求既有 session root、directory lock、latest metadata、target lock 与 transcript 全部存在且类型安全；缺失或不安全时 fail closed，不创建任何对象。

Target lock 以 existing-only、nonblocking exclusive lease 获取并保持到 commit/abort。Transcript 使用 `O_NOFOLLOW` descriptor 打开，所有读取和 identity 采样来自该 descriptor，并验证 pathname 仍映射同一 inode。正常 JSONL 按既有 codec/replay严格验证；仅最后一段无换行且不是完整 UTF-8 JSON 时形成 `PendingTailRecovery`，prepare 不 truncate、不 append。完整 JSON 缺换行、newline-terminated corruption 与 middle corruption仍原样拒绝。

### Exact stale/CAS 与 selector 语义

Prepare 捕获 transcript 的 device/inode/size/mtime/ctime 与 exact-byte SHA-256；`latest` selector 另外捕获 `latest.json` 的相同 identity/content token。Commit 在 directory lock 内、第一笔 durable write前重读并比较 token，同时验证 retained target lock descriptor及其pathname identity。

因此 append、same-size byte replacement、inode/path/symlink swap、lock replacement 和 `/resume latest` 期间 latest pointer变化都会成为 retryable stale conflict。显式 UUID/path不把其他 Session 推进 latest视为冲突；成功 commit会有意识地把显式目标设为latest。Live selector若已指向current writer，则返回 `ALREADY_CURRENT`，不count、不追加resume record，也不重写latest。

### Descriptor-bound writer 与 recovery chain

Commit后的 `SessionWriter` 接管prepare期间保留的 transcript descriptor和target lock。普通 append/close也通过该descriptor写入，并在每次写前验证pathname仍匹配，避免revalidation与reopen之间的TOCTOU。新建Session同样使用descriptor-owning writer。

`Recovery` 现在可以紧跟 `SessionClosed`，但不会清除closed state；仍只有后续`SessionResumed`才能重新打开。带crash tail的closed transcript因此形成真实链：

```text
SessionClosed -> Recovery -> SessionResumed
```

Commit先对 proposed recovery/resume records执行candidate replay，再固定按以下顺序写入：

1. 如有pending tail，truncate并append+fsync `Recovery`；
2. append+fsync `SessionResumed`；
3. 以0600 temp file、fsync、`os.replace`、directory fsync更新`latest.json`；
4. 把prepared资源promote为writer。

不重写任何合法旧record，也不增加新的Session record schema；`Recovery`和`SessionResumed`继续使用schema v1。

### 当前 runtime screening lease

`RuntimeProviderManager.provider_for_context_transition()`固定provider、route、capability、status和generation，并复用单一active-operation lifecycle。在lease内只允许adapter-owned count/estimate，不调用generation或tool，不改变runtime selection、generation或cache；switch、turn、compact与close被阻止，Exception和BaseException都释放lease。

Screening直接使用candidate loop的`effective_context_snapshot()`。因此普通Session按full committed history计量，compacted Session按summary + retained suffix计量，同时总是采用当前binary的canonical system prompt和tool contract。Transition共享policy只拒绝两种known overflow：

- `FITS`：允许并返回input/method、reserve与window证据；
- `UNKNOWN`：fail open并warning，下一次真实invocation仍执行完整preflight；
- fake runtime：允许但明确screening unavailable，且不伪造provider request；
- known context/model-output overflow：precommit拒绝。

### 语义commit point与truthful partial outcomes

`SessionResumed`成功fsync是resume的语义commit point。此前的stale/precommit failure可声明target/latest未变；此后不得把操作描述为rejected或尝试跨文件rollback。

Typed outcome区分：

- 无durable write的stale/precommit failure；
- 只有Recovery durable，resume尚未提交；
- transcript write/fsync结果不确定；
- resume已生效但latest replace前失败；
- latest已replace但directory fsync失败，crash durability未知。

Latest更新失败发生在resume commit point之后时，返回可用target writer并安装candidate Session；CLI准确提示pointer结果。新Session creation若latest已经replace但directory fsync失败，不删除已可能被pointer引用的transcript。

### ProjectSession 与 CLI

Startup `ProjectSession.open(resume=...)` 和live `switch_session()`使用同一prepared target、effective projection与screening policy。Live路径在facade lock中冻结current writer/loop/sequence/context ID和runtime generation，commit前重查，durable resume后才swap candidate并释放old writer。跨writer/loop的公开读取也在facade lock下完成，避免观察混合Session状态。

`SessionResumeResult`区分already-current、applied、applied-with-latest-failure与latest-durability-unknown。Top-level `--resume ... prompt`把resume evidence写到stderr，stdout只保留最终model response；known reject/stale/precommit failure以exit 2和空stdout结束。REPL `/resume`按FITS、UNKNOWN、fake、same-current和partial durable stage显示不同message kind与准确状态。

## System prompt 审阅

本切片改变Host的Session存储、runtime screening与CLI反馈，不改变模型可见工具、权限、workspace或summary语义。Canonical model system prompt保持version 2、exact text与golden fingerprint不变。

## 明确不做

- 自动compact、overflow retry、自动新Session或自动切换/fallback model；
- 按历史binding恢复runtime；
- transcript删除、重写、fork、branch、import或export；
- cross-workspace或short-ID resume；
- 持久化count、fit report或当前context ID；
- streaming、parallel tools、write/Bash/approval。

## 验证

确定性测试覆盖prepare byte/mode/pointer不变、abort与exclusive lease、deferred tail recovery、closed recovery chain、exact transcript/latest/lock stale detection、显式ID与latest CAS差异、descriptor-bound writes、compacted effective projection、FITS/UNKNOWN/fake/known reject policy、same-current no-op、runtime lease及BaseException释放、CLI stdout/stderr边界和known rejection不变性。完整pytest、Ruff、format、lock与diff检查是release gate；官方Anthropic count-only smoke仍仅在用户另行明确同意credential、网络和API使用时执行。
