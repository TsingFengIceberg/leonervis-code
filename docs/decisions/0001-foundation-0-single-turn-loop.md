# 0001：Foundation 0 的单轮确定性 Loop

- **状态**：已采纳
- **日期**：2026-07-15

## 要解决的问题

环境引导完成后，项目已有可安装的命令入口，却还没有任何 Harness 控制流。直接接入真实模型会同时引入认证、网络、流式输出、模型参数、成本和不确定响应，使学习时难以判断问题出在 CLI、Loop 还是 provider adapter。

Foundation 0 只验证第一个不可省略的边界：CLI 将用户 prompt 交给编排层，编排层调用 provider，并把文本结果交回 CLI。

## 决策

实现一个一次性、无状态、确定性的命令路径：

```text
leonervis-code prompt "..."
  → CLI 解析输入
  → AgentLoop.run(prompt)
  → PromptProvider.respond(prompt)
  → DeterministicFakeProvider 返回文本
  → CLI 输出文本并以 0 退出
```

生产代码按当前确实存在的职责拆分：

```text
cli → agent/core/providers
agent → core
providers → core
core → Python 标准库
```

- `core.contracts.PromptProvider` 是唯一 provider seam，只定义 `respond(prompt: str) -> str`。
- `agent.loop.AgentLoop` 只做一次委托，不保存历史、不打印、不重试、不调用工具。
- `providers.fake.DeterministicFakeProvider` 用固定的 `Fake response: <prompt>` 形状返回文本。
- `cli.main` 是本切片的 composition root：解析 `prompt` 子命令，实例化 fake 与 loop，打印结果。

## 为什么先使用 fake provider

fake provider 不读取凭据、不访问网络、不依赖时钟或随机数。测试因此可以准确断言：

1. prompt 是否被原样转交；
2. provider 是否恰好调用一次；
3. 返回文本是否未被 Loop 改写；
4. provider 异常是否没有被静默吞掉；
5. 三种 CLI 入口是否都连到同一条路径。

这比首先集成真实 API 更能证明 Harness 的控制流正确。未来的真实 provider 只需实现同一个 contract；它不应迫使 `AgentLoop` 或 CLI 的输入/输出契约重写。

## 参考与差异

本切片借鉴了学习材料中“先以可控 model double 验证 loop”的一般方法，但不从 `learning-submodules/` 导入或复制运行时代码。与成熟 Coding Agent 的多轮工具循环相比，Foundation 0 故意只保留一轮纯文本委托，以便先理解最小边界。

## 明确不做的内容

Foundation 0 不包含：

- 真实模型 SDK、API key、环境配置或网络请求；
- assistant content blocks、流式输出、模型选择、缓存或重试；
- 工具、workspace、文件读写、Bash、权限确认或审批；
- REPL、session、JSONL transcript、持久化或恢复；
- MCP、插件、多 Agent、服务端、RAG 或后台任务。

每个能力都必须在后续切片中先说明问题、边界、数据流与确定性测试，再实现。

## 验证证据

本切片的验证应覆盖：

- `AgentLoop` 的单次精确委托与错误传播；
- fake provider 的稳定、可重现输出；
- `prompt` CLI 的成功、帮助和参数错误路径；
- `python -m leonervis_code prompt ...` 的 subprocess 端到端行为；
- `pytest`、Ruff 与锁文件一致性检查。
