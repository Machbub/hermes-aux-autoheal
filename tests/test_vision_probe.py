"""Tests for probing vision with a real image.

The compression probe sends ``'ping'`` as a plain string. Any text model
answers that. A vision route needs a model that accepts an image payload —
asking a text-only model for a vision route is how a conversation ends with
``Model do not support image input`` on every image the user sends.

The probe carries a 16×16 PNG. A text-only model refuses it with a 400, which is
classified as a permanent verdict, so it is demoted on the first strike and
never routed for vision. The health cache is scoped per task, so a verdict
earned on the text probe cannot leak into the vision route (or back).
"""
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from hermes_aux_autoheal import health


# --- probe payload shape ---------------------------------------------------

def test_compression_probe_payload_is_plain_text():
    content = health.probe_payload('some-model', task='compression')
    assert content == 'ping'
    assert isinstance(content, str)


def test_vision_probe_payload_is_multimodal():
    content = health.probe_payload('some-model', task='vision')
    assert isinstance(content, list)
    kinds = [part['type'] for part in content]
    assert kinds == ['text', 'image_url']
    assert content[1]['image_url']['url'].startswith('data:image/png;base64,')


def test_probe_payload_defaults_to_text():
    assert health.probe_payload('some-model') == 'ping'


def test_probe_payload_ignores_unknown_task():
    # Unknown tasks degrade to the text probe rather than raising.
    assert health.probe_payload('some-model', task='spellcheck') == 'ping'


# --- capability rejection is a permanent verdict ---------------------------

def test_image_capability_400_is_permanent():
    assert health.failure_kind(
        "HTTP 400 {'error': {'message': 'Model do not support image input.'}}"
    ) == 'permanent'


def test_serde_unknown_variant_is_permanent():
    assert health.failure_kind(
        "HTTP 400 unknown variant `image_url`, expected `text`"
    ) == 'permanent'


def test_text_probe_200_is_not_a_verdict():
    assert health.failure_kind('') == 'ambiguous'


def test_vision_rejection_demotes_on_first_strike():
    entry = health.apply_verdict({}, ok=False,
                                 err='HTTP 400 Model do not support image input')
    assert entry['state'] == 'down'
    assert entry['fail_streak'] == 1


def test_vision_accept_keeps_model_up():
    entry = health.apply_verdict({}, ok=True, err='')
    assert entry['state'] == 'up'


# --- probe() sends the image over the wire ---------------------------------

def test_probe_sends_image_for_vision(monkeypatch):
    seen = {}

    class Resp:
        def read(self):
            return json.dumps({'choices': [{'text': 'x'}]}).encode()

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    def fake_urlopen(req, timeout=None):
        seen['body'] = json.loads(req.data.decode())
        return Resp()

    monkeypatch.setattr('urllib.request.urlopen', fake_urlopen)

    ok, latency, err = health.probe('https://x.test/v1', 'm', 'k',
                                    task='vision', timeout=5)
    assert ok is True
    content = seen['body']['messages'][0]['content']
    assert isinstance(content, list)
    assert content[1]['type'] == 'image_url'


def test_probe_sends_plain_text_for_compression(monkeypatch):
    seen = {}

    class Resp:
        def read(self):
            return json.dumps({'choices': [{'text': 'x'}]}).encode()

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    def fake_urlopen(req, timeout=None):
        seen['body'] = json.loads(req.data.decode())
        return Resp()

    monkeypatch.setattr('urllib.request.urlopen', fake_urlopen)

    ok, _, _ = health.probe('https://x.test/v1', 'm', 'k',
                            task='compression', timeout=5)
    assert ok is True
    assert seen['body']['messages'][0]['content'] == 'ping'


# --- cache is scoped per task ----------------------------------------------

def test_cache_key_is_scoped_by_task():
    assert health.HealthCache.key('u', 'm', 'ProviderA', '') == \
        'ProviderA|u|m'
    assert health.HealthCache.key('u', 'm', 'ProviderA', 'vision') == \
        'ProviderA|vision|u|m'
    assert health.HealthCache.key('u', 'm', None, 'vision') == \
        'vision|u|m'


def test_verdict_does_not_leak_between_tasks(tmp_path):
    cache = health.HealthCache(str(tmp_path / 'h.json'), ttl=600)
    cache.record('u', 'm', {'ok': True, 'state': 'up', 'ts': 1.0},
                 'ProviderA', 'compression')
    assert cache.get('u', 'm', 'ProviderA', 'compression')['state'] == 'up'
    # Vision probe of the same model must not see the compression verdict.
    assert cache.get('u', 'm', 'ProviderA', 'vision') == {}


def test_migrate_scopes_into_running_task(tmp_path):
    cache = health.HealthCache(str(tmp_path / 'h.json'), ttl=600)
    cache.data = {'u|m': {'ok': True, 'state': 'up', 'ts': 1.0}}
    moved = cache.migrate([{'base_url': 'u', 'model': 'm',
                            'provider': 'ProviderA'}], task='vision')
    assert moved == 1
    assert cache.get('u', 'm', 'ProviderA', 'vision')['state'] == 'up'
    assert cache.get('u', 'm', 'ProviderA', 'compression') == {}
