"""Tests for the CHAT fallback chain (top-level ``fallback_providers``).

Different failure profile from the compression chain, so a different picker:
compression dedupes by model (three labels for one model are false diversity),
chat treats a same-model different-key spare as the FIRST choice.

The incidents this is built around (one afternoon, live install):
  - ``balance=0`` on two models of one provider  -> key/quota death
  - ``429 model quota is temporarily paused``     -> model throttled
  - Cloudflare 522 from the relay                 -> origin death

And the ordering bug caught on the FIRST live sync: tier_of() puts flash at
tier 0, so the naive reuse of the compression ranking offered a flash model
ahead of a spare key for the user's own flagship model. chat_slot_key ranks by
closeness to the primary instead.

Provider/model names are deliberately generic placeholders.
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from hermes_aux_autoheal import router

FLAGSHIP = 'flagship-model'          # tier 1, no fast/heavy marker
FLASH = 'flash-x'                    # tier 0


def _cand(provider, model, *, base_url=None, context=1_000_000, ok=True, latency=1.0):
    """A probed candidate in the live pool shape."""
    return {
        'provider': provider,
        'model': model,
        'base_url': base_url or f'https://{provider.lower()}.test/v1',
        'key_env': f'{provider.upper()}_API_KEY',
        'api_mode': 'chat_completions',
        'latency': latency,
        'lat_median': latency,
        'context': context,
        'ok_now': ok,
        'fail_streak': 0,
    }


def _entry(provider, model, base_url=None):
    """One on-disk fallback_providers entry."""
    return {
        'provider': provider,
        'model': model,
        'base_url': base_url or f'https://{provider.lower()}.test/v1',
        'key_env': f'{provider.upper()}_API_KEY',
        'api_mode': 'chat_completions',
    }


def _idents(chain):
    return [(c['provider'], c['model']) for c in chain]


# ----------------------------------------------------------- same model, new key

def test_same_model_behind_another_key_is_picked_first():
    """The key/quota death: capability-identical spare beats any downgrade.

    The flagship is tier 1; a flash model is tier 0. The compression ranking
    would offer flash first (tier 0 sorts first) — for chat that is a
    capability downgrade before a like-for-like replacement, the exact bug
    caught on the first live sync.
    """
    primary = _cand('ProvA', FLAGSHIP)
    spare_key = _cand('ProvB', FLAGSHIP)
    flash = _cand('ProvC', FLASH)
    deep = _cand('ProvD', 'vendor/deep-flash')

    chain = router.pick_chat_chain([primary, spare_key, flash, deep],
                                   ('ProvA', FLAGSHIP), 4)
    assert _idents(chain)[0] == ('ProvB', FLAGSHIP)
    assert ('ProvA', FLAGSHIP) not in _idents(chain)


def test_same_model_spares_are_capped_at_depth_halved():
    """Keys on one origin must not fill the whole chain — a dead host takes all."""
    primary = _cand('A', FLAGSHIP)
    pool = [primary,
            _cand('B', FLAGSHIP),
            _cand('C', FLAGSHIP),
            _cand('D', FLAGSHIP),
            _cand('E', FLASH)]
    chain = router.pick_chat_chain(pool, ('A', FLAGSHIP), 4)
    same = [c for c in chain if router.model_id(c) == FLAGSHIP]
    assert len(same) <= 4 // 2, f'same-model spares not capped: {_idents(chain)}'


# ------------------------------------------------------------- origin diversity

def test_a_different_origin_is_second_after_same_model_spares():
    """522 scenario: dead host kills every key on it, so the chain must leave."""
    primary = _cand('ProvA', FLAGSHIP, base_url='https://relay-a.test/v1')
    spare = _cand('ProvB', FLAGSHIP, base_url='https://relay-a.test/v1')
    other_host = _cand('ProvC', FLASH, base_url='https://api-b.test/v1')

    chain = router.pick_chat_chain([primary, spare, other_host],
                                   ('ProvA', FLAGSHIP), 4)
    origins = {(c['base_url'] or '').split('//')[-1].split('/')[0] for c in chain}
    assert 'relay-a.test' in origins and 'api-b.test' in origins


def test_chain_never_all_lands_on_one_origin():
    """All candidates on one host must not fill every slot."""
    primary = _cand('A', FLAGSHIP, base_url='https://same.test/v1')
    pool = [primary,
            _cand('B', FLAGSHIP, base_url='https://same.test/v1'),
            _cand('C', 'other-model', base_url='https://same.test/v1'),
            _cand('D', 'yet-another', base_url='https://same.test/v1')]
    chain = router.pick_chat_chain(pool, ('A', FLAGSHIP), 4)
    origins = {(c['base_url'] or '').split('//')[-1].split('/')[0] for c in chain}
    assert len(origins) == 1
    assert len(chain) <= 3  # same-model cap (2) + one backfill, host still breaks


# ------------------------------------------------------------------- stickiness

def test_latency_alone_does_not_reorder_an_occupied_chat_slot():
    """Same guard as the compression chain: blind to latency.

    Holder and challenger are tier-identical, same model-closeness, same
    context — only latency differs. A faster median must not evict the
    incumbent: ``chat_slot_key`` has no latency component, so neither of them
    outranks the other and pass 0 keeps the holder.
    """
    primary = _cand('A', 'lead-model')
    holder = _cand('B', 'model-b', latency=8.0)
    challenger = _cand('C', 'model-c', latency=0.4)   # 20x faster, still loses
    pool = [primary, challenger, holder]
    on_disk = (_entry('B', 'model-b'),)

    chain = router.pick_chat_chain(pool, ('A', 'lead-model'), 1, on_disk)
    assert _idents(chain) == [('B', 'model-b')]


def test_holder_failing_its_latest_probe_loses_the_chat_slot():
    """ok_now false must sink — a suspect entry at chain[0] costs a request."""
    primary = _cand('A', 'lead')
    holder = _cand('B', 'flaky', ok=False)
    healthy = _cand('C', 'steady')
    on_disk = (_entry('B', 'flaky'),)

    chain = router.pick_chat_chain([primary, holder, healthy],
                                   ('A', 'lead'), 1, on_disk)
    assert _idents(chain) == [('C', 'steady')]


def test_holder_that_left_the_pool_is_replaced():
    primary = _cand('A', 'lead')
    live = _cand('B', 'live-model')
    on_disk = (_entry('C', 'retired-model'),)

    chain = router.pick_chat_chain([primary, live], ('A', 'lead'), 1, on_disk)
    assert _idents(chain) == [('B', 'live-model')]


def test_primary_is_never_in_its_own_chat_chain():
    primary = _cand('B', 'model-b')
    other = _cand('C', 'model-c')
    on_disk = (_entry('B', 'model-b'), _entry('C', 'model-c'))

    chain = router.pick_chat_chain([primary, other], ('B', 'model-b'), 2, on_disk)
    assert ('B', 'model-b') not in _idents(chain)


def test_cold_build_is_unchanged_without_incumbent_chain():
    primary = _cand('A', 'lead')
    pool = [primary, _cand('B', 'b-model'), _cand('C', 'c-model')]
    assert (router.pick_chat_chain(pool, ('A', 'lead'), 2)
            == router.pick_chat_chain(pool, ('A', 'lead'), 2, ()))


def test_unknown_primary_origin_still_yields_cross_origin_spares():
    """Primary not in the pool (e.g. not probed): every host is 'cross'."""
    primary = ('ProvA', FLAGSHIP)   # tuple only — no candidate
    pool = [_cand('ProvB', FLASH),
            _cand('ProvC', 'vendor/deep-flash')]
    chain = router.pick_chat_chain(pool, primary, 2)
    assert len(chain) == 2


# ------------------------------------------------------- primary self-exclusion

@pytest.mark.parametrize('primary_spelling', [
    'lead-model',                    # exact
    'ProvA/lead-model',              # provider prefix, as a dashboard writes it
    'Lead-Model',                    # case
    'vendor/lead-model',             # aggregator vendor slug
    ' lead-model ',                  # stray whitespace
])
def test_primary_never_enters_its_own_fallback_list(primary_spelling):
    """The chain must exclude the primary however config.yaml spells it.

    ``chat_primary`` comes from ``model.provider``/``model.default``; candidates
    come from a provider listing or a SQLite table. The two disagree about
    formatting, and a raw tuple comparison meant the primary was offered as its
    own fallback — five of six real spellings leaked. A fallback list whose first
    entry is the thing that just failed protects against nothing.
    """
    pool = [_cand('ProvA', 'lead-model'),
            _cand('ProvB', 'peer-one'),
            _cand('ProvC', 'peer-two')]
    chain = router.pick_chat_chain(pool, ('ProvA', primary_spelling), 3)
    assert router.ident_of({'provider': 'ProvA', 'model': 'lead-model'}) \
        not in [router.ident_of(c) for c in chain]


@pytest.mark.parametrize('provider_spelling', ['ProvA', 'prova', 'PROVA', ' ProvA '])
def test_primary_provider_spelling_does_not_matter(provider_spelling):
    pool = [_cand('ProvA', 'lead-model'), _cand('ProvB', 'peer-one')]
    chain = router.pick_chat_chain(pool, (provider_spelling, 'lead-model'), 2)
    assert router.ident_of({'provider': 'ProvA', 'model': 'lead-model'}) \
        not in [router.ident_of(c) for c in chain]


def test_a_same_model_spare_is_still_selected_after_normalising():
    """Normalising must not swallow the pass-1 spare it exists to protect.

    ``ident_of`` collapses vendor prefixes, so a different KEY serving the same
    model must still read as a distinct candidate — otherwise the fix for the
    self-match bug would delete the "same model, different key" spare, which is
    the most valuable entry in the chain.
    """
    pool = [_cand('ProvA', 'lead-model'),
            _cand('ProvA_spare', 'lead-model'),
            _cand('ProvB', 'peer-one')]
    chain = router.pick_chat_chain(pool, ('ProvA', 'ProvA/lead-model'), 3)
    idents = [(c['provider'], c['model']) for c in chain]
    assert ('ProvA_spare', 'lead-model') in idents, idents
    assert ('ProvA', 'lead-model') not in idents


# ------------------------------------------------------- tiebreak stability

def test_merit_ties_are_broken_by_name_not_latency():
    """The churn source measured on the reference install: merit ties.

    Six of eleven live candidates shared one merit key, so the tied group fell
    through to the caller's ``rank()`` order — i.e. to latency. Every median
    crossing reshuffled the slots and every reshuffle was a config write: 151
    writes over 200 replayed ticks, 150 of them with nothing flapping at all.
    """
    a = _cand('Zeta', 'peer-one', latency=0.2)
    b = _cand('Alpha', 'peer-two', latency=30.0)
    pm, pt = 'lead', router.tier_of('lead')
    assert (router.chat_merit_key(a, pm, pt)
            == router.chat_merit_key(b, pm, pt)), 'fixture: must be merit-equal'
    assert (router.chat_slot_key(b, pm, pt)
            < router.chat_slot_key(a, pm, pt)), 'Alpha sorts first despite 150x latency'


def test_chain_is_identical_across_latency_permutations():
    """The whole chain, not just slot 0, must be latency-independent."""
    import itertools
    pool_spec = [('ProvA', FLAGSHIP), ('ProvB', FLAGSHIP), ('ProvC', FLAGSHIP),
                 ('ProvD', FLASH), ('ProvE', 'peer-one'), ('ProvF', 'peer-two')]
    seen = set()
    for perm in itertools.permutations([0.3, 1.1, 2.7, 5.5, 11.0, 22.0]):
        pool = [_cand(p, m, latency=lat)
                for (p, m), lat in zip(pool_spec, perm)]
        chain = router.pick_chat_chain(pool, ('ProvA', FLAGSHIP), 4)
        seen.add(tuple(_idents(chain)))
    assert len(seen) == 1, f'latency reordered the chain {len(seen)} ways'


def test_tiebreak_carries_nothing_mutable():
    """``fail_streak`` in the tail was tried and measured WORSE (182 writes).

    A peer wobbling through its grace period would reorder the group every tick.
    The tail may only contain fields that cannot change between ticks.
    """
    base = _cand('P', 'peer')
    pm, pt = 'lead', router.tier_of('lead')
    before = router.chat_slot_key(base, pm, pt)
    for field, value in (('fail_streak', 2), ('latency', 40.0),
                         ('lat_median', 40.0), ('ok_now', False)):
        mutated = dict(base)
        mutated[field] = value
        assert router.chat_slot_key(mutated, pm, pt) == before, \
            f'{field} leaked into the ordering key'


def test_name_tail_cannot_evict_an_incumbent():
    """``outranks_for_chat_slot`` must compare merit only.

    Using the full slot key there lets any merit-equal peer with an
    alphabetically earlier name displace the holder, defeating slot stickiness
    with a field that exists only to break ties.
    """
    holder = _cand('Zeta', 'peer-one', latency=9.0)
    challenger = _cand('Alpha', 'peer-two', latency=0.4)
    pm, pt = 'lead', router.tier_of('lead')
    assert not router.outranks_for_chat_slot(challenger, holder, pm, pt)


def test_a_genuinely_better_spare_still_evicts():
    """The guard must not become lock-in: real merit still takes the slot."""
    holder = _cand('Zeta', 'peer-one', context=32_000)
    better = _cand('Alpha', 'lead')          # identical model to the primary
    pm, pt = 'lead', router.tier_of('lead')
    assert router.outranks_for_chat_slot(better, holder, pm, pt)


# ------------------------------------------------------------------ write gating

def test_tail_reorder_does_not_require_a_write():
    """Swapping the TAIL while the head stays put is not worth a write."""
    changed, reason = router.chat_chain_needs_write(
        [_entry('B', 'x'), _entry('C', 'y'), _entry('D', 'z')],
        [_entry('B', 'x'), _entry('D', 'z'), _entry('C', 'y')])
    assert changed is False, reason


def test_chain0_change_requires_a_write():
    """Membership identical but the HEAD moved: Hermes tries chain[0] first,
    so this changes behaviour and must be written."""
    changed, reason = router.chat_chain_needs_write(
        [_entry('B', 'x'), _entry('C', 'y')],
        [_entry('C', 'y'), _entry('B', 'x')])
    assert changed is True and reason == 'chat chain[0] changed'


def test_membership_change_requires_a_write():
    changed, reason = router.chat_chain_needs_write(
        [_entry('B', 'x')], [_entry('B', 'x'), _entry('C', 'y')])
    assert changed is True and 'length' in reason


def test_missing_chain_requires_a_write():
    changed, reason = router.chat_chain_needs_write(None, [_entry('B', 'x')])
    assert changed is True and 'missing' in reason


# ---------------------------------------------------------------------- shapes

def test_as_chat_entry_shape_matches_what_hermes_reads():
    cand = _cand('ProvB', FLASH)
    e = router.as_chat_entry(cand)
    assert set(e) == {'provider', 'model', 'base_url', 'key_env', 'api_mode'}
    assert 'timeout' not in e
