"""Unit tests for discovery, health hysteresis, and route building.

Run: python -m pytest tests/ -q
"""
import json
import os
import sqlite3
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from hermes_aux_autoheal import discovery, health, router


# --------------------------------------------------------------- discovery
def test_env_var_name_matches_hermes_convention():
    assert discovery.env_var_name('Acme') == 'ACME_API_KEY'
    assert discovery.env_var_name('my provider') == 'MY_PROVIDER_API_KEY'
    assert discovery.env_var_name('kk-token.cc') == 'KK_TOKEN_CC_API_KEY'


def test_from_config_reads_models_allowlist():
    cfg = {'custom_providers': [{
        'name': 'Alpha',
        'base_url': 'https://alpha.example/v1/',
        'models': {'a-flash': {}, 'a-big': {}},
    }]}
    got = discovery.from_config(cfg)
    assert {c['model'] for c in got} == {'a-flash', 'a-big'}
    # trailing slash stripped so probe URLs don't double up
    assert all(c['base_url'] == 'https://alpha.example/v1' for c in got)
    assert all(c['key_env'] == 'ALPHA_API_KEY' for c in got)


def test_from_config_single_model_leads():
    cfg = {'custom_providers': [{
        'name': 'Beta',
        'base_url': 'https://beta.example/v1',
        'model': 'b-primary',
        'models': {'b-other': {}},
    }]}
    got = discovery.from_config(cfg)
    assert [c['model'] for c in got] == ['b-primary', 'b-other']


def test_from_config_explicit_key_env_wins():
    cfg = {'custom_providers': [{
        'name': 'Gamma',
        'base_url': 'https://g.example/v1',
        'model': 'g1',
        'key_env': 'CUSTOM_TOKEN',
    }]}
    assert discovery.from_config(cfg)[0]['key_env'] == 'CUSTOM_TOKEN'


def test_from_config_ignores_malformed_entries():
    cfg = {'custom_providers': [
        'not-a-dict',
        {'base_url': 'https://x/v1', 'model': 'no-name'},   # missing name
        {'name': 'Ok', 'base_url': 'https://ok/v1', 'model': 'm'},
    ]}
    got = discovery.from_config(cfg)
    assert [c['provider'] for c in got] == ['Ok']


def test_discover_requires_a_key():
    cfg = {'custom_providers': [
        {'name': 'HasKey', 'base_url': 'https://h/v1', 'model': 'm1'},
        {'name': 'NoKey', 'base_url': 'https://n/v1', 'model': 'm2'},
    ]}
    usable, skipped = discovery.discover(
        cfg, keys={'HASKEY_API_KEY': 'sk-1'})
    assert [c['provider'] for c in usable] == ['HasKey']
    assert usable[0]['api_key'] == 'sk-1'
    assert any('NOKEY_API_KEY' in why for _, why in skipped)


def test_discover_dedupes_same_route():
    cfg = {'custom_providers': [
        {'name': 'One', 'base_url': 'https://same/v1', 'model': 'dup'},
        {'name': 'Two', 'base_url': 'https://same/v1', 'model': 'dup'},
    ]}
    usable, _ = discovery.discover(
        cfg, keys={'ONE_API_KEY': 'k', 'TWO_API_KEY': 'k'})
    assert len(usable) == 1, 'same base_url+model is one route'
    assert usable[0]['provider'] == 'One', 'first (config) entry wins'


def test_discover_skips_missing_base_url():
    cfg = {'custom_providers': [{'name': 'NoUrl', 'model': 'm'}]}
    usable, skipped = discovery.discover(cfg, keys={'NOURL_API_KEY': 'k'})
    assert usable == []
    assert any('base_url' in why for _, why in skipped)


