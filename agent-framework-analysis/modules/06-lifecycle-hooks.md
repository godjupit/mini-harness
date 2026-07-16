# 生命周期 Hook 与 Verification Gate

> 分析状态：已验证  
> 优先级：P1  
> 模块类型：Extension  
> 主要代码：`src/mini_openharness/hooks.py`；`engine.py` 四类调用点

## 1. 模块职责

**已确认**：Hook 是模型不可绕过的可信 control-plane 扩展点，在 prompt、工具执行前后和完成前运行，可改写 payload、阻断行为或执行验证；`hooks.py:22-29`、`engine.py:140-152,291-312,389-500`。

它不替代 Tool：模型不能选择 Hook；也不替代 Permission：Hook 表达组织生命周期策略，Permission 表达 capability/effect 授权。

## 2. 独立模块依据

有独立 Protocol、Registry、Executor、callback/command adapters、配置 loader、优先级/匹配/timeout/failure policy 和专项测试，是框架最完整的扩展机制之一。

## 3. 对外接口

- `HookEvent`：`user_prompt_submit/pre_tool_use/post_tool_use/stop`。
- `HookContext`、`HookResult`、`AggregatedHookResult`。
- `Hook` Protocol：name、priority、matcher、timeout、failure_mode、async run。
- `CallbackHook`：适配 sync/async Python callback。
- `CommandHook`：适配 argv 子进程和可选 JSON 协议。
- `HookRegistry.register/get`：按事件保存，读取时 priority 降序稳定排序。
- `HookExecutor.execute`：顺序执行、链式合并 payload、阻断短路。
- `load_hook_registry`：从严格 JSON 创建 CommandHook。

## 4. 运行语义

每个匹配 Hook 接收前一个 Hook 更新后的 payload。只有未阻断的 `updated_payload` 才合并；显式 blocked 立即短路；`hooks.py:226-293`。

异常/timeout 由 `failure_mode` 决定：`block` 变成阻断，`continue` 记录失败后继续。显式 `HookResult(blocked=True)` 不受 failure mode 改写。

Matcher 对 tool Hook 匹配 `tool_name`，其他事件匹配 prompt/response，使用 `fnmatchcase`；`hooks.py:360-367`。

## 5. CommandHook 协议与安全边界

- `create_subprocess_exec(*argv)`，不经过 shell；cwd 固定 workspace。
- stdin 是 `{event,payload}` JSON。
- 默认以退出码表示成功；`expect_json` 时 stdout 可返回 allow/block、reason、updated_payload/output。
- 默认仅继承 PATH/locale/temp/Python 环境等白名单；可显式升级为完整环境。
- 取消时 kill/wait；Executor 外层施加 timeout；`hooks.py:132-190,249-262`。

Command/Python Hook 都是受信任代码，不是 sandbox。Python callback 与 Agent 同进程；命令 Hook 可在 workspace 启动任意 argv。

## 6. 四个调用点

| 事件 | 精确时机 | 改写/阻断效果 |
| -- | -- | -- |
| user_prompt_submit | user Message 入 history 前 | 改 prompt；阻断整个 run |
| pre_tool_use | resource/schema/permission 前 | 改真实 arguments；阻断为 error observation |
| post_tool_use | 工具完成、history 回填前 | 改 output/metadata/error；阻断结果 |
| stop | 无 tool calls、done 前 | 允许完成；阻断则把验证原因反馈模型 |

## 7. Verification Gate

stop Hook 是最关键的独特语义：失败不会伪造 done，而是追加“可信验证失败” user Message，使模型修复并再申请完成。它将外部 pytest/lint/security scan 变成 Agent 状态机中的完成条件，同时仍受 max steps 限制。

## 8. 扩展方式

实现 Hook Protocol 并注册到某个 HookEvent。新增 HTTP/queue/policy adapter 不需要修改 Executor，因为 Executor 只调用 `run(context)`。若要通过 JSON 配置启用新 adapter，当前 loader 只接受 `type=command`，仍需扩展 loader。

并行 tool batch 会并发运行多个 pre/post Hook 链；有共享状态的自定义 Hook 必须自行同步。

## 9. 错误与边界情况

- Hook payload 是未版本化 dict，字段错误在 Engine 侧才发现。
- `inherit_environment=true` 可能暴露 API key，应视为信任升级。
- 普通 command stdout 若恰好是 JSON object，会被解释为结构化响应，即使 `expect_json=false`。
- CommandHook timeout 的 process cleanup 依赖 task cancellation 触发 `communicate` 的 except。
- stop Hook reason 进入 Provider history，可能包含测试输出或敏感路径。

## 10. 测试依据

- `tests/test_hooks.py::test_registry_runs_priority_order_and_chains_payload_updates`
- `test_matcher_skips_unrelated_tool_and_block_short_circuits`
- `test_timeout_obeys_failure_mode`
- `test_command_failure_blocks_without_inheriting_api_key`
- `test_pre_and_post_tool_hooks_transform_the_real_execution`
- `test_stop_hook_blocks_completion_then_agent_recovers_and_trace_proves_it`
- `test_user_prompt_hook_can_rewrite_or_reject_before_provider_call`

## 11. 设计评价与阅读建议

- 值得学习：Protocol 驱动、顺序 payload chaining、fail-open/closed 显式化、真正的 completion gate。
- 复杂度来源：四类 payload 的隐式 schema和可信代码边界。
- 改进方向：为各事件定义 typed payload/result；把 command adapter 的 plain/JSON 模式区分得更严格；提供并发安全说明。
- 精读：`Hook`、`CallbackHook.run`、`CommandHook.run`、`HookRegistry.get`、`HookExecutor.execute`、`load_hook_registry`。
