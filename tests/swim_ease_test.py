"""Build and run tests/swim_ease_test.c: the per-call fix runtime/swim_ease.h carries, at -O0 and -O1.

The file is header-only and needs no game, no hook and no runtime, so this is a single compile of one translation unit
(a second or two) rather than a build.

Run: python tests/swim_ease_test.py
"""
import os
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'tests' / 'diff'))
from common import MSYS_BIN, compiler  # noqa: E402


def main():
    work = Path(tempfile.gettempdir()) / 'totk-swim-ease-test'
    work.mkdir(parents=True, exist_ok=True)
    exe = work / ('swim_ease_test.exe' if os.name == 'nt' else 'swim_ease_test')
    env = dict(os.environ)
    if os.name == 'nt' and MSYS_BIN.exists():
        env['PATH'] = str(MSYS_BIN) + os.pathsep + env.get('PATH', '')
    for opt in ('-O0', '-O1'):
        subprocess.check_call([compiler(), opt, '-fno-strict-aliasing', '-I', str(ROOT / 'runtime'),
                               str(ROOT / 'tests' / 'swim_ease_test.c'), '-o', str(exe), '-lm'], env=env)
        out = subprocess.run([str(exe)], capture_output=True, text=True)
        last = out.stdout.strip().splitlines()[-1] if out.stdout.strip() else out.stderr.strip()
        print(f'{opt}: {last}')
        if out.returncode != 0:
            print(out.stdout)
            return 1
    print('\nswim_ease_test: all passed')
    return 0


if __name__ == '__main__':
    sys.exit(main())
