"""Translate lifted functions from a generated C chunk to textual LLVM IR.

The chunks' C is machine-generated with a closed grammar (lifter.py + gen.py emit it),
so a parser over that grammar moves a function from the C route to the IR route
without touching the lifter. Functions whose text resists parsing stay on the C
route: the migration is per function, never all-or-nothing.

Coverage (prototype): prologue locals (single and comma-separated), Flags F,
braced blocks, LD8/16/32/64 and ST8/16/32/64, straight assignments, if/goto
(and else), jump-table switch, direct calls, musttail rt_dispatch, ret.
Skipped (documented ceiling): FN_ENTER (the profiling hook, no-op unless
g_fnprof is armed), inline-asm chunks (the aarch64 host path).
"""
import re
import sys

# ---------------------------------------------------------------- tokenizer

TOK = re.compile(r'''
    0[xX][0-9a-fA-F]+[uUlL]* | \d+[uUlL]*
  | [A-Za-z_][A-Za-z0-9_]*
  | -> | << | >> | == | != | <= | >= | && | \|\|
  | [-+*/%&|^~!<>=?:().,\[\]{};]
''', re.X)

def toks(text):
    text = re.sub(r'/\*.*?\*/', ' ', text, flags=re.S)   # /* hint */ and /* prfm */ markers
    out, i = [], 0
    while i < len(text):
        if text[i] in ' \t\n':   # ';' never reaches toks: the statement splitter consumed it
            i += 1
            continue
        m = TOK.match(text, i)
        if not m:
            raise ValueError(f'token at: {text[i:i+40]!r}')
        out.append(m.group(0))
        i = m.end()
    return out

# ---------------------------------------------------------------- expression AST

class Num:
    def __init__(self, v, ty): self.v, self.ty = v, ty
class Ref:
    def __init__(self, name): self.name = name
class FlagField:
    "F.a / F.b / F.nz: a member of the function-local Flags struct."
    def __init__(self, member): self.member = member
class VecElem:
    "VS8(n)/VS16(n)/VS32(n)/VS64(n)/VSF32(n)/VSF64(n): the vector element view c->v[n]."
    def __init__(self, name, idx): self.name, self.idx = name, idx
class CpuField:
    def __init__(self, name, idx=None): self.name, self.idx = name, idx
class Cast:
    def __init__(self, ty, e, signed=False): self.ty, self.e, self.signed = ty, e, signed
class Bin:
    def __init__(self, op, a, b): self.op, self.a, self.b = op, a, b
class Un:
    def __init__(self, op, e): self.op, self.e = op, e
class Call:
    def __init__(self, name, args): self.name, self.args = name, args
class Tern:
    def __init__(self, c, a, b): self.c, self.a, self.b = c, a, b

CASTS = {'void': 'void', 'uint64_t': 'i64', 'int64_t': 'i64', 'uint32_t': 'i32', 'int32_t': 'i32',
         'uint16_t': 'i16', 'int16_t': 'i16', 'uint8_t': 'i8', 'int8_t': 'i8',
         'u128': 'i128', '__uint128_t': 'i128', '__int128': 'i128', 'unsigned __int128': 'i128', 'float': 'f32', 'double': 'f64'}
INT_W = {'i8': 8, 'i16': 16, 'i32': 32, 'i64': 64, 'i128': 128}
COND_SHIFTS = {'COND_EQ': 30, 'COND_NE': 29, 'COND_CS': 28, 'COND_CC': 27, 'COND_MI': 26,
               'COND_PL': 25, 'COND_VS': 24, 'COND_VC': 23, 'COND_HI': 22, 'COND_LS': 21,
               'COND_GE': 20, 'COND_LT': 19, 'COND_GT': 18, 'COND_LE': 17}
ATOMIC_ORDERS = {'__ATOMIC_RELAXED': 0, '__ATOMIC_CONSUME': 1, '__ATOMIC_ACQUIRE': 2,
                 '__ATOMIC_RELEASE': 3, '__ATOMIC_ACQ_REL': 4, '__ATOMIC_SEQ_CST': 5}
VS_MAP = {'VS8': ('i8', 1), 'VS16': ('i16', 2), 'VS32': ('i32', 4), 'VS64': ('i64', 8),
          'VSF32': ('f32', 4), 'VSF64': ('f64', 8)}
LLVM_TYPE = {'f32': 'float', 'f64': 'double'}
CC_CONSTS = {n: i for i, n in enumerate(
    ['CC_EQ', 'CC_NE', 'CC_CS', 'CC_CC', 'CC_MI', 'CC_PL', 'CC_VS', 'CC_VC',
     'CC_HI', 'CC_LS', 'CC_GE', 'CC_LT', 'CC_GT', 'CC_LE'])}

def parse_expr(t):
    """Parse a full expression; any trailing tokens are a grammar miss, never a silent drop."""
    p = Parser(t)
    e = p.expr()
    if p.i != len(t):
        raise ValueError(f'trailing tokens: {" ".join(t[p.i:])!r}')
    return e

