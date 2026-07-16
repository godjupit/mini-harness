# 权限策略与人工审批

> 分析状态：已验证  
> 优先级：P1  
> 模块类型：Cross-cutting  
> 主要代码：`permissions.py`；`tools.py:165-198`；`cli.py:226-250`

## 1. 模块职责与边界

**已确认**：权限机制在每次真实 Tool 执行前，以 tool name、read-only effect 和可选 path 产生 allow/deny/ask 决策；ask 可通过 async callback 请求人工确认；`permissions.py:30-84`、`tools.py:165-198`。

它不提供 OS sandbox，不判断资源并发冲突，也不执行组织级生命周期验证。Permission、Resource Lock、Hook 各回答不同问题。

## 2. 独立性依据

有独立规则状态、JSON loader、决策数据结构、async approval contract、CLI 默认策略和专项测试；所有本地/Skill/MCP/Docker Tool 共用，因此是横切模块。

## 3. 对外接口

- `PermissionRule(action,tool="*",path="*")`。
- `PermissionDecision(action,reason,rule)`。
- `PermissionPolicy(rules,default_mutation="ask")`。
- `PermissionPolicy.from_file/evaluate`。
- `ApprovalCallback(tool,reason) -> Awaitable[bool]`。
- `extract_path(arguments)`。

CLI 提供 `--permission-config`、`--allow-write`、`--yes` 与交互 TTY callback。

## 4. 决策顺序

```text
按配置顺序扫描第一条 tool/path glob 匹配规则
→ 命中：直接返回规则动作
→ 未命中且 read_only：allow
→ 未命中且 mutation/MCP：default_mutation
→ ask 且 callback 存在：等待人工 bool
→ ask 且无 callback：拒绝
```

显式 rule 优先于 `--allow-write`：CLI 读取配置后只替换 mutation default，不修改 rules；`cli.py:226-232`。

## 5. 与 Tool 执行的精确位置

Registry 先做 JSON Schema，再 evaluate。pre Tool Hook 更早，可改写 arguments；因此权限看到的是真实执行参数。允许后才进入 `Tool.run()`；decision 无论允许与否都进入 Trace；`engine.py:389-443`、`tools.py:165-203`。

Permission deny 不是 Runtime terminal error，而是 ToolResult error observation，模型可换方案。

## 6. 安全边界

- read_only 完全信任 Tool/adapter 的 effect 声明；本地 Tool 由代码控制，MCP hint 默认不信任。
- Path policy 只从 `path/file_path/root` 提取，其他字段无法精细匹配。
- `--yes` 批准所有 ask，但不能覆盖显式 deny。
- `--allow-write` 是兼容开关，仅改变 mutation default。
- 非交互环境没有 callback 时 ask 安全拒绝。

## 7. 扩展方式

可增加规则或自定义 approval callback。当前 Policy 是具体类，不支持异步外部 policy engine；若扩展 RBAC/OPA/tenant context，应定义 PermissionEvaluator Protocol，并让 Tool 提供结构化 permission resources，而非猜 path 字段。

## 8. 错误与边界情况

- JSON loader 对 payload/rules 元素结构验证有限，缺字段会抛原生异常。
- 第一匹配获胜，宽规则放前会遮蔽后面的精细规则。
- `fnmatch` 是字符串匹配，不规范化 path；实际文件工具之后会 resolve containment，但 policy 看到原始字符串。
- approval reason 包含完整 arguments，交互显示或自定义 callback 日志可能泄露内容。
- Permission 不能约束 Provider/Hook/MCP connection 建立等非 Tool 副作用。

## 9. 测试依据

- `tests/test_permissions.py::test_rules_match_tool_and_path_before_default`
- `test_ask_callback_and_decision_are_traced`
- `test_explicit_deny_overrides_allow_write`
- `test_permission_policy_loads_json_rules`
- `tests/test_tools.py::test_write_requires_explicit_permission`
- `tests/test_mcp.py::test_mcp_adapter_uses_same_permission_and_registry_path`

## 10. 设计评价与阅读建议

- 值得学习：默认最小权限、显式 deny 优先、拒绝作为 observation、MCP effect fail-closed。
- 潜在问题：effect/path metadata 过于简化、Policy 非 Protocol、approval 参数可能含秘密。
- 改进方向：typed permission request、normalized resource paths、规则冲突诊断、外部 policy adapter。
- 精读：`PermissionPolicy.from_file/evaluate`、`extract_path`、`ToolRegistry.execute` 权限段、CLI `_permission_policy/_approval_callback`。
