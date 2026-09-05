# Stability and measurements

Everything here was found by measuring a running install, not by reasoning ahead
of it. The README states the conclusions; this file keeps the evidence, including
the attempts that made things worse.

It is not a tour of the internals. It answers one question: **is a tool that
rewrites your config on a timer safe to leave running?** The first honest answer
was no. Probe-and-write with no guards rewrote `config.yaml` on 130 of 245 ticks —
53%, most of them changing nothing that mattered, which is worse than leaving the
file alone. Everything below is how that came down, what each guard cost to find,
and where it still sits: **65 writes across 673 live ticks, 9.7%**, measured on
the install this was written on. Replays of individual guards go lower; a replay
is not a steady state, and the difference between those two numbers is the honest
part of this file.

Numbers are quoted with the window they were taken over. A single headline figure
from a short window is flattering by accident — one of the rates below fell to
6.7% over fifteen ticks and rose to 10.8% over ninety-three, on identical code.

## The incident

A long conversation died mid-compaction. The model named under
`auxiliary.compression` had been retired by the provider serving it weeks
earlier — it was still listed in that endpoint's `/v1/models`, so nothing looked
broken until the compression call itself returned `no available channel for
model`. Hermes' `fallback_chain` was configured, but the next two entries had
gone stale the same way.

The fix was not a better fallback list. It was checking, on a timer, whether the
names in that list still resolve to something that answers. That is all this
tool does.

## Six churn sources, six guards

Probe-and-write on every tick is unstable in six different ways. Each needs its
own guard; none substitutes for another. On a live install before any of them
existed: **130 config writes across 245 cron ticks — 53%**.

| # | Source | Guard | Measured |
|---|---|---|---|
| 1 | a model near the timeout flapping in and out | failure classified permanent vs ambiguous, `--demote-streak` | 53% baseline |
| 2 | two healthy models swapping the primary slot | `choose_primary` hysteresis | made it **worse**: 67% |
| 3 | probe latency noise (1.3s / 6.7s / 42.0s, same model) | rank on a 5-sample median | 6.7% (15 ticks), 10.8% (93 ticks) |
| 4 | two same-tier models trading a chain slot | slot stickiness, blind to latency | replay: 6 → 0 swaps |
| 5 | merit-tied chat spares reshuffling on latency noise | immutable `(provider, model)` tie-break tail | replay: 151 → 2 writes / 200 ticks |
| 6 | unseatable duplicates evicting every slot holder | challenger must be seatable; per-slot grace | replay: 9.2% → 4.0% of ticks |

### 1. A failing model that keeps recovering

A model sitting near the timeout boundary flaps: in, out, in, out, each swing
rewriting config and firing a notification. Observed in the wild: one model
entered and left a chain four times in 2.5 hours, having answered a probe in 23s
against a 30s limit.

Failures are therefore classified:

- **permanent** — `model_not_found`, 400/401/403/404, revoked credentials.
  A verdict about the model or the key, so it demotes on the **first** strike.
  Waiting would keep a provably unusable entry in the route.
- **ambiguous** — timeout, generic 5xx, 429, connection reset. Could be a
  passing blip, so it needs `--demote-streak` consecutive strikes (default 2).

Recovery is symmetric: a model that was down needs `--promote-streak`
consecutive passes before it is trusted again. While a model is inside its grace
period it stays in the chain but is barred from the primary slot.

Streaks live in the health cache so they survive between ticks. Results are
reused for `--ttl` seconds (default 600) to keep probe traffic down; `--no-cache`
forces a fresh probe of everything.

### 2. Two healthy models trading places

Failure classification does nothing here, because nothing is failing. Two models
of the same tier and the same context window, separated only by probe latency,
will swap rank and rewrite the config on almost every tick.

The first attempt was hysteresis alone: require a challenger to beat an incumbent
by a margin. **It did not work, and it is worth stating why, because the mistake
is easy to repeat.** The margins assumed latency wobbles by a few hundred
milliseconds. It does not — under changing upstream load, the same model measured
**1.3s, then 6.7s, then 42.0s inside twenty minutes**. No threshold makes a noisy
input produce a stable output. Raising the threshold is the wrong instrument: it
trades churn for lock-in.

Write rate: 53% before the guard, **67% over the 15 ticks after it**.

### 3. Smooth the input first

Latency goes into a rolling window (`--latency-window`, default 5 successful
probes, persisted in the health cache) and **ranking reads the median, not the
latest sample**.

