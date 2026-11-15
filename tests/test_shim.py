import http.client, json, os, pathlib, socket, subprocess, sys, threading, time, unittest
from http.server import BaseHTTPRequestHandler, HTTPServer
ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT)); import shim

class Upstream(BaseHTTPRequestHandler):
    seen=[]
    def do_GET(self):
        self.send_response(200); self.send_header('Content-Type','application/json'); self.end_headers(); self.wfile.write(b'{"ok":true}')
    def do_POST(self):
        n=int(self.headers.get('Content-Length','0')); body=self.rfile.read(n); Upstream.seen.append((self.path,json.loads(body)))
        if self.path.endswith('/stream'):
            self.send_response(200); self.send_header('Content-Type','text/event-stream'); self.end_headers()
            for x in (b'data: {"ok":1}\n\n', b'data: [DONE]\n\n'): self.wfile.write(x); self.wfile.flush()
        else:
            self.send_response(200); self.send_header('Content-Type','application/json'); self.end_headers(); self.wfile.write(b'{"ok":true}')
    def log_message(self,*a): pass

class Tests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.up=HTTPServer(('127.0.0.1',0),Upstream); cls.ut=threading.Thread(target=cls.up.serve_forever,daemon=True); cls.ut.start()
        cls.mode_path='/tmp/cpa-shim-test-mode'; pathlib.Path(cls.mode_path).write_text('wrap\n')
        cfg=shim.Config(listen_host='127.0.0.1',listen_port=0,upstream_url='http://127.0.0.1:%d'%cls.up.server_port,mode_file=cls.mode_path,rules_file='')
        cls.rt=shim.Runtime('/tmp/no-such-cpa-shim-config'); cls.rt._config=cfg; cls.rt._config_mtime=None
        cls.server=shim.Server(('127.0.0.1',0),shim.Handler); cls.server.runtime=cls.rt; cls.th=threading.Thread(target=cls.server.serve_forever,daemon=True); cls.th.start()
    @classmethod
    def tearDownClass(cls): cls.server.shutdown(); cls.up.shutdown()
    def post(self,path,body):
        c=http.client.HTTPConnection('127.0.0.1',self.server.server_address[1],timeout=5); c.request('POST',path,json.dumps(body),{'Content-Type':'application/json'}); r=c.getresponse(); return r.status,r.read()
    def test_wrap_leading_system_and_preserve_tools(self):
        status,_=self.post('/v1/chat/completions',{'model':'gemini-4-carbon','messages':[{'role':'system','content':'RULE'}, {'role':'user','content':'hi'}], 'tools':[{'type':'function','function':{'name':'x'}}]})
        self.assertEqual(status,200); body=Upstream.seen[-1][1]; self.assertEqual(body['messages'][0]['role'],'system'); self.assertIn('developer_instructions',body['messages'][1]['content']); self.assertEqual(body['tools'][0]['function']['name'],'x')
    def test_non_gemini_passthrough(self):
        self.post('/v1/chat/completions',{'model':'gpt-x','messages':[{'role':'system','content':'RULE'},{'role':'user','content':'hi'}]}); self.assertEqual(Upstream.seen[-1][1]['messages'][0]['role'],'system')
    def test_passthrough_mode(self):
        self.rt._mode='passthrough'; pathlib.Path(self.mode_path).write_text('passthrough\n')
        self.post('/v1/chat/completions',{'model':'gemini-x','messages':[{'role':'system','content':'RULE'},{'role':'user','content':'hi'}]})
        self.assertEqual(Upstream.seen[-1][1]['messages'][0]['role'],'system')
        self.rt._mode='wrap'; pathlib.Path(self.mode_path).write_text('wrap\n')
    def test_sse_is_unchanged(self):
        c=http.client.HTTPConnection('127.0.0.1',self.server.server_address[1],timeout=5); c.request('POST','/v1/stream',b'{}'); r=c.getresponse(); self.assertEqual(r.status,200); self.assertIn(b'[DONE]',r.read())
    def test_system_only_is_not_moved_if_not_leading_text(self):
        self.post('/v1/chat/completions',{'model':'gemini-x','messages':[{'role':'user','content':'first'},{'role':'system','content':'late'}]}); self.assertEqual(Upstream.seen[-1][1]['messages'][0]['role'],'user')

if __name__=='__main__': unittest.main(verbosity=2)
