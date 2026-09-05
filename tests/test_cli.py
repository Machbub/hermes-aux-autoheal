"""End-to-end CLI test: real files, real config writes, no network.

The probe is monkeypatched (no live endpoints in CI) but everything else is the
real path: real discovery from a real config.yaml, real health cache on disk,
real atomic config write, real re-read to verify.

Run: python -m pytest tests/test_cli.py -q
"""
import json
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from hermes_aux_autoheal import cli, config_io, health

CONFIG_SRC = """\
# user's own comment at the top
model:
  provider: Alpha
  default: alpha-chat

auxiliary:
  compression:
    provider: Dead
    model: dead-model
    timeout: 300
    fallback_chain:
      - provider: AlsoDead
        model: also-dead
        base_url: https://dead.example/v1
        key_env: DEAD_API_KEY
        api_mode: chat_completions
    # comment after the chain

custom_providers:
  - name: Alpha
    base_url: https://alpha.example/v1
    key_env: ALPHA_API_KEY
    models:
      alpha-flash: {}
      alpha-reasoner: {}
  - name: Beta
    base_url: https://beta.example/v1
    key_env: BETA_API_KEY
    model: beta-mini
  - name: Dead
    base_url: https://dead.example/v1
    key_env: DEAD_API_KEY
    model: dead-model

tools:
  enabled: true
"""


@pytest.fixture
def home(tmp_path, monkeypatch):
    cfg = tmp_path / 'config.yaml'
    cfg.write_text(CONFIG_SRC)
    (tmp_path / '.env').write_text(
        'ALPHA_API_KEY=sk-alpha\n'
        'BETA_API_KEY=sk-beta\n'
        'DEAD_API_KEY=sk-dead\n')
    monkeypatch.setenv('HERMES_HOME', str(tmp_path))
    monkeypatch.setenv('HERMES_CONFIG', str(cfg))
    monkeypatch.setenv('HERMES_CONFIG_LOCK', str(tmp_path / '.lock'))
    monkeypatch.delenv('HERMES_ENV_FILE', raising=False)
    config_io.CONFIG_PATH = str(cfg)
    config_io.LOCK_FILE = str(tmp_path / '.lock')
    return tmp_path


def fake_probe(alive, *, latency=1.0):
    """Probe stub: models in ``alive`` answer, everything else 503s."""
    def _probe(base_url, model, api_key, *, timeout=None, user_agent=None,
               task='compression'):
        if model in alive:
            return True, latency, ''
        return False, 0.1, 'HTTP 503 {"error":{"code":"model_not_found"}}'
    return _probe


def read_cfg(home):
    import yaml
    with open(home / 'config.yaml') as f:
        return yaml.safe_load(f)


def test_dry_run_does_not_write(home, monkeypatch, capsys):
    monkeypatch.setattr(health, 'probe',
                        fake_probe({'alpha-flash', 'beta-mini'}))
    before = (home / 'config.yaml').read_text()

    rc = cli.main(['--task', 'compression'])
    out = capsys.readouterr().out

    assert rc == 0
    assert 'DRY RUN' in out
    assert (home / 'config.yaml').read_text() == before, 'dry run must not write'


def test_apply_heals_dead_route(home, monkeypatch, capsys):
    monkeypatch.setattr(health, 'probe',
                        fake_probe({'alpha-flash', 'beta-mini'}))

    rc = cli.main(['--task', 'compression', '--apply'])
    out = capsys.readouterr().out
    cfg = read_cfg(home)
    comp = cfg['auxiliary']['compression']

    assert rc == 0
    assert 'route updated' in out
    assert comp['provider'] != 'Dead', 'dead primary must be replaced'
    assert comp['model'] in {'alpha-flash', 'beta-mini'}

    chain_models = [e['model'] for e in comp['fallback_chain']]
    assert 'dead-model' not in chain_models
    assert 'also-dead' not in chain_models

    # written entries must be complete enough for Hermes to call
    for entry in comp['fallback_chain']:
        assert entry['base_url'] and entry['key_env'] and entry['api_mode']


def test_apply_preserves_comments_and_other_sections(home, monkeypatch):
    monkeypatch.setattr(health, 'probe',
                        fake_probe({'alpha-flash', 'beta-mini'}))
    cli.main(['--task', 'compression', '--apply'])

    text = (home / 'config.yaml').read_text()
    cfg = read_cfg(home)

    if config_io.HAS_RUAMEL:
        assert text.startswith("# user's own comment at the top")
        assert '# comment after the chain' in text
    else:
        # Documented degradation: the PyYAML fallback cannot round-trip
        # comments. The data must still be correct.
        assert '# user' not in text

    assert cfg['model']['default'] == 'alpha-chat', 'chat model untouched'
    assert cfg['tools']['enabled'] is True
    assert len(cfg['custom_providers']) == 3