- **Median, not mean** — these are spikes, not drift. One 42s outlier among five
  samples moves a median not at all and a mean by 8s.
- **Successes only.** A failure's elapsed time measures how long the error took
  to arrive; mixing them lets a fast 401 look like a fast model.
- The log prints `med=3.2s(n=5)` beside `probe=20.4s`, so a spike reads as a
  spike.

On top of the smoothed value, an incumbent is compared at a discounted latency
and a challenger must beat it by **both** margins:

| flag | default | meaning |
|------|---------|---------|
| `--sticky-rel` | 0.30 | challenger must be 30% faster |
| `--sticky-abs` | 0.5 | and 0.5s faster in absolute terms |

Both, because either alone is cheap to hit by accident. At 0.4s a 30% lead is
120ms of noise; at 12s a 0.5s lead is rounding. Set either to `0` to disable.

Hysteresis applies to latency only. Tier and context window are stable properties
of a model, so a challenger that wins on either takes the slot immediately. A
model that fails its probe loses the slot regardless.

Two slots need protecting, and they need different code. Route **membership** is
protected by ranking at a discount, so an outsider needs a margin to push a
member out. The **primary slot** is protected by comparing the leader against the
incumbent directly — when both are already in the route the ranking discount
applies to both sides and cancels out. Ranking alone was the first attempt and it
failed; the end-to-end test is what caught it.

### 4. Two healthy models trading one slot

The guards above left the primary stable for hours and cut writes to roughly 11%
— and then almost every remaining write turned out to be the same event. Over one
6.5-hour window: **12 writes, 9 of them pure chain reorders.** The two models
involved were the same tier, both advertising a 1M context window, medians
crossing every few ticks (2.1s vs 6.8s, then the reverse). The route was
rewritten to say the same thing in a different order.

`choose_primary` had a stickiness guard. The chain picker did not — it re-picked
from a fresh ranking every tick, so any crossing evicted the incumbent.

A chain slot now defends itself: the holder keeps its position unless a challenger
out-ranks it on **tier or context window**. Latency is deliberately excluded. A
fallback is not there to be fast, it is there to answer when the primary stops
answering, and latency is the one input too noisy to act on.

Replaying the 99 real ticks of that pair:

| Slot defence | Swaps |
|---|---|
| none (0.4.0) | 6 |
| latency, 30% + 0.5s margins | 6 |
| latency, 30% + 2.0s margins | 4 |
| latency, 30% + 5.0s margins | 0 |
| tier and context only | 0 |

Only an absurd 5-second absolute margin reached zero on latency, and that would
also block genuine failovers. Tier and context reached zero while still yielding
instantly to a better-suited model.

Latency keeps its old job: ordering candidates that compete for an **empty** slot.
It no longer evicts an incumbent from an occupied one.

### 5. Merit ties in the chat chain

The chat chain ranks by closeness to the model you chose, and merit alone left
**6 of 11 live candidates tied on one key**. A tie falls through to the caller's
latency order, so every median crossing inside a tied group reshuffled which peer
held which slot — and a reorder is a config write.

The fix is a tie-break tail on `(provider, model)`. Replaying the real pool over
200 ticks: **151 writes without the tail, 2 with it.** With nothing flapping at
all, 150 of those 151 were pure churn.

The tail must be **immutable**. `fail_streak` was tried in that position and
measured worse than nothing — **182 writes** over the same 200 ticks, because a
peer wobbling 0/1/2 through its grace period reorders the group every tick.

Related trap: the eviction test must compare merit only, never the full key with
its tail. With the tail included, any merit-equal peer with an alphabetically
earlier name "out-ranks" the incumbent, and stickiness is dead. Unit tests missed
this; replay caught it.

### 6. Challengers that could never take the slot

Found by measuring writes *after* identity comparison was fixed: **11 chat-chain
writes in 157 live ticks**, several on ticks where nothing had failed.

The eviction check asked "does any better-ranked candidate exist?". The question
that justifies a write is "does any better-ranked candidate **that would actually
be seated** exist?". Instrumented on the real pool, the first pass seated **1 of
4 slots** — so the chain was rebuilt from scratch nearly every tick, and slot
stickiness, the latency margin and the tie-break tail were all dead code for
chat. Most evictions were also demotions: the phantom freed a slot it could not
fill, so whatever ranked next moved in.

