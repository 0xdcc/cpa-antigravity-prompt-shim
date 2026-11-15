#!/usr/bin/env python3
"""Standard library acceptance runner for CPA Antigravity Prompt Shim and upstream CPA.

Ensures safe loopback testing, strict gating, request budgets, and desensitized reporting.
"""
from __future__ import annotations

import argparse
import concurrent.futures
from dataclasses import dataclass, field
import json
import os
from pathlib import Path
import re
import stat
import sys
import threading
import time
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
import urllib.error
import urllib.request

ALLOWED_LIVE_BASE_URLS: set[str] = {"http://127.0.0.1:8328"}
DEFAULT_MODEL: str = "gemini-3.8-flash-high"
DEFAULT_MAX_REQUESTS: int = 16
DEFAULT_TIMEOUT_SECONDS: float = 90.0
NEUTRAL_MARKER: str = "MARKER:OK"
TOOL_RESULT_SENTINEL: str = "TOOL_RESULT_7391"
HISTORY_CODE_SENTINEL: str = "HISTORY_CODE_4821"


class _NoRedirectHandler(urllib.request.HTTPRedirectHandler):
    """Disable HTTP redirects so Authorization / x-api-key headers cannot leak."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise urllib.error.HTTPError(req.full_url, code, "Redirects disabled", headers, fp)


class _RequestStoppedError(RuntimeError):
    """Raised when RunnerState denies a slot due to stop or budget exhaustion."""


def validate_base_url(url: str, is_live: bool = False) -> str:
    """Validate base URL against security invariants."""
    if not url:
        raise ValueError("base-url cannot be empty")
    u = urlsplit(url)
    if u.scheme not in ("http", "https"):
        raise ValueError(f"Invalid URL scheme: '{u.scheme}'; only http or https allowed")
    if u.username is not None or u.password is not None:
        raise ValueError("URL must not contain credentials/userinfo")
    if u.query:
        raise ValueError("URL must not contain query parameters")
    if u.fragment:
        raise ValueError("URL must not contain fragments")

    hostname = (u.hostname or "").lower()
    if hostname not in ("127.0.0.1", "localhost", "::1"):
        raise ValueError(f"Host '{hostname}' is not a loopback address; only loopback allowed")

    clean = f"{u.scheme}://{u.netloc}{u.path.rstrip('/')}"
    if is_live:
        if clean not in ALLOWED_LIVE_BASE_URLS:
            raise ValueError(
                f"Base URL '{clean}' is not in allowed live endpoints: {sorted(ALLOWED_LIVE_BASE_URLS)}"
            )
    return clean


def check_key_file_permissions(path: str | Path) -> None:
    """Verify that the key file exists and has strict 0600 permissions."""
    p = Path(path).resolve()
    if not p.is_file():
        raise FileNotFoundError(f"Key file does not exist: {path}")
    mode = stat.S_IMODE(p.stat().st_mode)
    if mode != 0o600:
        raise PermissionError(f"Key file {path} has permissions {oct(mode)}, required: 0600 (0o600)")


def load_key_from_file(path: str | Path) -> str:
    """Load API key from a 0600-permission file."""
    check_key_file_permissions(path)
    content = Path(path).read_text(encoding="utf-8").strip()
    if not content:
        raise ValueError("Key file is empty")
    return content


_CONFIG_LIST_KEYS = ("api-keys", "api_keys", "apiKeys")
_CONFIG_SINGLE_KEYS = ("api_key", "api-key", "apiKey", "key", "gemini_api_key", "cpa_key", "token")
_CONFIG_SECTIONS = ("cpa", "api", "auth", "upstream", "relay")


def _extract_key_from_value(val: Any) -> str | None:
    if isinstance(val, str):
        s = val.strip()
        return s if s else None
    if isinstance(val, list):
        for item in val:
            if isinstance(item, str) and item.strip():
                return item.strip()
    return None


def _extract_key_from_mapping(parsed: Any) -> str | None:
    if not isinstance(parsed, dict):
        return None

    for k in _CONFIG_LIST_KEYS + _CONFIG_SINGLE_KEYS:
        if k in parsed:
            found = _extract_key_from_value(parsed[k])
            if found:
                return found

    for sec in _CONFIG_SECTIONS:
        sub = parsed.get(sec)
        if isinstance(sub, dict):
            found = _extract_key_from_mapping(sub)
            if found:
                return found

    for v in parsed.values():
        if isinstance(v, dict):
            found = _extract_key_from_mapping(v)
            if found:
                return found

    return None


def _strip_yaml_scalar(val: str) -> str:
    s = val.strip()
    if not s:
        return ""
    if (s.startswith('"') and s.endswith('"') and len(s) >= 2) or (
        s.startswith("'") and s.endswith("'") and len(s) >= 2
    ):
        return s[1:-1].strip()
    # Strip trailing unquoted comment
    s = re.split(r"\s+#", s, maxsplit=1)[0].strip()
    if (s.startswith('"') and s.endswith('"') and len(s) >= 2) or (
        s.startswith("'") and s.endswith("'") and len(s) >= 2
    ):
        return s[1:-1].strip()
    return s


def _extract_yaml_list_key_stdlib(raw_text: str) -> str | None:
    lines = raw_text.splitlines()
    key_header_re = re.compile(r"^(\s*)(?:api[-_]?keys|apiKeys)\s*:\s*(.*)$", re.IGNORECASE)
    for idx, line in enumerate(lines):
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        m = key_header_re.match(line)
        if not m:
            continue
        key_indent = len(m.group(1))
        rest = m.group(2).strip()
        # Handle inline YAML list: api-keys: ["sk-...", ...]
        if rest.startswith("["):
            end_bracket = rest.find("]")
            inner = rest[1:end_bracket] if end_bracket != -1 else rest[1:]
            for part in inner.split(","):
                candidate = _strip_yaml_scalar(part)
                if candidate:
                    return candidate
            continue
        # Handle single scalar on the same line (ignoring comment)
        if rest and not rest.startswith("#"):
            candidate = _strip_yaml_scalar(rest)
            if candidate:
                return candidate
        # Scan indented YAML list items below api-keys:
        for sub in lines[idx + 1 :]:
            sub_stripped = sub.strip()
            if not sub_stripped or sub_stripped.startswith("#"):
                continue
            sub_indent = len(sub) - len(sub.lstrip())
            lstripped = sub.lstrip()
            if lstripped.startswith("-") and (len(lstripped) == 1 or lstripped[1].isspace()):
                if sub_indent < key_indent:
                    break
                item_raw = lstripped[1:].strip()
                candidate = _strip_yaml_scalar(item_raw)
                if candidate:
                    return candidate
                continue
            if sub_indent <= key_indent or not lstripped.startswith("-"):
                break
    return None


def _extract_strict_inline_key(raw_text: str) -> str | None:
    inline_re = re.compile(
        r"""^\s*["']?(?:api[-_]?key|apiKey|gemini[-_]?api[-_]?key|cpa[-_]?key|key|token)["']?\s*[:=]\s*(.+?)\s*$""",
        re.IGNORECASE,
    )
    for line in raw_text.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        m = inline_re.match(line)
        if not m:
            continue
        val_raw = m.group(1).strip()
        val = _strip_yaml_scalar(val_raw)
        if re.fullmatch(r"[A-Za-z0-9_.\-]{8,}", val) and val.lower() not in {"undefined"}:
            return val
    return None


