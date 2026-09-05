"""Turn a health cache into rows a dashboard can render.

The autoheal writes one JSON cache per task; that file is the only thing a
dashboard needs to answer the question people actually open a dashboard to ask:
*which of my models are broken, and what do I do about it?*

Nothing here probes, writes or blocks. It is a pure read of the cache the last
run left behind, so opening a page costs zero API calls and cannot burn quota —
a dashboard that probes on page load will exhaust a rate limit the first time
someone refreshes twice.

Three jobs, deliberately small:

* :func:`parse_key` — a cache key is a packed tuple whose length carries meaning
  (``provider|task|base_url|model``, ``provider|base_url|model``, legacy
  ``base_url|model``). A reader that assumes one shape mislabels the others.
* :func:`classify` — map a probe error to an ACTIONABLE category. "down" tells
  you nothing; ``no_balance`` means top up, ``rate_limit`` means wait,
  ``model_gone`` means untick the model, ``auth`` means fix the key.
* :func:`summarize` — fold one or more caches into ``{'problems': [...],
  'ok_count': N}``, worst first.

The categories, not the labels, are the API. Labels here are English defaults
for display; a localised UI should map from ``category`` and ignore them.

See ``DASHBOARD.md`` for how these pieces fit together with ``--sqlite-db`` and
``--exclude-file``.
"""

import json
import re

#: Task names recognised when a 3-part key is ambiguous. ``health.HealthCache.key``
#: emits ``provider|base_url|model`` when a provider is known and
#: ``task|base_url|model`` when only a task is — both have three parts. The CLI
#: always knows the provider, so provider is the default reading; pass
#: ``tasks=()`` to :func:`parse_key` to force it.
DEFAULT_TASKS = ('compression', 'vision', 'summarization')

#: Categories worth offering a "stop probing this" button for. The rest either
#: heal themselves (``rate_limit``, ``timeout``, ``upstream``) or need a
#: different fix (``auth`` — replace the key, do not hide the row).
DELETABLE = ('no_balance', 'model_gone', 'no_vision', 'unknown', 'other')

#: Display order: things a human must act on before things that pass on their own.
SEVERITY = {
    'no_balance': 0,
    'auth': 1,
    'model_gone': 2,
    'no_vision': 3,
    'upstream': 4,
    'timeout': 5,
    'rate_limit': 6,
    'unknown': 7,
    'other': 8,
}

# A probe error is echoed from an upstream response nobody here controls, so it
# is treated as hostile text: anything key-shaped is stripped before it can
# reach a browser or a log aggregator.
_SECRET_PAT = re.compile(
    r'\b(?:sk-[A-Za-z0-9_\-]{8,}'
    r'|ghp_[A-Za-z0-9]{20,}'
    r'|Bearer\s+\S+'
    r'|(?:api[-_]?key|token|password)\s*[:=]\s*\S+)',
    re.IGNORECASE)


def redact(text):
    """Replace anything key-shaped in ``text`` with ``[redacted]``."""
    return _SECRET_PAT.sub('[redacted]', str(text or ''))


def parse_key(key, tasks=DEFAULT_TASKS):
    """Split a health-cache key into ``{provider, task, base_url, model}``.

    Length carries the meaning:

    ==== ==========================================  ===================
    len  shape                                       written by
    ==== ==========================================  ===================
    4    ``provider|task|base_url|model``             task-scoped run
    3    ``provider|base_url|model``                  provider-scoped run
    3    ``task|base_url|model``                      task, no provider
    2    ``base_url|model``                           pre-scoping legacy
    ==== ==========================================  ===================

    The two 3-part shapes collide. Provider wins by default because every CLI
    path knows the provider; ``tasks`` lists the names to read as a task
    instead. Pass ``tasks=()`` to always read part 0 as a provider.

    Unknown shapes return empty strings rather than raising — a dashboard must
    survive a cache written by a future version.
    """
    parts = str(key).split('|')
    out = {'provider': '', 'task': '', 'base_url': '', 'model': ''}
    if len(parts) >= 4:
        out.update(provider=parts[0], task=parts[1],
                   base_url=parts[2], model=parts[-1])
    elif len(parts) == 3:
        if parts[0].lower() in {t.lower() for t in (tasks or ())}:
            out.update(task=parts[0], base_url=parts[1], model=parts[2])
        else:
            out.update(provider=parts[0], base_url=parts[1], model=parts[2])
    elif len(parts) == 2:
        out.update(base_url=parts[0], model=parts[1])
    elif len(parts) == 1:
        out.update(model=parts[0])
    return out


def _status(text, code):
    """True when ``text`` reports HTTP ``code``.

    Matched as ``http <code>``, never as a bare number. Providers echo a request
    id (``request id: 20260903121131601478965c955d568``) and a bare ``'404' in
    text`` finds digits inside it — which mislabels a quota error as a dead
    model, then offers a delete button for a model that was fine.
    """
    return f'http {code}' in text


