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

**Ranking hysteresis.** Write gating alone does not stop churn when two equally
healthy models keep trading places on probe latency that varies tick to tick.
A model already in the route is ranked at a discounted latency, so a challenger
must win by a real margin — not by noise — before it displaces one.
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

# Ranking hysteresis. The health state machine keeps a FAILING model from
# flapping; these keep two HEALTHY models from trading places. Measured on a
# live install: 130 config writes across 245 ticks (53%), the primary bouncing
# between two models of the same tier and the same 1M context window, separated
# only by probe latency that swung a few hundred milliseconds.
#
# Two distinct churn sources need two distinct guards:
#
#   * WHICH models are in the route — ``rank`` discounts anything already in it,
#     so an outsider needs a real margin to push a member out.
#   * WHICH ONE is primary — ``choose_primary`` holds the incumbent against
#     challengers already in the route, which the ranking discount cannot do
#     (it discounts both sides of that comparison).
#
# Hysteresis applies to LATENCY only. Tier and context window are stable
# properties of a model, so a challenger that wins on either takes the slot
# immediately; there is nothing noisy to smooth out.
DEFAULT_STICKY_REL = 0.30   # challenger must be 30% faster
DEFAULT_STICKY_ABS = 0.5    # and 0.5s faster in absolute terms


def tier_of(model):
    """0 = fast/cheap, 1 = unknown, 2 = heavy reasoning."""
    if HEAVY_PAT.search(model):
        return 2
    if FAST_PAT.search(model):
        return 0
    return 1


def ident_of(cand):
    """The ``(provider, model)`` pair identifying a candidate or route entry."""
    if not isinstance(cand, dict):
        return (None, None)
    return (cand.get('provider'), cand.get('model'))


def beats(challenger, holder, *, sticky_rel=DEFAULT_STICKY_REL,
          sticky_abs=DEFAULT_STICKY_ABS):
    """Is ``challenger`` decisively better than ``holder`` for this job?

    Tier and context window are decisive on their own — they do not fluctuate.
    When both are equal the comparison falls to latency, which does fluctuate,
    so the challenger must clear BOTH margins. At small latencies a percentage
    is easy to hit by noise; at large ones a fixed number of seconds is. Neither
    alone is evidence.
    """
    c_key = (tier_of(challenger['model']), -min(challenger.get('context') or 0, 1_000_000))
    h_key = (tier_of(holder['model']), -min(holder.get('context') or 0, 1_000_000))
    if c_key != h_key:
        return c_key < h_key

    h_lat = holder.get('latency', 99.0)
    threshold = min(h_lat * (1.0 - sticky_rel), h_lat - sticky_abs)
    return challenger.get('latency', 99.0) <= threshold


def sticky_latency(cand, incumbents, *, sticky_rel=DEFAULT_STICKY_REL,
                   sticky_abs=DEFAULT_STICKY_ABS):
    """The latency a candidate is COMPARED at, not the one it measured.

    A model already in the route is credited the displacement margin (the
    tighter of the two, so both must be cleared). Everything else is compared at
    raw latency.
    """
    lat = cand.get('latency', 99.0)
    if ident_of(cand) not in incumbents:
        return lat
    return min(lat * (1.0 - sticky_rel), lat - sticky_abs)


def rank(candidates, incumbents=frozenset(), *,
         sticky_rel=DEFAULT_STICKY_REL, sticky_abs=DEFAULT_STICKY_ABS):
    """Sort candidates best-first for use as a summariser.

    ``incumbents`` is the set of ``(provider, model)`` pairs already in the
    route. They rank at a discounted latency so probe jitter cannot swap a
    member for an outsider every tick. Verification state, tier, and context
    window all outrank the discount, so a failing or ill-suited member is still
    pushed out.

    This stabilises route MEMBERSHIP. It cannot stabilise the primary slot,
    because two models already in the route are both discounted — see
    ``choose_primary``.
    """
    return sorted(
        candidates,
        key=lambda c: (
            0 if c.get('ok_now') else 1,
            tier_of(c['model']),
            -min(c.get('context') or 0, 1_000_000),
            sticky_latency(c, incumbents,
                           sticky_rel=sticky_rel, sticky_abs=sticky_abs),
        ),
    )


def choose_primary(ordered, incumbent=None, *, sticky_rel=DEFAULT_STICKY_REL,
                   sticky_abs=DEFAULT_STICKY_ABS):
    """Pick the primary, holding the incumbent unless decisively beaten.

    ``ordered`` is the output of ``rank``; ``incumbent`` is the
    ``(provider, model)`` pair currently in the primary slot, or None.

    Only verified-alive candidates are eligible: a model inside its grace
    period stays in the chain but must never be primary, because Hermes stops
    walking the chain at the first entry that errors mid-request.

    Returns None when nothing is verified alive — the caller must then leave the
    config alone rather than write a guess.
    """
    verified = [c for c in ordered if c.get('ok_now')]
    if not verified:
        return None
    best = verified[0]
    if incumbent is None:
        return best

    holder = next((c for c in verified if ident_of(c) == incumbent), None)
    if holder is None or ident_of(holder) == ident_of(best):
        return best
    if beats(best, holder, sticky_rel=sticky_rel, sticky_abs=sticky_abs):
        return best
    return holder


def route_idents(current):
    """``(provider, model)`` pairs in an existing route: primary plus chain.

    Accepts the raw ``auxiliary.<task>`` mapping (possibly None or malformed)
    and never raises — a route that cannot be read simply yields no incumbents,
    which degrades to the pre-hysteresis ranking.
    """
    if not isinstance(current, dict):
        return frozenset()
    idents = set()
    if current.get('provider') and current.get('model'):
        idents.add((current['provider'], current['model']))
    chain = current.get('fallback_chain')
    if isinstance(chain, list):
        for entry in chain:
            if isinstance(entry, dict) and entry.get('provider') and entry.get('model'):
                idents.add((entry['provider'], entry['model']))
    return frozenset(idents)


def primary_ident(current):
    """The ``(provider, model)`` currently in the primary slot, or None."""
    if not isinstance(current, dict):
        return None
    if current.get('provider') and current.get('model'):
        return (current['provider'], current['model'])
    return None


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
          call_timeout=DEFAULT_CALL_TIMEOUT, min_context=None,
          incumbents=frozenset(), incumbent_primary=None,
          sticky_rel=DEFAULT_STICKY_REL, sticky_abs=DEFAULT_STICKY_ABS):
    """Compute the desired route. Returns None when nothing is verified alive.

    Returning None is deliberate and load-bearing: with no healthy candidate
    the caller must leave the existing config ALONE. Overwriting a route with
    an empty or guessed one turns a recoverable outage into a broken config.

    ``incumbents`` stabilises which models are in the route; ``incumbent_primary``
    stabilises which one leads it. Pass both from the existing config (see
    ``route_idents`` and ``primary_ident``) or neither for a cold build.
    """
    pool = list(eligible)
    if min_context:
        # A model whose context window is unknown (0) is not excluded — the
        # window is metadata, and dropping unknowns would reject every model on
        # a provider that does not publish one.
        pool = [c for c in pool
                if not c.get('context') or c['context'] >= min_context]

    ordered = rank(pool, incumbents,
                   sticky_rel=sticky_rel, sticky_abs=sticky_abs)
    primary = choose_primary(ordered, incumbent_primary,
                             sticky_rel=sticky_rel, sticky_abs=sticky_abs)
    if primary is None:
        return None

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
