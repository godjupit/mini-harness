# 工具能力边界与 effect-aware 调度

> 分析状态：已验证  
> 优先级：P0  
> 模块类型：Core  
> 主要代码：`src/mini_openharness/tools.py`；`engine.py::AgentLoop._execute_timed/_execute_all`

## 1. 模块职责

**已确认**：该模块把模型请求的 capability 转为受控副作用：Tool Protocol/Registry 负责发现、Schema、权限、timeout 和错误归一化；ResourceAccess/LockManager 与 Engine 批调度负责只串行真正冲突的调用。

它不决定模型何时调用工具，不实现组织级 Hook，不提供跨进程锁，也不保证恶意 Tool 实现的安全隔离。

## 2. 为什么它是独立模块

- `Tool` 是公开扩展形状，本地/Skill/MCP/Docker 全部实现它。
- `ToolRegistry` 拥有独立注册状态和重复名称异常。
- `ResourceLockManager` 拥有独立 async condition/active locks 生命周期。
- 工具执行是主循环中的完整阶段，有专项权限、schema、timeout、并发与路径测试。
- 虽横跨 `tools.py` 和 `engine.py`，两处共同实现同一个 tool-call lifecycle，合并分析更能还原真实边界。

## 3. 对外接口

- `Tool` Protocol：name、description、parameters、read_only、async `run()`；`tools.py:95-103`。
- `ToolContext`：workspace、legacy allow_write、Policy、approval、tracer、timeout。
- `ToolResult`：字符串 output、is_error、metadata。
- `ToolRegistry.register/schemas/source/is_read_only/resources/execute`。
- `ResourceAccess(key, mode, tree)`：一次调用声明的逻辑资源。
- `default_tools()`：注册 read_file、list_files、write_file。

包根只导出 ToolRegistry/default_tools，Tool/Context/Result 需从模块导入。

## 4. 内部实现

### 注册与模型可见 Schema

Registry 按 name 保存对象，拒绝重复；`schemas()` 只向模型暴露 name/description/parameters，不暴露 read_only/resources/source；`tools.py:105-122`。

### Effect/resource resolution

Tool 可选实现 `resources(arguments, context)`。解析失败、返回空/非法或工具未知时采用保守 fallback：只读已知工具锁 `tool:name` read，mutation/unknown 使用全局 `*` tree write；`tools.py:136-156`。

### 层级读写冲突

read/read 兼容；相同 key、全局 key 或 tree 与后代 key 在至少一方 write 时冲突；`tools.py:64-75`。文件工具使用规范化绝对 `fs:` key，目录 list 使用 tree read，shell 使用 workspace tree write。

### 统一执行栈

`ToolRegistry.execute()` 顺序是：查找 → JSON Schema validate → Policy evaluate → 可选 approval → trace permission → deny observation 或 `wait_for(tool.run)` → 把 timeout/常见异常/未知异常转为 error ToolResult；`tools.py:158-215`。

### Engine 外围调度

`_execute_all()` 并发启动所有 call，`_execute_timed()` 先执行 pre Hook，再按改写后参数计算资源、获取锁、调用 Registry、释放锁，最后执行 post Hook。`gather()` 保持结果顺序；`engine.py:381-521`。

## 5. 输入、输出与副作用

- 输入：ToolCall name/arguments 与 ToolContext。
- 输出：任何正常/已知失败都收敛为 ToolResult。
- 状态：Registry tools map；LockManager active resources。
- 副作用：由 Tool 实现决定；Registry 本身产生 approval/Trace。
- 异常：重复注册直接抛；execute 尽量捕获并转成 observation；CancelledError 不被 `except Exception` 捕获，向上传播。

## 6. 调用关系

- 上游：AgentLoop；CLI/MCP/Skill/Sandbox 在 run 前注册工具。
- 下游：PermissionPolicy、TraceWriter、jsonschema、具体 Tool。
- Adapter 反向依赖 Tool contract，保持核心不 import MCP/Docker/Skill class。
- 例外：Engine 用名称识别 `mcp__` 与 `load_skill` source 细节，形成轻度反向知识泄漏。

## 7. 核心执行顺序

