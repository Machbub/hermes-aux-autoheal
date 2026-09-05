#!/usr/bin/env python3
"""Reference dashboard for hermes-aux-autoheal — read the health, act on it.

One file, no database of its own, ~250 lines. It exists to make the integration
contract in ../../DASHBOARD.md concrete: what to read, what to write, and where
the auth boundary goes.

    pip install fastapi uvicorn
    python app.py --cache ~/.hermes/.aux_autoheal_health.json \
                  --exclude-file ~/.hermes/.aux_probe_blocklist.json

Then open http://127.0.0.1:8787 and log in with the token the startup banner
prints.

WHAT IT DOES
  GET  /                  one page, server-rendered, no build step
  GET  /api/health        the problem rows (reads the cache, probes nothing)
  POST /api/exclude       stop probing one model: exclude + untick + purge
  POST /api/login         session cookie from a startup token

WHAT IT DELIBERATELY DOES NOT DO
  - probe on page load (that spends quota per refresh; autoheal probes on a timer)
  - write config.yaml (autoheal owns it; two writers is a race)
  - render api_key values, even masked
  - user management, CSRF tokens, login rate limiting, TLS

SECURITY: binds 127.0.0.1 by default and every endpoint except /api/login and
the login page requires a session cookie. The exclude endpoint changes what gets
routed, so unauthenticated it is a remote "disable this operator's models"
button. If you expose this past localhost, put it behind a reverse proxy with
TLS and real authentication — "nobody knows the URL" is not access control.
"""

import argparse
import json
import os
import secrets
import sqlite3
import sys
import tempfile
import time
from datetime import datetime, timezone

try:
    from fastapi import Cookie, Depends, FastAPI, HTTPException
    from fastapi.responses import HTMLResponse, JSONResponse
    from pydantic import BaseModel
except ImportError:                                     # pragma: no cover
    sys.exit('this example needs fastapi + uvicorn: pip install fastapi uvicorn')

# Import from the installed package; running from a clone works too.
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', '..'))
from hermes_aux_autoheal import report                  # noqa: E402

app = FastAPI(title='autoheal dashboard (reference)')

# Populated in main(). A dict rather than globals so the request handlers read
# one obvious source.
CFG = {
    'caches': [],          # [(task_label, path)]
    'exclude_file': None,
    'sqlite_db': None,
    'token': '',
}
SESSIONS = {}              # cookie value -> expiry epoch
SESSION_TTL = 12 * 3600
COOKIE = 'autoheal_session'


# ---------------------------------------------------------------- auth


def require_auth(session: str = Cookie(None, alias=COOKIE)):
    """Reject anything without a live session cookie.

    Every state-changing endpoint depends on this. The check is intentionally
    boring: a token compared in constant time, an expiry, nothing else.
    """
    expiry = SESSIONS.get(session or '')
    if not expiry or expiry < time.time():
        SESSIONS.pop(session or '', None)
        raise HTTPException(status_code=401, detail='login required')
    return session


class LoginRequest(BaseModel):
    token: str


@app.post('/api/login')
def login(req: LoginRequest):
    # compare_digest, not ==: a plain comparison leaks the token's prefix
    # through timing, and this token is the only thing guarding the write path.
    if not secrets.compare_digest(req.token.strip(), CFG['token']):
        raise HTTPException(status_code=401, detail='bad token')
    cookie = secrets.token_urlsafe(32)
    SESSIONS[cookie] = time.time() + SESSION_TTL
    resp = JSONResponse({'status': 'ok'})
    # httponly: JS never needs it, so JS should never be able to read it.
    # samesite=strict: this app has no cross-site flow to break.
    resp.set_cookie(COOKIE, cookie, httponly=True, samesite='strict',
                    max_age=SESSION_TTL)
    return resp


# ---------------------------------------------------------------- read path


@app.get('/api/health')
def api_health(_=Depends(require_auth)):
    """The problem rows. Reads the cache; probes nothing.

    Cost of opening this page: one file read per task. That property is the
    reason the dashboard cannot burn quota no matter how often it is refreshed.
    """
    out = report.summarize(CFG['caches'])
    out['status'] = 'ok'
    out['cache_age_s'] = (int(time.time() - out['last_probe_ts'])
                          if out['last_probe_ts'] else None)
    return out


# ---------------------------------------------------------------- write path


class ExcludeRequest(BaseModel):
    provider: str
    model: str
    task: str = '*'


