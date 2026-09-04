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
DEFAULT_CHAT_CHAIN_DEPTH = 4

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
# Both act on the MEDIAN of a probe window, not the latest sample. Latency on an
# aggregator swings by an order of magnitude tick to tick (measured: 1.3s, 6.7s,
# 42.0s for one model inside twenty minutes), and no threshold on a noisy input
# produces a stable output. See ``health.record_latency``.
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
    """The ``(provider, model)`` pair identifying a candidate or route entry.

    NORMALISED, because the two sides of every comparison come from different
    places and disagree about formatting. ``config.yaml`` is hand-edited or
    written by a dashboard; candidates come from a provider listing or a SQLite
    table. Comparing the raw pairs let the chat primary slip into its own
    fallback list on any cosmetic difference — five of six real-world spellings
    leaked, not just the ``provider/model`` form that was reported:

    * ``'bai/flagship-v2'`` vs ``'flagship-v2'``  (provider prefix in model)
    * ``'BAI'`` vs ``'bai'``                      (case)
    * ``'Flagship-V2'`` vs ``'flagship-v2'``      (case)
    * ``'bai '`` vs ``'bai'``                     (stray whitespace)
    * ``'vendor/flagship-v2'`` vs ``'flagship-v2'`` (aggregator vendor slug)

    So: strip, lowercase, and reduce the model to its bare name — the same
    reduction :func:`model_id` performs, kept consistent on purpose. A vendor or
    provider prefix identifies who resells the model, never which model it is.
    """
    if not isinstance(cand, dict):
        return (None, None)
    provider = cand.get('provider')
    model = cand.get('model')
    return (_norm_provider(provider), _norm_model(model))


def _norm_provider(name):
    """Provider label, comparison-ready. ``None`` stays ``None``."""
    if name is None:
        return None
    return str(name).strip().lower()


def _norm_model(name):
    """Model identity, comparison-ready: bare name, no vendor prefix, lowercase.

    ``None`` stays ``None`` so a missing field never compares equal to a present
    one.
    """
    if name is None:
        return None
    return str(name).strip().rsplit('/', 1)[-1].lower()


def rank_latency(cand):
    """The latency a candidate is RANKED on: the median of its probe window.

    Falls back to the latest raw probe when no window has been recorded (first
    ever tick, or a caller that does not track one).
    """
    return cand.get('lat_median', cand.get('latency', 99.0))


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

    h_lat = rank_latency(holder)
    threshold = min(h_lat * (1.0 - sticky_rel), h_lat - sticky_abs)
    return rank_latency(challenger) <= threshold


def _norm_ident(pair):
    """Normalise a bare ``(provider, model)`` tuple the way :func:`ident_of` does.

    Callers hand these in from config, from a test, or from another tool, and
    they are compared against candidate identities. Normalising on receipt means
    a caller cannot silently lose incumbency by spelling the pair differently —
    a miss here is invisible: nothing errors, the incumbent is simply never
    recognised and stickiness stops working.
    """
    if not pair or len(pair) != 2:
        return pair
    return (_norm_provider(pair[0]), _norm_model(pair[1]))


def sticky_latency(cand, incumbents, *, sticky_rel=DEFAULT_STICKY_REL,
                   sticky_abs=DEFAULT_STICKY_ABS):
    """The latency a candidate is COMPARED at, not the one it measured.

    A model already in the route is credited the displacement margin (the
    tighter of the two, so both must be cleared). Everything else is compared at
    its raw ranking latency.

    ``incumbents`` is normalised on receipt: it usually comes from
    :func:`route_idents`, but a caller may build it by hand, and an
    un-normalised pair would just never match.
    """
    lat = rank_latency(cand)
    if ident_of(cand) not in {_norm_ident(i) for i in incumbents}:
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
    incumbents = {_norm_ident(i) for i in incumbents}
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

    ``incumbent`` is normalised on receipt for the same reason as
    :func:`sticky_latency`: an un-normalised pair silently fails to match and the
    incumbent primary is never defended.
    """
    verified = [c for c in ordered if c.get('ok_now')]
    if not verified:
        return None
    best = verified[0]
    if incumbent is None:
        return best
    incumbent = _norm_ident(incumbent)

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

    Pairs are normalised through :func:`ident_of` because they are compared
    against candidate identities, and the two sides are spelled by different
    systems. An un-normalised pair here silently disables latency stickiness:
    every lookup misses, so no incumbent is ever recognised.
    """
    if not isinstance(current, dict):
        return frozenset()
    idents = set()
    if current.get('provider') and current.get('model'):
        idents.add(ident_of(current))
    chain = current.get('fallback_chain')
    if isinstance(chain, list):
        for entry in chain:
            if isinstance(entry, dict) and entry.get('provider') and entry.get('model'):
                idents.add(ident_of(entry))
    return frozenset(idents)


