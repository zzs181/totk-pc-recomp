"""The host hooks behind recomp/patches.py `hook`: runtime/hook.c in C, and the generator side in Python.

Part 1 builds tests/hook_test.c with runtime/hook.c against a one-slot hooks.h written here, and runs it: the caller's
return address in c->x[30], a hook that takes a call over, the body's writes and the hook's reaching the caller, and
the unbound slot.

Part 2 checks the two places that produce that slot: patches.load on a patch file with a `hook` line (its table, its
slots, the hooks.h text, and the three ways a bad line is refused), and recomp/gen.py wrapping a hooked function --
its lifted body moves behind the name, and the name becomes rt_hook_run, while a function that is not hooked is left
alone.

Run: python tests/hook_test.py
"""
import os
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'tests' / 'diff'))
from common import MSYS_BIN, compiler  # noqa: E402

sys.path.insert(0, str(ROOT / 'recomp'))
import gen  # noqa: E402
import patches  # noqa: E402
from lifter import Lifter  # noqa: E402

HOOKS_H = """// a hooks.h for the test: one slot
#pragma once
#define RT_HOOK_COUNT 1
#define RT_HOOKS(X) X(0, sway)
"""

failed = []


def check(cond, what):
    print(('ok    ' if cond else 'FAIL  ') + what)
    if not cond:
        failed.append(what)


def build_and_run():
    """tests/hook_test.c with runtime/hook.c, at both -O0 and -O1 (the compiler's own optimisation must not matter)."""
    work = Path(tempfile.gettempdir()) / 'totk-hook-test'
    work.mkdir(parents=True, exist_ok=True)
    (work / 'hooks.h').write_text(HOOKS_H, encoding='utf-8')
    exe = work / ('hook_test.exe' if os.name == 'nt' else 'hook_test')
    env = dict(os.environ)
    if os.name == 'nt' and MSYS_BIN.exists():
        env['PATH'] = str(MSYS_BIN) + os.pathsep + env.get('PATH', '')
    for opt in ('-O0', '-O1'):
        subprocess.check_call([compiler(), opt, '-fno-strict-aliasing',
                               '-I', str(work), '-I', str(ROOT / 'runtime'),
                               str(ROOT / 'tests' / 'hook_test.c'), str(ROOT / 'runtime' / 'hook.c'),
                               '-o', str(exe)], env=env)
        out = subprocess.run([str(exe)], capture_output=True, text=True)
        print(f'{opt}: {out.stdout.strip().splitlines()[-1] if out.stdout.strip() else out.stderr.strip()}')
        if out.stdout:
            print(out.stdout.rstrip())
        check(out.returncode == 0, f'hook_test.c at {opt}')


class FakeModule:
    """The part of recomp/module.py the emitter reads: a word at an address, and the lifter."""

    def __init__(self, base, words):
        self.base = base
        self.lo = base
        self.hi = base + 4 * len(words)
        self.words = words
        self.lifter = Lifter(base, 'c')

    def word(self, a):
        return self.words[(a - self.lo) >> 2]

    def lift(self, a):
        return self.lifter.lift(self.word(a), a)


MOV_X0_X0 = 0xAA0003E0
RET = 0xD65F03C0


def table_side():
    """recomp/patches.py: the `hook` line, its slot, hooks.h, and what it refuses."""
    with tempfile.TemporaryDirectory() as td:
        p = Path(td) / 'patches.txt'
        p.write_text('set  feel TOTK_FEEL 1\nhook main 1000 sway\n', encoding='utf-8')
        t = patches.load(p, [('main', 0, 0x4000)])
        check(t.hooks == [('main', 0x1000, 'sway')], 'a hook line lands in the table')
        check(t.hooks_for('main', 0) == {0x1000: 0}, 'its slot is its place in the file')
        check(t.hooks_for('sdk', 0) == {}, 'another module has none of it')
        text = patches.hooks_header_text(t, 'generated')
        check('RT_HOOK_COUNT 1' in text and 'X(0, sway)' in text, 'hooks.h carries the slot and its name')

        for body, why in (
                ('hook main 1000 sway\nhook main 2000 sway\n', 'a name used twice'),
                ('hook main 1000 sw-ay\n', 'a name that is not a C identifier'),
                ('hook sdk 1000 sway\n', 'a module that is not in the layout')):
            p.write_text('set  feel TOTK_FEEL 1\n' + body, encoding='utf-8')
            try:
                patches.load(p, [('main', 0, 0x4000)])
                check(False, f'{why} is refused')
            except patches.PatchError:
                check(True, f'{why} is refused')


def generator_side():
    """recomp/gen.py: the hooked function's body moves behind its name, and the name becomes the wrapper."""
    mod = FakeModule(0x1000, [MOV_X0_X0, RET])
    lines, _ = gen.Emitter(mod, {0x1000}, hooks={0x1000: 0}).function(0x1000, 0x1008)
    text = '\n'.join(lines)
    check('static void f_1000_body(Cpu *c) {' in text, 'the lifted body is emitted under f_<addr>_body')
    check('void f_1000(Cpu *c) { rt_hook_run(0, c, f_1000_body); }' in text, 'the name becomes the hook wrapper')
    check(text.index('f_1000_body') < text.index('rt_hook_run'), 'the body comes before the wrapper that calls it')

    plain, _ = gen.Emitter(mod, {0x1000}).function(0x1000, 0x1008)
    plain = '\n'.join(plain)
    check('void f_1000(Cpu *c) {' in plain and 'rt_hook_run' not in plain, 'an unhooked function is untouched')


def main():
    build_and_run()
    table_side()
    generator_side()
    if failed:
        print(f'\nhook_test: {len(failed)} failed')
        return 1
    print('\nhook_test: all passed')
    return 0


if __name__ == '__main__':
    sys.exit(main())
