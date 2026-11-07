"""IR validity check: emit every parseable function of a chunk into one module and compile it.

Parse success is not IR validity. This harness closes the gap: one .ll per chunk, clang -x ir -c.
Reports per chunk: functions emitted, compile ok/fail, and the first error text.
"""
import json
import multiprocessing as mp
import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'recomp'))
sys.dont_write_bytecode = True

import importlib
E = importlib.import_module(os.environ.get('IR_MOD', 'emit_ir'))

CLANG = Path(r'C:\msys64\ucrt64\bin\clang.exe')
FN_RE = re.compile(r'void (f_[0-9a-f]+)\(Cpu \*c\) \{')

PREAMBLE = '''; IR validity probe
target datalayout = "e-m:w-p270:32:32-p271:32:32-p272:64:64-i64:64-i128:128-f80:128-n8:16:32:64-S128"
target triple = "x86_64-w64-windows-gnu"

%Cpu = type { [32 x i64], i64, i64, i64, i64, i64, i64, i64, ptr, i64, i64, i64, i64, [160 x i8], [32 x i128] }
%Flags = type { i64, i64, i64 }
'''


def body_end(s, start):
    depth, i = 0, s.index('{', start)
    while True:
        c = s[i]
        if c == '{':
            depth += 1
        elif c == '}':
            depth -= 1
            if depth == 0:
                return i + 1
        i += 1


def module_for(path):
    s = Path(path).read_text(encoding='utf-8', errors='replace')
    ok = 0
    bodies, calls = [], {}
    first_err = None
    for fm in FN_RE.finditer(s):
        start = fm.start()
        ll, why = E.emit_function(fm.group(1), s[start:body_end(s, start)])
        if ll is None:
            continue
        ok += 1
        bodies.append(ll)
        for ret, name in re.findall(r'call (void|i\d+|ptr|float|double) @([A-Za-z_0-9.]+)\(', ll):
            calls.setdefault(name, set()).add(ret)
        for ret, name in re.findall(r'musttail call (void|i\d+|ptr) @([A-Za-z_0-9.]+)\(', ll):
            calls.setdefault(name, set()).add(ret)
    decls = []
    conflicts = []
    defined = set()
    for b in bodies:
        defined |= set(re.findall(r'define void @([A-Za-z_0-9.]+)\(', b))
    for name, rets in sorted(calls.items()):
        if name in defined:      # declare + define of one name is a redefinition in LLVM IR
            continue
        if len(rets) > 1:
            conflicts.append(f'{name}: {sorted(rets)}')
        ret = sorted(rets)[0]
        if re.fullmatch(r'f_[0-9a-f]+', name):
            decls.append(f'declare void @{name}(ptr)')
        else:
            decls.append(f'declare {ret} @{name}(...)')
    text = PREAMBLE + '\n'.join(decls) + '\n\n' + '\n\n'.join(bodies) + '\n'
    return ok, text, conflicts


def check(path):
    ok, text, conflicts = module_for(path)
    with tempfile.TemporaryDirectory() as td:
        src = Path(td) / 'm.ll'
        src.write_text(text, encoding='utf-8')
        r = subprocess.run([str(CLANG), '-x', 'ir', '-c', '-O1', '-march=x86-64-v3', '-Wno-override-module',
                            '-o', str(Path(td) / 'm.o'), str(src)], capture_output=True, text=True)
    err = '' if r.returncode == 0 else r.stderr.strip().splitlines()[0][:200]
    nerr = r.stderr.count('error:')
    return {'chunk': Path(path).name, 'functions': ok, 'ok': r.returncode == 0, 'errors': nerr,
            'first': err, 'conflicts': conflicts[:5], 'nconflicts': len(conflicts)}


def main():
    mod = sys.argv[1] if len(sys.argv) > 1 else 'main'
    files = sorted(str(p) for p in (ROOT / 'generated' / 'v143' / mod).glob(f'{mod}_[0-9]*.c'))
    limit = int(os.environ.get('LIMIT', '0'))
    if limit:
        files = files[:limit]
    with mp.get_context('spawn').Pool(10) as pool:
        rows = pool.map(check, files, chunksize=1)
    good = [r for r in rows if r['ok']]
    print(f'{mod}: {len(good)}/{len(rows)} chunks compile as one module; '
          f'{sum(r["functions"] for r in rows)} functions emitted')
    print(f'conflicting call return types: {sum(r["nconflicts"] for r in rows)}')
    for r in rows:
        if not r['ok']:
            print(f'  FAIL {r["chunk"]}: {r["errors"]} errors | {r["first"]}')
    for r in rows[:3]:
        if r['conflicts']:
            print(f'  signature conflict {r["chunk"]}: {r["conflicts"]}')
    Path(ROOT / 'build' / '_irci' / f'vet-{mod}.json').write_text(json.dumps(rows), encoding='utf-8')


if __name__ == '__main__':
    main()
