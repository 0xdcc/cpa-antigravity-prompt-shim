# CPA Antigravity Prompt Shim

> 面向**中转站运营方**的独立透明 sidecar，不是 CPA fork，也不是终端用户软件。

某些 Agent harness 原文放在 Antigravity 的 `systemInstruction` 中时可能触发特定提示词指纹误报。此 sidecar 将符合条件的纯文本指令放进首条 user 的 `<developer_instructions>` 块，并保留短的中性 anchor。它不保证角色权重等价，不解决所有 429，也不增加配额、解除封禁或处理 OAuth。

## 协议支持矩阵

仅对 **POST、未压缩正文、model selector 命中**的请求尝试改写。精确路径后可带 query；query 与上游原目标保持不变。

| 路径 | 可搬移字段 | 保留的 anchor | 验收状态（CPA 8.0.70 隔离端口实测） |
| --- | --- | --- | --- |
| `/v1/chat/completions` | 开头连续 `system` / `developer` 的 string 或纯 text blocks（保留 `ephemeral cache_control` 块边界） | 首条 system | 本地回归 + 真实上游通过（非流式、SSE、双轮工具调用、95 条长会话、多模型及无规则指纹样本） |
| `/v1/messages` | 顶层 `system` string 或纯 text blocks（保留 `ephemeral cache_control` 块边界） → 首条 user text blocks | 顶层 system | 本地回归 + 真实上游通过（Antigravity Claude 非流式、Gemini SSE 及 `ephemeral cache_control`） |
| `/v1/responses` | `instructions` string + 开头连续 system/developer string 或 input_text blocks → 首条 user input_text | 顶层 instructions | 本地回归 + 真实上游通过（Gemini 非流式与 SSE） |
| `/v1beta/models/<model>:generateContent` | `systemInstruction` 或 `system_instruction` 的纯 text parts → 首条 user parts | 同名 system 字段 | 本地回归 + 真实上游通过（Gemini 非流式） |
| `/v1beta/models/<model>:streamGenerateContent` | 同上；响应字节流原样转发 | 同上 | 本地回归 + 真实上游通过（`?alt=sse` 流式） |
| `/v1/messages/count_tokens`、原生 `:countTokens`、其他路径 | 不改写，整包 byte-exact 透传 | 不变 | 计数指的是原始请求，不估算搬移后的 token |

默认 selector 仍为 `^gemini-`，**不会默认匹配 Claude / GPT**。同名模型可能路由到不同 provider，sidecar 无法识别最终 provider。Messages 也必须命中 selector；运营方确认 CPA 别名确实走预期渠道后，才显式配置，例如 `model_regex = "^(gemini-|antigravity-)"`。本工具不创建、移除或改名 CPA 模型别名。

原生路径中的 model 只接受安全 ASCII token，允许一次 percent decode（如 `%2D`）；含编码斜线、冒号、控制字符、坏转义或双重编码的路径不改写。请求仍按原路径透传，不做路径规范化。

## 无损边界

- 仅搬移纯文本（Chat/Messages 允许标准 `cache_control: {"type": "ephemeral", "ttl"?: ...}` 并保留至搬移后的首条 user 文本块）。被搬移的消息/块/parts 带其他未知字段、非 ephemeral 缓存配置、非文本、签名或附加 metadata 时，**整份原始正文逐字节透传**，固定原因 `complex-system`，不挑出文本再丢弃剩余内容。
- 未搬移的 tools、历史消息、attachments、thinking、tool calls/results、签名及其他顶层字段保持原 JSON 值；被改写的正文会重新序列化，只有不改写的正文保证 byte-exact。
- Responses 含 `previous_response_id` 或 `conversation`（即使值为 null），以及 Gemini 含 `cachedContent` / `cached_content` 时保守透传，固定原因 `server-continuation`。不支持对服务端续接/缓存历史插入首条 user，也不声称完整覆盖所有未来协议扩展。
- Chat/Responses 只搬移开头连续指令；出现在后续历史中的 system/developer 原样保留。Messages 的 history 不做角色合并；是否接受连续 user 消息由真实 CPA/上游决定，必须实测。
- 可选 rules **有意修改**被搬移的文本，不是无损改写；默认不开启。工具 schema 不经 rules 处理。

## 部署（私有同机链路）

需要 Python 3.11+。只支持 `127.0.0.1` 监听与 loopback 上游，禁止公网暴露。

