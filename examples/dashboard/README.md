# Reference dashboard

A working example of the integration contract in
[../../DASHBOARD.md](../../DASHBOARD.md). One file, ~250 lines of FastAPI, no
database of its own.

```bash
pip install 'hermes-aux-autoheal[dashboard]'      # or: pip install fastapi uvicorn
python app.py --cache compression=~/.hermes/.aux_autoheal_health.json \
              --cache vision=~/.hermes/.aux_vision_health.json \
              --exclude-file ~/.hermes/.aux_probe_blocklist.json \
              --sqlite-db ~/.hermes/dashboard/data.db
```

The startup banner prints a login token. Bound to `127.0.0.1` unless you pass
`--host`.

## What it demonstrates

- **Read path** — health cache → problem rows via `report.summarize()`, worst
  first, each with an actionable category. Probes nothing, so refreshing the page
  costs no API calls.
- **Write path** — "stop probing" does all three writes that are actually
  required: exclude-list entry (stops the probe), untick in the provider DB
  (stops rediscovery), purge the cache row (clears the row now instead of at TTL).
- **Server-side category check** — a `rate_limit` row is refused even if the
  client asks nicely. Excluding a model that recovers on its own permanently
  removes a healthy model from the pool.
- **Auth boundary** — every data and write endpoint requires a session cookie.

## What it is not

Not a product. No user management, no CSRF tokens, no login rate limiting, no
TLS, sessions in memory. Read it, take the parts you need, and put real auth in
front of anything that leaves localhost — the exclude endpoint changes what gets
routed, so unauthenticated it is a remote "disable this operator's models"
button.

Tests: `tests/test_dashboard_example.py` in the repo root (skipped unless
FastAPI is installed).