Same release, second fix: a mid-chain holder was surrendering its slot on a
single failed probe, but one failed probe means `strike 1/demote-streak` and the
candidate is still eligible — two contradictory policies in one file, costing two
writes per blip (demote, then restore). Slot 0 stays strict, because Hermes tries
it first on every request.

Measured over 400-tick replays, per-model blip rates taken from the live log, 5
seeds: writes **9.2% → 4.0%** of ticks; demoting writes **36 → 2**. Live
observation over the same window was 7.0%, which is how the replay was validated.

## What it adds up to

| Window | Ticks | Writes | Rate |
|---|---|---|---|
| before any guard | 245 | 130 | 53% |
| hysteresis alone (0.2.0) | 66 | 44 | 67% |
| median ranking, first hour (0.3.0) | 15 | 1 | 6.7% |
| median ranking, 6.5h (0.4.0) | 93 | 10 | 10.8% |
| chain defence, replayed on that same 6.5h (0.5.0) | 100 | 0 | 0% |
| chat tie-break tail, replayed (0.6.2) | 200 | 2 | 1% |
| seatable-challenger check, replayed (0.7.1) | 400 | — | 9.2% → 4.0% |
| **live, all guards, through 0.8.3** | **673** | **65** | **9.7%** |

Read the last row against the replay rows above it, not instead of them. The
replays measure one guard on the pool it was built for; the live row is every
guard together against four days of real provider behaviour — in that window the
log carries an exhausted account balance, a model whose quota was "temporarily
paused", a `model_not_found` on a name still being listed, and one model timing
out on 115 probes. 9.7% is the number to plan around. Anyone quoting the 4.0% as
this tool's steady state — including its own release notes, if they ever do — is
quoting a replay.

Two rows worth reading carefully. Hysteresis alone made churn **worse** — the
margins were calibrated for noise an order of magnitude smaller than the real
thing. And the 6.7% figure was real but flattering: a longer window put the same
code at 10.8%, because rarer crossings need time to show up.

The replay rows are replays, not fresh observations: recorded medians pushed back
through both code paths. They count writes avoided on the contending pair. They
do not prove a steady state — only that every write in that window came from a
cause the new guard removes.

Verify it on your own install by comparing tick and write counts:

```bash
grep -c 'route sync starting' autoheal.log
grep -c 'route updated'       autoheal.log
```

And check *why* it wrote, which is what tells you whether the route actually
changed or merely got reordered:

```bash
grep -o 'route updated ([^)]*)' autoheal.log | sort | uniq -c | sort -rn
```

## The quota wall a probe cannot see

The clearest limit on scheduled probing, measured rather than assumed.

On the reference install, one model accumulated **441 `HTTP 429` responses in
real traffic** across six days (counted in the agent's own error log and its
rotation, deduplicated; 332 of them in a single day). Over the same period the
4-token probe kept returning `200 OK`.

One pairing from the retained logs, precise enough to check:

```
real traffic  02:35:51   HTTP 429   model quota is temporarily paused
probe         02:30:28   ok         probe=1.9s   med=2.1s(n=5)
probe         02:37:16   ok         probe=1.5s   med=2.1s(n=5)
```

The probe is too small to trip a limit that a 25k-token request trips
immediately, and the outage arrives in bursts that begin and end between two
ticks. This is not a bug to be fixed; it is the floor on what a scheduled probe
can detect. A proxy in the request path sees this and this tool does not — which
is exactly why the two compose rather than compete.

## A text probe cannot certify a vision route

The plain `'ping'` probe proves a model answers text. It proves nothing about
images — and a vision route built from text-verified models fails on the first
image the user sends, then keeps failing, because the failure is a capability
mismatch, not an outage.

Observed on the reference install: the chat model was switched to a text-only
model (via the dashboard), `auxiliary.vision` was unset, and every image
request then hit that model. The provider refused the payload with
`400 Model do not support image input`; the error is *not* one of the
capability classes Hermes' auxiliary client matches, so its fallback chain
never engaged and `vision_analyze` died per-image. Three failure modes stacked
in one symptom:

1. **Vision routed by default to the chat model** — the backend changes every
   time the user switches chat models, with no warning that the new one is
   text-only.
2. **The text probe certified text-only models as healthy** — they answered
   `'ping'` fine.
3. **The 400 was not classified as fallback-worthy** by the auxiliary client,
   so even a configured `fallback_chain` was never consulted.