class Parser:
    def __init__(self, t):
        self.t, self.i = t, 0
    def peek(self, k=0):
        return self.t[self.i + k] if self.i + k < len(self.t) else None
    def take(self):
        t = self.t[self.i]; self.i += 1; return t
    def expect(self, t):
        got = self.take()
        if got != t:
            raise ValueError(f'expected {t!r}, got {got!r} near {self.t[max(0,self.i-3):self.i+3]}')
    def expr(self):
        return self.ternary()
    def ternary(self):
        c = self.binary(0)
        if self.peek() == '?':
            self.take(); a = self.ternary(); self.expect(':'); b = self.ternary()
            return Tern(c, a, b)
        return c
    LEVELS = [['||'], ['&&'], ['|'], ['^'], ['&'], ['==', '!='], ['<', '>', '<=', '>='],
              ['<<', '>>'], ['+', '-'], ['*', '/', '%']]
    def binary(self, lvl):
        if lvl >= len(self.LEVELS):
            return self.unary()
        a = self.binary(lvl + 1)
        while self.peek() in self.LEVELS[lvl]:
            a = Bin(self.take(), a, self.binary(lvl + 1))
        return a
    def unary(self):
        t = self.peek()
        if t == '(' and self.peek(1) == 'unsigned' and self.peek(2) == '__int128' and self.peek(3) == ')':
            self.take(); self.take(); self.take(); self.take()
            return Cast('i128', self.unary())
        if t == '(' and self.peek(1) in CASTS and self.peek(2) == ')':
            self.take(); cty = self.take(); self.take()
            return Cast(CASTS[cty], self.unary(), cty in SIGNED)
        if t in ('-', '~', '!', '&'):
            return Un(self.take(), self.unary())
        return self.atom()
    def atom(self):
        t = self.take()
        if t == '(':
            e = self.expr(); self.expect(')'); return e
        if re.match(r'^(0[xX])?\d', t):
            hexv = 'x' in t.lower()
            v = int(re.match(r'(0[xX][0-9a-fA-F]+|\d+)', t).group(1), 16 if hexv else 10)
            if 'll' in t.lower() or (not t.lower().endswith(('u', 'ul')) and (v > 0xFFFFFFFF or not hexv)):
                return Num(v, 'i64')
            return Num(v, 'i32')
        if re.match(r'^[A-Za-z_]', t):
            if t in ('VS8', 'VS16', 'VS32', 'VS64', 'VSF32', 'VSF64') and self.peek() == '(':
                self.take()          # '('
                idx = self.expr(); self.expect(')')
                return VecElem(t, idx)
            if self.peek() == '(':
                self.take()
                args = []
                if self.peek() != ')':
                    args.append(self.expr())
                    while self.peek() == ',':
                        self.take(); args.append(self.expr())
                self.expect(')')
                return Call(t, args)
            if t == 'F' and self.peek() == '.':
                self.take()          # '.'
                return FlagField(self.take())   # the member name
            if t == 'c' and self.peek() == '->':
                self.take()          # '->'
                name = self.take()   # the field name
                if self.peek() == '[':
                    self.take(); idx = self.expr(); self.expect(']')
                    return CpuField(name, idx)
                return CpuField(name)
            return Ref(t)
        raise ValueError(f'unexpected token {t!r}')

# ---------------------------------------------------------------- IR

CPU_TYPE = ('%Cpu = type { [32 x i64], i64, i64, i64, i64, i64, i64, i64, ptr, i64, i64, '
            'i64, i64, [160 x i8], [32 x i128] }')
CPU_IDX = {'x': 0, 'sp': 1, 'pc': 2, 'nzcv': 3, 'tpidr': 4, 'tpidrro': 5, 'excl_addr': 6,
           'excl_val': 7, 'thread': 8, 'fpcr': 9, 'fpsr': 10, 'excl_val_hi': 11,
           'excl_gen': 12, 'v': 14}
LD_SIZES = {'LD8': 'i8', 'LD16': 'i16', 'LD32': 'i32', 'LD64': 'i64', 'LD128': 'i128',
            'ST8': 'i8', 'ST16': 'i16', 'ST32': 'i32', 'ST64': 'i64', 'ST128': 'i128'}
BIN_IR = {'+': 'add', '-': 'sub', '&': 'and', '|': 'or', '^': 'xor',
          '*': 'mul', '/': 'udiv', '%': 'urem'}
CMP_IR = {'==': 'eq', '!=': 'ne', '<': 'ult', '>': 'ugt', '<=': 'ule', '>=': 'uge'}
SIGNED = {'int64_t', 'int32_t', 'int16_t', 'int8_t'}   # C type names, kept on the Cast node


def is_signed(e):
    """Whether a C expression's value is signed, as far as a widening conversion needs to know: an explicit cast to a
    signed type, or an arithmetic result with such an operand. The lifter writes every signed value as a cast over the
    unsigned register image, so this is where the sign lives."""
    if isinstance(e, Cast):
        return e.signed
    if isinstance(e, Bin):
        return is_signed(e.a) or is_signed(e.b)
    return False

