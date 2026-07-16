    # 第 17 章：用其余测试建立完整心智模型

    > 适用对象：刚开始学习 Python、第一次接触 Agent（智能体）运行框架的读者。  
    > 对应项目：`mini-openharness` 0.6.0。  
    > 建议做法：一边打开源码，一边按本章给出的行号和问题阅读。

    ## 1. 本章目标

前面按实现模块阅读；最后一章按行为类别阅读测试。目标不是追求覆盖率数字，而是理解每个测试保护哪条架构不变量。

运行全部测试：

```bash
pytest -q
```

运行某个文件：

```bash
pytest -q tests/test_permissions.py
```

运行单个测试：

```bash
pytest -q tests/test_engine.py::test_unknown_tool_becomes_observation
```

## 2. test_cli.py：入口和配置行为

重点验证：

- `.env` 会加载，但不覆盖终端中已有变量；
- 默认 API mode；
- Sandbox 必须显式开启；
- Provider 错误映射为非零退出码；
- 某些 404 给出兼容模式提示。

它保护“用户怎样启动程序”的契约。

## 3. test_engine.py：主状态机

建议顺序：

```text
test_model_tool_model_loop
→ test_unknown_tool_becomes_observation
→ test_max_steps_is_a_hard_guard
→ test_repeated_tool_batch...
→ test_cancel_stops_in_flight_tool_task
→ test_context_error_forces_one_compaction...
```

关键不变量：

- 工具结果按调用 ID 回填；
- 并发结果顺序保持；
- 工具错误可恢复；
- 无限循环有硬保护；
- 取消能停止正在执行的工具；
- 上下文错误只进行受控恢复。

## 4. test_tools.py：能力边界

重点：

```text
超时变成 observation
目录树读锁阻止子文件写
路径不能逃离 workspace
运行秘密不可读取和列出
写操作需要权限
Schema 在执行前校验
```

新增工具时，至少为路径、Schema、超时和权限补充测试。

## 5. test_permissions.py：策略顺序

保护：

- 显式规则先于默认；
- ask 回调实际参与决策；
- 权限决定写入 Trace；
- 显式 deny 不被 `allow_write` 覆盖；
- JSON 配置能正确加载。

读测试时特别观察预期的 `reason`，它体现系统是否可解释。

## 6. test_hooks.py：扩展不能破坏确定性

重点测试：

- 优先级顺序；
- payload 更新链；
- matcher；
- block 短路；
- 超时和 failure_mode；
- 外部命令不默认继承 API Key；
- pre/post 修改真实执行输入输出；
- Stop Hook 拒绝后 Agent 能恢复。

## 7. test_provider.py：网络边界

它通常使用 `httpx` 的假 Transport，不访问真实网络，却模拟：

```text
分片文本和工具参数
Responses typed items
输出截断
401/429/500
超时和网络异常
请求前已取消
重试发生在输出任何内容之前
```

Provider 测试的核心是：不让外部协议异常泄漏成模糊行为。

## 8. test_compaction.py：协议完整性

重点断言：

- 最近工具调用及全部结果保持在一起；
- 强制压缩忽略阈值但仍保护原子单元；
- 大输出完整保存到 Artifact 文件。

## 9. test_trace.py：证据和安全

验证：

- JSONL 可 list/show/replay；
- replay 无副作用；
- run_id 不能路径穿越；
- finish 幂等；
- 常见秘密默认脱敏。

## 10. test_skills.py、test_mcp.py、test_sandbox.py

### Skills

验证“先发现 metadata，正文只在 load 后读取”。

### MCP

验证配置、统一权限路径、输出 Schema、只读注解信任、OAuth Token 安全、真实传输。

### Sandbox

验证 Docker 参数确实包含隔离选项；有 Docker 时做真实集成测试，并确认超时后容器清理。

## 11. 如何用测试读一个新模块

固定步骤：

1. 先列出测试名；
2. 猜每个测试保护什么风险；
3. 看 Arrange 如何构造输入；
4. 看 Assert 最终观察什么；
5. 回源码寻找让断言成立的最少代码路径；
6. 临时改坏一行，确认测试真的失败，再撤销修改。

第 6 步叫 mutation-style learning：通过故意破坏理解测试的保护作用。只在 Git 分支或副本中操作。

## 12. 推荐的小白动手任务

### 任务 A：新增 file_size 工具

要求：

- 只读；
- 不可逃逸 workspace；
- 声明具体文件读资源；
- 参数缺失时由 Schema 拒绝；
- 为成功、文件不存在、路径逃逸写测试。

### 任务 B：新增禁止写 `.lock` 的权限规则测试

先写失败测试，再修改配置或策略。

### 任务 C：新增一个 post Hook

把工具输出中的指定词替换为 `[MASKED]`，验证模型第二轮看到的是替换后内容。

## 13. 最终自测题

1. 一次运行的主调用链是什么？
2. 工具错误和 Provider 错误为何处理不同？
3. 权限、Hook、资源锁和 Sandbox 各处于哪一层？
4. 为什么压缩必须保护工具调用原子单元？
5. Safe replay 为什么是安全的？

## 14. 答案要点

1. CLI 装配 → AgentLoop → Provider → Tool 执行链 → 消息回填 → 下一轮。
2. 工具错误通常是模型可观察并恢复的任务信息；Provider 错误可能意味着本轮无法取得任何有效模型回复。
3. 权限控制是否允许；Hook 扩展生命周期；资源锁协调并发副作用；Sandbox 限制系统执行环境。
4. 外部模型 API 要求 tool result 与先前 call 完整配对。
5. 它只读取记录并渲染，不重新执行 Provider 或 Tool。