The fix has two halves. The tool half is this release: the vision probe carries
a real image (16×16 PNG), the `400` capability rejection is a permanent verdict,
and the health cache is scoped per task so a text-probe verdict cannot leak
into the vision route. The Hermes half is out of this repo's hands: routing a
text-only chat model to vision, and not classifying the resulting 400, are
behaviour in the auxiliary client — this tool works around them by never
letting a text-only model into the route in the first place.

## Behaviours the tests pin

423 tests, run against both YAML backends — with and without `ruamel.yaml`, since
the fallback path is what most people hit first. No network: probes and the
`/v1/models` listing are stubbed, but discovery, the health state machine, route
building and config writing all run against real files, including a genuine
three-process write race.

These are the ones that are easy to regress:

- **Sibling providers are probed separately.** Several providers may share one
  `base_url` (one endpoint, different keys and quotas), so the health cache is
  keyed by `provider|base_url|model`. Keying it without the provider makes them
  collide, and one sibling's verdict is read back as every sibling's — a dead key
  looks alive.
- **Cache entries migrate, they do not reset.** Upgrading a legacy 2-part key
  fans it out to each sibling instead of dropping it, because a cache miss
  silently resets the streak counters and re-enables flapping.
- **Hysteresis must not become lock-in.** A jitter-sized lead is rejected, but a
  decisive one still wins, and a failing incumbent always loses its slot.
- **Smoothing must not hide a regression.** A single spike is ignored, but when
  the median itself moves the model does lose its slot. Smoothing that swallowed
  real slowdowns would be worse than no smoothing.
- **A pinned model is never re-listed, and a keyless provider is never
  contacted.** Discovery spends a listing request only where it has nothing to go
  on and a key to go with it.
- **A chain slot holder is not evicted by latency.** Both directions of a real
  median crossing are pinned, because a guard that only holds in the ordering it
  was written against is not a guard. A holder still loses `chain[0]` when it
  fails a probe; further down the chain it rides out its grace window instead.
  The grace check is indexed by the slot the holder would OCCUPY, so promotion
  into the front slot faces the front slot's bar.
- **The chat chain still knows where its primary lives when the primary is
  down.** Origin diversity is computed against the pool *before* health
  filtering, because the primary leaves the filtered pool exactly when its host
  becomes unreachable — and that is when a spare must not be placed on the same
  host. Losing that one fact put a dead-host entry at `fallback_providers[0]`,
  which Hermes tries first, and no later tick could correct it. Stable-and-wrong
  is harder to spot than flapping.
- **A text-probe verdict never routes a vision chain.** The vision probe is a
  real image payload, the `400` capability rejection is permanent, and the
  cache key carries the task — so a model verified healthy on text cannot
  inherit that verdict for images, and a model demoted for refusing images
  cannot be saved by a later text-probe pass.
- **Every documentation link resolves.** A dead `#anchor` is invisible: the page
  loads, the link scrolls nowhere, and no test notices. `tests/doc_links.py`
  indexes the headings of every markdown file in the repo and checks each
  relative link and anchor against them, so this file and the README cannot drift
  apart silently. Two subtleties the checker exists for: GitHub's slugs strip
  dots, so `#writing-config.yaml-safely` is dead and
  `#writing-configyaml-safely` is not — that link shipped broken once — and a `#`
  line inside a fenced code block is a shell comment, not a heading, so a
  line-based checker invents anchors and reports PASS on links GitHub cannot
  resolve. Its own tests mutate the checker three ways (unfenced indexing,
  dot-preserving slugs, anchor check disabled) and require each mutation to make
  the suite fail; a linter that cannot fail is decoration.

## How a churn fix gets verified here

A churn test that passes on the **old** code tests nothing. Three hand-built pool
shapes all passed against the unfixed version before the real one was found.

The procedure that works:

1. Replay both versions over identical tick sequences — the released module
   against the patched file, same seeds.
2. Capture the first state where one writes and the other does not.
3. Anonymise **that** state into a fixture, not an invented one.
4. When anonymising, remember tier is read from the model **name**: assert the
   placeholder lands on the same tier, or the fixture stops reproducing.
5. Build the pool from the real health cache, jitter the latency, count writes.

Two traps that silently invalidated earlier drafts:

- Naming a test challenger something like `fast-preview` matches the fast-tier
  pattern, turning a latency test into a tier test. Use tier-neutral names and
  assert the tiers are equal inside the test.
- The cross-provider rule runs before tier and context, so a challenger from the
  primary's own provider cannot take the slot at all — the holder is simply
  re-selected and the test looks like stickiness when it is not. Give the
  challenger a third provider.
