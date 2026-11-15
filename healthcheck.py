#!/usr/bin/env python3
import argparse, json, sys, urllib.request, urllib.error
p=argparse.ArgumentParser(); p.add_argument('--url', default='http://127.0.0.1:8327'); a=p.parse_args()
try:
    r=urllib.request.urlopen(a.url.rstrip('/')+'/healthz', timeout=5)
    d=json.load(r); print(json.dumps(d, ensure_ascii=False)); sys.exit(0 if d.get('status')=='ok' else 1)
except Exception as e:
    print(json.dumps({'status':'error','error':type(e).__name__})); sys.exit(1)
