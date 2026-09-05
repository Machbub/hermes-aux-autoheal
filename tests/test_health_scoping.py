"""Tests for provider-scoped health-cache keys.

Several providers can share one base_url — an aggregator fronted by different
keys and quotas. Keying the cache on base_url|model alone makes them collide,
so one sibling's verdict is read back as every sibling's.
"""
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from hermes_aux_autoheal import health

AGG = 'https://agg.example/v1'


def cand(provider, model='shared-model', base_url=AGG):
    return {'provider': provider, 'model': model, 'base_url': base_url,
            'api_key': 'k', 'key_env': f'{provider.upper()}_API_KEY'}


def _cache(tmp_path, data=None):
    path = str(tmp_path / 'health.json')
    if data is not None:
        with open(path, 'w') as f:
            json.dump(data, f)
    return health.HealthCache(path, ttl=600)


# --- keying --------------------------------------------------------------

def test_siblings_on_one_base_url_get_distinct_keys():
    keys = {health.HealthCache.key(AGG, 'shared-model', p)
            for p in ('ProviderA', 'ProviderB', 'ProviderC')}
    assert len(keys) == 3


def test_key_carries_provider_name():
    assert health.HealthCache.key(AGG, 'm', 'ProviderA').startswith('ProviderA|')


def test_key_without_provider_is_the_legacy_shape():
    assert health.HealthCache.key(AGG, 'm') == f'{AGG}|m'


def test_sibling_verdict_does_not_leak(tmp_path):
    cache = _cache(tmp_path)
    cache.record(AGG, 'shared-model', {'ok': True, 'state': 'up', 'ts': 1.0},
                 'ProviderA')
    assert cache.get(AGG, 'shared-model', 'ProviderB') == {}
    assert cache.get(AGG, 'shared-model', 'ProviderA')['state'] == 'up'


def test_fresh_is_scoped_too(tmp_path):
    cache = _cache(tmp_path)
    cache.record(AGG, 'm', {'ok': True, 'ts': 1000.0}, 'ProviderA')
    assert cache.fresh(AGG, 'm', provider='ProviderA', now=1100.0) is True
    assert cache.fresh(AGG, 'm', provider='ProviderB', now=1100.0) is False


# --- migration -----------------------------------------------------------

def test_migration_fans_legacy_entry_out_to_every_sibling(tmp_path):
    legacy = {f'{AGG}|shared-model': {'ok': True, 'state': 'up',
                                      'pass_streak': 2, 'ts': 5.0}}
    cache = _cache(tmp_path, legacy)
    cands = [cand('ProviderA'), cand('ProviderB'), cand('ProviderC')]

    assert cache.migrate(cands) == 3
    for c in cands:
        entry = cache.get(AGG, 'shared-model', c['provider'])
        assert entry['pass_streak'] == 2, c['provider']


def test_migration_drops_legacy_key_once_siblings_exist(tmp_path):
    cache = _cache(tmp_path, {f'{AGG}|m': {'ok': True, 'ts': 1.0}})
    cache.migrate([cand('ProviderA', 'm')])
    assert f'{AGG}|m' not in cache.data


def test_migrated_entries_are_independent_copies(tmp_path):
    cache = _cache(tmp_path, {f'{AGG}|m': {'state': 'up', 'ts': 1.0}})
    cands = [cand('ProviderA', 'm'), cand('ProviderB', 'm')]
    cache.migrate(cands)

    cache.data[health.HealthCache.key(AGG, 'm', 'ProviderA')]['state'] = 'down'
    assert cache.get(AGG, 'm', 'ProviderB')['state'] == 'up'


def test_migration_is_idempotent(tmp_path):
    cache = _cache(tmp_path, {f'{AGG}|m': {'ok': True, 'ts': 1.0}})
    cands = [cand('ProviderA', 'm'), cand('ProviderB', 'm')]
    first = cache.migrate(cands)
    second = cache.migrate(cands)
    assert (first, second) == (2, 0)
    assert len(cache.data) == 2


def test_migration_on_empty_cache_is_a_noop(tmp_path):
    cache = _cache(tmp_path)
    assert cache.migrate([cand('ProviderA')]) == 0
    assert cache.data == {}


def test_migration_leaves_unrelated_legacy_keys_alone(tmp_path):
    other = {'https://elsewhere.example/v1|x': {'ok': True}}
    cache = _cache(tmp_path, other)
    cache.migrate([cand('ProviderA')])
    assert cache.data['https://elsewhere.example/v1|x'] == {'ok': True}


def test_migration_preserves_an_already_scoped_entry(tmp_path):
    scoped = {health.HealthCache.key(AGG, 'm', 'ProviderA'):
              {'state': 'down', 'ts': 9.0}}
    cache = _cache(tmp_path, scoped)
    cache.migrate([cand('ProviderA', 'm'), cand('ProviderB', 'm')])
    assert cache.get(AGG, 'm', 'ProviderA')['state'] == 'down'
    assert cache.get(AGG, 'm', 'ProviderB') == {}


def test_candidate_without_provider_still_works(tmp_path):
    """Discovery sources may omit the name; the legacy key must still resolve."""
    cache = _cache(tmp_path, {f'{AGG}|m': {'ok': True, 'ts': 1.0}})
    nameless = {'base_url': AGG, 'model': 'm', 'api_key': 'k'}
    assert cache.migrate([nameless]) == 0
    assert cache.get(AGG, 'm')['ok'] is True


# --- end-to-end through evaluate() --------------------------------------

def test_evaluate_probes_each_sibling_separately(tmp_path, monkeypatch):
    """One dead key among siblings must not condemn the others."""
    calls = []

    def fake_probe(base_url, model, api_key, timeout=None, task='compression'):
        calls.append((base_url, model, api_key))
        # ProviderB's key is the broken one.
        if api_key == 'dead':
            return False, 0.1, 'HTTP 401 invalid key'
        return True, 0.5, ''

    monkeypatch.setattr(health, 'probe', fake_probe)

    a = cand('ProviderA'); a['api_key'] = 'good'
    b = cand('ProviderB'); b['api_key'] = 'dead'
    c = cand('ProviderC'); c['api_key'] = 'good'

    cache = _cache(tmp_path)
    eligible, rejected = health.evaluate([a, b, c], cache, timeout=5)

    assert len(calls) == 3, 'each sibling must be probed, not read from a twin'
    names = {e['provider'] for e in eligible}
    assert names == {'ProviderA', 'ProviderC'}
    assert [c['provider'] for c, _ in rejected] == ['ProviderB']