def test_from_sqlite_reads_active_only(tmp_path):
    db = tmp_path / 'dash.db'
    conn = sqlite3.connect(db)
    conn.execute('CREATE TABLE llm_providers (name TEXT, base_url TEXT, '
                 'model TEXT, enabled_models TEXT, is_active INTEGER)')
    conn.execute('INSERT INTO llm_providers VALUES (?,?,?,?,1)',
                 ('Live', 'https://live/v1', 'm-main',
                  json.dumps(['m-main', 'm-extra'])))
    conn.execute('INSERT INTO llm_providers VALUES (?,?,?,?,0)',
                 ('Off', 'https://off/v1', 'm-off', '[]'))
    conn.commit()
    conn.close()

    got = discovery.from_sqlite(str(db))
    assert {c['model'] for c in got} == {'m-main', 'm-extra'}
    assert all(c['provider'] == 'Live' for c in got)


def test_from_sqlite_missing_file_is_not_fatal():
    assert discovery.from_sqlite('/nonexistent/nope.db') == []


# ------------------------------------------------------------------ health
def test_failure_kind_permanent_vs_ambiguous():
    assert health.failure_kind('HTTP 503 {"code":"model_not_found"}') == 'permanent'
    assert health.failure_kind('HTTP 404 not found') == 'permanent'
    assert health.failure_kind('HTTP 401 unauthorized') == 'permanent'
    assert health.failure_kind('No available channel for model x') == 'permanent'
    # these must NOT be permanent — they are the flap-prone ones
    assert health.failure_kind('HTTP 503 upstream busy') == 'ambiguous'
    assert health.failure_kind('HTTP 429 rate limited') == 'ambiguous'
    assert health.failure_kind('timeout: read timed out') == 'ambiguous'
    assert health.failure_kind('ConnectionResetError: reset by peer') == 'ambiguous'


def test_ambiguous_failure_needs_a_streak():
    entry = {'state': 'up', 'fail_streak': 0, 'pass_streak': 5}
    after_one = health.apply_verdict(entry, False, 'timeout', demote_streak=2)
    assert after_one['state'] == 'up', 'one blip must not evict a working model'
    assert after_one['fail_streak'] == 1

    after_two = health.apply_verdict(after_one, False, 'timeout', demote_streak=2)
    assert after_two['state'] == 'down'


def test_permanent_failure_demotes_immediately():
    entry = {'state': 'up', 'fail_streak': 0, 'pass_streak': 9}
    after = health.apply_verdict(entry, False, 'HTTP 503 model_not_found',
                                 demote_streak=2)
    assert after['state'] == 'down', 'a verdict should not wait for a streak'


def test_recovery_needs_a_streak():
    entry = {'state': 'down', 'fail_streak': 3, 'pass_streak': 0}
    once = health.apply_verdict(entry, True, '', promote_streak=2)
    assert once['state'] == 'down', 'one pass is not proof of recovery'
    twice = health.apply_verdict(once, True, '', promote_streak=2)
    assert twice['state'] == 'up'


def test_first_sighting_that_works_is_up():
    assert health.apply_verdict({}, True, '')['state'] == 'up'


def test_first_sighting_that_fails_is_down():
    assert health.apply_verdict({}, False, 'timeout')['state'] == 'down'


def test_success_resets_fail_streak():
    entry = {'state': 'up', 'fail_streak': 1, 'pass_streak': 0}
    after = health.apply_verdict(entry, True, '')
    assert after['fail_streak'] == 0


def test_health_cache_roundtrip(tmp_path):
    path = tmp_path / 'health.json'
    cache = health.HealthCache(str(path), ttl=600)
    cache.record('https://x/v1', 'm', {'ok': True, 'ts': 1000, 'state': 'up'})
    assert cache.save()

    reopened = health.HealthCache(str(path), ttl=600)
    assert reopened.get('https://x/v1', 'm')['state'] == 'up'
    assert reopened.fresh('https://x/v1', 'm', now=1100)
    assert not reopened.fresh('https://x/v1', 'm', now=9999)


def test_health_cache_survives_corruption(tmp_path):
    path = tmp_path / 'health.json'
    path.write_text('{ this is not json')
    cache = health.HealthCache(str(path))
    assert cache.data == {}, 'a corrupt cache must degrade, not crash'


