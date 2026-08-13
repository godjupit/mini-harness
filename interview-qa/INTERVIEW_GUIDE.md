# Mini OpenHarness 面试问题

1. 请完整讲述用户提交 Prompt 后，`AgentLoop` 从接收输入到最终返回 `done` 的调用流程。

2. 模型返回 Tool Call 后，系统会经过哪些步骤执行工具并将结果回填给模型？

3. 为什么 Agent 需要采用 model → tools → observations → model 的循环，而不是只调用一次模型？

4. 工具执行失败时，为什么 Agent 通常不会立即退出，而是把错误结果回填给模型？

5. Tool 错误和 Provider 错误在语义和处理方式上有什么区别？

6. `ConversationState` 和 `RunState` 分别保存什么状态，为什么必须将它们分开？

7. 为什么同一个 `AgentLoop` 不允许两个 `run()` 同时执行？如果需要并发会话应该怎么做？

8. `max_steps` 和重复 Tool Batch 检测分别防止什么问题？

9. 调用方提前关闭 async generator 时，系统如何保证 active run 和相关资源被正确清理？

10. `Message`、`ToolCall` 和 `ModelReply` 分别承担什么职责？

11. Assistant Tool Call 和 Tool Result 如何通过 `tool_call_id` 正确配对？

12. 为什么需要定义与具体模型供应商无关的内部消息模型？

13. 一次 Tool Call 从进入 `ToolRegistry.execute()` 到返回结果，会依次经历哪些阶段？

14. 为什么模型生成的工具参数仍然必须由 Runtime 使用 JSON Schema 再次校验？

15. `Tool` Protocol 和 `ToolRegistry` 分别解决了什么问题？

16. `ToolContext` 为什么要包含 workspace、权限策略、审批回调、Trace、超时和文件快照？

17. `ToolDescriptor` 中的 `source`、`source_id`、`effect`、`destructive` 和 `path_argument` 分别有什么作用？

18. 为什么不能只使用一个 `read_only: bool` 描述工具的安全属性？

19. `ToolResult` 和 `ToolFailure` 为什么需要同时存在？

20. `ToolFailure` 中的 `code`、`stage`、`retryable` 和自然语言错误信息分别服务于谁？

21. 为什么未知工具、未知 effect 或资源解析失败时要采用 fail-closed 策略？

22. 如何新增一个工具，使权限系统、资源调度和 Trace 都能正确识别它？

23. 为什么多个 Tool Call 不能简单地全部交给 `asyncio.gather()` 并发执行？

24. `Semaphore` 和 `ResourceLockManager` 分别解决什么问题？

25. 资源锁中的 read/read、read/write 和 write/write 分别是否冲突，为什么？

26. tree lock 是什么，为什么目录读取需要与子文件写入冲突？

27. 两个不同文件的写操作能否并发执行，当前实现如何判断？

28. 一个工具如果同时读取文件 A、写入文件 B，应该如何声明资源并避免死锁？

29. 当前资源冲突检测的时间复杂度是多少，生产环境可以怎样优化？

30. 当前资源锁是否可能产生写饥饿，应该怎样增加公平性？

31. 任务在等待并发槽、资源锁或工具执行期间被取消时，系统应该如何清理？

32. `_safe_path()` 如何防止 `../secret` 之类的路径穿越？

33. 为什么文件工具需要禁止读取 `.env` 和 OAuth Token 等运行时秘密？

34. 为什么 `EditFileTool` 要求先读取文件或显式提供 `expected_sha256`？

35. 文件在 Agent 读取后被用户修改，系统如何避免覆盖用户的新内容？

36. 什么是乐观并发控制，它在 `EditFileTool` 中是如何实现的？

37. 为什么安全编辑不能只调用 `write_text()`，而要使用临时文件和 `os.replace()`？

38. 文件 `fsync`、目录 `fsync` 和原子替换分别提供什么保证？

39. 当前文件编辑方案是否完全消除了 TOCTOU 风险，还可以怎样加强？

40. 权限系统中的 allow、deny 和 ask 分别表示什么？

41. 权限规则的匹配顺序是什么，为什么显式 deny 不能被 `allow_write` 覆盖？

42. 为什么只读工具可以默认允许，而写工具和远程 MCP 工具通常默认询问？

43. 非交互环境无法进行人工审批时，ask 决策应该如何处理？

44. Permission、Hook、Resource Lock 和 Sandbox 分别解决什么问题，为什么不能互相替代？

