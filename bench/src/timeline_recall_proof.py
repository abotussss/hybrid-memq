from __future__ import annotations
import json
import time
import urllib.request

BASE='http://127.0.0.1:7781'
SESSION='timeline-proof-v3'

def post(path,payload):
    req=urllib.request.Request(BASE+path, data=json.dumps(payload).encode(), headers={'Content-Type':'application/json'})
    with urllib.request.urlopen(req, timeout=180) as r:
        return json.loads(r.read().decode())

if __name__ == '__main__':
    post('/memory/ingest_turn', {
        'sessionKey': SESSION,
        'userText': '昨日は障害ログを確認し、その後メモリ仕様を更新した。',
        'assistantText': '了解。',
        'ts': int(time.time()) - 86400,
    })
    res = post('/qctx/query', {
        'sessionKey': SESSION,
        'prompt': '昨日何した？',
        'recentMessages': [{'role':'user','text':'昨日何した？'}],
        'budgets': {'qctxTokens':500,'qruleTokens':500,'qstyleTokens':500},
        'topK': 5,
    })
    memctx = res['qctx']
    debug = res['meta']['debug']
    assert 't.range=' in memctx
    assert 't.label=' in memctx
    assert ('t.digest=' in memctx) or ('t.ev1=' in memctx)
    assert debug['ps_seen'] == 1
    print(json.dumps({'memctx': memctx, 'debug': debug}, ensure_ascii=False, indent=2))