class Emitter:
    def __init__(self, name):
        self.name = name
        self.blocks = {'entry': []}
        self.cur = 'entry'
        self.n = 0
        self.used_calls = set()

    def t(self):
        self.n += 1
        return f'%t{self.n}'

    def emit(self, line):
        self.blocks[self.cur].append('  ' + line)

    def block(self, tag):
        name = f'{tag}.{self.n}'
        self.n += 1
        self.blocks.setdefault(name, [])
        return name

    def terminated(self):
        lines = self.blocks[self.cur]
        if not lines:
            return False
        last = lines[-1].strip()
        return last.startswith(('br ', 'ret ', 'switch ', 'unreachable')) or 'musttail' in last

    def enter(self, lbl):
        lbl = lbl.rstrip(':').strip()
        if not self.terminated():
            self.emit(f'br label %{lbl}')
        self.blocks.setdefault(lbl, [])
        self.cur = lbl

    def alloca(self, name, ty='i64', init=None):
        an = self.decl_name(name)
        al = 'align 16' if ty == 'i128' else 'align 8'
        self.emit(f'%v.{an} = alloca {ty}, {al}')
        if init is not None:
            self.emit(f'store {ty} {init}, ptr %v.{an}, {al}')
        return an

    def cvt(self, v, src, want, signed_src=False):
        if src == want:
            return v
        r = self.t()
        if INT_W[src] < INT_W[want]:
            # C converts a narrow value by its own signedness: (int64_t)(int32_t)x sign-extends, (uint64_t)(uint32_t)x
            # does not. The C build gets this from the compiler; the IR has to say it.
            self.emit(f'{r} = {"sext" if signed_src else "zext"} {src} {v} to {want}')
        else:
            self.emit(f'{r} = trunc {src} {v} to {want}')
        return r

    def eval(self, e, ty=None):
        if isinstance(e, Num):
            want = ty or e.ty
            if want in ('f32', 'f64', 'float', 'double'):
                return 'f64' if want in ('f64', 'double') else 'f32', f'{e.v:.6e}'
            if want not in INT_W:
                raise ValueError(f'num into {want}')
            return want, str(e.v & ((1 << INT_W[want]) - 1))
        if isinstance(e, VecElem):
            elt = LLVM_TYPE.get(VS_MAP[e.name][0], VS_MAP[e.name][0])
            _, iv = self.eval(e.idx, 'i64')
            r1 = self.t()
            self.emit(f'{r1} = getelementptr %Cpu, ptr %c, i32 0, i32 14')
            r2 = self.t()
            self.emit(f'{r2} = getelementptr inbounds [32 x i128], ptr {r1}, i64 0, i64 {iv}')
            if ty == 'ptr':
                return 'ptr', r2
            r3 = self.t()
            self.emit(f'{r3} = load {elt}, ptr {r2}, align 1')
            want = ty or elt
            if want != elt and want in INT_W and elt in INT_W:
                return want, self.cvt(r3, elt, want)
            return elt, r3
        if isinstance(e, FlagField):
            midx = {'a': 0, 'b': 1, 'nz': 2}[e.member]
            r = self.t()
            self.emit(f'{r} = getelementptr %Flags, ptr %v.F, i32 0, i32 {midx}')
            if ty == 'ptr':
                return 'ptr', r
            r2 = self.t()
            self.emit(f'{r2} = load i64, ptr {r}, align 8')
            return 'i64', r2
        if isinstance(e, Ref) and e.name == 'c':
            return 'ptr', '%c'   # the function parameter itself
        if isinstance(e, Ref) and e.name in CC_CONSTS:
            want = ty or 'i32'
            return want, str(CC_CONSTS[e.name] & ((1 << INT_W[want]) - 1))
        if isinstance(e, Ref) and e.name in ATOMIC_ORDERS:
            return 'i32', str(ATOMIC_ORDERS[e.name])
        if isinstance(e, Ref):
            if e.name not in self.scopes[-1] and e.name not in CC_CONSTS                     and e.name not in ATOMIC_ORDERS and e.name != 'c':
                raise ValueError(f'unknown name {e.name!r} (enum const, global or unhandled macro)')
            want = ty or self.ty_scopes[-1].get(e.name, 'i64')
            if e.name in CC_CONSTS:
                return want, str(CC_CONSTS[e.name] & ((1 << INT_W[want]) - 1))
            r = self.t()
            an = self.scopes[-1].get(e.name, e.name)
            self.emit(f'{r} = load {want}, ptr %v.{an}, align 8')
            return want, r
        if isinstance(e, CpuField):
            return self.cpu_field(e, ty)
        if isinstance(e, Cast):
            want = e.ty
            if want == 'void':
                self.eval(e.e)
                return 'void', None
            st, v = self.eval(e.e, want)
            if st not in INT_W or want not in INT_W:
                return want, v
            return want, self.cvt(v, st, want, is_signed(e.e))
        if isinstance(e, Un):
            if e.op == '-':
                t, v = self.eval(e.e, ty or 'i64')
                r = self.t()
                self.emit(f'{r} = sub {t} 0, {v}')
                return t, r
            if e.op == '~':
                t, v = self.eval(e.e, ty or 'i64')
                r = self.t()
                self.emit(f'{r} = xor {t} {v}, -1')
                return t, r
            if e.op == '&' and isinstance(e.e, Ref):
                return 'ptr', f'%v.{e.e.name}'   # &local: the alloca itself is the pointer
            raise ValueError(f'unary {e.op}')
        if isinstance(e, Bin):
            return self.bin(e, ty)
        if isinstance(e, Call) and e.name in COND_SHIFTS:
            t, v = self.eval(Ref('F') if False else CpuField('nzcv'), 'i64')
            r = self.t()
            self.emit(f'{r} = lshr i64 {v}, {COND_SHIFTS[e.name]}')
            r2 = self.t()
            self.emit(f'{r2} = and i64 {r}, 1')
            return 'i64', r2
        if isinstance(e, Call):
            return self.call(e, ty)
        if isinstance(e, Tern):
            return self.tern(e, ty)
        raise ValueError(type(e))

    def tern(self, e, ty):
        tmp = f'%tern.{self.n}'
        self.n += 1
        it = ty or 'i64'
        self.emit(f'{tmp} = alloca {it}, align 8')
        c = self.eval_i1(e.c)
        tb = self.block('tern.t'); fb = self.block('tern.f'); endb = self.block('tern.end')
        self.emit(f'br i1 {c}, label %{tb}, label %{fb}')
        self.cur = tb
        t, v = self.eval(e.a, it)
        self.emit(f'store {it} {v}, ptr {tmp}, align 8')
        self.emit(f'br label %{endb}')
        self.cur = fb
        t, v = self.eval(e.b, it)
        self.emit(f'store {it} {v}, ptr {tmp}, align 8')
        self.emit(f'br label %{endb}')
        self.cur = endb
        r = self.t()
        self.emit(f'{r} = load {it}, ptr {tmp}, align 8')
        return it, r

    def eval_i1(self, e):
        if isinstance(e, Bin) and e.op in CMP_IR:
            t, a = self.eval(e.a)
            _, b = self.eval(e.b, t)
            r = self.t()
            self.emit(f'{r} = icmp {CMP_IR[e.op]} {t} {a}, {b}')
            return r
        t, v = self.eval(e)
        r = self.t()
        self.emit(f'{r} = icmp ne {t} {v}, 0')
        return r

    def cpu_field(self, e, ty):
        ci = CPU_IDX[e.name]
        r1 = self.t()
        self.emit(f'{r1} = getelementptr %Cpu, ptr %c, i32 0, i32 {ci}')
        if e.name in ('x', 'v'):
            elt = 'i64' if e.name == 'x' else 'i128'
            _, iv = self.eval(e.idx, 'i64')
            r2 = self.t()
            self.emit(f'{r2} = getelementptr inbounds [32 x {elt}], ptr {r1}, i64 0, i64 {iv}')
            if ty == 'ptr':
                return 'ptr', r2
            r3 = self.t()
            al = 'align 8' if e.name == 'x' else 'align 16'
            self.emit(f'{r3} = load {elt}, ptr {r2}, {al}')
            return elt, r3
        if ty == 'ptr':
            return 'ptr', r1
        r2 = self.t()
        self.emit(f'{r2} = load i64, ptr {r1}, align 8')
        return 'i64', r2

    def bin(self, e, ty):
        if e.op in ('<<', '>>'):
            # The lifter writes an arithmetic shift as (int64_t)x >> n, which LLVM only keeps signed as ashr. Both
            # AArch64 and x86 mask a variable count to the width; LLVM's shift is poison past it, and the C macro
            # relied on the host's implicit masking, so a count of 64 or more never showed in the C build.
            signed = is_signed(e.a)
            t, a = self.eval(e.a, ty or 'i64')
            _, sh = self.eval(e.b, t)
            if INT_W.get(t, 0) > 64:
                sh = self.cvt(sh, 'i64', t)   # shift counts promote to the value's width
            m = self.t()
            self.emit(f'{m} = and {t} {sh}, {INT_W[t] - 1}')
            r = self.t()
            self.emit(f'{r} = {"shl" if e.op == "<<" else ("ashr" if signed else "lshr")} {t} {a}, {m}')
            return t, r
        if e.op in CMP_IR:
            t, a = self.eval(e.a)
            _, b = self.eval(e.b, t)
            r = self.t()
            self.emit(f'{r} = icmp {CMP_IR[e.op]} {t} {a}, {b}')
            rb = self.t()
            self.emit(f'{rb} = zext i1 {r} to i32')
            return 'i32', rb
        if e.op in BIN_IR:
            t, a = self.eval(e.a, ty)
            bt, bv = self.eval(e.b, t)
            b = bv
            if bt != t:
                if INT_W.get(t, 0) >= INT_W.get(bt, 0):
                    b = self.cvt(bv, bt, t, is_signed(e.b))
                else:
                    a = self.cvt(a, t, bt)
                    t = bt
            r = self.t()
            self.emit(f'{r} = {BIN_IR[e.op]} {t} {a}, {b}')
            return t, r
        if e.op in ('&&', '||'):
            c = self.eval_i1(e)
            r = self.t()
            self.emit(f'{r} = zext i1 {c} to i32')
            return 'i32', r
        raise ValueError(f'bin {e.op}')

    def call(self, e, ty):
        name = e.name
        if name in ('ROR64', 'ROR32'):
            # ROR64(v, r) = (v >> (r & 63)) | (v << ((64 - (r & 63)) & 63)) — cpu.h macro
            bits = 64 if name == 'ROR64' else 32
            t = 'i64' if bits == 64 else 'i32'
            _, v = self.eval(e.args[0], t)
            _, r = self.eval(e.args[1], t)
            masked = self.t()
            mask = str(bits - 1)
            self.emit(f'{masked} = and {t} {r}, {mask}')
            shr = self.t()
            self.emit(f'{shr} = lshr {t} {v}, {masked}')
            remain = self.t()
            self.emit(f'{remain} = sub {t} {bits}, {masked}')
            rot = self.t()
            self.emit(f'{rot} = and {t} {remain}, {mask}')   # (64 - 0) & 63: a count of 64 would be poison
            shifted = self.t()
            self.emit(f'{shifted} = shl {t} {v}, {rot}')
            res = self.t()
            self.emit(f'{res} = or {t} {shr}, {shifted}')
            return t, res
        real_extern = name.startswith('rt_') and name != 'rt_fpcr_sync'
        if name not in ALLOWED and not real_extern and not re.match(r'^f_[0-9a-f]+$', name) \
                and name not in LD_SIZES:
            raise ValueError(f'call to {name!r}: not a helper in the signature table (macro or unknown)')
        if name in LD_SIZES and name.startswith('LD'):
            _, a = self.eval(e.args[0], 'i64')
            r = self.t()
            self.emit(f'{r} = inttoptr i64 {a} to ptr')
            rv = self.t()
            self.emit(f'{rv} = load {LD_SIZES[name]}, ptr {r}, align 1')
            want = ty or 'i64'
            return want, self.cvt(rv, LD_SIZES[name], want)
        if name == '__atomic_thread_fence':
            self.eval(e.args[0])
            self.emit(f'fence seq_cst')
            return 'void', None
        if name in LD_SIZES and name.startswith('ST'):
            _, a = self.eval(e.args[0], 'i64')
            r = self.t()
            self.emit(f'{r} = inttoptr i64 {a} to ptr')
            sz = LD_SIZES[name]
            vt, v = self.eval(e.args[1], sz)
            if vt in INT_W and sz in INT_W:
                v = self.cvt(v, vt, sz)
            self.emit(f'store {sz} {v}, ptr {r}, align 1')
            return 'void', None
        wrap = not re.match(r'^f_[0-9a-f]+$', name) and not real_extern
        if wrap:
            sig = _tbl['functions'][name]   # the true signature; the gate above guarantees presence
            atys, ret = sig['params'], sig['ret']
            ret = LLVM_TYPE.get(ret, ret)
            name = name + '_w'   # cpu.h/simd.h statics: called through the generated wrapper TU
        self.used_calls.add(name)
        args = []
        for i, a in enumerate(e.args):
            at, av = self.eval(a, atys[i] if wrap and i < len(atys) else None)
            at = LLVM_TYPE.get(at, at) if wrap else at
            args.append(f'{at} {av}')
        if re.match(r'^f_[0-9a-f]+$', name):
            ret = 'void'   # guest functions: void(Cpu *), always
        else:
            ret = _tbl['functions'].get(name[:-2], {}).get('ret') if name.endswith('_w') else None
            if ret is None:
                ret = RET_TYPES.get(name[:-2] if name.endswith('_w') else name, ty or 'i64')
        if ret == 'void':
            self.emit(f'call void @{name}({", ".join(args)})')
            if name in NORETURN:
                self.emit('unreachable')
            return 'void', None
        r = self.t()
        self.emit(f'{r} = call {ret} @{name}({", ".join(args)})')
        return ret, r

