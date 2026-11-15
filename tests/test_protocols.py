"""Local fake-upstream contract tests, not real provider acceptance."""
import contextlib
import copy
import http.client
import json
import os
import pathlib
import subprocess
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest.mock import patch

import shim
from adapters import route

ROOT = pathlib.Path(__file__).resolve().parents[1]


class Echo(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def do_POST(self):
        body = self.rfile.read(int(self.headers['Content-Length']))
        self.server.seen = (self.path, body, dict(self.headers))
        if self.headers.get('X-Test-Stream'):
            body = b'data: {"delta":"ok"}\n\ndata: [DONE]\n\n'
            self.send_response(200)
            self.send_header('Content-Type', 'text/event-stream')
            self.send_header('X-Upstream-Test', 'preserved')
            self.send_header('Content-Length', str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        self.send_response(200)
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)


class ProtocolTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory(dir=os.environ.get('TMPDIR'))
        cls.base = pathlib.Path(cls.tmp.name)
        cls.up = ThreadingHTTPServer(('127.0.0.1', 0), Echo)
        cls.ut = threading.Thread(target=cls.up.serve_forever, daemon=True)
        cls.ut.start()
        cls.cfg, cls.mode, cls.rules = [cls.base / x for x in ('config.toml', 'mode', 'rules.json')]
        cls.rt = shim.Runtime(str(cls.cfg))
        cls.server = shim.Server(('127.0.0.1', 0), shim.Handler)
        cls.server.runtime = cls.rt
        cls.st = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.st.start()

    @classmethod
    def tearDownClass(cls):
        for server in (cls.server, cls.up):
            server.shutdown()
            server.server_close()
        cls.st.join(2)
        cls.ut.join(2)
        cls.tmp.cleanup()

    def setUp(self):
        self.mode.write_text('wrap')
        self.rules.write_text('[]')
        self.config_text = ('[relay]\nmode_file=' + json.dumps(str(self.mode)) + '\nrules_file=' + json.dumps(str(self.rules)) + '\nlog_level="quiet"\n[upstream]\nurl="http://127.0.0.1:%d"\n' % self.up.server_port)
        self.cfg.write_text(self.config_text)

    def request(self, path, payload, headers=None):
        body = payload if isinstance(payload, bytes) else json.dumps(payload, indent=2).encode()
        with contextlib.closing(http.client.HTTPConnection(*self.server.server_address, timeout=3)) as conn:
            conn.request('POST', path, body, headers or {})
            response = conn.getresponse()
            return response.status, response.read(), body

    def cases(self):
        tools = [{'name': 'lookup', 'input_schema': {'type': 'object'}}]
        return [
            ('/v1/chat/completions', {'model': 'gemini-x', 'messages': [{'role': 'system', 'content': 'RULE'}, {'role': 'user', 'content': 'hello'}], 'tools': tools}, 'messages', 'content'),
            ('/v1/messages', {'model': 'gemini-x', 'system': [{'type': 'text', 'text': 'RULE'}], 'messages': [{'role': 'assistant', 'content': [{'type': 'thinking', 'thinking': 'x', 'signature': 'opaque'}]}, {'role': 'user', 'content': [{'type': 'tool_result', 'tool_use_id': 'id', 'content': 'ok'}]}], 'tools': tools, 'max_tokens': 100, 'thinking': {'type': 'enabled', 'budget_tokens': 30}}, 'messages', 'content'),
            ('/v1/responses', {'model': 'gemini-x', 'instructions': 'RULE', 'input': [{'type': 'function_call_output', 'call_id': 'id', 'output': 'ok'}, {'role': 'user', 'content': [{'type': 'input_image', 'image_url': 'data:image/png;base64,AA=='}]}], 'tools': tools, 'reasoning': {'effort': 'low'}}, 'input', 'content'),
            ('/v1beta/models/gemini-x:generateContent', {'systemInstruction': {'parts': [{'text': 'RULE'}]}, 'contents': [{'role': 'model', 'parts': [{'functionCall': {'name': 'lookup', 'args': {}}, 'thoughtSignature': 'opaque'}]}, {'role': 'user', 'parts': [{'functionResponse': {'name': 'lookup', 'response': {'ok': True}}}]}], 'tools': tools, 'generationConfig': {'thinkingConfig': {'includeThoughts': True}}}, 'contents', 'parts')]

    def test_protocols_preserve_tools_history_auth_and_query(self):
        for path, payload, field, block_field in self.cases():
            with self.subTest(path=path):
                status, body, _ = self.request(path + '?alt=sse', payload, {'X-Api-Key': 'local-test', 'Authorization': 'Bearer test'})
                self.assertEqual(status, 200)
                got = json.loads(body)
                consumed = 1 if path == '/v1/chat/completions' else 0
                offset = 2 if consumed else 1
                self.assertEqual(got[field][offset:], payload[field][consumed:])
                self.assertIn('RULE', str(got[field][offset - 1][block_field]))
                for key in set(payload) - {field, 'system', 'instructions', 'systemInstruction'}:
                    self.assertEqual(got[key], payload[key])
                self.assertEqual(self.up.seen[0], path + '?alt=sse')
                self.assertEqual(self.up.seen[2]['X-Api-Key'], 'local-test')
                self.assertEqual(self.up.seen[2]['Authorization'], 'Bearer test')

    def test_messages_selector_opt_in_not_provider_guessing(self):
        payload = self.cases()[1][1]
        for model in ('claude-sonnet', 'gpt-x', 'antigravity-claude-sonnet'):
            payload['model'] = model
            _, got, original = self.request('/v1/messages', payload)
            self.assertEqual(got, original)
        self.cfg.write_text(self.config_text.replace('log_level="quiet"', 'log_level="quiet"\nmodel_regex="^(gemini-|antigravity-)"'))
        _, got, original = self.request('/v1/messages', payload)
        self.assertNotEqual(got, original)
        self.assertEqual(json.loads(got)['system'], shim.DEFAULT_ANCHOR)

    def test_responses_leading_roles_and_string_input(self):
        p = self.cases()[2][1]
        history = p['input']
        p['input'] = [{'role': 'system', 'content': 'SYS'}, {'type': 'message', 'role': 'developer', 'content': [{'type': 'input_text', 'text': 'DEV'}]}] + history
        _, got, _ = self.request('/v1/responses', p)
        d = json.loads(got)
        self.assertEqual(d['input'][1:], history)
        self.assertEqual(d['input'][0]['content'][0]['text'], '<developer_instructions>\nRULE\n\nSYS\n\nDEV\n</developer_instructions>')
        self.assertEqual(d['instructions'], shim.DEFAULT_ANCHOR)
        p['input'] = 'hello'
        _, got, _ = self.request('/v1/responses', p)
        self.assertEqual(json.loads(got)['input'][1], {'role': 'user', 'content': [{'type': 'input_text', 'text': 'hello'}]})

    def test_native_stream_snake_alias_and_safe_decoding(self):
        p = self.cases()[3][1]
        p['system_instruction'] = p.pop('systemInstruction')
        path = '/v1beta/models/gemini%2Dx:streamGenerateContent?alt=sse'
        _, got, _ = self.request(path, p)
        self.assertEqual(json.loads(got)['system_instruction'], {'parts': [{'text': shim.DEFAULT_ANCHOR}]})
        self.assertEqual(self.up.seen[0], path)
        for token in ('gemini%2Fx', 'gemini%252Dx', 'gemini%3Ax', 'gemini%00x', 'gemini%x', 'gemini%FFx'):
            self.assertEqual(route('/v1beta/models/' + token + ':generateContent')[0], 'unsupported')

    def test_complex_moved_fields_byte_exact(self):
        mutations = [
            (0, lambda p: p['messages'][0].update(name='metadata')),
            (0, lambda p: p['messages'][0].update(content=[{'type': 'text', 'text': 'x', 'cache_control': {'type': 'custom'}}])),
            (1, lambda p: p.update(system=[{'type': 'text', 'text': 'x', 'cache_control': {'type': 'ephemeral', 'extra': 1}}])),
            (1, lambda p: p.update(system=[{'type': 'text', 'text': 'x', 'citations': []}])),
            (1, lambda p: p.update(system=[{'type': 'image', 'source': {'type': 'base64', 'data': 'x'}}])),
            (2, lambda p: p.update(instructions={'text': 'x', 'unknown': 1})),
            (2, lambda p: p.update(input=[{'role': 'system', 'content': 'x', 'id': 'msg-id'}] + p['input'])),
            (3, lambda p: p['systemInstruction'].update(unknown=True)),
            (3, lambda p: p['systemInstruction']['parts'][0].update(thoughtSignature='opaque')),
            (3, lambda p: p['systemInstruction']['parts'].append({'inlineData': {'data': 'x'}}))]
        for index, mutate in mutations:
            path, original, _, _ = self.cases()[index]
            payload = copy.deepcopy(original)
            mutate(payload)
            _, got, raw = self.request(path, payload)
            self.assertEqual(got, raw)
            self.assertEqual(self.rt.transform(raw, path=path), (None, 'complex-system'))
        # Valid Anthropic/OpenRouter ephemeral cache_control preserves EVERY block boundary & TTL.
        msg_payload = copy.deepcopy(self.cases()[1][1])
        msg_payload['system'] = [
            {'type': 'text', 'text': 'A', 'cache_control': {'type': 'ephemeral', 'ttl': '5m'}},
            {'type': 'text', 'text': 'B', 'cache_control': {'type': 'ephemeral', 'ttl': '1h'}},
        ]
        _, got, _ = self.request('/v1/messages', msg_payload)
        parsed = json.loads(got)
        self.assertEqual(parsed['messages'][0]['content'], [
            {'type': 'text', 'text': '<developer_instructions>\nA', 'cache_control': {'type': 'ephemeral', 'ttl': '5m'}},
            {'type': 'text', 'text': 'B\n</developer_instructions>', 'cache_control': {'type': 'ephemeral', 'ttl': '1h'}},
        ])
        chat_payload = copy.deepcopy(self.cases()[0][1])
        chat_payload['messages'][0]['content'] = [{'type': 'text', 'text': 'RULE', 'cache_control': {'type': 'ephemeral'}}]
        _, got, _ = self.request('/v1/chat/completions', chat_payload)
        parsed_chat = json.loads(got)
        self.assertEqual(parsed_chat['messages'][1]['content'][0]['cache_control'], {'type': 'ephemeral'})
        # Startup invalid config must not leak sentinel value in stderr.
        bad_cfg = self.base / 'bad_startup.toml'
        bad_cfg.write_text('[upstream]\nurl = "http://127.0.0.1:PRIVATE_KEY_SENTINEL"\n')
        proc = subprocess.run(['/usr/bin/python3', str(ROOT / 'shim.py'), '--config', str(bad_cfg)], capture_output=True, text=True)
        self.assertEqual(proc.returncode, 1)
        self.assertNotIn('PRIVATE_KEY_SENTINEL', proc.stderr + proc.stdout)

    def test_server_continuation_byte_exact(self):
        for index, keys in ((2, ('previous_response_id', 'conversation')), (3, ('cachedContent', 'cached_content'))):
            for key in keys:
                path, payload, _, _ = self.cases()[index]
                payload[key] = 'opaque-id'
                _, got, raw = self.request(path, payload)
                self.assertEqual(got, raw)
                self.assertEqual(self.rt.transform(raw, path=path), (None, 'server-continuation'))

    def test_token_count_and_nonexact_paths_byte_exact(self):
        paths = ['/v1/messages/count_tokens', '/v1/responses/input_tokens', '/v1beta/models/gemini-x:countTokens', '/v1/messages/', '/prefix/v1/responses', '/v1beta/models/gemini-x:generateContent/extra']
        for path in paths:
            _, got, raw = self.request(path, self.cases()[1][1])
            self.assertEqual(got, raw)
        for path, payload, _, _ in self.cases():
            _, got, raw = self.request(path, payload, {'Content-Encoding': 'gzip'})
            self.assertEqual(got, raw)

    def test_cached_parsing_and_invalid_hot_edits_fail_closed(self):
        rt = shim.Runtime(str(self.cfg))
        with patch.object(shim.Config, 'load', wraps=shim.Config.load) as loader:
            first = rt.config()
            self.assertIs(first, rt.config())
            self.assertEqual(loader.call_count, 1)
        with patch.object(shim.json, 'loads', wraps=json.loads) as loads:
            self.assertIs(rt.rules(first), rt.rules(first))
            self.assertEqual(loads.call_count, 1)
        self.assertEqual(rt.mode(first), 'wrap')
        for file, invalid, valid in ((self.cfg, 'invalid config', self.config_text), (self.mode, 'bad', 'wrap'), (self.rules, '[{"pattern":"["}]', '[]'), (self.rules, '[{"pattern":"x","replacement":"\\\\9"}]', '[]')):
            file.write_text(invalid)
            for _ in range(2):
                status, _, _ = self.request('/v1/messages', self.cases()[1][1])
                self.assertEqual(status, 503)
            file.write_text(valid)
            self.assertEqual(self.request('/v1/messages', self.cases()[1][1])[0], 200)
        # Atomic same-size/same-mtime replacement must invalidate by inode.
        rt.mode(first)
        stat = self.mode.stat()
        replacement = self.base / 'new-mode'
        replacement.write_text('xxxx')
        os.utime(replacement, ns=(stat.st_atime_ns, stat.st_mtime_ns))
        replacement.replace(self.mode)
        with self.assertRaises(ValueError):
            rt.mode(first)

    def test_all_protocols_sse_response_bytes_and_headers(self):
        for path, payload, _, _ in self.cases():
            with contextlib.closing(http.client.HTTPConnection(*self.server.server_address, timeout=3)) as conn:
                conn.request('POST', path, json.dumps(payload), {'X-Test-Stream': '1'})
                response = conn.getresponse()
                self.assertEqual(response.status, 200)
                self.assertEqual(response.getheader('Content-Type'), 'text/event-stream')
                self.assertEqual(response.getheader('X-Upstream-Test'), 'preserved')
                self.assertEqual(response.read(), b'data: {"delta":"ok"}\n\ndata: [DONE]\n\n')
                self.assertNotEqual(json.loads(self.up.seen[1]), payload)

    def test_installer_healthcheck_uses_existing_custom_port(self):
        self.cfg.write_text(self.config_text.replace('[relay]', '[relay]\nlisten_port=%d' % self.server.server_address[1]))
        source = (ROOT / 'install.sh').read_text()
        block = source[source.index('HEALTH_URL=$('):source.index("printf '%s\\n' 'Installed.")]
        env = dict(os.environ, ETC=str(self.base), PREFIX=str(ROOT))
        result = subprocess.run(['bash', '-ec', block], env=env, capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('ok', result.stdout.lower())

    def test_example_fallback_rules_and_staged_adapter(self):
        rules = json.loads((ROOT / 'rules.example.json').read_text())
        text = '## Mid-turn user steering\nMid-turn, the user can send updates'
        import re
        for rule in rules:
            text = re.sub(rule['pattern'], rule['replacement'], text)
        self.assertNotIn('Mid-turn user steering', text)
        self.assertNotIn('Mid-turn, the user can', text)
        inline = 'prefix ## Google model operational directives and ## Hermes runtime environment'
        for rule in rules:
            inline = re.sub(rule['pattern'], rule['replacement'], inline)
        self.assertNotIn('Google model operational directives', inline)
        self.assertNotIn('Hermes runtime environment', inline)
        stage = self.base / 'stage'
        result = subprocess.run(['bash', str(ROOT / 'install.sh'), '--yes', '--destdir', str(stage)], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        installed = stage / 'opt/cpa-antigravity-prompt-shim'
        self.assertEqual((installed / 'adapters.py').read_bytes(), (ROOT / 'adapters.py').read_bytes())
        result = subprocess.run(['/usr/bin/python3', '-c', 'import shim; print(shim.Config().listen_host)'], cwd=installed, capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), '127.0.0.1')


if __name__ == '__main__':
    unittest.main()