def test_chain_crosses_providers(home, monkeypatch):
    monkeypatch.setattr(health, 'probe',
                        fake_probe({'alpha-flash', 'alpha-reasoner', 'beta-mini'}))
    cli.main(['--task', 'compression', '--apply'])

    comp = read_cfg(home)['auxiliary']['compression']
    assert comp['provider'] == 'Alpha'
    assert comp['fallback_chain'][0]['provider'] == 'Beta', \
        'first fallback should not share the primary\'s provider'


def test_second_run_is_a_noop(home, monkeypatch, capsys):
    monkeypatch.setattr(health, 'probe',
                        fake_probe({'alpha-flash', 'beta-mini'}))
    cli.main(['--task', 'compression', '--apply'])
    mtime = os.stat(home / 'config.yaml').st_mtime_ns
    capsys.readouterr()

    rc = cli.main(['--task', 'compression', '--apply'])
    out = capsys.readouterr().out

    assert rc == 0
    assert out.strip() == '', 'a correct route must produce no output'
    assert os.stat(home / 'config.yaml').st_mtime_ns == mtime


def test_all_dead_leaves_config_alone(home, monkeypatch, capsys):
    monkeypatch.setattr(health, 'probe', fake_probe(set()))
    before = (home / 'config.yaml').read_text()

    rc = cli.main(['--task', 'compression', '--apply'])
    out = capsys.readouterr().out

    assert rc == 1
    assert 'no healthy candidate' in out
    assert (home / 'config.yaml').read_text() == before, \
        'an outage must not clobber the existing route'


def test_hysteresis_keeps_flaky_model_out_of_primary(home, monkeypatch):
    """A model failing ambiguously stays in the route but not at the front."""
    monkeypatch.setattr(health, 'probe',
                        fake_probe({'alpha-flash', 'beta-mini'}))
    cli.main(['--task', 'compression', '--apply'])
    first = read_cfg(home)['auxiliary']['compression']['model']

    # Now the winner starts timing out (ambiguous), everything else fine.
    def flaky(base_url, model, api_key, *, timeout=None, user_agent=None,
              task='compression'):
        if model == first:
            return False, 45.0, 'timeout: read timed out'
        if model in {'alpha-flash', 'beta-mini'}:
            return True, 1.0, ''
        return False, 0.1, 'HTTP 503 model_not_found'

    monkeypatch.setattr(health, 'probe', flaky)
    cli.main(['--task', 'compression', '--apply', '--no-cache'])

    comp = read_cfg(home)['auxiliary']['compression']
    assert comp['model'] != first, 'a timing-out model must lose the primary slot'
    # first strike only: still present as a fallback, not evicted outright
    assert first in [e['model'] for e in comp['fallback_chain']]


def test_permanent_failure_evicts_immediately(home, monkeypatch):
    monkeypatch.setattr(health, 'probe',
                        fake_probe({'alpha-flash', 'alpha-reasoner', 'beta-mini'}))
    cli.main(['--task', 'compression', '--apply'])

    def gone(base_url, model, api_key, *, timeout=None, user_agent=None,
             task='compression'):
        if model == 'alpha-flash':
            return False, 0.1, 'HTTP 404 model does not exist'
        if model in {'alpha-reasoner', 'beta-mini'}:
            return True, 1.0, ''
        return False, 0.1, 'HTTP 503 model_not_found'

    monkeypatch.setattr(health, 'probe', gone)
    cli.main(['--task', 'compression', '--apply', '--no-cache'])

    comp = read_cfg(home)['auxiliary']['compression']
    everything = [comp['model']] + [e['model'] for e in comp['fallback_chain']]
    assert 'alpha-flash' not in everything, \
        'a 404 is a verdict — no grace period'


def test_lock_contention_returns_2(home, monkeypatch, capsys):
    import fcntl
    monkeypatch.setattr(health, 'probe',
                        fake_probe({'alpha-flash', 'beta-mini'}))
    monkeypatch.setenv('HERMES_CONFIG_LOCK_WAIT', '0.5')
    monkeypatch.setattr(config_io, 'LOCK_WAIT_SECONDS', 0.5)

    before = (home / 'config.yaml').read_text()
    holder = open(config_io.LOCK_FILE, 'w')
    fcntl.flock(holder, fcntl.LOCK_EX | fcntl.LOCK_NB)
    try:
        rc = cli.main(['--task', 'compression', '--apply'])
    finally:
        fcntl.flock(holder, fcntl.LOCK_UN)
        holder.close()

    out = capsys.readouterr().out
    assert rc == 2
    assert 'another writer' in out
    assert (home / 'config.yaml').read_text() == before