import json as _json, pathlib as _path
_tbl = _json.loads((_path.Path(__file__).parent / 'sig' / 'table.json').read_text(encoding='utf-8'))
FN_HEAD = re.compile(r'(?m)^(?:static )?void ((?:f|fb)_[0-9a-f]+(?:_body)?)\(Cpu \*c\) \{')
ALLOWED = set(_tbl['functions']) | {'memcpy', 'memset', 'memmove', '__atomic_thread_fence'}

NORETURN = {'rt_udf', 'rt_brk'}
RET_TYPES = {'rt_dispatch': 'void', 'rt_udf': 'void', 'rt_brk': 'void', 'rt_svc': 'void',
             'rt_spin_gate': 'void', 'rt_hook_run': 'void', 'fnprof_hit': 'void',
             'rt_ticks': 'i64',
             'memcpy': 'void', '__atomic_thread_fence': 'void', 'fc_sub64': 'i32', 'fc_sub32': 'i32', 'fc_add64': 'i32',
             'fc_add32': 'i32', 'fc_logic64': 'i32', 'fc_logic32': 'i32',
             'fnz_sub64': 'i64', 'fnz_sub32': 'i64', 'fnz_add64': 'i64', 'fnz_add32': 'i64',
             'fnz_logic64': 'i64', 'fnz_logic32': 'i64', 'fnz_nz': 'i64'}