def primary_ident(current):
    """The ``(provider, model)`` currently in the primary slot, or None.

    Normalised, for the same reason as :func:`route_idents`: this is compared
    against candidates in :func:`choose_primary`, and a miss there means the
    incumbent primary is never defended.
    """
    if not isinstance(current, dict):
        return None
    if current.get('provider') and current.get('model'):
        return ident_of(current)
    return None


def chain_entries(current):
    """The existing ``fallback_chain`` entries, in order, as a tuple.

    Order matters here where ``route_idents`` deliberately discards it: this is
    what ``pick_chain`` defends slot by slot. Malformed entries are dropped
    rather than raising — an unreadable chain degrades to a cold pick, not a
    crash.
    """
    if not isinstance(current, dict):
        return ()
    chain = current.get('fallback_chain')
    if not isinstance(chain, list):
        return ()
    return tuple(
        e for e in chain
        if isinstance(e, dict) and e.get('provider') and e.get('model')
    )


def model_id(cand):
    """Identity of the MODEL behind a candidate, ignoring who resells it.

    Compared on the bare name, so ``vendor/model`` from one aggregator and
    ``model`` from another collapse to the same identity. Shares
    :func:`_norm_model` with :func:`ident_of` so the two can never drift apart.
    """
    return _norm_model(cand.get('model')) or ''


def outranks_for_slot(challenger, holder):
    """Does ``challenger`` deserve ``holder``'s CHAIN slot?

    Deliberately stricter than :func:`beats`, and deliberately blind to latency.

    A fallback is not there to be fast, it is there to answer when the primary
    stops answering. Tier and context window decide whether it can do that job;
    probe latency does not, and on a busy relay it is the one input too noisy to
    act on — the same model measured 1.3s, 6.7s and 42.0s inside twenty minutes.
    Ranking chain slots on that number is what produced 9 pure reorder writes in
    6.5 hours between two models that were tier-identical and context-identical.

    Measured over 99 ticks of that pair: defending on latency with the standard
    margins still permitted 6 swaps (and 0 only at an absurd 5s absolute
    margin, which would also block real failovers). Defending on tier and
    context alone permitted 0, while still yielding instantly to a genuinely
    better-suited model.

    Latency keeps its job — it orders candidates competing for an EMPTY slot.
    It just no longer evicts an incumbent from an occupied one.
    """
    c_key = (tier_of(challenger['model']),
             -min(challenger.get('context') or 0, 1_000_000))
    h_key = (tier_of(holder['model']),
             -min(holder.get('context') or 0, 1_000_000))
    return c_key < h_key


