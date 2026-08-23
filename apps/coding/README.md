# Coding Agent

该目录与 Homestay Agent 使用相同的产品结构：

```text
coding/
├── config/       # System Prompt 与应用配置
├── skills/       # Coding 工作流
├── memory/       # 长期记忆索引
└── data/         # Session、Trace 和 Artifact
```

`data/` 是本地运行数据，不提交到 Git。入口 `../coding_agent.py` 会显式传入
Session、Trace 和 Artifact 路径。

## GitHub MCP

GitHub MCP 是 Coding App 的可选集成，不会在普通启动时自动拉起。配置
`GITHUB_PERSONAL_ACCESS_TOKEN` 并确保 Docker 可用后：

```bash
python apps/coding_agent.py \
  --mcp-config apps/coding/config/github-mcp.json \
  --workspace ../my-project \
  "查看当前仓库的开放 issue"
```