# the runtime externals' full signatures, for the .ll preamble's declare lines
RT_DECLARES = {
    'rt_dispatch': ('void', ['ptr']),
    'rt_udf': ('void', ['ptr', 'i64']),
    'rt_brk': ('void', ['ptr', 'i64']),
    'rt_svc': ('void', ['ptr', 'i32', 'i64']),
    'rt_spin_gate': ('void', ['ptr', 'i64']),
    'rt_hook_run': ('void', ['i32', 'ptr', 'ptr']),
    'fnprof_hit': ('void', ['i64', 'i64', 'i64']),
    'rt_ticks': ('i64', []),
}

# ---------------------------------------------------------------- statements

class FnEmitter(Emitter):
    def __init__(self, name):
        super().__init__(name)
        self.taken = set()
        self.scopes = [{}]     # C name -> alloca name
        self.ty_scopes = [{}]  # C name -> IR type it was declared with
        self.jump_ended = False   # the last statement was goto/ret/musttail/switch: no fall-through

    def decl_name(self, name):
        base = self.scopes[-1].get(name, name)
        alloca = base if base not in self.taken else f'{base}.{len(self.taken)}'
        self.taken.add(alloca)
        self.scopes[-1][name] = alloca
        return alloca

    def push(self):
        self.scopes.append(dict(self.scopes[-1]))
        self.ty_scopes.append(dict(self.ty_scopes[-1]))
    def pop(self):
        self.scopes.pop()
        self.ty_scopes.pop()

    def prologue_decl(self, t):
        """`Flags F = {0};` or `uint64_t a = e, b;` → allocas + stores. Returns leftover tokens or None."""
        if t[0] == 'Flags':
            self.emit('%v.F = alloca %Flags, align 8')
            for fidx in (0, 1, 2):
                self.emit(f'%.fca.{fidx}.gep = getelementptr %Flags, ptr %v.F, i32 0, i32 {fidx}')
                self.emit(f'store i64 0, ptr %.fca.{fidx}.gep, align 8')
            i = t.index('}')
            return t[i + 1:] if t[i + 1:i + 2] == [';'] else t[i + 1:]
        ty = t[0]
        decls, cur, depth = [], [], 0
        i = 1
        while i < len(t):
            tk = t[i]
            if tk in '([{':
                depth += 1
            elif tk in ')]}':
                depth -= 1
            if tk == ',' and depth == 0:
                decls.append(cur); cur = []; i += 1; continue
            cur.append(tk)
            i += 1
        if cur:
            decls.append(cur)
        for d in decls:
            if '=' in d:
                j = d.index('=')
                name = d[0]
                an = self.alloca(name, CASTS[ty])
                self.ty_scopes[-1][name] = CASTS[ty]
                ty2, v = self.eval(parse_expr(d[j + 1:]), CASTS[ty])
                self.emit(f'store {ty2} {v}, ptr %v.{an}, align 8')
            else:
                self.alloca(d[0], CASTS[ty])
                self.ty_scopes[-1][d[0]] = CASTS[ty]
        return None

    def statement(self, t):
        if not t:
            return None
        if t[0] == 'FN_ENTER':
            return t[4:] if len(t) > 3 and t[3] == ')' else None
        if t[0] == 'Flags' or (t[0] in CASTS):
            return self.prologue_decl(t)
        if t[0] == 'return' and (len(t) == 1 or t[1] == ';'):
            self.emit('ret void')
            self.jump_ended = True
            return t[2:]
        if t[0] == 'return':
            raise ValueError('non-void return')
        if t[0] == 'goto':
            self.enter(t[1])
            self.jump_ended = True
            return t[2:]
        if t[0] == 'musttail_return':
            e = parse_expr(t[1:])
            if not isinstance(e, Call):
                raise ValueError('musttail of non-call')
            self.used_calls.add(e.name)
            args = []
            for a in e.args:
                at, av = self.eval(a)
                args.append(f'{at} {av}')
            self.emit(f'musttail call void @{e.name}({", ".join(args)})')
            self.emit('ret void')
            self.jump_ended = True
            return None
        if t[0] == 'if':
            return self.if_stmt(t)
        if t[0] == 'switch':
            return self.switch_stmt(t)
        if t[0] == '(' and t[1] == 'void' and t[2] == ')':
            t = t[3:]
        if t[0] == '{':
            inner = t[1:-1]
            depth, cur = 0, []
            stmts = []
            for tk in inner:
                if tk in '([{':
                    depth += 1
                elif tk in ')]}':
                    depth -= 1
                elif tk == ';' and depth == 0:
                    if cur:
                        stmts.append(cur)
                    cur = []
                    continue
                cur.append(tk)
            if cur:
                stmts.append(cur)
            self.push()
            for st in stmts:
                self.statement(st)
            self.pop()
            return None
        depth, eq = 0, None
        for i, tk in enumerate(t):
            if tk == '(':
                depth += 1
            elif tk == ')':
                depth -= 1
            elif tk == '=' and self.peek_eq(t, i) and depth == 0:
                eq = i
                break
        if eq is None:
            e = parse_expr(t)
            self.eval(e)
            self.jump_ended = False
            return None
        lhs = parse_expr(t[:eq])
        rhs = parse_expr(t[eq + 1:])
        if isinstance(lhs, Ref):
            want = self.ty_scopes[-1].get(lhs.name, 'i64')
            ty, v = self.eval(rhs)
            if ty in INT_W and want in INT_W:
                v = self.cvt(v, ty, want)
                ty = want
            an = self.scopes[-1].get(lhs.name, lhs.name)
            self.emit(f'store {ty} {v}, ptr %v.{an}, align 8')
            self.jump_ended = False
            return None
        if isinstance(lhs, FlagField):
            midx = {'a': 0, 'b': 1, 'nz': 2}[lhs.member]
            r = self.t()
            self.emit(f'{r} = getelementptr %Flags, ptr %v.F, i32 0, i32 {midx}')
            ty, v = self.eval(rhs)
            if ty in INT_W and ty != 'i64':
                v = self.cvt(v, ty, 'i64')
                ty = 'i64'
            self.emit(f'store {ty} {v}, ptr {r}, align 8')
            self.jump_ended = False
            return None
        if isinstance(lhs, VecElem):
            _, ptr = self.eval(lhs, ty='ptr')
            elt = LLVM_TYPE.get(VS_MAP[lhs.name][0], VS_MAP[lhs.name][0])
            ty, v = self.eval(rhs, elt)
            if elt in INT_W and ty in INT_W and ty != elt:
                v = self.cvt(v, ty, elt)
                ty = elt
            self.emit(f'store {ty} {v}, ptr {ptr}, align 1')
            self.jump_ended = False
            return None
        if isinstance(lhs, CpuField):
            pt, ptr = self.cpu_field(lhs, ty='ptr')
            want = 'i128' if lhs.name == 'v' else 'i64'
            ty, v = self.eval(rhs)
            if ty in INT_W and want in INT_W:
                v = self.cvt(v, ty, want)
                ty = want
            al = 'align 16' if lhs.name == 'v' else 'align 8'
            self.emit(f'store {ty} {v}, ptr {ptr}, {al}')
            self.jump_ended = False
            return None
        raise ValueError(f'lhs {type(lhs)}')

    def peek_eq(self, t, i):
        return i + 1 >= len(t) or t[i + 1] != '='

    def if_stmt(self, t):
        self.n += 1
        i = 1
        assert t[i] == '('
        depth, j = 0, i
        while True:
            if t[j] == '(':
                depth += 1
            elif t[j] == ')':
                depth -= 1
                if depth == 0:
                    break
            j += 1
        if j >= len(t):
            raise ValueError('unbalanced if condition')
        c = self.eval_i1(parse_expr(t[i + 1:j]))
        then_b = self.block('if.then')
        end_b = self.block('if.end')
        rest = t[j + 1:]
        if not rest:
            raise ValueError('if without a body')
        if rest[0] == ';':   # an empty then-arm, or the splitter's ';'
            rest = rest[1:]
        if not rest:
            raise ValueError('if without a body')
        # find the then-arm's extent, then look for an else after its ';'
        used = self.match_brace(rest, 0) + 1 if rest[0] == '{' else self.stmt_end(rest)
        tail = rest[used:]
        if tail and tail[0] == ';':
            tail = tail[1:]
        has_else = bool(tail) and tail[0] == 'else'
        else_b = self.block('if.else') if has_else else end_b
        self.emit(f'br i1 {c}, label %{then_b}, label %{else_b}')
        self.cur = then_b
        self.statement(rest[:used])
        if not self.terminated() and not self.jump_ended:
            self.enter(end_b)
        self.cur = end_b
        self.jump_ended = False
        if has_else:
            self.cur = else_b
            if len(tail) > 1 and tail[1] == 'if':
                self.if_stmt(tail[1:])
                return None
            used = self.match_brace(tail, 1) + 1 if tail[1:2] == ['{'] else self.stmt_end(tail)
            self.statement(tail[1:used])
            if not self.terminated() and not self.jump_ended:
                self.enter(end_b)
            self.cur = end_b
            self.jump_ended = False
            tail = tail[used:]
        if tail and all(t == ';' for t in tail):
            return None   # the splitter's terminating ';' rides along; nothing follows
        if tail:
            raise ValueError(f'tokens after the if statement: {" ".join(tail[:8])}')
        return None

    def match_brace(self, t, start):
        depth, i = 0, start
        while i < len(t):
            if t[i] == '{':
                depth += 1
            elif t[i] == '}':
                depth -= 1
                if depth == 0:
                    return i
            i += 1
        raise ValueError('unbalanced braces')
    def stmt_end(self, t):
        depth, i = 0, 0
        while i < len(t):
            if t[i] in '([{':
                depth += 1
            elif t[i] in ')]}':
                depth -= 1
            elif t[i] == ';' and depth == 0:
                return i
            i += 1
        return len(t)

    def switch_stmt(self, t):
        self.n += 1
        i, j = t.index('('), t.index(')')
        _, v = self.eval(parse_expr(t[i + 1:j]), 'i64')
        # Token-wise, not a regex over the joined text: ' '.join() puts a space before ':',
        # which the old pattern (r'ull: goto') could never match, dropping every case.
        pairs, k = [], j + 1
        if t[k] != '{':
            raise ValueError('switch without a braced body')
        end = self.match_brace(t, k)
        while k < end:
            if t[k] == 'case' and k + 4 < end and t[k + 2] == ':':
                lab = t[k + 4].rstrip(';')
                if t[k + 3] != 'goto' or not re.fullmatch(r'L_[0-9a-f]+', lab):
                    raise ValueError(f'switch case body is not a goto: {" ".join(t[k:k + 6])}')
                pairs.append((int(t[k + 1].rstrip('uUlL'), 16 if t[k + 1].lower().startswith('0x') else 10), lab))
                k += 6
                continue
            k += 1
        tail = t[end + 1:]
        if tail:
            raise ValueError(f'tokens after the switch body: {" ".join(tail[:8])}')
        endb = self.block('sw.end')
        if pairs:
            cases = ' '.join(f'i64 {v_}, label %{lbl}' for v_, lbl in pairs)
            self.emit(f'switch i64 {v}, label %{endb} [ {cases} ]')
        else:
            self.emit(f'br label %{endb}')
        self.cur = endb
        return None

