# Changelog

All notable changes to this project are documented here.

Format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/);
versioning follows [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [0.7.0] — 2026-09-04

Two identity bugs, reported from outside and confirmed by reproduction. Both
were silent: nothing errored, the wrong thing simply happened. Together they
disabled the chat chain's first pass — the one that covers the most common
failure — while its unit tests stayed green, because the tests supplied
already-consistent identities that real config never guarantees.

### Fixed

- **A spare key at a shared relay was deleted before it could be probed.**
  `discovery.discover` deduped on `(base_url, model)`, so two provider labels
  fronting one endpoint with *different credentials* collapsed to one candidate.
  That is precisely the "same model, different key" spare `pick_chat_chain`
  selects first, and the only kind that survives a `balance=0` or a `429` quota
  pause — both properties of the credential, not the model. Identity is now
  `(base_url, model, resolved_key)`. Two labels sharing one credential still
  collapse (that is one route listed twice, and it is what lets a pinned entry's
  `api_mode` win over the same model from a listing).
- **The chat primary was offered as its own fallback.** `pick_chat_chain`
  excluded it by raw tuple equality, but `chat_primary` comes from `config.yaml`
  while candidates come from a provider listing or a SQLite table, and the two
  disagree about spelling. Five of six real-world spellings leaked — provider
  prefix in the model name, either field's case, stray whitespace, an aggregator
  vendor slug. A fallback list whose first entry is the model that just failed
  protects against nothing. `ident_of` now normalises: strip, lowercase, and
  reduce the model to its bare name.
- **Incumbency lookups silently missed for the same reason.** `route_idents`,
  `primary_ident`, `sticky_latency`, `choose_primary`, `needs_write` and
  `chat_chain_needs_write` all compared identities across that same boundary. A
  miss there disables latency stickiness and leaves the incumbent primary
  undefended — invisibly, since a lookup that finds nothing looks exactly like a
  candidate that is genuinely new.

### Notes

`model_id` now shares `_norm_model` with `ident_of` so the two cannot drift.
Normalising does **not** merge spare keys: a different credential serving the
same model is still a distinct candidate, which is what keeps pass 1 working.

11 new tests (293 total), including the five leaking spellings as a
parametrised set and an assertion that the pass-1 spare survives normalisation.

## [0.6.2] — 2026-09-04

The fifth churn source, and the first one that lived in the chat chain rather
than the compression route. Found by replaying the real candidate pool, not by
reading the code: **151 writes over 200 ticks**, and with nothing at all
flapping, **150 of them were pure churn**.

`chat_slot_key` ranked on merit alone — same-model-behind-another-key, tier
distance, context width — which left large tied groups: 6 of 11 live candidates
shared one key. A tie fell through to the caller's `rank()` order, which is
latency-driven, so every median crossing inside a tied group reshuffled which
peer held which slot, and every reshuffle was a config write.

- **Deterministic tail** on `chat_slot_key`: `(provider, model)`, lowercased.
  151 -> 2 writes over 200 ticks; identical result across 5 RNG seeds and all
  720 latency permutations of the live pool.
- **`chat_merit_key` split out** from `chat_slot_key`. The two questions are
  different: ordering needs a total order, eviction must compare merit only.
  `outranks_for_chat_slot` now uses the merit key — with the full key, any
  merit-equal peer with an alphabetically earlier name evicted the incumbent,
  which defeated slot stickiness entirely. Caught by replay, then pinned by a
  test.
- **The tail must be immutable.** `fail_streak` was tried in it and measured
  *worse than nothing*: a peer wobbling 0/1/2 through its grace period reorders
  the group every tick — 182 writes over the same 200 ticks. Health is already
  handled upstream.
- 5 new tests (20 in `test_chat_chain.py`; 280 overall).

## [0.6.1] — 2026-09-04

The chat chain shipped in 0.6.0 was dead on arrival as *behaviour*: the
`pick_chat_chain` family lived in the router but the CLI never called it, so
`--apply` maintained only `auxiliary.*` routes and never touched top-level
`fallback_providers` — the chat model kept its stale chain no matter how many
probes failed. The logic was unit-tested; nothing wired it to a real config.

- **CLI wiring**: `--apply` now reads `model.provider`/`model.default`,
  picks the chat chain from the same probe results, and writes
  `fallback_providers` in the same transaction as the auxiliary route.
  `model.*` is never rewritten — the user's chat choice stays.
- New `--chat-depth` flag (default `DEFAULT_CHAT_CHAIN_DEPTH` = 4).
- Chat chain obeys the same write gate: a correct chain produces zero output,
  a stale one is healed, dry runs report without writing.
- 4 new CLI tests (17 CLI total; 275 overall).

## [0.6.0] — 2026-09-04

A second, different fallback chain: this time for the CHAT model, not the
auxiliary summariser.

The compression chain dedupes by model — three labels reselling one model are
false diversity, because if the model dies upstream every label dies together.
The chat chain deliberately does the opposite. The failures observed on a live
install in one afternoon were `balance=0` on two models (key exhausted), `429
model quota is temporarily paused` (model throttled), and a Cloudflare 522
(origin unreachable). Three distinct failures, three distinct kinds of spare:

1. **Same model, different key** — covers the key/quota death, which is the
   most frequent. Capability-identical, cheapest possible degradation. Capped
   at `depth // 2` slots so one model cannot fill the whole chain (that cap now
   also applies across origins — a same-model spare on a NEW host still counts
   against it).
2. **Different origin** — covers the 522: a dead origin takes every key on it.
3. **Backfill**, one slot per model.

Ordering is by *closeness to what the user chose* (`chat_slot_key`), not by
`tier_of`: the first live sync proved the naive reuse wrong when it offered a
cheap flash model (tier 0) ahead of a spare key for the user's own flagship
model (tier 2). For chat that is a capability downgrade before a
like-for-like replacement. Slot stickiness is the same latency-blind guard as
the compression chain.

New functions: `chat_slot_key`, `outranks_for_chat_slot`, `pick_chat_chain`,
`as_chat_entry`, `chat_chain_needs_write`; new constant
`DEFAULT_CHAT_CHAIN_DEPTH` (4).

## [0.5.0] — 2026-09-03

The third churn source, found the same way as the first two: by measuring the
live install instead of trusting the previous fix. Median ranking (0.3.0) cut the
write rate from 53% to roughly 11%, and the primary held steady for hours. The
remaining writes were almost all the same event — two models trading one
fallback slot.

Over 6.5 hours: 12 writes, 9 of them pure chain reorders. The two models
involved were the same tier, both advertising a 1M context window, with medians
crossing every few ticks (2.1s vs 6.8s, then the reverse). Nothing about which
models were reachable changed; the file was rewritten to say the same thing in a
different order.

`choose_primary` had a stickiness guard. `pick_chain` had none — it re-picked
from a fresh ranking every tick, so any crossing evicted the incumbent.

### Added

- **Chain slot stickiness.** `pick_chain` accepts the chain currently on disk
  (`incumbent_chain`, from the new `router.chain_entries()`) and defends each
  slot: a holder keeps its position unless a challenger out-ranks it on tier or
  context window.
- `router.outranks_for_slot()` — the displacement bar for chain slots,
  deliberately **blind to latency**. A fallback exists to answer when the
  primary stops answering, and tier plus context decide whether it can; probe
  latency is the one input too noisy to act on. Replaying the 99 real ticks:
  defending slots on latency with the standard hysteresis margins still allowed
  6 swaps, and only an absurd 5s absolute margin reached zero — which would also
  block genuine failovers. Defending on tier and context allowed 0.
- 24 tests (`tests/test_chain_stickiness.py`), covering both directions of the
  observed crossing plus every case where a holder must still lose its slot.

### Changed

- Latency keeps its old job of ordering candidates for an **empty** slot. It no
  longer evicts an incumbent from an occupied one.
- A holder is not defended when it has left the eligible pool (retired, down,
  demoted) or failed its latest probe — `ok_now` false must never sit at
  `chain[0]`, since Hermes stops walking the chain at the first entry that
  errors mid-request.

### Verified

Replaying the real log through both code paths, 100 ticks where both contenders
were alive: **6 writes before, 0 after**. Full suite 256 tests.

Rules 1 and 2 still outrank stickiness: two labels for one model take one slot,
and cross-provider placement still comes before same-provider backfill. A holder
that pass 0 declines to defend can still be re-selected in pass 1 — same chain,
so no write.

## [0.4.0] — 2026-09-03

Discovery only read models pinned by hand in `config.yaml`. That matched one kind
of install and silently failed the other: a relay or gateway fronting dozens of
upstreams is configured as **one** provider entry with `discover_models: true`,
and nobody enumerates sixty models by hand. Such a config produced zero
candidates and the tool exited with `no candidate models with usable API keys` —
correct message, useless outcome.

The bug was invisible from the install this grew out of, which pins every model
explicitly. It only appears on the shape most relay users actually have.

### Added

- **Model discovery from `/v1/models`.** Providers that pin no models are asked
  for their own catalogue (`discovery.list_models()`, `discovery.from_endpoint()`,
  `discovery.pending_discovery()`). Listing supplies *names* only — every name
  still gets a real completion before it can enter a route, because being listed
  is not evidence of being routable. That was already the project's whole thesis
  and it does not change here.
- **Non-chat filtering.** A relay's catalogue also fronts embeddings, rerankers,
  speech, image, video and moderation models; sending those a chat completion
  produces a confusing failure rather than a verdict. `discovery.is_chat_model()`
  filters by name and each skip is reported. Applied **only** to discovered
  models — anything pinned by hand is taken at the user's word, since the
  heuristic is wrong in both directions on unusual names.
- `--no-discover-models` to switch listing off entirely, and `--max-discovered`
  (default 25) to cap how many models one listing contributes. A gateway
  advertising 300 ids would otherwise mean 300 probes per tick.
- 53 tests for the above (`tests/test_discovery_endpoint.py`), including the
  regression itself: a relay-only config previously yielded zero candidates.

### Changed

- Listing responses are cached per `base_url` for the life of the process, so
  sibling providers sharing one relay do not each fetch the same catalogue.
- Both OpenAI shapes are accepted: `{'data': [{'id': ...}]}` and a bare array.
- A provider whose API key is missing is never contacted — no point asking a
  gateway for its catalogue with credentials we do not have. The skip says which
  variable is absent.
- Discovery order is now config-pinned, then listing, then SQLite, with earlier
  winning on collision. A hand-pinned entry keeps its explicit `api_mode` and
  `key_env` even when the same model also appears in a listing.

### Documented

- New README section on relays and gateways, including the limit worth being
  honest about: when every model reaches you through one relay, a chain of four
  is four models behind one endpoint, and cross-provider fallback buys nothing
  until a genuinely separate second endpoint exists.

## [0.3.0] — 2026-09-03

Ranking hysteresis (0.2.0) was calibrated for the wrong magnitude of noise. On a
live install the write rate did not fall — it sat at 53% before the guard and
67% over the 15 ticks after it. The assumption behind the 30% / 0.5s margins was
that probe latency wobbles by a few hundred milliseconds. It does not: the same
model measured 1.3s, then 6.7s, then 42.0s inside twenty minutes on an
aggregator under changing upstream load.

No threshold fixes a noisy input, so this release smooths the input instead, and
fixes a second churn source that hysteresis never touched.

Measured after both changes, on a clean 15-tick cron window: **6.7%** write rate
(1 of 15), and that one write was a legitimate displacement — the challenger was
41% and 0.9s faster on median, clearing both margins. The primary held for 71
minutes, against every 4–5 minutes before.

### Added

- **Median latency ranking.** `health.record_latency()` keeps a rolling window
  of the last N successful probes in the health cache; `health.median_latency()`
  reduces it. `router.rank_latency()` is what ranking, `sticky_latency()` and
  `beats()` now read. Window size is `--latency-window` (default 5; `1` disables
  smoothing).

  Median rather than a mean because these are spikes, not drift: one 42s outlier
  among five samples moves a median not at all and a mean by 8s.

  Only successful probes are recorded. A failure's elapsed time measures how
  long the error took to arrive, and mixing the two would let a fast 401 look
  like a fast model.

- **One chain slot per model** (`router.model_id()`, rewritten
  `router.pick_chain()`). Several providers may resell the identical model under
  different labels — one shared endpoint, different keys and quotas. Treating
  those as distinct fallbacks is false diversity: when the model is retired
  upstream, every label dies in the same instant. It was also 24 of 139 observed
  writes, which were nothing but three labels for one model rotating through the
  same slots.

  Identity is the bare model name, so `vendor/model` from one aggregator and
  `model` from another collapse to one entry. The cross-provider rule from 0.1.0
  still applies first.

- 40 tests (`tests/test_latency_smoothing.py`), including that smoothing does
  not hide a real regression, that `beats()` compares medians rather than latest
  probes, and that a sibling-label reshuffle no longer produces a write.

### Changed

- `health.evaluate()` emits `lat_median` and `lat_n` on each eligible candidate
  and accepts `latency_window`.
- `--verbose` prints `med=<median>s(n=<samples>)` next to the raw probe time, so
  a spike is visible as a spike rather than as a route change.

### Fixed

- `beats()` and `sticky_latency()` compared raw `latency`, which meant a single
  slow probe could displace a model whose typical latency was far better.

## [0.2.0] — 2026-09-03

### Added

- **Ranking hysteresis** (`--sticky-rel`, `--sticky-abs`). A model already in the
  route is compared at a discounted latency, so a challenger must be both 30%
  and 0.5s faster (defaults) before it displaces one. Set either margin to `0`
  to disable.

  Failure classification, added in 0.1.0, stops a *failing* model from flapping.
  It does nothing when nothing is failing — two healthy models of the same tier
  and context window, separated only by probe latency that swings a few hundred
  milliseconds, swap rank almost every tick, and every swap is a config write.
  Measured on a live install: 130 writes across 245 ticks (53%), the primary
  bouncing between two perfectly healthy models.

  Hysteresis applies to latency only. Tier and context window are stable
  properties, so a challenger winning on either takes the slot immediately, and
  an incumbent that fails its probe always loses it.

- `router.choose_primary()` protects the primary slot specifically. The ranking
  discount cannot do this job: when the leader and the incumbent are both already
  in the route, both sides of the comparison are discounted and the effect
  cancels out. The incumbent is therefore compared against the challenger
  directly, via `router.beats()`.

- `router.route_idents()` and `router.primary_ident()` read incumbency out of an
  existing `auxiliary.<task>` block. Both tolerate malformed or missing config by
  reporting no incumbency, which degrades to 0.1.0 ranking rather than raising.

- 42 tests for the above (`tests/test_hysteresis_ranking.py`), including that
  hysteresis does **not** become lock-in: a decisive lead still wins, a better
  tier wins immediately, a failing incumbent is always displaced, and each margin
  alone is insufficient.

### Changed

- `router.rank()` takes an optional `incumbents` set; `router.build()` takes
  `incumbents` and `incumbent_primary`. Both default to empty, so existing calls
  behave exactly as in 0.1.0.
- `--verbose` marks route members with `HELD`.
- Author metadata corrected to `Machbub`.
- README rewritten: the originating incident stated up front, real dry-run output
  and a real before/after config diff, and stability split into the two distinct
  failure modes it addresses.

## [0.1.0] — 2026-09-02

Initial release.

### Added

- Health-probes every `(provider, model)` pair discoverable from
  `custom_providers` in `config.yaml`, optionally also from a dashboard SQLite
  database, and rewrites `auxiliary.<task>` from what is verified alive.
- Probes with a real 4-token completion rather than a `/v1/models` listing.
  Aggregators list models they cannot route; a listing check calls those healthy.
- Failure classification: permanent verdicts (`model_not_found`, 400/401/403/404,
  revoked credentials) demote on the first strike; ambiguous ones (timeout, 5xx,
  429, connection reset) need `--demote-streak` consecutive strikes. Recovery is
  symmetric via `--promote-streak`. A model inside its grace period stays in the
  chain but is barred from the primary slot.
- Persistent health cache keyed by `provider|base_url|model`, so sibling
  providers sharing one endpoint with different keys are tracked separately.
  Legacy 2-part keys are fanned out on upgrade rather than dropped — a cache miss
  would silently reset streak counters and re-enable flapping.
- `config_io.config_transaction`: cross-process `flock`, re-read inside the lock,
  atomic replace via temp file + `fsync` + `os.replace`, mtime conflict detection
  against non-participating writers, top-level key-count validation before
  commit, and per-writer namespaced backups. Usable standalone for any script
  that writes Hermes config from outside the package.
- Write gating: reordering the tail of the chain is not worth a config write.
  A write happens only when the primary changes, `chain[0]` changes, chain
  membership changes, or the timeout changes.
- Ranking tuned for background summarisation — cheap and fast over smart, widest
  context window as a tiebreak — with tier patterns matching generic size and
  speed words rather than vendor brand names. The fallback chain crosses
  providers before taking a second model from the primary's provider.
- Comment-preserving writes through `ruamel.yaml` when installed, with a PyYAML
  fallback that works but drops comments.
- Dry run by default; `--apply` to write.
- Exit codes: `0` correct or corrected, `1` nothing healthy (config untouched),
  `2` write refused.

### Fixed

- Health cache key scoped by provider, not just `base_url` + model. Without the
  provider, siblings on a shared endpoint collided and one sibling's verdict was
  read back as every sibling's, so a dead key could look alive.

[0.5.0]: https://github.com/Machbub/hermes-aux-autoheal/compare/v0.4.0...v0.5.0
[0.4.0]: https://github.com/Machbub/hermes-aux-autoheal/compare/v0.3.0...v0.4.0
[0.3.0]: https://github.com/Machbub/hermes-aux-autoheal/compare/v0.2.0...v0.3.0
[0.2.0]: https://github.com/Machbub/hermes-aux-autoheal/compare/v0.1.0...v0.2.0
[0.1.0]: https://github.com/Machbub/hermes-aux-autoheal/releases/tag/v0.1.0
