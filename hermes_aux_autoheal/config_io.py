#!/usr/bin/env python3
"""Safe config.yaml mutation for out-of-tree Hermes helpers.

Hermes itself writes ``config.yaml`` through
``hermes_cli.config.save_config`` -> ``utils.atomic_yaml_write``, holding an
in-process ``threading.RLock``. Anything running OUTSIDE the Hermes package
(a cron job, a sync daemon, this package) cannot reach that lock, so it needs
its own cross-process coordination or it will eventually interleave with a
gateway write and truncate the file.

``config_transaction`` provides that. Everything here is deliberately
dependency-light: ruamel when present, PyYAML otherwise.

Guarantees:

* one writer at a time across processes (flock on LOCK_FILE)
* the file is re-read INSIDE the lock, so a mutation is always computed
  against current content
* atomic replace (temp file in the same directory + fsync + os.replace), so a
  crash or a full disk cannot leave a half-written config that Hermes then
  refuses to parse
* mtime is re-checked immediately before the replace; if a non-participating
  writer (the gateway) landed in between, ConfigConflict is raised instead of
  silently reverting their change
* the rendered YAML is re-parsed and its top-level key count compared with the
  original before it is allowed to replace the live file
* comments survive, because ruamel round-trips them. PyYAML's safe_load ->
  safe_dump silently deletes every comment in the file.

Usage::

    from hermes_aux_autoheal import config_io

    with config_io.config_transaction(backup_ns='myscript') as tx:
        tx.doc['model']['default'] = 'your-model-name'

Nothing is written when the block raises, and nothing is written when the
mutation leaves the document byte-identical.
"""
import fcntl
import glob
import hashlib
import os
import re
import tempfile
import time
from contextlib import contextmanager

HERMES_HOME = os.environ.get('HERMES_HOME',
                             os.path.expanduser('~/.hermes'))
CONFIG_PATH = os.environ.get('HERMES_CONFIG', f'{HERMES_HOME}/config.yaml')

# One lock for EVERY out-of-tree config writer on this machine. If two helpers
# pick different lock paths the exclusion is fiction, so this default is
# intentionally boring and shared.
LOCK_FILE = os.environ.get('HERMES_CONFIG_LOCK',
                           f'{HERMES_HOME}/.config_write.lock')

BACKUP_KEEP = int(os.environ.get('HERMES_CONFIG_BACKUP_KEEP', 10))
BACKUP_MAX_AGE_DAYS = float(os.environ.get('HERMES_CONFIG_BACKUP_AGE_DAYS', 7))
# Absolute ceiling on retained backups per namespace. dedup + age pruning are
# content-dependent, so without a cap a stream of distinct recent writes grows
# unbounded (observed in the wild: 57 files in 2.6 days, 26 byte-identical).
BACKUP_HARD_CAP = int(os.environ.get('HERMES_CONFIG_BACKUP_CAP', 30))
LOCK_WAIT_SECONDS = float(os.environ.get('HERMES_CONFIG_LOCK_WAIT', 30))


class ConfigConflict(RuntimeError):
    """Another writer changed config.yaml while we were preparing a write."""


class ConfigInvalid(RuntimeError):
    """The rendered YAML failed its safety check and was not written."""


# --------------------------------------------------------------------------
# YAML handles
# --------------------------------------------------------------------------

def _yaml_rt():
    """Round-trip YAML handle, or None when ruamel is unavailable."""
    try:
        from ruamel.yaml import YAML
    except ImportError:
        return None
    y = YAML(typ='rt')
    y.preserve_quotes = True
    y.width = 4096              # never re-wrap long lines into a new shape
    y.indent(mapping=2, sequence=4, offset=2)
    return y


# Public so callers (and tests) can branch on the documented degradation:
# without ruamel, writes are correct but comments are lost.
HAS_RUAMEL = _yaml_rt() is not None