def test_heals_a_task_that_does_not_exist_yet(home, monkeypatch):
    monkeypatch.setattr(health, 'probe',
                        fake_probe({'alpha-flash', 'beta-mini'}))
    rc = cli.main(['--task', 'summarization', '--apply'])

    cfg = read_cfg(home)
    assert rc == 0
    assert cfg['auxiliary']['summarization']['model'] in {'alpha-flash', 'beta-mini'}
    # the pre-existing task must be untouched
    assert cfg['auxiliary']['compression']['provider'] == 'Dead'


def test_sqlite_source_adds_candidates(home, monkeypatch):
    import sqlite3
    db = home / 'dash.db'
    conn = sqlite3.connect(db)
    conn.execute('CREATE TABLE llm_providers (name TEXT, base_url TEXT, '
                 'model TEXT, enabled_models TEXT, is_active INTEGER)')
    conn.execute('INSERT INTO llm_providers VALUES (?,?,?,?,1)',
                 ('Alpha', 'https://alpha.example/v1', 'alpha-extra',
                  json.dumps(['alpha-extra'])))
    conn.commit()
    conn.close()

    monkeypatch.setattr(health, 'probe', fake_probe({'alpha-extra'}))
    rc = cli.main(['--task', 'compression', '--apply', '--sqlite-db', str(db)])

    comp = read_cfg(home)['auxiliary']['compression']
    assert rc == 0
    assert comp['model'] == 'alpha-extra', \
        'a model only the dashboard knows about should still be usable'


def test_health_cache_persists_between_runs(home, monkeypatch):
    monkeypatch.setattr(health, 'probe',
                        fake_probe({'alpha-flash', 'beta-mini'}))
    cli.main(['--task', 'compression', '--apply'])

    cache_path = home / '.aux_autoheal_health.json'
    assert cache_path.exists()
    data = json.loads(cache_path.read_text())
    assert any('alpha-flash' in k for k in data)

    # Probe now raises: a cached-fresh run must not call it at all.
    def explode(*a, **k):
        raise AssertionError('probe called despite a fresh cache')

    monkeypatch.setattr(health, 'probe', explode)
    rc = cli.main(['--task', 'compression', '--apply'])
    assert rc == 0


