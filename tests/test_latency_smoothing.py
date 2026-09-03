"""Tests for the two guards added after hysteresis alone failed in production.

Ranking hysteresis (0.2.0) assumed probe latency wobbled by a few hundred
milliseconds. It does not: on a live aggregator the same model measured 1.3s,
6.7s and 42.0s inside twenty minutes. Thresholds cannot fix a noisy input, so:

* latency is recorded in a rolling window and ranked on its MEDIAN
* a chain takes one slot per MODEL, not per provider label
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from hermes_aux_autoheal import health, router


# --------------------------------------------------------- record_latency

def test_records_successive_samples():
    e = {}
    for v in (1.0, 2.0, 3.0):
        e = health.record_latency(e, v)
    assert e['lat_samples'] == [1.0, 2.0, 3.0]


def test_window_keeps_only_the_newest():
    e = {}
    for v in range(1, 12):
        e = health.record_latency(e, float(v), window=5)
    assert e['lat_samples'] == [7.0, 8.0, 9.0, 10.0, 11.0]


def test_does_not_mutate_the_input_entry():
    """The cache entry is reused by the caller; mutation would be a surprise."""
    e = {'lat_samples': [1.0]}
    health.record_latency(e, 2.0)
    assert e['lat_samples'] == [1.0]


def test_rounds_to_two_places():
    e = health.record_latency({}, 1.23456)
    assert e['lat_samples'] == [1.23]


def test_replaces_a_corrupt_window():
    e = health.record_latency({'lat_samples': 'not a list'}, 2.0)
    assert e['lat_samples'] == [2.0]


def test_filters_junk_out_of_an_existing_window():
    e = health.record_latency({'lat_samples': [1.0, None, 'x']}, 2.0)
    assert e['lat_samples'] == [1.0, 2.0]


def test_zero_window_records_nothing():
    e = health.record_latency({}, 2.0, window=0)
    assert e['lat_samples'] == []


# --------------------------------------------------------- median_latency

def test_median_of_odd_window():
    e = {'lat_samples': [3.0, 1.0, 2.0]}
    assert health.median_latency(e, 99.0) == 2.0


def test_median_of_even_window_averages_the_middles():
    e = {'lat_samples': [1.0, 2.0, 3.0, 4.0]}
    assert health.median_latency(e, 99.0) == 2.5


def test_single_outlier_does_not_move_the_median():
    """The production case: one 42s spike among normal samples."""
    e = {'lat_samples': [1.2, 1.3, 42.0, 1.4, 1.5]}
    assert health.median_latency(e, 99.0) == 1.4


def test_mean_would_have_moved_but_median_does_not():
    """Stated as a contrast, because it is why median was chosen."""
    samples = [1.2, 1.3, 42.0, 1.4, 1.5]
    mean = sum(samples) / len(samples)
    median = health.median_latency({'lat_samples': samples}, 99.0)
    assert median == 1.4
    assert mean > 9.0


@pytest.mark.parametrize('entry', [
    {}, None, 'nope', {'lat_samples': None}, {'lat_samples': 'x'},
    {'lat_samples': []}, {'lat_samples': [None, 'x']},
])
def test_missing_or_corrupt_window_uses_the_fallback(entry):
    assert health.median_latency(entry, 7.5) == 7.5


def test_partial_junk_still_yields_a_median():
    e = {'lat_samples': [1.0, None, 'x', 3.0]}
    assert health.median_latency(e, 99.0) == 2.0


# ------------------------------------------------------------ rank_latency

def test_rank_latency_prefers_the_median():
    cand = {'latency': 42.0, 'lat_median': 1.2}
    assert router.rank_latency(cand) == 1.2


def test_rank_latency_falls_back_to_the_raw_probe():
    assert router.rank_latency({'latency': 4.0}) == 4.0


def test_rank_latency_sentinel_when_nothing_is_known():
    assert router.rank_latency({}) == 99.0


# ------------------------------------------- ranking uses the smoothed value

def _cand(provider, model, latency, median=None, *, context=1_000_000, ok=True):
    return {
        'provider': provider, 'model': model,
        'base_url': f'https://{provider.lower()}.test/v1',
        'key_env': f'{provider.upper()}_API_KEY',
        'latency': latency,
        'lat_median': latency if median is None else median,
        'context': context, 'ok_now': ok, 'fail_streak': 0,
    }


def test_a_spiked_probe_does_not_lose_the_slot():
    """42s now, 1.2s typical — the model that is actually fast still leads."""
    spiked = _cand('P', 'steady', 42.0, 1.2)
    calm = _cand('P', 'slower', 3.0, 3.0)
    assert router.rank([calm, spiked])[0]['model'] == 'steady'


def test_a_sustained_regression_does_lose_the_slot():
    """Smoothing must not hide a real slowdown: median moved, so rank moves."""
    regressed = _cand('P', 'degraded', 40.0, 38.0)
    calm = _cand('P', 'healthy', 3.0, 3.0)
    assert router.rank([regressed, calm])[0]['model'] == 'healthy'


def test_beats_compares_medians_not_latest_probes():
    """Raw latency says the challenger wins by 39s; the medians say otherwise."""
    challenger = _cand('P', 'spiky', 1.0, 5.0)
    holder = _cand('P', 'holder', 40.0, 5.2)
    assert not router.beats(challenger, holder)


def test_sticky_latency_discounts_the_median():
    holder = _cand('P', 'holder', 42.0, 0.9)
    got = router.sticky_latency(holder, frozenset({('P', 'holder')}))
    assert got == pytest.approx(0.4)


def test_choose_primary_holds_through_a_spike():
    """End-to-end: a single 42s outlier must not rewrite the route."""
    holder = _cand('P', 'holder', 42.0, 1.0)
    rival = _cand('P', 'rival', 1.1, 1.1)
    ordered = router.rank([rival, holder], frozenset({('P', 'holder')}))
    got = router.choose_primary(ordered, ('P', 'holder'))
    assert got['model'] == 'holder'


# --------------------------------------------------------------- model_id

@pytest.mark.parametrize('model,expected', [
    ('big-model-v5', 'big-model-v5'),
    ('vendor-ai/big-model-v5', 'big-model-v5'),
    ('vendor-ai/Compact-V4-Flash', 'compact-v4-flash'),
    ('COMPACT-V4-FLASH', 'compact-v4-flash'),
    ('a/b/c-model', 'c-model'),
])
def test_model_id_strips_vendor_and_case(model, expected):
    assert router.model_id({'model': model}) == expected


def test_model_id_tolerates_a_missing_model():
    assert router.model_id({}) == ''


# -------------------------------------------------- one chain slot per model

def test_three_providers_for_one_model_take_one_slot():
    """The production case: three labels, one shared endpoint, one model."""
    primary = _cand('FastCo', 'swift-flash', 0.5)
    pool = [primary,
            _cand('ResellerA', 'big-model-v5', 3.0),
            _cand('ResellerB', 'big-model-v5', 3.1),
            _cand('ResellerC', 'big-model-v5', 3.2),
            _cand('OtherCo', 'rapid-mini', 2.0)]
    chain = router.pick_chain(pool, primary, 3)
    assert sum(1 for c in chain if c['model'] == 'big-model-v5') == 1
    assert len(chain) == 2


def test_vendor_prefixed_duplicates_also_collapse():
    primary = _cand('FastCo', 'swift-flash', 0.5)
    pool = [primary,
            _cand('ResellerA', 'vendor-ai/Compact-V4', 1.0),
            _cand('FastCo', 'compact-v4', 1.1),
            _cand('ResellerB', 'COMPACT-V4', 1.2)]
    chain = router.pick_chain(pool, primary, 3)
    assert len(chain) == 1


def test_primary_model_never_reappears_in_its_own_chain():
    primary = _cand('ResellerA', 'big-model-v5', 3.0)
    pool = [primary,
            _cand('ResellerB', 'big-model-v5', 3.1),
            _cand('FastCo', 'swift-flash', 1.0)]
    chain = router.pick_chain(pool, primary, 3)
    assert all(c['model'] != 'big-model-v5' for c in chain)
    assert [c['model'] for c in chain] == ['swift-flash']


def test_cross_provider_still_preferred_over_same_provider_spare():
    """Dedup must not undo the anti-correlation rule from 0.1.0."""
    primary = _cand('FastCo', 'a-model', 0.1)
    pool = [primary,
            _cand('FastCo', 'b-model', 0.2),
            _cand('OtherCo', 'c-model', 5.0)]
    chain = router.pick_chain(pool, primary, 2)
    assert [c['provider'] for c in chain] == ['OtherCo', 'FastCo']


def test_chain_can_still_reach_full_depth():
    primary = _cand('FastCo', 'a-model', 0.1)
    pool = [primary,
            _cand('OtherCo', 'b-model', 0.2),
            _cand('ThirdCo', 'c-model', 0.3),
            _cand('FourthCo', 'd-model', 0.4)]
    chain = router.pick_chain(pool, primary, 3)
    assert len(chain) == 3


def test_build_emits_a_deduplicated_chain():
    primary = _cand('FastCo', 'swift-flash', 0.4)
    pool = [primary,
            _cand('ResellerA', 'big-model-v5', 3.0),
            _cand('ResellerB', 'big-model-v5', 3.1),
            _cand('ResellerC', 'big-model-v5', 3.2)]
    desired = router.build(pool, chain_depth=3)
    models = [e['model'] for e in desired['fallback_chain']]
    assert models == ['big-model-v5']


def test_sibling_rotation_no_longer_triggers_a_write():
    """A route already holding one label must not be rewritten to another."""
    primary = _cand('FastCo', 'swift-flash', 0.4)
    pool = [primary,
            _cand('ResellerA', 'big-model-v5', 3.0),
            _cand('ResellerB', 'big-model-v5', 2.9),
            _cand('ResellerC', 'big-model-v5', 3.1)]
    first = router.build(pool, chain_depth=3)

    current = {
        'provider': first['provider'], 'model': first['model'],
        'timeout': first['timeout'],
        'fallback_chain': first['fallback_chain'],
    }
    # next tick: the labels reorder among themselves
    reshuffled = [primary,
                  _cand('ResellerC', 'big-model-v5', 2.8),
                  _cand('ResellerA', 'big-model-v5', 3.3),
                  _cand('ResellerB', 'big-model-v5', 3.4)]
    second = router.build(
        reshuffled, chain_depth=3,
        incumbents=router.route_idents(current),
        incumbent_primary=router.primary_ident(current))
    changed, reason = router.needs_write(current, second)
    assert changed is False, reason