# ---------------------------------------------------------------- function driver

def _brace_end(t, i):
    """Index of the '}' that matches t[i] == '{'."""
    depth = 0
    while i < len(t):
        if t[i] == '{':
            depth += 1
        elif t[i] == '}':
            depth -= 1
            if depth == 0:
                return i
        i += 1
    raise ValueError('unbalanced braces')


def _paren_end(t, i):
    depth = 0
    while i < len(t):
        if t[i] in '([':
            depth += 1
        elif t[i] in ')]':
            depth -= 1
            if depth == 0:
                return i
        i += 1
    raise ValueError('unbalanced parentheses')


def _simple_end(t, i):
    """Index of the ';' ending the statement that starts at t[i]."""
    depth = 0
    while i < len(t):
        if t[i] in '([{':
            depth += 1
        elif t[i] in ')]}':
            depth -= 1
        elif t[i] == ';' and depth == 0:
            return i
        i += 1
    raise ValueError('statement without a ;')


def _construct_end(t, i):
    """Index of the last token of the if/switch statement starting at t[i].

    A braced if/else body ends at its '}' and carries no ';', so scanning to the next
    top-level ';' would swallow the statements that follow it. That merge is what dropped
    the tail of every `if (...) { ... }` (86,650 functions of main, measured 2026-11-07).
    """
    kw = t[i]
    j = _paren_end(t, i + 1)
    if kw == 'switch':
        if t[j + 1] != '{':
            raise ValueError('switch without a braced body')
        return _brace_end(t, j + 1)

    def body(k):
        return _brace_end(t, k) if t[k] == '{' else _simple_end(t, k)

    j = body(j + 1)
    while j + 1 < len(t) and t[j + 1] == 'else':
        k = j + 2
        j = _construct_end(t, k) if t[k] == 'if' else body(k)
    return j


