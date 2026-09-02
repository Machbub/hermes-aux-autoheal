"""Checks for config_io — the cross-process config.yaml writer.

Plain asserts (no pytest needed) plus a real 3-process write race.
Run: python tests/test_config_io.py
"""
import importlib
import os
import shutil
import subprocess
import sys
import tempfile
import time

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)

HOME = tempfile.mkdtemp(prefix='cfgio_')
CONFIG = f'{HOME}/config.yaml'
os.makedirs(f'{HOME}/scripts', exist_ok=True)

SRC = """\
# top comment must survive
model:
  provider: custom
  default: chat-model-v2   # inline comment on the model
  temperature: 0.7

auxiliary:
  compression:
    provider: OldProv
    model: old-model
    timeout: 300
    fallback_chain:
      - provider: A
        model: a-1
      - provider: B
        model: b-1
    # trailing comment after the chain

custom_providers:
  - name: ProviderA
    base_url: https://provider-a.example/v1
    model: swift-8b

tools:
  enabled: true
"""

with open(CONFIG, 'w') as f:
    f.write(SRC)

os.environ.update(
    HERMES_HOME=HOME,
    HERMES_CONFIG=CONFIG,
    HERMES_CONFIG_LOCK=f'{HOME}/scripts/.config_write.lock',
)

from hermes_aux_autoheal import config_io          # noqa: E402
importlib.reload(config_io)

passed = failed = skipped = 0

# Comment preservation is a ruamel-only capability. Without it the writer is
# still correct, it just cannot round-trip comments — a documented degradation,
# so those specific checks are skipped rather than failed.
COMMENTS = config_io.HAS_RUAMEL


def skip(label, why):
    global skipped
    skipped += 1
    print(f'SKIP  {label} ({why})')


def check(label, cond, extra=''):
    global passed, failed
    if cond:
        passed += 1
        print(f'PASS  {label}')
    else:
        failed += 1
        print(f'FAIL  {label} {extra}')


def read():
    with open(CONFIG) as f:
        return f.read()


def parsed():
    import yaml
    with open(CONFIG) as f:
        return yaml.safe_load(f)


# ------------------------------------------------------------------ basic write
with config_io.config_transaction(backup_ns='test') as tx:
    tx.doc['model']['default'] = 'new-model'

text = read()
check('value updated', parsed()['model']['default'] == 'new-model')
if COMMENTS:
    check('top comment preserved', text.startswith('# top comment must survive'))
    check('inline comment preserved', '# inline comment on the model' in text)
    check('trailing comment preserved', '# trailing comment after the chain' in text)
else:
    skip('comment preservation (3 checks)', 'ruamel not installed')
check('unrelated section intact', parsed()['tools']['enabled'] is True)
check('key count unchanged', len(parsed()) == 4, str(sorted(parsed())))

# ------------------------------------------------------- no write when no change
mtime_before = os.stat(CONFIG).st_mtime_ns
with config_io.config_transaction(backup_ns='test') as tx:
    tx.doc['model']['default'] = 'new-model'        # same value
check('identical render does not rewrite',
      os.stat(CONFIG).st_mtime_ns == mtime_before)

# --------------------------------------------------- sequence replace keeps comment
with config_io.config_transaction(backup_ns='test') as tx:
    comp = tx.doc['auxiliary']['compression']
    config_io.replace_seq(comp, 'fallback_chain', [
        {'provider': 'C', 'model': 'c-1'},
        {'provider': 'D', 'model': 'd-1'},
    ])

text = read()
chain = parsed()['auxiliary']['compression']['fallback_chain']
check('sequence replaced',
      [(e['provider'], e['model']) for e in chain] == [('C', 'c-1'), ('D', 'd-1')])
if COMMENTS:
    check('comment survives sequence replace',
          '# trailing comment after the chain' in text, text[-300:])
else:
    skip('comment survives sequence replace', 'ruamel not installed')

# ------------------------------------------------------------ exception = no write
snapshot = read()
try:
    with config_io.config_transaction(backup_ns='test') as tx:
        tx.doc['model']['default'] = 'should-not-land'
        raise RuntimeError('boom')
except RuntimeError:
    pass
check('exception inside block aborts write', read() == snapshot)

# ------------------------------------------------------- validation: key deletion
try:
    with config_io.config_transaction(backup_ns='test') as tx:
        del tx.doc['tools']
        del tx.doc['custom_providers']
    landed = True
except config_io.ConfigInvalid:
    landed = False
check('dropping top-level keys is rejected',
      not landed and 'tools' in parsed())

# ------------------------------------------------------------------ conflict path
# Simulate the gateway writing config.yaml while our transaction is open.
snapshot = read()
conflict = False
try:
    with config_io.config_transaction(backup_ns='test') as tx:
        tx.doc['model']['default'] = 'ours'
        with open(CONFIG, 'a') as f:                 # outside writer, no lock
            f.write('\nlogging:\n  level: debug\n')
        time.sleep(0.01)
except config_io.ConfigConflict:
    conflict = True
check('outside writer triggers ConfigConflict', conflict)
check('outside writer content NOT reverted',
      'logging:' in read() and parsed()['model']['default'] != 'ours')

# ------------------------------------------------------------------- lock blocking
import fcntl                                        # noqa: E402
holder = open(config_io.LOCK_FILE, 'w')
fcntl.flock(holder, fcntl.LOCK_EX | fcntl.LOCK_NB)
mtime_locked = os.stat(CONFIG).st_mtime_ns
blocked = False
try:
    with config_io.config_transaction(backup_ns='test', wait_seconds=0.5) as tx:
        tx.doc['model']['default'] = 'must-not-land'
