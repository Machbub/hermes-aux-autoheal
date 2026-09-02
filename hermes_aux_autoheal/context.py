"""Resolve a model's context window, when something knows it.

Ranking prefers a wider window, and ``--min-context`` filters on it, so an
unresolved window degrades both. Two sources, tried in order:

1. Hermes' own ``agent.model_metadata``, when the Hermes package is importable
   (``--hermes-path``, or ``$HERMES_PACKAGE``). This is the same table Hermes
   uses at runtime, so agreeing with it is the point.
2. The provider's ``/v1/models`` endpoint, which many OpenAI-compatible
   gateways populate with ``context_length`` or ``max_context_length``.

Both are best-effort. A window of 0 means "unknown", and unknown is never
treated as small — see ``router.build``.
"""
import contextlib
import io
import json
import os
import sys
import urllib.error
import urllib.request

_MODELS_CACHE = {}


@contextlib.contextmanager
def _quiet():
    """Swallow whatever the imported package prints.

    Hermes' metadata module logs to stdout when its optional remote refresh
    fails (e.g. ``requests`` absent), which would otherwise interleave with
    this tool's own output and, worse, corrupt it for anything parsing stdout.
    The static table it falls back to is still perfectly usable.
    """
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(buf):
        yield


def _hermes_lookup(hermes_path=None):
    """``agent.model_metadata.get_model_context_length``, or None."""
    path = hermes_path or os.environ.get('HERMES_PACKAGE')
    if path and path not in sys.path:
        sys.path.insert(0, path)
    try:
        with _quiet():
            from agent.model_metadata import get_model_context_length
    except Exception:
        return None

    def quiet_lookup(model):
        with _quiet():
            return get_model_context_length(model)

    return quiet_lookup


def _from_models_endpoint(base_url, api_key, model, *, timeout=15):
    """Read ``context_length`` from a provider's model listing."""
    listing = _MODELS_CACHE.get(base_url)
    if listing is None:
        req = urllib.request.Request(
            f'{base_url}/models',
            headers={'Authorization': f'Bearer {api_key}',
                     'User-Agent': 'hermes-aux-autoheal'})
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                body = json.loads(resp.read())
            listing = {m.get('id'): m for m in (body.get('data') or [])
                       if isinstance(m, dict)}
        except (urllib.error.URLError, ValueError, OSError, TypeError):
            listing = {}
        _MODELS_CACHE[base_url] = listing

    entry = listing.get(model) or {}
    for field in ('context_length', 'max_context_length', 'context_window',
                  'max_model_len'):
        value = entry.get(field)
        if isinstance(value, int) and value > 0:
            return value
    return 0


def make_lookup(*, hermes_path=None, use_models_endpoint=True):
    """Build the ``context_lookup`` callable ``health.evaluate`` expects."""
    hermes_fn = _hermes_lookup(hermes_path)

    def lookup(cand):
        if hermes_fn is not None:
            try:
                value = hermes_fn(cand['model'])
                if isinstance(value, int) and value > 0:
                    return value
            except Exception:
                pass
        if use_models_endpoint:
            return _from_models_endpoint(
                cand['base_url'], cand['api_key'], cand['model'])
        return 0

    return lookup