def load_key_from_cpa_config(path: str | Path) -> str:
    """Extract API key from local CPA config (YAML/TOML/JSON) without exposing contents."""
    p = Path(path).resolve()
    if not p.is_file():
        raise FileNotFoundError(f"CPA config does not exist: {path}")
    mode = stat.S_IMODE(p.stat().st_mode)
    if mode & 0o002:
        raise PermissionError(f"CPA config {path} is world-writable ({oct(mode)})")

    raw_text = p.read_text(encoding="utf-8", errors="ignore")
    key: str | None = None

    # 1. Try optional PyYAML
    try:
        import yaml  # type: ignore

        parsed = yaml.safe_load(raw_text)
        key = _extract_key_from_mapping(parsed)
    except Exception:
        pass

    # 2. Try tomllib
    if not key:
        try:
            import tomllib

            parsed = tomllib.loads(raw_text)
            key = _extract_key_from_mapping(parsed)
        except Exception:
            pass

    # 3. Try JSON
    if not key:
        try:
            parsed = json.loads(raw_text)
            key = _extract_key_from_mapping(parsed)
        except Exception:
            pass

    # 4. Standard library YAML list parser (api-keys / api_keys)
    if not key:
        key = _extract_yaml_list_key_stdlib(raw_text)

    # 5. Strict single-line key-value fallback
    if not key:
        key = _extract_strict_inline_key(raw_text)

    if not key:
        raise ValueError(f"Could not locate API key in CPA config: {path}")
    return key


@dataclass
class UsageSummary:
    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0
    cached_tokens: int | str = "NOT_OBSERVED"

    def to_dict(self) -> dict[str, Any]:
        return {
            "prompt_tokens": self.prompt_tokens,
            "completion_tokens": self.completion_tokens,
            "total_tokens": self.total_tokens,
            "cached_tokens": self.cached_tokens,
        }


@dataclass
class TestResult:
    name: str
    protocol: str
    status: str  # PASS, FAIL, STOPPED, SKIPPED
    code: int | None = None
    latency_ms: float = 0.0
    first_frame_ms: float | None = None
    usage: dict[str, Any] = field(default_factory=lambda: UsageSummary().to_dict())
    error_category: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "protocol": self.protocol,
            "status": self.status,
            "code": self.code,
            "latency_ms": round(self.latency_ms, 2),
            "first_frame_ms": round(self.first_frame_ms, 2) if self.first_frame_ms is not None else None,
            "usage": self.usage,
            "error_category": self.error_category,
        }


class RunnerState:
    """Thread-safe request budget tracking and 403/429 circuit breaker."""

    def __init__(self, max_requests: int = DEFAULT_MAX_REQUESTS):
        self.max_requests = max_requests
        self.total_requests = 0
        self.stopped = False
        self.stop_reason: str | None = None
        self._lock = threading.Lock()

    @property
    def request_count(self) -> int:
        return self.total_requests

    def can_request(self) -> tuple[bool, str | None]:
        with self._lock:
            if self.stopped:
                return False, self.stop_reason or "Runner stopped"
            if self.total_requests >= self.max_requests:
                self.stopped = True
                self.stop_reason = f"Request budget exhausted ({self.max_requests})"
                return False, self.stop_reason
            return True, None

    def acquire_slot(self) -> tuple[bool, str | None]:
        """Atomically verify stop/budget state and reserve one request slot."""
        with self._lock:
            if self.stopped:
                return False, self.stop_reason or "Runner stopped"
            if self.total_requests >= self.max_requests:
                self.stopped = True
                self.stop_reason = f"Request budget exhausted ({self.max_requests})"
                return False, self.stop_reason
            self.total_requests += 1
            return True, None

    def record_response_status(self, status: int) -> None:
        with self._lock:
            if status == 429:
                self.stopped = True
                self.stop_reason = "RateLimit429 (immediate stop, no retry)"
            elif status == 403:
                self.stopped = True
                self.stop_reason = "Forbidden403 (immediate stop, no retry)"


