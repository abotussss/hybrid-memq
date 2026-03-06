from __future__ import annotations
import json
import urllib.request

BASE='http://127.0.0.1:7781'

def post(path,payload):
    req=urllib.request.Request(BASE+path, data=json.dumps(payload).encode(), headers={'Content-Type':'application/json'})
    with urllib.request.urlopen(req, timeout=180) as r:
        return json.loads(r.read().decode())

if __name__ == '__main__':
    res = post('/audit/output', {'sessionKey':'audit-proof-v3','text':'secret is sk-1234567890abcdefghijklmnop','mode':'primary'})
    assert '[REDACTED_SECRET]' in res['redactedText']
    assert res['block'] is True
    print(json.dumps(res, ensure_ascii=False, indent=2))
