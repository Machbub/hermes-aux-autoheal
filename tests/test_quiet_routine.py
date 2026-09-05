"""--quiet-routine: a healthy autoheal tick must say nothing.

The cron watchdog contract is "empty stdout = nothing to report". Without this
flag every successful route update reached the chat, which trains the reader to
ignore the job — so the one message that matters (chain exhausted) gets ignored
too.
"""
import json

import pytest

from hermes_aux_autoheal import cli, health


@pytest.fixture
def env(tmp_path, monkeypatch):
    """Two providers, both healthy, so a route update is always produced."""
    import yaml

    cfg = {
        'custom_providers': [
            {'name': 'Alpha', 'base_url': 'https://alpha.example/v1',
             'key_env': 'ALPHA_KEY', 'model': 'alpha-chat',
             'api_mode': 'chat_completions'},
            {'name': 'Beta', 'base_url': 'https://beta.example/v1',
             'key_env': 'BETA_KEY', 'model': 'beta-chat',
             'api_mode': 'chat_completions'},
            {'name': 'Gamma', 'base_url': 'https://gamma.example/v1',
             'key_env': 'GAMMA_KEY', 'model': 'gamma-chat',
             'api_mode': 'chat_completions'},
        ],
        'model': {'provider': 'Alpha', 'default': 'alpha-chat'},
        'auxiliary': {'compression': {'provider': 'Zeta', 'model': 'gone'}},
    }
    cfg_path = tmp_path / 'config.yaml'
    cfg_path.write_text(yaml.safe_dump(cfg))
    env_path = tmp_path / '.env'
    env_path.write_text('ALPHA_KEY=k\nBETA_KEY=k\nGAMMA_KEY=k\n')

    def fake_probe(base_url, model, api_key, *, timeout=None, user_agent=None,
                   task='compression'):
        return True, 1.0, ''

    monkeypatch.setattr(health, 'probe', fake_probe)
    return {'config': str(cfg_path), 'env': str(env_path),
            'cache': str(tmp_path / 'health.json')}


def _run(env, extra):
    return cli.main(['--config', env['config'], '--env-file', env['env'],
                     '--cache', env['cache'], '--no-context-lookup',
                     '--no-discover-models', '--min-context', '0'] + extra)


def test_routine_update_is_silent_with_quiet_routine(env, capsys):
    rc = _run(env, ['--apply', '--quiet-routine'])
    out = capsys.readouterr().out
    assert rc == 0
    assert out == '', f'expected silence, got: {out!r}'


def test_same_update_is_reported_without_the_flag(env, capsys):
    rc = _run(env, ['--apply'])
    out = capsys.readouterr().out
    assert rc == 0
    assert 'route updated' in out


def test_exhausted_chain_still_speaks_under_quiet_routine(env, capsys,
                                                          monkeypatch):
    """One healthy model left = no spare. That must always reach the user."""
    import yaml
    cfg = yaml.safe_load(open(env['config']))
    cfg['custom_providers'] = cfg['custom_providers'][:1]
    with open(env['config'], 'w') as f:
        yaml.safe_dump(cfg, f)

    rc = _run(env, ['--apply', '--quiet-routine'])
    out = capsys.readouterr().out
    assert rc == 0
    assert 'route updated' in out
    assert 'last entry' in out


def test_no_healthy_candidate_still_speaks(env, capsys, monkeypatch):
    def dead_probe(base_url, model, api_key, *, timeout=None, user_agent=None,
                   task='compression'):
        return False, 0.0, 'HTTP 401 unauthorized'

    monkeypatch.setattr(health, 'probe', dead_probe)
    rc = _run(env, ['--apply', '--quiet-routine'])
    out = capsys.readouterr().out
    assert rc != 0 or 'ERROR' in out
    assert 'no healthy candidate' in out


def test_dry_run_still_prints_under_quiet_routine(env, capsys):
    """Dry run is interactive — someone is waiting for the answer."""
    rc = _run(env, ['--quiet-routine'])
    out = capsys.readouterr().out
    assert rc == 0
    assert 'DRY RUN' in out


def test_already_correct_is_silent_either_way(env, capsys):
    _run(env, ['--apply'])
    capsys.readouterr()
    # second run: nothing changed, and --verbose is off
    rc = _run(env, ['--apply', '--quiet-routine'])
    out = capsys.readouterr().out
    assert rc == 0
    assert out == ''