```text
ToolCall
→ pre Hook（可改参数/阻断）
→ resolve ResourceAccess
→ wait/acquire lock
→ lookup + JSON Schema
→ PermissionPolicy + optional approval
→ wait_for Tool.run
→ normalize result/error
→ release lock
→ post Hook（可改结果/阻断）
→ Artifact/history
```

批内所有 call 同时进入这条链，只有资源冲突段会等待。

## 8. 关键技术原理

### Capability 与 effect 分离

Tool schema 描述“能调用什么”，read_only/resource 描述“调用会影响什么”。Permission 回答能否执行，resource lock 回答何时可并行，两者不可互替。

### Hierarchical RW lock

使用 condition + active set 做 O(requested×active) 冲突检测。每个调用的资源排序，当前一次性整体申请，因此没有逐锁顺序死锁；但没有严格 FIFO 公平性。

### Fail-closed metadata

未知 Tool、resolver 错误或不可信 effect 都按全局 mutation 处理，避免错误并行或默认放行。

### Error as observation

Schema、permission、timeout、Tool exception 都转换为字符串结果，使 Agent 能换参数/工具，而不是打断状态机。

## 9. 扩展方式

新增 Tool 需要：

1. 唯一 name、清晰 description、JSON Schema；
2. 准确 `read_only`；
3. async `run(arguments, context) -> ToolResult`；
4. 建议实现 `resources()`，尤其 mutation；
5. 注册到 ToolRegistry；
6. 为 schema、permission、timeout、资源冲突和副作用边界写测试。

若不实现 resources，read-only tools 仅按 tool name 共享 read lock；mutation 会全局串行。

## 10. 默认本地工具

- `ReadFileTool`：workspace containment、secret deny、异步线程读取、精确 file read lock。
- `ListFilesTool`：递归最多返回 500 个文件，过滤运行/缓存目录与 secrets，目录 tree read lock。
- `WriteFileTool`：workspace containment、创建父目录、线程写入、精确 file write lock。

**已确认**：workspace containment 使用 `resolve()+relative_to()`，可阻止常见 `..` 和解析后逃逸；`tools.py:218-225`。

## 11. 错误与边界情况

- `write_file` 不是原子写；并发同文件通过进程内锁串行，但进程崩溃可能留下部分内容。
- 锁仅在一个 AgentLoop 内；不同 Loop/进程写同一路径不协调。
- `extract_path()` 只识别 path/file_path/root，自定义 Tool 的其他路径字段不能用 path glob 精细授权。
- `resources()` 可能在 pre Hook 后再次对路径做 resolve 并抛；Registry 对 resolver 的有限异常 fail-closed，但 Engine 后续仍正常获取 fallback。
- Tool 超时只取消协程；实现若吞取消或把工作交给不可取消线程，副作用可能继续。
- `gather()` 未使用 `return_exceptions=True`；理论上的外围未捕获异常会影响整批。

## 12. 测试依据

- `tests/test_tools.py::test_json_schema_is_enforced_before_tool_execution`
- `test_tree_read_lock_blocks_child_write_until_release`
- `test_read_cannot_escape_workspace`
- `test_runtime_secrets_are_hidden_from_file_tools`
- `tests/test_engine.py::test_parallel_tool_calls_preserve_result_order`
- `test_mutating_tool_batch_is_serialized`
- `test_non_conflicting_mutations_run_in_parallel`
- `test_unknown_tool_becomes_observation`
- `tests/test_mcp.py::test_mcp_adapter_uses_same_permission_and_registry_path`

## 13. 设计评价

- 值得学习：effect-aware 并发比 read-only/mutation 二分更精确；改写后重新计算资源与权限；失败闭合为 observation。
- 复杂度来源：Tool lifecycle 分散在 Engine 和 Registry，Hook/lock/permission/timeout 顺序必须保持。
- 潜在问题：source 使用名字约定；公平性有限；锁范围只在单 Loop；path permission 用字段猜测。
- 改进方向：结构化 ToolMetadata（source/effects/permission resources）、独立 ToolScheduler、原子文件写、跨 Loop lock service 或明确单实例范围。

## 14. 阅读建议

精读 `ToolRegistry.execute`、`resources`、`ResourceLockManager.acquire`、`_resources_conflict`、`AgentLoop._execute_timed/_execute_all`、`_safe_path` 和三个默认工具的 `resources()`。