def test_evaluate_uses_cache_without_probing(tmp_path):
    path = tmp_path / 'h.json'
    cache = health.HealthCache(str(path), ttl=600)
    cache.record('https://a/v1', 'm1',
                 {'ok': True, 'ts': 1000, 'state': 'up', 'latency': 1.5,
                  'context': 128000, 'fail_streak': 0})
    cand = {'provider': 'A', 'model': 'm1', 'base_url': 'https://a/v1',
            'key_env': 'A_API_KEY', 'api_key': 'k'}
    eligible, rejected = health.evaluate([cand], cache, now=1010)
    assert len(eligible) == 1 and not rejected
    assert eligible[0]['latency'] == 1.5, 'no network call was made'


def test_evaluate_excludes_down_models(tmp_path):
    cache = health.HealthCache(str(tmp_path / 'h.json'), ttl=600)
    cache.record('https://d/v1', 'dead',
                 {'ok': False, 'ts': 1000, 'state': 'down',
                  'err': 'HTTP 503 model_not_found', 'latency': 0.1})
    cand = {'provider': 'D', 'model': 'dead', 'base_url': 'https://d/v1',
            'key_env': 'D_API_KEY', 'api_key': 'k'}
    eligible, rejected = health.evaluate([cand], cache, now=1010)
    assert eligible == [] and len(rejected) == 1


def test_evaluate_keeps_grace_model_but_flags_it(tmp_path):
    """A model in its grace period stays eligible with ok_now False."""
    cache = health.HealthCache(str(tmp_path / 'h.json'), ttl=600)
    cache.record('https://g/v1', 'flaky',
                 {'ok': False, 'ts': 1000, 'state': 'up', 'fail_streak': 1,
                  'err': 'timeout', 'latency': 45.0, 'context': 200000})
    cand = {'provider': 'G', 'model': 'flaky', 'base_url': 'https://g/v1',
            'key_env': 'G_API_KEY', 'api_key': 'k'}
    eligible, _ = health.evaluate([cand], cache, now=1010)
    assert len(eligible) == 1
    assert eligible[0]['ok_now'] is False


# ------------------------------------------------------------------ router
def _cand(provider, model, *, latency=1.0, context=200_000, ok_now=True,
          fail_streak=0):
    return {'provider': provider, 'model': model,
            'base_url': f'https://{provider.lower()}.example/v1',
            'key_env': f'{provider.upper()}_API_KEY',
            'latency': latency, 'context': context, 'ok_now': ok_now,
            'fail_streak': fail_streak}


def _route(*args, **kwargs):
    """``router.build`` that asserts a route was produced.

    ``build`` returns None when nothing is verified alive; every test below
    except the explicit None case wants a real route, so failing loudly here
    keeps those tests honest.
    """
    route = router.build(*args, **kwargs)
    assert route is not None, 'expected a route to be built'
    return route


def test_tier_ordering():
    assert router.tier_of('vendor/swift-mini') == 0
    assert router.tier_of('deep-reasoner-v2') == 2
    assert router.tier_of('some-unknown-model') == 1


def test_rank_puts_verified_before_grace():
    grace = _cand('A', 'a-flash', latency=0.1, ok_now=False)
    verified = _cand('B', 'b-flash', latency=9.0, ok_now=True)
    assert router.rank([grace, verified])[0] is verified


def test_rank_prefers_fast_tier_then_latency():
    slow_fast = _cand('A', 'a-flash', latency=5.0)
    quick_fast = _cand('B', 'b-mini', latency=0.5)
    heavy = _cand('C', 'c-reasoner', latency=0.1)
    ordered = router.rank([heavy, slow_fast, quick_fast])
    assert ordered[0] is quick_fast
    assert ordered[-1] is heavy, 'heavy reasoning models rank last'


