"""Tests for the overridable tier patterns.

The defaults are generic on purpose (no vendor brand names), so these check the
generic words classify correctly AND that an override actually takes effect.
"""
import os
import re
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from hermes_aux_autoheal import router


@pytest.fixture(autouse=True)
def restore_patterns():
    """Patterns are module-level globals; put them back after each test."""
    fast, heavy = router.FAST_PAT, router.HEAVY_PAT
    yield
    router.FAST_PAT, router.HEAVY_PAT = fast, heavy


@pytest.mark.parametrize('name', [
    'vendor/swift-mini', 'some-flash-2', 'chat-lite', 'turbo-v3',
    'small-instruct', 'nano-1', 'model-8b', 'fast-preview',
])
def test_generic_fast_words(name):
    assert router.tier_of(name) == 0, name


@pytest.mark.parametrize('name', [
    'deep-reasoner-v2', 'chat-thinking', 'model-r1', 'o3-preview',
    'engine-max', 'engine-ultra',
])
def test_generic_heavy_words(name):
    assert router.tier_of(name) == 2, name


@pytest.mark.parametrize('name', [
    'unknown-model-xyz', 'vendor/experimental-4', 'plain',
])
def test_unmatched_lands_in_middle(name):
    assert router.tier_of(name) == 1, name


def test_defaults_stay_vendor_neutral():
    """Guards the neutrality decision against a well-meaning future edit.

    Asserted positively — every alternative in the default patterns must be a
    generic size/speed/capability descriptor drawn from this allowlist. A
    positive check avoids naming vendors even to forbid them, and it catches a
    brand nobody thought to blocklist.
    """
    allowed = {
        'mini', 'flash', 'lite', 'turbo', 'small', 'nano', 'tiny', 'fast',
        'instant', 'thinking', 'reason', 'ultra', 'max',
        r'-r1\b', r'\bo[13]\b', r'\bmax\b', r'\b\d{1,2}b\b',
    }
    for pattern in (router.DEFAULT_FAST, router.DEFAULT_HEAVY):
        for alt in pattern.strip('()').split('|'):
            assert alt in allowed, (
                f'{alt!r} is not a generic descriptor — keep the default tier '
                f'patterns vendor-neutral, or add it to this allowlist '
                f'deliberately')


def test_set_patterns_overrides_fast():
    router.set_patterns(fast=r'my-quick')
    assert router.tier_of('my-quick-model') == 0
    assert router.tier_of('some-flash') == 1, 'override replaces, not appends'


def test_set_patterns_overrides_heavy():
    router.set_patterns(heavy=r'my-big')
    assert router.tier_of('my-big-model') == 2


def test_set_patterns_none_leaves_default():
    router.set_patterns(fast=None, heavy=None)
    assert router.tier_of('some-flash') == 0


def test_heavy_wins_when_both_match():
    """A name matching both must not be treated as cheap."""
    router.set_patterns(fast=r'combo', heavy=r'combo')
    assert router.tier_of('combo-model') == 2


def test_bad_pattern_raises():
    with pytest.raises(re.error):
        router.set_patterns(fast=r'unclosed(')


def test_override_changes_ranking_order():
    cands = [
        {'provider': 'A', 'model': 'house-special', 'base_url': 'https://a/v1',
         'key_env': 'A_API_KEY', 'latency': 5.0, 'context': 100_000,
         'ok_now': True, 'fail_streak': 0},
        {'provider': 'B', 'model': 'plain-model', 'base_url': 'https://b/v1',
         'key_env': 'B_API_KEY', 'latency': 1.0, 'context': 100_000,
         'ok_now': True, 'fail_streak': 0},
    ]
    # By default neither matches, so the faster one wins on latency.
    assert router.rank(cands)[0]['model'] == 'plain-model'

    # Declaring the slower one "fast tier" promotes it above latency.
    router.set_patterns(fast=r'house-special')
    assert router.rank(cands)[0]['model'] == 'house-special'
