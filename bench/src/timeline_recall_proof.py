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
        'userText': '昨日はBirdでログを確認し、その後MEMQの仕様を修正した。',
        'assistantText': '了解。',
        'ts': int(time.time()) - 86400,
    })
    res = post('/memctx/query', {
        'sessionKey': SESSION,
        'prompt': '昨日何した？',
        'recentMessages': [{'role':'user','text':'昨日何した？'}],
        'budgets': {'memctxTokens':120,'rulesTokens':80,'styleTokens':120},
        'topK': 5,
    })
    memctx = res['memctx']
    debug = res['meta']['debug']
    assert 't.range=' in memctx
    assert 't.label=' in memctx
    assert ('t.digest=' in memctx) or ('t.ev1=' in memctx)
    assert debug['ps_seen'] == 1
    print(json.dumps({'memctx': memctx, 'debug': debug}, ensure_ascii=False, indent=2))
