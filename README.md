# hermes-aux-autoheal

[![tests](https://github.com/Machbub/hermes-aux-autoheal/actions/workflows/tests.yml/badge.svg)](https://github.com/Machbub/hermes-aux-autoheal/actions/workflows/tests.yml)

Keeps [Hermes Agent](https://github.com/NousResearch/hermes-agent)'s auxiliary
task routes pointed at models that actually answer.

Third-party project. Not affiliated with or endorsed by Nous Research.

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

That config is static. You write it once, and nothing ever checks whether those
entries still work. Aggregator gateways retire models without warning, keys get
revoked, providers go down. When that happens the route keeps naming a corpse,
and you find out when a background task fails — for compression, that surfaces
as a conversation that stalls or dies right when it grew long enough to need
compacting.

Hermes' own `fallback_chain` is reactive: it moves on after a call fails. What's
missing is anyone checking, ahead of time, whether the names in that list are
still real.

## What this does

Every run:

1. discovers the `(provider, model)` pairs your install can actually call, from
   `custom_providers` in `config.yaml` (plus, optionally, a dashboard database)
2. sends each one a real 4-token completion — not a `/v1/models` listing, which
   aggregators happily populate with models they cannot route
3. classifies failures, applies hysteresis, drops what's dead
4. rewrites the route from what's verified alive, ranked for the job
5. writes `config.yaml` safely enough to run on a timer beside other writers

```console
$ hermes-aux-autoheal --task compression --verbose
  skip ProviderA/fast-preview: probe failed: HTTP 429 quota temporarily paused
  skip ProviderB/legacy-chat-v4: probe failed: HTTP 503 {"code":"model_not_found",
       "message":"no available channel for model legacy-chat-v4"}
  ok   ProviderA/swift-8b: tier=0 ctx=1,000,000 probe=7.8s
  ok   ProviderA/compact-mini: tier=0 ctx=204,800 probe=2.3s
  ok   ProviderA/mid-27b: tier=0 ctx=131,072 probe=3.5s
  ok   ProviderB/reasoner-xl: tier=2 ctx=1,000,000 probe=4.4s
DRY RUN would update compression: primary=ProviderA/swift-8b,
  chain=[('ProviderB', 'reasoner-xl'), ('ProviderA', 'compact-mini'),
         ('ProviderA', 'mid-27b')] (primary changed)
re-run with --apply to write it
```

Provider and model names above are placeholders over a real run — the latencies,
context windows and error bodies are what actually came back. The 503 is the
interesting one: the model was still listed in that endpoint's `/v1/models`
while no backend could serve it, which is exactly the failure a listing-based
check misses. That is a routing state, not a judgement about any vendor;
aggregators multiplex changing upstream capacity and this is a normal
consequence.

Dry run is the default. Nothing writes your config until you pass `--apply`.

## Install

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

Silent when the route is already correct, so a cron entry only speaks when
something changed.

Exit codes, for monitoring:

| code | meaning |
|------|---------|
| 0 | route correct, or corrected |
| 1 | nothing healthy to route to — config left untouched |
| 2 | write refused (lock contention, conflicting writer, failed validation) |

## How models are ranked

For a background summarizer, cheap and fast beats smart. A frontier reasoning
model given 250k tokens to compress will often hit its own timeout — the result
isn't a worse summary, it's no summary and a stalled conversation.

So ranking is: freshly-verified first, then cheap/fast tiers, then widest
context window, then lowest probe latency. Models whose names mark them as
heavy reasoning variants sink to the bottom of the chain but stay in it as a
last resort.

The defaults match generic size and speed descriptors (`mini`, `flash`, `lite`,
`8b`; `thinking`, `reason`, `ultra`) rather than vendor brand names, so they
stay useful across providers and age better. Override them per-run:

```bash
hermes-aux-autoheal --fast-pattern 'my-quick-model|another-fast-one' \
                    --heavy-pattern 'my-big-model'
```

Nothing here rates a vendor's quality. A "heavy" model is not worse; it is
being kept out of a job where its cost and latency are a liability. A model
matching neither pattern lands in the middle tier, which is a fine place to be.

The fallback chain deliberately crosses providers before it takes a second
model from the primary's provider. A chain of one provider's models dies
wholesale when the provider or its key is what broke — which is the exact
failure this tool exists to survive.

## Hysteresis

This is the part that took a real incident to get right. Probe-and-write on
every tick makes a model sitting near the timeout boundary flap: in, out, in,
out, each swing rewriting config and firing a notification. Observed in the
wild: one model entered and left a chain four times in 2.5 hours, having
answered a probe in 23s against a 30s limit.

Failures are therefore classified:

- **permanent** — `model_not_found`, 400/401/403/404, revoked credentials.
  A verdict about the model or the key, so it demotes on the **first** strike.
  Waiting would keep a provably unusable entry in the route.
- **ambiguous** — timeout, generic 5xx, 429, connection reset. Could be a
  passing blip, so it needs `--demote-streak` consecutive strikes (default 2).

Recovery is symmetric: a model that was down needs `--promote-streak`
consecutive passes before it's trusted again.

While a model is inside its grace period it stays in the chain but is barred
from the primary slot.

Streaks are persisted in a health cache (`~/.hermes/.aux_autoheal_health.json`),
so they survive between cron ticks. Results are reused for `--ttl` seconds
(default 600) to keep probe traffic down; `--no-cache` forces a fresh probe of
everything.

## Writing config.yaml safely

Hermes writes `config.yaml` through `atomic_yaml_write` under an in-process
lock. Anything running **outside** the Hermes package — a cron job, a sync
daemon, this tool — cannot reach that lock. Two writers, no mutual exclusion,
and eventually one truncates the other's file.

`config_io.config_transaction` is the answer, and it's usable on its own if
you're writing your own Hermes helper:

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
| `--chain-depth` | 3 | fallback entries to keep |
| `--call-timeout` | 300 | timeout written into each route entry |
| `--probe-timeout` | 45 | health probe timeout |
| `--min-context` | 0 | skip models with a known window below this |
| `--ttl` | 600 | reuse cached probe results for this long |
| `--demote-streak` | 2 | ambiguous failures before eviction |
| `--promote-streak` | 2 | passes before a down model is trusted |
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
  gone at 12:03. This narrows the window; it does not close it. The
  `fallback_chain` is still what saves an in-flight call.
- **Probing costs tokens.** Four output tokens per model per TTL window. Small,
  but not zero on a metered key.
- **Ranking is heuristic.** Tiers come from substring matching on model names,
  so a model named unconventionally lands in the middle tier. It won't be wrong
  so much as unopinionated.
- **Context windows come from provider metadata**, which is sometimes absent.
  Models with an unknown window are not excluded by `--min-context`, since
  dropping unknowns would reject every model on a provider that publishes none.
- **This only heals `auxiliary.*` routes.** Your chat model
  (`model.provider` / `model.default`) is never touched. If that's what you
  want, other projects do that.

## Tests

```bash
python -m pytest tests/ -q
```

59 tests, run against both YAML backends (with and without `ruamel.yaml`,
since the fallback path is what most people hit first). No network: probes and
the `/v1/models` listing are stubbed, but discovery, the health state machine,
route building, and config writing all run against real files. The config
writer suite includes a genuine three-process write race.

## Credits

The `fallback_chain` config shape belongs to Hermes Agent; this tool only keeps
it honest. Hermes Agent is MIT-licensed and © 2025 Nous Research.

## License

MIT. Contains no Hermes Agent code — it reads the config format only.
