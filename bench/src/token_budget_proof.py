from __future__ import annotations
import json
import urllib.request

BASE='http://127.0.0.1:7781'

def est_tokens(text: str) -> int:
    clean = " ".join(str(text or "").split())
    if not clean:
        return 0
    return max(1, (len(clean) + 3) // 4)

def post(path,payload):
    req=urllib.request.Request(BASE+path, data=json.dumps(payload).encode(), headers={'Content-Type':'application/json'})
    with urllib.request.urlopen(req, timeout=180) as r:
        return json.loads(r.read().decode())

if __name__ == '__main__':
    prompt = '長文の最近の作業履歴を踏まえて要点だけ教えて'
    recent = [{'role':'user','text':'a'*2000},{'role':'assistant','text':'b'*2000},{'role':'user','text':'c'*2000}]
    res = post('/memctx/query', {'sessionKey':'budget-proof-v3','prompt':prompt,'recentMessages':recent,'budgets':{'memctxTokens':120,'rulesTokens':80,'styleTokens':120},'topK':5})
    out = {'memrules': est_tokens(res['memrules']), 'memstyle': est_tokens(res['memstyle']), 'memctx': est_tokens(res['memctx'])}
    assert 0 < out['memrules'] <= 80
    assert 0 < out['memstyle'] <= 120
    assert 0 <= out['memctx'] <= 120
    print(json.dumps(out, ensure_ascii=False, indent=2))
