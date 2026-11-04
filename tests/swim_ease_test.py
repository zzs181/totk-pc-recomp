"""The swim ease's two halves, compiled and run: the per-call step in runtime/swim_ease.h at -O0 and -O1, and the hook
itself (runtime/swim_ease.c) on a synthetic frame, with and without TOTK_SWIM_EASE.

Neither needs a game, a build or the runtime: two single-file compiles of a second or two each. The hook test gets a
fake layout.h (MOD_MAIN_BASE points at a float it owns) and stubs for what the host provides.

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

FAKE_LAYOUT_H = """// a layout.h for the hook test: the factor reads through this base into a float the test owns
#pragma once
#include <stdint.h>
extern uint64_t g_fake_main_base;
#define MOD_MAIN_BASE g_fake_main_base
"""


def env_with_toolchain():
    env = dict(os.environ)
    if os.name == 'nt' and MSYS_BIN.exists():
        env['PATH'] = str(MSYS_BIN) + os.pathsep + env.get('PATH', '')
    return env


def exe_name(name):
    return name + ('.exe' if os.name == 'nt' else '')


def run(exe, args=(), env=None):
    out = subprocess.run([str(exe)] + list(args), capture_output=True, text=True, env=env)
    last = out.stdout.strip().splitlines()[-1] if out.stdout.strip() else out.stderr.strip()
    print(f'  {last}')
    if out.returncode != 0:
        print(out.stdout)
    return out.returncode


def main():
    work = Path(tempfile.gettempdir()) / 'totk-swim-ease-test'
    work.mkdir(parents=True, exist_ok=True)
    (work / 'layout.h').write_text(FAKE_LAYOUT_H, encoding='utf-8')
    env = env_with_toolchain()
    failed = 0

    print('the per-call step (runtime/swim_ease.h):')
    for opt in ('-O0', '-O1'):
        exe = work / exe_name('swim_ease_test')
        subprocess.check_call([compiler(), opt, '-fno-strict-aliasing', '-I', str(ROOT / 'runtime'),
                               str(ROOT / 'tests' / 'swim_ease_test.c'), '-o', str(exe), '-lm'], env=env)
        print(f' {opt}:', end='')
        failed += run(exe, env=env) != 0

    print('\nthe hook on a synthetic frame (runtime/swim_ease.c):')
    for opt in ('-O0', '-O1'):
        exe = work / exe_name('swim_ease_hook_test')
        subprocess.check_call([compiler(), opt, '-fno-strict-aliasing', '-I', str(work), '-I', str(ROOT / 'runtime'),
                               str(ROOT / 'tests' / 'swim_ease_hook_test.c'), str(ROOT / 'runtime' / 'swim_ease.c'),
                               '-o', str(exe), '-lm'], env=env)
        print(f' {opt}:', end='')
        failed += run(exe, env=env) != 0
        print('        off:', end='')
        failed += run(exe, ['off'], env=env) != 0

    if failed:
        print(f'\nswim_ease_test: {failed} run(s) failed')
        return 1
    print('\nswim_ease_test: all passed')
    return 0


if __name__ == '__main__':
    sys.exit(main())
