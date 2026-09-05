"""The reference dashboard, exercised end to end.

Skipped unless FastAPI is installed — it is an example's dependency, not the
package's, and CI must not be forced to install a web framework to test a cron
tool.

The point of these tests is the CONTRACT between the two halves, not the HTML:
what the dashboard writes must be what autoheal reads back, and a category that
recovers on its own must never reach the exclude file.
"""
import json
import os
import sqlite3
import sys

import pytest

fastapi = pytest.importorskip('fastapi', reason='examples/dashboard needs fastapi')
# Importing TestClient is its own gate: starlette raises RuntimeError (not
# ImportError) when httpx is absent, which pytest would report as a collection
# ERROR rather than a skip. `pip install -e '.[dashboard]'` covers both.
try:
    from fastapi.testclient import TestClient                   # noqa: E402
except (ImportError, RuntimeError) as exc:                      # pragma: no cover
    pytest.skip(f'fastapi TestClient unavailable: {exc}',
                allow_module_level=True)

from hermes_aux_autoheal import exclude                         # noqa: E402

EXAMPLE_DIR = os.path.join(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))), 'examples', 'dashboard')


@pytest.fixture
def dash():
    if EXAMPLE_DIR not in sys.path:
        sys.path.insert(0, EXAMPLE_DIR)
    import app as module
    module.SESSIONS.clear()
    return module


@pytest.fixture
def env(tmp_path, dash):
    """Two caches, a provider DB, and a fresh exclude path.

    One deletable row (no credit), one self-healing row (rate limited), one
    healthy row whose ``state`` is still ``down`` — the recovered-model case
    that a state-based reader gets wrong.
    """
    comp = tmp_path / 'comp.json'
    comp.write_text(json.dumps({
        'VendorA|https://a.example/v1|dead-chat': {
            'ok': False, 'state': 'down',
            'err': 'HTTP 400 insufficient_user_quota',
            'fail_streak': 4, 'ts': 1788600000},
        'VendorB|https://b.example/v1|busy-chat': {
            'ok': False, 'state': 'down', 'err': 'HTTP 429 rate limit exceeded',
            'fail_streak': 1, 'ts': 1788600001},
        'VendorC|https://c.example/v1|good-chat': {
            'ok': True, 'state': 'down', 'err': '', 'ts': 1788600002},
    }))
    vis = tmp_path / 'vis.json'
    vis.write_text(json.dumps({
        'VendorA|vision|https://a.example/v1|dead-chat': {
            'ok': False, 'state': 'down',
            'err': 'HTTP 400 this model do not support image input',
            'fail_streak': 2, 'ts': 1788600003},
    }))

    db = tmp_path / 'data.db'
    conn = sqlite3.connect(str(db))
    conn.execute('CREATE TABLE llm_providers ('
                 'id INTEGER PRIMARY KEY, name TEXT NOT NULL, '
                 'base_url TEXT NOT NULL, api_key TEXT NOT NULL, '
                 'model TEXT NOT NULL, is_active INTEGER DEFAULT 1, '
                 'enabled_models TEXT)')
    conn.execute('INSERT INTO llm_providers '
                 '(name, base_url, api_key, model, enabled_models) '
                 'VALUES (?,?,?,?,?)',
                 ('VendorA', 'https://a.example/v1', 'secret', 'dead-chat',
                  json.dumps(['dead-chat', 'other-chat'])))
    conn.commit()
    conn.close()

    excl = tmp_path / 'blocklist.json'
    dash.CFG.update(caches=[('compression', str(comp)), ('vision', str(vis))],
                    exclude_file=str(excl), sqlite_db=str(db),
                    token='test-token')
    return {'client': TestClient(dash.app), 'exclude': excl, 'db': db,
            'comp': comp, 'vis': vis}


def _login(client):
    assert client.post('/api/login', json={'token': 'test-token'}).status_code == 200


# --------------------------------------------------------------- auth

def test_read_endpoint_requires_a_session(env):
    assert env['client'].get('/api/health').status_code == 401


def test_write_endpoint_requires_a_session(env):
    """Unauthenticated, this is a remote 'disable this operator's models' button."""
    r = env['client'].post('/api/exclude',
                           json={'provider': 'VendorA', 'model': 'dead-chat'})
    assert r.status_code == 401
    assert not env['exclude'].exists()


def test_a_bad_token_is_rejected(env):
    assert env['client'].post('/api/login',
                              json={'token': 'wrong'}).status_code == 401


def test_session_cookie_is_httponly(env):
    r = env['client'].post('/api/login', json={'token': 'test-token'})
    assert 'httponly' in r.headers.get('set-cookie', '').lower()


# --------------------------------------------------------------- read path

def test_recovered_model_is_not_reported_as_a_problem(env):
    """`ok=True, state='down'` is a model that just came back, not a broken one."""
    _login(env['client'])
    d = env['client'].get('/api/health').json()
    assert d['ok_count'] == 1
    assert 'good-chat' not in [p['model'] for p in d['problems']]


