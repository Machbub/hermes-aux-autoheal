"""End-to-end: a vision run must never route a text-only model.

The unit tests above prove the payload shape and the classification. This one
proves the CLI outcome: probing ``--task vision`` writes a route whose entries
all accepted an image payload, and the text-only model that passes the text
probe is demoted the moment an image is involved.
"""
import json
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from hermes_aux_autoheal import cli, config_io, health  # noqa: E402

TEXT_ONLY = 'deep-v4-text'          # answers 'ping', refuses images
VISION_OK = 'glm-vision-pro'        # answers both


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.delenv('HERMES_ENV_FILE', raising=False)
    cfg = tmp_path / 'config.yaml'
    cfg.write_text("""
model:
  provider: Alpha
  default: {vision_ok}
custom_providers:
  - name: Alpha
    base_url: https://alpha.test/v1
    key_env: ALPHA_API_KEY
    model: {vision_ok}
    models: {{ {vision_ok}: {{}}, {text_only}: {{}} }}
auxiliary:
  compression:
    provider: Alpha
    model: {vision_ok}
""".format(vision_ok=VISION_OK, text_only=TEXT_ONLY))
    monkeypatch.setenv('HERMES_HOME', str(tmp_path))
    monkeypatch.setenv('ALPHA_API_KEY', 'k')
    config_io.CONFIG_PATH = str(cfg)
    config_io.LOCK_FILE = str(tmp_path / '.lock')
    return tmp_path


def fake_vision_probe(alive_vision, *, latency=1.0):
    """Models that answer the VISION probe (image accepted)."""
    def _probe(base_url, model, api_key, *, timeout=None, user_agent=None,
               task='compression'):
        if task == 'vision':
            if model in alive_vision:
                return True, latency, ''
            return False, 0.1, 'HTTP 400 Model do not support image input'
        # text probe: everything answers
        return True, latency, ''
    return _probe


def read_cfg(home):
    import yaml
    with open(home / 'config.yaml') as f:
        return yaml.safe_load(f)


def test_vision_route_excludes_text_only_model(home, monkeypatch, capsys):
    monkeypatch.setattr(health, 'probe',
                        fake_vision_probe({VISION_OK}))

    rc = cli.main(['--task', 'vision', '--apply'])
    out = capsys.readouterr().out

    assert rc == 0
    assert 'route updated' in out
    vis = read_cfg(home)['auxiliary']['vision']
    assert vis['provider'] == 'Alpha'
    assert vis['model'] == VISION_OK
    chain_models = [e['model'] for e in vis['fallback_chain']]
    assert TEXT_ONLY not in chain_models, \
        'text-only model must never enter a vision route'


def test_vision_dry_run_reports_route(home, monkeypatch, capsys):
    monkeypatch.setattr(health, 'probe',
                        fake_vision_probe({VISION_OK, TEXT_ONLY}))
    rc = cli.main(['--task', 'vision'])
    out = capsys.readouterr().out
    assert rc == 0
    assert 'DRY RUN' in out


def test_compression_route_still_accepts_text_only_model(home, monkeypatch,
                                                         capsys):
    """The text probe stays text: the same model is fine for compression."""
    monkeypatch.setattr(health, 'probe',
                        fake_vision_probe({VISION_OK}))
    rc = cli.main(['--task', 'compression', '--apply'])
    out = capsys.readouterr().out
    assert rc == 0
    comp = read_cfg(home)['auxiliary']['compression']
    chain_models = [e['model'] for e in comp['fallback_chain']]
    assert TEXT_ONLY in chain_models or comp['model'] == TEXT_ONLY, \
        'text-only model is a legitimate compression candidate'
