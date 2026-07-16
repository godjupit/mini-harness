    # 第 10 章：理解 Hooks——生命周期扩展与验证门

    > 适用对象：刚开始学习 Python、第一次接触 Agent（智能体）运行框架的读者。  
    > 对应项目：`mini-openharness` 0.6.0。  
    > 建议做法：一边打开源码，一边按本章给出的行号和问题阅读。

    ## 1. 本章目标

阅读：

```text
src/mini_openharness/hooks.py
examples/hooks-verification.json
tests/test_hooks.py
```

Hook 可以在不修改 AgentLoop 主流程的情况下，插入额外检查或转换。

## 2. 四个生命周期事件

```python
USER_PROMPT_SUBMIT = "user_prompt_submit"
PRE_TOOL_USE      = "pre_tool_use"
POST_TOOL_USE     = "post_tool_use"
STOP              = "stop"
```

时序：

```text
用户输入
→ user_prompt_submit
→ 模型请求工具
→ pre_tool_use
→ 工具执行
→ post_tool_use
→ 模型准备结束
→ stop
```

## 3. HookResult 能做什么

```python
HookResult(
    blocked=False,
    reason="",
    output="",
    updated_payload={}
)
```

- `blocked`：是否阻止当前阶段；
- `reason`：解释原因；
- `output`：Hook 自己的输出信息；
- `updated_payload`：修改传给后续 Hook 或运行时的数据。

例如 pre Hook 可把：

```json
{"tool_input": {"path": "a.txt"}}
```

改成：

```json
{"tool_input": {"path": "docs/a.txt"}}
```

后续资源锁、Schema、权限和真实工具都使用修改后的参数。

## 4. CallbackHook 与 CommandHook

### CallbackHook

把 Python 函数适配成 Hook。函数可同步也可异步，适合应用内部扩展和测试。

### CommandHook

启动外部子进程，将事件和 payload 作为 JSON 写入标准输入，根据退出码或 JSON 输出决定允许、阻止和修改。

它使用：

```python
asyncio.create_subprocess_exec(*command)
```

而不是通过 shell 拼接字符串，减少 shell 注入风险。

## 5. 为什么默认不继承完整环境变量

CommandHook 默认使用最小环境，只保留 `PATH`、语言、临时目录等必要变量。

这样外部验证脚本不会自动看到 `OPENAI_API_KEY` 等秘密。需要继承时必须明确配置 `inherit_environment=true`。

## 6. HookRegistry 的优先级

注册时保存 Hook，获取时按 `priority` 从高到低排序：

```python
sorted(hooks, key=lambda hook: -hook.priority)
```

多个 Hook 顺序执行。前一个成功更新的 payload 会传给下一个，这称为 payload chaining。

一旦某个 Hook 阻止，后面的 Hook 不再执行。

## 7. matcher

对于工具事件，matcher 匹配工具名；对于 prompt/stop 事件，匹配输入或回答文本。

例如：

```text
matcher = "write_*"
```

只作用于名称以 `write_` 开头的工具。

## 8. failure_mode

Hook 自己也可能超时或崩溃：

```text
failure_mode="block"    Hook 失败时阻止，安全优先
failure_mode="continue" Hook 失败时继续，可用性优先
```

验证安全关键操作时通常应 block；非关键日志 Hook 可 continue。

## 9. Stop Hook 是完成验证门

模型说“完成了”不代表测试真的通过。Stop Hook 可以运行检查脚本：

```text
退出码 0 → 接受完成
非 0     → 拒绝完成，把原因反馈给模型
```

AgentLoop 收到阻止后不会结束，而会要求模型修正并再次尝试。

这形成：

```text
生成答案 → 验证 → 失败反馈 → 修正 → 再验证
```

## 10. Post Hook 可以改写结果

工具真实输出完成后，post Hook 可：

- 去除敏感内容；
- 增加元数据；
- 把某个结果标记为错误；
- 完全阻止结果交给模型。

但 Hook 返回的数据必须满足类型要求，否则运行时把它转换为错误结果。

## 11. 本章练习

1. pre Hook 修改了工具路径，权限应检查旧路径还是新路径？
2. 为什么 Stop Hook 阻止完成后不直接报最终错误？
3. 一个仅用于记录统计的 Hook 应倾向哪种 failure_mode？

## 12. 参考答案

1. 新路径，因为它才是实际执行输入。
2. 验证失败通常可恢复，应把原因交回模型修正。
3. `continue`，避免非关键统计故障阻断主要任务。