class AcceptanceRunner:
    """Executes validation checks against loopback upstream."""

    def __init__(
        self,
        base_url: str,
        api_key: str,
        model: str = DEFAULT_MODEL,
        messages_model: str | None = None,
        extra_model: str | None = None,
        max_requests: int = DEFAULT_MAX_REQUESTS,
        fixtures_file: str | None = None,
        zwsp_fixture: bool = False,
        timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
    ):
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.model = model
        self.messages_model = messages_model or model
        self.extra_model = extra_model
        self.state = RunnerState(max_requests=max_requests)
        self.fixtures_file = fixtures_file
        self.zwsp_fixture = zwsp_fixture
        self.timeout_seconds = timeout_seconds
        # Direct loopback opener with redirects disabled to prevent proxy/credential leakage
        self.opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), _NoRedirectHandler())

    def _execute_http(
        self,
        method: str,
        endpoint: str,
        payload: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
        stream: bool = False,
    ) -> tuple[int, Any, float, float | None]:
        """Execute request with atomic budget gate and timing. No retries."""
        acquired, reason = self.state.acquire_slot()
        if not acquired:
            raise _RequestStoppedError(reason or "Request not permitted")

        url = f"{self.base_url}{endpoint}"
        req_headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {self.api_key}",
            "User-Agent": "cpa-acceptance-runner/1.0",
        }
        if headers:
            req_headers.update(headers)

        body_bytes = json.dumps(payload).encode("utf-8") if payload is not None else None
        req = urllib.request.Request(url, data=body_bytes, headers=req_headers, method=method)

        started = time.monotonic()
        first_frame_ms: float | None = None

        try:
            with self.opener.open(req, timeout=self.timeout_seconds) as resp:
                status = resp.status
                self.state.record_response_status(status)

                if stream:
                    chunks: list[dict[str, Any]] = []
                    stream_ended = False
                    for raw_line in resp:
                        line = raw_line.decode("utf-8", errors="replace").strip()
                        if not line:
                            continue
                        if line.startswith("data:"):
                            if first_frame_ms is None:
                                first_frame_ms = (time.monotonic() - started) * 1000.0
                            data_part = line[len("data:"):].strip()
                            if data_part == "[DONE]":
                                stream_ended = True
                                break
                            try:
                                chunk = json.loads(data_part)
                                chunks.append(chunk)
                                choices = chunk.get("choices", [])
                                if choices and isinstance(choices, list) and isinstance(choices[0], dict):
                                    if choices[0].get("finish_reason") in ("stop", "end_turn", "tool_calls", "length"):
                                        stream_ended = True
                            except Exception:
                                pass
                    latency_ms = (time.monotonic() - started) * 1000.0
                    return status, {"chunks": chunks, "stream_ended": stream_ended}, latency_ms, first_frame_ms

                resp_body = resp.read()
                latency_ms = (time.monotonic() - started) * 1000.0
                try:
                    parsed = json.loads(resp_body.decode("utf-8"))
                except Exception:
                    parsed = {"raw": resp_body}
                return status, parsed, latency_ms, None

        except HTTPError as e:
            latency_ms = (time.monotonic() - started) * 1000.0
            self.state.record_response_status(e.code)
            try:
                e.close()
            except Exception:
                pass
            return e.code, None, latency_ms, None
        except (URLError, TimeoutError, ConnectionResetError) as e:
            latency_ms = (time.monotonic() - started) * 1000.0
            raise e

    def _extract_usage(self, data: dict[str, Any]) -> dict[str, Any]:
        usage = data.get("usage") or {}
        cached: int | str = "NOT_OBSERVED"
        if isinstance(usage, dict):
            if isinstance(usage.get("cached_tokens"), int) and usage["cached_tokens"] > 0:
                cached = usage["cached_tokens"]
            elif isinstance(usage.get("prompt_tokens_details"), dict):
                ptd = usage["prompt_tokens_details"]
                if isinstance(ptd.get("cached_tokens"), int) and ptd["cached_tokens"] > 0:
                    cached = ptd["cached_tokens"]
            elif isinstance(usage.get("cache_read_input_tokens"), int) and usage["cache_read_input_tokens"] > 0:
                cached = usage["cache_read_input_tokens"]

        summary = UsageSummary(
            prompt_tokens=int(usage.get("prompt_tokens", 0)) if isinstance(usage.get("prompt_tokens"), (int, float)) else 0,
            completion_tokens=int(usage.get("completion_tokens", 0)) if isinstance(usage.get("completion_tokens"), (int, float)) else 0,
            total_tokens=int(usage.get("total_tokens", 0)) if isinstance(usage.get("total_tokens"), (int, float)) else 0,
            cached_tokens=cached,
        )
        return summary.to_dict()

    # --- Test Scenarios ---

    @staticmethod
    def _contains_marker(text: Any) -> bool:
        return isinstance(text, str) and NEUTRAL_MARKER.lower() in text.lower()

    @staticmethod
    def _extract_responses_text(data: Any) -> str:
        if not isinstance(data, dict):
            return ""
        texts: list[str] = []
        output = data.get("output")
        if isinstance(output, str):
            texts.append(output)
        elif isinstance(output, list):
            for item in output:
                if isinstance(item, str):
                    texts.append(item)
                elif isinstance(item, dict):
                    if isinstance(item.get("text"), str):
                        texts.append(item["text"])
                    content = item.get("content")
                    if isinstance(content, str):
                        texts.append(content)
                    elif isinstance(content, list):
                        for part in content:
                            if isinstance(part, str):
                                texts.append(part)
                            elif isinstance(part, dict) and isinstance(part.get("text"), str):
                                texts.append(part["text"])
        if not texts and isinstance(data.get("output_text"), str):
            texts.append(data["output_text"])
        if not texts and isinstance(data.get("choices"), list) and data["choices"]:
            c0 = data["choices"][0]
            if isinstance(c0, dict) and isinstance(c0.get("message"), dict) and isinstance(c0["message"].get("content"), str):
                texts.append(c0["message"]["content"])
        if not texts and data.get("content"):
            c = data["content"]
            if isinstance(c, str):
                texts.append(c)
            elif isinstance(c, list):
                for part in c:
                    if isinstance(part, dict) and isinstance(part.get("text"), str):
                        texts.append(part["text"])
        return "".join(texts)

    def test_chat_basic(self) -> TestResult:
        """OpenAI chat protocol: basic non-streaming."""
        payload = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": f"You are a verification harness. When the user says ping, reply with exact token {NEUTRAL_MARKER}."},
                {"role": "user", "content": "ping"},
            ],
            "stream": False,
        }
        try:
            status, data, latency, _ = self._execute_http("POST", "/v1/chat/completions", payload=payload)
        except _RequestStoppedError as e:
            return TestResult("chat_basic", "chat", "STOPPED", latency_ms=0, error_category=str(e))
        except Exception as e:
            return TestResult("chat_basic", "chat", "FAIL", latency_ms=0, error_category=type(e).__name__)

        if status != 200:
            cat = "RateLimit429" if status == 429 else ("Forbidden403" if status == 403 else f"HTTP_{status}")
            return TestResult("chat_basic", "chat", "FAIL", code=status, latency_ms=latency, error_category=cat)

        if not isinstance(data, dict):
            return TestResult("chat_basic", "chat", "FAIL", code=status, latency_ms=latency, error_category="NonJsonBody")

        choices = data.get("choices")
        if not isinstance(choices, list) or len(choices) == 0:
            return TestResult("chat_basic", "chat", "FAIL", code=status, latency_ms=latency, error_category="EmptyChoices")

        choice = choices[0]
        if not isinstance(choice, dict):
            return TestResult("chat_basic", "chat", "FAIL", code=status, latency_ms=latency, error_category="InvalidChoice")

        msg = choice.get("message", {})
        content = msg.get("content") if isinstance(msg, dict) else None
        tool_calls = msg.get("tool_calls") if isinstance(msg, dict) else None
        if not content and not tool_calls:
            return TestResult("chat_basic", "chat", "FAIL", code=status, latency_ms=latency, error_category="EmptyContentAndNoToolCalls")

        if not self._contains_marker(content):
            return TestResult("chat_basic", "chat", "FAIL", code=status, latency_ms=latency, error_category="MarkerMissing")

        return TestResult(
            name="chat_basic",
            protocol="chat",
            status="PASS",
            code=status,
            latency_ms=latency,
            usage=self._extract_usage(data),
        )

    def test_chat_sse(self) -> TestResult:
        """OpenAI chat protocol: SSE streaming with first-frame timing and stream end check."""
        payload = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": f"You are a verification harness. When the user says ping, reply with exact token {NEUTRAL_MARKER}."},
                {"role": "user", "content": "ping"},
            ],
            "stream": True,
        }
        headers = {"Accept": "text/event-stream"}
        try:
            status, data, latency, first_frame = self._execute_http(
                "POST", "/v1/chat/completions", payload=payload, headers=headers, stream=True
            )
        except _RequestStoppedError as e:
            return TestResult("chat_sse", "chat", "STOPPED", latency_ms=0, error_category=str(e))
        except Exception as e:
            return TestResult("chat_sse", "chat", "FAIL", latency_ms=0, error_category=type(e).__name__)

        if status != 200:
            cat = "RateLimit429" if status == 429 else ("Forbidden403" if status == 403 else f"HTTP_{status}")
            return TestResult("chat_sse", "chat", "FAIL", code=status, latency_ms=latency, error_category=cat)

        chunks = data.get("chunks", []) if isinstance(data, dict) else []
        stream_ended = data.get("stream_ended", False) if isinstance(data, dict) else False

        if not stream_ended:
            return TestResult("chat_sse", "chat", "FAIL", code=status, latency_ms=latency, error_category="MissingStreamEnd")

        delta_parts: list[str] = []
        for c in chunks:
            choices = c.get("choices")
            if isinstance(choices, list) and len(choices) > 0 and isinstance(choices[0], dict):
                delta = choices[0].get("delta")
                if isinstance(delta, dict):
                    part = delta.get("content")
                    if isinstance(part, str) and part:
                        delta_parts.append(part)
        streamed_text = "".join(delta_parts)

        if not streamed_text:
            return TestResult("chat_sse", "chat", "FAIL", code=status, latency_ms=latency, error_category="EmptyStreamContent")

        if not self._contains_marker(streamed_text):
            return TestResult("chat_sse", "chat", "FAIL", code=status, latency_ms=latency, error_category="MarkerMissing")

        # Extract usage from last chunk if present
        usage_data = {}
        for c in reversed(chunks):
            if c.get("usage"):
                usage_data = c
                break

        return TestResult(
            name="chat_sse",
            protocol="chat",
            status="PASS",
            code=status,
            latency_ms=latency,
            first_frame_ms=first_frame,
            usage=self._extract_usage(usage_data),
        )

    def test_chat_multi_turn(self) -> TestResult:
        """Multi-turn conversation history test."""
        messages = [
            {
                "role": "system",
                "content": (
                    f"You are a verification assistant. Every reply must include {NEUTRAL_MARKER} "
                    "and repeat the history code from earlier user messages when asked."
                ),
            },
            {"role": "user", "content": f"Remember this history code: {HISTORY_CODE_SENTINEL}"},
            {"role": "assistant", "content": "Noted."},
            {"role": "user", "content": "What was the history code? Reply concisely."},
        ]
        payload = {"model": self.model, "messages": messages, "stream": False}
        try:
            status, data, latency, _ = self._execute_http("POST", "/v1/chat/completions", payload=payload)
        except _RequestStoppedError as e:
            return TestResult("chat_multi_turn", "chat", "STOPPED", latency_ms=0, error_category=str(e))
        except Exception as e:
            return TestResult("chat_multi_turn", "chat", "FAIL", latency_ms=0, error_category=type(e).__name__)

        if status != 200:
            cat = "RateLimit429" if status == 429 else ("Forbidden403" if status == 403 else f"HTTP_{status}")
            return TestResult("chat_multi_turn", "chat", "FAIL", code=status, latency_ms=latency, error_category=cat)

        choices = data.get("choices", []) if isinstance(data, dict) else []
        msg = choices[0].get("message", {}) if choices and isinstance(choices[0], dict) else {}
        content = msg.get("content") if isinstance(msg, dict) else None
        if not isinstance(content, str) or not content.strip():
            return TestResult("chat_multi_turn", "chat", "FAIL", code=status, latency_ms=latency, error_category="EmptyContent")

        if HISTORY_CODE_SENTINEL not in content:
            return TestResult("chat_multi_turn", "chat", "FAIL", code=status, latency_ms=latency, error_category="HistoryCodeMissing")

        if not self._contains_marker(content):
            return TestResult("chat_multi_turn", "chat", "FAIL", code=status, latency_ms=latency, error_category="MarkerMissing")

        return TestResult("chat_multi_turn", "chat", "PASS", code=status, latency_ms=latency, usage=self._extract_usage(data))

    def test_chat_long95(self) -> TestResult:
        """Long 95 messages history scenario."""
        messages: list[dict[str, str]] = [
            {
                "role": "system",
                "content": (
                    f"You are a verification assistant handling long history. Every reply must include {NEUTRAL_MARKER} "
                    "and repeat the history code from earlier user messages when asked."
                ),
            },
            {"role": "developer", "content": "Maintain consistency across long dialogue."},
        ]
        # 2 system/developer + 46 alternating pairs (92) + 1 final user = 95 messages
        for i in range(46):
            if i == 0:
                messages.append({"role": "user", "content": f"Remember this history code: {HISTORY_CODE_SENTINEL}"})
                messages.append({"role": "assistant", "content": "Noted."})
            else:
                messages.append({"role": "user", "content": f"history ping message {i}"})
                messages.append({"role": "assistant", "content": f"history pong ack {i}"})
        messages.append({"role": "user", "content": "What was the history code? Reply concisely."})
        assert len(messages) == 95, f"Expected 95 messages, got {len(messages)}"

        payload = {"model": self.model, "messages": messages, "stream": False}
        try:
            status, data, latency, _ = self._execute_http("POST", "/v1/chat/completions", payload=payload)
        except _RequestStoppedError as e:
            return TestResult("chat_long95", "chat", "STOPPED", latency_ms=0, error_category=str(e))
        except Exception as e:
            return TestResult("chat_long95", "chat", "FAIL", latency_ms=0, error_category=type(e).__name__)

        if status != 200:
            cat = "RateLimit429" if status == 429 else ("Forbidden403" if status == 403 else f"HTTP_{status}")
            return TestResult("chat_long95", "chat", "FAIL", code=status, latency_ms=latency, error_category=cat)

        choices = data.get("choices", []) if isinstance(data, dict) else []
        msg = choices[0].get("message", {}) if choices and isinstance(choices[0], dict) else {}
        content = msg.get("content") if isinstance(msg, dict) else None
        if not isinstance(content, str) or not content.strip():
            return TestResult("chat_long95", "chat", "FAIL", code=status, latency_ms=latency, error_category="EmptyContent")

        if HISTORY_CODE_SENTINEL not in content:
            return TestResult("chat_long95", "chat", "FAIL", code=status, latency_ms=latency, error_category="HistoryCodeMissing")

        if not self._contains_marker(content):
            return TestResult("chat_long95", "chat", "FAIL", code=status, latency_ms=latency, error_category="MarkerMissing")

        return TestResult("chat_long95", "chat", "PASS", code=status, latency_ms=latency, usage=self._extract_usage(data))

    def test_chat_tool_call(self) -> TestResult:
        """2-step tool call loop with offline noop tool."""
        tool_definition = {
            "type": "function",
            "function": {
                "name": "noop_offline_lookup",
                "description": "Offline no-op lookup tool for schema verification",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "query": {"type": "string", "description": "Search target"}
                    },
                    "required": ["query"],
                },
            },
        }
        messages: list[dict[str, Any]] = [
            {
                "role": "system",
                "content": (
                    "You are a tool testing assistant. Always call the provided tool first. "
                    f"When you receive the tool result, include the exact tool result verbatim and {NEUTRAL_MARKER} in your final response."
                ),
            },
            {"role": "user", "content": "Perform lookup for target alpha."},
        ]
        payload_step1 = {
            "model": self.model,
            "messages": messages,
            "tools": [tool_definition],
            "stream": False,
        }

        # Step 1: Model must return a non-empty tool_call
        try:
            status1, data1, latency1, _ = self._execute_http("POST", "/v1/chat/completions", payload=payload_step1)
        except _RequestStoppedError as e:
            return TestResult("chat_tool_call", "chat", "STOPPED", latency_ms=0, error_category=str(e))
        except Exception as e:
            return TestResult("chat_tool_call", "chat", "FAIL", latency_ms=0, error_category=type(e).__name__)

        if status1 != 200:
            cat = "RateLimit429" if status1 == 429 else ("Forbidden403" if status1 == 403 else f"HTTP_{status1}")
            return TestResult("chat_tool_call", "chat", "FAIL", code=status1, latency_ms=latency1, error_category=cat)

        choices1 = data1.get("choices", []) if isinstance(data1, dict) else []
        if not choices1 or not isinstance(choices1[0], dict):
            return TestResult("chat_tool_call", "chat", "FAIL", code=status1, latency_ms=latency1, error_category="EmptyChoices")

        msg1 = choices1[0].get("message", {})
        tool_calls = msg1.get("tool_calls") if isinstance(msg1, dict) else None
        if not isinstance(tool_calls, list) or len(tool_calls) == 0:
            return TestResult(
                "chat_tool_call",
                "chat",
                "FAIL",
                code=status1,
                latency_ms=latency1,
                usage=self._extract_usage(data1 if isinstance(data1, dict) else {}),
                error_category="ToolCallNotTriggered",
            )

        # Step 2: Feed offline tool_result back with unique sentinel
        first_call = tool_calls[0] if isinstance(tool_calls[0], dict) else {}
        call_id = first_call.get("id", "call_noop_1")
        messages.append(msg1)
        messages.append({
            "role": "tool",
            "tool_call_id": call_id,
            "content": json.dumps({"status": "ok", "result": TOOL_RESULT_SENTINEL, "marker": NEUTRAL_MARKER}),
        })

        payload_step2 = {"model": self.model, "messages": messages, "stream": False}
        try:
            status2, data2, latency2, _ = self._execute_http("POST", "/v1/chat/completions", payload=payload_step2)
        except _RequestStoppedError as e:
            return TestResult("chat_tool_call", "chat", "STOPPED", latency_ms=latency1, error_category=str(e))
        except Exception as e:
            return TestResult("chat_tool_call", "chat", "FAIL", latency_ms=latency1, error_category=type(e).__name__)

        if status2 != 200:
            cat = "RateLimit429" if status2 == 429 else ("Forbidden403" if status2 == 403 else f"HTTP_{status2}")
            return TestResult("chat_tool_call", "chat", "FAIL", code=status2, latency_ms=latency1 + latency2, error_category=cat)

        choices2 = data2.get("choices", []) if isinstance(data2, dict) else []
        msg2 = choices2[0].get("message", {}) if choices2 and isinstance(choices2[0], dict) else {}
        content2 = msg2.get("content") if isinstance(msg2, dict) else None
        if not isinstance(content2, str) or not content2.strip():
            return TestResult("chat_tool_call", "chat", "FAIL", code=status2, latency_ms=latency1 + latency2, error_category="EmptyToolFollowup")

        if TOOL_RESULT_SENTINEL not in content2:
            return TestResult("chat_tool_call", "chat", "FAIL", code=status2, latency_ms=latency1 + latency2, error_category="ToolResultMissing")

        if not self._contains_marker(content2):
            return TestResult("chat_tool_call", "chat", "FAIL", code=status2, latency_ms=latency1 + latency2, error_category="MarkerMissing")

        return TestResult(
            name="chat_tool_call",
            protocol="chat",
            status="PASS",
            code=status2,
            latency_ms=latency1 + latency2,
            usage=self._extract_usage(data2),
        )

    def test_chat_prompt_cache(self) -> TestResult:
        """Prompt cache repetition observation (marks NOT_OBSERVED if absent, no fabrication)."""
        ref_lines = [
            f"[REF-{i:03d}] protocol=openai_compat component=prompt_cache_probe segment=baseline_{i % 10} status=verified"
            for i in range(1, 116)
        ]
        prompt_text = (
            "Structured Cache Reference Baseline:\n"
            + "\n".join(ref_lines)
            + f"\n\nWhen the user says ping, reply with exact token {NEUTRAL_MARKER} and nothing else."
        )
        payload = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": prompt_text},
                {"role": "user", "content": "ping"},
            ],
            "max_tokens": 128,
            "stream": False,
        }
        # Send twice and verify both responses contain NEUTRAL_MARKER
        try:
            status1, data1, latency1, _ = self._execute_http("POST", "/v1/chat/completions", payload=payload)
        except _RequestStoppedError as e:
            return TestResult("chat_prompt_cache", "chat", "STOPPED", latency_ms=0, error_category=str(e))
        except Exception as e:
            return TestResult("chat_prompt_cache", "chat", "FAIL", latency_ms=0, error_category=type(e).__name__)

        if status1 != 200:
            cat = "RateLimit429" if status1 == 429 else ("Forbidden403" if status1 == 403 else f"HTTP_{status1}")
            return TestResult("chat_prompt_cache", "chat", "FAIL", code=status1, latency_ms=latency1, error_category=cat)

        choices1 = data1.get("choices", []) if isinstance(data1, dict) else []
        msg1 = choices1[0].get("message", {}) if choices1 and isinstance(choices1[0], dict) else {}
        content1 = msg1.get("content") if isinstance(msg1, dict) else None
        if not isinstance(content1, str) or not content1.strip():
            return TestResult("chat_prompt_cache", "chat", "FAIL", code=status1, latency_ms=latency1, error_category="EmptyContent")
        if not self._contains_marker(content1):
            return TestResult("chat_prompt_cache", "chat", "FAIL", code=status1, latency_ms=latency1, error_category="MarkerMissing")

        try:
            status2, data2, latency2, _ = self._execute_http("POST", "/v1/chat/completions", payload=payload)
        except _RequestStoppedError as e:
            return TestResult("chat_prompt_cache", "chat", "STOPPED", latency_ms=latency1, error_category=str(e))
        except Exception as e:
            return TestResult("chat_prompt_cache", "chat", "FAIL", latency_ms=latency1, error_category=type(e).__name__)

        if status2 != 200:
            cat = "RateLimit429" if status2 == 429 else ("Forbidden403" if status2 == 403 else f"HTTP_{status2}")
            return TestResult("chat_prompt_cache", "chat", "FAIL", code=status2, latency_ms=latency2, error_category=cat)

        choices2 = data2.get("choices", []) if isinstance(data2, dict) else []
        msg2 = choices2[0].get("message", {}) if choices2 and isinstance(choices2[0], dict) else {}
        content2 = msg2.get("content") if isinstance(msg2, dict) else None
        if not isinstance(content2, str) or not content2.strip():
            return TestResult("chat_prompt_cache", "chat", "FAIL", code=status2, latency_ms=latency2, error_category="EmptyContent")
        if not self._contains_marker(content2):
            return TestResult("chat_prompt_cache", "chat", "FAIL", code=status2, latency_ms=latency2, error_category="MarkerMissing")

        usage = self._extract_usage(data2 if isinstance(data2, dict) else {})
        return TestResult(
            name="chat_prompt_cache",
            protocol="chat",
            status="PASS",
            code=status2,
            latency_ms=latency2,
            usage=usage,
        )

    def test_messages_basic(self) -> TestResult:
        """Anthropic Messages protocol: basic non-streaming."""
        payload = {
            "model": self.messages_model,
            "max_tokens": 1024,
            "system": [
                {
                    "type": "text",
                    "text": f"You are a verification harness. When the user says ping, reply with exact token {NEUTRAL_MARKER}.",
                    "cache_control": {"type": "ephemeral"},
                }
            ],
            "messages": [
                {"role": "user", "content": "ping"}
            ],
        }
        headers = {
            "x-api-key": self.api_key,
            "anthropic-version": "2023-06-01",
        }
        try:
            status, data, latency, _ = self._execute_http("POST", "/v1/messages", payload=payload, headers=headers)
        except _RequestStoppedError as e:
            return TestResult("messages_basic", "messages", "STOPPED", latency_ms=0, error_category=str(e))
        except Exception as e:
            return TestResult("messages_basic", "messages", "FAIL", latency_ms=0, error_category=type(e).__name__)

        if status != 200:
            cat = "RateLimit429" if status == 429 else ("Forbidden403" if status == 403 else f"HTTP_{status}")
            return TestResult("messages_basic", "messages", "FAIL", code=status, latency_ms=latency, error_category=cat)

        content = data.get("content") if isinstance(data, dict) else None
        if not isinstance(content, list) or len(content) == 0:
            return TestResult("messages_basic", "messages", "FAIL", code=status, latency_ms=latency, error_category="EmptyContent")

        text_parts: list[str] = []
        for block in content:
            if isinstance(block, dict):
                txt = block.get("text")
                if isinstance(txt, str) and txt:
                    text_parts.append(txt)
        combined_text = "".join(text_parts)
        if not combined_text:
            return TestResult("messages_basic", "messages", "FAIL", code=status, latency_ms=latency, error_category="EmptyContent")

        if not self._contains_marker(combined_text):
            return TestResult("messages_basic", "messages", "FAIL", code=status, latency_ms=latency, error_category="MarkerMissing")

        return TestResult(
            name="messages_basic",
            protocol="messages",
            status="PASS",
            code=status,
            latency_ms=latency,
            usage=self._extract_usage(data if isinstance(data, dict) else {}),
        )

    def test_responses_basic(self) -> TestResult:
        """OpenAI responses protocol: basic non-streaming."""
        payload = {
            "model": self.model,
            "instructions": f"You are a verification harness. When the user says ping, reply with exact token {NEUTRAL_MARKER}.",
            "input": "ping",
        }
        try:
            status, data, latency, _ = self._execute_http("POST", "/v1/responses", payload=payload)
        except _RequestStoppedError as e:
            return TestResult("responses_basic", "responses", "STOPPED", latency_ms=0, error_category=str(e))
        except Exception as e:
            return TestResult("responses_basic", "responses", "FAIL", latency_ms=0, error_category=type(e).__name__)

        if status != 200:
            cat = "RateLimit429" if status == 429 else ("Forbidden403" if status == 403 else f"HTTP_{status}")
            return TestResult("responses_basic", "responses", "FAIL", code=status, latency_ms=latency, error_category=cat)

        extracted_text = self._extract_responses_text(data)
        if not isinstance(data, dict) or not extracted_text.strip():
            return TestResult("responses_basic", "responses", "FAIL", code=status, latency_ms=latency, error_category="EmptyContent")

        if not self._contains_marker(extracted_text):
            return TestResult("responses_basic", "responses", "FAIL", code=status, latency_ms=latency, error_category="MarkerMissing")

        return TestResult(
            name="responses_basic",
            protocol="responses",
            status="PASS",
            code=status,
            latency_ms=latency,
            usage=self._extract_usage(data),
        )

    def test_native_basic(self) -> TestResult:
        """Gemini Native protocol: generateContent non-streaming."""
        endpoint = f"/v1beta/models/{self.model}:generateContent"
        payload = {
            "systemInstruction": {
                "parts": [
                    {"text": f"You are a verification harness. When the user says ping, reply with exact token {NEUTRAL_MARKER}."}
                ]
            },
            "contents": [
                {"role": "user", "parts": [{"text": "ping"}]}
            ],
        }
        try:
            status, data, latency, _ = self._execute_http("POST", endpoint, payload=payload)
        except _RequestStoppedError as e:
            return TestResult("native_basic", "native", "STOPPED", latency_ms=0, error_category=str(e))
        except Exception as e:
            return TestResult("native_basic", "native", "FAIL", latency_ms=0, error_category=type(e).__name__)

        if status != 200:
            cat = "RateLimit429" if status == 429 else ("Forbidden403" if status == 403 else f"HTTP_{status}")
            return TestResult("native_basic", "native", "FAIL", code=status, latency_ms=latency, error_category=cat)

        candidates = data.get("candidates") if isinstance(data, dict) else None
        if not isinstance(candidates, list) or len(candidates) == 0:
            return TestResult("native_basic", "native", "FAIL", code=status, latency_ms=latency, error_category="EmptyCandidates")

        cand0 = candidates[0] if isinstance(candidates[0], dict) else {}
        raw_content = cand0.get("content")
        content_obj = raw_content if isinstance(raw_content, dict) else {}
        parts = content_obj.get("parts")
        if not isinstance(parts, list) or not parts:
            return TestResult("native_basic", "native", "FAIL", code=status, latency_ms=latency, error_category="EmptyParts")

        text_parts = [p["text"] for p in parts if isinstance(p, dict) and isinstance(p.get("text"), str) and p["text"]]
        combined_text = "".join(text_parts)
        if not combined_text:
            return TestResult("native_basic", "native", "FAIL", code=status, latency_ms=latency, error_category="EmptyParts")

        if not self._contains_marker(combined_text):
            return TestResult("native_basic", "native", "FAIL", code=status, latency_ms=latency, error_category="MarkerMissing")

        return TestResult(
            name="native_basic",
            protocol="native",
            status="PASS",
            code=status,
            latency_ms=latency,
            usage=self._extract_usage(data if isinstance(data, dict) else {}),
        )

    def test_extra_model(self) -> TestResult:
        """Optional test against extra model if specified."""
        if not self.extra_model:
            return TestResult("extra_model", "chat", "SKIPPED")
        payload = {
            "model": self.extra_model,
            "messages": [
                {
                    "role": "system",
                    "content": f"You are a verification harness. When the user says ping, reply with exact token {NEUTRAL_MARKER}.",
                },
                {"role": "user", "content": "ping"},
            ],
            "max_tokens": 128,
            "stream": False,
        }
        try:
            status, data, latency, _ = self._execute_http("POST", "/v1/chat/completions", payload=payload)
        except _RequestStoppedError as e:
            return TestResult("extra_model", "chat", "STOPPED", latency_ms=0, error_category=str(e))
        except Exception as e:
            return TestResult("extra_model", "chat", "FAIL", latency_ms=0, error_category=type(e).__name__)

        if status != 200:
            cat = "RateLimit429" if status == 429 else ("Forbidden403" if status == 403 else f"HTTP_{status}")
            return TestResult("extra_model", "chat", "FAIL", code=status, latency_ms=latency, error_category=cat)

        choices = data.get("choices", []) if isinstance(data, dict) else []
        msg = choices[0].get("message", {}) if choices and isinstance(choices[0], dict) else {}
        content = msg.get("content") if isinstance(msg, dict) else None
        if not isinstance(content, str) or not content.strip():
            return TestResult("extra_model", "chat", "FAIL", code=status, latency_ms=latency, error_category="EmptyContent")

        if not self._contains_marker(content):
            return TestResult("extra_model", "chat", "FAIL", code=status, latency_ms=latency, error_category="MarkerMissing")

        return TestResult("extra_model", "chat", "PASS", code=status, latency_ms=latency, usage=self._extract_usage(data))

    def test_fingerprint_fixture(self) -> TestResult:
        """Evaluate local fingerprint fixture text file if provided. Never fake pass if absent."""
        if not self.fixtures_file:
            return TestResult(
                name="fingerprint_fixture",
                protocol="chat",
                status="SKIPPED",
                error_category="FixtureNotConfigured_NOT_OBSERVED",
            )
        fixture_path = Path(self.fixtures_file)
        if not fixture_path.is_file():
            return TestResult(
                name="fingerprint_fixture",
                protocol="chat",
                status="FAIL",
                error_category="FixtureFileNotFound",
            )

        fixture_text = fixture_path.read_text(encoding="utf-8").strip()
        if self.zwsp_fixture:
            fixture_text = re.sub(
                r"\b([A-Za-z]{2})([A-Za-z]{2,})",
                lambda m: m.group(1) + "\u200b" + m.group(2),
                fixture_text,
            )
        system_content = (
            f"{fixture_text}\n\nWhen the user says ping, reply with {NEUTRAL_MARKER}."
            if fixture_text
            else f"When the user says ping, reply with {NEUTRAL_MARKER}."
        )
        payload = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": system_content},
                {"role": "user", "content": "ping"},
            ],
            "max_tokens": 128,
            "stream": False,
        }
        try:
            status, data, latency, _ = self._execute_http("POST", "/v1/chat/completions", payload=payload)
        except _RequestStoppedError as e:
            return TestResult("fingerprint_fixture", "chat", "STOPPED", latency_ms=0, error_category=str(e))
        except Exception as e:
            return TestResult("fingerprint_fixture", "chat", "FAIL", latency_ms=0, error_category=type(e).__name__)

        if status != 200:
            cat = "RateLimit429" if status == 429 else ("Forbidden403" if status == 403 else f"HTTP_{status}")
            return TestResult("fingerprint_fixture", "chat", "FAIL", code=status, latency_ms=latency, error_category=cat)

        if not isinstance(data, dict):
            return TestResult("fingerprint_fixture", "chat", "FAIL", code=status, latency_ms=latency, error_category="NonJsonBody")

        choices = data.get("choices")
        if not isinstance(choices, list) or len(choices) == 0:
            return TestResult("fingerprint_fixture", "chat", "FAIL", code=status, latency_ms=latency, error_category="EmptyChoices")

        choice = choices[0]
        if not isinstance(choice, dict):
            return TestResult("fingerprint_fixture", "chat", "FAIL", code=status, latency_ms=latency, error_category="InvalidChoice")

        msg = choice.get("message")
        content = msg.get("content") if isinstance(msg, dict) else None
        tool_calls = msg.get("tool_calls") if isinstance(msg, dict) else None
        if not (isinstance(content, str) and content.strip()) and not tool_calls:
            return TestResult("fingerprint_fixture", "chat", "FAIL", code=status, latency_ms=latency, error_category="EmptyContentAndNoToolCalls")

        if not self._contains_marker(content):
            return TestResult("fingerprint_fixture", "chat", "FAIL", code=status, latency_ms=latency, error_category="MarkerMissing")

        return TestResult(
            name="fingerprint_fixture",
            protocol="chat",
            status="PASS",
            code=status,
            latency_ms=latency,
            usage=self._extract_usage(data),
        )


