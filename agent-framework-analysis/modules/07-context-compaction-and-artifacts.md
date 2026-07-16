# 上下文压缩与 Artifact offload

> 分析状态：已验证  
> 优先级：P1  
> 模块类型：Infrastructure  
> 主要代码：`src/mini_openharness/compaction.py`；`engine.py::_compact_if_needed/_offload`

## 1. 模块职责与边界

**已确认**：该模块用两个互补机制控制模型上下文：旧 history 变成确定性 summary，大工具输出完整落盘而只将头尾/路径写入 history；`compaction.py:15-120`。

它不持久化通用 Session，不做语义检索，也不调用 LLM 生成 summary。

## 2. 独立模块依据

- 对 conversation state 进行独立、可测试的转换。
- 维护 tool-call/result 原子性，这是 Provider 协议不变量。
- Artifact 有独立文件生命周期和原子替换。
- 在正常阈值路径与 Provider context error 恢复路径中都有完整调用阶段。

## 3. 对外接口

- `estimate_tokens(messages)`：字符数加 tool call 文字后除 4，最小 1。
- `ContextCompactor(threshold_tokens, keep_recent_units).compact(messages, force=False)`。
- `CompactionResult`：新 messages、是否压缩、前后估算、被摘要消息数。
- `ArtifactStore(root,max_inline_chars).offload(run_id,tool_call_id,output)`。

这些类未从包根导出，但通过 CLI 参数启用并可程序化注入 AgentLoop。

## 4. 压缩算法

1. 估算当前 tokens；低于阈值且非 force，或消息少于 4，不处理。
2. 分离首个 system Message。
3. `_atomic_units()` 将普通消息各自成单元；含 calls 的 assistant 与随后所有 tool results 合为一个单元。
4. 保留最近 `keep_recent_units`；若没有足够旧单元，不压缩。
5. 旧消息由 `_summarize()` 变成额外 system Message。
6. 返回原 system + summary + recent units。

Summary 最多取每条 content 的规范化前 300 字，并列出 assistant 请求的 tool names；`compaction.py:111-120`。

## 5. 两种触发方式

- **阈值式**：每个 model step 前 `_compact_if_needed()`；超过 threshold 时压缩。
- **反应式**：Provider 抛 typed ContextWindowError 后 `force=True`，忽略 threshold，在同一 step 最多重试一次；`engine.py:168-170,222-240`。

Force 仍不会破坏 `keep_recent_units` 或在无旧单元时强行压缩。

## 6. Artifact offload

输出不超过阈值时原样返回。过大时：

- 按 run ID 建目录；sanitize tool call ID；
- 写 `.tmp` 后 `os.replace()`；
- history 只保留一半 head、一半 tail、被省略字符数和实际路径；
- Engine 将 path/original chars 放入 ToolResult metadata；`compaction.py:61-85`、`engine.py:351-357`。

没有 Trace 时 run ID 固定为 `untraced`，不同运行同 call ID 可能覆盖 artifact；`engine.py:537-541`。

## 7. 调用关系与状态

- 依赖 domain Message 与本地文件系统。
- 上游是 AgentLoop，CLI 只负责构造参数。
- Compactor 无内部可变状态；ArtifactStore 只持 root/threshold。
- AgentLoop 是唯一决定替换 `self.messages` 的 owner。

## 8. 关键技术原理

- **Protocol-preserving compaction**：以工具 turn 为原子单元，避免 orphan function output。
- **Deterministic summary**：无网络、可复现、低成本，但语义保真有限。
- **Reactive recovery**：Provider 的 typed signal 驱动一次强制降载，而不是盲目重试所有 400。
- **Externalization**：完整大输出留在 artifact，模型仅看到摘要视图。

## 9. 扩展方式

文档声称可换 LLM summarizer。当前 AgentLoop 只要求对象有 `compact(messages,force=...)` 并返回相同字段，duck typing 可行，但没有正式 Protocol，也不支持 async compact。要正式扩展应先定义 `Compactor` Protocol 和 conformance tests。

ArtifactStore 同理可替换成对象存储 adapter，但 Engine 目前期望返回本地 `Path | None`，接口偏向文件系统。

## 10. 错误与边界情况

- token 估算不考虑 tokenizer、schema/system overhead，可能早/晚压缩。
- summary 截断可能丢失关键约束和旧工具结果语义。
- artifact 默认权限取决于 umask，内容不做 secret redaction/encryption。
- inline 文本包含宿主绝对路径并会发送给 Provider。
- `safe_id` 可能碰撞；写失败会打断 Agent run。
- incomplete tool turn 被保持为单元，但注释里的 pending 不触发额外保护逻辑。

## 11. 测试依据

- `tests/test_compaction.py::test_compaction_keeps_recent_tool_call_and_all_results_together`
- `test_large_output_is_fully_preserved_as_artifact`
- `test_forced_compaction_ignores_threshold_but_preserves_recent_units`
- `tests/test_engine.py::test_agent_loop_offloads_large_tool_output_before_next_model_call`
- `test_context_error_forces_one_compaction_and_retries_same_model_step`
- `test_context_error_without_compactable_history_fails_without_looping`

## 12. 设计评价与阅读建议

- 值得学习：原子单元优先于摘要质量；threshold 与 reactive recovery 分层；完整输出不丢失。
- 潜在问题：具体类接口、粗 token 估算、artifact 保密/覆盖和绝对路径泄露。
- 改进方向：Compactor Protocol、tokenizer adapter、结构化 summary、artifact URI/ACL/TTL、无 Trace 的唯一 run ID。
- 精读：`ContextCompactor.compact`、`_atomic_units`、`_summarize`、`ArtifactStore.offload`、`AgentLoop._compact_if_needed/_offload`。
