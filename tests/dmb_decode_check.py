"""Decode-level check for the DMB barrier weakening: every barrier encoding the game uses, in and out.

The lifter turns DMB ...LD / ...ST into a compiler barrier (no machine code) and keeps a hardware fence for
DMB ISH / SY, DSB and ISB. This feeds the raw encodings to Lifter.system() and checks the answer, so a change
to the CRm list or to the fall-through is caught without a build.

Run: python tests/dmb_decode_check.py
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / 'recomp'))
from lifter import Lifter  # noqa: E402

COMPILER = '__atomic_signal_fence(__ATOMIC_SEQ_CST);'
HARDWARE = '__atomic_thread_fence(__ATOMIC_SEQ_CST);'
CLREX = 'c->excl_addr = ~0ull;'


def dmb(crm):
    # DMB: 1101 0101 0000 0011 CRm(4) 0101 1111
    return 0xD50330BF | (crm << 8)


def dsb(crm):
    # DSB: 1101 0101 0000 0011 CRm(4) 0100 1111
    return 0xD503309F | (crm << 8)


CASES = [
    ('DMB ISHLD', dmb(0x9), COMPILER),
    ('DMB ISHST', dmb(0xA), COMPILER),
    ('DMB LD', dmb(0xD), COMPILER),
    ('DMB ST', dmb(0xE), COMPILER),
    ('DMB ISH', dmb(0xB), HARDWARE),
    ('DMB SY', dmb(0xF), HARDWARE),
    ('DMB OSH', dmb(0x3), HARDWARE),
    ('DMB NSHLD', dmb(0x1), HARDWARE),
    ('DSB ISH', dsb(0xB), HARDWARE),
    ('DSB SY', dsb(0xF), HARDWARE),
    ('ISB', 0xD5033FDF, HARDWARE),
    ('CLREX', 0xD503305F, CLREX),
]


def main():
    bad = 0
    for name, w, want in CASES:
        try:
            got = Lifter.system(None, w, 0)
            text = got.code if got.kind == 'op' else f'<{got.kind}>'
        except Exception as e:  # noqa: BLE001
            text = f'<raised {type(e).__name__}: {e}>'
        ok = text == want
        bad += not ok
        print(f'{"PASS" if ok else "FAIL"}  {name:10s} {w:08x}  {text}')
    print(f'\n{len(CASES) - bad}/{len(CASES)} passed')
    return 1 if bad else 0


if __name__ == '__main__':
    sys.exit(main())
