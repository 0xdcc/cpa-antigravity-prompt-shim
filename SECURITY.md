# Security

这是给中转站运营方部署的私有基础设施，不是开放给终端用户的 API。

## 默认安全边界

- 仅支持 `127.0.0.1` 监听及上游；拒绝公网、域名和含 userinfo/query/fragment 的上游 URL。不实现额外公网鉴权，CPA 的原始鉴权头照常透传。
- 不记录 URI（包括路径及 query）、model、请求正文、Authorization、Cookie、OAuth、token 或响应内容；访问日志仅含状态、耗时和固定转换类别。
- systemd 模板启用 `NoNewPrivileges`、`PrivateTmp`、`ProtectSystem` 与只读 Home。
- sidecar 不读取或管理 OAuth，不看 CPA credential 目录，不实现账号池和出口切换。

## 协议与无损边界

默认 selector 仅 `^gemini-`，不会凭模型名字推断 provider、自动匹配全部 Claude/GPT。显式别名匹配须由运营方确认路由后在隔离端口按自身工作负载实测。

被搬移的指令含非文本、未知缓存字段（仅标准 ephemeral `cache_control` 会完整保留各块边界与 TTL）、未知字段或签名时整包 byte-exact 透传；不搬移的 tools/history/attachments/thinking 保留原值。Responses 服务端续接与 Gemini cachedContent 不改写。count_tokens/countTokens 不改写，因此不保证与转换后输入计数一致。配置缓存按文件 mtime_ns/size/inode 失效，无效热改返回 503 而不是沿用旧配置；启动配置错误统一脱敏输出，不回显原始配置值。

## Prompt 语义风险

把 system/developer 指令放到 user 消息是为了避免上游对 systemInstruction 的特定原文误报，不是通用安全增强。模型可能改变指令权重，尤其是非 Gemini 模型、复杂多轮会话、非 leading system、工具结果和超长输入。必须使用真实模型、真实工具定义和真实长会话做验收。

## 工具与重试

sidecar 必须原样保留 `tools`。缺工具声明时，模型可能输出 malformed function call，CPA/上游客户端可能跨账号重试。sidecar 不把 429 变成成功，也不负责账号隔离、限流或熔断。

## 报告问题

不要提交真实 Authorization、Cookie、OAuth 文件、代理地址、账号邮箱、完整 system prompt 或响应日志。请提供版本、部署模式、脱敏请求结构和最小复现。
