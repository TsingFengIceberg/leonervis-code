# 0029：Foundation 4C Bounded Command Execution与Process-group Cleanup

- 状态：已接受
- 日期：2026-07-24
- 范围：Foundation 4C Slice 4–6

## 问题

ADR 0028只固定了`run_command(argv, cwd, timeout_seconds)`的请求、权限与无副作用prepare边界。要真正运行测试、lint和build verification，Host还必须回答：是否经过shell、继承哪些environment、如何避免stdout/stderr pipe死锁、如何表示非UTF-8和截断输出、timeout或用户取消时如何清理子进程，以及进程已经启动后还能否声称失败没有副作用。

普通本地进程不是workspace sandbox。即使cwd位于workspace，程序仍可读取用户目录、联网、修改workspace外文件、解释自己的参数或继续创建子进程。因此executor只能提供明确而有界的进程管理，不得把`danger-full-access`描述成隔离环境，也不得承诺rollback。

## 决策

### 直接执行argv，不解析shell source

Executor使用`subprocess.Popen`直接传入prepared argv，固定`shell=False`、`stdin=DEVNULL`并为每次命令建立新的process session/process group。Leonervis本身不解释管道、重定向、通配符、变量展开或命令替换；这些字符作为普通argument传给executable。

如果模型显式请求并获批某个shell executable及其参数，该shell仍可自行解释后续文本。这属于已批准的危险本地进程执行，不代表Leonervis提供了Bash source-string工具。

Executor在紧贴`Popen`的边界再次逐段检查workspace root与cwd；若它们已经变成missing、non-directory或symlink，则以稳定的`command_cwd_invalid`失败结果拒绝spawn。Path检查与真正的OS spawn之间仍存在无法由普通path API完全消除的local single-user TOCTOU窗口，因此本实现不声称portable hostile-concurrency安全。

### Host-owned closed environment

模型不能提交environment。Executor只从启动Leonervis的Host environment复制固定allowlist，并按实际cwd覆盖`PWD`。Allowlist包括PATH、HOME、locale、terminal/temp、UV/VIRTUAL_ENV和XDG相关字段；provider credential变量和任意project变量不会自动继承。

该过滤只减少意外secret传播。被执行程序仍可能直接读取filesystem、访问credential agent或使用network，因此它不是credential sandbox。

### 有界且持续drain的stdout/stderr

stdout与stderr分别使用独立后台reader持续读取到EOF，避免子进程因pipe buffer填满而死锁；每条stream只保留前32 KiB，但继续drain剩余bytes。结果记录captured bytes、observed total bytes和truncated标志。

保留内容若是合法UTF-8则返回text；否则返回base64。模型收到的是稳定JSON，而不是依赖terminal locale的隐式decode。`ToolResult.truncated`在任一stream超过cap时为true；截断只证明存在被省略输出，不能证明省略内容的含义。

### Timeout、取消与process group清理

`timeout_seconds`限制主进程等待。Timeout或`KeyboardInterrupt`后，Host对整个process group执行有界TERM→KILL升级，并等待主进程与group退出。若主进程正常退出但后代仍持有stdout/stderr pipe，Host也会启动同一清理流程，避免普通返回路径无限等待后台child。

清理和pipe drain都有独立的短grace period，因此终止处理本身有界。若无法确认process、process group或reader已经结束，结果必须标为cleanup incomplete，而不是谎称已经安全停止。程序主动以signal结束、timeout、cancel以及cleanup uncertainty都属于partial outcome，因为命令可能已产生不可回滚副作用。

### 稳定结果语义

Executor用稳定result code区分：成功退出、非零退出、spawn前cwd失效、spawn失败、signal、timeout、cancel以及cleanup不完整。Exit 0且清理完整为`succeeded`；未启动或普通非零退出且清理完整为`failed`；启动后被signal/timeout/cancel或任何清理状态不确定为`partial`，即使主进程本身返回了非零exit code。结果不包含raw OS exception、绝对workspace路径或Host environment。

Command开始后，Leonervis无法断言其filesystem、network或外部服务副作用范围，也不自动retry。模型必须依据结构化exit/status/output继续推理，不得把timeout、signal或truncation解释成“没有发生其他事情”。

## 不变量

- Permission/approval不能提高argv、cwd、timeout或output上限。
- `shell=False`不能被模型参数覆盖。
- stdin不可交互，避免命令秘密读取Leonervis终端输入或无限等待。
- Reader在capture cap后继续drain，output bounding不能引入pipe deadlock。
- Timeout和取消清理process group，而不只终止直接child。
- 进程可能已运行时，失败不得表述成可证明的零副作用或rollback。
- Executor不读取或修改Session，不决定permission，也不直接展示CLI。

## 不在范围

本切片不提供OS sandbox、container、seccomp、namespace、network隔离、filesystem allowlist、resource quota、shell source string、interactive stdin、PTY、streaming output、后台job管理、命令allowlist、自动retry或side-effect rollback。