45. Hook 为什么是受信任的生命周期扩展点，而不是由模型自主选择的普通工具？

46. `user_prompt_submit`、`pre_tool_use`、`post_tool_use` 和 `stop` 四种 Hook 分别适合做什么？

47. Pre Tool Hook 修改工具参数后，Schema 校验、权限判断和资源锁应该使用修改前还是修改后的参数？

48. Stop Hook 拒绝 Agent 完成后，系统为什么要让模型继续修复而不是直接终止？

49. Hook 的 matcher、priority 和注册顺序如何共同决定执行顺序？

50. Hook 的 fail-open 和 fail-closed 分别适合什么场景？

51. 为什么 Command Hook 不经过 shell，并且默认不继承完整宿主环境？

52. 为什么 Runtime 需要 Provider 抽象，而不能在 `AgentLoop` 中直接处理 OpenAI 响应？

53. Chat Completions 和 Responses API 如何转换为统一的内部消息和事件？

54. 流式返回的文本和 Tool Call arguments 应该如何聚合与最终确认？

55. 401、429、500、timeout、network error 和 context-window error 应该如何分类处理？

56. Provider 的指数退避重试是如何工作的？

57. 为什么 Provider 已经输出部分文本后不能再进行透明重试？

58. 为什么不能把所有 HTTP 400 都识别为上下文窗口超限？

59. 模型输出被截断时，为什么不能进入正常的 `done` 路径？

60. 用户取消请求时，Provider 如何停止流式请求并向 Agent 返回明确的取消状态？

61. 为什么上下文压缩不能简单删除最早的若干条 Message？

62. 为什么 Assistant Tool Call 和它对应的全部 Tool Result 必须作为一个原子单元保留或压缩？

63. 基于阈值的主动压缩和收到 context-window 错误后的强制压缩有什么区别？

64. 为什么 context-window 错误恢复只能进行受控次数的重试？

65. 为什么大型工具输出应该保存到 Artifact，而不是完整保留在消息历史中？

66. 确定性摘要与 LLM 摘要分别有什么优点、缺点和适用场景？

67. Trace 应该记录模型、工具、权限、Hook、资源锁和运行状态中的哪些关键事件？

68. 为什么工具参数、工具输出和 Provider 请求不能未经处理直接写入 Trace？

69. Trace 的 best-effort 和 strict 模式分别适合什么场景？

70. 如果工具已经产生副作用，但 Trace 写入失败，系统应该如何处理？

71. 为什么 Trace Replay 只能读取和渲染历史事件，不能重新调用模型或执行工具？

72. Trace 的 `finish()` 为什么需要具备幂等性？

73. MCP Server 暴露的工具如何注册到现有 `ToolRegistry`，而不需要修改 `AgentLoop`？

74. 为什么 MCP 工具需要记录 server 归因，并使用更保守的权限默认值？

75. MCP 的 input schema、output schema 和工具注解为什么都不能被客户端无条件信任？

76. stdio MCP 和 Streamable HTTP MCP 在连接、生命周期和安全边界上有什么区别？

77. OAuth PKCE、state 和 resource audience 分别防御什么风险？

78. OAuth Token 应该如何存储，为什么不能进入模型上下文或普通 Trace？

79. Skill 为什么只在初始 Prompt 中提供名称和描述，而在使用时才加载完整正文？

80. Skill 的渐进式加载如何减少上下文占用，并保持能力可发现性？

81. Docker Sandbox 主要限制哪些宿主资源和执行能力？

82. Docker 不可用时，为什么 Sandbox 应该 fail-closed，而不是自动回退到宿主 shell？

83. Sandbox 中的命令超时后，如何保证进程和容器被正确清理？

84. 如果需要为这个项目增加多 Agent 协作，你会如何设计状态、消息和调度边界？

85. 如果远程工具不支持幂等操作，如何避免网络重试造成重复副作用？

86. 如果恶意 MCP Server 返回超大内容、错误 Schema 或敏感数据，Runtime 应如何防御？

87. 如果上下文摘要丢失了重要约束，系统应该如何检测和缓解？

88. 如何把当前单机 JSONL Trace 扩展成生产级分布式可观测系统？

89. 你会如何对 Provider、工具、权限、锁、Sandbox 和 Trace 进行故障注入测试？

90. 如果要把 Mini OpenHarness 演进成生产级 Coding Agent Runtime，你认为最优先需要补充哪三项能力，为什么？