def classify(err, ok=None):
    """Map a probe error to ``(category, label, hint)``.

    Order matters and is not alphabetical — the specific cases have to be tested
    before the generic status codes that also carry them:

    * a text-only model refusing an image probe arrives as HTTP 400, which a
      generic status check would file under "bad request"
    * an exhausted balance arrives as 400 **or** 429 depending on the provider
    * ``model_not_found`` arrives as 404 **or** 503 (gateway has the name, no
      backend serves it)

    ``ok=True`` with no error is not a problem row at all; callers filter those
    out before getting here (see :func:`summarize`).
    """
    text = str(err or '').lower()
    if not text:
        return ('unknown', 'Failed without a message',
                'The probe failed but the provider did not say why')

    # Vision first: permanent, and shaped like a generic 400.
    if ('do not support image' in text or 'does not support image' in text
            or 'image_url' in text or 'only text' in text):
        return ('no_vision', 'No image support',
                'Text-only model — exclude it from the vision task')
    if ('insufficient' in text or 'balance=0' in text
            or 'insufficient_user_quota' in text or 'no balance' in text):
        return ('no_balance', 'Out of credit', 'Top up this provider')
    if ('temporarily paused' in text or 'rate limit' in text
            or 'rate_limit' in text or _status(text, 429)):
        return ('rate_limit', 'Rate limited', 'Usually clears on its own')
    if ('model_not_found' in text or 'no available channel' in text
            or _status(text, 404)):
        return ('model_gone', 'Model not served',
                'Untick this model — the provider no longer routes it')
    if (_status(text, 401) or _status(text, 403) or 'unauthorized' in text
            or 'invalid api key' in text):
        return ('auth', 'Key rejected', 'Check or replace this provider key')
    if 'timeout' in text or 'timed out' in text:
        return ('timeout', 'Timeout', 'Endpoint is slow or hanging')
    if any(_status(text, c) for c in (500, 502, 503, 504)):
        return ('upstream', 'Provider-side error', 'Outside your control')
    return ('other', 'Other error', '')


def load_cache(path):
    """Read one health cache. Missing or corrupt returns ``{}``, never raises."""
    try:
        with open(path, encoding='utf-8') as f:
            data = json.load(f)
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def problems(cache, task='', tasks=DEFAULT_TASKS):
    """Unhealthy rows from one loaded cache, plus the healthy count.

    Returns ``(rows, ok_count)``.

    **Healthy means the last probe succeeded — ``ok is True`` — not
    ``state == 'up'``.** They are different questions. ``state`` only reaches
    ``'up'`` after ``promote_streak`` consecutive successes, so a model that
    just came back is ``ok=True, state='down'`` for one tick. Reading ``state``
    reports a working model as broken, which is how a dashboard loses trust:
    the user tests the model by hand, sees it answer, and stops believing the
    page.
    """
    rows, ok_count = [], 0
    for key, entry in (cache or {}).items():
        if not isinstance(entry, dict):
            continue
        if entry.get('ok') is True:
            ok_count += 1
            continue
        ident = parse_key(key, tasks=tasks)
        raw = str(entry.get('err') or '')
        category, label, hint = classify(raw, entry.get('ok'))
        rows.append({
            'task': ident['task'] or task,
            'provider': ident['provider'],
            'model': ident['model'],
            'base_url': ident['base_url'],
            'state': entry.get('state', 'unknown'),
            'category': category,
            'label': label,
            'hint': hint,
            'deletable': category in DELETABLE,
            'fail_streak': int(entry.get('fail_streak', 0) or 0),
            'error': redact(raw)[:300],
            'last_probe_ts': entry.get('ts', 0) or 0,
            # state 'up' while the last probe failed = inside the grace window;
            # it is still eligible and will recover without anyone touching it.
            'in_grace': entry.get('state') == 'up' and entry.get('ok') is not True,
        })
    return rows, ok_count


def summarize(caches, tasks=DEFAULT_TASKS):
    """Fold several caches into one report, worst first.

    ``caches`` is an iterable of ``(task_label, path)``. The task label is only
    a fallback for keys that do not carry one, so a 3-part compression key and a
    4-part vision key can share one table and still say which run found them.
    """
    rows, ok_count = [], 0
    for task, path in caches:
        cache_rows, ok = problems(load_cache(path), task=task, tasks=tasks)
        rows.extend(cache_rows)
        ok_count += ok
    rows.sort(key=lambda r: (SEVERITY.get(r['category'], 9), -r['fail_streak']))
    return {
        'problems': rows,
        'ok_count': ok_count,
        'total': ok_count + len(rows),
        'last_probe_ts': max((r['last_probe_ts'] for r in rows), default=0),
    }
