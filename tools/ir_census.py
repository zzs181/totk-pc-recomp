"""IR parse-coverage census over the generated chunks (read-only measurement).

Runs the pinned emit_ir snapshot (build/_irci/emit_ir_pin.py, sha256 in the header) over every
`void f_<addr>(Cpu *c)` of a module and reports, per chunk, the IR-eligible function count and the
distribution of the *first* blocking reason. Also counts grammar features per function, so a planned
grammar item can be sized against the functions that actually carry it.
"""
import json
import multiprocessing as mp
import re
import sys
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'recomp'))
sys.dont_write_bytecode = True

import importlib, os
E = importlib.import_module(os.environ.get("IR_MOD", "emit_ir"))

FN_RE = re.compile(r'void (f_[0-9a-f]+)\(Cpu \*c\) \{')

# feature probes: (name, regex). Counted per function body.
FEATURES = [
    ('flags_member', re.compile(r'\bF\.(?:nz|a|b)\s*=')),
    ('flags_init_nz', re.compile(r'Flags F = \{\.nz')),
    ('int128', re.compile(r'__int128')),
    ('fence', re.compile(r'__atomic_thread_fence')),
    ('udf', re.compile(r'\brt_udf\(')),
    ('patch', re.compile(r'\brt_patch_on\[')),
    ('fb', re.compile(r'\bfb_[0-9a-f]+')),
    ('switch', re.compile(r'\bswitch \(')),
    ('hook', re.compile(r'\brt_hook_run\(')),
    ('nonbraced_if', re.compile(r'\bif \([^;{}]*\) (?!\{)[^;{}]')),
    ('simd_macro', re.compile(r'\b(?:fp_op_[sc]|cmp_[sc]|vsplat_[a-z]|vld|vst|shuf_[a-z0-9]+|VW|VREG|VSF32|SV_FOP_)')),
    ('enum_const', re.compile(r'\b(?:FOP_|LOP_|SV_FOP_)[A-Z0-9_]+')),
    ('atomic_builtin', re.compile(r'__atomic_\w+')),
    ('helper_call', re.compile(r'\b(?:ROR64|ROR32|udiv|sdiv|clz|cls|rbit|rev|fc_|fnz_|fsub|fadd|flogic|adc|sbc|ldx|lda|stx|stl|atomic_)')),
]


def classify(why):
    """Bucket the message emit_function returned into a grammar item."""
    inner = why.rsplit(' -> ', 1)[-1]
    m = re.search(r"unknown name '([^']+)'", inner)
    if m:
        name = m.group(1)
        for pre, tag in (('FOP_', 'const:FOP_*'), ('LOP_', 'const:LOP_*'), ('SV_FOP_', 'const:SV_FOP_*'),
                         ('__ATOMIC_', 'atomic_fence_const'), ('VSF', 'simd_macro'), ('VW', 'simd_macro')):
            if name.startswith(pre):
                return tag
        return f'unknown_name:{name[:24]}'
    m = re.search(r"trailing tokens: '([^']*)'", inner)
    if m:
        tail = m.group(1)
        if re.match(r'^\.\s*(?:nz|a|b)\b', tail):
            return 'flags_member'
        return 'expr_tail:' + tail[:16]
    if '__int128' in why:
        return 'int128_cast'
    if 'non-void return' in inner:
        return 'nonvoid_return'
    if 'unbalanced' in inner:
        return 'unbalanced'
    if inner.startswith('unexpected token') or inner.startswith('expected') or inner.startswith('token at'):
        return 'parser_shape:' + inner[:24]
    if inner.startswith('lhs '):
        return 'lhs_shape'
    if inner.startswith('num into'):
        return 'num_into'
    return 'other:' + inner[:32]


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


def scan_chunk(path):
    s = Path(path).read_text(encoding='utf-8', errors='replace')
    ok = fail = 0
    reasons = Counter()
    feats = Counter()
    feat_ok = Counter()
    fails = []
    for fm in FN_RE.finditer(s):
        start = fm.start()
        text = s[start:body_end(s, start)]
        hit = [n for n, rx in FEATURES if rx.search(text)]
        for n in hit:
            feats[n] += 1
        ll, why = E.emit_function(fm.group(1), text)
        if ll is not None:
            ok += 1
            for n in hit:
                feat_ok[n] += 1
        else:
            fail += 1
            k = classify(why)
            reasons[k] += 1
            if len(fails) < 40:
                fails.append({'fn': fm.group(1), 'reason': k, 'msg': why[:220]})
    return {'chunk': Path(path).name, 'ok': ok, 'fail': fail, 'reasons': dict(reasons),
            'features': dict(feats), 'features_ok': dict(feat_ok), 'samples': fails}


def main():
    mod = sys.argv[1] if len(sys.argv) > 1 else 'main'
    files = sorted(str(p) for p in (ROOT / 'generated' / 'v143' / mod).glob(f'{mod}_[0-9]*.c'))
    print(f'{mod}: {len(files)} chunks', flush=True)
    with mp.get_context('spawn').Pool(12) as pool:
        rows = pool.map(scan_chunk, files, chunksize=1)
    out = {'module': mod, 'chunks': rows}
    dst = ROOT / 'build' / '_irci' / f'census-{mod}-{os.environ.get("IR_MOD", "pin")}.json'
    dst.write_text(json.dumps(out), encoding='utf-8')
    ok = sum(r['ok'] for r in rows)
    fail = sum(r['fail'] for r in rows)
    print(f'{mod}: IR-eligible {ok}/{ok + fail} ({100 * ok / max(1, ok + fail):.2f}%)')
    tot = Counter()
    for r in rows:
        tot.update(r['reasons'])
    print('first blockers:')
    for k, n in tot.most_common(30):
        print(f'  {n:7d}  {k}')
    ft = Counter()
    fo = Counter()
    for r in rows:
        ft.update(r['features'])
        fo.update(r['features_ok'])
    print('feature -> functions carrying it (eligible of those):')
    for k, n in ft.most_common():
        print(f'  {k:16s} {n:7d} functions, {fo[k]:7d} eligible')
    worst = sorted(rows, key=lambda r: r['ok'] / max(1, r['ok'] + r['fail']))[:5]
    print('lowest-coverage chunks:', ', '.join(f"{r['chunk']} {r['ok']}/{r['ok'] + r['fail']}" for r in worst))
    print(f'wrote {dst}')


if __name__ == '__main__':
    main()
