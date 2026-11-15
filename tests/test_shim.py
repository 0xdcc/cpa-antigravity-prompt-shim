import contextlib
import http.client
import io
import json
import os
import pathlib
import re
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest.mock import patch

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import shim


class Upstream(BaseHTTPRequestHandler):
    protocol_version = 'HTTP/1.1'

    def log_message(self, *args):
        pass

    def do_GET(self):
        body = self.rfile.read(int(self.headers.get('Content-Length', '0')))
        self.server.seen.append((self.path, dict(self.headers), body))
        self.close_connection = True
        if self.path == '/timeout':
            self.server.release.wait(2)
            return
        if self.path in ('/stream', '/broken'):
            self.send_response(200)
            self.send_header('Content-Type', 'text/event-stream')
            if self.path == '/broken':
                self.send_header('Content-Length', '1000')
            self.end_headers()
            self.wfile.write(b'data: first\n\n')
            self.wfile.flush()
            self.server.first.set()
            self.server.release.wait(3)
            if self.path == '/stream':
                self.wfile.write(b'data: [DONE]\n\n')
            return
        if self.path == '/chunked':
            self.send_response(200)
            self.send_header('Transfer-Encoding', 'chunked')
            self.end_headers()
            self.wfile.write(b'5\r\nhello\r\n6\r\n world\r\n0\r\n\r\n')
            return
        status = int(self.path.split('/')[-1]) if self.path.startswith('/status/') else (429 if self.path == '/status' else 200)
        data = b'upstream body'
        self.send_response(status)
        self.send_header('Content-Length', str(len(data)))
        self.send_header('Retry-After', '7')
        self.send_header('Connection', 'close, X-Hop')
        self.send_header('X-Hop', 'must disappear')
        self.end_headers()
        if self.command != 'HEAD':
            self.wfile.write(data)

    do_POST = do_HEAD = do_GET


