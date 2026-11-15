# Security

这是给中转站运营方部署的私有基础设施，不是开放给终端用户的 API。

## 默认安全边界

- 默认只监听 loopback；不要把 8327 暴露公网。
- 默认只允许 CPA 同机 loopback 上游，避免把 Authorization、请求体和模型流量误转到未知主机。
- 不记录请求正文、Authorization、Cookie、OAuth、token 或响应内容；普通访问日志只含方法、路径、状态、耗时和转换类别。
- systemd 模板启用 `NoNewPrivileges`、`PrivateTmp`、`ProtectSystem` 与只读 Home。
- sidecar 不读取或管理 OAuth，不看 CPA credential 目录，不实现账号池和出口切换。

## Prompt 语义风险

把 system/developer 指令放到 user 消息是为了避免上游对 systemInstruction 的特定原文误报，不是通用安全增强。模型可能改变指令权重，尤其是非 Gemini 模型、复杂多轮会话、非 leading system、工具结果和超长输入。必须使用真实模型、真实工具定义和真实长会话做验收。

## 工具与重试

sidecar 必须原样保留 `tools`。缺工具声明时，模型可能输出 malformed function call，CPA/上游客户端可能跨账号重试。sidecar 不把 429 变成成功，也不负责账号隔离、限流或熔断。

## 报告问题

不要提交真实 Authorization、Cookie、OAuth 文件、代理地址、账号邮箱、完整 system prompt 或响应日志。请提供版本、部署模式、脱敏请求结构和最小复现。
