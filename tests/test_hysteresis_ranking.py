"""Tests for ranking hysteresis: an incumbent route entry resists jitter.

The health state machine (test_units.py) stops a FAILING model from flapping.
These cover the other half: two HEALTHY models must not trade places just
because probe latency moved a few hundred milliseconds.
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from hermes_aux_autoheal import router


def cand(provider, model, latency, *, context=1_000_000, ok=True):
    """A candidate in the shape health.evaluate() emits.

    Tier is classified from the model name, so a test that needs a specific
    tier says so in the name it passes (``-mini`` fast, ``-thinking`` heavy).
    """
    return {
        'provider': provider,
        'model': model,
        'base_url': f'https://{provider.lower()}.test/v1',
        'key_env': f'{provider.upper()}_API_KEY',
        'latency': latency,
        'context': context,
        'ok_now': ok,
        'fail_streak': 0,
    }


INCUMBENT = frozenset({('P', 'holder')})
HOLDER = ('P', 'holder')


def first(candidates, incumbents=INCUMBENT, **kw):
    return router.rank(candidates, incumbents, **kw)[0]['model']


def primary(candidates, incumbents=INCUMBENT, incumbent=HOLDER, **kw):
    """The model choose_primary lands on, given the ranked pool."""
    ordered = router.rank(candidates, incumbents, **kw)
    got = router.choose_primary(ordered, incumbent, **kw)
    return None if got is None else got['model']


# ------------------------------------------------- the core rule (primary slot)

def test_jitter_sized_lead_does_not_take_the_primary_slot():
    """0.6s vs 0.9s is noise on a network probe — the holder keeps the slot."""
    pair = [cand('P', 'rival', 0.6), cand('P', 'holder', 0.9)]
    assert primary(pair) == 'holder'


def test_same_pair_ranks_on_raw_latency_without_incumbency():
    """Remove the incumbency and the identical pair sorts on measured latency."""
    pair = [cand('P', 'rival', 0.6), cand('P', 'holder', 0.9)]
    assert primary(pair, frozenset(), None) == 'rival'


def test_decisive_lead_still_displaces():
    """Hysteresis must not become a lock-in: a real win still wins."""
    pair = [cand('P', 'rival', 0.2), cand('P', 'holder', 5.0)]
    assert primary(pair) == 'rival'


def test_membership_ranking_also_prefers_a_member():
    """``rank`` alone protects a route MEMBER against an outsider."""
    pair = [cand('P', 'outsider', 0.6), cand('P', 'holder', 0.9)]
    assert first(pair) == 'holder'


# ------------------------------------------------- both margins are required

def test_relative_margin_alone_is_not_enough():
    """40% faster but only 0.3s — at small latencies percentage is cheap."""
    pair = [cand('P', 'rival', 0.45), cand('P', 'holder', 0.75)]
    assert primary(pair) == 'holder'


def test_absolute_margin_alone_is_not_enough():
    """0.6s faster but only 12% — at large latencies seconds are cheap."""
    pair = [cand('P', 'rival', 4.4), cand('P', 'holder', 5.0)]
    assert primary(pair) == 'holder'


def test_both_margins_cleared_displaces():
    """2.0s vs 5.0s clears 30% and 0.5s together."""
    pair = [cand('P', 'rival', 2.0), cand('P', 'holder', 5.0)]
    assert primary(pair) == 'rival'


# ------------------------------------------- stronger signals outrank the hold

def test_verification_state_outranks_incumbency():
    """A holder that failed its latest probe must not keep the primary slot."""
    pair = [cand('P', 'rival', 9.0), cand('P', 'holder', 0.5, ok=False)]
    assert primary(pair) == 'rival'


def test_tier_outranks_incumbency():
    """A heavy incumbent loses to a fast challenger regardless of latency."""
    pair = [
        cand('P', 'rival-mini', 9.0),
        cand('P', 'holder-thinking', 0.5),
    ]
    inc = frozenset({('P', 'holder-thinking')})
    ordered = router.rank(pair, inc)
    got = router.choose_primary(ordered, ('P', 'holder-thinking'))
    assert got['model'] == 'rival-mini'


def test_context_window_outranks_incumbency():
    """A wider context window beats the hold: compression needs the room."""
    pair = [
        cand('P', 'rival', 9.0, context=1_000_000),
        cand('P', 'holder', 0.5, context=64_000),
    ]
    assert primary(pair) == 'rival'


# ------------------------------------------------------------------- beats()

def test_beats_is_false_for_a_jitter_lead():
    assert not router.beats(cand('P', 'a', 0.6), cand('P', 'b', 0.9))


def test_beats_is_true_for_a_decisive_lead():
    assert router.beats(cand('P', 'a', 0.2), cand('P', 'b', 5.0))


def test_beats_ignores_margins_when_tier_differs():
    """A better tier wins even when it is slower — tier does not fluctuate."""
    assert router.beats(cand('P', 'a-mini', 9.0), cand('P', 'b-thinking', 0.5))


def test_beats_is_false_when_tier_is_worse_however_fast():
    assert not router.beats(cand('P', 'a-thinking', 0.1), cand('P', 'b-mini', 9.0))


# ----------------------------------------------------------- choose_primary

def test_choose_primary_returns_none_when_nothing_verified():
    pool = [cand('P', 'x', 1.0, ok=False), cand('P', 'y', 2.0, ok=False)]
    assert router.choose_primary(router.rank(pool), HOLDER) is None


def test_choose_primary_skips_grace_period_models():
    """A model in grace stays in the chain but must never lead it."""
    pool = [cand('P', 'in-grace', 0.1, ok=False), cand('P', 'alive', 8.0)]
    got = router.choose_primary(router.rank(pool), None)
    assert got['model'] == 'alive'


def test_choose_primary_falls_back_when_incumbent_is_gone():
    """An incumbent that no longer probes alive cannot be held."""
    pool = [cand('P', 'newcomer', 3.0)]
    got = router.choose_primary(router.rank(pool), HOLDER)
    assert got['model'] == 'newcomer'


def test_choose_primary_with_no_incumbent_takes_the_best():
    pool = [cand('P', 'fast', 0.5), cand('P', 'slow', 9.0)]
    got = router.choose_primary(router.rank(pool), None)
    assert got['model'] == 'fast'


# ----------------------------------------------------------- sticky_latency

def test_sticky_latency_leaves_non_incumbents_alone():
    assert router.sticky_latency(cand('P', 'x', 2.0), INCUMBENT) == 2.0


def test_sticky_latency_applies_the_tighter_margin():
    """0.9s: 30% off is 0.63, minus 0.5s is 0.4 — the tighter one is used."""
    got = router.sticky_latency(cand('P', 'holder', 0.9), INCUMBENT)
    assert got == pytest.approx(0.4)


def test_sticky_latency_relative_margin_wins_at_high_latency():
    """10s: 30% off is 7.0, minus 0.5s is 9.5 — 7.0 is tighter."""
    got = router.sticky_latency(cand('P', 'holder', 10.0), INCUMBENT)
    assert got == pytest.approx(7.0)


def test_zero_margins_disable_hysteresis():
    pair = [cand('P', 'rival', 0.6), cand('P', 'holder', 0.9)]
    assert primary(pair, sticky_rel=0.0, sticky_abs=0.0) == 'rival'


def test_missing_latency_falls_back_to_sentinel():
    """A candidate with no latency key must not raise; it sorts last."""
    broken = {'provider': 'P', 'model': 'nolatency', 'context': 1_000_000,
              'ok_now': True}
    got = router.sticky_latency(broken, frozenset())
    assert got == 99.0


# ------------------------------------------------------------- route_idents

def test_route_idents_collects_primary_and_chain():
    current = {
        'provider': 'P', 'model': 'primary',
        'fallback_chain': [
            {'provider': 'Q', 'model': 'spare-1'},
            {'provider': 'R', 'model': 'spare-2'},
        ],
    }
    assert router.route_idents(current) == frozenset({
        ('P', 'primary'), ('Q', 'spare-1'), ('R', 'spare-2'),
    })


@pytest.mark.parametrize('current', [
    None,
    {},
    'not-a-mapping',
    {'provider': 'P'},                                   # no model
    {'model': 'm'},                                      # no provider
    {'provider': 'P', 'model': 'm', 'fallback_chain': 'nope'},
    {'provider': 'P', 'model': 'm', 'fallback_chain': [None, 'x', {}]},
])
def test_route_idents_never_raises_on_malformed_input(current):
    """A config we cannot read yields no incumbents, not a crash."""
    got = router.route_idents(current)
    assert isinstance(got, frozenset)


def test_route_idents_ignores_incomplete_chain_entries():
    current = {
        'provider': 'P', 'model': 'primary',
        'fallback_chain': [
            {'provider': 'Q'},                # no model — skipped
            {'model': 'spare'},               # no provider — skipped
            {'provider': 'R', 'model': 'ok'},
        ],
    }
    assert router.route_idents(current) == frozenset({
        ('P', 'primary'), ('R', 'ok'),
    })


# ----------------------------------------------------------- build() wiring

def _pool():
    return [cand('P', 'rival', 0.6), cand('P', 'holder', 0.9)]


def test_build_honours_incumbency():
    desired = router.build(_pool(), incumbents=INCUMBENT,
                           incumbent_primary=HOLDER)
    assert desired['model'] == 'holder'


def test_build_without_incumbency_picks_fastest():
    desired = router.build(_pool())
    assert desired['model'] == 'rival'


def test_build_returns_none_when_nothing_verified():
    pool = [cand('P', 'x', 1.0, ok=False)]
    assert router.build(pool, incumbents=INCUMBENT, incumbent_primary=HOLDER) is None


def test_build_then_needs_write_reports_no_change_for_jitter():
    """The end-to-end property: jitter must not produce a config write."""
    current = {
        'provider': 'P', 'model': 'holder', 'timeout': 300,
        'fallback_chain': [
            {'provider': 'P', 'model': 'rival', 'base_url': 'https://p.test/v1',
             'key_env': 'P_API_KEY', 'api_mode': 'chat_completions',
             'timeout': 300},
        ],
    }
    desired = router.build(
        _pool(), chain_depth=1,
        incumbents=router.route_idents(current),
        incumbent_primary=router.primary_ident(current))
    changed, reason = router.needs_write(current, desired)
    assert changed is False, reason


def test_primary_ident_reads_the_slot():
    assert router.primary_ident({'provider': 'P', 'model': 'm'}) == ('P', 'm')


@pytest.mark.parametrize('current', [None, {}, 'nope', {'provider': 'P'}, {'model': 'm'}])
def test_primary_ident_returns_none_on_malformed_input(current):
    assert router.primary_ident(current) is None