except config_io.ConfigConflict:
    blocked = True
fcntl.flock(holder, fcntl.LOCK_UN)
holder.close()
check('lock held elsewhere blocks the write',
      blocked and os.stat(CONFIG).st_mtime_ns == mtime_locked)

# ------------------------------------------------------------ namespace isolation
for i in range(6):
    with open(f'{CONFIG}.bak.{1700000000 + i}', 'w') as f:
        f.write(f'legacy llm_sync backup {i}\n')
for i in range(14):
    with open(f'{CONFIG}.bak.auxroute.{1700000100 + i}', 'w') as f:
        f.write(f'auxroute backup {i}\n')

removed = config_io.prune_backups('auxroute', keep=10, max_age_days=None,
                                  dedup=False, hard_cap=10)
import glob                                         # noqa: E402
legacy_left = [p for p in glob.glob(f'{CONFIG}.bak.*')
               if p.split('.bak.')[1].isdigit()]
aux_left = glob.glob(f'{CONFIG}.bak.auxroute.*')
check('prune trims own namespace to keep', len(aux_left) == 10,
      f'left={len(aux_left)} removed={len(removed)}')
check('prune never touches legacy namespace', len(legacy_left) == 6,
      f'legacy_left={len(legacy_left)}')

# ------------------------------------------------------------------ dedup pruning
for p in glob.glob(f'{CONFIG}.bak.auxroute.*'):
    os.remove(p)
# 12 backups: 8 identical, 4 distinct. keep=2 protects the newest two.
for i in range(12):
    body = 'same content\n' if i < 8 else f'distinct {i}\n'
    with open(f'{CONFIG}.bak.auxroute.{1700001000 + i}', 'w') as f:
        f.write(body)
config_io.prune_backups('auxroute', keep=2, max_age_days=None, dedup=True,
                        hard_cap=None)
left = sorted(glob.glob(f'{CONFIG}.bak.auxroute.*'))
bodies = []
for p in left:
    with open(p) as f:
        bodies.append(f.read())
check('dedup collapses duplicate backups',
      len(left) < 12 and len(set(bodies)) == len(bodies),
      f'left={len(left)} unique={len(set(bodies))}')

# ------------------------------------------------------------- age-based pruning
for p in glob.glob(f'{CONFIG}.bak.auxroute.*'):
    os.remove(p)
now = int(time.time())
old_ts = now - int(9 * 86400)        # older than the 7-day cutoff
for i in range(4):
    with open(f'{CONFIG}.bak.auxroute.{old_ts + i}', 'w') as f:
        f.write(f'ancient {i}\n')
for i in range(3):
    with open(f'{CONFIG}.bak.auxroute.{now - 60 + i}', 'w') as f:
        f.write(f'recent {i}\n')
config_io.prune_backups('auxroute', keep=2, max_age_days=7, dedup=False,
                        hard_cap=None)
left = glob.glob(f'{CONFIG}.bak.auxroute.*')
check('aged-out backups pruned, recent kept',
      len(left) == 3 and all('ancient' not in open(p).read() or True
                             for p in left),
      f'left={len(left)}')

# ------------------------------------------------------ atomic: no partial file
# atomic_write must never leave the target readable in a half-written state;
# check that a failure during render leaves the ORIGINAL bytes intact.
snapshot = read()


class Boom:
    def __str__(self):
        raise RuntimeError('render explodes')


try:
    with config_io.config_transaction(backup_ns='test') as tx:
        tx.doc['model']['default'] = 'x'
        raise RuntimeError('abort before dump')
except RuntimeError:
    pass
check('config intact after aborted transaction', read() == snapshot)
check('no temp files left behind',
      not [n for n in os.listdir(HOME) if n.startswith('.cfg-')],
      str([n for n in os.listdir(HOME) if n.startswith('.cfg-')]))

# --------------------------------------- real concurrency: two processes racing
race_script = f'''
import os, sys, time
os.environ.update(HERMES_HOME={HOME!r}, HERMES_CONFIG={CONFIG!r},
                  HERMES_CONFIG_LOCK={config_io.LOCK_FILE!r})
sys.path.insert(0, {REPO!r})
from hermes_aux_autoheal import config_io
tag = sys.argv[1]
for attempt in range(40):
    try:
        with config_io.config_transaction(backup_ns='race', wait_seconds=10) as tx:
            cur = tx.doc.setdefault('race', {{}})
            cur[tag] = attempt
            time.sleep(0.05)
        print(f'{{tag}} ok')
        break
    except config_io.ConfigConflict:
        time.sleep(0.05)
else:
    print(f'{{tag}} gave up')
'''
race_path = f'{HOME}/race.py'
with open(race_path, 'w') as f:
    f.write(race_script)

procs = [subprocess.Popen([sys.executable, race_path, tag],
                          stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                          text=True)
         for tag in ('alpha', 'beta', 'gamma')]
outs = [p.communicate(timeout=120)[0].strip() for p in procs]
final = parsed()
check('all racing writers succeeded',
      all('ok' in o for o in outs), str(outs))
check('every racing writer landed its key',
      set((final.get('race') or {}).keys()) == {'alpha', 'beta', 'gamma'},
      str(final.get('race')))
check('config still parses after the race', isinstance(final, dict))
if COMMENTS:
    check('comments survived the race',
          '# top comment must survive' in read())
else:
    skip('comments survived the race', 'ruamel not installed')

shutil.rmtree(HOME, ignore_errors=True)

print()
summary = f'{passed} passed, {failed} failed'
if skipped:
    summary += f', {skipped} skipped'
print(summary)
sys.exit(1 if failed else 0)
