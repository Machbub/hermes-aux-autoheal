"""Turn verified-alive candidates into an ``auxiliary.<task>`` route.

Three decisions live here.

**Ranking.** Freshly-verified models first, then cheap/fast tiers, then widest
context, then lowest latency. ``ok_now`` leads the sort key so a model inside
its grace period never lands at the head of the chain.

**Provider diversity.** A chain made only of one provider's models dies
wholesale when the provider or its key is what broke — the exact failure this
package exists to survive. So the chain takes the best candidate from each
OTHER provider first, then backfills with same-provider spares.

**Write gating.** Reordering the TAIL of the chain is not worth a config write.
Unbounded churn is what produced 57 backups and a notification every 20 minutes
in the incident that motivated this. A write happens only when the primary
changes, chain[0] changes, chain membership changes, or the timeout changes.
"""
import re

# Tier heuristics. These are substring patterns over model NAMES, and they are
# not a quality judgement about any vendor: a heavy reasoning model is excellent
# at reasoning and a poor fit for a background summariser, where its cost and
# latency reach the user as a stalled conversation.
#
# The defaults deliberately match generic size/speed descriptors rather than
# vendor brand names, so they stay useful across providers and age better. If a
# model you use is named unconventionally, override the patterns instead of
# editing this file — see set_patterns() and the --fast-pattern/--heavy-pattern
# flags.
DEFAULT_FAST = r'(mini|flash|lite|turbo|small|nano|tiny|fast|instant|\b\d{1,2}b\b)'
DEFAULT_HEAVY = r'(thinking|reason|-r1\b|\bo[13]\b|\bmax\b|ultra)'

FAST_PAT = re.compile(DEFAULT_FAST, re.I)
HEAVY_PAT = re.compile(DEFAULT_HEAVY, re.I)


def set_patterns(fast=None, heavy=None):
    """Override the tier heuristics at runtime.

    Passing None leaves that pattern at its default. Raises ``re.error`` on a
    bad pattern, which the CLI surfaces rather than silently ignoring.
    """
    global FAST_PAT, HEAVY_PAT
    if fast:
        FAST_PAT = re.compile(fast, re.I)
    if heavy:
        HEAVY_PAT = re.compile(heavy, re.I)

DEFAULT_CHAIN_DEPTH = 3
DEFAULT_CALL_TIMEOUT = 300


def tier_of(model):
    """0 = fast/cheap, 1 = unknown, 2 = heavy reasoning."""
    if HEAVY_PAT.search(model):
        return 2
    if FAST_PAT.search(model):
        return 0
    return 1


def rank(candidates):
    """Sort candidates best-first for use as a summariser."""
    return sorted(
        candidates,
        key=lambda c: (
            0 if c.get('ok_now') else 1,
            tier_of(c['model']),
            -min(c.get('context') or 0, 1_000_000),
            c.get('latency', 99.0),
        ),
    )


def pick_chain(ordered, primary, depth=DEFAULT_CHAIN_DEPTH):
    """Fallbacks that survive the failure the primary just had."""
    seen_providers = {primary['provider']}
    chain = []
    rest = [c for c in ordered if c is not primary]
    for c in rest:
        if len(chain) >= depth:
            break
        if c['provider'] not in seen_providers:
            seen_providers.add(c['provider'])
            chain.append(c)
    for c in rest:
        if len(chain) >= depth:
            break
        if c not in chain:
            chain.append(c)
    return chain


def as_entry(cand, *, timeout=DEFAULT_CALL_TIMEOUT):
    """One ``fallback_chain`` entry in the shape Hermes reads."""
    return {
        'provider': cand['provider'],
        'model': cand['model'],
        'base_url': cand['base_url'],
        'key_env': cand['key_env'],
        'api_mode': cand.get('api_mode') or 'chat_completions',
        'timeout': timeout,
    }


def build(eligible, *, chain_depth=DEFAULT_CHAIN_DEPTH,
          call_timeout=DEFAULT_CALL_TIMEOUT, min_context=None):
    """Compute the desired route. Returns None when nothing is verified alive.

    Returning None is deliberate and load-bearing: with no healthy candidate
    the caller must leave the existing config ALONE. Overwriting a route with
    an empty or guessed one turns a recoverable outage into a broken config.
    """
    pool = list(eligible)
    if min_context:
        # A model whose context window is unknown (0) is not excluded — the
        # window is metadata, and dropping unknowns would reject every model on
        # a provider that does not publish one.
        pool = [c for c in pool
                if not c.get('context') or c['context'] >= min_context]

    ordered = rank(pool)
    verified = [c for c in ordered if c.get('ok_now')]
    if not verified:
        return None

    primary = verified[0]
    chain = pick_chain(ordered, primary, chain_depth)
    return {
        'provider': primary['provider'],
        'model': primary['model'],
        'timeout': call_timeout,
        'fallback_chain': [as_entry(c, timeout=call_timeout) for c in chain],
    }


def needs_write(current, desired):
    """``(bool, reason)`` — is the on-disk route materially different?"""
    if not isinstance(current, dict):
        return True, 'task section missing'
    if (current.get('provider') != desired['provider']
            or current.get('model') != desired['model']):
        return True, 'primary changed'

    cur_chain = current.get('fallback_chain')
    if not isinstance(cur_chain, list):
        return True, 'chain missing'
    new_chain = desired['fallback_chain']
    if len(cur_chain) != len(new_chain):
        return True, 'chain length changed'

    def ident(e):
        if isinstance(e, dict):
            return (e.get('provider'), e.get('model'))
        return (None, None)

    if {ident(e) for e in cur_chain} != {ident(e) for e in new_chain}:
        return True, 'chain members changed'
    if cur_chain and ident(cur_chain[0]) != ident(new_chain[0]):
        return True, 'chain[0] changed'
    if current.get('timeout') != desired['timeout']:
        return True, 'timeout changed'
    return False, 'equivalent'


def should_notify(reason, desired):
    """Only surface changes a human can act on.

    A different primary matters. A route thinned to one entry matters — it is
    the last warning before there is nothing left to fall back to. Tail
    reordering does not; it goes to the log.
    """
    return reason == 'primary changed' or len(desired['fallback_chain']) <= 1
