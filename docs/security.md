# Security Note

- Never include secrets in `MEMCTX`, logs, or trace facts.
- Keep sidecar bound to localhost.
- Treat plugin code as trusted code in gateway process.
- Mark sensitive config fields in `openclaw.plugin.json` `uiHints`.