def test_pick_chain_crosses_providers_first():
    primary = _cand('Same', 'p-1', latency=0.1)
    same2 = _cand('Same', 'p-2', latency=0.2)
    other = _cand('Other', 'o-1', latency=5.0)
    ordered = router.rank([primary, same2, other])
    chain = router.pick_chain(ordered, primary, 2)
    assert chain[0]['provider'] == 'Other', \
        'a same-provider chain dies with its provider'


def test_build_returns_none_when_nothing_verified():
    assert router.build([_cand('A', 'a', ok_now=False)]) is None


def test_build_picks_verified_primary_only():
    grace = _cand('A', 'a-flash', latency=0.1, ok_now=False)
    good = _cand('B', 'b-flash', latency=2.0, ok_now=True)
    route = _route([grace, good])
    assert (route['provider'], route['model']) == ('B', 'b-flash')


def test_build_respects_min_context_but_keeps_unknowns():
    small = _cand('A', 'a-flash', context=8_000)
    unknown = _cand('B', 'b-flash', context=0)
    big = _cand('C', 'c-flash', context=500_000)
    route = _route([small, unknown, big], min_context=64_000)
    models = [route['model']] + [e['model'] for e in route['fallback_chain']]
    assert 'a-flash' not in models, 'too-small window excluded'
    assert 'b-flash' in models, 'unknown window is not a reason to exclude'


def test_build_entry_shape_is_what_hermes_reads():
    route = _route([_cand('A', 'a-flash'), _cand('B', 'b-flash')],
                         call_timeout=300)
    entry = route['fallback_chain'][0]
    assert set(entry) == {'provider', 'model', 'base_url', 'key_env',
                          'api_mode', 'timeout'}
    assert entry['api_mode'] == 'chat_completions'
    assert entry['timeout'] == 300


def test_needs_write_ignores_tail_reorder():
    desired = _route([_cand('A', 'a1'), _cand('B', 'b1'),
                            _cand('C', 'c1'), _cand('D', 'd1')])
    current = {
        'provider': desired['provider'],
        'model': desired['model'],
        'timeout': desired['timeout'],
        'fallback_chain': [desired['fallback_chain'][0]]
                          + list(reversed(desired['fallback_chain'][1:])),
    }
    changed, reason = router.needs_write(current, desired)
    assert changed is False, f'tail reorder should not write ({reason})'


def test_needs_write_detects_primary_change():
    desired = _route([_cand('A', 'a1'), _cand('B', 'b1')])
    current = dict(desired, provider='Other', model='other-1')
    changed, reason = router.needs_write(current, desired)
    assert changed and reason == 'primary changed'


def test_needs_write_detects_chain_head_change():
    desired = _route([_cand('A', 'a1'), _cand('B', 'b1'), _cand('C', 'c1')])
    swapped = list(desired['fallback_chain'])
    swapped[0], swapped[1] = swapped[1], swapped[0]
    current = dict(desired, fallback_chain=swapped)
    changed, reason = router.needs_write(current, desired)
    assert changed and reason == 'chain[0] changed'


def test_needs_write_detects_member_change():
    desired = _route([_cand('A', 'a1'), _cand('B', 'b1'), _cand('C', 'c1')])
    current = dict(desired, fallback_chain=[
        dict(desired['fallback_chain'][0]),
        dict(desired['fallback_chain'][1], model='something-else'),
    ])
    changed, _ = router.needs_write(current, desired)
    assert changed


def test_needs_write_on_missing_section():
    desired = _route([_cand('A', 'a1'), _cand('B', 'b1')])
    changed, reason = router.needs_write(None, desired)
    assert changed and reason == 'task section missing'


def test_should_notify_only_on_meaningful_change():
    desired = _route([_cand('A', 'a1'), _cand('B', 'b1'), _cand('C', 'c1')])
    assert router.should_notify('primary changed', desired) is True
    assert router.should_notify('chain[0] changed', desired) is False

    thin = _route([_cand('A', 'a1'), _cand('B', 'b1')], chain_depth=1)
    assert router.should_notify('chain[0] changed', thin) is True, \
        'a nearly-empty chain is worth surfacing'
