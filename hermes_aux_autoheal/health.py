"""Probe models and decide, with hysteresis, which ones the route may use.

The probe is a real 4-token completion against the provider's own endpoint.
Listing ``/v1/models`` is not enough: aggregator gateways happily advertise a
model they cannot route, and answer ``503 model_not_found`` only when you try
to use it.

Hysteresis is the part that took a real incident to get right. Probing and
writing on every tick makes a model sitting near the timeout boundary flap:
in, out, in, out — each swing rewriting config and firing a notification. So
failures are classified:

* **permanent** — ``model_not_found``, 400/401/403/404, revoked credentials.
  A verdict about the model or the key. Demote on the FIRST strike; waiting
  would keep a provably unusable entry in the route.
* **ambiguous** — timeout, generic 5xx, 429, connection reset. Could be a
  passing blip. Needs ``demote_streak`` consecutive strikes.

Recovery is symmetric: a model that was down needs ``promote_streak``
consecutive passes before it is trusted again.
"""
import json
import os
import re
import tempfile
import time
import urllib.error
import urllib.request

# A failure that is a verdict, not a blip.
PERMANENT_FAIL_PAT = re.compile(
    r'(model_not_found|does not exist|no available channel|no such model'
    r'|unknown model|not a valid model|invalid model|invalid_request_error'
    r'|unauthorized|invalid api key|permission denied'
    r'|account is not authorized)',
    re.I,
)
# 400 bad request, 401/403 credential, 404 missing. 429 and 5xx are deliberately
# absent — those are the ambiguous ones that must earn a streak.
PERMANENT_STATUS_PAT = re.compile(r'HTTP (400|401|403|404)\b')

DEFAULT_PROBE_TIMEOUT = 45.0
DEFAULT_DEMOTE_STREAK = 2
DEFAULT_PROMOTE_STREAK = 2
DEFAULT_TTL = 600


def failure_kind(err):
    """``'permanent'`` (demote now) or ``'ambiguous'`` (needs a streak)."""
    text = err or ''
    if PERMANENT_STATUS_PAT.search(text) or PERMANENT_FAIL_PAT.search(text):
        return 'permanent'
    return 'ambiguous'


def probe(base_url, model, api_key, *, timeout=DEFAULT_PROBE_TIMEOUT,
          user_agent='hermes-aux-autoheal'):
    """One tiny real completion. Returns ``(ok, latency_seconds, error_text)``.

    A 200 response with no ``choices`` counts as a failure: some gateways
    return an empty envelope for a model they cannot actually serve.
    """
    payload = json.dumps({
        'model': model,
        'messages': [{'role': 'user', 'content': 'ping'}],
        'max_tokens': 4,
        'stream': False,
    }).encode()
    req = urllib.request.Request(
        f'{base_url}/chat/completions',
        data=payload,
        headers={
            'Authorization': f'Bearer {api_key}',
            'Content-Type': 'application/json',
            'User-Agent': user_agent,
        },
    )
    t0 = time.time()
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = json.loads(resp.read())
        if not body.get('choices'):
            return False, time.time() - t0, 'no choices in response'
        return True, time.time() - t0, ''
    except urllib.error.HTTPError as exc:
        detail = ''
        try:
            detail = exc.read().decode()[:200]
        except Exception:
            pass
        return False, time.time() - t0, f'HTTP {exc.code} {detail}'
    except Exception as exc:
        return False, time.time() - t0, f'{type(exc).__name__}: {exc}'


def apply_verdict(entry, ok, err, *, demote_streak=DEFAULT_DEMOTE_STREAK,
                  promote_streak=DEFAULT_PROMOTE_STREAK):
    """Advance one candidate's up/down state machine.

    ``state`` is the committed verdict used for routing; ``ok`` is the latest
    raw probe result. The two differ exactly during a grace period, which is
    the entire point: one ambiguous failure must not evict a model that has
    been working.
    """
    entry = dict(entry)
    state = entry.get('state')
    fail = int(entry.get('fail_streak', 0))
    passes = int(entry.get('pass_streak', 0))

    if ok:
        passes += 1
        fail = 0
        if state is None:
            state = 'up'            # first sighting and it works
        elif state == 'down' and passes >= promote_streak:
            state = 'up'
    else:
        fail += 1
        passes = 0
        if (state is None
                or failure_kind(err) == 'permanent'
                or fail >= demote_streak):
            state = 'down'
        # else: stays 'up' — grace period, one strike is not a verdict

    entry.update(state=state, fail_streak=fail, pass_streak=passes)
    return entry


