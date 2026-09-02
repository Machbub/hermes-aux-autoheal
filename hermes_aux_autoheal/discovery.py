"""Find the (provider, model) pairs this Hermes install can actually call.

Ordered by how universal the source is:

1. ``custom_providers`` in ``config.yaml`` — every Hermes user has this, so it
   is the default and needs no extra setup.
2. ``models:`` allowlists inside those providers — when a provider pins an
   explicit model map, every entry is a candidate.
3. an optional SQLite table, for installs that manage providers in a dashboard
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

DEFAULT_ENV_FILE = None            # resolved lazily against HERMES_HOME


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
             sqlite_options=None):
    """All candidates that have a usable API key, de-duplicated.

    ``(base_url, model)`` is the identity: the same model reachable through two
    provider aliases is one route, and probing it twice wastes a call.
    Config-derived candidates are yielded before SQLite ones, so when both
    describe the same route the config's richer metadata (``api_mode``,
    explicit ``key_env``) wins.
    """
    if keys is None:
        keys = load_env_keys(env_file)

    found = list(from_config(config))
    if sqlite_db:
        found.extend(from_sqlite(sqlite_db, **(sqlite_options or {})))

    seen = set()
    usable, skipped = [], []
    for cand in found:
        ident = (cand['base_url'], cand['model'])
        if ident in seen:
            continue
        seen.add(ident)
        if not cand['base_url']:
            skipped.append((cand, 'no base_url'))
            continue
        if not keys.get(cand['key_env']):
            skipped.append((cand, f'no {cand["key_env"]} in env or .env'))
            continue
        cand = dict(cand)
        cand['api_key'] = keys[cand['key_env']]
        usable.append(cand)
    return usable, skipped
