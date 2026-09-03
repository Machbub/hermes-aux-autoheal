"""Tests for discovering models from a provider's own /v1/models listing.

Before this, discovery read only models pinned by hand in config.yaml. That
matched one kind of install and missed the other: a relay or gateway fronting
dozens of upstreams is configured as ONE provider entry, and nobody enumerates
sixty models by hand. Such a config produced zero candidates and the tool exited
with "no candidate models with usable API keys".
"""
import json
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from hermes_aux_autoheal import discovery


@pytest.fixture(autouse=True)
def _clear_listing_cache():
    discovery._LISTING_CACHE.clear()
    yield
    discovery._LISTING_CACHE.clear()


RELAY = {'name': 'Relay', 'base_url': 'https://relay.test/v1',
         'key_env': 'RELAY_API_KEY'}


def fake_urlopen(payload, *, status=200):
    """Stand in for urllib.request.urlopen returning one JSON body."""
    class Resp:
        def read(self):
            return json.dumps(payload).encode()

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    return lambda req, timeout=None: Resp()


def listing(*ids):
    return {'data': [{'id': i} for i in ids]}


# ------------------------------------------------------ pending_discovery

def test_provider_with_no_models_is_pending():
    config = {'custom_providers': [dict(RELAY, discover_models=True)]}
    assert [p['name'] for p in discovery.pending_discovery(config)] == ['Relay']


def test_provider_with_pinned_models_is_not_pending():
    config = {'custom_providers': [dict(RELAY, models={'a-model': {}})]}
    assert discovery.pending_discovery(config) == []


def test_provider_with_single_model_is_not_pending():
    config = {'custom_providers': [dict(RELAY, model='a-model')]}
    assert discovery.pending_discovery(config) == []


def test_empty_models_mapping_counts_as_unpinned():
    """`models: {}` says nothing, so fall through to the listing."""
    config = {'custom_providers': [dict(RELAY, models={})]}
    assert len(discovery.pending_discovery(config)) == 1


def test_provider_without_base_url_is_skipped():
    config = {'custom_providers': [{'name': 'Broken'}]}
    assert discovery.pending_discovery(config) == []


@pytest.mark.parametrize('config', [
    {}, {'custom_providers': None}, {'custom_providers': []},
    {'custom_providers': ['not a dict']}, {'custom_providers': [{}]},
])
def test_pending_discovery_tolerates_junk(config):
    assert discovery.pending_discovery(config) == []


# ------------------------------------------------------------ list_models

def test_reads_openai_shaped_listing(monkeypatch):
    monkeypatch.setattr(discovery.urllib.request, 'urlopen',
                        fake_urlopen(listing('a-model', 'b-model')))
    ids, err = discovery.list_models('https://relay.test/v1', 'k')
    assert ids == ['a-model', 'b-model']
    assert err == ''


def test_reads_bare_list_shape(monkeypatch):
    """Some gateways return a plain array instead of {'data': [...]}."""
    monkeypatch.setattr(discovery.urllib.request, 'urlopen',
                        fake_urlopen([{'id': 'a-model'}]))
    ids, err = discovery.list_models('https://relay.test/v1', 'k')
    assert ids == ['a-model']
    assert err == ''


def test_deduplicates_ids(monkeypatch):
    monkeypatch.setattr(discovery.urllib.request, 'urlopen',
                        fake_urlopen(listing('a-model', 'a-model', 'b-model')))
    ids, _ = discovery.list_models('https://relay.test/v1', 'k')
    assert ids == ['a-model', 'b-model']


def test_caches_per_base_url(monkeypatch):
    """Sibling providers on one relay must not each fetch the listing."""
    calls = []

    def counting(req, timeout=None):
        calls.append(req.full_url)
        return fake_urlopen(listing('a-model'))(req, timeout)

    monkeypatch.setattr(discovery.urllib.request, 'urlopen', counting)
    discovery.list_models('https://relay.test/v1', 'k')
    discovery.list_models('https://relay.test/v1', 'k')
    assert len(calls) == 1