class Tests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory(dir=os.environ.get('TMPDIR'))
        cls.base = pathlib.Path(cls.tmp.name)
        cls.up = ThreadingHTTPServer(('127.0.0.1', 0), Upstream)
        cls.up.daemon_threads = True
        cls.up.seen, cls.up.first, cls.up.release = [], threading.Event(), threading.Event()
        cls.ut = threading.Thread(target=cls.up.serve_forever, daemon=True)
        cls.ut.start()
        cls.mode = cls.base / 'mode'
        cls.cfg = cls.base / 'config.toml'
        cls.mode.write_text('wrap')
        cls.config_text = ('[relay]\nmode_file=' + json.dumps(str(cls.mode)) + '\nlog_level="quiet"\nrequest_timeout_seconds=0.4\nmax_body_bytes=4096\n[upstream]\nurl="http://127.0.0.1:%d"\ntimeout_seconds=0.4\n' % cls.up.server_port)
        cls.cfg.write_text(cls.config_text)
        cls.rt = shim.Runtime(str(cls.cfg))
        cls.server = shim.Server(('127.0.0.1', 0), shim.Handler)
        cls.server.runtime = cls.rt
        cls.th = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.th.start()

    @classmethod
    def tearDownClass(cls):
        cls.up.release.set()
        cls.server.shutdown()
        cls.server.server_close()
        cls.up.shutdown()
        cls.up.server_close()
        cls.ut.join(2)
        cls.th.join(2)
        cls.tmp.cleanup()

    def setUp(self):
        self.mode.write_text('wrap')
        self.cfg.write_text(self.config_text)
        self.up.release.clear()
        self.up.first.clear()

    def connect(self):
        return http.client.HTTPConnection('127.0.0.1', self.server.server_address[1], timeout=2)

    def request(self, path, body=b'', headers=None, method='POST'):
        with contextlib.closing(self.connect()) as c:
            c.request(method, path, body, headers or {})
            r = c.getresponse()
            return r.status, dict(r.getheaders()), r.read()

    def payload(self):
        return {'model': 'gemini-x', 'messages': [{'role': 'system', 'content': 'RULE'}, {'role': 'developer', 'content': [{'type': 'text', 'text': 'MORE'}]}, {'role': 'user', 'content': [{'type': 'text', 'text': 'hi'}, {'type': 'image_url', 'image_url': {'url': 'data:image/png;base64,AA=='}}]}, {'role': 'assistant', 'tool_calls': [{'id': 'a', 'type': 'function', 'function': {'name': 'x', 'arguments': '{}'}}]}, {'role': 'tool', 'tool_call_id': 'a', 'content': 'done'}], 'tools': [{'type': 'function', 'function': {'name': 'x', 'parameters': {'type': 'object'}}}], 'tool_choice': 'auto', 'stream': True, 'temperature': 0.7}

    def test_wrap_and_preserve_full_tool_multimodal_payload(self):
        p = self.payload()
        self.assertEqual(self.request('/v1/chat/completions', json.dumps(p))[0], 200)
        got = json.loads(self.up.seen[-1][2])
        self.assertEqual(got['messages'][0], {'role': 'system', 'content': shim.DEFAULT_ANCHOR})
        self.assertEqual(got['messages'][1], {'role': 'user', 'content': '<developer_instructions>\nRULE\n\nMORE\n</developer_instructions>'})
        self.assertEqual(got['messages'][2:], p['messages'][2:])
        self.assertEqual({k: v for k, v in got.items() if k != 'messages'}, {k: v for k, v in p.items() if k != 'messages'})

    def test_mode_hot_switch_byte_exact(self):
        body = json.dumps(self.payload(), indent=2).encode()
        for mode in ('passthrough', 'wrap', 'passthrough'):
            self.mode.write_text(mode)
            self.assertEqual(self.request('/v1/chat/completions', body)[0], 200)
            got = self.up.seen[-1][2]
            self.assertEqual(got == body, mode == 'passthrough')

    def test_passthrough_models_paths_nonjson(self):
        body = json.dumps(self.payload(), indent=2).encode()
        for path, data in [('/unknown?x=1', body), ('/v1/messages', body), ('/v1/responses', body), ('/nested/v1/chat/completions', body), ('/v1/chat/completions', b'not json'), ('/v1/chat/completions', b'[]'), ('/v1/chat/completions', body.replace(b'gemini-x', b'gpt-oss'))]:
            with self.subTest(path=path, data=data[:20]):
                self.assertEqual(self.request(path, data)[0], 200)
                self.assertEqual(self.up.seen[-1][0], path)
                self.assertEqual(self.up.seen[-1][2], data)

    def test_complex_system_no_silent_loss(self):
        for content in ([{'type': 'text', 'text': 'x'}, {'type': 'image_url', 'image_url': {'url': 'x'}}], [{'type': 'text', 'text': 3}], [], None):
            p = self.payload()
            p['messages'][0]['content'] = content
            body = json.dumps(p).encode()
            self.request('/v1/chat/completions', body)
            self.assertEqual(self.up.seen[-1][2], body)
        p['messages'][0] = {'role': 'system', 'content': 'x', 'name': 'important'}
        body = json.dumps(p).encode()
        self.request('/v1/chat/completions', body)
        self.assertEqual(self.up.seen[-1][2], body)

    def test_late_system_exact(self):
        p = self.payload()
        p['messages'] = [p['messages'][2], p['messages'][0]]
        body = json.dumps(p).encode()
        self.request('/v1/chat/completions', body)
        self.assertEqual(self.up.seen[-1][2], body)

    def test_encoded_and_no_leading_passthrough(self):
        body = json.dumps(self.payload()).encode()
        self.request('/v1/chat/completions', body, {'Content-Encoding': 'gzip'})
        self.assertEqual(self.up.seen[-1][2], body)
        p = self.payload()
        p['messages'] = p['messages'][2:]
        body = json.dumps(p).encode()
        self.request('/v1/chat/completions', body)
        self.assertEqual(self.up.seen[-1][2], body)
        self.request('/v1/chat/completions', b'\xff\xfe')
        self.assertEqual(self.up.seen[-1][2], b'\xff\xfe')

    def test_expect_rejected_and_truncated_body_closed(self):
        out = self.raw(b'POST /unknown HTTP/1.1\r\nHost: localhost\r\nExpect: 100-continue\r\nContent-Length: 20\r\n\r\n')
        self.assertIn(b'HTTP/1.1 417', out)
        with socket.create_connection(self.server.server_address, timeout=2) as s:
            s.sendall(b'POST /unknown HTTP/1.1\r\nHost: localhost\r\nContent-Length: 20\r\n\r\na')
            s.shutdown(socket.SHUT_WR)
            out = b''
            while True:
                data = s.recv(4096)
                if not data:
                    break
                out += data
        self.assertIn(b'HTTP/1.1 400', out)

    def test_auth_host_and_hop_headers(self):
        self.request('/unknown', b'raw', {'Authorization': 'Bearer fake-test-only', 'X-Api-Key': 'fake-key', 'Host': 'attacker.invalid', 'Connection': 'keep-alive, X-Hop', 'X-Hop': 'remove'})
        headers = {k.lower(): v for k, v in self.up.seen[-1][1].items()}
        self.assertEqual(headers['authorization'], 'Bearer fake-test-only')
        self.assertEqual(headers['x-api-key'], 'fake-key')
        self.assertEqual(headers['host'], '127.0.0.1:%d' % self.up.server_port)
        self.assertNotIn('x-hop', headers)
        self.assertEqual(headers['connection'], 'close')

    def test_status_and_response_headers(self):
        for expected in (200, 400, 401, 403, 404, 429, 500, 502, 503):
            status, headers, body = self.request('/status/%d' % expected)
            self.assertEqual((status, body), (expected, b'upstream body'))
            self.assertEqual(headers['Retry-After'], '7')
            self.assertNotIn('X-Hop', headers)

    def test_sse_first_frame_before_upstream_finish_barrier(self):
        with contextlib.closing(self.connect()) as c:
            c.request('POST', '/stream', b'{}')
            r = c.getresponse()
            self.assertTrue(self.up.first.wait(1))
            try:
                self.assertEqual(r.fp.readline(), b'data: first\n')
                self.assertEqual(r.fp.readline(), b'\n')
                self.assertFalse(self.up.release.is_set())
            finally:
                self.up.release.set()
            self.assertEqual(r.read(), b'data: [DONE]\n\n')

    def raw(self, request):
        with socket.create_connection(self.server.server_address, timeout=2) as s:
            s.settimeout(2)
            s.sendall(request)
            chunks = []
            while True:
                data = s.recv(8192)
                if not data:
                    return b''.join(chunks)
                chunks.append(data)

    def test_keepalive_is_closed_no_second_request(self):
        before = len(self.up.seen)
        out = self.raw(b'GET /unknown HTTP/1.1\r\nHost: localhost\r\nConnection: keep-alive\r\n\r\nGET /second HTTP/1.1\r\nHost: localhost\r\n\r\n')
        self.assertEqual(out.count(b'HTTP/1.1'), 1)
        self.assertEqual(len(self.up.seen), before + 1)

    def test_after_headers_failure_never_appends_502(self):
        # Stream stalls after a first frame; timeout must close, not write JSON/502.
        out = self.raw(b'GET /broken HTTP/1.1\r\nHost: localhost\r\n\r\n')
        self.assertIn(b'data: first', out)
        self.assertEqual(out.count(b'HTTP/1.1'), 1)
        self.assertNotIn(b'relay_error', out)
        self.up.release.set()

    def test_upstream_timeout(self):
        self.assertEqual(self.request('/timeout')[0], 504)
        self.up.release.set()

    def test_body_limits_and_framing(self):
        for framing, expected in [(b'Content-Length: 4097', 413), (b'Content-Length: -1', 400), (b'Content-Length: 1\r\nContent-Length: 1', 400), (b'Transfer-Encoding: chunked', 501), (b'Transfer-Encoding: chunked\r\nContent-Length: 0', 400)]:
            out = self.raw(b'POST /unknown HTTP/1.1\r\nHost: localhost\r\n' + framing + b'\r\n\r\n')
            self.assertIn(('HTTP/1.1 %d' % expected).encode(), out)

    def test_request_body_timeout(self):
        out = self.raw(b'POST /unknown HTTP/1.1\r\nHost: localhost\r\nContent-Length: 20\r\n\r\na')
        self.assertIn(b'HTTP/1.1 504', out)

    def test_invalid_hot_config_mode_rules_rejected(self):
        self.mode.write_text('typo')
        self.assertEqual(self.request('/v1/chat/completions', b'{}')[0], 503)
        self.mode.write_text('wrap')
        self.cfg.write_text(self.config_text.replace('log_level="quiet"', 'log_level=123'))
        # Invalid hot configuration must return 503 without reaching upstream.
        self.assertRaises(ValueError, self.rt.config)
        self.assertEqual(self.request('/unknown')[0], 503)
        self.cfg.write_text(self.config_text)
        rules = self.base / 'rules.json'
        rules.write_text('[{"pattern": "["}]')
        self.cfg.write_text(self.config_text.replace('log_level="quiet"', 'log_level="quiet"\nrules_file=' + json.dumps(str(rules))))
        self.assertEqual(self.request('/v1/chat/completions', b'{}')[0], 503)

    def test_private_config_only(self):
        for kw in ({'listen_host': '0.0.0.0'}, {'upstream_url': 'http://example.org'}, {'upstream_url': 'http://fake:fake@127.0.0.1'}, {'upstream_url': 'http://127.0.0.1?secret=x'}, {'upstream_url': 'http://127.0.0.1:0'}, {'max_body_bytes': -1}, {'upstream_timeout_seconds': 0}, {'model_regex': '['}):
            with self.subTest(kw=kw), self.assertRaises((ValueError, re.error)):
                shim.Config(**kw)
        bad = self.base / 'bad.toml'
        bad.write_text('[security]\nallow_nonlocal_without_key=true')
        with self.assertRaises(ValueError):
            shim.Config.load(str(bad))

    def test_connection_finally_closed(self):
        class Broken:
            closed = False
            def __init__(self, *a, **kw):
                pass
            def putrequest(self, *a, **kw):
                raise OSError('fake')
            def close(self):
                Broken.closed = True
        with patch.object(shim.http.client, 'HTTPConnection', Broken):
            # Use raw sockets because shim and test share http.client module.
            out = self.raw(b'GET /unknown HTTP/1.1\r\nHost: localhost\r\n\r\n')
        self.assertIn(b'HTTP/1.1 502', out)
        self.assertTrue(Broken.closed)

    def test_logs_do_not_include_uri_model_body_or_credentials(self):
        self.cfg.write_text(self.config_text.replace('log_level="quiet"', 'log_level="info"'))
        p = self.payload()
        p['model'] = 'gemini-SECRET_MODEL\nforged'
        p['messages'][0]['content'] = 'SECRET_BODY'
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            self.request('/v1/chat/completions?SECRET_QUERY', json.dumps(p), {'Authorization': 'Bearer SECRET_AUTH'})
            # Read EOF only follows request handler finishing.
        text = output.getvalue()
        self.assertIn('mode=wrapped', text)
        self.assertNotIn('SECRET', text)
        self.assertNotIn('/v1/', text)

    def test_head_and_healthcheck_both_url_forms(self):
        self.assertEqual(self.request('/healthz', method='HEAD')[0], 200)
        for suffix in ('', '/healthz'):
            result = subprocess.run([sys.executable, str(ROOT / 'healthcheck.py'), '--url', 'http://127.0.0.1:%d%s' % (self.server.server_address[1], suffix)], capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_chunked_upstream_response_and_head(self):
        status, headers, body = self.request('/chunked')
        self.assertEqual((status, body), (200, b'hello world'))
        self.assertNotIn('Transfer-Encoding', headers)
        status, headers, body = self.request('/unknown', method='HEAD')
        self.assertEqual((status, body), (200, b''))
        self.assertEqual(headers['Content-Length'], str(len(b'upstream body')))

    def test_rules_hot_reload(self):
        rules = self.base / 'rules.json'
        self.cfg.write_text(self.config_text.replace('log_level="quiet"', 'log_level="quiet"\nrules_file=' + json.dumps(str(rules))))
        for replacement in ('ONE', 'TWO'):
            rules.write_text(json.dumps([{'pattern': 'RULE', 'replacement': replacement}]))
            self.request('/v1/chat/completions', json.dumps(self.payload()))
            self.assertIn(replacement, json.loads(self.up.seen[-1][2])['messages'][1]['content'])


class InstallTests(unittest.TestCase):
    def test_dry_run_staged_custom_prefix_and_no_systemctl(self):
        with tempfile.TemporaryDirectory(dir=os.environ.get('TMPDIR')) as tmp:
            base = pathlib.Path(tmp)
            stage = base / 'stage'
            tools = base / 'bin'
            tools.mkdir()
            marker = base / 'systemctl_called'
            fake = tools / 'systemctl'
            fake.write_text('#!/bin/sh\ntouch "' + str(marker) + '"\nexit 99\n')
            fake.chmod(0o755)
            env = dict(os.environ, PATH=str(tools) + ':' + os.environ['PATH'])
            command = [str(ROOT / 'install.sh'), '--yes', '--prefix', '/opt/custom-shim', '--destdir', str(stage)]
            dry = subprocess.run(command + ['--dry-run'], env=env, cwd='/', capture_output=True, text=True)
            self.assertEqual(dry.returncode, 0, dry.stderr)
            self.assertFalse(stage.exists())
            staged = subprocess.run(command, env=env, cwd='/', capture_output=True, text=True)
            self.assertEqual(staged.returncode, 0, staged.stderr)
            self.assertFalse(marker.exists())
            unit = (stage / 'etc/systemd/system/cpa-antigravity-prompt-shim.service').read_text()
            self.assertIn('/opt/custom-shim/shim.py', unit)
            self.assertIn('RuntimeDirectory=cpa-antigravity-prompt-shim', unit)
            self.assertTrue(os.access(stage / 'opt/custom-shim/healthcheck.py', os.X_OK))
            self.assertEqual((stage / 'etc/cpa-antigravity-prompt-shim/config.toml').stat().st_mode & 0o777, 0o600)
            for args in (['--prefix'], ['--unknown'], ['--prefix', '/bad path']):
                r = subprocess.run([str(ROOT / 'install.sh')] + args, capture_output=True)
                self.assertEqual(r.returncode, 2)


if __name__ == '__main__':
    unittest.main(verbosity=2)
