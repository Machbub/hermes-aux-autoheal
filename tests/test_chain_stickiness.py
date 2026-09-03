"""Tests for chain slot stickiness — the third churn source.

Hysteresis (0.2.0) guarded ranking; ``choose_primary`` (0.3.0) guarded the
primary. Neither guarded the CHAIN, so two same-tier models whose medians
crossed every few ticks kept trading a fallback slot. Every trade rewrites
config.yaml, and a reorder is as much a write as a real failover.

Measured on the install this was found on: 12 writes in 6.5 hours, 9 of them
pure tail swaps between two models 2-6s apart on a probe whose noise floor is
tens of seconds. The primary was stable throughout — this was chain-only churn.

The fix is to let the on-disk chain defend its slots: a holder keeps its
position unless a challenger clears ``beats``. The tests below pin both halves
of that — the holder is defended when the difference is noise, and evicted when
the difference is real, has failed, or has left the pool.
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from hermes_aux_autoheal import router


def _cand(provider, model, median, *, context=1_000_000, ok=True, tier_model=None):
    """A probed candidate. ``median`` is what ranking actually compares."""
    return {
        'provider': provider,
        'model': tier_model or model,
        'base_url': f'https://{provider.lower()}.test/v1',
        'key_env': f'{provider.upper()}_API_KEY',
        'latency': median,
        'lat_median': median,
        'context': context,
        'ok_now': ok,
        'fail_streak': 0,
    }


def _entry(provider, model):
    """One on-disk fallback_chain entry."""
    return {
        'provider': provider,
        'model': model,
        'base_url': f'https://{provider.lower()}.test/v1',
        'key_env': f'{provider.upper()}_API_KEY',
        'api_mode': 'chat_completions',
        'timeout': 300,
    }


def _idents(chain):
    return [(c['provider'], c['model']) for c in chain]


# ------------------------------------------------------------ the actual bug

def test_noise_sized_lead_does_not_take_an_occupied_slot():
    """The production flap: 2.1s vs 6.8s, same tier, one slot, swapping forever.

    Without the guard the faster median wins the slot every time it crosses.
    With it, the holder stays until the challenger clears both margins.
    """
    primary = _cand('Relay', 'lead-model', 0.4)
    holder = _cand('Relay', 'model-b', 6.8)
    challenger = _cand('Relay', 'model-a', 5.0)   # faster, but not by 30%+0.5s
    pool = [primary, challenger, holder]
    on_disk = (_entry('Relay', 'model-b'),)

    chain = router.pick_chain(pool, primary, 1, on_disk)
    assert _idents(chain) == [('Relay', 'model-b')]


def test_a_decisive_challenger_still_takes_the_slot():
    """Stickiness must not become a lock — a wider context window is decisive."""
    primary = _cand('Relay', 'lead-model', 0.4)
    holder = _cand('Relay', 'model-b', 6.8, context=200_000)
    challenger = _cand('Relay', 'model-a', 0.5, context=1_000_000)
    pool = [primary, challenger, holder]
    on_disk = (_entry('Relay', 'model-b'),)

    chain = router.pick_chain(pool, primary, 1, on_disk)
    assert _idents(chain) == [('Relay', 'model-a')]


def test_both_directions_of_the_same_crossing_are_stable():
    """Feed the two observed medians in both orders; the holder wins both."""
    primary = _cand('Relay', 'lead-model', 0.4)
    on_disk = (_entry('Relay', 'model-b'),)

    # model-b slower this tick
    chain = router.pick_chain(
        [primary, _cand('Relay', 'model-a', 2.1), _cand('Relay', 'model-b', 6.8)],
        primary, 1, on_disk)
    assert _idents(chain) == [('Relay', 'model-b')], 'holder evicted by a 2.1 vs 6.8 gap'

    # model-b faster this tick — trivially keeps it
    chain = router.pick_chain(
        [primary, _cand('Relay', 'model-a', 6.8), _cand('Relay', 'model-b', 2.1)],
        primary, 1, on_disk)
    assert _idents(chain) == [('Relay', 'model-b')]


def test_slot_order_is_preserved_not_just_membership():
    """needs_write() fires on chain[0] changing, so order is load-bearing."""
    primary = _cand('A', 'lead', 0.1)
    pool = [primary,
            _cand('B', 'first-slot', 5.0),
            _cand('C', 'second-slot', 4.0)]   # ranks higher, must not jump ahead
    on_disk = (_entry('B', 'first-slot'), _entry('C', 'second-slot'))

    chain = router.pick_chain(pool, primary, 2, on_disk)
    assert _idents(chain) == [('B', 'first-slot'), ('C', 'second-slot')]


# ------------------------------------------------- when NOT to defend a holder

def test_holder_that_left_the_pool_is_replaced():
    """Retired or demoted upstream — defending it would keep a dead entry."""
    primary = _cand('A', 'lead', 0.1)
    pool = [primary, _cand('B', 'live-model', 3.0)]
    on_disk = (_entry('C', 'retired-model'),)

    chain = router.pick_chain(pool, primary, 1, on_disk)
    assert _idents(chain) == [('B', 'live-model')]


def test_holder_failing_its_latest_probe_loses_the_slot():
    """In grace: still eligible, but must never hold a chain slot.

    Hermes stops walking the chain at the first entry that errors, so a suspect
    entry ahead of a healthy one costs a request. Ordering comes from ``rank``,
    which sinks ``ok_now`` false to the back — this test goes through it rather
    than hand-ordering, because that sinking is half the guarantee.
    """
    primary = _cand('A', 'lead', 0.1)
    holder = _cand('B', 'flaky', 1.0, ok=False)
    healthy = _cand('C', 'steady', 9.0)
    ordered = router.rank([primary, holder, healthy])
    on_disk = (_entry('B', 'flaky'),)

    chain = router.pick_chain(ordered, primary, 1, on_disk)
    assert _idents(chain) == [('C', 'steady')]


def test_holder_promoted_to_primary_does_not_also_hold_a_slot():
    """The primary must never appear in its own chain."""
    primary = _cand('B', 'model-b', 0.5)
    pool = [primary, _cand('C', 'model-c', 3.0)]
    on_disk = (_entry('B', 'model-b'), _entry('C', 'model-c'))

    chain = router.pick_chain(pool, primary, 2, on_disk)
    assert ('B', 'model-b') not in _idents(chain)
    assert _idents(chain) == [('C', 'model-c')]


def test_two_labels_for_one_model_still_take_one_slot():
    """Rule 1 outranks stickiness: defending both would be false diversity."""
    primary = _cand('A', 'lead', 0.1)
    pool = [primary,
            _cand('R1', 'vendor/Shared-V2', 2.0),
            _cand('R2', 'shared-v2', 2.1)]
    on_disk = (_entry('R1', 'vendor/Shared-V2'), _entry('R2', 'shared-v2'))

    chain = router.pick_chain(pool, primary, 2, on_disk)
    assert len(chain) == 1


def test_a_better_tier_beats_a_holder_outright():
    """Tier is decisive on its own — it does not fluctuate like latency."""
    primary = _cand('A', 'lead', 0.1)
    holder = _cand('B', 'thinking-heavy', 1.0)      # heavy tier by name
    challenger = _cand('C', 'flash-quick', 8.0)     # fast tier, slower probe
    pool = [primary, challenger, holder]
    on_disk = (_entry('B', 'thinking-heavy'),)

    chain = router.pick_chain(pool, primary, 1, on_disk)
    assert _idents(chain) == [('C', 'flash-quick')]


def test_a_wider_context_beats_a_holder_outright():
    """Context is the other non-fluctuating input — 200k loses to 1M."""
    primary = _cand('A', 'lead', 0.1)
    holder = _cand('B', 'narrow', 0.5, context=200_000)
    challenger = _cand('C', 'wide', 9.0, context=1_000_000)
    pool = [primary, challenger, holder]
    on_disk = (_entry('B', 'narrow'),)

    chain = router.pick_chain(pool, primary, 1, on_disk)
    assert _idents(chain) == [('C', 'wide')]


def test_latency_never_evicts_a_slot_holder():
    """Even a 20x faster challenger does not reorder an occupied slot.

    This is the deliberate difference from choose_primary: the primary is worth
    optimising for speed, a fallback is worth keeping stable. 0.4s vs 8.0s at
    equal tier and context does not move it.

    Model names here are tier-neutral on purpose — ``tier_of`` reads the name,
    so calling the challenger something like 'much-faster' would match the fast
    pattern and make this a tier test instead of a latency test.
    """
    primary = _cand('A', 'lead', 0.1)
    holder = _cand('B', 'model-b', 8.0)
    challenger = _cand('C', 'model-c', 0.4)
    pool = [primary, challenger, holder]
    on_disk = (_entry('B', 'model-b'),)

    assert router.tier_of('model-b') == router.tier_of('model-c'), \
        'test names must not differ in tier or this proves nothing'
    chain = router.pick_chain(pool, primary, 1, on_disk)
    assert _idents(chain) == [('B', 'model-b')]


# ----------------------------------------------------------- shape and wiring

def test_cold_build_is_unchanged_without_an_incumbent_chain():
    """No on-disk chain (first run) must behave exactly as before."""
    primary = _cand('A', 'lead', 0.1)
    pool = [primary, _cand('B', 'b-model', 0.2), _cand('C', 'c-model', 0.3)]

    assert router.pick_chain(pool, primary, 2) == router.pick_chain(pool, primary, 2, ())


def test_cross_provider_rule_survives_stickiness():
    """Rule 2 must still hold when a same-provider spare is the incumbent."""
    primary = _cand('FastCo', 'a-model', 0.1)
    pool = [primary,
            _cand('FastCo', 'b-model', 0.2),
            _cand('OtherCo', 'c-model', 5.0)]
    on_disk = (_entry('FastCo', 'b-model'),)

    chain = router.pick_chain(pool, primary, 2, on_disk)
    assert [c['provider'] for c in chain] == ['FastCo', 'OtherCo'] or \
           [c['provider'] for c in chain] == ['OtherCo', 'FastCo']
    assert len(chain) == 2


def test_depth_is_respected_with_more_holders_than_slots():
    primary = _cand('A', 'lead', 0.1)
    pool = [primary,
            _cand('B', 'b', 1.0), _cand('C', 'c', 2.0), _cand('D', 'd', 3.0)]
    on_disk = (_entry('B', 'b'), _entry('C', 'c'), _entry('D', 'd'))

    chain = router.pick_chain(pool, primary, 2, on_disk)
    assert len(chain) == 2
    assert _idents(chain) == [('B', 'b'), ('C', 'c')]


def test_malformed_on_disk_entries_are_ignored_not_fatal():
    primary = _cand('A', 'lead', 0.1)
    pool = [primary, _cand('B', 'b-model', 1.0)]
    on_disk = ({'provider': 'B'}, {'model': 'b-model'}, {})

    chain = router.pick_chain(pool, primary, 1, on_disk)
    assert _idents(chain) == [('B', 'b-model')]


# ----------------------------------------------------------- chain_entries()

def test_chain_entries_keeps_order():
    current = {'provider': 'A', 'model': 'lead',
               'fallback_chain': [_entry('B', 'b'), _entry('C', 'c')]}
    assert [(e['provider'], e['model']) for e in router.chain_entries(current)] == \
           [('B', 'b'), ('C', 'c')]


@pytest.mark.parametrize('current', [
    None, {}, 'nonsense', {'fallback_chain': 'nonsense'},
    {'fallback_chain': None},
])
def test_chain_entries_tolerates_garbage(current):
    assert router.chain_entries(current) == ()


def test_chain_entries_drops_incomplete_entries():
    current = {'fallback_chain': [
        _entry('B', 'b'), {'provider': 'C'}, {'model': 'd'}, 'x',
    ]}
    assert [(e['provider'], e['model']) for e in router.chain_entries(current)] == \
           [('B', 'b')]


# ------------------------------------------------------------- via build()

def test_build_defends_the_chain_end_to_end():
    """The path the CLI actually takes: config -> chain_entries -> build."""
    current = {
        'provider': 'Relay', 'model': 'lead-model',
        'fallback_chain': [_entry('Relay', 'model-b')],
    }
    pool = [_cand('Relay', 'lead-model', 0.4),
            _cand('Relay', 'model-a', 5.0),
            _cand('Relay', 'model-b', 6.8)]

    desired = router.build(
        pool, chain_depth=1,
        incumbents=router.route_idents(current),
        incumbent_primary=router.primary_ident(current),
        incumbent_chain=router.chain_entries(current))

    assert [(e['provider'], e['model']) for e in desired['fallback_chain']] == \
           [('Relay', 'model-b')]


def test_build_then_needs_write_reports_no_change_on_a_noise_tick():
    """The point of the whole exercise: a noise tick must not write."""
    current = {
        'provider': 'Relay', 'model': 'lead-model', 'timeout': 300,
        'fallback_chain': [_entry('Relay', 'model-b')],
    }
    pool = [_cand('Relay', 'lead-model', 0.4),
            _cand('Relay', 'model-a', 5.0),
            _cand('Relay', 'model-b', 6.8)]

    desired = router.build(
        pool, chain_depth=1, call_timeout=300,
        incumbents=router.route_idents(current),
        incumbent_primary=router.primary_ident(current),
        incumbent_chain=router.chain_entries(current))

    changed, reason = router.needs_write(current, desired)
    assert changed is False, f'noise tick still wrote: {reason}'
