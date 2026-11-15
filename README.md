# CPA Antigravity Prompt Shim

> 面向**中转站运营方**的透明 sidecar，不是终端用户软件。

CPA Antigravity 上游可能对 `systemInstruction` 中的某些 Agent harness 原文返回伪装的 `429 RESOURCE_EXHAUSTED`。本项目把 Gemini Chat Completions 请求开头连续的 `system`/`developer` 指令，改成首条 user 消息中的 `developer_instructions` 块，并留下短的中性 system anchor。中转站部署一次后，终端用户继续使用原来的 API，不安装、不改配置、无感接入。

## 设计边界

- 只改 `gemini-*` 的 `/v1/chat/completions`；其他模型、路径和没有 leading system 的请求透明转发。
- 保留 tools、历史消息、SSE 和流式响应；不处理 OAuth，不做多账号轮换，不替换 CPA，也不适配 Sub2API。
- 默认只监听 `127.0.0.1`，默认上游 `127.0.0.1:8317`；不写请求体、Authorization 或 token 日志。
- 这是降低特定 system-prompt 指纹误报的工程方案，不是 Google 风控绕过、封号保证或配额增加方案。
- 仍建议保留 CPA 原有 `sensitive-patterns-replace`，作为旁路/故障/其他入口的第二层防线。

## 中转站部署

在 CPA 同机准备 Python 3.11+，复制项目后审查配置：

```bash
sudo install -d -m 0755 /opt/cpa-antigravity-prompt-shim /etc/cpa-antigravity-prompt-shim
sudo install -m 0755 shim.py healthcheck.py /opt/cpa-antigravity-prompt-shim/
sudo install -m 0600 config.example.toml /etc/cpa-antigravity-prompt-shim/config.toml
sudo sh -c 'printf "wrap\n" > /etc/cpa-antigravity-prompt-shim/mode'
sudo install -m 0644 cpa-antigravity-prompt-shim.service /etc/systemd/system/
sudo systemctl daemon-reload && sudo systemctl enable --now cpa-antigravity-prompt-shim
```

也可以在人工确认后运行 `sudo ./install.sh --yes`。安装脚本不修改 CPA、New API、OAuth 或用户目录，也不自动改渠道地址。

## 接入中转站

### CPA 同机、New API 单渠道

```text
New API channel (Gemini) -> http://127.0.0.1:8327 -> CPA http://127.0.0.1:8317
```

只把需要保护的 Gemini 渠道 `base_url` 改到 `8327`。其他渠道保持直连 CPA。若 New API 与 sidecar 不同主机，使用受控内网地址和显式 API key，并先自行审查/改造 upstream guard；默认实现刻意拒绝非本机上游。

### 客户端直接接入

仅适合中转站内部组件把 sidecar 当作 CPA 的本机前置；终端用户不应直接访问 sidecar。公网入口、鉴权、计费和限流仍由 New API/中转站负责。

## 回滚

立即停用改写但保留链路：

```bash
sudo sh -c 'printf "passthrough\n" > /etc/cpa-antigravity-prompt-shim/mode'
```

完全绕过 sidecar：把对应 New API channel 的 `base_url` 改回 `http://127.0.0.1:8317`。两种操作都不需要重启 CPA。

## 配置与可选规则

`config.example.toml` 是运营方模板。`rules.example.json` 是可选的 defense-in-depth 规则，不是主机制；实际部署可在 `rules_file` 指向它。规则文件热加载。若不配置，system-to-user 仍然工作。

重要限制：只可靠处理开头连续的 system/developer 消息；若 system 出现在普通 user/assistant 之后，sidecar 不移动它。工具声明必须随原请求保留，否则模型可能产生 `MALFORMED_FUNCTION_CALL`，引起上游重试放大。

## 验证

```bash
python3 -m py_compile shim.py healthcheck.py
python3 -m unittest discover -s tests -v
```

生产接入前，运营方应使用自己的真实工具请求验证：最小请求、SSE、长上下文、重复 prompt cache、模型权限、429/502 统计及回滚。

## English

This is an operator-deployed transparent sidecar for CPA-backed relay services. It moves only leading system/developer instructions of Gemini OpenAI Chat Completions into a first user `developer_instructions` block and leaves a neutral system anchor. End users keep the same API and do not install anything. It does not handle OAuth, account pools, billing, Sub2API, or Google policy enforcement. Keep CPA's existing sensitive-pattern replacement as defense in depth. Default binding is loopback; review every deployment before exposing it behind a relay.

## License

Apache-2.0. See `LICENSE` and `SECURITY.md`.
