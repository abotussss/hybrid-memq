from __future__ import annotations
import json
import urllib.request
import time

BASE = 'http://127.0.0.1:7781'
MODEL = 'gpt-oss:20b'
SESSION = 'proof-required-v3'


def get(path: str):
    url = path if path.startswith('http://') or path.startswith('https://') else BASE + path
    with urllib.request.urlopen(url, timeout=180) as r:
        return json.loads(r.read().decode())


def post(path: str, payload: dict):
    req = urllib.request.Request(BASE + path, data=json.dumps(payload).encode(), headers={'Content-Type': 'application/json'})
    with urllib.request.urlopen(req, timeout=180) as r:
        return json.loads(r.read().decode())


if __name__ == '__main__':
    ts = int(time.time())
    health = get('/health')
    assert health['ok']
    assert health['config']['brainMode'] == 'brain-required'
    assert health['config']['brainModel'] == MODEL

    ingest = post('/memory/ingest_turn', {
        'sessionKey': SESSION,
        'userText': '今後はプロジェクトナビゲータとして話して。私のことはオペレーターと呼んで。一人称は私で、丁寧だけど協力的なトーンにして。昨日は障害ログを確認して、その後メモリ仕様を見直した。',
        'assistantText': '了解。',
        'ts': ts,
    })
    profile = get(f'/profile?session_key={SESSION}')
    query = post('/memctx/query', {
        'sessionKey': SESSION,
        'prompt': 'あなたは誰？ 昨日何した？',
        'recentMessages': [{'role': 'user', 'text': 'あなたは誰？ 昨日何した？'}],
        'budgets': {'memctxTokens': 120, 'rulesTokens': 80, 'styleTokens': 120},
        'topK': 5,
    })
    idle = post('/idle/run_once', {'nowTs': ts + 1, 'maxWorkMs': 1200})
    stats = get('/brain/stats')
    traces = get('/brain/trace/recent?n=10')
    ps = get('http://127.0.0.1:11434/api/ps')

    style_profile = profile['style_profile']
    memstyle = query['memstyle']
    debug = query['meta']['debug']
    trace_ops = [item['op'] for item in traces.get('items', [])]
    ps_models = [item.get('model') or item.get('name') for item in ps.get('models', [])]

    result = {
        'health_ok': health['ok'],
        'brain_mode': health['config']['brainMode'],
        'brain_model': health['config']['brainModel'],
        'ingest_ok': ingest['ok'],
        'ingest_wrote': ingest['wrote'],
        'style_updated': bool(style_profile),
        'style_profile': style_profile,
        'memstyle': memstyle,
        'memctx': query['memctx'],
        'recall_ps_seen': debug['ps_seen'],
        'trace_ops': trace_ops,
        'brain_stats': stats,
        'ps_models': ps_models,
        'idle': idle,
    }

    assert ingest['ok']
    assert ingest['wrote']['style'] >= 1
    assert style_profile.get('callUser') == 'オペレーター'
    assert style_profile.get('firstPerson') == '私'
    assert 'callUser=オペレーター' in memstyle
    assert 'firstPerson=私' in memstyle
    assert debug['ps_seen'] == 1
    assert 'ingest_plan' in trace_ops
    assert 'recall_plan' in trace_ops
    assert stats['last_ps_seen_model'] == MODEL
    assert MODEL in ps_models

    print(json.dumps(result, ensure_ascii=False, indent=2))
