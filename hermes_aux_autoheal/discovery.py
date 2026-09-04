"""Find the (provider, model) pairs this Hermes install can actually call.

Ordered by how universal the source is:

1. ``custom_providers`` in ``config.yaml`` — every Hermes user has this, so it
   is the default and needs no extra setup.
2. ``models:`` allowlists inside those providers — when a provider pins an
   explicit model map, every entry is a candidate.
3. the provider's own ``/v1/models`` listing, for providers that pin nothing.
   A relay or gateway fronting dozens of upstreams is configured as ONE entry
   with ``discover_models: true``; nobody enumerates sixty models by hand. See
   :func:`from_endpoint`.
4. an optional SQLite table, for installs that manage providers in a dashboard
   or admin UI. Off unless configured.

A candidate is only usable if its API key is present, so discovery resolves
key names the same way Hermes does (``key_env``, else the provider name
uppercased with non-alphanumerics collapsed to ``_`` plus ``_API_KEY``) and
reads them from the environment and from ``.env``.
"""
import json
import os
import re
import sqlite3
import urllib.error
import urllib.request

DEFAULT_ENV_FILE = None            # resolved lazily against HERMES_HOME

# A relay's listing is not all chat models: the same endpoint fronts embeddings,
# speech, image and video. Probing those with a chat completion produces a
# confusing failure rather than a useful verdict, so they are filtered by name.
# Substring match on the bare id, deliberately conservative — a false negative
# only costs one candidate, a false positive puts a TTS model in the route.
NON_CHAT_PAT = re.compile(
    r'(embed|embedding|rerank|bge-|gte-|e5-|voyage|'
    r'tts|whisper|speech|transcri|audio|voice|sonic|eleven|deepgram|'
    r'dall-e|stable-diffusion|flux|sdxl|midjourney|recraft|imagen|'
    r'veo|runway|topaz|kling|sora|'
    r'moderation|guard|classifier|ocr)',
    re.I)

# Ceiling on models taken from one listing. A gateway advertising 300 ids would
# otherwise mean 300 probes per tick. Ranking only ever uses the top few, so a
# cap costs nothing real and keeps the probe budget bounded.
DEFAULT_MAX_DISCOVERED = 25

DEFAULT_LISTING_TIMEOUT = 15

_LISTING_CACHE = {}


def env_var_name(provider_name):
    """``My Provider`` -> ``MY_PROVIDER_API_KEY`` (Hermes' own convention)."""
    return re.sub(r'[^A-Z0-9]', '_', (provider_name or '').upper()) + '_API_KEY'


def load_env_keys(env_file=None, *, include_os_environ=True):
    """Merge ``.env`` values with the process environment.

    Process environment wins: an operator exporting a key to override a stale
    ``.env`` entry should not be silently ignored.
    """
    keys = {}
    if env_file:
        try:
            with open(env_file) as f:
                for raw in f:
                    raw = raw.strip()
                    if not raw or raw.startswith('#') or '=' not in raw:
                        continue
                    k, v = raw.split('=', 1)
                    keys[k.strip()] = v.strip().strip('"').strip("'")
        except OSError:
            pass
    if include_os_environ:
        for k, v in os.environ.items():
            if k.endswith('_API_KEY') and v:
                keys[k] = v
    return keys


def _candidate(provider, model, base_url, key_env, api_mode=None):
    return {
        'provider': provider,
        'model': model,
        'base_url': (base_url or '').rstrip('/'),
        'key_env': key_env or env_var_name(provider),
        'api_mode': api_mode or 'chat_completions',
    }


def from_config(config):
    """Candidates from ``custom_providers`` in a loaded config mapping.

    Handles both provider shapes Hermes accepts:

    * a single ``model:`` field
    * a ``models:`` mapping (the allowlist written when ``discover_models`` is
      false) — every key becomes its own candidate

    Providers that pin neither are returned by :func:`pending_discovery`
    instead, to be resolved against their ``/v1/models`` listing.
    """
    out = []
    for entry in (config.get('custom_providers') or []):
        if not isinstance(entry, dict):
            continue
        name = entry.get('name')
        if not name:
            continue
        base_url = entry.get('base_url')
        key_env = entry.get('key_env')
        api_mode = entry.get('api_mode')

        models = []
        pinned = entry.get('models')
        if isinstance(pinned, dict):
            models.extend([m for m in pinned if m])
        elif isinstance(pinned, list):
            models.extend([m for m in pinned if isinstance(m, str)])
        single = entry.get('model')
        if single and single not in models:
            models.insert(0, single)

        for model in models:
            out.append(_candidate(name, model, base_url, key_env, api_mode))
    return out


