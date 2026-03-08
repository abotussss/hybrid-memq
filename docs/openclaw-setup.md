# OpenClaw Setup

## English

```bash
cd /path/to/hybrid-memq
scripts/memq-openclaw.sh setup
scripts/memq-openclaw.sh status
curl -sS http://127.0.0.1:7781/health
```

Expected:

- OpenClaw memory plugin = `openclaw-memory-memq`
- sidecar health = `ok=true`
- `qctxBackend = memory-lancedb-pro`
- `memory-lancedb-pro` integration is enabled automatically by the setup script

Upstream reference:

- [win4r/memory-lancedb-pro](https://github.com/win4r/memory-lancedb-pro)

This setup uses the bundled/adapted integration included in this repo.
You do not normally install the upstream repository separately for this OSS.

## 日本語

```bash
cd /path/to/hybrid-memq
scripts/memq-openclaw.sh setup
scripts/memq-openclaw.sh status
curl -sS http://127.0.0.1:7781/health
```

期待値:

- OpenClaw の memory plugin は `openclaw-memory-memq`
- sidecar health は `ok=true`
- `qctxBackend = memory-lancedb-pro`
- `memory-lancedb-pro` 連携は setup script により自動で有効化される

上流リポジトリ:

- [win4r/memory-lancedb-pro](https://github.com/win4r/memory-lancedb-pro)

この setup は、この repo に同梱されている bundled / adapted integration を有効化します。
この OSS の通常利用では、上流リポジトリを別途 install する必要はありません。