def parse_function(text):
    body = text[text.index('{') + 1: text.rindex('}')]
    body = re.sub(r'__attribute__\(\(musttail\)\)\s*return', 'musttail_return', body)
    out = []
    for piece in re.split(r'(?m)^(\s*L_[0-9a-f]+:\s*;)\s*$', body, flags=re.M):
        if piece.startswith('L_'):
            out.append(('label', piece.strip()[:-1].strip()))
            continue
        t = toks(piece)
        i, depth, start = 0, 0, 0
        while i < len(t):
            tk = t[i]
            if depth == 0 and i == start and tk in ('if', 'switch'):
                j = _construct_end(t, i)
                out.append(('stmt', t[i:j + 1]))
                i = j + 1
                start = i
                continue
            if tk == '{' and depth == 0 and i == start:
                j = _brace_end(t, i)
                out.append(('stmt', t[i:j + 1]))
                i = j + 1
                start = i
                continue
            if tk in '([{':
                depth += 1
            elif tk in ')]}':
                depth -= 1
            elif tk == ';' and depth == 0:
                if t[start:i]:
                    out.append(('stmt', t[start:i]))
                start = i + 1
            i += 1
        if t[start:]:
            out.append(('stmt', t[start:]))
    return out

def emit_function(name, text):
    """Returns (.ll text, used calls) or (None, reason) when the grammar resists."""
    em = FnEmitter(name)
    try:
        for kind, st in parse_function(text):
            if kind == 'label':
                em.enter(st.split()[0])
                em.jump_ended = False
            else:
                try:
                    em.statement(st)
                except Exception as ex:
                    raise ValueError(f'{" ".join(st)[:70]} -> {ex}') from None
    except Exception as ex:
        return None, f'{name}: {ex}'
    out = [f'define void @{name}(ptr %c) {{', 'entry:']
    for lbl, lines in em.blocks.items():
        if lbl != 'entry':
            out.append(f'{lbl}:')
        out.extend(lines)
    out.append('}')
    return '\n'.join(out), sorted(em.used_calls)

def split_functions(chunk_text):
    """[(name, full text)] of every f_/fb_ definition in a chunk, brace-matched."""
    out = []
    for m in FN_HEAD.finditer(chunk_text):
        start = m.start()
        i = chunk_text.index('{', m.end() - 1)
        depth, j = 0, i
        while j < len(chunk_text):
            if chunk_text[j] == '{':
                depth += 1
            elif chunk_text[j] == '}':
                depth -= 1
                if depth == 0:
                    break
            j += 1
        out.append((m.group(1), chunk_text[start:j + 1]))
    return out