def test_http_error_is_returned_not_raised(monkeypatch):
    import urllib.error

    def boom(req, timeout=None):
        raise urllib.error.HTTPError(req.full_url, 401, 'Unauthorized', {}, None)

    monkeypatch.setattr(discovery.urllib.request, 'urlopen', boom)
    ids, err = discovery.list_models('https://relay.test/v1', 'k')
    assert ids == []
    assert '401' in err


def test_connection_error_is_returned_not_raised(monkeypatch):
    import urllib.error

    def boom(req, timeout=None):
        raise urllib.error.URLError('connection refused')

    monkeypatch.setattr(discovery.urllib.request, 'urlopen', boom)
    ids, err = discovery.list_models('https://relay.test/v1', 'k')
    assert ids == []
    assert 'URLError' in err


def test_non_json_body_is_reported(monkeypatch):
    class Resp:
        def read(self):
            return b'<html>nope</html>'

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    monkeypatch.setattr(discovery.urllib.request, 'urlopen',
                        lambda req, timeout=None: Resp())
    ids, err = discovery.list_models('https://relay.test/v1', 'k')
    assert ids == []
    assert 'JSON' in err


def test_empty_listing_is_reported(monkeypatch):
    monkeypatch.setattr(discovery.urllib.request, 'urlopen',
                        fake_urlopen({'data': []}))
    ids, err = discovery.list_models('https://relay.test/v1', 'k')
    assert ids == []
    assert err


# ----------------------------------------------------------- is_chat_model

@pytest.mark.parametrize('mid', [
    'claude-sonnet-4-6', 'gpt-5.2-codex', 'gemini-3-flash', 'glm-5-air',
    'deepseek-v4', 'qwen3-coder-plus', 'some-30b-instruct',
])
def test_chat_models_pass(mid):
    assert discovery.is_chat_model(mid)


@pytest.mark.parametrize('mid', [
    'text-embedding-3-large', 'bge-m3', 'voyage-3', 'jina-reranker-v2',
    'whisper-large-v3', 'tts-1-hd', 'eleven-multilingual-v2',
    'dall-e-3', 'flux-1.1-pro', 'stable-diffusion-xl', 'imagen-4',
    'veo-3', 'sora-2', 'omni-moderation-latest', 'llama-guard-4',
])
def test_non_chat_models_are_filtered(mid):
    assert not discovery.is_chat_model(mid)


def test_missing_id_is_not_a_chat_model_crash():
    assert discovery.is_chat_model(None) is True


# ----------------------------------------------------------- from_endpoint

def test_builds_candidates_from_listing(monkeypatch):
    monkeypatch.setattr(discovery.urllib.request, 'urlopen',
                        fake_urlopen(listing('a-model', 'b-model')))
    cands, skips = discovery.from_endpoint(RELAY, 'k')
    assert [c['model'] for c in cands] == ['a-model', 'b-model']
    assert all(c['provider'] == 'Relay' for c in cands)
    assert all(c['key_env'] == 'RELAY_API_KEY' for c in cands)
    assert all(c['base_url'] == 'https://relay.test/v1' for c in cands)
    assert skips == []


def test_non_chat_models_are_reported_as_skips(monkeypatch):
    monkeypatch.setattr(discovery.urllib.request, 'urlopen',
                        fake_urlopen(listing('a-model', 'tts-1', 'bge-m3')))
    cands, skips = discovery.from_endpoint(RELAY, 'k')
    assert [c['model'] for c in cands] == ['a-model']
    assert len(skips) == 2
    assert all('not a chat model' in why for _, why in skips)


def test_max_models_caps_the_listing(monkeypatch):
    monkeypatch.setattr(discovery.urllib.request, 'urlopen',
                        fake_urlopen(listing(*[f'm{i}' for i in range(50)])))
    cands, skips = discovery.from_endpoint(RELAY, 'k', max_models=10)
    assert len(cands) == 10
    assert len(skips) == 40
    assert all('over --max-discovered' in why for _, why in skips)


def test_listing_failure_produces_one_explanatory_skip(monkeypatch):
    import urllib.error

    def boom(req, timeout=None):
        raise urllib.error.HTTPError(req.full_url, 403, 'Forbidden', {}, None)

    monkeypatch.setattr(discovery.urllib.request, 'urlopen', boom)
    cands, skips = discovery.from_endpoint(RELAY, 'k')
    assert cands == []
    assert len(skips) == 1
    assert '403' in skips[0][1]


