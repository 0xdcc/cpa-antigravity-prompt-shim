# Security

这是给中转站运营方部署的私有同机基础设施（适用于 CLIProxyAPI 与 Sub2API 前置），不是直接暴露给公网终端用户的 API 网关。

## 默认安全边界

- 仅支持 `127.0.0.1` 监听及同机回环上游；拒绝公网地址、外部域名和含 userinfo/query/fragment 的上游 URL。不实现额外公网鉴权，客户端发往 CPA 或 Sub2API 的原始鉴权头（`Authorization`、`x-api-key`、`x-goog-api-key`）照常透传。
- 不记录 URI（包括路径及 query）、model、请求正文、Authorization、Cookie、OAuth、token 或响应内容；访问日志仅含状态码、耗时和固定转换类别。
- systemd 模板启用 `NoNewPrivileges`、`PrivateTmp`、`ProtectSystem=strict` 与只读 Home。
- sidecar 不读取或管理 OAuth 凭据，不访问 CPA 凭据目录或 Sub2API 数据库/Redis，不执行 Token 刷新、账号池调度或出口切换。

## 协议与无损边界

默认 selector 仅 `^gemini-`，不会凭模型名字推断 provider 或默认匹配全部 Claude/GPT。显式别名匹配须由运营方确认上游渠道路由后在隔离端口按自身工作负载实测。

被搬移的指令含非文本、未知缓存字段（仅标准 ephemeral `cache_control` 会完整保留各块边界与 TTL）、未知字段或签名时整包 byte-exact 透传；不搬移的 tools/history/attachments/thinking 保留原值。Responses 服务端续接（`previous_response_id` / `conversation`）与 Gemini `cachedContent` 不改写。`count_tokens` / `countTokens` 不改写，因此不保证与转换后输入计数一致。配置缓存按文件 `mtime_ns/size/inode` 失效，无效热改返回 503 而不是沿用旧配置；启动配置错误统一脱敏输出，不回显原始配置值。

## Prompt 语义风险

把 system/developer 指令放到 user 消息是为了避免上游对 `systemInstruction` 的特定客户端原文误报，不是通用安全增强。模型可能改变指令权重（尤其在 Sub2API 等会在下游继续注入内置 `<identity>` 块的网关后方，或超长多轮工具对话中）。切流前必须使用真实模型、真实工具定义和真实长会话做隔离验收。

## 工具与重试

sidecar 原样保留 `tools` 与 `tool_choice`。测试时若缺失工具声明，模型可能输出 `MALFORMED_FUNCTION_CALL` 触发下游网关重试。此外请注意 Sub2API 默认在单次请求内部对 429 自动重试多次（单次客户端请求可能放大为 3 次上游调用）；sidecar 不把上游 429 变成成功，也不负责账号冷却、限流或熔断。

## 报告问题

不要在 Issue 或 PR 中提交真实 Authorization、Cookie、OAuth JSON、Refresh/Access Token、代理地址、账号邮箱、完整业务提示词或未脱敏响应日志。请仅提供版本号、后端类型（CPA / Sub2API）、脱敏请求结构和最小复现步骤。
