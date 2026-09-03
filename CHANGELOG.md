# Changelog

All notable changes to this project are documented here.

Format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/);
versioning follows [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

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

[0.2.0]: https://github.com/Machbub/hermes-aux-autoheal/compare/v0.1.0...v0.2.0
[0.1.0]: https://github.com/Machbub/hermes-aux-autoheal/releases/tag/v0.1.0
