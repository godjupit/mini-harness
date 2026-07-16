    # 第 12 章：理解上下文压缩与 Artifact 大输出存储

    > 适用对象：刚开始学习 Python、第一次接触 Agent（智能体）运行框架的读者。  
    > 对应项目：`mini-openharness` 0.6.0。  
    > 建议做法：一边打开源码，一边按本章给出的行号和问题阅读。

    ## 1. 本章目标

阅读：

```text
src/mini_openharness/compaction.py
ContextCompactor 33～58 行
ArtifactStore    61～85 行
_atomic_units    88～108 行
```

## 2. 为什么上下文会变长

每轮都会追加：

```text
用户消息
模型回复
工具调用
工具结果
下一轮回复
```

读取一个大文件可能一次加入数万字符。模型都有上下文窗口限制，输入过长会失败并增加成本。

## 3. token 估算

项目用近似规则：

```python
characters // 4
```

这不是精确 tokenizer，但计算便宜，适合作为提前压缩阈值。精确性不是这里的首要目标，稳定和低成本更重要。

## 4. 什么时候压缩

```text
未强制且估算 token ≤ threshold → 不压缩
消息少于 4 条                → 不压缩
原子单元数量不足             → 不压缩
否则：总结旧单元，保留最近单元
```

保留原 System Message，再增加一条压缩摘要 System Message。

## 5. 为什么不能按消息条数随便截断

假设历史：

```text
assistant: 调用 call-1 和 call-2
tool: call-1 的结果
tool: call-2 的结果
```

如果只保留最后两条，模型会看到工具结果，却看不到是谁请求的；若只保留 assistant 和一个结果，又会缺失另一个调用结果。

因此 `_atomic_units()` 把一条带工具调用的 assistant 消息和其后连续的 tool 消息视为不可拆分单元。

## 6. 摘要是确定性的

`_summarize()` 不调用另一个模型，而是按规则生成：

```text
[Compacted conversation summary]
- user: ...
- assistant requested tools: read_file
- tool: ...
```

优点：

- 不增加模型调用费用；
- 测试可重复；
- 不会因摘要模型波动改变行为。

缺点：摘要质量较简单，可能丢失细节。这是小型框架的明确取舍。

## 7. 主动压缩与被动恢复

### 主动

每个模型步骤前，根据阈值检查。

### 被动

Provider 明确返回上下文超限时，Engine `force=True` 强制压缩并重试同一步。每次 run 只进行一次这种反应式恢复，避免无限循环。

## 8. ArtifactStore 解决另一类问题

上下文压缩针对“历史太长”；ArtifactStore 针对“单次工具输出太大”。

若输出超过 `max_inline_chars`：

1. 完整内容写入 `.mini-oh/artifacts/<run-id>/<call-id>.txt`；
2. 消息中只保留头部和尾部；
3. 中间插入 Artifact 路径和省略字符数；
4. ToolResult metadata 记录原始长度和路径。

## 9. 为什么使用临时文件 + os.replace

```python
temporary.write_text(output)
os.replace(temporary, path)
```

这是原子写入模式：先写临时文件，成功后一次替换正式文件。若写入中途失败，不容易留下一个看似完整但内容残缺的目标文件。

## 10. Artifact 不是删除信息

完整输出仍保存在文件中，只是不全部塞进模型上下文。因此系统同时获得：

```text
模型可继续工作的小型内联摘要
人或后续工具可读取的完整证据
```

## 11. 本章练习

1. 为什么 assistant 工具调用与 tool 结果要作为原子单元？
2. 阈值估算不精确会不会让设计完全失效？
3. ArtifactStore 与 ContextCompactor 分别解决什么问题？

## 12. 参考答案

1. 防止破坏模型工具协议的配对完整性。
2. 不会；它只是提前触发策略，重要的是大体单调、便宜和可预测。
3. 前者处理单次大输出，后者处理累积对话历史。
