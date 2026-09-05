"""Never-probe list: models the operator has permanently ruled out.

Health demotion answers "is this model working right now?". This answers a
different question: "should we spend an API call asking at all?". A model whose
provider account has no credit, or that the gateway has stopped serving, will
fail every probe forever — probing it each TTL window is pure waste, and it
keeps a red row on every dashboard that reads the health cache.

The list is a plain JSON file so whatever writes it (a dashboard button, an
editor, a deploy script) needs no library:

.. code-block:: json

    {"version": 1,
     "entries": [{"provider": "vendorx", "model": "chat-v4",
                  "task": "vision", "reason": "no_balance"}]}

A bare list is accepted too, either of the same objects or of
``"provider/model"`` strings. ``task`` is ``"*"`` (or absent) for every task,
or one task name to exclude a model from vision while leaving it usable for
compression — a text-only model is exactly that case.

Matching is case-insensitive on both legs: a provider label is operator-typed,
and the same model id can arrive capitalised differently from a ``/v1/models``
listing than from ``config.yaml``.
"""

import json


def _norm(value):
    return (value or '').strip().lower()


def parse(data):
    """Normalise loaded JSON into a list of ``{'provider','model','task'}``.

    Unknown shapes yield an empty list rather than raising: an exclude list is
    an optimisation, and a malformed one must not stop the tool from running.
    """
    entries = data.get('entries') if isinstance(data, dict) else data
    if not isinstance(entries, list):
        return []
    out = []
    for item in entries:
        if isinstance(item, dict):
            provider, model = item.get('provider'), item.get('model')
            task = item.get('task') or '*'
        elif isinstance(item, str) and '/' in item:
            provider, _, model = item.partition('/')
            task = '*'
        else:
            continue
        if provider and model:
            out.append({'provider': provider, 'model': model, 'task': task})
    return out


def load(path):
    """Read an exclude file. Missing or unreadable file means "exclude nothing"."""
    if not path:
        return []
    try:
        with open(path, encoding='utf-8') as f:
            return parse(json.load(f))
    except (OSError, ValueError):
        return []


def parse_pairs(pairs):
    """Turn ``['vendorx/chat-v4', ...]`` CLI arguments into entries."""
    out = []
    for pair in pairs or []:
        provider, _, model = str(pair).partition('/')
        if provider and model:
            out.append({'provider': provider, 'model': model, 'task': '*'})
    return out


def is_excluded(provider, model, entries, task='*'):
    """True when this candidate must not be probed for ``task``."""
    prov, mod, tsk = _norm(provider), _norm(model), _norm(task)
    for entry in entries:
        if _norm(entry.get('provider')) != prov:
            continue
        if _norm(entry.get('model')) != mod:
            continue
        entry_task = _norm(entry.get('task')) or '*'
        if entry_task in ('*', '', tsk):
            return True
    return False


def split(candidates, entries, task='*'):
    """Split candidates into ``(allowed, excluded)``.

    ``excluded`` carries ``(candidate, reason)`` pairs so the caller can report
    them the same way it reports health rejections.
    """
    if not entries:
        return list(candidates), []
    allowed, excluded = [], []
    for cand in candidates:
        if is_excluded(cand.get('provider'), cand.get('model'), entries, task=task):
            excluded.append((cand, 'excluded by operator (never probed)'))
        else:
            allowed.append(cand)
    return allowed, excluded
