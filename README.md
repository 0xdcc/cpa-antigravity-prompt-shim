# Antigravity Prompt Shim (for CLIProxyAPI & Sub2API)

> 面向**中转站运营方**的独立透明前置代理（Operator-side Sidecar），同时支持 **CLIProxyAPI (CPA)** 与 **Sub2API**。不是 CPA / Sub2API 的 fork，不修改上游二进制或源码，终端用户 0 插件、0 配置无感接入。

部分第三方 Agent 客户端（如 Hermes、Claude Code、OpenCode 等）的系统提示词原文出现在 Google Antigravity 上游的 `request.systemInstruction`（system 角色）中时，会命中上游字面量指纹过滤并返回伪装的 `HTTP 429 RESOURCE_EXHAUSTED`。本工具部署在中转站/分发层（如 New API）与 CPA 或 Sub2API 之间，将匹配模型的纯文本首部系统指令搬移至首条 `user` 消息的 `<developer_instructions>` 块，同时在 `system` 角色保留简短中性锚点（声明 `developer_instructions` 优先于默认人设或冲突指令，工具返回不可覆盖）。

> **明确边界**：本工具仅解决由 `systemInstruction` 原文指纹引起的假 429；**不解决**真实账号配额耗尽、并发限流、请求头/TLS 指纹、OAuth 刷新或 Google TOS 封禁。

## 部署拓扑

```text
终端客户端 -> 中转站 (如 New API) -> Sidecar Shim (127.0.0.1:8327 / 18083) -> CPA (:8317) 或 Sub2API (:8081) -> Google Antigravity
```

## 协议与路径支持矩阵（CPA & Sub2API 通用）

仅对 **POST、未压缩正文、`model_regex` 命中**的请求执行改写。URL query 参数与原始鉴权头（`Authorization` / `x-api-key` / `x-goog-api-key`）原样透传。

| 协议族 | 支持路径（含 Sub2API 专用路由与根路径别名） | 搬移与锚点策略 | 真实上游端到端验证状态 |
| --- | --- | --- | --- |
| **Chat Completions** | `/v1/chat/completions`<br>`/chat/completions` | 开头连续 `system`/`developer` 纯文本（保留 `ephemeral cache_control` 块边界）→ 首条 `user`；保留首条 `system` 中性锚点 | **CPA v8.0.70 & Sub2API v0.2.21 实测通过**（非流式、SSE 流式、工具调用、95 条长会话、黑名单样本） |
| **Anthropic Messages** | `/v1/messages`<br>`/antigravity/v1/messages` | 顶层 `system` 纯文本或 text blocks（无损保留每块 `ephemeral cache_control` 与 `ttl`）→ 首条 `user`；保留顶层 `system` 锚点 | **CPA v8.0.70 & Sub2API v0.2.21 实测通过** |
| **OpenAI Responses** | `/v1/responses`<br>`/responses` | 顶层 `instructions` + 开头连续 `system`/`developer`（兼容 `input_text` 与 `text` 块）→ 首条 `user`；保留顶层 `instructions` 锚点 | **CPA v8.0.70 & Sub2API v0.2.21 实测通过** |
| **Gemini Native** | `/v1beta/models/<model>:generateContent`<br>`/v1beta/models/<model>:streamGenerateContent`<br>`/antigravity/v1beta/models/<model>:generateContent`<br>`/antigravity/v1beta/models/<model>:streamGenerateContent` | `systemInstruction` / `system_instruction` 纯文本 `parts`（兼容 `role: "system"` / `"user"`）→ 首条 `user`；保留同名字段锚点 | **CPA v8.0.70 & Sub2API v0.2.21 实测通过** |
| **计费计数与其他路由** | `/v1/messages/count_tokens`、`:countTokens`、`/antigravity/v1beta/models/*:countTokens`、其他路径 | 不改写，整包 **byte-exact** 原样透传 | 计数针对原始请求，不篡改非生成接口 |

默认模型选择器为 `model_regex = "^gemini-"`，**不会默认改写原生 Claude / OpenAI GPT 请求**。若运营方将自定义模型别名（如 `antigravity-*`）确认路由至 Antigravity 渠道，可在配置中显式开启（例如 `model_regex = "^(gemini-|antigravity-)"`）。

## 无损与安全回退边界