def pick_chain(ordered, primary, depth=DEFAULT_CHAIN_DEPTH, incumbent_chain=()):
    """Fallbacks that survive the failure the primary just had.

    Three rules. The first two are about not putting the same failure in the
    chain twice; the third is about not rewriting the config to say the same
    thing in a different order.

    1. **One slot per MODEL.** Several providers may resell the identical model
       under different labels — one shared endpoint, different keys and quotas.
       Treating those as distinct fallbacks is false diversity: if the model is
       retired upstream, every label dies in the same instant. It also churns the
       config; on one install 24 of 139 writes were nothing but three labels for
       one model rotating through the same slots.
    2. **Cross provider before backfilling.** A chain made only of one
       provider's models dies wholesale when the provider or its key is what
       broke — the orphaned-provider case this tool exists to survive.
    3. **A slot holder keeps its slot unless out-ranked on tier or context.**
       ``choose_primary`` guards the primary; the chain had no equivalent guard,
       so latency noise alone could evict a healthy fallback — and a reorder is
       a config write like any other. Pass ``incumbent_chain`` (the chain
       currently on disk, in order) to defend it. The bar is
       :func:`outranks_for_slot`, not :func:`beats`: see there for why latency
       is excluded.

    A holder is not defended when it has left ``ordered`` (retired, down,
    demoted) or when it failed its latest probe — ``ok_now`` false must sink to
    the back of the chain, never sit at ``chain[0]``, because Hermes stops
    walking the chain at the first entry that errors mid-request.
    """
    seen_providers = {primary['provider']}
    seen_models = {model_id(primary)}
    chain = []
    rest = [c for c in ordered if c is not primary]

    # pass 0: defend the chain already on disk (see rule 3)
    if incumbent_chain:
        by_ident = {ident_of(c): c for c in rest}
        held = {ident_of(e) for e in incumbent_chain}
        for entry in incumbent_chain:
            if len(chain) >= depth:
                break
            holder = by_ident.get(ident_of(entry))
            if holder is None:
                continue                        # left the pool: retired/down
            if not holder.get('ok_now', True):
                continue                        # in grace: must not hold a slot
            if model_id(holder) in seen_models:
                continue                        # primary or an earlier slot covers it
            # Another slot holder is not a challenger — it already has a slot,
            # so preferring it here would be pure reordering, the exact churn
            # rule 3 exists to stop.
            if any(outranks_for_slot(c, holder) for c in rest
                   if ident_of(c) not in held
                   and model_id(c) not in seen_models
                   and c.get('ok_now', True)):
                continue
            seen_providers.add(holder['provider'])
            seen_models.add(model_id(holder))
            chain.append(holder)

    # pass 1: a new provider AND a new model
    for c in rest:
        if len(chain) >= depth:
            break
        if c['provider'] in seen_providers or model_id(c) in seen_models:
            continue
        seen_providers.add(c['provider'])
        seen_models.add(model_id(c))
        chain.append(c)

    # pass 2: backfill with same-provider spares, still one slot per model
    for c in rest:
        if len(chain) >= depth:
            break
        if model_id(c) in seen_models:
            continue
        seen_models.add(model_id(c))
        chain.append(c)

    return chain


def chat_merit_key(cand, primary_model, primary_tier):
    """What makes one chat spare genuinely BETTER than another.

    Split out from :func:`chat_slot_key` because the two questions are different,
    and conflating them broke slot stickiness once already:

    * "which order should the spares be in?"    -> needs a total order
    * "may this challenger evict that holder?"  -> must compare merit only

    Only these three components are merit. Rank by CLOSENESS to what the user
    actually chose:

    1. the identical model behind another key — same capability, no surprise
    2. tier distance from the primary — a peer before a downgrade
    3. widest context window

    Latency is absent on purpose, same reason as :func:`outranks_for_slot`.
    """
    return (0 if model_id(cand) == primary_model else 1,
            abs(tier_of(cand['model']) - primary_tier),
            -min(cand.get('context') or 0, 1_000_000))


def chat_slot_key(cand, primary_model, primary_tier):
    """Total ordering key for a CHAT fallback slot: merit, then a stable tail.

    ``tier_of`` exists for compression, where a cheap fast model is genuinely the
    better pick — tier 0 (flash/mini/lite) sorts first. Applying that to the chat
    chain is backwards, and it showed up immediately on the live install: the
    first sync put a cheap flash model ahead of a spare key for the user's own
    flagship model, because flash is tier 0 and the flagship is tier 2. For chat
    that is a capability downgrade offered before a like-for-like replacement.
    Hence :func:`chat_merit_key`.

    The ``(provider, model)`` tail is NOT cosmetic. Merit alone leaves large tied
    groups — on the reference install 6 of 11 candidates shared one merit key —
    and a tie falls through to the caller's ``rank()`` order, which is
    latency-driven. Every median crossing inside a tied group then reshuffles
    which peer holds which slot, and a reorder is a config write. Replaying the
    real pool over 200 ticks: **151 writes without the tail, 2 with it**; with
    nothing flapping at all, 150 of those writes were pure churn.

    The tail must be IMMUTABLE. ``fail_streak`` was tried and measured worse than
    nothing: a peer wobbling 0/1/2 through its grace period reorders the group
    every tick — 182 writes over the same 200 ticks. Health is already handled
    upstream (``ok_now`` sinks a failing candidate, a demoted one leaves the pool
    entirely), so this tail exists only to be stable.
    """
    return chat_merit_key(cand, primary_model, primary_tier) + (
        (cand.get('provider') or '').lower(),
        (cand.get('model') or '').lower())