def _write_json_atomic(path, payload):
    """Write JSON atomically. A torn exclude file parses as 'nothing excluded'."""
    directory = os.path.dirname(os.path.abspath(path)) or '.'
    fd, tmp = tempfile.mkstemp(dir=directory, prefix='.tmp-', suffix='.json')
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as f:
            json.dump(payload, f, indent=1)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def _exclude_add(path, provider, model, task, reason):
    """Append one entry, case-insensitively de-duplicated. Returns True if added."""
    try:
        with open(path, encoding='utf-8') as f:
            data = json.load(f)
        entries = data.get('entries') if isinstance(data, dict) else data
        entries = entries if isinstance(entries, list) else []
    except (OSError, ValueError):
        entries = []

    def norm(v):
        return (v or '').strip().lower()

    for e in entries:
        if not isinstance(e, dict):
            continue
        if (norm(e.get('provider')) == norm(provider)
                and norm(e.get('model')) == norm(model)
                and (e.get('task') or '*') == task):
            return False
    entries.append({
        'provider': provider, 'model': model, 'task': task,
        'reason': reason,
        'added': datetime.now(timezone.utc).isoformat(timespec='seconds'),
    })
    _write_json_atomic(path, {'version': 1, 'entries': entries})
    return True


def _untick(db_path, provider, model):
    """Drop one model from a provider's enabled_models. Returns True if changed.

    Needed alongside the exclude entry only when the provider DB is in play:
    autoheal also discovers models from a provider's own listing, so a model
    that stays ticked keeps reappearing as a candidate (it will be excluded
    before any API call, but it clutters the registry).
    """
    if not db_path or not os.path.exists(db_path):
        return False
    try:
        conn = sqlite3.connect(db_path, timeout=10)
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            'SELECT id, enabled_models FROM llm_providers WHERE name = ?',
            (provider,)).fetchone()
        if not row:
            conn.close()
            return False
        try:
            enabled = json.loads(row['enabled_models'] or '[]')
        except (ValueError, TypeError):
            enabled = []
        keep = [m for m in enabled if str(m).strip().lower() != model.strip().lower()]
        if len(keep) == len(enabled):
            conn.close()
            return False
        conn.execute('UPDATE llm_providers SET enabled_models = ? WHERE id = ?',
                     (json.dumps(keep), row['id']))
        conn.commit()
        conn.close()
        return True
    except sqlite3.Error:
        return False


def _purge_cache_rows(provider, model):
    """Remove this candidate's rows from every cache, so the UI clears at once.

    Cosmetic but worth doing: without it the row sits there until the TTL
    expires and the operator presses the button again, wondering why nothing
    happened.
    """
    removed = 0
    for _task, path in CFG['caches']:
        cache = report.load_cache(path)
        if not cache:
            continue
        drop = []
        for key in cache:
            ident = report.parse_key(key)
            same_model = ident['model'].strip().lower() == model.strip().lower()
            same_prov = (not ident['provider']
                         or ident['provider'].strip().lower() == provider.strip().lower())
            if same_model and same_prov:
                drop.append(key)
        if drop:
            for key in drop:
                cache.pop(key, None)
            _write_json_atomic(path, cache)
            removed += len(drop)
    return removed


@app.post('/api/exclude')
def api_exclude(req: ExcludeRequest, _=Depends(require_auth)):
    """Stop probing one permanently-dead model. Three writes, all required.

    The category check is done HERE, on the server, from the cache — never
    trusted from the client. A rate-limited model recovers on its own; writing
    a permanent exclusion for it silently removes a healthy model from the pool.
    """
    provider, model = req.provider.strip(), req.model.strip()
    if not model:
        raise HTTPException(status_code=400, detail='model is required')

    category = None
    for task, path in CFG['caches']:
        rows, _ok = report.problems(report.load_cache(path), task=task)
        for row in rows:
            if row['model'].strip().lower() != model.lower():
                continue
            if provider and row['provider'] and row['provider'].lower() != provider.lower():
                continue
            category = row['category']
            break
        if category:
            break

    if category is None:
        raise HTTPException(
            status_code=404,
            detail='not a failing model in any cache — nothing to exclude')
    if category not in report.DELETABLE:
        raise HTTPException(
            status_code=400,
            detail=f'category "{category}" recovers on its own or needs a '
                   'different fix; excluding it would hide a working model')

    added = _exclude_add(CFG['exclude_file'], provider, model, req.task, category)
    unticked = _untick(CFG['sqlite_db'], provider, model)
    purged = _purge_cache_rows(provider, model)
    return {'status': 'ok', 'category': category, 'excluded': added,
            'unticked': unticked, 'cache_rows_purged': purged}


# ---------------------------------------------------------------- page