def parse(text):
    """Parse config text, preferring the comment-preserving loader.

    Returns (doc, dump_callable). ``dump_callable(doc)`` renders back to text.
    """
    y = _yaml_rt()
    if y is not None:
        import io
        doc = y.load(text) or {}

        def dump(d):
            buf = io.StringIO()
            y.dump(d, buf)
            return buf.getvalue()

        return doc, dump

    import yaml
    doc = yaml.safe_load(text) or {}

    def dump(d):
        return yaml.safe_dump(d, default_flow_style=False, sort_keys=False,
                              allow_unicode=True)

    return doc, dump


# --------------------------------------------------------------------------
# sequence replacement that keeps the comment following the block
# --------------------------------------------------------------------------

def _harvest_trailing_comment(seq):
    """Comment token trailing the last element of a block sequence.

    ruamel attaches a comment that FOLLOWS a block sequence to the last key of
    the sequence's last item, as slot 2 of that key's ``ca.items`` entry. So
    replacing the sequence wholesale deletes that comment unless it is moved.
    """
    try:
        last = seq[-1]
        items = last.ca.items
    except (AttributeError, IndexError, TypeError):
        return None
    for key in reversed(list(last.keys())):
        slot = items.get(key)
        if slot and len(slot) > 2 and slot[2] is not None:
            return slot[2]
    return None


def replace_seq(parent, key, entries):
    """Replace ``parent[key]`` with ``entries``, keeping the trailing comment.

    Falls back to a plain assignment when ruamel is not in play.
    """
    old = parent.get(key)
    token = _harvest_trailing_comment(old) if old is not None else None

    try:
        from ruamel.yaml.comments import CommentedMap, CommentedSeq
    except ImportError:
        parent[key] = entries
        return

    seq = CommentedSeq()
    for item in entries:
        seq.append(CommentedMap(item) if isinstance(item, dict) else item)
    parent[key] = seq

    if token is not None and len(seq):
        last = seq[-1]
        try:
            last_key = list(last.keys())[-1]
            last.ca.items.setdefault(last_key, [None, None, None, None])[2] = token
        except (AttributeError, IndexError):
            pass


# --------------------------------------------------------------------------
# backups
# --------------------------------------------------------------------------

def _backup_glob(ns):
    base = CONFIG_PATH
    return f'{base}.bak.{ns}.*' if ns else f'{base}.bak.*'


def _backup_paths(ns):
    """Timestamped backups for one namespace, oldest first.

    ns=None means the legacy bare ``config.yaml.bak.<ts>`` pattern, which must
    never be matched together with a namespaced one — pruning the wrong set
    deletes another script's history.
    """
    pat = re.compile(
        re.escape(CONFIG_PATH) + r'\.bak\.' + (re.escape(ns) + r'\.' if ns else '') + r'(\d+)$')
    found = []
    for path in glob.glob(_backup_glob(ns)):
        m = pat.fullmatch(path)
        if m:
            found.append((int(m.group(1)), path))
    found.sort()
    return found


def prune_backups(ns, *, keep=BACKUP_KEEP, max_age_days=BACKUP_MAX_AGE_DAYS,
                  dedup=True, hard_cap=BACKUP_HARD_CAP):
    """Trim backup history. Returns the list of removed paths.

    Policy, applied in order:

      1. the newest ``keep`` files are protected unconditionally
      2. beyond that, drop files whose content duplicates a NEWER surviving
         file (``dedup``) — repeated churn writes identical bytes
      3. beyond that, drop files older than ``max_age_days``
      4. finally, if more than ``hard_cap`` files still survive, drop the
         oldest until the count is at the cap

    Step 4 exists because steps 2-3 are content- and time-dependent: a stream of
    genuinely distinct, recent backups would otherwise grow without bound. The
    cap is the guarantee; dedup and age are the refinements.

    ``max_age_days=None`` disables the age rule. ``max_age_days=0`` means every
    unprotected file is too old.
    """
    entries = _backup_paths(ns)
    if not entries:
        return []

    removed = []
    protected = {p for _, p in entries[-keep:]} if keep else set()
    now = time.time()
    cutoff = None if max_age_days is None else now - max_age_days * 86400

    surviving = []
    seen_hashes = set()
    for ts, path in reversed(entries):          # newest first
        try:
            with open(path, 'rb') as f:
                digest = hashlib.sha256(f.read()).hexdigest()
        except OSError:
            continue

        if path in protected:
            seen_hashes.add(digest)
            surviving.append((ts, path))
            continue

        drop = False
        if dedup and digest in seen_hashes:
            drop = True
        elif cutoff is not None and ts < cutoff:
            drop = True

        if drop:
            try:
                os.remove(path)
                removed.append(path)
            except OSError:
                pass
        else:
            seen_hashes.add(digest)
            surviving.append((ts, path))

    # Hard ceiling: oldest first among the ones we would otherwise have kept,
    # never touching the protected newest ``keep``.
    if hard_cap and len(surviving) > hard_cap:
        excess = len(surviving) - hard_cap
        for ts, path in sorted(surviving)[:excess]:
            if path in protected:
                continue
            try:
                os.remove(path)
                removed.append(path)
            except OSError:
                pass

    return removed


