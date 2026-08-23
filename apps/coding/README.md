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
