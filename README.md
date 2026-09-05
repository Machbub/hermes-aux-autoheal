# hermes-aux-autoheal

[![tests](https://github.com/Machbub/hermes-aux-autoheal/actions/workflows/tests.yml/badge.svg)](https://github.com/Machbub/hermes-aux-autoheal/actions/workflows/tests.yml)
[![release](https://img.shields.io/github/v/release/Machbub/hermes-aux-autoheal)](https://github.com/Machbub/hermes-aux-autoheal/releases)
[![python](https://img.shields.io/badge/python-3.9%2B-blue)](https://www.python.org/downloads/)
[![license](https://img.shields.io/github/license/Machbub/hermes-aux-autoheal)](LICENSE)
[![works with](https://img.shields.io/badge/works%20with-Hermes%20Agent-5B4EE9)](https://github.com/NousResearch/hermes-agent)

Keeps [Hermes Agent](https://github.com/NousResearch/hermes-agent)'s auxiliary
task routes pointed at models that actually answer.

Third-party project. Not affiliated with or endorsed by Nous Research.

It exists because a long conversation died mid-compaction: the model named under
`auxiliary.compression` had been retired weeks earlier, was still listed in the
endpoint's `/v1/models`, and the two fallbacks behind it had gone stale the same
way. Nothing re-checks that list — so this does, on a timer.

## The problem

Hermes lets you pin a provider/model per auxiliary task (context compression,
summarization, vision) plus a `fallback_chain` of runners-up:

```yaml
auxiliary:
  compression:
    provider: SomeProvider
    model: some-fast-model
    fallback_chain:
      - provider: OtherProvider
        model: other-model
```

That config is static. Nothing ever re-checks it. Models get retired without
warning, keys get revoked, providers go down — and the route keeps naming a
corpse. You find out when a background task fails, which for compression means
a conversation that stalls exactly when it grew long enough to need compacting.

Hermes' own `fallback_chain` is reactive: it moves on *after* a call fails.
Nobody checks beforehand whether the names in the list are still real.

The chain also ships no default and appears in no generated config, so every
entry in it was typed by hand at some point — against whatever the provider
offered that week.

## What this does

Every run:

1. discovers the `(provider, model)` pairs your install can actually call, from
   `custom_providers` in `config.yaml` (plus, optionally, a dashboard database)
2. sends each one a real 4-token completion — not a `/v1/models` listing, which
   can advertise models no backend will serve
3. classifies failures, applies hysteresis, drops what's dead
4. rewrites the route from what's verified alive, ranked for the job
5. writes `config.yaml` safely enough to run on a timer beside other writers

```console
$ hermes-aux-autoheal --task compression --verbose
  skip ProviderA/fast-preview: probe failed: HTTP 429 quota temporarily paused
  skip ProviderB/legacy-chat-v4: probe failed: HTTP 503 model_not_found
  ok   ProviderA/swift-8b: tier=0 ctx=1,000,000 probe=7.8s
DRY RUN would update compression: primary=ProviderA/swift-8b
re-run with --apply to write it
```

The 503 is the case a listing-based check misses: the model was still
advertised while no backend could serve it. That is a routing state, not a
judgement about any vendor.

Dry run is the default. Nothing writes your config until you pass `--apply`.

## Vision routes

`--task vision` heals `auxiliary.vision` the same way, with one critical
difference: **the probe carries a real image**, not just text.

A text-only model answers a `'ping'` probe with a happy 200 — that is why the
plain probe would certify it for a vision route, and why a vision route built
from it fails on every image the user sends. The vision probe is a tiny 16×16
PNG in a multimodal payload:

- a model that accepts images answers it like any other completion;
- a text-only model refuses it with a `400` ("Model do not support image
  input"), which is classified as a **permanent** verdict — demoted on the
  first strike, never routed for vision;
- health-cache verdicts are scoped **per task**, so a model that passed the
  text probe for `compression` does not carry that verdict into the vision
  route (or back).

```console
$ hermes-aux-autoheal --task vision --verbose
  skip ProviderA/text-chat-8b: probe failed: HTTP 400 Model do not support image input
  ok   ProviderA/multimodal-9b: tier=1 ctx=1,000,000 probe=3.1s
DRY RUN would update vision: primary=ProviderA/multimodal-9b
re-run with --apply to write it
```

Why an explicit route at all: Hermes' `auxiliary.vision` left unset routes
images to the *main chat model*, so the vision backend changes whenever the
user switches chat models, and a text-only chat model silently takes over
image duty. Writing `auxiliary.vision.provider` + `model` pins vision to a
verified-capable backend; the `fallback_chain` written alongside it is what
saves a call when that backend is down.

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
provider's own `/v1/models` is asked for candidate names. Every name still gets
a real completion before it can enter the route — being listed is not evidence
of being routable.

A relay's catalogue is not all chat models: the same endpoint fronts
embeddings, speech, image and moderation. Those are filtered by name:

```console
  skip Relay/text-embedding-3-large: not a chat model (by name)
  skip Relay/flagship-pro: probe failed: HTTP 404 model_not_found
  ok   Relay/fast-flash-v1: tier=0 ctx=200,000 probe=0.4s
```

Name filtering is a heuristic, so it applies **only** to discovered models.
Anything you pinned by hand is taken at your word.

Practical notes:

- The listing is fetched once per `base_url` per run, so sibling providers on
  one relay do not each pay for it.
- `--max-discovered` (default 25) caps how many models one listing contributes.
- A provider whose key is missing is never contacted.
- `--no-discover-models` switches listing off entirely; only pinned models are
  probed.
- `--exclude-file` / `--exclude` carry a never-probe list: models ruled out
  permanently (dead account, gone from the gateway, text-only in a vision job).
  They cost nothing — filtered before any API call — and never occupy a health
  row again. Task-scoped entries (`"task": "vision"`) block only that task.

The honest limit: when every model reaches you through one relay, a chain of
four is four models behind one endpoint. If the relay breaks, the chain breaks
with it — cross-provider fallback only helps when a genuinely separate second
endpoint exists.

## Install

Tested against **Hermes Agent v0.20.6**, reading these config keys:

| key | used for |
|---|---|
| `custom_providers` | which providers exist, their `base_url` and key env var |
| `auxiliary.<task>` | the route this tool writes: `provider`, `model`, `fallback_chain` |
| `model` / `provider` | your chosen chat model — read to rank spares, never overwritten |

If your install nests these differently, `--dry-run` will say so before anything
is written — it prints the route it would produce and exits without touching the
file.

```bash
pip install git+https://github.com/Machbub/hermes-aux-autoheal
```

Or clone and run in place — the only hard dependency is PyYAML, which Hermes
already requires:

```bash
git clone https://github.com/Machbub/hermes-aux-autoheal
cd hermes-aux-autoheal
python -m hermes_aux_autoheal.cli --help
```

Install `ruamel.yaml` too if you keep comments in `config.yaml`. Without it the
PyYAML fallback works but deletes every comment on write. See
[Writing config safely](#writing-config-safely).

## Usage

```bash
hermes-aux-autoheal                            # dry run (default)
hermes-aux-autoheal --apply                    # actually write
hermes-aux-autoheal --task summarization --apply
# on a timer, every 5 minutes
*/5 * * * * hermes-aux-autoheal --apply --prune-backups >> ~/.hermes/logs/autoheal.log 2>&1
```

Silent when the route is already correct, so a cron entry only speaks when
something changed. Exit codes, for monitoring:

| code | meaning |
|------|---------|
| 0 | route correct, or corrected |
| 1 | nothing healthy to route to — config left untouched |
| 2 | write refused (lock contention, conflicting writer, failed validation) |

### Notifications

If the cron job's output goes somewhere a human reads — a chat, an email, a
pager — add `--quiet-routine`:

```bash
*/5 * * * * hermes-aux-autoheal --apply --quiet-routine --prune-backups
```

A healthy tick then prints nothing at all, and only two things still speak: a
chain down to its last entry, and an outright failure. Without it, every routine
primary swap arrives as a message; after a week of those, the reader has learned
to ignore the job, which means the one message that mattered gets ignored too.
Dry runs always print — someone is waiting for the answer.

## Building a dashboard on this

The health cache is a plain JSON file, and `hermes_aux_autoheal.report` turns it
into rows worth showing someone:

```python
from hermes_aux_autoheal import report

out = report.summarize([('compression', '~/.hermes/.aux_autoheal_health.json')])
for row in out['problems']:
    print(row['provider'], row['model'], row['category'], row['hint'])
```

Each row carries an actionable `category` (`no_balance` → top up, `rate_limit` →
wait, `model_gone` → untick the model, `auth` → replace the key) instead of a
bare "down". Opening such a page costs zero API calls: autoheal probes on its
timer and the page reads what it left behind.

[DASHBOARD.md](DASHBOARD.md) is the integration contract — which side owns which
file, and the five mistakes that are easy to make.
[examples/dashboard/](examples/dashboard/) is a working one-file reference
implementation.

## How models are ranked

For a background summarizer, cheap and fast beats smart. A frontier reasoning
model given 250k tokens to compress will often hit its own timeout — the result
is not a worse summary, it is no summary and a stalled conversation.

Ranking order: freshly-verified first, then cheap/fast tiers, then widest
context window, then lowest probe latency. Heavy reasoning variants sink to the
bottom of the chain but stay in it as a last resort. Tier patterns match
generic size and speed words (`mini`, `flash`, `lite`, `8b`; `thinking`,
`reason`, `ultra`) rather than vendor brand names, so they stay useful across
providers. Override per run with `--fast-pattern` / `--heavy-pattern`.

Nothing here rates a vendor's quality — a "heavy" model is not worse, it is
kept out of a job where its cost and latency are a liability.

The chain crosses providers before taking a second model from the primary's
provider: a chain of one provider's models dies wholesale when that provider or
its key is what broke.

The chat chain (`fallback_providers`) is ranked differently on purpose. A cheap
flash model is not offered ahead of a spare key for the user's own flagship
model — closeness to what you chose comes first: the identical model behind
another key (covers key/quota death, capability unchanged), then tier distance,
then widest context. Your chosen model is read, never overwritten; the chain is
built *around* it.

Stability guards keep the route from flapping: a model near the timeout
boundary gets hysteresis (`--demote-streak` / `--promote-streak`), latency is
smoothed through a median window (spikes of 1.3s → 6.7s → 42s inside twenty
minutes are real), a chain slot holder defends its position against latency but
yields to a better tier or wider context, and one model takes one slot no
matter how many providers resell it. The worst churn source found wrote 151
times in a 200-tick replay with nothing actually failing; after the fix, twice.

Six churn sources were found this way, one fix made things measurably worse
before it was replaced, and every rate is quoted with the window it was measured
over — [STABILITY.md](STABILITY.md) has the replay tables, the attempts that
failed, and how a churn fix gets verified here.

## How this differs from a proxy or router

A router or proxy sits in the request path and decides per request: it
intercepts traffic and moves a failing call to a live model. This tool sits
outside the request path and decides per tick: it probes what you configured,
then rewrites `config.yaml` so the route is correct on disk. Neither replaces
the other.

- A proxy is strictly better at detection — real traffic sees the 429 that a
  probe cannot, and it reacts in one request rather than one tick.
- A proxy cannot fix a stale config. Hermes walks `fallback_providers` and
  `auxiliary.<task>.fallback_chain` natively; if those entries name a model
  that is gone, the proxy only sees traffic never reaching it. Keeping those
  names real is what this tool exists for.
- They work together. Point a Hermes provider at a local proxy and this tool
  will probe and rank the models behind it. The proxy becomes the single
  `base_url`, routing moves into it, and this tool's job shrinks to keeping
  that one entry healthy — which may be exactly what you want.

Use a proxy when you want per-request failover and a single endpoint for many
tools. Use this when you want no extra process in front of your models, a
config file you can still read, and a route that is correct on disk rather
than corrected in flight.

## Writing config safely

Hermes writes `config.yaml` through `atomic_yaml_write` under an in-process
lock. Anything running **outside** the Hermes package — a cron job, a sync
daemon, this tool — cannot reach that lock, so this tool coordinates through
`config_io.config_transaction`, which is usable on its own:

```python
from hermes_aux_autoheal import config_io

with config_io.config_transaction(backup_ns='myscript') as tx:
    tx.doc['model']['default'] = 'some-model'
```

It guarantees:

- **one writer at a time across processes** — `flock` on a shared lock path
- **re-read inside the lock**, so a mutation is always computed against current
  content, never a stale snapshot
- **atomic replace** — temp file, `fsync`, `os.replace`. A crash or full disk
  cannot leave a half-written config
- **conflict detection** — mtime is re-checked before the replace; a
  non-participating writer landing in between raises `ConfigConflict` rather
  than being silently reverted
- **validation before commit** — the rendered YAML is re-parsed and its
  top-level key count compared against the original
- **timestamped backups**, namespaced per writer so tools don't prune each
  other's history

Nothing is written if the block raises, and nothing is written if the mutation
leaves the document byte-identical.

## Options

| flag | default | purpose |
|------|---------|---------|
| `--task` | `compression` | which `auxiliary.<task>` to heal (`vision` probes with an image so text-only models are never routed) |
| `--apply` | off | actually write (default is a dry run) |
| `--verbose` | off | print every candidate and verdict |
| `--config` | `$HERMES_HOME/config.yaml` | config path |
| `--env-file` | `$HERMES_HOME/.env` | where API keys are read from |
| `--sqlite-db` | none | also read providers from a dashboard database |
| `--quiet-routine` | off | for cron: say nothing on a healthy tick (see [Notifications](#notifications)) |
| `--exclude-file` | — | JSON file of `(provider, model)` pairs to never probe |
| `--exclude` | — | one `PROVIDER/MODEL` pair to never probe (repeatable) |
| `--no-discover-models` | off | never ask a provider for its `/v1/models` listing |
| `--max-discovered` | 25 | cap on models taken from one provider listing |
| `--chain-depth` | 3 | fallback entries to keep |
| `--chat-depth` | 4 | entries to keep in the chat model's `fallback_providers` |
| `--no-chat-chain` | off | never touch the top-level `fallback_providers` |
| `--call-timeout` | 300 | timeout written into each route entry |
| `--probe-timeout` | 45 | health probe timeout |
| `--min-context` | 0 | skip models with a known window below this |
| `--ttl` | 600 | reuse cached probe results for this long |
| `--cache` | `$HERMES_HOME/.aux_autoheal_health.json` | health cache path |
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

Worth knowing before you rely on it:

- **A probe is a sample, not a guarantee.** A model can pass at 12:00 and be
  gone at 12:03. This narrows the window; the `fallback_chain` is still what
  saves an in-flight call.
- **A vision probe is billed as a multimodal request, not a text one.** The
  image itself is tiny — a 16×16 PNG, well under a kilobyte — so the cost per
  model per probe is close to nothing. But providers can price image input on a
  different meter than the 4-token text probe, so check your rates before
  pointing `--task vision` at a metered key. Each model is probed at most once
  per `--ttl` (default 600s), no matter how often you run the tool.
- **A small probe cannot see a per-model quota wall.** Measured on a live
  install: **441 `HTTP 429` responses in real traffic** on one model while the
  4-token probe kept returning `200 OK`. The probe is too small to trip a limit
  a real request trips immediately — scheduled probing has a floor. Paired
  log evidence in [STABILITY.md](STABILITY.md#the-quota-wall-a-probe-cannot-see).
- **It reacts per tick, never per request.** No mid-request failover, no retry
  policy, no traffic splitting.
- **Probing costs tokens.** Four output tokens per model per TTL window (text
  tasks). Small, but not zero on a metered key.
- **Tiering is a heuristic.** Tiers come from substring matching on model
  names; an unconventionally named model lands in the middle. Overridable, but
  there is no semantic understanding.
- **Context windows come from provider metadata**, which is sometimes absent.
  Models with an unknown window are not excluded by `--min-context`.
- **Your chosen model is never overwritten.** `model.provider` and
  `model.default` are read, never written — only `fallback_providers` and
  `auxiliary.<task>` routes are rewritten.
- **One relay is still one point of failure.** A four-entry chain behind a
  single endpoint is four models and one outage away from empty.

## Tests

```bash
python -m pytest tests/ -q
```

423 tests, run against both YAML backends (with and without `ruamel.yaml`). No
network: probes and the `/v1/models` listing are stubbed, but discovery, the
health state machine, route building, and config writing all run against real
files — including a genuine three-process write race.

16 of those cover [`examples/dashboard/`](examples/dashboard/) and skip unless
FastAPI is installed (`pip install -e '.[dashboard]'`); a cron tool's test run
should not require a web framework.

26 check the documentation itself: every relative link and every `#anchor` in
these markdown files has to resolve, because a dead anchor is invisible — the
page loads and the link scrolls nowhere. Run it alone for a readable report:

```bash
python tests/doc_links.py
```

The behaviours that are easy to regress are named individually, with the reason
each test exists, in
[STABILITY.md](STABILITY.md#behaviours-the-tests-pin) — along with the procedure
for verifying a churn fix, which is replay against the released code, never a
hand-built fixture.

## Credits

`auxiliary.<task>` and the `fallback_chain` shape inside it are Hermes Agent's
own config surface — Hermes reads that chain in `agent/auxiliary_client.py` and
falls through it when an auxiliary call fails. This tool only keeps the entries
honest; it invents no config of its own. Hermes Agent is MIT-licensed and
© 2025 Nous Research.

## License

MIT. Contains no Hermes Agent code — it reads the config format only.
