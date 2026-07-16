    # 第 13 章：理解 Trace、可观测性与安全回放

    > 适用对象：刚开始学习 Python、第一次接触 Agent（智能体）运行框架的读者。  
    > 对应项目：`mini-openharness` 0.6.0。  
    > 建议做法：一边打开源码，一边按本章给出的行号和问题阅读。

    ## 1. 本章目标

阅读：

```text
src/mini_openharness/trace.py
TraceWriter 25～68 行
TraceStore  82～133 行
脱敏逻辑   152～202 行
```

## 2. 为什么 Agent 特别需要 Trace

普通函数可能输入一次、输出一次；Agent 运行包含多轮模型、并发工具、权限、Hook 和重试。只看最终回答很难知道：

- 模型请求了什么；
- 工具为何没执行；
- 哪一步等待很久；
- 完成为何被 Hook 拒绝；
- token 和成本是多少。

Trace 把运行过程变成可检查的事件序列。

## 3. JSONL 格式

每行一个 JSON 对象：

```json
{"sequence":1,"kind":"run_start",...}
{"sequence":2,"kind":"model_request",...}
{"sequence":3,"kind":"tool_start",...}
```

JSONL 相比一个巨大 JSON 数组的优点：

- 可以逐行追加；
- 运行中途崩溃，已写行仍保留；
- 适合流式处理和命令行工具。

## 4. TraceEvent 字段

```text
sequence    单个 run 内递增序号
timestamp   UTC 绝对时间
elapsed_ms  从 run 开始经过的毫秒
kind        事件种类
data        具体数据
```

`timestamp` 适合跨系统对时，`elapsed_ms` 适合性能分析。

## 5. 并发写入为什么使用线程锁

多个工具任务可能并发调用 `tracer.emit()`。`threading.Lock` 保护：

```text
sequence 增加
事件构造
文件追加
```

避免两个任务获得相同序号或写入内容交叉。

## 6. finish 的幂等性

```python
if self._finished_event is None:
    self._finished_event = self.emit("run_end", ...)
```

即使多个清理路径尝试结束 Trace，也只写一条 `run_end`。幂等表示重复调用不会造成重复副作用。

## 7. TraceStore

支持：

```text
list    汇总所有运行
read    读取一个 run 的事件
replay  渲染已记录事件
```

读取前严格检查 `run_id` 只含字母、数字、`-`、`_`，防止用 `../../` 读取任意文件。

## 8. Safe replay 不会重放副作用

```python
def replay(run_id):
    for event in read(run_id):
        yield render_event(event)
```

它只把记录格式化成文本，不重新调用 Provider 和 Tool。

因此：

```text
safe replay = 回看录像
不是 = 让演员重新表演
```

真正重新执行可能重复写文件、发送请求或调用外部系统，本项目明确不做。

## 9. 秘密脱敏

默认识别：

```text
api_key、password、secret、token
Bearer ...
sk-... 格式 Key
```

匹配后写为 `[REDACTED]`。

但代码注释强调这是 best-effort。不能因为有正则脱敏，就把 Trace 放到公开位置。安全还依赖文件权限、目录访问控制和不记录不必要数据。

## 10. 常见事件怎样串起来

```text
run_start
model_request
assistant_delta...
model_response
tool_start
permission_decision
resource_wait
resource_acquired
tool_end
model_request
run_end
```

你可以通过 `sequence` 还原逻辑顺序，通过 `elapsed_ms` 分析耗时。

## 11. 本章练习

1. 为什么 JSONL 适合长时间运行的 Agent？
2. Safe replay 为什么不会重复写文件？
3. 脱敏是否意味着 Trace 可以随意公开？

## 12. 参考答案

1. 可逐行追加，部分结果可保留，也便于流式读取。
2. replay 只读取和渲染事件，不调用工具。
3. 不能；脱敏只是尽力而为，仍需要访问控制和最小化记录。
