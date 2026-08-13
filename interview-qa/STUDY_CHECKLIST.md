# Mini OpenHarness 面试自测清单

> 用法：每条都是**问题或任务**。能口头回答的按面试题背，能动手的就当场做。
> 勾选标准（严格）：**不看源码**，能对着下面的代码链接讲出答案、或画出调用链、或跑通演示。
> 每节末尾有「过关标志」——满足才勾这一节的条目。

---

## P0 — 地基：主循环（[engine.py](../src/mini_openharness/engine.py)）

1. **任务**：跑一次 `mini-oh --demo "看下这个项目"`，再 `mini-oh trace show <run_id>`。不查代码，口头说出这次运行依次产生了哪些事件（model_start / tool_start / tool_end / permission_decision / done …），每个事件在干什么。
2. **问题**：Prompt 进入后，`AgentLoop.run()` 第一步做什么？（prompt hook → 校验 → user 消息入 messages → 落盘 → 建 ToolContext）
   - 代码：[engine.py：AgentLoop.run()](../src/mini_openharness/engine.py#L187)
3. **任务**：白板画出调用链 `run() → _drive() → _run() → _loop()`，并标注谁负责建 `RunState`、谁负责 `try/finally` 清理。
   - 代码：[engine.py：_drive()](../src/mini_openharness/engine.py#L208)
4. **问题**：`run()` 和 `resume()` 的公共部分在哪？为什么 `resume()` 不需要新的 user 消息？
   - 代码：[engine.py：resume()](../src/mini_openharness/engine.py#L195)
5. **问题**：`ConversationState` 和 `RunState` 各存什么？为什么必须分开？
   - 代码：[engine.py：ConversationState](../src/mini_openharness/engine.py#L63)、[RunState](../src/mini_openharness/engine.py#L72)
6. **问题**：为什么同一个 `AgentLoop` 禁止两个 `run()` 并发？`_active_run` 存的是 `RunState` 而不是 `bool`，好处是什么？
7. **问题**：调用方对 `async for` 提前 `aclose()` 时，系统如何保证 `_active_run` 被释放？生成器 `aclose()` 不会自动关闭内部生成器——这个坑在哪？（答不出就动手跑一下 `tests/test_engine.py::test_same_agent_loop_rejects_overlapping_runs_and_recovers_after_close`）
8. **问题**：`_loop()` 的一轮 step 里，模型返回 Tool Call 后要经过哪些阶段才把结果回填？重复 Tool Batch 检测防什么？
   - 代码：[engine.py：_loop()](../src/mini_openharness/engine.py#L276)、[重复批次上限](../src/mini_openharness/engine.py#L455)
9. **问题**：工具结果是通过什么字段和 assistant 的 Tool Call 配对的？`_append_tool_result` 里 prefix 是干什么的？
   - 代码：[engine.py：_append_tool_result()](../src/mini_openharness/engine.py#L775)
10. **问题**：`cancel()` 如何中断一个正在跑的 run？`CancelEvent` 在哪被检查？
11. **问题**：`_compact_if_needed()` 什么时候触发？触发了会怎样（compact 事件 → 清历史 → 继续）？
    - 代码：[engine.py：_compact_if_needed()](../src/mini_openharness/engine.py#L748)

**过关标志**：能不看代码画出 P0-3 的完整调用链，且能回答 P0-7 的 aclose 问题。

---

## P1 — 权限与工具系统（[tools.py](../src/mini_openharness/tools.py)）

12. **问题**：一次 `ToolRegistry.execute()` 从进入到最后返回，依次经历哪些阶段？（校验 Schema → 权限审批 → 并发锁 → 执行 → 结果/失败）
    - 代码：[tools.py：ToolRegistry.execute()](../src/mini_openharness/tools.py#L306)
13. **问题**：为什么模型生成的参数仍要 Runtime 用 JSON Schema 再校验一次？
14. **问题**：`ResourceAccess` 是什么？为什么用"资源 + 读写权限"而不是一个 `read_only: bool`？`_resources_conflict` 怎么判冲突？
    - 代码：[tools.py：ResourceAccess](../src/mini_openharness/tools.py#L28)、[冲突检测](../src/mini_openharness/tools.py#L70)
15. **问题**：两个写同一文件的工具并发执行，靠什么机制串行？（`acquire` / 锁管理器）
    - 代码：[tools.py：acquire()](../src/mini_openharness/tools.py#L48)
16. **问题**：`ToolResult` 和 `ToolFailure` 为什么同时存在？工具抛异常 vs 返回失败结果，行为有何不同？
    - 代码：[tools.py：ToolResult](../src/mini_openharness/tools.py#L174)、[ToolFailure](../src/mini_openharness/tools.py#L146)
17. **问题**：`ToolDescriptor` 里的 `effect` / `destructive` / `path_argument` 分别解决什么问题？
    - 代码：[tools.py：ToolDescriptor](../src/mini_openharness/tools.py#L127)
18. **问题**：`ToolContext` 为什么带 workspace、权限策略、审批回调、Trace、超时、文件快照这一整套？
    - 代码：[tools.py：ToolContext](../src/mini_openharness/tools.py#L116)
19. **问题**：默认权限策略下，读类工具（list_files/read_file）和写类工具（write_file）审批有何不同？`--allow-write` / `--yes` / `--permission-config` 分别改变什么？（在 [cli.py](../src/mini_openharness/cli.py) 里找 `_permission_policy` / `_approval_callback`）

**过关标志**：能手绘 P1-12 的执行阶段图，并回答 P1-14 的 `read_only: bool` 问题。

---

## P1 — 可观测性：Trace（[trace.py](../src/mini_openharness/trace.py)）

20. **问题**：`LocalJsonlTraceSink` 为什么能"并发安全"地从多个工具任务里写？（锁 + 每行原子追加）
    - 代码：[trace.py：LocalJsonlTraceSink](../src/mini_openharness/trace.py#L41)
21. **问题**：写入用的是 `O_APPEND` + `os.write`，为什么不用 `open()` 的 Python 层缓冲？半行容错在 `TraceStore.read` 怎么做的？
    - 代码：[trace.py：read()](../src/mini_openharness/trace.py#L230)
22. **问题**：`replay()` 为什么保证"绝不调用 provider 或 tool"？
    - 代码：[trace.py：replay()](../src/mini_openharness/trace.py#L257)
23. **问题**：`_redact` 是 best-effort 的，为什么不能当安全边界？哪些 key 会被遮掉？
    - 代码：[trace.py：_redact()](../src/mini_openharness/trace.py#L334)
24. **任务**：说出 trace 与 session 两个 JSONL 的**本质区别**（事件日志 vs 消息日志、各自解决什么问题）。

**过关标志**：能答 P1-21 的"为什么 O_APPEND + 为什么半行容错"。

---

## P1 — 会话持久化与恢复（[session.py](../src/mini_openharness/session.py)）

25. **问题**：`detect_interruption` 的四种状态各在什么情况下判定？为什么"最后一条是 user"代表 `interrupted_prompt`？
    - 代码：[session.py：detect_interruption()](../src/mini_openharness/session.py#L26)
26. **问题**：`dangling_tool_calls` 状态下 resume 前为什么要 `strip_dangling_tool_calls`？剥掉后模型会怎样重规划？
    - 代码：[session.py：strip_dangling_tool_calls()](../src/mini_openharness/session.py#L44)
27. **问题**：为什么选 append-only JSONL 而不是 SQLite？（**你的王牌叙事**：unfinished 分支做过 SQLite + effect-receipt，参考 Claude Code 源码后换回 JSONL + 中断检测，为什么？）
28. **问题**：会话文件每行是什么？首行 meta 头什么时候写、resume 时怎么不重写？进程被硬杀留下的半行怎么容错？
    - 代码：[session.py：SessionLog](../src/mini_openharness/session.py#L83)、[SessionStore](../src/mini_openharness/session.py#L147)
29. **问题**：CLI 的 `resume` / `continue` / `sessions` 各自做什么？`--no-session`、`--session-dir` 影响什么？
    - 代码：[cli.py：_resume_command()](../src/mini_openharness/cli.py#L193)、[sessions](../src/mini_openharness/cli.py#L392)
30. **问题**：SIGINT 和 SIGTERM 时系统各打印什么？为什么"无需 flush"？
    - 代码：[cli.py：_handle_sigterm()](../src/mini_openharness/cli.py#L557)
31. **问题**：resume 时剩余步数怎么算？`max(1, max_steps - 已有 assistant 数)` 防什么？
32. **问题**：你亲手修过的那个 bug：JSONL 记录如果不用换行符结尾会怎样？（整条文件拼成一行 → 解析失败）。这告诉了你什么设计教训？

**过关标志**：能不看代码讲完 P1-27 的"两版持久化对比"故事。

---

## P1 — Provider 抽象层（[provider.py](../src/mini_openharness/provider.py)）

33. **问题**：为什么定义 `Provider` 协议 + `OpenAICompatibleProvider` / `OpenAIResponsesProvider`？换一家模型要改哪里？
    - 代码：[provider.py：OpenAICompatibleProvider](../src/mini_openharness/provider.py#L94)、[OpenAIResponsesProvider](../src/mini_openharness/provider.py#L262)
34. **问题**：`complete()` 和 `stream()` 的区别？`ProviderTextDelta` / `ProviderComplete` 在流式里怎么用？
    - 代码：[provider.py：ProviderTextDelta](../src/mini_openharness/provider.py#L56)、[ProviderComplete](../src/mini_openharness/provider.py#L68)
35. **问题**：`ProviderError` 及其子类（认证、超限、上下文窗口）在 engine 里分别被怎么处理？为什么上下文窗口错误会触发 compaction/retry？
36. **问题**：`DemoProvider` 为什么能"确定性"跑完整个链路？它和真实 provider 的差别说明了什么？
    - 代码：[provider.py：DemoProvider](../src/mini_openharness/provider.py#L529)

**过关标志**：能答 P1-33 的"换供应商只改一处"。

---

## P2 — 知道存在、一句式能说清

37. [models.py：Message/ToolCall/ModelReply](../src/mini_openharness/models.py#L20)：为什么定义供应商无关的内部消息模型？（反序列化 round-trip 靠 `to_dict/from_dict`）
38. [hooks.py：HookRegistry/HookExecutor](../src/mini_openharness/hooks.py#L193)：生命周期扩展点，`USER_PROMPT_SUBMIT` 等事件在哪个时机触发、blocked 会怎样。
39. [skills.py：SkillCatalog/LoadSkillTool](../src/mini_openharness/skills.py#L20)：技能怎么注入 system prompt、`LoadSkillTool` 怎么按需加载完整指令。
40. [compaction.py：ContextCompactor/ArtifactStore](../src/mini_openharness/compaction.py#L33)：阈值触发压缩、保留最近 N 轮；Artifact 落盘位置。
41. [mcp.py：McpManager](../src/mini_openharness/mcp.py#L37)：stdio/HTTP 外部工具怎么注册进 `ToolRegistry`。
42. **问题**：`.mini-oh` 目录为什么不会被 agent 自己的工具读到？怎么保证的？（tools 排除规则）

**过关标志**：P2 每项都能一句话说出"做什么 + 为什么存在"。

---

## 面试叙事（必背，不看稿讲顺）

- **N1**：两版持久化对比（SQLite+effect-receipt vs JSONL+中断检测）→ 为什么最终选后者 → 这体现什么判断力。
- **N2**：完整跑一遍 `--demo`，边跑边讲每一步在做什么（这就是你的"现场 demo 脚本"）。
- **N3**：一个真实修过的 bug（换行符 bug / 生成器 aclose bug），讲清：现象 → 排查 → 根因 → 修复 → 预防。

---

## 模拟追问自测（答不上就回补对应节）

1. "你这个和 Claude Code 的区别是什么？" → P1-27 + P2。
2. "如果两个工具同时写同一个文件会怎样？" → P1-14/15。
3. "resume 会不会无限续跑？" → P1-31。
4. "trace 会不会泄密？" → P1-23。
5. "模型给你一个非法 JSON 参数会怎样？" → P1-13。
6. "你删掉一条中间消息，模型会怎么反应？" → P1-25/26。
7. "并发会话怎么做？" → P0-6。
8. "上下文爆了怎么办？" → P0-11 + P2-40。

---

## 建议节奏（3 天）

- **Day 1**：P0 全部 + N2（跑 demo 并讲顺）。
- **Day 2**：P1 的权限 + trace + session + provider（按你的深挖点分配）。
- **Day 3**：P2 一句式 + 三个叙事背顺 + 找人模拟追问。

> 进度建议：每过一节勾掉，勾不掉的条目就是下一次优先补的。别贪多——P0 + 任意两个 P1 深挖点 + N1 叙事，已经超过多数候选人的深度。
