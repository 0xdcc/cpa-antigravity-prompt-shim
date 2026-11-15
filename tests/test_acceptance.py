#!/usr/bin/env python3
"""Comprehensive regression and gate unit tests for acceptance runner."""
from __future__ import annotations

import io
import json
import os
from pathlib import Path
import stat
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import adapters
from tools import acceptance


class MockUpstreamHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *args):
        pass

    def _handle_post_or_redirect(self):
        if getattr(self.server, "server_delay", 0.0) > 0:
            time.sleep(self.server.server_delay)

        redirect_url = getattr(self.server, "force_redirect_url", None)
        if redirect_url:
            self.server.request_count += 1
            self.server.received_requests.append({
                "path": self.path,
                "headers": dict(self.headers),
                "body": {},
            })
            self.send_response(302)
            self.send_header("Location", redirect_url)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return True
        return False

    def do_POST(self):
        content_length = int(self.headers.get("Content-Length", "0"))
        body = self.rfile.read(content_length) if content_length > 0 else b""
        if self._handle_post_or_redirect():
            return

        req_json = {}
        try:
            req_json = json.loads(body.decode("utf-8")) if body else {}
        except Exception:
            pass

        self.server.request_count += 1
        req_idx = self.server.request_count
        self.server.received_requests.append({
            "path": self.path,
            "headers": dict(self.headers),
            "body": req_json,
        })

        unrelated_mode = getattr(self.server, "force_unrelated_text", False) or (
            getattr(self.server, "force_unrelated_on_request_num", None) == req_idx
        )

        # Scenario overrides based on server flags
        if getattr(self.server, "force_429", False):
            self.send_response(429)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(b'{"error": {"message": "Rate limit exceeded"}}')
            return

        if getattr(self.server, "force_403", False):
            self.send_response(403)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(b'{"error": {"message": "Forbidden"}}')
            return

        if getattr(self.server, "force_500", False):
            self.send_response(500)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(b'{"error": "Internal server error"}')
            return

        if getattr(self.server, "force_empty_content", False):
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps({
                "id": "chat-mock-empty",
                "choices": [{"message": {"role": "assistant", "content": ""}}],
            }).encode())
            return

        # 1. Chat completions
        if self.path == "/v1/chat/completions":
            is_stream = req_json.get("stream", False)
            if is_stream:
                if getattr(self.server, "force_broken_stream", False):
                    # Incomplete stream without [DONE] or finish marker
                    self.send_response(200)
                    self.send_header("Content-Type", "text/event-stream")
                    self.end_headers()
                    self.wfile.write(b'data: {"choices":[{"delta":{"content":"part"}}]}\n\n')
                    self.wfile.flush()
                    return

                # Normal SSE stream
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.end_headers()
                if unrelated_mode:
                    chunk = {
                        "choices": [{"delta": {"content": "UNRELATED_NO_TOOL_OR_MARKER"}, "finish_reason": "stop"}],
                        "usage": {"prompt_tokens": 10, "completion_tokens": 2, "total_tokens": 12},
                    }
                    self.wfile.write(f"data: {json.dumps(chunk)}\n\n".encode())
                    self.wfile.write(b"data: [DONE]\n\n")
                    self.wfile.flush()
                    return
                self.wfile.write(b'data: {"choices":[{"delta":{"content":"Hello "}}]}\n\n')
                self.wfile.flush()
                time.sleep(0.01)
                marker_suffix = "" if getattr(self.server, "force_missing_marker", False) else acceptance.NEUTRAL_MARKER
                chunk2 = {
                    "choices": [{"delta": {"content": f"world {marker_suffix}".strip()}, "finish_reason": "stop"}],
                    "usage": {"prompt_tokens": 10, "completion_tokens": 2, "total_tokens": 12},
                }
                self.wfile.write(f"data: {json.dumps(chunk2)}\n\n".encode())
                self.wfile.write(b'data: [DONE]\n\n')
                self.wfile.flush()
                return

            # Non-streaming chat
            # Check for tool definitions
            if "tools" in req_json:
                messages = req_json.get("messages", [])
                has_tool_result = any(m.get("role") == "tool" for m in messages)
                if unrelated_mode:
                    resp_data = {
                        "id": "chat-tool-unrelated",
                        "choices": [{
                            "message": {
                                "role": "assistant",
                                "content": "UNRELATED_NO_TOOL_OR_MARKER",
                            },
                            "finish_reason": "stop",
                        }],
                        "usage": {"prompt_tokens": 20, "completion_tokens": 5, "total_tokens": 25},
                    }
                elif not has_tool_result:
                    # Return tool call request
                    resp_data = {
                        "id": "chat-tool-1",
                        "choices": [{
                            "message": {
                                "role": "assistant",
                                "content": None,
                                "tool_calls": [{
                                    "id": "call_noop_1",
                                    "type": "function",
                                    "function": {
                                        "name": "noop_offline_lookup",
                                        "arguments": json.dumps({"query": "target alpha"}),
                                    },
                                }],
                            },
                            "finish_reason": "tool_calls",
                        }],
                        "usage": {"prompt_tokens": 20, "completion_tokens": 15, "total_tokens": 35},
                    }
                else:
                    # Final tool response echoing tool result sentinel and marker per protocol contract
                    tool_msgs = [m for m in messages if m.get("role") == "tool"]
                    raw_tool_content = str(tool_msgs[-1].get("content", "")) if tool_msgs else ""
                    extracted_result = raw_tool_content
                    try:
                        parsed_tool = json.loads(raw_tool_content)
                        if isinstance(parsed_tool, dict) and "result" in parsed_tool:
                            extracted_result = str(parsed_tool["result"])
                    except Exception:
                        pass
                    parts = ["Tool result processed:"]
                    if not getattr(self.server, "force_missing_tool_sentinel", False) and extracted_result:
                        parts.append(extracted_result)
                    if not getattr(self.server, "force_missing_marker", False):
                        parts.append(acceptance.NEUTRAL_MARKER)
                    resp_data = {
                        "id": "chat-tool-2",
                        "choices": [{
                            "message": {
                                "role": "assistant",
                                "content": " ".join(parts),
                            },
                            "finish_reason": "stop",
                        }],
                        "usage": {"prompt_tokens": 40, "completion_tokens": 10, "total_tokens": 50},
                    }
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(json.dumps(resp_data).encode())
                return

            # Check for tool_result follow-up (step 2 sends messages with role=tool without tools param)
            messages = req_json.get("messages", [])
            tool_msgs = [m for m in messages if isinstance(m, dict) and m.get("role") == "tool"]
            if tool_msgs:
                if unrelated_mode:
                    content_str = "UNRELATED_NO_TOOL_OR_MARKER"
                else:
                    raw_tool_content = str(tool_msgs[-1].get("content", ""))
                    extracted_result = raw_tool_content
                    try:
                        parsed_tool = json.loads(raw_tool_content)
                        if isinstance(parsed_tool, dict) and "result" in parsed_tool:
                            extracted_result = str(parsed_tool["result"])
                    except Exception:
                        pass
                    parts = ["Tool result processed:"]
                    if not getattr(self.server, "force_missing_tool_sentinel", False) and extracted_result:
                        parts.append(extracted_result)
                    if not getattr(self.server, "force_missing_marker", False):
                        parts.append(acceptance.NEUTRAL_MARKER)
                    content_str = " ".join(parts)
                resp_data = {
                    "id": "chat-tool-2",
                    "choices": [{
                        "message": {
                            "role": "assistant",
                            "content": content_str,
                        },
                        "finish_reason": "stop",
                    }],
                    "usage": {"prompt_tokens": 40, "completion_tokens": 10, "total_tokens": 50},
                }
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(json.dumps(resp_data).encode())
                return

            # Check for cache observation simulation
            cached_val = getattr(self.server, "simulate_cached_tokens", 0)
            usage_payload = {
                "prompt_tokens": 25,
                "completion_tokens": 8,
                "total_tokens": 33,
            }
            if cached_val > 0:
                usage_payload["cached_tokens"] = cached_val
                usage_payload["prompt_tokens_details"] = {"cached_tokens": cached_val}

            if unrelated_mode:
                content_str = "UNRELATED_NO_TOOL_OR_MARKER"
            else:
                parts = ["Acknowledge:"]
                has_history_code = any(
                    isinstance(m, dict)
                    and m.get("role") == "user"
                    and isinstance(m.get("content"), str)
                    and acceptance.HISTORY_CODE_SENTINEL in m["content"]
                    for m in messages
                )
                if has_history_code and not getattr(self.server, "force_missing_history_code", False):
                    parts.append(acceptance.HISTORY_CODE_SENTINEL)
                if not getattr(self.server, "force_missing_marker", False):
                    parts.append(acceptance.NEUTRAL_MARKER)
                content_str = " ".join(parts).strip()

            resp_data = {
                "id": "chat-mock-1",
                "choices": [{
                    "message": {
                        "role": "assistant",
                        "content": content_str,
                    },
                    "finish_reason": "stop",
                }],
                "usage": usage_payload,
            }
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps(resp_data).encode())
            return

        # 2. Anthropic Messages
        if self.path in ("/v1/messages", "/antigravity/v1/messages"):
            text_out = (
                "UNRELATED_NO_TOOL_OR_MARKER"
                if unrelated_mode
                else f"Anthropic response {'' if getattr(self.server, 'force_missing_marker', False) else acceptance.NEUTRAL_MARKER}".strip()
            )
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps({
                "id": "msg-mock-1",
                "type": "message",
                "content": [{"type": "text", "text": text_out}],
                "usage": {"input_tokens": 12, "output_tokens": 6},
            }).encode())
            return

        # 3. Responses API
        if self.path == "/v1/responses":
            marker_str = "" if getattr(self.server, "force_missing_marker", False) else acceptance.NEUTRAL_MARKER
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            if unrelated_mode:
                output_payload = "UNRELATED_NO_TOOL_OR_MARKER"
            elif getattr(self.server, "structured_responses_output", False):
                output_payload = [{
                    "type": "message",
                    "role": "assistant",
                    "content": [{"type": "output_text", "text": f"Responses structured {marker_str}".strip()}],
                }]
            else:
                output_payload = f"Responses format {marker_str}".strip()
            self.wfile.write(json.dumps({
                "id": "resp-mock-1",
                "output": output_payload,
            }).encode())
            return

        # 4. Gemini native
        if ":generateContent" in self.path:
            text_out = (
                "UNRELATED_NO_TOOL_OR_MARKER"
                if unrelated_mode
                else f"Gemini native {'' if getattr(self.server, 'force_missing_marker', False) else acceptance.NEUTRAL_MARKER}".strip()
            )
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps({
                "candidates": [{
                    "content": {
                        "parts": [{"text": text_out}],
                        "role": "model",
                    },
                    "finishReason": "STOP",
                }],
                "usageMetadata": {"promptTokenCount": 8, "candidatesTokenCount": 4, "totalTokenCount": 12},
            }).encode())
            return

        # Default 404
        self.send_response(404)
        self.end_headers()


class AcceptanceRunnerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), MockUpstreamHandler)
        cls.server.request_count = 0
        cls.server.received_requests = []
        cls.server_thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.server_thread.start()
        cls.server_port = cls.server.server_port
        cls.mock_base_url = f"http://127.0.0.1:{cls.server_port}"

        # Temporary files for keys and configs
        cls.tmp_dir = tempfile.TemporaryDirectory()
        cls.tmp_path = Path(cls.tmp_dir.name)

        # 0600 key file
        cls.valid_key_file = cls.tmp_path / "key.txt"
        cls.valid_key_file.write_text("test_secret_api_key_12345\n", encoding="utf-8")
        os.chmod(cls.valid_key_file, 0o600)

        # Insecure 0644 key file
        cls.insecure_key_file = cls.tmp_path / "insecure_key.txt"
        cls.insecure_key_file.write_text("insecure_key\n", encoding="utf-8")
        os.chmod(cls.insecure_key_file, 0o644)

        # CPA TOML config
        cls.cpa_toml = cls.tmp_path / "cpa.toml"
        cls.cpa_toml.write_text('[cpa]\napi_key = "cpa_toml_api_key_abcde"\n', encoding="utf-8")
        os.chmod(cls.cpa_toml, 0o600)

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.server_thread.join(timeout=2)
        cls.tmp_dir.cleanup()

    def setUp(self):
        # Reset server flags
        self.server.request_count = 0
        self.server.received_requests = []
        self.server.force_429 = False
        self.server.force_403 = False
        self.server.force_500 = False
        self.server.force_empty_content = False
        self.server.force_broken_stream = False
        self.server.force_missing_marker = False
        self.server.force_unrelated_text = False
        self.server.force_unrelated_on_request_num = None
        self.server.force_missing_tool_sentinel = False
        self.server.force_missing_history_code = False
        self.server.force_redirect_url = None
        self.server.server_delay = 0.0
        self.server.structured_responses_output = False
        self.server.simulate_cached_tokens = 0

    def test_live_gate_without_live_flag_makes_zero_requests(self):
        """Without --live flag, runner must not touch network and exit 0."""
        with patch("sys.argv", ["acceptance.py", "--base-url", self.mock_base_url]):
            exit_code = acceptance.main()
            self.assertEqual(exit_code, 0)
            self.assertEqual(self.server.request_count, 0)

    def test_base_url_security_validations(self):
        """Reject non-loopback, userinfo, query, fragment, and unallowed live endpoints."""
        # Non-loopback
        with self.assertRaises(ValueError) as ctx:
            acceptance.validate_base_url("http://example.com:8328", is_live=True)
        self.assertIn("not a loopback address", str(ctx.exception))

        # Userinfo
        with self.assertRaises(ValueError) as ctx:
            acceptance.validate_base_url("http://admin:pass@127.0.0.1:8328", is_live=True)
        self.assertIn("credentials/userinfo", str(ctx.exception))

        # Query parameter
        with self.assertRaises(ValueError) as ctx:
            acceptance.validate_base_url("http://127.0.0.1:8328?foo=bar", is_live=True)
        self.assertIn("query parameters", str(ctx.exception))

        # Fragment
        with self.assertRaises(ValueError) as ctx:
            acceptance.validate_base_url("http://127.0.0.1:8328#section", is_live=True)
        self.assertIn("fragments", str(ctx.exception))

        # Live constraint without monkeypatch: only 8328 is permitted
        with self.assertRaises(ValueError) as ctx:
            acceptance.validate_base_url("http://127.0.0.1:8327", is_live=True)
        self.assertIn("not in allowed live endpoints", str(ctx.exception))

        # Valid default live urls pass (CPA 8328 and Sub2API 18083)
        clean = acceptance.validate_base_url("http://127.0.0.1:8328", is_live=True)
        self.assertEqual(clean, "http://127.0.0.1:8328")
        clean_s2a = acceptance.validate_base_url("http://127.0.0.1:18083", is_live=True)
        self.assertEqual(clean_s2a, "http://127.0.0.1:18083")
        runner_s2a = acceptance.AcceptanceRunner(base_url=self.mock_base_url, api_key="test_key", antigravity_prefix=True)
        self.assertEqual(runner_s2a.test_messages_basic().status, "PASS")
        self.assertEqual(self.server.received_requests[-1]["path"], "/antigravity/v1/messages")
        self.assertEqual(runner_s2a.test_native_basic().status, "PASS")
        self.assertTrue(self.server.received_requests[-1]["path"].startswith("/antigravity/v1beta/models/"))
        self.assertEqual(self.server.received_requests[-1]["headers"].get("x-goog-api-key") or self.server.received_requests[-1]["headers"].get("X-Goog-Api-Key"), "test_key")

        # Monkeypatched allowed live endpoint works for unit testing
        with patch.object(acceptance, "ALLOWED_LIVE_BASE_URLS", {self.mock_base_url}):
            clean_mock = acceptance.validate_base_url(self.mock_base_url, is_live=True)
            self.assertEqual(clean_mock, self.mock_base_url)

    def test_key_file_permissions_check(self):
        """Key file must have strictly 0600 permissions."""
        # Valid 0600 key
        key = acceptance.load_key_from_file(self.valid_key_file)
        self.assertEqual(key, "test_secret_api_key_12345")

        # Insecure 0644 key file must raise PermissionError
        with self.assertRaises(PermissionError):
            acceptance.load_key_from_file(self.insecure_key_file)

    def test_cpa_config_loading(self):
        """CPA config loading extracts key without leakage."""
        key = acceptance.load_key_from_cpa_config(self.cpa_toml)
        self.assertEqual(key, "cpa_toml_api_key_abcde")

    def test_request_budget_strong_upper_limit(self):
        """Runner must stop once request budget (max_requests) is reached."""
        max_limit = 2
        runner = acceptance.AcceptanceRunner(
            base_url=self.mock_base_url,
            api_key="test_key",
            max_requests=max_limit,
        )
        plan = [
            ("test_1", runner.test_chat_basic),
            ("test_2", runner.test_chat_basic),
            ("test_3", runner.test_chat_basic),
        ]
        results = acceptance.run_tests(runner, plan, concurrency=1)
        self.assertEqual(len(results), 3)
        self.assertEqual(results[0].status, "PASS")
        self.assertEqual(results[1].status, "PASS")
        self.assertEqual(results[2].status, "STOPPED")
        self.assertIn("Request budget exhausted", results[2].error_category)
        self.assertEqual(self.server.request_count, max_limit)

    def test_immediate_stop_on_429(self):
        """429 status code halts runner immediately without retries."""
        self.server.force_429 = True
        runner = acceptance.AcceptanceRunner(
            base_url=self.mock_base_url,
            api_key="test_key",
            max_requests=10,
        )
        plan = [
            ("test_1", runner.test_chat_basic),
            ("test_2", runner.test_chat_basic),
            ("test_3", runner.test_chat_basic),
        ]
        results = acceptance.run_tests(runner, plan, concurrency=1)
        self.assertEqual(results[0].status, "FAIL")
        self.assertEqual(results[0].code, 429)
        self.assertEqual(results[0].error_category, "RateLimit429")
        # Subsequent tests must not execute network requests
        self.assertEqual(self.server.request_count, 1)
        self.assertTrue(runner.state.stopped)

    def test_immediate_stop_on_403(self):
        """403 status code halts runner immediately without retries."""
        self.server.force_403 = True
        runner = acceptance.AcceptanceRunner(
            base_url=self.mock_base_url,
            api_key="test_key",
            max_requests=10,
        )
        plan = [
            ("test_1", runner.test_chat_basic),
            ("test_2", runner.test_chat_basic),
        ]
        results = acceptance.run_tests(runner, plan, concurrency=1)
        self.assertEqual(results[0].status, "FAIL")
        self.assertEqual(results[0].code, 403)
        self.assertEqual(results[0].error_category, "Forbidden403")
        self.assertEqual(self.server.request_count, 1)
        self.assertTrue(runner.state.stopped)

    def test_http_200_empty_content_fails(self):
        """HTTP 200 with empty choices/content cannot pass."""
        self.server.force_empty_content = True
        runner = acceptance.AcceptanceRunner(
            base_url=self.mock_base_url,
            api_key="test_key",
        )
        result = runner.test_chat_basic()
        self.assertEqual(result.status, "FAIL")
        self.assertEqual(result.code, 200)
        self.assertEqual(result.error_category, "EmptyContentAndNoToolCalls")

    def test_sse_missing_end_marker_fails(self):
        """SSE stream missing [DONE] or finish marker cannot pass."""
        self.server.force_broken_stream = True
        runner = acceptance.AcceptanceRunner(
            base_url=self.mock_base_url,
            api_key="test_key",
        )
        result = runner.test_chat_sse()
        self.assertEqual(result.status, "FAIL")
        self.assertEqual(result.error_category, "MissingStreamEnd")

    def test_successful_sse_and_first_frame_recording(self):
        """Proper SSE stream passes, records first_frame_ms and latency."""
        runner = acceptance.AcceptanceRunner(
            base_url=self.mock_base_url,
            api_key="test_key",
        )
        result = runner.test_chat_sse()
        self.assertEqual(result.status, "PASS")
        self.assertEqual(result.code, 200)
        self.assertIsNotNone(result.first_frame_ms)
        self.assertGreater(result.first_frame_ms, 0)
        self.assertGreater(result.latency_ms, result.first_frame_ms)

    def test_tool_call_loop(self):
        """Offline tool call loop verifies tool schema preservation and 2-turn execution."""
        runner = acceptance.AcceptanceRunner(
            base_url=self.mock_base_url,
            api_key="test_key",
        )
        result = runner.test_chat_tool_call()
        self.assertEqual(result.status, "PASS")
        self.assertEqual(result.code, 200)
        # Should have made 2 requests to complete the tool call loop
        self.assertEqual(self.server.request_count, 2)

    def test_multi_turn_and_long95(self):
        """Verify multi-turn history and 95 messages payload scenarios."""
        runner = acceptance.AcceptanceRunner(
            base_url=self.mock_base_url,
            api_key="test_key",
        )
        res_mt = runner.test_chat_multi_turn()
        self.assertEqual(res_mt.status, "PASS")

        res_long = runner.test_chat_long95()
        self.assertEqual(res_long.status, "PASS")

        last_req = self.server.received_requests[-1]
        self.assertEqual(len(last_req["body"]["messages"]), 95)

    def test_cache_token_observation(self):
        """Cache hit records integer; unobserved records NOT_OBSERVED without fabrication."""
        runner = acceptance.AcceptanceRunner(
            base_url=self.mock_base_url,
            api_key="test_key",
        )
        # Case 1: unobserved
        res1 = runner.test_chat_prompt_cache()
        self.assertEqual(res1.status, "PASS")
        self.assertEqual(res1.usage["cached_tokens"], "NOT_OBSERVED")

        # Case 2: observed cached tokens
        self.server.simulate_cached_tokens = 128
        res2 = runner.test_chat_prompt_cache()
        self.assertEqual(res2.status, "PASS")
        self.assertEqual(res2.usage["cached_tokens"], 128)

    def test_report_desensitization(self):
        """Report JSON must only contain desensitized status/code/latency/usage integers."""
        report_file = self.tmp_path / "acceptance_report.json"
        with patch.object(acceptance, "ALLOWED_LIVE_BASE_URLS", {self.mock_base_url}):
            with patch("sys.argv", [
                "acceptance.py",
                "--live",
                "--base-url", self.mock_base_url,
                "--key-file", str(self.valid_key_file),
                "--protocols", "chat,messages,responses,native",
                "--report", str(report_file.resolve()),
            ]):
                exit_code = acceptance.main()
                self.assertEqual(exit_code, 0)

        self.assertTrue(report_file.is_file())
        raw_report = report_file.read_text(encoding="utf-8")
        report_json = json.loads(raw_report)

        # Invariants: No secrets or raw request/response body content
        self.assertNotIn("test_secret_api_key", raw_report)
        self.assertNotIn("developer_instructions", raw_report)
        self.assertNotIn("Round 1 user prompt", raw_report)
        self.assertNotIn("cpa_toml_api_key", raw_report)
        self.assertNotIn("Authorization", raw_report)
        self.assertNotIn("query", raw_report)

        # Summary checks
        summary = report_json["summary"]
        self.assertTrue(summary["all_passed"])
        self.assertGreater(summary["total_requests"], 0)
        self.assertLessEqual(summary["total_requests"], acceptance.DEFAULT_MAX_REQUESTS)

        # Tests list checks
        for t in report_json["tests"]:
            self.assertIn("name", t)
            self.assertIn("protocol", t)
            self.assertIn("status", t)
            self.assertIn("code", t)
            self.assertIn("latency_ms", t)
            self.assertIn("usage", t)
            self.assertIsInstance(t["usage"]["prompt_tokens"], int)
            self.assertIsInstance(t["usage"]["completion_tokens"], int)

    def test_concurrency_cap(self):
        """Concurrency cannot exceed 2."""
        with patch.object(acceptance, "ALLOWED_LIVE_BASE_URLS", {self.mock_base_url}):
            with patch("sys.argv", [
                "acceptance.py",
                "--live",
                "--base-url", self.mock_base_url,
                "--key-file", str(self.valid_key_file),
                "--concurrency", "5",
            ]):
                exit_code = acceptance.main()
                self.assertEqual(exit_code, 2)

    def test_cpa_yaml_api_keys_list_with_and_without_pyyaml(self):
        """CPA YAML api-keys list is extracted cleanly with PyYAML and in stdlib fallback."""
        yaml_cfg = self.tmp_path / "cpa_config.yaml"
        yaml_cfg.write_text(
            'host: "127.0.0.1"\n'
            'port: 8317\n'
            'video-result-auth-cache-ttl: 3600123456\n'
            '# api-key: "should-not-match-comment"\n'
            'api-keys:\n'
            '  - "sk-yaml-list-secret-01"\n'
            '  - "sk-yaml-list-secret-02"\n',
            encoding="utf-8",
        )
        os.chmod(yaml_cfg, 0o600)

        # 1. With PyYAML (if installed)
        self.assertEqual(acceptance.load_key_from_cpa_config(yaml_cfg), "sk-yaml-list-secret-01")

        # 2. Simulated no-PyYAML environment
        with patch.dict(sys.modules, {"yaml": None}):
            self.assertEqual(acceptance.load_key_from_cpa_config(yaml_cfg), "sk-yaml-list-secret-01")

            # Single-quoted item
            yaml_single = self.tmp_path / "cpa_single.yaml"
            yaml_single.write_text("api_keys:\n  - 'sk-single-quoted-999'\n", encoding="utf-8")
            os.chmod(yaml_single, 0o600)
            self.assertEqual(acceptance.load_key_from_cpa_config(yaml_single), "sk-single-quoted-999")

            # Bare token item with inline comment and unrelated ttl setting before it
            yaml_bare = self.tmp_path / "cpa_bare.yaml"
            yaml_bare.write_text(
                "video-result-auth-cache-ttl: 86400000\n"
                "auth:\n"
                "  api-keys:\n"
                "    - sk-bare-token-777  # primary key\n",
                encoding="utf-8",
            )
            os.chmod(yaml_bare, 0o600)
            self.assertEqual(acceptance.load_key_from_cpa_config(yaml_bare), "sk-bare-token-777")

            # Unrelated config must not falsely match video-result-auth-cache-ttl
            yaml_no_key = self.tmp_path / "cpa_nokey.yaml"
            yaml_no_key.write_text("video-result-auth-cache-ttl: 86400000\n", encoding="utf-8")
            os.chmod(yaml_no_key, 0o600)
            with self.assertRaises(ValueError):
                acceptance.load_key_from_cpa_config(yaml_no_key)

    def test_protocols_trigger_shim_wrapped_and_keep_user_ping_only(self):
        """chat_basic, chat_sse, messages_basic, responses_basic, native_basic must trigger wrapped adapt."""
        runner = acceptance.AcceptanceRunner(
            base_url=self.mock_base_url,
            api_key="test_key",
            model="gemini-3.8-flash-high",
            messages_model="claude-sonnet-4.6",
        )
        self.server.structured_responses_output = True

        self.assertEqual(runner.test_chat_basic().status, "PASS")
        self.assertEqual(runner.test_chat_sse().status, "PASS")
        self.assertEqual(runner.test_messages_basic().status, "PASS")
        self.assertEqual(runner.test_responses_basic().status, "PASS")
        self.assertEqual(runner.test_native_basic().status, "PASS")

        reqs = self.server.received_requests
        self.assertEqual(len(reqs), 5)

        # 0: chat_basic
        chat_body = reqs[0]["body"]
        self.assertEqual(chat_body["messages"][1], {"role": "user", "content": "ping"})
        self.assertIn(acceptance.NEUTRAL_MARKER, chat_body["messages"][0]["content"])
        _, st_chat = adapters.adapt(chat_body, "chat", "anchor", [])
        self.assertEqual(st_chat, "wrapped")

        # 1: chat_sse
        sse_body = reqs[1]["body"]
        self.assertEqual(sse_body["messages"][1], {"role": "user", "content": "ping"})
        self.assertIn(acceptance.NEUTRAL_MARKER, sse_body["messages"][0]["content"])
        _, st_sse = adapters.adapt(sse_body, "chat", "anchor", [])
        self.assertEqual(st_sse, "wrapped")

        # 2: messages_basic
        msg_body = reqs[2]["body"]
        self.assertEqual(msg_body["model"], "claude-sonnet-4.6")
        self.assertEqual(msg_body["messages"], [{"role": "user", "content": "ping"}])
        self.assertIsInstance(msg_body.get("system"), list)
        self.assertEqual(msg_body["system"][0].get("cache_control"), {"type": "ephemeral"})
        self.assertIn(acceptance.NEUTRAL_MARKER, msg_body["system"][0]["text"])
        _, st_msg = adapters.adapt(msg_body, "messages", "anchor", [])
        self.assertEqual(st_msg, "wrapped")

        # 3: responses_basic
        resp_body = reqs[3]["body"]
        self.assertEqual(resp_body["input"], "ping")
        self.assertIn(acceptance.NEUTRAL_MARKER, resp_body.get("instructions", ""))
        _, st_resp = adapters.adapt(resp_body, "responses", "anchor", [])
        self.assertEqual(st_resp, "wrapped")

        # 4: native_basic
        nat_body = reqs[4]["body"]
        self.assertEqual(nat_body["contents"], [{"role": "user", "parts": [{"text": "ping"}]}])
        self.assertIn(acceptance.NEUTRAL_MARKER, nat_body["systemInstruction"]["parts"][0]["text"])
        _, st_nat = adapters.adapt(nat_body, "gemini", "anchor", [])
        self.assertEqual(st_nat, "wrapped")

    def test_missing_marker_fails_anti_false_green(self):
        """Non-empty 200 responses without NEUTRAL_MARKER must fail with MarkerMissing."""
        self.server.force_missing_marker = True
        runner = acceptance.AcceptanceRunner(
            base_url=self.mock_base_url,
            api_key="test_key",
        )
        for fn in (
            runner.test_chat_basic,
            runner.test_chat_sse,
            runner.test_messages_basic,
            runner.test_responses_basic,
            runner.test_native_basic,
        ):
            res = fn()
            self.assertEqual(res.status, "FAIL", msg=f"{res.name} should fail without marker")
            self.assertEqual(res.error_category, "MarkerMissing", msg=f"{res.name} category mismatch")

    def test_fingerprint_fixture_zwsp_and_cli_flags(self):
        """--zwsp-fixture inserts ZWSP into English words, sets user=ping, and checks non-empty 200."""
        self.assertEqual(acceptance.DEFAULT_MAX_REQUESTS, 16)
        fixture_file = self.tmp_path / "fixture.txt"
        fixture_file.write_text("Alpha betagamma signature block", encoding="utf-8")

        report_file = self.tmp_path / "zwsp_report.json"
        with patch.object(acceptance, "ALLOWED_LIVE_BASE_URLS", {self.mock_base_url}):
            with patch("sys.argv", [
                "acceptance.py",
                "--live",
                "--base-url", self.mock_base_url,
                "--key-file", str(self.valid_key_file),
                "--protocols", "messages",
                "--messages-model", "claude-sonnet-4.6",
                "--fixtures-file", str(fixture_file),
                "--zwsp-fixture",
                "--report", str(report_file.resolve()),
            ]):
                exit_code = acceptance.main()
                self.assertEqual(exit_code, 0)

        self.assertEqual(len(self.server.received_requests), 2)
        msg_req = self.server.received_requests[0]
        self.assertEqual(msg_req["path"], "/v1/messages")
        self.assertEqual(msg_req["body"]["model"], "claude-sonnet-4.6")

        fix_req = self.server.received_requests[1]
        self.assertEqual(fix_req["path"], "/v1/chat/completions")
        sys_prompt = fix_req["body"]["messages"][0]["content"]
        self.assertEqual(fix_req["body"]["messages"][1], {"role": "user", "content": "ping"})
        self.assertIn("Al\u200bpha", sys_prompt)
        self.assertIn("be\u200btagamma", sys_prompt)
        self.assertIn("si\u200bgnature", sys_prompt)
        self.assertTrue(sys_prompt.endswith(f"When the user says ping, reply with {acceptance.NEUTRAL_MARKER}."))

        # Empty 200 on fixture must fail
        self.server.force_empty_content = True
        runner = acceptance.AcceptanceRunner(
            base_url=self.mock_base_url,
            api_key="test_key",
            fixtures_file=str(fixture_file),
            zwsp_fixture=True,
        )
        res_empty = runner.test_fingerprint_fixture()
        self.assertEqual(res_empty.status, "FAIL")
        self.assertEqual(res_empty.error_category, "EmptyContentAndNoToolCalls")

    def test_http_redirect_disabled_no_credential_leak(self):
        """302 redirects must fail directly without a second hop or leaking Authorization / x-api-key."""
        spy_requests: list[dict] = []

        class SpyRedirectTargetHandler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_GET(self):
                spy_requests.append({"method": "GET", "path": self.path, "headers": dict(self.headers)})
                self.send_response(200)
                self.end_headers()

            def do_POST(self):
                spy_requests.append({"method": "POST", "path": self.path, "headers": dict(self.headers)})
                self.send_response(200)
                self.end_headers()

        spy_server = ThreadingHTTPServer(("127.0.0.1", 0), SpyRedirectTargetHandler)
        spy_thread = threading.Thread(target=spy_server.serve_forever, daemon=True)
        spy_thread.start()
        try:
            self.server.force_redirect_url = f"http://127.0.0.1:{spy_server.server_port}/steal"
            runner = acceptance.AcceptanceRunner(
                base_url=self.mock_base_url,
                api_key="super_secret_credential_999",
            )
            res_chat = runner.test_chat_basic()
            self.assertEqual(res_chat.status, "FAIL")
            self.assertEqual(res_chat.code, 302)
            self.assertEqual(res_chat.error_category, "HTTP_302")

            res_msg = runner.test_messages_basic()
            self.assertEqual(res_msg.status, "FAIL")
            self.assertEqual(res_msg.code, 302)
            self.assertEqual(res_msg.error_category, "HTTP_302")

            # Also verify external domain Location fails immediately without attempting resolution/hop
            self.server.force_redirect_url = "https://external.example.invalid/exfiltrate"
            res_ext = runner.test_chat_basic()
            self.assertEqual(res_ext.status, "FAIL")
            self.assertEqual(res_ext.code, 302)
            self.assertEqual(res_ext.error_category, "HTTP_302")

            self.assertEqual(spy_requests, [])
            self.assertEqual(self.server.request_count, 3)
        finally:
            spy_server.shutdown()
            spy_server.server_close()
            spy_thread.join(timeout=2)

    def test_concurrent_budget_atomic_slot_max_1_concurrency_2(self):
        """With max_requests=1 and concurrency=2, interleaved threads must never send a 2nd request."""
        # Part A: direct barrier-synchronized two-thread race on RunnerState + _execute_http
        runner_race = acceptance.AcceptanceRunner(
            base_url=self.mock_base_url,
            api_key="test_key",
            max_requests=1,
        )
        barrier = threading.Barrier(2)
        race_results: list[acceptance.TestResult] = []
        race_lock = threading.Lock()

        def worker():
            barrier.wait(timeout=2)
            res = runner_race.test_chat_basic()
            with race_lock:
                race_results.append(res)

        t1 = threading.Thread(target=worker)
        t2 = threading.Thread(target=worker)
        t1.start()
        t2.start()
        t1.join(timeout=5)
        t2.join(timeout=5)

        self.assertEqual(len(race_results), 2)
        self.assertEqual(self.server.request_count, 1)
        self.assertEqual(runner_race.state.total_requests, 1)
        self.assertEqual(runner_race.state.request_count, 1)
        self.assertTrue(runner_race.state.stopped)
        statuses = sorted(r.status for r in race_results)
        self.assertEqual(statuses, ["PASS", "STOPPED"])

        # Part B: run_tests with concurrency=2, max_requests=1, and server delay to force overlap
        self.server.request_count = 0
        self.server.received_requests = []
        self.server.server_delay = 0.05
        runner_pool = acceptance.AcceptanceRunner(
            base_url=self.mock_base_url,
            api_key="test_key",
            max_requests=1,
        )
        plan = [
            ("test_1", runner_pool.test_chat_basic),
            ("test_2", runner_pool.test_chat_basic),
        ]
        pool_results = acceptance.run_tests(runner_pool, plan, concurrency=2)
        self.assertEqual(len(pool_results), 2)
        self.assertEqual(self.server.request_count, 1)
        self.assertEqual(runner_pool.state.total_requests, 1)
        self.assertTrue(runner_pool.state.stopped)
        self.assertEqual(sorted(r.status for r in pool_results), ["PASS", "STOPPED"])

    def test_tool_call_anti_false_green_cases(self):
        """Tool call test must fail if step 1 skips tool_calls or step 2 drops sentinel/marker."""
        runner = acceptance.AcceptanceRunner(
            base_url=self.mock_base_url,
            api_key="test_key",
        )
        # Step 1 returns plain text instead of tool_calls -> FAIL(ToolCallNotTriggered), no step 2
        self.server.force_unrelated_text = True
        res1 = runner.test_chat_tool_call()
        self.assertEqual(res1.status, "FAIL")
        self.assertEqual(res1.error_category, "ToolCallNotTriggered")
        self.assertEqual(self.server.request_count, 1)

        # Step 2 drops TOOL_RESULT_7391 -> FAIL(ToolResultMissing)
        self.server.force_unrelated_text = False
        self.server.force_missing_tool_sentinel = True
        res2 = runner.test_chat_tool_call()
        self.assertEqual(res2.status, "FAIL")
        self.assertEqual(res2.error_category, "ToolResultMissing")

        # Step 2 drops NEUTRAL_MARKER -> FAIL(MarkerMissing)
        self.server.force_missing_tool_sentinel = False
        self.server.force_missing_marker = True
        res3 = runner.test_chat_tool_call()
        self.assertEqual(res3.status, "FAIL")
        self.assertEqual(res3.error_category, "MarkerMissing")

    def test_unrelated_text_anti_false_green_all_scenarios(self):
        """Injecting UNRELATED_NO_TOOL_OR_MARKER must FAIL tool, multi_turn, long95, cache, extra_model, and fixture."""
        fixture_file = self.tmp_path / "unrelated_fixture.txt"
        fixture_file.write_text("Fingerprint probe sample text", encoding="utf-8")

        self.server.force_unrelated_text = True
        runner = acceptance.AcceptanceRunner(
            base_url=self.mock_base_url,
            api_key="test_key",
            extra_model="extra-test-model",
            fixtures_file=str(fixture_file),
            max_requests=32,
        )

        res_tool = runner.test_chat_tool_call()
        self.assertEqual(res_tool.status, "FAIL")
        self.assertEqual(res_tool.error_category, "ToolCallNotTriggered")

        res_mt = runner.test_chat_multi_turn()
        self.assertEqual(res_mt.status, "FAIL")
        self.assertEqual(res_mt.error_category, "HistoryCodeMissing")

        res_long = runner.test_chat_long95()
        self.assertEqual(res_long.status, "FAIL")
        self.assertEqual(res_long.error_category, "HistoryCodeMissing")

        res_cache = runner.test_chat_prompt_cache()
        self.assertEqual(res_cache.status, "FAIL")
        self.assertEqual(res_cache.error_category, "MarkerMissing")

        res_extra = runner.test_extra_model()
        self.assertEqual(res_extra.status, "FAIL")
        self.assertEqual(res_extra.error_category, "MarkerMissing")

        res_fix = runner.test_fingerprint_fixture()
        self.assertEqual(res_fix.status, "FAIL")
        self.assertEqual(res_fix.error_category, "MarkerMissing")

        # Also check multi_turn and long95 fail when marker is missing even if history code is present
        self.server.force_unrelated_text = False
        self.server.force_missing_marker = True
        self.assertEqual(runner.test_chat_multi_turn().error_category, "MarkerMissing")
        self.assertEqual(runner.test_chat_long95().error_category, "MarkerMissing")

        # Check multi_turn and long95 fail when history code is missing even if marker is present
        self.server.force_missing_marker = False
        self.server.force_missing_history_code = True
        self.assertEqual(runner.test_chat_multi_turn().error_category, "HistoryCodeMissing")
        self.assertEqual(runner.test_chat_long95().error_category, "HistoryCodeMissing")

        # Check cache fails if request 1 passes and request 2 returns UNRELATED_NO_TOOL_OR_MARKER
        self.server.force_missing_history_code = False
        self.server.request_count = 0
        self.server.force_unrelated_on_request_num = 2
        res_cache_req2 = runner.test_chat_prompt_cache()
        self.assertEqual(res_cache_req2.status, "FAIL")
        self.assertEqual(res_cache_req2.error_category, "MarkerMissing")

    def test_extra_model_cache_payload_contracts_and_timeout_cli(self):
        """Verify extra_model, cache payload contracts (~2600 tokens, ping, max_tokens=128) and --timeout default 90s."""
        self.assertEqual(acceptance.DEFAULT_TIMEOUT_SECONDS, 90.0)
        runner = acceptance.AcceptanceRunner(
            base_url=self.mock_base_url,
            api_key="test_key",
            extra_model="extra-model-check",
        )
        self.assertEqual(runner.timeout_seconds, 90.0)

        res_extra = runner.test_extra_model()
        self.assertEqual(res_extra.status, "PASS")
        extra_body = self.server.received_requests[-1]["body"]
        self.assertEqual(extra_body["model"], "extra-model-check")
        self.assertEqual(extra_body["max_tokens"], 128)
        self.assertEqual(extra_body["messages"][1], {"role": "user", "content": "ping"})
        self.assertIn(acceptance.NEUTRAL_MARKER, extra_body["messages"][0]["content"])
        _, st_extra = adapters.adapt(extra_body, "chat", "anchor", [])
        self.assertEqual(st_extra, "wrapped")

        res_cache = runner.test_chat_prompt_cache()
        self.assertEqual(res_cache.status, "PASS")
        cache_body = self.server.received_requests[-1]["body"]
        self.assertEqual(cache_body["max_tokens"], 128)
        self.assertEqual(cache_body["messages"][1], {"role": "user", "content": "ping"})
        sys_cache = cache_body["messages"][0]["content"]
        self.assertGreaterEqual(len(sys_cache), 9000)
        self.assertTrue(
            sys_cache.endswith(
                f"When the user says ping, reply with exact token {acceptance.NEUTRAL_MARKER} and nothing else."
            )
        )
        _, st_cache = adapters.adapt(cache_body, "chat", "anchor", [])
        self.assertEqual(st_cache, "wrapped")

        # Verify CLI --timeout overrides runner timeout_seconds and rejects non-positive values
        seen_timeouts: list[float] = []
        orig_init = acceptance.AcceptanceRunner.__init__

        def spy_init(self_runner, *args, **kwargs):
            seen_timeouts.append(kwargs.get("timeout_seconds"))
            return orig_init(self_runner, *args, **kwargs)

        with patch.object(acceptance, "ALLOWED_LIVE_BASE_URLS", {self.mock_base_url}):
            with patch.object(acceptance.AcceptanceRunner, "__init__", spy_init):
                with patch("sys.argv", [
                    "acceptance.py",
                    "--live",
                    "--base-url", self.mock_base_url,
                    "--key-file", str(self.valid_key_file),
                    "--protocols", "native",
                    "--timeout", "42.5",
                ]):
                    rc = acceptance.main()
                    self.assertEqual(rc, 0)
            self.assertEqual(seen_timeouts, [42.5])

            with patch("sys.argv", [
                "acceptance.py",
                "--live",
                "--base-url", self.mock_base_url,
                "--key-file", str(self.valid_key_file),
                "--timeout", "0",
            ]):
                rc_bad = acceptance.main()
                self.assertEqual(rc_bad, 2)


if __name__ == "__main__":
    unittest.main()
