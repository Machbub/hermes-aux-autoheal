# hermes-aux-autoheal

[![tests](https://github.com/Machbub/hermes-aux-autoheal/actions/workflows/tests.yml/badge.svg)](https://github.com/Machbub/hermes-aux-autoheal/actions/workflows/tests.yml)
[![release](https://img.shields.io/github/v/release/Machbub/hermes-aux-autoheal)](https://github.com/Machbub/hermes-aux-autoheal/releases)
[![python](https://img.shields.io/badge/python-3.9%2B-blue)](https://www.python.org/downloads/)
[![license](https://img.shields.io/github/license/Machbub/hermes-aux-autoheal)](LICENSE)

Keeps [Hermes Agent](https://github.com/NousResearch/hermes-agent)'s auxiliary
task routes pointed at models that actually answer.

Third-party project. Not affiliated with or endorsed by Nous Research.

## The incident

A long conversation died mid-compaction. The model named under
`auxiliary.compression` had been retired by the aggregator serving it weeks
earlier — it was still listed in that endpoint's `/v1/models`, so nothing looked
broken until the compression call itself returned `no available channel for
model`. Hermes' `fallback_chain` was configured, but the next two entries had
gone stale the same way.

The fix wasn't a better fallback list. It was checking, on a timer, whether the
names in that list still resolve to something that answers. That is all this
tool does.

## The problem

Hermes lets you pin a provider/model per auxiliary task — context compression,
summarization, vision — plus a `fallback_chain` of runners-up:

```yaml
auxiliary:
  compression:
    provider: SomeProvider
    model: some-fast-model
    fallback_chain:
      - provider: OtherProvider
        model: other-model
```

That config is static. Nothing ever re-checks it. Aggregator gateways retire
models without warning, keys get revoked, providers go down — and the route
keeps naming a corpse. You find out when a background task fails, which for
compression means a conversation that stalls exactly when it grew long enough
to need compacting.

Hermes' `fallback_chain` is reactive: it moves on *after* a call fails. Nobody
checks beforehand whether the names in the list are still real.

## What it looks like

Dry run is the default. Nothing touches your config until you pass `--apply`.

```console
$ hermes-aux-autoheal --task compression --verbose
  ok   ProviderA/swift-flash: tier=0 ctx=1,000,000 probe=1.7s med=0.9s(n=5) HELD
  ok   ProviderB/compact-flash: tier=0 ctx=1,000,000 probe=1.3s med=1.3s(n=5)
  ok   ProviderB/rapid-mini: tier=0 ctx=1,000,000 probe=2.0s med=2.2s(n=5) HELD
  ok   ProviderA/quick-flash: tier=0 ctx=1,048,576 probe=1.6s med=1.6s(n=5)
  ok   ProviderA/swift-lite: tier=0 ctx=1,000,000 probe=1.0s med=1.2s(n=5)
  ok   ProviderA/legacy-flash: tier=0 ctx=1,000,000 probe=20.4s med=3.2s(n=5)
  ok   ProviderB/compact-mini: tier=0 ctx=204,800 probe=2.2s med=2.2s(n=5)
  ok   ProviderB/mid-27b: tier=0 ctx=131,072 probe=1.2s med=1.2s(n=5)
  ok   ProviderA/general-v2: tier=1 ctx=1,048,576 probe=6.8s med=8.6s(n=5)
  ok   ProviderA/general-h3: tier=1 ctx=262,144 probe=0.8s med=1.5s(n=5)
  ok   ProviderC/general-v5: tier=2 ctx=1,000,000 probe=2.9s med=3.7s(n=5) HELD
  ok   ProviderE/reasoner-xl: tier=2 ctx=1,000,000 probe=3.6s med=3.6s(n=5) HELD
DRY RUN would update compression: primary=ProviderA/swift-flash,
  chain=[('ProviderB', 'compact-flash'), ('ProviderC', 'general-v5'),
         ('ProviderE', 'reasoner-xl')] (primary changed)
re-run with --apply to write it
```

Two columns matter more than the model list.

`med=` is the **median of the last 5 probes**, and it is what ranking uses — not
`probe=`, the latest one. Look at `legacy-flash`: 20.4s now, 3.2s typically.
Ranking on the latest sample would drop it several places and rewrite the config;
ranking on the median leaves it where it belongs.

`HELD` marks a model already in the route. It is compared at a discounted
latency, so a challenger has to win by a real margin rather than by noise. See
[Stability](#stability).

With `--apply`, the same run produces this diff:

```diff
 auxiliary:
   compression:
-    provider: ProviderB
-    model: compact-flash
+    provider: ProviderA
+    model: swift-flash
     timeout: 300
     fallback_chain:
-      - provider: ProviderA
-        model: quick-flash
-        base_url: https://a.example/v1
-        key_env: PROVIDERA_API_KEY
+      - provider: ProviderB
+        model: rapid-mini
+        base_url: https://b.example/v1
+        key_env: PROVIDERB_API_KEY
         api_mode: chat_completions
         timeout: 300
       - provider: ProviderC
         model: general-v5
         base_url: https://c.example/v1
         key_env: PROVIDERC_API_KEY
         api_mode: chat_completions
         timeout: 300
-      - provider: ProviderD
-        model: general-v5
+      - provider: ProviderE
+        model: reasoner-xl
         base_url: https://e.example/v1
-        key_env: PROVIDERD_API_KEY
+        key_env: PROVIDERE_API_KEY
         api_mode: chat_completions
         timeout: 300
```

Only the `auxiliary.compression` block changes. Everything else in the file —
including comments, when `ruamel.yaml` is installed — is byte-identical, and a
timestamped backup is written first.

Two details in that diff are deliberate. The chain crosses to a different
provider before taking a second model from the primary's own provider. And the
chain holds three *distinct models* — if two providers resold the same model, only
one of them would get a slot, because both would die together.

Provider and model names throughout this README are placeholders. The
latencies, context windows, error bodies and orderings are from real runs.

## What it does

Every run:

1. discovers the `(provider, model)` pairs your install can actually call, from
   `custom_providers` in `config.yaml` — models you pinned by hand, plus, for
   providers that pin none, whatever their own `/v1/models` advertises (see
   [Relays and gateways](#relays-and-gateways))
2. sends each one a real 4-token completion — not a `/v1/models` listing, which
   aggregators happily populate with models they cannot route
3. classifies failures, applies hysteresis, drops what's dead
4. rewrites the route from what's verified alive, ranked for the job
5. writes `config.yaml` safely enough to run on a timer beside other writers

Probing rather than listing is the point. A typical rejection looks like this:

```console
  skip ProviderA/fast-preview: probe failed: HTTP 429 quota temporarily paused
  skip ProviderB/legacy-chat-v4: probe failed: HTTP 503 {"code":"model_not_found",
       "message":"no available channel for model legacy-chat-v4"}
```

The 503 is the case a listing-based check misses: the model was still
advertised while no backend could serve it. That is a routing state, not a
judgement about any vendor — aggregators multiplex changing upstream capacity
and this is a normal consequence.

## Relays and gateways

Two ways people configure Hermes, and this tool has to handle both.

**Models pinned by hand** — one entry per provider, models enumerated:

```yaml
custom_providers:
  - name: ProviderA
    base_url: https://provider-a.example/v1
    key_env: PROVIDER_A_API_KEY
    models:
      fast-flash-v1: {}
      big-thinking-v1: {}
```

**A relay in front of many upstreams** — one entry, one key, models left to the
relay. Nobody enumerates sixty models by hand:

```yaml
custom_providers:
  - name: Relay
    base_url: https://relay.example/v1
    key_env: RELAY_API_KEY
    discover_models: true
```

For the second shape there is nothing in `config.yaml` to probe, so the
provider's own `/v1/models` is asked for the candidate list. Listing is only
used to find *names*; every name still gets a real completion before it can
enter the route, because being listed is not evidence of being routable.

A relay's catalogue is not all chat models — the same endpoint fronts
embeddings, speech, image and moderation. Those are filtered by name and the
reason is printed:

```console
  skip Relay/text-embedding-3-large: not a chat model (by name)
  skip Relay/whisper-large-v3: not a chat model (by name)
  skip Relay/flagship-pro: probe failed: HTTP 404 model_not_found
  ok   Relay/fast-flash-v1: tier=0 ctx=200,000 probe=0.4s med=0.4s(n=1)
```

Name-based filtering is a heuristic and wrong in both directions on unusual
names, so it applies **only** to discovered models. Anything you pinned by hand
is taken at your word.

Three practical notes:

- The listing is fetched once per `base_url` per run, so sibling providers on
  one relay do not each pay for it.
- `--max-discovered` (default 25) caps how many models one listing contributes.
  A gateway advertising 300 ids would otherwise mean 300 probes per tick, and
  ranking only ever uses the top few.
- A provider whose key is missing is never contacted. No point asking a gateway
  for its catalogue with credentials you do not have.

The honest limit: when every model reaches you through one relay, a chain of
four is four models behind one endpoint. If the relay is what breaks, the whole
chain breaks with it. Cross-provider fallback only buys you anything when there
is a genuinely separate second endpoint — so pin a direct provider alongside the
relay if you want that.

Use `--no-discover-models` to switch listing off entirely and probe only what
you pinned.

## What it is good at, and what it is not

**Good at:**

- **Nothing sits in the request path.** There is no process to keep alive, no
  port to bind, no localhost hop. If this tool crashes, is uninstalled, or its
  timer is disabled, Hermes keeps running on the last route it wrote. The
  failure mode is a stale route, not an outage.
- **It verifies routability, not advertisement.** Every candidate gets a real
  4-token completion. That is the only way to catch the case a `/v1/models`
  check cannot see: a model still listed while no backend can serve it
  (`HTTP 503 model_not_found`).
- **It uses the credentials you already have.** Your providers, your keys, your
  `.env`. No third-party account, no margin on top of your spend, no prompt
  content leaving the machine.
- **The config stays yours.** Comments and formatting survive the rewrite, edits
  by hand are not clobbered, every write leaves a timestamped backup (kept 10
  deep / 7 days, hard cap 30), and the default is a dry run that prints its
  reasoning.
- **Churn control is the part that took the work.** Hysteresis, median latency
  over a window, slot stickiness, per-slot grace. A route that flaps is worse
  than one that is slightly stale, because every write invalidates whatever is
  reading the file. The worst churn source found so far wrote **151 times in a
  200-tick replay with nothing actually failing**; after the fix, twice. Live
  write rate on the reference install today is under 10% of ticks, and every
  remaining write corresponds to a real health change.

**Not good at:**

- **A probe is a sample, not a subscription to the truth.** The blind window is
  one tick — five minutes at the recommended interval. A model that dies at
  12:01 stays in the route until 12:05. Hermes's own `fallback_chain` is what
  saves the request in between; this tool only makes sure that chain is worth
  walking.
- **A small probe cannot see a per-model quota wall.** Measured on a live
  install: 244 `HTTP 429` responses on one model in real traffic over the same
  period the 4-token probe returned `200 OK` at 1.8s. Not a bug to be fixed —
  the probe is too small to trip a limit that a 25k-token request trips
  immediately, and the outage arrives in bursts that begin and end between two
  ticks. Scheduled probing has a floor on what it can detect, and this is it.
- **It reacts per tick, never per request.** No mid-request failover, no retry
  policy, no traffic splitting, no cost-aware routing, no load balancing across
  keys. Those belong to whatever handles the request, not to a config rewriter.
- **It costs a few tokens.** Four output tokens per model per TTL window. Small,
  metered, non-zero.
- **Tiering is a heuristic.** Tiers come from substring matching on model names,
  so an unconventionally named model lands in the middle. Overridable with
  `--fast-pattern` / `--heavy-pattern`, but there is no semantic understanding
  of what a model is.
- **It writes a file that other processes also write.** The write path is
  defensive (read, verify, atomic replace, backup) and safe enough to run on a
  timer, but a second writer holding a stale copy of `config.yaml` in memory can
  still overwrite the route on its next save.
- **One relay is still one point of failure.** A four-entry chain behind a single
  endpoint is four models and one outage away from empty. See
  [Relays and gateways](#relays-and-gateways).

## Versus a router or gateway

Routers like [OpenRouter](https://openrouter.ai) (hosted),
[9Router](https://9router.com) and [LiteLLM](https://docs.litellm.ai) (both
self-hosted proxies) solve an overlapping problem, and it is worth being precise
about the overlap, because the difference is not a feature list — it is **where
the decision lives**.

A router decides per request, inside the request. This decides per tick, then
gets out of the way. Everything else follows from that.

| | this | 9Router / LiteLLM | OpenRouter |
|---|---|---|---|
| in the request path | no | yes, a local process | yes, their infrastructure |
| what it changes | your `config.yaml` | nothing — it intercepts | nothing — it intercepts |
| failure signal | scheduled probe, 4 tokens | your real traffic, plus background health checks | fleet-wide telemetry, 30s outage window |
| reacts within | one tick (5 min default) | one request | mid-request |
| mid-request failover | no — Hermes's own chain does that | yes | yes |
| if it stops running | route keeps working, goes stale | all traffic stops | all traffic stops |
| API keys | stay in your `.env` | stay on your box | theirs, or BYOK for a fee |
| prompt content | never leaves the machine | never leaves the machine | passes through them |
| extra moving parts | none | one process, one port | one network hop |
| cost | your own keys | your own keys | their margin |
| quota / spend dashboard | no | yes | yes |
| catalogue | whatever you configured | 60+ providers, OAuth pooling | 400+ models, 25+ free |
| flap control | hysteresis, median, stickiness, per-slot grace | `cooldown_time` + `allowed_fails` | inverse-square price weighting |

Three things that table understates:

- **A router is strictly better at detection.** Real traffic sees the 429 that a
  4-token probe cannot, and a 30-second window beats a five-minute tick. If your
  priority is never sending a request to a dead endpoint, the request path is
  where that belongs, and no amount of probing catches up.
- **A router cannot fix your config.** Hermes walks `fallback_providers` and
  `auxiliary.<task>.fallback_chain` natively. If those entries name a model that
  has been unroutable for a week, a proxy does not know and does not care —
  you simply never reach it. That specific failure is the one this tool exists
  for, and no proxy addresses it.
- **They compose.** Point a Hermes provider at a local 9Router or LiteLLM
  instance and this tool will happily probe and rank the models behind it. When
  you do, most of what this tool decides collapses into one `base_url`, and the
  routing intelligence moves into the proxy — which may be exactly what you
  want.

**Pick a router when** you want per-request failover, a spend dashboard, quota
pooling across subscriptions, or one endpoint for many tools. **Pick this when**
you want no extra process in front of your models, keys and prompts that never
leave the box, a config file you can still read, and a route that is correct on
disk rather than corrected in flight — and you can live with a five-minute
reaction time.

The honest summary: this is not a router and does not compete with one. It is a
janitor for a config file that would otherwise quietly rot.

## Install

```bash
pip install git+https://github.com/Machbub/hermes-aux-autoheal
```

Or clone and run in place. The only hard dependency is PyYAML, which Hermes
already requires:

```bash
git clone https://github.com/Machbub/hermes-aux-autoheal
cd hermes-aux-autoheal
python -m hermes_aux_autoheal.cli --help
```

Install `ruamel.yaml` as well if your `config.yaml` has comments. Without it the
PyYAML fallback works but **deletes every comment in the file** on write. See
[Comments](#comments).

## Usage

```bash
# see what it would do (default)
hermes-aux-autoheal

# actually write it
hermes-aux-autoheal --apply

# a different auxiliary task
hermes-aux-autoheal --task summarization --apply

# on a timer, every 5 minutes
*/5 * * * * hermes-aux-autoheal --apply --prune-backups >> ~/.hermes/logs/autoheal.log 2>&1
```

Output is silent when the route is already correct, so a cron entry only speaks
when something changed.

Exit codes, for monitoring:

| code | meaning |
|------|---------|
| 0 | route correct, or corrected |
| 1 | nothing healthy to route to — config left untouched |
| 2 | write refused (lock contention, conflicting writer, failed validation) |

## How models are ranked

For a background summarizer, cheap and fast beats smart. A frontier reasoning
model given 250k tokens to compress will often hit its own timeout — and the
result is not a worse summary, it is no summary and a stalled conversation.

Ranking order: freshly-verified first, then cheap/fast tiers, then widest
context window, then lowest probe latency. Models whose names mark them as heavy
reasoning variants sink to the bottom of the chain but stay in it as a last
resort.

Tier patterns match generic size and speed words (`mini`, `flash`, `lite`, `8b`;
`thinking`, `reason`, `ultra`) rather than vendor brand names, so they stay
useful across providers and age better. Override per run:

```bash
hermes-aux-autoheal --fast-pattern 'my-quick-model|another-fast-one' \
                    --heavy-pattern 'my-big-model'
```

None of this rates a vendor's quality. A "heavy" model is not worse; it is being
kept out of a job where its cost and latency are a liability. A model matching
neither pattern lands in the middle tier, which is a fine place to be.

The fallback chain crosses providers before it takes a second model from the
primary's provider. A chain of one provider's models dies wholesale when the
provider or its key is what broke — the exact failure this tool exists to
survive.

### The chat chain is ranked differently on purpose

The compression rules are wrong for the chat model (`model.default` +
`fallback_providers`), and the first live sync proved it: `tier_of` put a
cheap flash model (tier 0) ahead of a spare key for the user's own flagship
model (tier 2) — a capability downgrade offered before a like-for-like
replacement.

`pick_chat_chain` / `chat_slot_key` rank by *closeness to what the user
chose*: the identical model behind another key first (covers key/quota death,
capability unchanged), then tier distance from the primary, then widest
context, and finally `(provider, model)` as a deterministic tail. Origin
diversity is its own pass — a dead origin (Cloudflare 522)
takes every key on it, so the chain must leave the host. Same-model spares are
capped at `depth // 2`, across origins too: the cap protects against one model
filling the chain, regardless of how many hosts resell it. Slot stickiness is
the same latency-blind guard as the compression chain.

That tail is load-bearing, not cosmetic. The three merit components leave large
tied groups (6 of 11 candidates on the reference install), and a tie falls
through to `rank()`'s latency order, so every median crossing reshuffled the
slots: **151 writes over 200 replayed ticks, 150 of them with nothing
flapping**. With the tail, 2. It must also be immutable — `fail_streak` was
tried there and made it worse (182 writes), because a peer wobbling through its
grace period reorders the group every tick.

Eviction is a separate question from ordering, so it uses `chat_merit_key`
rather than the full key: with the tail included, any merit-equal peer with an
alphabetically earlier name displaced the incumbent, which defeated slot
stickiness entirely.

Eviction also asks whether the challenger could *actually take the slot*
(`could_be_seated`), not merely whether it out-ranks the holder. Without that
check the guard was decorative on any relay-shaped install: the primary's own
origin usually also hosts several provider labels of a nearby model, each
carrying merit `(1, tier±1, -1M)` — better than any holder on a narrower context
— while no pass can seat them, since pass 1 wants the primary's model, pass 2
wants an unused origin, and pass 3 allows one slot per model. Instrumented on the
reference pool, pass 0 seated **1 of 4 slots**; the chain was rebuilt from
scratch nearly every tick, which quietly disabled slot stickiness, the sticky
latency margin and the tie-break tail for chat. Replays at blip rates measured
from that install: **9.2% of ticks wrote before, 4.0% after**, and writes that
*demoted* a slot fell from 36 to 2.

A holder that just failed a probe keeps a mid-chain slot while it is inside its
grace window (`holder_may_hold_slot`). `ok_now` false means `strike 1` of
`--demote-streak`, not a verdict, and evicting on it cost two writes per blip —
one to demote, one to restore. Slot 0 stays strict: it is tried first, so a
suspect entry there costs a round-trip on every request until the next tick.

The CLI applies it: `--apply` writes `fallback_providers` (top-level) in the
same transaction as the auxiliary route, reading the chat primary from
`model.provider` / `model.default` — which it never rewrites. `--chat-depth`
sets the chain length.

### Identity is normalised, and that is load-bearing

Every comparison in this tool crosses a boundary: `config.yaml` is written by
hand or by a dashboard, candidates come from a provider `/v1/models` listing or
a SQLite table. The two sides disagree about spelling, so `ident_of` strips,
lowercases, and reduces the model to its bare name before comparing.

Without that, two things went wrong silently — no error, just the wrong
outcome:

- **The primary was offered as its own fallback.** Five of six real spellings
  slipped past a raw tuple comparison: a provider prefix in the model name
  (`bai/flagship-v2` vs `flagship-v2`), either field's case, stray whitespace,
  an aggregator vendor slug. A chain whose first entry is the model that just
  failed protects against nothing.
- **Incumbency lookups missed.** `route_idents`, `primary_ident`,
  `sticky_latency` and `choose_primary` all look candidates up by identity. A
  miss disables latency stickiness and leaves the incumbent primary undefended,
  and it is invisible: a lookup that finds nothing looks exactly like a
  candidate that is genuinely new.

Discovery draws the line in a different place, because it is answering a
different question. A route's identity there is `(base_url, model, key)` — the
credential included. Two labels fronting one relay with *different* keys are two
routes with independent quotas, and collapsing them deleted the "same model,
different key" spare that pass 1 selects first, before it could even be probed.
Two labels sharing one key are still one route listed twice.

## Stability

Probe-and-write on every tick is unstable in four different ways, and each needs
its own guard. On a live install before any of them existed, **130 config writes
across 245 cron ticks** — 53%.

### A failing model that keeps recovering

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

Streaks live in a health cache (`~/.hermes/.aux_autoheal_health.json`) so they
survive between cron ticks. Results are reused for `--ttl` seconds (default 600)
to keep probe traffic down; `--no-cache` forces a fresh probe of everything.

### Two healthy models trading places

Failure classification does nothing here, because nothing is failing. Two models
of the same tier and the same context window, separated only by probe latency,
will swap rank and rewrite the config on almost every tick.

The first attempt at this was hysteresis alone: require a challenger to beat an
incumbent by a margin. It did not work, and the reason is worth stating because
it is easy to repeat. The margins assumed latency wobbles by a few hundred
milliseconds. It does not — on an aggregator under changing upstream load, the
same model measured **1.3s, then 6.7s, then 42.0s inside twenty minutes**. No
threshold makes a noisy input produce a stable output.

So the input is smoothed first. Latency goes into a rolling window
(`--latency-window`, default 5 successful probes, persisted in the health cache)
and **ranking reads the median, not the latest sample**. Median rather than a
mean because these are spikes, not drift: one 42s outlier among five samples
moves a median not at all and a mean by 8s.

Only successful probes are recorded. A failure's elapsed time measures how long
the error took to arrive, and mixing the two would let a fast 401 look like a
fast model.

On top of the smoothed value, an incumbent is compared at a discounted latency,
and a challenger must beat it by **both** margins:

| flag | default | meaning |
|------|---------|---------|
| `--sticky-rel` | 0.30 | challenger must be 30% faster |
| `--sticky-abs` | 0.5 | and 0.5s faster in absolute terms |

Both, because either alone is cheap to hit by accident. At 0.4s a 30% lead is
120ms of noise; at 12s a 0.5s lead is rounding. Set either to `0` to disable.

Hysteresis applies to latency only. Tier and context window are stable
properties of a model, so a challenger that wins on either takes the slot
immediately — there is nothing noisy to smooth out. A model that fails its
probe loses the slot regardless.

Two slots need protecting, and they need different code. Route **membership** is
protected by ranking at a discount, so an outsider needs a margin to push a
member out. The **primary slot** is protected by comparing the leader against
the incumbent directly, because when both are already in the route the ranking
discount applies to both sides and cancels out.

### The same model wearing three provider labels

Also invisible to the guards above, because again nothing is failing and nothing
is even changing — the same models keep rotating through the same slots under
different names. This was 24 of those 139 writes.

Several providers may resell one model: one shared endpoint, different keys and
quotas, three entries in `custom_providers`. Giving each its own chain slot is
false diversity. If the model is retired upstream, all three die in the same
instant, and the chain that looked three deep was one model.

So the chain takes **one slot per model**, compared on the bare name so
`vendor/model` and `model` collapse to one entry. The cross-provider rule still
applies first: a different provider is preferred before backfilling with a
same-provider spare.

### Two healthy models trading one slot

The three guards above left the primary stable for hours and cut writes to
roughly 11% — and then almost every remaining write turned out to be the same
event. Over one 6.5-hour window: **12 writes, 9 of them pure chain reorders.**
The two models involved were the same tier, both advertising a 1M context
window, medians crossing every few ticks (2.1s vs 6.8s, then the reverse). The
route was rewritten to say the same thing in a different order.

`choose_primary` had a stickiness guard. `pick_chain` did not — it re-picked from
a fresh ranking every tick, so any crossing evicted the incumbent.

A chain slot now defends itself: the holder keeps its position unless a
challenger out-ranks it on **tier or context window**. Latency is deliberately
excluded. A fallback is not there to be fast, it is there to answer when the
primary stops answering, and latency is the one input too noisy to act on — the
same model measured 1.3s, 6.7s and 42.0s inside twenty minutes.

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

Latency keeps its old job: ordering candidates that compete for an **empty**
slot. It no longer evicts an incumbent from an occupied one.

### What it adds up to

Each guard was measured on the install it was written for, and the numbers moved
as the window grew — which is the point of quoting the window instead of a single
headline figure:

| Window | Ticks | Writes | Rate |
|---|---|---|---|
| before any guard | 245 | 130 | 53% |
| hysteresis alone (0.2.0) | 66 | 44 | 67% |
| median ranking, first hour (0.3.0) | 15 | 1 | 6.7% |
| median ranking, 6.5h (0.4.0) | 93 | 10 | 10.8% |
| chain defence, replayed on that same 6.5h (0.5.0) | 100 | 0 | 0% |

Two things worth reading carefully. Hysteresis alone made churn **worse** — the
margins were calibrated for noise an order of magnitude smaller than the real
thing. And the 6.7% figure was real but flattering: a longer window put the same
code at 10.8%, because rarer crossings need time to show up.

The last row is a replay, not a fresh observation: the recorded medians from that
window pushed back through both code paths. It counts writes avoided on the
contending pair, and it does not prove a steady state — only that every write in
that window came from a cause the new guard removes.

Verify it on your own install by comparing tick and write counts in the log:

```bash
grep -c 'route sync starting' autoheal.log
grep -c 'route updated'       autoheal.log
```

And check *why* it wrote, which is the part that tells you whether the route
actually changed or merely got reordered:

```bash
grep -o 'route updated ([^)]*)' autoheal.log | sort | uniq -c | sort -rn
```

## Writing config.yaml safely

Hermes writes `config.yaml` through `atomic_yaml_write` under an in-process
lock. Anything running **outside** the Hermes package — a cron job, a sync
daemon, this tool — cannot reach that lock. Two writers, no mutual exclusion,
and eventually one truncates the other's file.

`config_io.config_transaction` is the answer, and it is usable on its own if you
are writing your own Hermes helper:

```python
from hermes_aux_autoheal import config_io

with config_io.config_transaction(backup_ns='myscript') as tx:
    tx.doc['model']['default'] = 'some-model'
```

What it guarantees:

- **one writer at a time across processes** — `flock` on a shared lock path
- **re-read inside the lock**, so a mutation is always computed against current
  content, never a stale snapshot
- **atomic replace** — temp file in the same directory, `fsync`, `os.replace`.
  A crash or a full disk cannot leave a half-written config that Hermes then
  refuses to parse
- **conflict detection** — mtime is re-checked immediately before the replace.
  If a non-participating writer (the gateway) landed in between, it raises
  `ConfigConflict` rather than silently reverting their change
- **validation before commit** — the rendered YAML is re-parsed and its
  top-level key count compared against the original. A mutation that would drop
  sections is refused
- **timestamped backups**, namespaced per writer so tools don't prune each
  other's history

Nothing is written if the block raises, and nothing is written if the mutation
leaves the document byte-identical.

## Comments

With `ruamel.yaml` installed, comments and formatting survive — including the
awkward case of a comment sitting directly after the `fallback_chain` block,
which ruamel attaches to the last key of the last list item.

Without it, the PyYAML fallback works but `safe_load` → `safe_dump` silently
deletes every comment in the file. If your `config.yaml` is hand-annotated,
install ruamel.

## Options

| flag | default | purpose |
|------|---------|---------|
| `--task` | `compression` | which `auxiliary.<task>` to heal |
| `--apply` | off | actually write (default is a dry run) |
| `--verbose` | off | print every candidate and verdict |
| `--config` | `$HERMES_HOME/config.yaml` | config path |
| `--env-file` | `$HERMES_HOME/.env` | where API keys are read from |
| `--sqlite-db` | none | also read providers from a dashboard database |
| `--no-discover-models` | off | never ask a provider for its `/v1/models` listing |
| `--max-discovered` | 25 | cap on models taken from one provider listing |
| `--chain-depth` | 3 | fallback entries to keep |
| `--call-timeout` | 300 | timeout written into each route entry |
| `--probe-timeout` | 45 | health probe timeout |
| `--min-context` | 0 | skip models with a known window below this |
| `--ttl` | 600 | reuse cached probe results for this long |
| `--demote-streak` | 2 | ambiguous failures before eviction |
| `--promote-streak` | 2 | passes before a down model is trusted |
| `--sticky-rel` | 0.30 | fraction faster a challenger must be to displace |
| `--sticky-abs` | 0.5 | seconds faster it must also be |
| `--latency-window` | 5 | probes to take the median of when ranking (1 = off) |
| `--no-cache` | off | probe everything, ignore the cache |
| `--hermes-path` | `$HERMES_PACKAGE` | Hermes package path, for context windows |
| `--no-context-lookup` | off | skip context-window resolution |
| `--fast-pattern` | generic size/speed words | regex marking a name as cheap/fast |
| `--heavy-pattern` | generic reasoning words | regex marking a name as heavy |
| `--prune-backups` | off | also trim this tool's own backup history |

API keys are resolved the way Hermes does it: a provider's `key_env` if set,
otherwise the provider name uppercased with non-alphanumerics collapsed to `_`
plus `_API_KEY`. Both the process environment and `.env` are read; the
environment wins.

## Limits

The trade-offs are in [What it is good at, and what it is
not](#what-it-is-good-at-and-what-it-is-not). Mechanical details that section
does not cover:

- **Context windows come from provider metadata**, which is sometimes absent.
  Models with an unknown window are not excluded by `--min-context`, since
  dropping unknowns would reject every model on a provider that publishes none.
- **Your chosen model is never overwritten.** `model.provider` and
  `model.default` are read, never written — the chat chain is built *around*
  whatever you picked. What does get rewritten is the list beside it,
  `fallback_providers`, plus `auxiliary.<task>` routes.
- **Discovered models are filtered by name.** A chat model with an unusual name
  containing something like `guard` or `audio` is skipped; pin it by hand if you
  want it considered.
- **Probe results are cached per `(base_url, model, provider)` for `--ttl`
  seconds.** Two runs inside one TTL window see the same verdict, which is what
  makes a 5-minute timer affordable, and also means `--no-cache` is the only way
  to force a fresh look.

## Tests

```bash
python -m pytest tests/ -q
```

303 tests, run against both YAML backends — with and without `ruamel.yaml`,
since the fallback path is what most people hit first. No network: probes and
the `/v1/models` listing are stubbed, but discovery, the health state machine,
route building, and config writing all run against real files. The config writer
suite includes a genuine three-process write race.

Six behaviours worth naming, because they are easy to regress:

- **Sibling providers are probed separately.** Several providers may share one
  `base_url` (one aggregator, different keys and quotas), so the health cache is
  keyed by `provider|base_url|model`. Keying it without the provider makes them
  collide, and one sibling's verdict is read back as every sibling's — a dead key
  looks alive.
- **Cache entries migrate, they don't reset.** Upgrading a legacy 2-part key
  fans it out to each sibling instead of dropping it, because a cache miss
  silently resets the streak counters and re-enables flapping.
- **Hysteresis must not become lock-in.** A jitter-sized lead is rejected, but a
  decisive one still wins, and a failing incumbent always loses its slot.
- **Smoothing must not hide a regression.** A single spike is ignored, but when
  the median itself moves the model does lose its slot. Smoothing that swallowed
  real slowdowns would be worse than no smoothing.
- **A pinned model is never re-listed, and a keyless provider is never
  contacted.** Discovery spends a listing request only where it has nothing to
  go on and a key to go with it.
- **A chain slot holder is not evicted by latency.** Both directions of a real
  median crossing are pinned, because a guard that only holds in the ordering it
  was written against is not a guard. A holder still loses its slot to a better
  tier or a wider context window, and it always loses `chain[0]` when it fails a
  probe — further down the chain it rides out its grace window instead
  (`holder_may_hold_slot`, same rule in both chains since v0.7.2). The grace
  check is indexed by the slot the holder would OCCUPY, so promotion into the
  front slot faces the front slot's bar.

## Changelog

See [CHANGELOG.md](CHANGELOG.md).

## Credits

The `fallback_chain` config shape belongs to Hermes Agent; this tool only keeps
it honest. Hermes Agent is MIT-licensed and © 2025 Nous Research.

## License

MIT. Contains no Hermes Agent code — it reads the config format only.
