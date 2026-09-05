# Building a dashboard on top of autoheal

This tool is a cron job with no UI. That is deliberate — a route healer needs to
work on a headless box with no browser anywhere near it. But the data it produces
answers a question people want to see rather than grep: *which of my models are
broken right now, and what do I do about each one?*

This document is the integration contract. It describes the four files autoheal
reads and writes, the direction data flows between them, and the mistakes that
are easy to make when a dashboard joins the loop. A working reference
implementation lives in [`examples/dashboard/`](examples/dashboard/) — about 200
lines of FastAPI, no database of its own.

If you only read one section, read [Five ways to get this
wrong](#five-ways-to-get-this-wrong). Every item in it was a real bug.

## The four files

| file | written by | read by | contains |
|---|---|---|---|
| `config.yaml` | autoheal (`--apply`) | Hermes | the route: `auxiliary.<task>`, `fallback_providers` |
| health cache (`--cache`) | autoheal, every run | a dashboard | last probe verdict, streaks, latency window, per candidate |
| exclude list (`--exclude-file`) | **a dashboard** | autoheal | models to never probe again |
| provider DB (`--sqlite-db`) | **a dashboard** | autoheal | which providers/models exist at all |

Autoheal owns `config.yaml` and the health cache. A dashboard owns the exclude
list and the provider DB. Nothing is owned by both, and that is the whole design:
two writers on one file is the failure this project already fought once.

```
                      ┌─────────────────┐
   operator ──────────▶  dashboard UI   │
                      └────────┬────────┘
                    writes     │     reads
          ┌────────────────────┼────────────────────┐
          ▼                    ▼                    ▼
   provider DB          exclude list          health cache
  (which models)      (never probe these)   (what the probes found)
          │                    │                    ▲
          └──────────┬─────────┘                    │
                     ▼                              │
              ┌─────────────┐                       │
              │  autoheal   │───────────────────────┘
              │  (cron)     │        writes
              └──────┬──────┘
                     ▼ writes
                config.yaml ──────▶ Hermes reads the route
```

Read the arrows before designing anything: the dashboard never writes
`config.yaml`, and autoheal never writes the provider DB.

## Reading the health cache

The cache is a flat JSON object, one key per candidate:

```json
{
  "VendorA|https://a.example/v1|chat-v4": {
    "ok": true,
    "state": "up",
    "err": "",
    "fail_streak": 0,
    "pass_streak": 5,
    "latency": 1.2,
    "lat_window": [1.1, 1.4, 1.2, 1.2, 1.3],
    "context": 1000000,
    "ts": 1788623429
  }
}
```

Do not parse it by hand. `hermes_aux_autoheal.report` ships the parser and the
error classifier:

```python
from hermes_aux_autoheal import report

out = report.summarize([
    ('compression', '~/.hermes/.aux_autoheal_health.json'),
    ('vision',      '~/.hermes/.aux_vision_health.json'),
])
# {'problems': [...], 'ok_count': 24, 'total': 27, 'last_probe_ts': 1788623429}
```

Each problem row carries a `category` — the field a UI should branch on:

| category | means | what the operator does | offer delete? |
|---|---|---|---|
| `no_balance` | account out of credit | top up | yes |
| `auth` | key rejected | replace the key | **no** — fix it, don't hide it |
| `model_gone` | gateway no longer serves it | untick the model | yes |
| `no_vision` | text-only model, image probe refused | exclude from vision only | yes |
| `upstream` | provider-side 5xx | nothing, wait | no |
| `timeout` | slow or hanging endpoint | nothing, wait | no |
| `rate_limit` | 429 / quota paused | wait | no |
| `unknown` | failed with no message | investigate | yes |
| `other` | unmatched error | read the text | yes |

`report.DELETABLE` holds that last column. Categories that heal themselves must
not get a delete button — the button writes a permanent exclusion, and using it
on a rate limit silently removes a healthy model from the pool forever.

`label` and `hint` are English defaults for display. Localise from `category`;
do not translate the labels in place, and do not match on them.

## Writing the exclude list

The exclude list is the only *write* a dashboard makes into autoheal's world.
Format is plain JSON so a UI needs no library:

```json
{"version": 1,
 "entries": [
   {"provider": "VendorA", "model": "chat-v4",
    "task": "*", "reason": "no_balance", "added": "2026-09-05T21:10:00"}
 ]}
```

- `task` is `"*"` (every task) or one task name. `"task": "vision"` excludes a
  text-only model from vision while leaving it usable for compression — that is
  the single most common real case.
- Matching is case-insensitive on both provider and model. A provider label is
  operator-typed; a model id arrives capitalised differently from a `/v1/models`
  listing than from `config.yaml`.
- `reason` and `added` are ignored by autoheal. Store them anyway: six weeks
  later nobody remembers why a model is on the list.
- Point the CLI at the file with `--exclude-file`, or use `--exclude
  VendorA/chat-v4` (repeatable) for one-offs.

Write it atomically (temp file + `os.replace`). Autoheal reads it on every tick,
and a half-written file parses as "nothing excluded" — fail-open by design, but
it means a torn write silently re-probes everything for one tick.

## Serving the provider DB

`--sqlite-db` lets autoheal read candidates from a table instead of (or as well
as) `custom_providers` in `config.yaml`. Default shape:

```sql
CREATE TABLE llm_providers (
  id             INTEGER PRIMARY KEY,
  name           TEXT NOT NULL,   -- provider label, also picks the key env var
  base_url       TEXT NOT NULL,
  api_key        TEXT NOT NULL,
  model          TEXT NOT NULL,   -- the primary/default model
  is_active      INTEGER DEFAULT 1,
  enabled_models TEXT             -- JSON array of model ids
);
```

Every column name is overridable, so an existing schema does not need a
migration:

```python
discovery.from_sqlite(db, table='providers', name_col='label',
                      url_col='endpoint', model_col='default_model',
                      enabled_col='models_json', active_col='active')
```

Two things to know:

- **The API key in the DB is not what autoheal uses.** It resolves `key_env`
  from the provider name and reads the value from `.env`. The DB is the registry;
  `.env` is the keystore. A dashboard that stores keys must keep both in sync, or
  probes fail with `auth` while the UI insists the key is fine.
- **`is_active = 0` removes a provider from probing entirely**, which is a
  blunter instrument than the exclude list: it drops every model of that
  provider. Use `is_active` for "I am not using this vendor", the exclude list
  for "this one model is dead".

### If the DB has a "fetch models" button

A provider's `/v1/models` listing is the obvious way to populate
`enabled_models`, and it has one trap: a relay's catalogue also fronts
embeddings, speech, image and moderation models. `discovery.is_chat_model()`
filters those for autoheal's own discovery path, but a dashboard's fetch button
must filter them itself:

```python
from hermes_aux_autoheal import discovery

ids = [m for m in fetched if discovery.is_chat_model(m)]
```

Without it, an embedding id gets ticked, the probe posts `/chat/completions`,
gets `HTTP 400 invalid_request_error`, and autoheal classifies that as permanent
on the first strike. Nothing breaks — the model is never routed — but the
dashboard grows a red row nobody can act on.

Also cap what the button ticks. The probe budget is one API call per model per
TTL window; a relay returning 60 ids turns a 5-minute cron into 60 calls every
10 minutes.

## Five ways to get this wrong

**1. Reading `state` instead of `ok`.** These answer different questions.
`ok` is "did the last probe succeed?". `state` is the hysteresis verdict and only
reaches `up` after `promote_streak` (default 2) consecutive successes — so a
model that just recovered is `ok=true, state="down"` for one tick. A dashboard
that filters on `state == "up"` reports working models as broken. The user then
tests the model by hand, watches it answer, and stops believing the page. Healthy
means `ok is True`, whatever `state` says.

**2. Matching status codes as bare numbers.** Providers echo a request id:

```
HTTP 429 quota paused (request id: 20260903404131601478965c955d568)
```

`'404' in err` is true here. The quota error gets filed as a dead model, and the
UI offers a delete button that permanently excludes a model that was fine.
Match `http 404`, not `404`. `report.classify()` already does.

**3. Probing on page load.** Tempting, and it burns quota: every refresh costs
one API call per model, and a rate limit reached this way looks exactly like a
provider outage in the cache. The dashboard is a *reader*. Autoheal probes on a
timer, writes the cache, and the page renders what was found — opening it costs
nothing and cannot affect the route.

**4. Writing `config.yaml` from the dashboard.** Two writers on one config file
is a race, and it is the one this project already lost once: a dashboard-ordered
provider list overwrote a health-ranked chain, so a dead host stayed at slot 0
and every request paid a failed round-trip first. If the dashboard must change
the route, change the *inputs* (provider DB, exclude list) and let the next tick
write the file. Untick a model and it is gone from the route within one TTL
window, with the chain re-ranked around the gap.

**5. Untick without exclude.** Removing a model from `enabled_models` is not
enough on its own. Autoheal also discovers models from a provider's `/v1/models`
listing, so an unticked model comes straight back on the next tick. The delete
action needs all three: exclude-list entry (stops the probe), untick (stops the
discovery), purge the cache row (clears the UI immediately instead of waiting for
the TTL). The reference implementation does all three in one endpoint.

## Security, if the dashboard is on a network

The reference app binds `127.0.0.1` and requires a session cookie. Both matter,
and neither is optional theatre:

- The health cache contains provider names, endpoints and error text. Error text
  comes from upstream and has been observed to echo request context; treat it as
  untrusted and redact anything key-shaped before rendering
  (`report.redact()` does this, and `report.problems()` applies it).
- The exclude-list and untick endpoints change what gets routed. Unauthenticated,
  they are a remote "disable this operator's models" button.
- If you expose it beyond localhost, put it behind a reverse proxy with TLS and
  real auth. Do not rely on "only I know the URL" — that is not access control.

Never render `api_key` from the provider DB, not even masked-by-CSS. Masking in
the browser means the value was still sent.

## Reference implementation

[`examples/dashboard/app.py`](examples/dashboard/app.py) — FastAPI, one file,
stdlib + `fastapi`/`uvicorn`. It shows the read path (health cache → rows), the
write path (delete → exclude + untick + purge), and the auth boundary. It is a
starting point to read and adapt, not a product: no user management, no CSRF
tokens, no rate limiting on login.

```bash
pip install fastapi uvicorn
python examples/dashboard/app.py --help
```
