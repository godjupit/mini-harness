    # 第 15 章：理解 MCP 工具桥接与 OAuth

    > 适用对象：刚开始学习 Python、第一次接触 Agent（智能体）运行框架的读者。  
    > 对应项目：`mini-openharness` 0.6.0。  
    > 建议做法：一边打开源码，一边按本章给出的行号和问题阅读。

    ## 1. 本章目标

阅读：

```text
src/mini_openharness/mcp.py
src/mini_openharness/mcp_auth.py
examples/mcp.json
examples/mcp-http-oauth.json
tests/test_mcp.py
```

这一章较难。小白先理解主线，不必第一次就掌握 OAuth 每个 RFC 细节。

## 2. MCP 解决什么问题

MCP（Model Context Protocol）让外部服务器以统一协议提供工具。服务器可能通过：

```text
stdio：本地启动一个子进程，通过标准输入输出通信
HTTP：连接远程 URL
```

Mini OpenHarness 的目标不是让 Engine 直接理解每种 MCP 传输，而是把远程工具包装为普通本地 `Tool`。

## 3. McpManager 的职责

```text
读取配置
→ 建立 stdio 或 HTTP 连接
→ 初始化 ClientSession
→ list_tools 获取远程工具
→ 为每个工具创建 McpTool
→ 注册进 ToolRegistry
→ 运行结束时关闭会话和连接
```

一旦注册完成，AgentLoop 看到的只是名称类似：

```text
mcp__server_name__tool_name
```

## 4. 为什么工具名要加服务器前缀

不同服务器可能都提供 `search`。加前缀避免冲突，也便于 Trace 和权限规则区分来源。

名称片段会被清理，只保留适合工具名的字符。

## 5. McpTool 仍走同一执行链

McpTool 提供：

```text
name
description
parameters
read_only
run()
```

所以它进入：

```text
pre Hook
→ 资源锁
→ Schema
→ 权限/审批
→ MCP session.call_tool
→ 输出校验
→ post Hook
```

这是本章最重要的结论：**外部工具没有成为安全旁路。**

## 6. MCP 输出也要验证

远程服务器可能声明 output schema。适配器对 structured content 做验证，并把结构化数据保留下来。远程返回不应因为来自“协议服务器”就自动可信。

## 7. 只读注解为何需要显式信任

MCP 工具可以带只读等注解，但服务器也可能误标或恶意标记。默认不信任时，远程工具按变更工具处理，需要更严格权限。

只有配置：

```text
trustToolAnnotations = true
```

才采用其只读声明。

## 8. stdio 与 HTTP 配置互斥

每个服务器必须且只能配置一个：

```text
command
或
url
```

两者都没有或都有都报错。配置在连接前校验，避免模糊行为。

## 9. HTTP Header 与环境变量

敏感 Header 可以通过环境变量注入，而不是直接写入 JSON 配置：

```json
{
  "headersEnv": {
    "Authorization": "MY_MCP_TOKEN"
  }
}
```

若环境变量缺失，配置加载立即失败。

## 10. OAuth 主流程

OAuth 场景大致是：

```text
发现授权服务器元数据
→ 注册或读取客户端信息
→ 生成 PKCE challenge 和 state
→ 打开浏览器授权
→ 本地 loopback callback 接收 code
→ 校验 state
→ 用 code 换 token
→ 保存 token，后续刷新
```

`state` 防止请求被调包；PKCE S256 防止授权码被截获后直接利用。

## 11. Token 存储安全

`FileOAuthStorage`：

- 拒绝符号链接；
- 使用原子更新；
- 尽量设置仅所有者可读写权限；
- 数据放在 `.mini-oh/oauth`；
- 文件工具和 Sandbox 对该目录做额外保护。

这是跨模块的纵深防御实例。

## 12. 远程 OAuth 为什么要求 HTTPS

除本机 loopback 等明确安全例外外，远程 HTTP 会让授权信息和 token 暴露在明文网络中，因此配置校验要求安全 URL。

## 13. 初学者第一遍可以跳过什么

第一次只需掌握：

```text
MCP Manager 连接服务器
McpTool 把远程工具包装成本地 Tool 协议
所有工具仍进入统一权限和执行链
OAuth 为 HTTP 服务提供安全授权
```

之后再读 PKCE、动态注册、scope step-up 等细节。

## 14. 本章练习

1. 为什么 MCP 工具不能直接绕过 ToolRegistry？
2. 为什么默认不信任远程只读注解？
3. Token 文件为什么同时需要原子写入和权限限制？

## 15. 参考答案

1. 统一 Schema、权限、Hook、锁、Trace 和错误处理，避免外部能力成为旁路。
2. 远程服务器的声明可能错误或恶意，错误信任会降低权限要求。
3. 原子写入防止损坏，权限限制防止其他用户读取秘密，两者解决不同风险。