```bash
sudo install -d -m 0755 /opt/cpa-antigravity-prompt-shim /etc/cpa-antigravity-prompt-shim
sudo install -m 0755 shim.py healthcheck.py /opt/cpa-antigravity-prompt-shim/
sudo install -m 0644 adapters.py /opt/cpa-antigravity-prompt-shim/
sudo install -m 0600 config.example.toml /etc/cpa-antigravity-prompt-shim/config.toml
sudo sh -c 'printf "wrap\n" > /etc/cpa-antigravity-prompt-shim/mode'
sudo install -m 0644 cpa-antigravity-prompt-shim.service /etc/systemd/system/
sudo systemctl daemon-reload && sudo systemctl enable --now cpa-antigravity-prompt-shim
```

审查配置后也可 `sudo ./install.sh --yes`。安装保留已有配置，healthcheck 从实际 config 读取监听端口，不硬编码 8327。脚本不修改 CPA、New API、OAuth 或渠道地址。

```text
Local relay channel -> http://127.0.0.1:8327 -> CPA http://127.0.0.1:8317
```

只将确认需要改写的同机渠道地址改到 sidecar；外部入口的鉴权、计费、限流仍由中转站处理。Authorization / X-Api-Key 透传 CPA，sidecar 不另加会覆盖原鉴权的 API key。不适配 Sub2API，不实现账号轮换、风控管理或出口切换。

安全演练安装：`./install.sh --yes --prefix /opt/custom-shim --destdir /absolute/staging/root`，不调用 systemctl/healthcheck；加 `--dry-run` 则不写文件。自定义前缀卸载时也需传 `--prefix`。

## 配置、热加载与回滚

`config.example.toml` 为配置模板，`rules.example.json` 为可选防御纵深规则。部署可在 `rules_file` 指向运营方审查后的规则。继续保留 CPA 自己的 `sensitive-patterns-replace`，以覆盖旁路与其他入口；sidecar rules 不会自动写入 CPA 配置。

config/mode/rules 每次检查 `(mtime_ns, size, inode)`，未变化不重复 parse/compile。缺失或无效修改在启动拒绝、请求返回 503；不会继续使用上次有效值。规则的非法 regex / replacement 同样 fail closed。热改建议原子替换文件；不要刻意保留原 inode/size/mtime。监听地址/端口变更需重启 sidecar，不会热重绑 socket。

立即停止改写、保留链路：

```bash
sudo sh -c 'printf "passthrough\n" > /etc/cpa-antigravity-prompt-shim/mode'
```

完全绕过：将渠道 upstream 改回 CPA 地址。以上不需要重启 CPA。配置文件无效时即使 passthrough 也不放行；故障时直接绕过 sidecar。

## 传输与安全限制

- 原样转发上游状态、SSE/流式正文与非 hop-by-hop headers，不记录 URI/model/body/secret；日志只有状态、耗时及固定分类。
- 客户端/上游连接每请求关闭，不支持 WebSocket/升级协议。请求必须为 Content-Length 或无正文；chunked 请求 501，与 Content-Length 混用 400。上游 chunked 响应支持流式转发。
- 正文默认上限 16 MiB，超限 413；正文 socket I/O 超时默认 30 秒，上游 900 秒；不是全请求总时限。
- `/healthz`、`/readyz` 验证本地 config/mode/rules，不验证 CPA、凭据、真实模型或端到端服务质量。
- 凭据、响应、生产私有 IP/域名/邮箱不应进入仓库或问题报告。见 `SECURITY.md`。

## 本地验证与真实上游门禁

```bash
/usr/bin/python3 -m unittest discover -s tests -v
/usr/bin/python3 -m compileall -q shim.py adapters.py healthcheck.py tools tests
bash -n install.sh uninstall.sh
```

全仓 58 项本地单测覆盖原有 23 条传输/安全/安装回归、多协议 fake upstream 契约及验收器负例门禁。仓库自带显式门控的真实上游验收工具 `tools/acceptance.py`（默认不传 `--live` 绝不联网，强制 loopback、禁止重定向、预算原子上限、遇 429/403 立即熔断停机并输出脱敏报告）。切换生产渠道前，请用您自己的模型路由在隔离端口（如 `127.0.0.1:8328`）执行真实工作负载验收。

## English

Operator-deployed independent CPA sidecar, not a fork. Conservative request adapters cover Chat Completions, Messages, Responses and native Gemini generation endpoints, selected by an explicit model regex (default `^gemini-`, never all Claude/GPT). Plain text instructions (and standard `ephemeral` `cache_control` block boundaries on Chat/Messages) are moved into the first user turn under `<developer_instructions>`; complex instruction metadata and server-side continuations pass through byte-for-byte. Tools and history retain their JSON values. Verified on an isolated loopback port against CPA 8.0.70 + Antigravity upstream across non-streaming, SSE, tool calls, and multi-turn history; still verify prompt precedence on your own workloads. This does not solve legitimate quota 429s, request-header fingerprints, OAuth, or Google TOS enforcement.

## License

MIT. See `LICENSE` and `SECURITY.md`.
