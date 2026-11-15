# Changelog

## Unreleased

- Unified **CLIProxyAPI (CPA)** and **Sub2API** support in a single operator-side sidecar repository:
  - Added Sub2API route aliases (`/chat/completions`, `/responses`, `/antigravity/v1/messages`, and `/antigravity/v1beta/models/<model>:<action>`).
  - Accepted `"text"` blocks alongside `"input_text"` in leading Responses `system`/`developer` items and `role: "user"` alongside `"system"` in Gemini `systemInstruction` for Sub2API compatibility.
  - Strengthened `DEFAULT_ANCHOR` so moved `<developer_instructions>` reliably override conflicting downstream persona injections (such as Sub2API's built-in Antigravity identity patch), verified live on Sub2API `v0.2.21`.
  - Added `config.sub2api.example.toml`, `./install.sh --sub2api --service-name ...`, and `--antigravity-prefix` support in `tools/acceptance.py`.
- Added conservative request adapters for Messages (`/v1/messages`), Responses (`/v1/responses`) and native Gemini `generateContent` / `streamGenerateContent` endpoints; default selector remains `^gemini-`, with explicit alias opt-in only.
- Preserved multi-block `ephemeral` `cache_control` boundaries and TTLs on Chat/Messages while keeping complex instruction metadata and server-side continuation bodies byte-exact; token counting endpoints remain untouched. Tools and history retain their original JSON values.
- Cached config/mode/rules by `mtime_ns/size/inode` with fail-closed invalid hot edits, sanitized startup configuration errors, and disabled HTTP redirects in `healthcheck.py`.
- Installed/uninstalled `adapters.py`; installer healthcheck reads the configured port; extended example fallback rules for inline and mid-turn steering fingerprints.
- Added `tools/acceptance.py` (gated `--live` loopback acceptance runner with redirect blocking, atomic request budget, and semantic marker verification) and expanded test suite; verified all four protocols against both CPA 8.0.70 and Sub2API v0.2.21 on isolated loopback ports.

## Initial Release

- Initial operator-side CPA Antigravity prompt shim (`/v1/chat/completions` loopback proxy, SSE pass-through, hot `wrap`/`passthrough` mode, defense-in-depth rules, systemd template, health check, and unit tests).