def pending_discovery(config):
    """Providers with a ``base_url`` but no models pinned in config.

    These are the relay-shaped entries: one gateway, many upstreams, models
    enumerated by the gateway rather than by hand. Returned as raw provider
    dicts so the caller can decide whether to spend a listing request on them.
    """
    out = []
    for entry in (config.get('custom_providers') or []):
        if not isinstance(entry, dict):
            continue
        if not entry.get('name') or not entry.get('base_url'):
            continue
        pinned = entry.get('models')
        has_pinned = bool(entry.get('model')) or (
            isinstance(pinned, (dict, list)) and len(pinned) > 0)
        if not has_pinned:
            out.append(entry)
    return out


def list_models(base_url, api_key, *, timeout=DEFAULT_LISTING_TIMEOUT,
                use_cache=True):
    """Model ids advertised by an OpenAI-compatible ``/v1/models`` endpoint.

    Returns ``(ids, error)``. An error is returned rather than raised, and the
    caller reports it as a skip reason: a gateway that will not list its models
    is a configuration problem for the operator to see, not a crash.

    Cached per ``base_url`` for the life of the process, because sibling
    providers on one relay would otherwise each fetch the same listing.
    """
    key = base_url.rstrip('/')
    if use_cache and key in _LISTING_CACHE:
        return _LISTING_CACHE[key]

    req = urllib.request.Request(
        f'{key}/models',
        headers={'Authorization': f'Bearer {api_key}',
                 'User-Agent': 'hermes-aux-autoheal'})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = json.loads(resp.read())
    except urllib.error.HTTPError as exc:
        result = ([], f'HTTP {exc.code} from /models')
    except (urllib.error.URLError, OSError) as exc:
        result = ([], f'{type(exc).__name__} from /models: {exc}')
    except ValueError:
        result = ([], '/models did not return JSON')
    else:
        # OpenAI shape is {'data': [{'id': ...}]}; some gateways return a bare
        # list. Accept both rather than making the user care.
        rows = body.get('data') if isinstance(body, dict) else body
        if not isinstance(rows, list):
            result = ([], '/models returned no data array')
        else:
            ids = []
            for row in rows:
                mid = row.get('id') if isinstance(row, dict) else row
                if isinstance(mid, str) and mid and mid not in ids:
                    ids.append(mid)
            result = (ids, '' if ids else '/models listed no models')

    if use_cache:
        _LISTING_CACHE[key] = result
    return result


def is_chat_model(model_id):
    """Does this id look like something a chat completion can be sent to?

    Name-based, because a listing rarely says. Wrong in both directions on
    unusual names, which is why it only filters discovered models — anything a
    user pinned by hand is taken at their word.
    """
    return not NON_CHAT_PAT.search(model_id or '')


def from_endpoint(provider, api_key, *, max_models=DEFAULT_MAX_DISCOVERED,
                  timeout=DEFAULT_LISTING_TIMEOUT, use_cache=True):
    """Candidates for one relay-shaped provider, from its own listing.

    Returns ``(candidates, skipped)``. Non-chat models are filtered out and
    reported individually, so ``--verbose`` explains why a listing of forty ids
    produced twelve candidates.
    """
    name = provider.get('name')
    base_url = provider.get('base_url')
    key_env = provider.get('key_env')
    api_mode = provider.get('api_mode')

    ids, err = list_models(base_url, api_key, timeout=timeout,
                           use_cache=use_cache)
    if err:
        return [], [(_candidate(name, '*', base_url, key_env, api_mode), err)]

    out, skipped = [], []
    for mid in ids:
        cand = _candidate(name, mid, base_url, key_env, api_mode)
        if not is_chat_model(mid):
            skipped.append((cand, 'not a chat model (by name)'))
            continue
        if len(out) >= max_models:
            skipped.append((cand, f'over --max-discovered ({max_models})'))
            continue
        out.append(cand)
    return out, skipped


