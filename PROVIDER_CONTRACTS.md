# Provider 兼容矩阵

Provider Contract Matrix 使用真实模型、真实 `AgentLoop` 和真实文件工具验证三条协议路径：

| Case | Adapter | Endpoint | 默认模型 |
|---|---|---|---|
| `openai-responses` | `OpenAIResponsesProvider` | `POST /responses` | `gpt-4.1-mini` |
| `openai-chat` | `OpenAICompatibleProvider` | `POST /chat/completions` | `gpt-4.1-mini` |
| `deepseek-chat` | `OpenAICompatibleProvider` | `POST /chat/completions` | `deepseek-v4-flash` |

每个 case 在独立 job 中克隆 `pallets/itsdangerous` 作为陌生项目，然后执行两轮：

1. 模型必须调用 `write_file` 写入唯一 contract token；
2. 同一个 `AgentLoop` 必须保留历史，并调用 `read_file` 读回 token。

通过条件同时包含：两轮完成、write/read tool call、工具无错误、文件内容精确、最终 marker、SSE text delta 和非零 usage。这样不会把“模型只回复成功”误判为兼容。

实现遵循官方契约：[OpenAI streaming responses](https://developers.openai.com/api/docs/guides/streaming-responses)、[OpenAI function calling](https://developers.openai.com/api/docs/guides/function-calling)、[GPT-4.1 mini capabilities](https://developers.openai.com/api/docs/models/gpt-4.1-mini) 和 [DeepSeek Chat Completion](https://api-docs.deepseek.com/api/create-chat-completion/)。

## GitHub 配置

在仓库的 `Settings → Secrets and variables → Actions` 配置：

### 必需 Secrets

| Secret | 用途 |
|---|---|
| `OPENAI_API_KEY` | OpenAI Responses + Chat 两个 case |
| `DEEPSEEK_API_KEY` | DeepSeek Chat case |

CLI 配置方式：

```bash
gh secret set OPENAI_API_KEY
gh secret set DEEPSEEK_API_KEY
```

密钥只通过 job environment 注入，不出现在命令参数、JSON artifact 或日志中。GitHub 官方也建议通过 environment 传递 secret，且未配置的 secret 会解析为空字符串；对应 case 会生成显式 `skipped` 结果。[GitHub Actions secrets](https://docs.github.com/en/actions/how-tos/write-workflows/choose-what-workflows-do/use-secrets)

### 可选 Variables

| Variable | 默认值 |
|---|---|
| `OPENAI_RESPONSES_MODEL` | `gpt-4.1-mini` |
| `OPENAI_CHAT_MODEL` | `gpt-4.1-mini` |
| `OPENAI_BASE_URL` | `https://api.openai.com/v1` |
| `DEEPSEEK_MODEL` | `deepseek-v4-flash` |
| `DEEPSEEK_BASE_URL` | `https://api.deepseek.com` |

示例：

```bash
gh variable set OPENAI_RESPONSES_MODEL --body gpt-4.1-mini
gh variable set OPENAI_CHAT_MODEL --body gpt-4.1-mini
gh variable set DEEPSEEK_MODEL --body deepseek-v4-flash
```

## 运行与结果

工作流支持手动运行和每周定时运行：

```bash
gh workflow run provider-contract-matrix.yml
gh run watch
```

每个 provider 上传一份 `provider-contract-<case>` artifact，汇总 job 再生成：

- `provider-contract-matrix.json`：机器可读完整结果；
- `provider-contract-matrix.md`：包含 model、protocol、tools、stream delta、usage 和 duration 的表格；
- GitHub Job Summary：无需下载即可查看矩阵。

GitHub matrix 与 artifact 汇总方式参考官方 [matrix context](https://docs.github.com/en/actions/reference/workflows-and-actions/contexts#matrix-context) 和 [upload-artifact 示例](https://docs.github.com/en/actions/reference/workflows-and-actions/contexts#example-usage-of-the-strategy-context)。

## 本地运行

API key 必须放进环境变量，不能作为命令参数：

```bash
export PROVIDER_API_KEY=...
python -m mini_openharness.provider_contract run \
  --case deepseek-chat \
  --api-mode chat \
  --model deepseek-v4-flash \
  --base-url https://api.deepseek.com \
  --workspace /tmp/provider-contract-workspace \
  --output /tmp/deepseek-chat.json
```

失败时 runner 仍会尽量写出脱敏 JSON，并以状态码 1 退出；缺少 credential 默认返回 2，CI 使用 `--allow-missing-key` 将其记录为 skipped。