PAGE = """<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>autoheal</title>
<style>
 body{font:14px/1.5 system-ui,sans-serif;background:#111;color:#eee;margin:0;padding:1.5rem}
 h1{font-size:1.1rem;margin:0 0 1rem}
 table{border-collapse:collapse;width:100%;max-width:60rem}
 th,td{text-align:left;padding:.4rem .6rem;border-bottom:1px solid #333;vertical-align:top}
 th{color:#888;font-weight:600;font-size:.8rem;text-transform:uppercase}
 .cat{font-family:ui-monospace,monospace;font-size:.85rem}
 .err{color:#999;font-size:.8rem;max-width:24rem;word-break:break-word}
 button{background:#333;color:#eee;border:1px solid #555;border-radius:4px;
        padding:.25rem .6rem;cursor:pointer;font:inherit}
 button:hover{background:#444}
 button:disabled{opacity:.35;cursor:not-allowed}
 .ok{color:#6c6}.muted{color:#777}
 input{background:#222;color:#eee;border:1px solid #444;border-radius:4px;padding:.4rem}
</style></head><body>
<h1>autoheal &mdash; model health</h1>
<div id="login" hidden>
  <p class="muted">Paste the token from the server's startup banner.</p>
  <input id="tok" type="password" autocomplete="off">
  <button onclick="doLogin()">Log in</button>
  <p id="loginerr" class="muted"></p>
</div>
<div id="main" hidden>
  <p id="summary" class="muted"></p>
  <table><thead><tr>
    <th>task</th><th>provider / model</th><th>category</th><th>error</th><th></th>
  </tr></thead><tbody id="rows"></tbody></table>
</div>
<script>
async function doLogin(){
  const r = await fetch('/api/login',{method:'POST',
    headers:{'content-type':'application/json'},
    body:JSON.stringify({token:document.getElementById('tok').value})});
  if(r.ok){ load(); } else {
    document.getElementById('loginerr').textContent = 'rejected';
  }
}
function esc(s){ const d=document.createElement('div'); d.textContent=s??''; return d.innerHTML; }
async function load(){
  const r = await fetch('/api/health');
  if(r.status===401){
    document.getElementById('login').hidden=false;
    document.getElementById('main').hidden=true; return;
  }
  const d = await r.json();
  document.getElementById('login').hidden=true;
  document.getElementById('main').hidden=false;
  const age = d.cache_age_s===null?'no probe yet':`probed ${d.cache_age_s}s ago`;
  document.getElementById('summary').textContent =
    `${d.problems.length} failing of ${d.total} — ${age}`;
  document.getElementById('rows').innerHTML = d.problems.map(p=>`
    <tr>
      <td class="muted">${esc(p.task)}</td>
      <td>${esc(p.provider)}<br><span class="muted">${esc(p.model)}</span></td>
      <td class="cat">${esc(p.category)}${p.in_grace?' <span class="muted">(grace)</span>':''}
          <br><span class="muted">${esc(p.hint)}</span></td>
      <td class="err">${esc(p.error)}</td>
      <td>${p.deletable
            ? `<button onclick="excl('${esc(p.provider)}','${esc(p.model)}')">stop probing</button>`
            : '<span class="muted">&mdash;</span>'}</td>
    </tr>`).join('') || '<tr><td colspan="5" class="ok">all probed models answered</td></tr>';
}
async function excl(provider, model){
  if(!confirm(`Never probe ${provider}/${model} again?`)) return;
  const r = await fetch('/api/exclude',{method:'POST',
    headers:{'content-type':'application/json'},
    body:JSON.stringify({provider, model, task:'*'})});
  const d = await r.json().catch(()=>({}));
  if(!r.ok) alert(d.detail || 'failed');
  load();
}
load();
</script></body></html>"""


@app.get('/', response_class=HTMLResponse)
def index():
    """The page itself is public; every byte of DATA on it requires the cookie."""
    return PAGE


# ---------------------------------------------------------------- main


def main(argv=None):
    home = os.environ.get('HERMES_HOME', os.path.expanduser('~/.hermes'))
    p = argparse.ArgumentParser(description=__doc__.split('\n')[0])
    p.add_argument('--cache', action='append', metavar='TASK=PATH',
                   help='health cache to read, repeatable '
                        f'(default: compression={home}/.aux_autoheal_health.json)')
    p.add_argument('--exclude-file',
                   default=f'{home}/.aux_probe_blocklist.json',
                   help='never-probe list this dashboard writes')
    p.add_argument('--sqlite-db',
                   help='optional provider DB to untick models in')
    p.add_argument('--host', default='127.0.0.1',
                   help='bind address (default 127.0.0.1 — see the security '
                        'note in the module docstring before changing it)')
    p.add_argument('--port', type=int, default=8787)
    p.add_argument('--token', help='login token (default: generated and printed)')
    args = p.parse_args(argv)

    caches = []
    for spec in (args.cache or [f'compression={home}/.aux_autoheal_health.json']):
        task, _, path = spec.partition('=')
        caches.append((task, os.path.expanduser(path or task)))
    CFG.update(caches=caches,
               exclude_file=os.path.expanduser(args.exclude_file),
               sqlite_db=os.path.expanduser(args.sqlite_db) if args.sqlite_db else None,
               token=args.token or secrets.token_urlsafe(18))

    print(f'reading  {", ".join(f"{t}: {p}" for t, p in caches)}')
    print(f'writing  {CFG["exclude_file"]}')
    if CFG['sqlite_db']:
        print(f'untick   {CFG["sqlite_db"]}')
    if args.host != '127.0.0.1':
        print(f'WARNING: bound to {args.host} — a session token is the ONLY '
              'thing protecting the write path. Put TLS and real auth in front.')
    print(f'\n  http://{args.host}:{args.port}   login token: {CFG["token"]}\n')

    import uvicorn
    uvicorn.run(app, host=args.host, port=args.port, log_level='warning')


if __name__ == '__main__':
    main()