def build_test_plan(
    runner: AcceptanceRunner,
    protocols: set[str],
    has_extra_model: bool,
    has_fixtures: bool,
) -> list[tuple[str, Any]]:
    """Build ordered plan of tests according to active protocols."""
    plan: list[tuple[str, Any]] = []

    if "chat" in protocols:
        plan.append(("chat_basic", runner.test_chat_basic))
        plan.append(("chat_sse", runner.test_chat_sse))
        plan.append(("chat_multi_turn", runner.test_chat_multi_turn))
        plan.append(("chat_long95", runner.test_chat_long95))
        plan.append(("chat_tool_call", runner.test_chat_tool_call))
        plan.append(("chat_prompt_cache", runner.test_chat_prompt_cache))

    if "messages" in protocols:
        plan.append(("messages_basic", runner.test_messages_basic))

    if "responses" in protocols:
        plan.append(("responses_basic", runner.test_responses_basic))

    if "native" in protocols:
        plan.append(("native_basic", runner.test_native_basic))

    if has_extra_model:
        plan.append(("extra_model", runner.test_extra_model))

    if has_fixtures:
        plan.append(("fingerprint_fixture", runner.test_fingerprint_fixture))

    return plan


def run_tests(
    runner: AcceptanceRunner,
    plan: list[tuple[str, Any]],
    concurrency: int = 1,
) -> list[TestResult]:
    """Execute test plan either serially or with max concurrency 2."""
    results: list[TestResult] = []

    if concurrency <= 1:
        for name, fn in plan:
            can_run, reason = runner.state.can_request()
            if not can_run:
                results.append(TestResult(name=name, protocol=name.split("_")[0], status="STOPPED", error_category=reason))
                continue
            res = fn()
            results.append(res)
            if runner.state.stopped:
                break
    else:
        # Max concurrency 2
        with concurrent.futures.ThreadPoolExecutor(max_workers=min(concurrency, 2)) as executor:
            future_to_name = {}
            for name, fn in plan:
                can_run, reason = runner.state.can_request()
                if not can_run:
                    results.append(TestResult(name=name, protocol=name.split("_")[0], status="STOPPED", error_category=reason))
                    continue
                future = executor.submit(fn)
                future_to_name[future] = name

            for future in concurrent.futures.as_completed(future_to_name):
                try:
                    res = future.result()
                    results.append(res)
                except Exception as e:
                    name = future_to_name[future]
                    results.append(TestResult(name=name, protocol=name.split("_")[0], status="FAIL", error_category=type(e).__name__))

    return results