def from_sqlite(db_path, *, table='llm_providers',
                name_col='name', url_col='base_url', model_col='model',
                enabled_col='enabled_models', active_col='is_active'):
    """Candidates from a dashboard-style SQLite table.

    Optional by design — most installs have no such database. Every column name
    is overridable so this works against a differently-shaped schema without a
    code change. Returns [] on any error: a missing dashboard must never stop
    the config-derived candidates from being used.
    """
    out = []
    if not db_path or not os.path.exists(db_path):
        return out
    try:
        conn = sqlite3.connect(db_path, timeout=10)
        conn.row_factory = sqlite3.Row
        cols = f'{name_col}, {url_col}, {model_col}, {enabled_col}'
        where = f' WHERE {active_col} = 1' if active_col else ''
        rows = conn.execute(f'SELECT {cols} FROM {table}{where}').fetchall()
        conn.close()
    except sqlite3.Error:
        return out

    for r in rows:
        try:
            enabled = json.loads(r[enabled_col]) if r[enabled_col] else []
        except (ValueError, TypeError, IndexError, KeyError):
            enabled = []
        if not isinstance(enabled, list):
            enabled = []
        primary = r[model_col]
        if primary and primary not in enabled:
            enabled = [primary] + enabled
        for model in enabled:
            if model:
                out.append(_candidate(r[name_col], model, r[url_col], None))
    return out


def discover(config, *, sqlite_db=None, env_file=None, keys=None,
             sqlite_options=None, discover_models=True,
             max_discovered=DEFAULT_MAX_DISCOVERED,
             listing_timeout=DEFAULT_LISTING_TIMEOUT):
    """All candidates that have a usable API key, de-duplicated.

    ``(base_url, model)`` is the identity: the same model reachable through two
    provider aliases is one route, and probing it twice wastes a call.

    Sources are consulted in order of how much the user told us, and earlier
    wins on collision: models pinned in config, then a provider's own
    ``/v1/models`` listing, then SQLite. So a hand-pinned entry keeps its
    explicit ``api_mode`` and ``key_env`` even when the same model also appears
    in a listing.

    Listing requests only happen for providers that pin nothing at all
    (``discover_models=False`` disables them entirely). A provider whose key is
    missing is never contacted — no point asking a gateway for its catalogue
    with credentials we do not have.
    """
    if keys is None:
        keys = load_env_keys(env_file)

    found = list(from_config(config))
    listing_skips = []

    if discover_models:
        for provider in pending_discovery(config):
            key_env = provider.get('key_env') or env_var_name(provider['name'])
            api_key = keys.get(key_env)
            if not api_key:
                listing_skips.append((
                    _candidate(provider['name'], '*', provider['base_url'],
                               key_env, provider.get('api_mode')),
                    f'no {key_env} in env or .env'))
                continue
            cands, skips = from_endpoint(
                provider, api_key, max_models=max_discovered,
                timeout=listing_timeout)
            found.extend(cands)
            listing_skips.extend(skips)

    if sqlite_db:
        found.extend(from_sqlite(sqlite_db, **(sqlite_options or {})))

    seen = set()
    usable, skipped = [], list(listing_skips)
    for cand in found:
        if not cand['base_url']:
            skipped.append((cand, 'no base_url'))
            continue
        api_key = keys.get(cand['key_env'])
        if not api_key:
            skipped.append((cand, f'no {cand["key_env"]} in env or .env'))
            continue
        # A route's identity is what actually goes on the wire: endpoint, model,
        # and CREDENTIAL. Keying on (base_url, model) alone silently deleted
        # every spare key at a shared relay before it could be probed — exactly
        # the "same model, different key" spare that pick_chat_chain's first pass
        # exists to select, and the only kind that survives a balance=0 or a 429
        # quota pause. That pass was selecting from a pool the deduper had
        # already emptied.
        #
        # The resolved key, not the env var name or the provider label, is the
        # discriminator. Two labels pointing at one credential are the same route
        # listed twice, and collapsing them is what lets a pinned entry's
        # api_mode win over the same model discovered from a listing. Two labels
        # with different credentials are genuinely different routes with
        # independent quotas.
        #
        # Model is compared raw here: it is the literal string sent upstream, so
        # two spellings at one endpoint are not interchangeable. That is a
        # different question from router.model_id, which asks which spares are
        # genuinely diverse.
        ident = (cand['base_url'], cand['model'], api_key)
        if ident in seen:
            continue
        seen.add(ident)
        cand = dict(cand)
        cand['api_key'] = api_key
        usable.append(cand)
    return usable, skipped
