# Homestay Agent

该目录集中保存 Homestay Agent 的产品资源：

```text
homestay/
├── config/       # System Prompt、权限和 MCP 配置
├── skills/       # 民宿业务流程
├── memory/       # 长期记忆索引
└── data/         # Session、Trace、Artifact 和 OAuth token
```

`data/` 是本地运行数据，不提交到 Git。入口
`../homestay_agent.py` 会显式传入 Session、Trace 和 Artifact 路径，因此
Homestay Agent 不使用通用的 `.mini-oh` 默认目录。