def _write_backup(text, ns):
    path = f'{CONFIG_PATH}.bak.{ns}.{int(time.time())}'
    with open(path, 'w') as f:
        f.write(text)
    return path


# --------------------------------------------------------------------------
# atomic replace
# --------------------------------------------------------------------------

def atomic_write(path, text):
    """Replace ``path`` with ``text`` atomically, preserving its mode."""
    directory = os.path.dirname(path) or '.'
    try:
        mode = os.stat(path).st_mode & 0o777
    except OSError:
        mode = 0o644
    fd, tmp = tempfile.mkstemp(dir=directory, prefix='.cfg-', suffix='.tmp')
    try:
        with os.fdopen(fd, 'w') as f:
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
        os.chmod(tmp, mode)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def _acquire(lock_fd, wait_seconds):
    """Take the exclusive lock, polling so a stuck holder cannot hang us."""
    deadline = time.time() + wait_seconds
    while True:
        try:
            fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return True
        except OSError:
            if time.time() >= deadline:
                return False
            time.sleep(0.2)


class _Transaction:
    __slots__ = ('doc', 'text', 'path')

    def __init__(self, doc, text, path):
        self.doc = doc
        self.text = text
        self.path = path


@contextmanager
def config_transaction(*, backup_ns, path=None, keep=BACKUP_KEEP,
                       wait_seconds=LOCK_WAIT_SECONDS, prune=True):
    """Mutate config.yaml under the shared lock, atomically and validated.

    Raises ConfigConflict when the lock cannot be taken in ``wait_seconds`` or
    when another writer changed the file between our read and the replace, and
    ConfigInvalid when the render fails its sanity check. Callers that poll on
    a timer can simply let the next tick retry.
    """
    target = path or CONFIG_PATH
    os.makedirs(os.path.dirname(LOCK_FILE) or '.', exist_ok=True)

    with open(LOCK_FILE, 'w') as lock_fd:
        if not _acquire(lock_fd, wait_seconds):
            raise ConfigConflict(
                f'config write lock busy for {wait_seconds:.0f}s ({LOCK_FILE})')
        try:
            with open(target) as f:
                original = f.read()
            stat_before = os.stat(target)
            doc, dump = parse(original)
            top_keys = len(doc)

            tx = _Transaction(doc, original, target)
            yield tx

            rendered = dump(tx.doc)
            if rendered == original:
                return                      # nothing changed; no churn

            # Validate before touching the live file.
            try:
                reparsed, _ = parse(rendered)
            except Exception as exc:
                raise ConfigInvalid(f'rendered YAML does not parse: {exc}') from exc
            if not isinstance(reparsed, dict):
                raise ConfigInvalid('rendered YAML is not a mapping')
            if len(reparsed) < top_keys:
                raise ConfigInvalid(
                    f'top-level keys shrank {top_keys} -> {len(reparsed)}')

            now_stat = os.stat(target)
            if (now_stat.st_mtime_ns != stat_before.st_mtime_ns
                    or now_stat.st_size != stat_before.st_size):
                raise ConfigConflict(
                    'config.yaml changed underneath us; not overwriting')

            _write_backup(original, backup_ns)
            atomic_write(target, rendered)
            if prune:
                prune_backups(backup_ns, keep=keep)
        finally:
            fcntl.flock(lock_fd, fcntl.LOCK_UN)