class HealthCache:
    """Probe results persisted between runs, so streaks survive a cron tick.

    Written atomically: a truncated cache would parse as empty, silently reset
    every streak, and re-enable the flapping this class exists to prevent.
    """

    def __init__(self, path, *, ttl=DEFAULT_TTL):
        self.path = path
        self.ttl = ttl
        self.data = self._load()

    def _load(self):
        try:
            with open(self.path) as f:
                data = json.load(f)
            return data if isinstance(data, dict) else {}
        except (OSError, ValueError):
            return {}

    @staticmethod
    def key(base_url, model, provider=None):
        """Cache key for one candidate.

        Scoped by provider name when one is given. Several providers can share
        a single ``base_url`` (one aggregator fronted by different keys and
        quotas); keying on ``base_url|model`` alone makes them collide, so the
        first probe's verdict is read back for every sibling — a dead key looks
        alive and a live one looks dead. The provider name is what
        distinguishes their credentials.

        ``provider=None`` yields the legacy 2-part key, which
        :meth:`migrate` upgrades in place.
        """
        if provider:
            return f'{provider}|{base_url}|{model}'
        return f'{base_url}|{model}'

    def migrate(self, candidates):
        """Upgrade legacy ``base_url|model`` entries to provider-scoped keys.

        Returns the number of entries copied. Without this the scoping change
        silently resets every streak (an unscoped entry becomes a cache miss),
        re-enabling the flapping hysteresis exists to prevent. A legacy entry
        fans out to each provider sharing that base_url+model; their verdicts
        then diverge on the next probe.
        """
        migrated = 0
        if not self.data:
            return 0
        for cand in candidates:
            legacy = self.key(cand['base_url'], cand['model'])
            scoped = self.key(cand['base_url'], cand['model'],
                              cand.get('provider'))
            if scoped == legacy or scoped in self.data:
                continue
            entry = self.data.get(legacy)
            if isinstance(entry, dict):
                self.data[scoped] = dict(entry)
                migrated += 1
        # Remove a legacy key only once every sibling has its own entry.
        for cand in candidates:
            legacy = self.key(cand['base_url'], cand['model'])
            scoped = self.key(cand['base_url'], cand['model'],
                              cand.get('provider'))
            if scoped != legacy and legacy in self.data and scoped in self.data:
                self.data.pop(legacy, None)
        return migrated

    def get(self, base_url, model, provider=None):
        entry = self.data.get(self.key(base_url, model, provider))
        return entry if isinstance(entry, dict) else {}

    def fresh(self, base_url, model, *, provider=None, now=None):
        entry = self.get(base_url, model, provider)
        if not entry:
            return False
        now = time.time() if now is None else now
        return (now - entry.get('ts', 0)) < self.ttl

    def record(self, base_url, model, entry, provider=None):
        self.data[self.key(base_url, model, provider)] = entry

    def save(self):
        directory = os.path.dirname(self.path) or '.'
        try:
            os.makedirs(directory, exist_ok=True)
            fd, tmp = tempfile.mkstemp(dir=directory, prefix='.health_',
                                       suffix='.tmp')
            with os.fdopen(fd, 'w') as f:
                json.dump(self.data, f, indent=1, sort_keys=True)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp, self.path)
            return True
        except OSError:
            return False


def evaluate(candidates, cache, *, timeout=DEFAULT_PROBE_TIMEOUT,
             demote_streak=DEFAULT_DEMOTE_STREAK,
             promote_streak=DEFAULT_PROMOTE_STREAK,
             context_lookup=None, now=None):
    """Probe (or reuse cached results for) every candidate.

    Returns ``(eligible, rejected)``. ``eligible`` entries carry ``ok_now``,
    ``latency``, ``context`` and ``fail_streak``; ``ok_now`` is False for a
    model inside its grace period, which callers should use to keep it out of
    the primary slot.
    """
    now = time.time() if now is None else now
    eligible, rejected = [], []

    cache.migrate(candidates)

    for cand in candidates:
        base_url, model = cand['base_url'], cand['model']
        provider = cand.get('provider')
        entry = cache.get(base_url, model, provider)

        if cache.fresh(base_url, model, provider=provider, now=now):
            ok = entry.get('ok', False)
            latency = entry.get('latency', 99.0)
            err = entry.get('err', '')
            context = entry.get('context', 0)
        else:
            ok, latency, err = probe(base_url, model, cand['api_key'],
                                     timeout=timeout)
            context = 0
            if ok and context_lookup:
                context = context_lookup(cand) or 0
            if not ok and entry.get('context'):
                context = entry['context']      # keep last known window
            entry = apply_verdict(entry, ok, err,
                                  demote_streak=demote_streak,
                                  promote_streak=promote_streak)
            entry.update(ok=ok, latency=round(latency, 2), err=err,
                         context=context, ts=now)
            cache.record(base_url, model, entry, provider)

        state = entry.get('state', 'up' if ok else 'down')
        if state != 'up':
            reason = f'probe failed: {err}' if not ok else 'held down pending recovery'
            rejected.append((cand, reason))
            continue

        enriched = dict(cand)
        enriched.update(ok_now=bool(ok), latency=latency, context=context,
                        fail_streak=entry.get('fail_streak', 0))
        eligible.append(enriched)

    return eligible, rejected