def emit_chunk_partial(chunk_text):
    """Per-function mixing: returns (.ll text, residual C text, helpers, guest calls).

    The .ll defines the functions that translated; the residual C (the chunk's own
    header — includes and prototypes — plus the rest of the functions) defines the
    others. Both TUs are compiled and linked; cross-side guest calls resolve through
    the prototypes. Returns (None, chunk_text, fails, set()) when nothing translated.
    Static pairs (fb_ + its f_ wrapper, a hook body + its wrapper) stay on one side:
    a static definition cannot satisfy the other TU's reference."""
    first = FN_HEAD.search(chunk_text)
    if not first:
        return None, chunk_text, [('chunk', 'no functions found')], set()
    header = chunk_text[:first.start()]
    fns = split_functions(chunk_text)
    src = {name: ftext for name, ftext in fns}
    translated, residual, fails = {}, [], []
    # fb_ fallbacks are static and only their f_ wrapper calls them (musttail); the hook
    # body/wrapper pair is the same shape. Keep each static pair on the C route: a static
    # definition cannot satisfy the other TU's reference, and the IR side cannot take the
    # callback's address without more plumbing.
    static_pairs = {n for n, _ in fns if n.endswith('_body')} |                     {n[:-5] for n, _ in fns if n.endswith('_body')} |                     {n for n, _ in fns if n.startswith('fb_')} |                     {'f_' + n[3:] for n, _ in fns if n.startswith('fb_')}
    for name, ftext in fns:
        ll, used = emit_function(name, ftext)
        if ll is None or name in static_pairs:
            if ll is None:
                fails.append((name, used))
            residual.append((name, ftext))
        else:
            translated[name] = (ll, used)
    # static-pair coupling: fb_X is static and only its f_X wrapper calls it; a hook
    # body f_X_body is static too. Keep each pair on one side (the residual side).
    changed = True
    while changed:
        changed = False
        for name in list(translated):
            pair = None
            if name.startswith('fb_'):
                pair = 'f_' + name[3:]
            elif name.endswith('_body'):
                pair = name[:-5]
            if pair and pair in residual_names(residual):
                fails.append((name, 'static pair with residual ' + pair))
                residual.append((name, src[name]))
                del translated[name]
                changed = True
            elif pair and pair in translated and pair not in residual_names(residual):
                pass   # both translated: fine (same TU, same side)
    if not translated:
        return None, chunk_text, fails, set()
    helpers, calls, ir_defined = set(), set(), set()
    for name, (ll, used) in translated.items():
        helpers |= {u for u in used if u.endswith('_w')}
        calls |= {u for u in used if re.match(r'^(f|fb)_[0-9a-f]+$', u)}
        ir_defined.add(name)
    preamble = [CPU_TYPE, '%Flags = type { i64, i64, i64 }']
    for rt_name, (ret, atys) in sorted(RT_DECLARES.items()):
        preamble.append(f'declare {ret} @{rt_name}({", ".join(atys)})')
    for g in sorted(calls - ir_defined):
        preamble.append(f'declare void @{g}(ptr)')
    for h in sorted(helpers):
        sig = _tbl['functions'][h[:-2]]
        params = ', '.join(LLVM_TYPE.get(t_, t_) for t_ in sig['params'])
        preamble.append(f'declare {LLVM_TYPE.get(sig["ret"], sig["ret"])} @{h}({params})')
    ll = '\n\n'.join(preamble + [ll for name, (ll, _) in translated.items()]) + '\n'
    res = ''
    if residual:
        # the residual's own prototypes: chunk-internal statics (fb_, hook bodies)
        # and anything the residual calls that is defined later in it
        protos = ''.join(('static void %s(Cpu *c);\n') % n if (n.startswith('fb_') or n.endswith('_body')) else ('void %s(Cpu *c);\n') % n for n, _ in residual)
        res = header + protos + ''.join(ftext for name, ftext in residual) + '\n'
    else:
        res = ''

    return ll, res, helpers, calls

def residual_names(residual):
    return {name for name, _ in residual}

def main():
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument('chunks', nargs='+')
    ap.add_argument('--fn', help='emit one function to stdout (debug)')
    ap.add_argument('--stats', action='store_true', help='parse-coverage report over the chunks')
    a = ap.parse_args()

    if a.stats:
        from collections import Counter
        ok = fail = 0
        reasons = Counter()
        per_chunk = []
        for chunk in a.chunks:
            s = open(chunk, encoding='utf-8').read()
            c_ok = c_all = 0
            for fm in re.finditer(r'void (f_[0-9a-f]+)\(Cpu \*c\) \{', s):
                start = fm.start()
                # the body ends at the matching closing brace of this function
                depth, i = 0, s.index('{', start)
                while True:
                    if s[i] == '{':
                        depth += 1
                    elif s[i] == '}':
                        depth -= 1
                        if depth == 0:
                            break
                    i += 1
                name = fm.group(1)
                c_all += 1
                ll, why = emit_function(name, s[start:i + 1])
                if ll is not None:
                    ok += 1; c_ok += 1
                else:
                    fail += 1
                    reasons[why.split('->')[0].strip()[:60]] += 1
            per_chunk.append((chunk, c_ok, c_all))
        print(f'IR-eligible: {ok}/{ok + fail} functions ({100 * ok / max(1, ok + fail):.1f}%)')
        print('failure reasons:')
        for why, n in reasons.most_common(15):
            print(f'  {n:6d}  {why}')
        worst = sorted(per_chunk, key=lambda x: x[1] / max(1, x[2]))[:5]
        print('lowest-coverage chunks:')
        for chunk, c_ok, c_all in worst:
            print(f'  {chunk}: {c_ok}/{c_all}')
        return

    chunk, fname = a.chunks[0], a.fn
    s = open(chunk, encoding='utf-8').read()
    m = re.search(r'(void ' + re.escape(fname) + r'\(Cpu \*c\) \{.*?\n\})', s, re.S)
    if not m:
        sys.exit(f'{fname} not found in {chunk}')
    ll, wraps = emit_function(fname, m.group(1))
    if ll is None:
        sys.exit(f'{fname} not IR-eligible: {wraps}')
    known = {'rt_dispatch', 'rt_udf', 'rt_brk', 'rt_svc', 'rt_spin_gate', 'rt_hook_run', 'fnprof_hit'}
    helpers = [w for w in wraps if not re.match(r'^(f|fb)_[0-9a-f]+$', w) and w not in known]
    preamble = [CPU_TYPE, '%Flags = type { i64, i64, i64 }',
                'declare void @rt_dispatch(ptr)', 'declare void @rt_udf(ptr, i64)',
                'declare void @rt_brk(ptr, i64)', 'declare void @rt_svc(ptr, i32, i64)',
                'declare void @rt_spin_gate(ptr, i64)', 'declare void @fnprof_hit(i64, i64, i64)']
    wrappers_c = ['#include "cpu.h"', '#include "simd.h"', '']
    CTY = {'i64': 'uint64_t', 'i32': 'uint32_t', 'i16': 'uint16_t', 'i8': 'uint8_t',
           'i128': '__uint128_t', 'ptr': 'void *', 'f32': 'float', 'f64': 'double'}
    for h in helpers:
        orig = h[:-2] if h.endswith('_w') else h
        sig = _tbl['functions'][orig]
        preamble.append(f'declare {sig["ret"]} @{h}({", ".join(sig["params"])})')
        params = ', '.join(f'{CTY[t]} a{i}' for i, t in enumerate(sig['params']))
        call_args = ', '.join(f'a{i}' for i in range(len(sig['params'])))
        wrappers_c.append(f'{sig["ret"]} {h}({params}) {{ return {orig}({call_args}); }}')
    print('\n'.join(preamble))
    print(ll)
    open('ir_wrappers.c', 'w', encoding='utf-8', newline='\n').write('\n'.join(wrappers_c))

if __name__ == '__main__':
    main()
