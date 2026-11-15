# Changelog

## Unreleased

- Added conservative request adapters for Messages (`/v1/messages`), Responses (`/v1/responses`) and native Gemini `generateContent` / `streamGenerateContent` endpoints; default selector remains `^gemini-`, with explicit alias opt-in only.
- Preserved multi-block `ephemeral` `cache_control` boundaries and TTLs on Chat/Messages while keeping complex instruction metadata and server-side continuation bodies byte-exact; token counting endpoints remain untouched. Tools and history retain their original JSON values.
- Cached config/mode/rules by `mtime_ns/size/inode` with fail-closed invalid hot edits, sanitized startup configuration errors, and disabled HTTP redirects in `healthcheck.py`.
- Installed/uninstalled `adapters.py`; installer healthcheck reads the configured port; extended example fallback rules for inline and mid-turn steering fingerprints.
- Added `tools/acceptance.py` (gated `--live` loopback acceptance runner with redirect blocking, atomic request budget, and semantic marker verification) and expanded test suite to 58 tests; verified all four protocols (non-streaming and SSE) against CPA 8.0.70 on an isolated loopback port.

- Initial operator-side CPA Antigravity prompt shim.
- Added loopback-by-default transparent proxy, Gemini leading system/developer materialization, SSE pass-through, hot `wrap`/`passthrough` mode, optional defense-in-depth rules, systemd template, health check and fake-upstream tests.
- Explicitly out of scope: end-user installation, OAuth, account pools, Sub2API, billing and public API exposure.