def test_report_merges_both_caches_and_sorts_worst_first(env):
    _login(env['client'])
    d = env['client'].get('/api/health').json()
    cats = [p['category'] for p in d['problems']]
    assert set(cats) == {'no_balance', 'no_vision', 'rate_limit'}
    assert cats.index('no_balance') < cats.index('rate_limit')
    assert {p['task'] for p in d['problems']} == {'compression', 'vision'}


def test_only_actionable_rows_offer_a_button(env):
    _login(env['client'])
    d = env['client'].get('/api/health').json()
    by_cat = {p['category']: p for p in d['problems']}
    assert by_cat['no_balance']['deletable'] is True
    assert by_cat['rate_limit']['deletable'] is False


def test_the_read_path_never_touches_the_provider_key(env):
    _login(env['client'])
    body = env['client'].get('/api/health').text
    assert 'secret' not in body


# --------------------------------------------------------------- write path

def test_a_self_healing_category_is_refused(env):
    """Excluding a rate limit would permanently remove a healthy model."""
    _login(env['client'])
    r = env['client'].post('/api/exclude',
                           json={'provider': 'VendorB', 'model': 'busy-chat'})
    assert r.status_code == 400
    assert 'recovers on its own' in r.json()['detail']
    assert not env['exclude'].exists()


def test_delete_does_all_three_writes(env):
    _login(env['client'])
    r = env['client'].post('/api/exclude',
                           json={'provider': 'VendorA', 'model': 'dead-chat'})
    d = r.json()
    assert r.status_code == 200
    assert d['category'] == 'no_balance'      # decided server-side, not sent
    assert d['excluded'] is True              # 1. exclude entry
    assert d['unticked'] is True              # 2. untick in the provider DB
    assert d['cache_rows_purged'] == 2        # 3. purge, both caches


def test_untick_removes_only_the_named_model(env):
    _login(env['client'])
    env['client'].post('/api/exclude',
                       json={'provider': 'VendorA', 'model': 'dead-chat'})
    conn = sqlite3.connect(str(env['db']))
    left = json.loads(conn.execute(
        'SELECT enabled_models FROM llm_providers WHERE name=?',
        ('VendorA',)).fetchone()[0])
    conn.close()
    assert left == ['other-chat']


def test_what_the_dashboard_writes_is_what_autoheal_reads(env):
    """The contract. A file the CLI cannot parse is a button that does nothing."""
    _login(env['client'])
    env['client'].post('/api/exclude',
                       json={'provider': 'VendorA', 'model': 'dead-chat'})

    entries = exclude.load(str(env['exclude']))
    assert len(entries) == 1
    assert exclude.is_excluded('VendorA', 'dead-chat', entries)
    # case-insensitive on both legs, as the CLI matches it
    assert exclude.is_excluded('vendora', 'DEAD-CHAT', entries)
    assert not exclude.is_excluded('VendorA', 'other-chat', entries)

    allowed, blocked = exclude.split(
        [{'provider': 'VendorA', 'model': 'dead-chat'},
         {'provider': 'VendorA', 'model': 'other-chat'}], entries)
    assert [c['model'] for c in allowed] == ['other-chat']
    assert len(blocked) == 1


def test_the_written_entry_keeps_a_reason_and_a_timestamp(env):
    _login(env['client'])
    env['client'].post('/api/exclude',
                       json={'provider': 'VendorA', 'model': 'dead-chat'})
    data = json.loads(env['exclude'].read_text())
    assert data['version'] == 1
    entry = data['entries'][0]
    assert entry['reason'] == 'no_balance'
    assert entry['added']                     # six weeks later, nobody remembers


def test_purged_rows_disappear_from_the_next_report(env):
    _login(env['client'])
    env['client'].post('/api/exclude',
                       json={'provider': 'VendorA', 'model': 'dead-chat'})
    d = env['client'].get('/api/health').json()
    assert [p['model'] for p in d['problems']] == ['busy-chat']


def test_excluding_something_not_failing_is_a_404(env):
    _login(env['client'])
    r = env['client'].post('/api/exclude',
                           json={'provider': 'Nope', 'model': 'nope-chat'})
    assert r.status_code == 404


def test_exclude_file_is_written_atomically(env, dash, monkeypatch):
    """A torn exclude file parses as 'nothing excluded' — one tick of full probing."""
    calls = []
    real_replace = os.replace
    monkeypatch.setattr(os, 'replace',
                        lambda a, b: (calls.append((a, b)), real_replace(a, b))[1])
    _login(env['client'])
    env['client'].post('/api/exclude',
                       json={'provider': 'VendorA', 'model': 'dead-chat'})
    assert any(dst == str(env['exclude']) for _src, dst in calls)