def test_api_mode_is_carried_through(monkeypatch):
    monkeypatch.setattr(discovery.urllib.request, 'urlopen',
                        fake_urlopen(listing('a-model')))
    provider = dict(RELAY, api_mode='responses')
    cands, _ = discovery.from_endpoint(provider, 'k')
    assert cands[0]['api_mode'] == 'responses'


# ---------------------------------------------------- discover integration

RELAY_CONFIG = {'custom_providers': [dict(RELAY, discover_models=True)]}
KEYS = {'RELAY_API_KEY': 'secret'}


def test_relay_only_config_yields_candidates(monkeypatch):
    """The regression this whole module exists for: previously zero."""
    monkeypatch.setattr(discovery.urllib.request, 'urlopen',
                        fake_urlopen(listing('a-model', 'b-model')))
    usable, _ = discovery.discover(RELAY_CONFIG, keys=KEYS)
    assert [c['model'] for c in usable] == ['a-model', 'b-model']
    assert all(c['api_key'] == 'secret' for c in usable)


def test_no_discover_models_keeps_old_behaviour(monkeypatch):
    def boom(req, timeout=None):
        raise AssertionError('listing must not be fetched')

    monkeypatch.setattr(discovery.urllib.request, 'urlopen', boom)
    usable, _ = discovery.discover(RELAY_CONFIG, keys=KEYS,
                                   discover_models=False)
    assert usable == []


def test_missing_key_skips_without_contacting_the_relay(monkeypatch):
    """No point asking a gateway for its catalogue with no credentials."""
    def boom(req, timeout=None):
        raise AssertionError('must not contact the relay without a key')

    monkeypatch.setattr(discovery.urllib.request, 'urlopen', boom)
    usable, skipped = discovery.discover(RELAY_CONFIG, keys={})
    assert usable == []
    assert any('RELAY_API_KEY' in why for _, why in skipped)


def test_pinned_models_are_not_re_listed(monkeypatch):
    def boom(req, timeout=None):
        raise AssertionError('a pinned provider must not be listed')

    monkeypatch.setattr(discovery.urllib.request, 'urlopen', boom)
    config = {'custom_providers': [dict(RELAY, models={'pinned-model': {}})]}
    usable, _ = discovery.discover(config, keys=KEYS)
    assert [c['model'] for c in usable] == ['pinned-model']


def test_pinned_metadata_wins_over_discovered_duplicate(monkeypatch):
    """Same model from both sources: keep the user's explicit api_mode."""
    monkeypatch.setattr(discovery.urllib.request, 'urlopen',
                        fake_urlopen(listing('shared-model')))
    config = {'custom_providers': [
        dict(RELAY, name='Pinned', models={'shared-model': {}},
             api_mode='responses'),
        dict(RELAY, name='Listed'),
    ]}
    usable, _ = discovery.discover(config, keys=KEYS)
    # one base_url + one model = one route, and the pinned entry came first
    assert len(usable) == 1
    assert usable[0]['api_mode'] == 'responses'
    assert usable[0]['provider'] == 'Pinned'


def test_mixed_config_gets_both_sources(monkeypatch):
    monkeypatch.setattr(discovery.urllib.request, 'urlopen',
                        fake_urlopen(listing('listed-model')))
    config = {'custom_providers': [
        {'name': 'Direct', 'base_url': 'https://direct.test/v1',
         'key_env': 'DIRECT_API_KEY', 'model': 'direct-model'},
        dict(RELAY),
    ]}
    keys = {'DIRECT_API_KEY': 'd', 'RELAY_API_KEY': 'r'}
    usable, _ = discovery.discover(config, keys=keys)
    assert sorted(c['model'] for c in usable) == ['direct-model', 'listed-model']


def test_non_chat_models_do_not_reach_candidates(monkeypatch):
    monkeypatch.setattr(discovery.urllib.request, 'urlopen',
                        fake_urlopen(listing('a-model', 'tts-1', 'bge-m3')))
    usable, skipped = discovery.discover(RELAY_CONFIG, keys=KEYS)
    assert [c['model'] for c in usable] == ['a-model']
    assert len(skipped) == 2