def generate_report(
    runner: AcceptanceRunner,
    results: list[TestResult],
    total_duration_ms: float,
) -> dict[str, Any]:
    """Generate strictly desensitized JSON report."""
    passed = sum(1 for r in results if r.status == "PASS")
    failed = sum(1 for r in results if r.status == "FAIL")
    stopped = sum(1 for r in results if r.status == "STOPPED")
    skipped = sum(1 for r in results if r.status == "SKIPPED")

    return {
        "summary": {
            "total_tests": len(results),
            "passed": passed,
            "failed": failed,
            "stopped": stopped,
            "skipped": skipped,
            "total_requests": runner.state.total_requests,
            "max_requests": runner.state.max_requests,
            "stopped_early": runner.state.stopped,
            "stop_reason": runner.state.stop_reason,
            "duration_ms": round(total_duration_ms, 2),
            "all_passed": (failed == 0 and stopped == 0 and passed > 0),
        },
        "tests": [r.to_dict() for r in results],
    }


def main() -> int:
    ap = argparse.ArgumentParser(
        description="Loopback Acceptance Runner for CPA Antigravity Prompt Shim & CPA Upstream"
    )
    ap.add_argument("--live", action="store_true", help="Explicit gate to execute live upstream requests")
    ap.add_argument("--base-url", default="http://127.0.0.1:8328", help="Base URL for CPA upstream (default: http://127.0.0.1:8328)")
    ap.add_argument("--key-file", help="Path to 0600-permission API key file")
    ap.add_argument("--cpa-config", help="Path to CPA config file to extract key")
    ap.add_argument("--model", default=DEFAULT_MODEL, help=f"Primary model to test (default: {DEFAULT_MODEL})")
    ap.add_argument("--messages-model", help="Optional model override for /v1/messages protocol (defaults to --model)")
    ap.add_argument("--extra-model", help="Optional secondary model to test")
    ap.add_argument("--protocols", default="chat", help="Comma-separated protocols to test: chat,messages,responses,native")
    ap.add_argument("--chat", action=argparse.BooleanOptionalAction, default=None, help="Enable/disable chat protocol")
    ap.add_argument("--messages", action=argparse.BooleanOptionalAction, default=None, help="Enable/disable messages protocol")
    ap.add_argument("--responses", action=argparse.BooleanOptionalAction, default=None, help="Enable/disable responses protocol")
    ap.add_argument("--native", action=argparse.BooleanOptionalAction, default=None, help="Enable/disable native protocol")
    ap.add_argument("--max-requests", type=int, default=DEFAULT_MAX_REQUESTS, help=f"Strong upper limit on requests (default: {DEFAULT_MAX_REQUESTS})")
    ap.add_argument("--concurrency", type=int, default=1, help="Concurrency (1 or 2, default: 1)")
    ap.add_argument("--timeout", type=float, default=DEFAULT_TIMEOUT_SECONDS, help=f"HTTP timeout in seconds (default: {DEFAULT_TIMEOUT_SECONDS})")
    ap.add_argument("--fixtures-file", help="Optional path to local fixtures file for fingerprint evaluation")
    ap.add_argument("--zwsp-fixture", action="store_true", help="Insert zero-width spaces into English words in fixture text")
    ap.add_argument("--report", help="Absolute path to output JSON acceptance report")

    args = ap.parse_args()

    # Safe Live Gate: If --live is not explicitly provided, exit safely without sending requests
    if not args.live:
        print("[SAFE-GATE] Acceptance runner executed without --live flag.")
        print("[SAFE-GATE] Upstream network calls are gated off. No requests sent.")
        print("[SAFE-GATE] To execute against upstream, explicitly pass --live and --base-url http://127.0.0.1:8328.")
        return 0

    # 1. Base URL Validation
    try:
        clean_base_url = validate_base_url(args.base_url, is_live=True)
    except Exception as e:
        print(f"[ERROR] URL validation failed: {e}", file=sys.stderr)
        return 2

    # 2. Key Resolution
    cpa_cfg_arg = getattr(args, "cpa_config", None)
    if not args.key_file and not cpa_cfg_arg:
        print("[ERROR] In live mode, either --key-file or --cpa-config must be specified.", file=sys.stderr)
        return 2

    api_key: str | None = None
    try:
        if args.key_file:
            api_key = load_key_from_file(args.key_file)
        elif cpa_cfg_arg:
            api_key = load_key_from_cpa_config(cpa_cfg_arg)
    except Exception as e:
        print(f"[ERROR] Key retrieval failed: {e}", file=sys.stderr)
        return 2

    if not api_key:
        print("[ERROR] Failed to obtain valid API key.", file=sys.stderr)
        return 2

    # 3. Report path validation (must be absolute)
    if args.report:
        if not os.path.isabs(args.report):
            print("[ERROR] --report must be an absolute path.", file=sys.stderr)
            return 2

    # 4. Concurrency and timeout checks
    if args.concurrency > 2:
        print("[ERROR] Concurrency cannot exceed 2 (serial default, max 2).", file=sys.stderr)
        return 2
    if args.timeout <= 0:
        print("[ERROR] Timeout must be positive.", file=sys.stderr)
        return 2

    # 5. Protocol resolution
    all_protocols = {"chat", "messages", "responses", "native"}
    requested_protocols = {p.strip().lower() for p in args.protocols.split(",") if p.strip()}
    for p, flag in (("chat", args.chat), ("messages", args.messages), ("responses", args.responses), ("native", args.native)):
        if flag is True:
            requested_protocols.add(p)
        elif flag is False:
            requested_protocols.discard(p)

    active_protocols = requested_protocols & all_protocols
    if not active_protocols:
        print("[ERROR] No valid protocols enabled to test.", file=sys.stderr)
        return 2

    # 6. Initialize Runner and Plan
    runner = AcceptanceRunner(
        base_url=clean_base_url,
        api_key=api_key,
        model=args.model,
        messages_model=args.messages_model,
        extra_model=args.extra_model,
        max_requests=args.max_requests,
        fixtures_file=args.fixtures_file,
        zwsp_fixture=args.zwsp_fixture,
        timeout_seconds=args.timeout,
    )

    plan = build_test_plan(
        runner=runner,
        protocols=active_protocols,
        has_extra_model=bool(args.extra_model),
        has_fixtures=bool(args.fixtures_file),
    )

    started_all = time.monotonic()
    results = run_tests(runner, plan, concurrency=args.concurrency)
    duration_ms = (time.monotonic() - started_all) * 1000.0

    report = generate_report(runner, results, duration_ms)

    # 7. Print desensitized console summary
    print("=" * 60)
    print("ACCEPTANCE RUNNER EXECUTION SUMMARY")
    print("=" * 60)
    for res in results:
        detail = f"status={res.code}" if res.code else f"err={res.error_category}"
        first_frame = f" ff={res.first_frame_ms:.1f}ms" if res.first_frame_ms is not None else ""
        print(f"[{res.status}] {res.name:<25} ({detail}{first_frame}, latency={res.latency_ms:.1f}ms)")

    sm = report["summary"]
    print("-" * 60)
    print(
        f"TOTAL: {sm['total_tests']} | PASS: {sm['passed']} | FAIL: {sm['failed']} | "
        f"STOPPED: {sm['stopped']} | REQUESTS: {sm['total_requests']}/{sm['max_requests']}"
    )
    if sm["stopped_early"]:
        print(f"EARLY STOP REASON: {sm['stop_reason']}")
    print("=" * 60)

    # 8. Write report if requested
    if args.report:
        try:
            Path(args.report).parent.mkdir(parents=True, exist_ok=True)
            Path(args.report).write_text(json.dumps(report, indent=2), encoding="utf-8")
            print(f"Desensitized report saved to: {args.report}")
        except Exception as e:
            print(f"[ERROR] Failed to write report file: {e}", file=sys.stderr)
            return 1

    return 0 if sm["all_passed"] else 1


if __name__ == "__main__":
    sys.exit(main())
