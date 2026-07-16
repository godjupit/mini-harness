    # 第 9 章：理解权限策略与人工审批

    > 适用对象：刚开始学习 Python、第一次接触 Agent（智能体）运行框架的读者。  
    > 对应项目：`mini-openharness` 0.6.0。  
    > 建议做法：一边打开源码，一边按本章给出的行号和问题阅读。

    ## 1. 本章目标

阅读：

```text
src/mini_openharness/permissions.py
src/mini_openharness/tools.py 的 execute()
examples/permissions.json
```

## 2. 为什么工具存在还不等于允许执行

工具注册表回答的是：

```text
系统是否具备这个能力？
```

权限策略回答的是：

```text
本次调用是否可以使用这个能力？
```

例如 `write_file` 已注册，但写入生产配置可能仍应被拒绝或人工确认。

## 3. 三种决策

```python
PermissionAction = Literal["allow", "deny", "ask"]
```

- `allow`：立即执行；
- `deny`：拒绝执行；
- `ask`：请求用户批准。

`PermissionDecision` 还记录 `reason` 和命中的 `rule`，便于 Trace 和解释。

## 4. 规则结构

```python
PermissionRule(
    action="deny",
    tool="write_file",
    path="*.env"
)
```

`tool` 和 `path` 使用 glob 匹配，例如：

```text
*              任意字符串
*.py           所有 Python 文件
src/*          src 下的路径
mcp__github__* 某个 MCP 服务器工具前缀
```

## 5. evaluate 的顺序

```text
按配置顺序检查显式规则
→ 第一条匹配规则立即返回
→ 若无规则且工具只读，默认 allow
→ 若为变更工具，使用 default_mutation
```

这里的“第一条匹配”意味着规则顺序有意义。更具体的规则通常放在更宽泛规则之前。

## 6. read_only 只是声明，不是万能安全证明

本地工具作者声明 `read_only=True`。MCP 工具是否只读更复杂，因此项目只有在明确配置“信任服务器注解”时才采用远程注解。

安全系统常使用原则：

```text
不确定时，不提升权限
```

## 7. ask 怎样与 CLI 配合

CLI 根据环境返回审批回调：

```text
--yes             自动批准
交互终端          显示 y/N
非交互环境        callback=None
```

ToolRegistry 中：

```python
if decision.action == "ask" and callback is not None:
    allowed = await callback(...)
```

如果没有回调，`allowed` 不会自动变成 True。这是安全默认。

## 8. --allow-write 不应覆盖显式 deny

CLI 的兼容选项 `--allow-write` 会把默认变更策略改为 allow，但从文件读取的显式规则仍优先。

```text
显式 deny 规则
  优先于
默认 mutation allow
```

因此“允许写入”不是“关闭全部安全规则”。

## 9. 权限拒绝为何返回 ToolResult

拒绝时：

```python
ToolResult(
    "Permission denied for write_file: ...",
    is_error=True
)
```

模型会看到这个 observation，可以解释给用户，或改为只读分析。权限系统本身没有让整个 Agent 进程崩溃。

## 10. 一个配置示例

```json
{
  "default": "ask",
  "rules": [
    {"action": "deny", "tool": "write_file", "path": ".env*"},
    {"action": "allow", "tool": "write_file", "path": "docs/*"},
    {"action": "allow", "tool": "read_*", "path": "*"}
  ]
}
```

阅读顺序：

1. 写 `.env`：命中 deny；
2. 写 `docs/guide.md`：命中 allow；
3. 其他写入：没有显式规则，使用 default ask；
4. 读取：命中 read allow，或者走只读默认 allow。

## 11. 权限、Hook、Sandbox 的区别

```text
权限：决定“能不能做”
Hook：在生命周期点检查、修改或阻止
Sandbox：即使允许做，也限制“在哪里、以什么系统权限做”
```

它们是多层防御，不互相替代。

## 12. 本章练习

1. 若规则第一条允许 `write_file *`，第二条拒绝 `write_file .env`，写 `.env` 会怎样？
2. 为什么 CI 中的 ask 不应自动允许？
3. 写一条规则，只允许写 `docs/*.md`。

## 13. 参考答案

1. 第一条先匹配，会允许；所以具体 deny 应放在宽泛 allow 前面。
2. 没有人真实批准，自动允许会把“需要确认”偷偷降级为“允许”。
3. `{"action":"allow","tool":"write_file","path":"docs/*.md"}`。
