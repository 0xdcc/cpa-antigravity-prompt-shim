#!/usr/bin/env python3
"""Check relay configuration liveness (not CPA readiness)."""
import argparse
import json
import sys
import urllib.request
from urllib.parse import urlsplit

p = argparse.ArgumentParser()
p.add_argument('--url', default='http://127.0.0.1:8327')
a = p.parse_args()
u = urlsplit(a.url)
if u.scheme != 'http' or u.hostname != '127.0.0.1' or u.username or u.password or u.query or u.fragment or u.path not in ('', '/', '/healthz', '/readyz'):
    p.error('use a loopback base URL or health URL')
url = a.url.rstrip('/') if u.path in ('/healthz', '/readyz') else a.url.rstrip('/') + '/healthz'
class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise urllib.error.HTTPError(req.full_url, code, "redirects disabled", headers, fp)


try:
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), _NoRedirect())
    with opener.open(url, timeout=5) as r:
        d = json.load(r)
    print(json.dumps(d))
    sys.exit(0 if d.get('status') == 'ok' else 1)
except Exception as e:
    print(json.dumps({'status': 'error', 'error': type(e).__name__}))
    sys.exit(1)
