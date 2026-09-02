"""Run the standalone config_io checks under pytest.

``tests/config_io_checks.py`` is deliberately a plain script with no pytest
dependency — it has to be runnable on a bare interpreter, and it spawns real
subprocesses to test cross-process locking. Wrapping it here means CI covers it
too, without pytest trying to collect its module-level asserts.
"""
import os
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
SCRIPT = os.path.join(HERE, 'config_io_checks.py')


def test_config_io_checks_pass():
    proc = subprocess.run([sys.executable, SCRIPT],
                          capture_output=True, text=True, timeout=300)
    tail = '\n'.join(proc.stdout.strip().split('\n')[-6:])
    assert proc.returncode == 0, f'config_io checks failed:\n{tail}\n{proc.stderr[-2000:]}'
    assert '0 failed' in proc.stdout, tail