- **严格保守回退（`complex-system`）**：仅搬移纯文本（Chat / Messages 允许标准 `cache_control: {"type": "ephemeral", "ttl"?: "..."}` 并完整保留块边界与 TTL）。若被搬移字段含未知属性、非 ephemeral 缓存、图片/多模态块或签名，**整份请求逐字节原样透传**，绝不静默丢弃字段。
- **服务端状态保护（`server-continuation`）**：Responses 携带 `previous_response_id` / `conversation`，或 Gemini 原生请求携带 `cachedContent` / `cached_content` 时，整包原样透传，不向服务端已有历史强插首轮消息。
- **下游内置身份兼容（Sub2API 适配）**：Sub2API 会在前置代理之后向 `systemInstruction` 注入内置 `<identity>You are Antigravity...</identity>`（该官方文本本身不触发 429）。本工具的默认锚点明确声明 `<developer_instructions>` 内的规则优先于冲突默认人设，在 Sub2API 指定验证场景（非流式、SSE 流式、工具调用及多协议标记测试）中验证通过；复杂自定义长指令仍建议按自身负载实测确认。
- **Sub2API 付费端点提示**：Sub2API 根据账号凭据中的 `plan_type`（如 `"Pro"` / `"Ultra"`）选择 `daily-cloudcode-pa.googleapis.com`；若手工导入凭据漏填 `plan_type`，Sub2API 会打向 `cloudcode-pa.googleapis.com` 导致假 429，请确保账号套餐元数据完整。

## 快速部署（同机回环私有链路）

需要 Python 3.11+（零第三方依赖）。仅允许绑定 `127.0.0.1` 与同机回环上游，严禁暴露公网。

### 方式 A：接入 CLIProxyAPI (CPA)

默认监听 `127.0.0.1:8327`，转发至同机 CPA `http://127.0.0.1:8317`：

```bash
sudo ./install.sh --yes
```

安装完成后，将中转站（如 New API）对应 Gemini 渠道的 Base URL 指向 `http://127.0.0.1:8327`（建议继续保留 CPA 自身的 `sensitive-patterns-replace` 作为纵深兜底）。

### 方式 B：接入 Sub2API

使用 `--sub2api` 一键预设（自动使用 `sub2api-antigravity-prompt-shim` 服务名与 `/opt/sub2api-antigravity-prompt-shim` 目录，默认监听 `127.0.0.1:18083`，转发至同机 Sub2API `http://127.0.0.1:8081`；可按实际端口调整 `/etc/sub2api-antigravity-prompt-shim/config.toml`）：

```bash
sudo ./install.sh --yes --sub2api
```

> 支持在同一台宿主机上使用不同 `--service-name <prefix>-antigravity-prompt-shim`、监听端口与 `--prefix` 同时为 CPA 与 Sub2API 各自独立运行一套实例（服务名强制约束为 `*-antigravity-prompt-shim` 并校验管理标记，防止误覆盖系统无关单元）。卸载 Sub2API 实例执行 `sudo ./uninstall.sh --yes --sub2api`。安全演练可加 `--destdir /tmp/stage` 或 `--dry-run`。

## 热切换与秒级回滚

config / mode / rules 按 `(mtime_ns, size, inode)` 自动热感知；配置损坏或非法正则会立即 Fail-Closed 返回 `503`（不静默沿用旧配置）。

1. **不断流热关闭改写（秒级退回纯透传）**：
   ```bash
   # CPA 默认实例（Sub2API 实例替换为对应 /etc/<service-name>/mode）
   sudo sh -c 'printf "passthrough\n" > /etc/cpa-antigravity-prompt-shim/mode'
   ```
2. **完全绕过**：将中转站渠道 Base URL 切回原 CPA（`:8317`）或 Sub2API（`:8081`）地址，无需重启后端反代。

## 本地测试与验收工具

```bash
/usr/bin/python3 -m unittest discover -s tests -v
/usr/bin/python3 -m compileall -q shim.py adapters.py healthcheck.py tools tests
bash -n install.sh uninstall.sh
```

全仓单测覆盖传输流式无缓冲刷新（`read1`）、连接安全关闭、CPA + Sub2API 全量 8 类路由与别名、`ephemeral cache_control` 边界、URL 编码注入防护及安装器演练。`tools/acceptance.py` 提供显式 `--live` 门控的同机回环真实验收器（Sub2API 专用路由可加 `--antigravity-prefix`）。

## English Summary

Operator-deployed transparent loopback sidecar for **CLIProxyAPI (CPA)** and **Sub2API** — not a fork, requiring zero client-side plugins or upstream code patches. For requests matching an explicit model selector (default `^gemini-`), it moves plain-text leading system instructions into the first user turn under `<developer_instructions>` while keeping a short neutral system anchor that preserves instruction priority even when Sub2API injects its downstream `<identity>` block. Supports OpenAI Chat Completions (`/v1/chat/completions`, `/chat/completions`), Anthropic Messages (`/v1/messages`, `/antigravity/v1/messages` with lossless `ephemeral` `cache_control` preservation), OpenAI Responses (`/v1/responses`, `/responses`), and Gemini Native (`generateContent` / `streamGenerateContent` under `/v1beta` and `/antigravity/v1beta`). Complex instruction payloads and server-side continuations fall back byte-for-byte. Verified end-to-end against both CPA v8.0.70 and Sub2API v0.2.21. Does not bypass genuine quota exhaustion, concurrency limits, OAuth issues, or Google TOS enforcement.

## License

MIT. See `LICENSE` and `SECURITY.md`.
