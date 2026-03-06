from __future__ import annotations
import json
import time
import urllib.request

BASE='http://127.0.0.1:7781'
SESSION='generic-recall-v3'

def post(path,payload):
    req=urllib.request.Request(BASE+path, data=json.dumps(payload).encode(), headers={'Content-Type':'application/json'})
    with urllib.request.urlopen(req, timeout=180) as r:
        return json.loads(r.read().decode())

if __name__ == '__main__':
    ts = int(time.time()) - 2
    turns = [
        ('僕の名前はヒロ。妻と犬がいる。', '了解。'),
        ('昨日はOpenClawの設定を直して、MEMQの設計を見直した。', '了解。'),
        ('今後は簡潔だけど丁寧に話して。', '了解。'),
    ]
    for user, assistant in turns:
        post('/memory/ingest_turn', {'sessionKey': SESSION, 'userText': user, 'assistantText': assistant, 'ts': ts})
        ts += 1
    cases = ['君は誰？', '家族構成は？', '昨日何した？', '最近の要点は？']
    results = {}
    for prompt in cases:
        res = post('/memctx/query', {
            'sessionKey': SESSION,
            'prompt': prompt,
            'recentMessages': [{'role': 'user', 'text': prompt}],
            'budgets': {'memctxTokens':120,'rulesTokens':80,'styleTokens':120},
            'topK': 5,
        })
        results[prompt] = {'memctx': res['memctx'], 'debug': res['meta']['debug']}

    assert 'p.snapshot=' in results['君は誰？']['memctx']
    assert 'p.snapshot=' in results['家族構成は？']['memctx']
    assert 't.range=' in results['昨日何した？']['memctx']
    assert 't.recent=' in results['最近の要点は？']['memctx']
    print(json.dumps(results, ensure_ascii=False, indent=2))