def outranks_for_chat_slot(challenger, holder, primary_model, primary_tier):
    """May ``challenger`` take the slot ``holder`` already occupies?

    Compares MERIT only, never the name tail. Using the full
    :func:`chat_slot_key` here is a real bug, caught by replay: the tail makes
    every merit-equal peer with an alphabetically earlier name "outrank" the
    incumbent, so pass 0 evicts a perfectly good holder on the strength of its
    name — stickiness defeated by a field that exists only to break ties.

    Blind to latency, same reason as :func:`outranks_for_slot`: a fallback is not
    there to be fast, it is there to answer when the primary stops.
    """
    return (chat_merit_key(challenger, primary_model, primary_tier)
            < chat_merit_key(holder, primary_model, primary_tier))


def pick_chat_chain(ordered, chat_primary, depth, incumbent_chain=()):
    """Choose the CHAT model's fallbacks (top-level ``fallback_providers``).

    Deliberately NOT :func:`pick_chain`. That function dedupes by model, because
    for compression three labels reselling one model are false diversity — if the
    model dies upstream all three die together.

    For chat the dominant failure observed on the reference install is different.
    In one afternoon: ``balance=0`` on two models of one provider (key exhausted),
    ``429 model quota is temporarily paused`` on another (model throttled), and a
    Cloudflare 522 (origin unreachable). Those are three distinct failures and
    they need three distinct kinds of spare:

    1. **Same model, different key.** Covers the key/quota death, which is the
       most frequent. Capability is identical, so this is the cheapest possible
       degradation. Capped at ``depth // 2`` slots so keys on one origin cannot
       fill the whole chain.
    2. **Different origin.** Covers the 522 — a dead origin takes every key on
       it, so a chain of one host protects against nothing.
    3. **Backfill**, one slot per model, for whatever depth remains.

    Slot stickiness is the same idea as the compression chain
    (blind to latency) but ranked by :func:`chat_slot_key`, not ``tier_of``
    directly — for chat a same-tier peer beats a cheap tier-0 model, which is the
    opposite of what compression wants.

    ``chat_primary`` is the ``(provider, model)`` from ``model.default`` and is
    never placed in its own fallback list.
    """

    def ident(c):
        return ident_of(c)

    def origin(c):
        return (c.get('base_url') or '').split('//')[-1].split('/')[0].lower()

    # Normalised, because ``chat_primary`` comes from config.yaml while the
    # candidates come from a provider listing or a SQLite table, and the two
    # disagree about spelling. See :func:`ident_of`: comparing raw pairs let the
    # primary into its own fallback list on any cosmetic difference.
    primary_ident = ident_of({'provider': chat_primary[0],
                              'model': chat_primary[1]})
    primary_model = _norm_model(chat_primary[1])

    # The primary's own tier and origin, when it is in the pool. Cross-origin
    # means "not this host", so an unknown primary origin makes every host cross.
    primary_tier = tier_of(primary_ident[1])
    primary_origin = ''
    for c in ordered:
        if ident(c) == primary_ident:
            primary_origin = origin(c)
            break

    # Sort by CHAT slot quality rather than trusting the caller's ordering.
    # pick_chain gets away with relying on rank()'s output; this function must
    # not, for two measured reasons:
    #   * rank() sorts tier 0 first (right for compression, backwards for chat —
    #     it offered a flash model ahead of a spare flagship key)
    #   * later passes here can re-take a holder that pass 0 deliberately
    #     rejected, so the challenger set has to already be in slot order
    # Failing candidates go last, same reason rank() does it: Hermes stops
    # walking the chain at the first entry that errors, so a suspect entry at
    # position 0 costs a request. Ties keep the caller's latency order (stable).
    rest = sorted(
        (c for c in ordered if ident(c) != primary_ident),
        key=lambda c: ((0 if c.get('ok_now', True) else 1,)
                       + chat_slot_key(c, primary_model, primary_tier)))

    chain = []
    taken = set()

    def take(cand):
        taken.add(ident(cand))
        chain.append(cand)

    # pass 0: defend what is already on disk (same rule as the compression chain)
    if incumbent_chain:
        by_ident = {ident(c): c for c in rest}
        held = {ident(e) for e in incumbent_chain}
        for entry in incumbent_chain:
            if len(chain) >= depth:
                break
            holder = by_ident.get(ident(entry))
            if holder is None:
                continue                        # left the pool: retired/down
            if not holder.get('ok_now', True):
                continue                        # in grace: must not hold a slot
            if ident(holder) in taken:
                continue
            if any(outranks_for_chat_slot(c, holder, primary_model, primary_tier)
                   for c in rest
                   if ident(c) not in held
                   and ident(c) not in taken
                   and c.get('ok_now', True)):
                continue
            take(holder)

    # pass 1: identical model behind a different key (covers key/quota death)
    same_model_cap = max(1, depth // 2)
    same_model_used = sum(
        1 for c in chain if model_id(c) == primary_model)
    for c in rest:
        if len(chain) >= depth or same_model_used >= same_model_cap:
            break
        if ident(c) in taken or model_id(c) != primary_model:
            continue
        take(c)
        same_model_used += 1

    # pass 2: a different origin (covers the 522 — a dead host takes all its keys)
    seen_origins = {primary_origin} | {origin(c) for c in chain}
    for c in rest:
        if len(chain) >= depth:
            break
        if ident(c) in taken or origin(c) in seen_origins:
            continue
        # A same-model spare on a NEW origin still counts against the same-model
        # cap: the cap exists so one MODEL cannot fill the chain, regardless of
        # how many hosts happen to resell it.
        if (model_id(c) == primary_model and same_model_used >= same_model_cap):
            continue
        seen_origins.add(origin(c))
        take(c)
        if model_id(c) == primary_model:
            same_model_used += 1

    # pass 3: backfill, one slot per model
    seen_models = {primary_model} | {model_id(c) for c in chain}
    for c in rest:
        if len(chain) >= depth:
            break
        if ident(c) in taken or model_id(c) in seen_models:
            continue
        seen_models.add(model_id(c))
        take(c)

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


def as_chat_entry(cand):
    """One top-level ``fallback_providers`` entry in the shape Hermes reads.

    No ``timeout``: the chat chain is resolved per call through the gateway's
    fallback config, which has no per-entry timeout knob. ``base_url`` /
    ``key_env`` are carried so a bare config file still resolves without the
    dashboard; the gateway also re-derives them from ``custom_providers``.
    """
    return {
        'provider': cand['provider'],
        'model': cand['model'],
        'base_url': cand['base_url'],
        'key_env': cand['key_env'],
        'api_mode': cand.get('api_mode') or 'chat_completions',
    }


def chat_chain_needs_write(current, desired):
    """``(bool, reason)`` — is the on-disk chat chain materially different?

    Same tail-churn policy as :func:`needs_write`: a write happens when the
    membership changes or when the FIRST entry changes (the one Hermes tries
    first). Reordering the tail is not worth a write.
    """
    if not isinstance(current, (list, tuple)):
        return True, 'chat chain missing'
    if len(current) != len(desired):
        return True, 'chat chain length changed'

    # ident_of, so a cosmetic respelling on disk (case, vendor prefix, stray
    # space) does not read as a membership change and trigger a pointless write.
    if {ident_of(e) for e in current} != {ident_of(e) for e in desired}:
        return True, 'chat chain members changed'
    if current and ident_of(current[0]) != ident_of(desired[0]):
        return True, 'chat chain[0] changed'
    return False, 'equivalent'


def build(eligible, *, chain_depth=DEFAULT_CHAIN_DEPTH,
          call_timeout=DEFAULT_CALL_TIMEOUT, min_context=None,
          incumbents=frozenset(), incumbent_primary=None, incumbent_chain=(),
          sticky_rel=DEFAULT_STICKY_REL, sticky_abs=DEFAULT_STICKY_ABS):
    """Compute the desired route. Returns None when nothing is verified alive.

    Returning None is deliberate and load-bearing: with no healthy candidate
    the caller must leave the existing config ALONE. Overwriting a route with
    an empty or guessed one turns a recoverable outage into a broken config.

    ``incumbents`` stabilises which models are in the route; ``incumbent_primary``
    stabilises which one leads it; ``incumbent_chain`` (the on-disk chain, in
    order) stabilises which fallback holds which slot. Pass all three from the
    existing config (see ``route_idents``, ``primary_ident`` and
    ``chain_entries``) or none of them for a cold build.
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

    chain = pick_chain(ordered, primary, chain_depth, incumbent_chain)
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
            return ident_of(e)
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
