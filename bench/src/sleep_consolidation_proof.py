from __future__ import annotations
import json
import sys
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from sidecar.memq.config import load_config
from sidecar.memq.db import MemqDB

BASE='http://127.0.0.1:7781'
SESSION='sleep-proof-v3'

def post(path,payload):
    req=urllib.request.Request(BASE+path, data=json.dumps(payload).encode(), headers={'Content-Type':'application/json'})
    with urllib.request.urlopen(req, timeout=180) as r:
        return json.loads(r.read().decode())

if __name__ == '__main__':
    cfg = load_config()
    db = MemqDB(Path(cfg.db_path), timezone_name=cfg.timezone)
    try:
        db.insert_memory(session_key='global', layer='deep', kind='fact', fact_key='profile.identity.card', value='A', text='A', summary='dup A', confidence=0.8, importance=0.8, strength=0.8)
        db.insert_memory(session_key='global', layer='deep', kind='fact', fact_key='profile.identity.card', value='A', text='A', summary='dup A second', confidence=0.8, importance=0.8, strength=0.8)
        db.insert_event(session_key='global', ts=int(time.time()), actor='user', kind='progress', summary='MEMQ v3 の sleep consolidation を検証した', salience=0.9)
    finally:
        db.close()
    res = post('/idle/run_once', {'nowTs': int(time.time()), 'maxWorkMs': 1200})
    assert res['ok']
    assert 'refresh_digests' in res['did']
    assert 'brain_merge_plan' in res['did']
    print(json.dumps(res, ensure_ascii=False, indent=2))