def test_missing_config_is_reported(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv('HERMES_HOME', str(tmp_path))
    rc = cli.main(['--config', str(tmp_path / 'nope.yaml')])
    err = capsys.readouterr().err
    assert rc == 2
    assert 'config not found' in err


def test_apply_heals_dead_chat_fallback_chain(home, monkeypatch, capsys):
    """The v0.6.0 wiring: --apply must also maintain fallback_providers.

    Seeded with dead entries (the pre-fix state: the tool never touched the
    top-level chat list). After apply the chain must be healthy, and
    model.provider/model.default — the user's choice — must be untouched.
    """
    # seed a stale chat chain pointing at dead models
    text = (home / 'config.yaml').read_text()
    stale = ('fallback_providers:\n'
             '  - provider: Dead\n'
             '    model: dead-model\n'
             '    base_url: https://dead.example/v1\n'
             '    key_env: DEAD_API_KEY\n'
             '    api_mode: chat_completions\n')
    (home / 'config.yaml').write_text(text + stale)

    monkeypatch.setattr(health, 'probe',
                        fake_probe({'alpha-flash', 'beta-mini'}))
    rc = cli.main(['--task', 'compression', '--apply'])
    out = capsys.readouterr().out
    cfg = read_cfg(home)

    assert rc == 0
    assert 'chat fallback_providers' in out, \
        'apply must report the chat chain write'
    chain = cfg['fallback_providers']
    assert isinstance(chain, list) and chain, 'fallback_providers must exist'
    models = {(e['provider'], e['model']) for e in chain}
    assert ('Dead', 'dead-model') not in models
    assert models <= {('Alpha', 'alpha-flash'), ('Beta', 'beta-mini')}
    for entry in chain:
        assert entry['base_url'] and entry['key_env'] and entry['api_mode'], \
            'written chat entries must resolve without the dashboard'

    # the user's chat choice is never rewritten
    assert cfg['model']['provider'] == 'Alpha'
    assert cfg['model']['default'] == 'alpha-chat'


def test_no_chat_chain_leaves_fallback_providers_alone(home, monkeypatch,
                                                       capsys):
    """--no-chat-chain must skip the top-level list entirely.

    Two autoheal jobs may run side by side (compression + vision); the vision
    one must not fight the compression one over fallback_providers.
    """
    # seed a stale chat chain; even so, --no-chat-chain must not touch it
    text = (home / 'config.yaml').read_text()
    stale = ('fallback_providers:\n'
             '  - provider: Dead\n'
             '    model: dead-model\n'
             '    base_url: https://dead.example/v1\n'
             '    key_env: DEAD_API_KEY\n'
             '    api_mode: chat_completions\n')
    (home / 'config.yaml').write_text(text + stale)

    monkeypatch.setattr(health, 'probe',
                        fake_probe({'alpha-flash', 'beta-mini'}))
    rc = cli.main(['--task', 'compression', '--apply', '--no-chat-chain'])
    out = capsys.readouterr().out
    cfg = read_cfg(home)

    assert rc == 0
    assert 'chat fallback_providers' not in out, \
        'no-chat-chain must not report a chat chain write'
    assert cfg['fallback_providers'] == [{
        'provider': 'Dead', 'model': 'dead-model',
        'base_url': 'https://dead.example/v1', 'key_env': 'DEAD_API_KEY',
        'api_mode': 'chat_completions',
    }], 'fallback_providers must be untouched'


def test_chat_chain_dry_run_does_not_write(home, monkeypatch, capsys):
    (home / 'config.yaml').write_text(
        (home / 'config.yaml').read_text()
        + ('fallback_providers:\n'
           '  - provider: Dead\n'
           '    model: dead-model\n'
           '    base_url: https://dead.example/v1\n'
           '    key_env: DEAD_API_KEY\n'
           '    api_mode: chat_completions\n'))
    monkeypatch.setattr(health, 'probe',
                        fake_probe({'alpha-flash', 'beta-mini'}))

    rc = cli.main(['--task', 'compression'])
    out = capsys.readouterr().out
    cfg = read_cfg(home)

    assert rc == 0
    assert 'DRY RUN' in out and 'chat fallback_providers' in out
    assert cfg['fallback_providers'][0]['model'] == 'dead-model', \
        'dry run must not rewrite the chat chain'


def test_chat_chain_second_run_is_a_noop(home, monkeypatch, capsys):
    monkeypatch.setattr(health, 'probe',
                        fake_probe({'alpha-flash', 'beta-mini'}))
    cli.main(['--task', 'compression', '--apply'])
    capsys.readouterr()
    mtime = os.stat(home / 'config.yaml').st_mtime_ns

    rc = cli.main(['--task', 'compression', '--apply'])
    out = capsys.readouterr().out

    assert rc == 0
    assert out.strip() == '', 'a correct chat chain must produce no output'
    assert os.stat(home / 'config.yaml').st_mtime_ns == mtime


def test_chat_chain_avoids_the_primarys_dead_host_end_to_end(home, monkeypatch,
                                                            capsys):
    """The whole path, with the chat primary failing its own probe.

    `Alpha/alpha-chat` is the chat model and `Alpha` is the only provider on
    alpha.example. When alpha-chat stops answering, health filtering removes it
    from the pool the picker sees — and with it the only record of which host the
    primary lives on. A spare on that same dead host must not be offered first.
    """
    # Alpha gains a peer model on its own host; Beta lives elsewhere.
    text = (home / 'config.yaml').read_text().replace(
        '      alpha-reasoner: {}',
        '      alpha-reasoner: {}\n      alpha-chat: {}')
    (home / 'config.yaml').write_text(text)

    # alpha-chat (the chat primary) is DOWN. Its peers on the same host answer.
    monkeypatch.setattr(health, 'probe',
                        fake_probe({'alpha-flash', 'alpha-reasoner',
                                    'beta-mini'}))

    rc = cli.main(['--task', 'compression', '--apply', '--chat-depth', '2'])
    capsys.readouterr()
    cfg = read_cfg(home)

    assert rc == 0
    chain = cfg['fallback_providers']
    assert chain, 'spares must be maintained while the primary is down'
    assert 'alpha.example' not in chain[0]['base_url'], (
        'slot 0 sits on the host that just took the primary down: '
        f'{[(e["provider"], e["model"]) for e in chain]}')
    assert cfg['model']['default'] == 'alpha-chat', 'chat model untouched'


def test_chat_chain_skipped_without_model_default(home, monkeypatch, capsys):
    import re
    text = (home / 'config.yaml').read_text()
    # drop only the top-level model: block (it precedes custom_providers,
    # which must survive — removing it would kill every candidate)
    no_model = re.sub(r'(?m)^model:.*?(?=^custom_providers:)',
                      '', text, flags=re.S)
    assert not re.search(r'(?m)^model:', no_model), 'top-level model block must be gone'
    assert 'custom_providers:' in no_model
    (home / 'config.yaml').write_text(no_model)

    monkeypatch.setattr(health, 'probe',
                        fake_probe({'alpha-flash', 'beta-mini'}))
    rc = cli.main(['--task', 'compression', '--apply', '--verbose'])
    out = capsys.readouterr().out

    assert rc == 0
    assert 'skip chat chain' in out
    assert 'fallback_providers' not in read_cfg(home)
