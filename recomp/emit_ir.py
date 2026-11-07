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
class CpuField:
    def __init__(self, name, idx=None): self.name, self.idx = name, idx
class Cast:
    def __init__(self, ty, e): self.ty, self.e = ty, e
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
         'u128': 'i128', '__uint128_t': 'i128', '__int128': 'i128', 'float': 'f32', 'double': 'f64'}
INT_W = {'i8': 8, 'i16': 16, 'i32': 32, 'i64': 64, 'i128': 128}
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
            self.take(); ty = CASTS[self.take()]; self.take()
            return Cast(ty, self.unary())
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
            if self.peek() == '(':
                self.take()
                args = []
                if self.peek() != ')':
                    args.append(self.expr())
                    while self.peek() == ',':
                        self.take(); args.append(self.expr())
                self.expect(')')
                return Call(t, args)
            if t == 'c' and self.peek() == '->':
                self.take()          # '->'
                name = self.take()   # the field name
                if self.peek() == '[':
                    self.take(); idx = self.expr(); self.expect(']')
                    return CpuField(name, idx)
                return CpuField(name)
            if t == 'F' and self.peek() == '.':
                self.take(); self.take()
                return FlagField(self.take())
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

class Emitter:
    def __init__(self, name):
        self.name = name
        self.blocks = {'entry': []}
        self.cur = 'entry'
        self.n = 0
        self.used_calls = set()
        self.wrappers = {}   # name -> (ret ty, arg types); signatures from the first call site

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
        return last.startswith(('br ', 'ret ', 'switch ')) or 'musttail' in last

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

    def cvt(self, v, src, want):
        if src == want:
            return v
        r = self.t()
        if INT_W[src] < INT_W[want]:
            self.emit(f'{r} = zext {src} {v} to {want}')
        else:
            self.emit(f'{r} = trunc {src} {v} to {want}')
        return r

    def eval(self, e, ty=None):
        if isinstance(e, Num):
            want = ty or e.ty
            if want not in INT_W:
                raise ValueError(f'num into {want}')
            return want, str(e.v & ((1 << INT_W[want]) - 1))
        if isinstance(e, FlagField):
            midx = {'a': 0, 'b': 1, 'nz': 2}[e.member]
            r = self.t()
            self.emit(f'{r} = getelementptr %Flags, ptr %v.F, i32 0, i32 {midx}')
            if ty == 'ptr':
                return 'ptr', r
            r2 = self.t()
            self.emit(f'{r2} = load i64, ptr {r}, align 8')
            return 'i64', r2
        if isinstance(e, Ref) and e.name in ('rt_patch_on', 'g_fnprof'):
            raise ValueError(f'global {e.name!r}: needs the Phase-1 indexed-global path')
        if isinstance(e, Ref) and e.name == 'c':
            return 'ptr', '%c'   # the function parameter itself
        if isinstance(e, Ref) and e.name in CC_CONSTS:
            want = ty or 'i32'
            return want, str(CC_CONSTS[e.name] & ((1 << INT_W[want]) - 1))
        if isinstance(e, Ref):
            if e.name not in self.scopes[-1] and e.name not in CC_CONSTS and e.name != 'c':
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
            return want, self.cvt(v, st, want)
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
            t, a = self.eval(e.a, ty or 'i64')
            _, sh = self.eval(e.b, t)
            r = self.t()
            self.emit(f'{r} = {"shl" if e.op == "<<" else "lshr"} {t} {a}, {sh}')
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
            _, b = self.eval(e.b, t)
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
        if name in LD_SIZES and name.startswith('LD'):
            _, a = self.eval(e.args[0], 'i64')
            r = self.t()
            self.emit(f'{r} = inttoptr i64 {a} to ptr')
            rv = self.t()
            self.emit(f'{rv} = load {LD_SIZES[name]}, ptr {r}, align 1')
            want = ty or 'i64'
            return want, self.cvt(rv, LD_SIZES[name], want)
        if name in LD_SIZES and name.startswith('ST'):
            _, a = self.eval(e.args[0], 'i64')
            r = self.t()
            self.emit(f'{r} = inttoptr i64 {a} to ptr')
            sz = LD_SIZES[name]
            _, v = self.eval(e.args[1], sz)
            self.emit(f'store {sz} {v}, ptr {r}, align 1')
            return 'void', None
        args = []
        for a in e.args:
            at, av = self.eval(a)
            args.append((at, av))
        if re.match(r'^f_[0-9a-f]+$', name):
            self.used_calls.add(name)
            self.emit(f'call void @{name}(' + ', '.join(f'{t} {v}' for t, v in args) + ')')
            return 'void', None
        if name.startswith('rt_') or name in ('memcpy', '__atomic_thread_fence'):
            ret = RET_TYPES.get(name, 'void')
            self.used_calls.add(name)
        else:
            # cpu.h/simd.h statics: the call site knows the argument types, so the wrapper's
            # signature comes from here (the first site wins; C implicit conversions at the
            # wrapper tolerate the rest)
            name = name + '_w'
            sig = self.wrappers.get(name)
            atys = tuple(t for t, _ in args)
            if sig is None:
                ret = ty or 'i64'
                self.wrappers[name] = (ret, atys)
            else:
                ret, atys = sig
                args = [(atys[i], self.cvt(v, t, atys[i])) for i, (t, v) in enumerate(args[:len(atys)])]
            self.used_calls.add(name)
        if ret == 'void':
            self.emit(f'call void @{name}(' + ', '.join(f'{t} {v}' for t, v in args) + ')')
            return 'void', None
        r = self.t()
        self.emit(f'{r} = call {ret} @{name}(' + ', '.join(f'{t} {v}' for t, v in args) + ')')
        return ret, r

RET_TYPES = {'rt_dispatch': 'void', 'rt_udf': 'void', 'rt_brk': 'void', 'rt_svc': 'void',
             'rt_spin_gate': 'void', 'rt_hook_run': 'void', 'fnprof_hit': 'void',
             'memcpy': 'void', '__atomic_thread_fence': 'void', 'rt_fpcr_sync': 'void',
             'fc_sub64': 'i32', 'fc_sub32': 'i32', 'fc_add64': 'i32',
             'fc_add32': 'i32', 'fc_logic64': 'i32', 'fc_logic32': 'i32',
             'fnz_sub64': 'i32', 'fnz_sub32': 'i32', 'fnz_add64': 'i32', 'fnz_add32': 'i32',
             'fnz_logic64': 'i32', 'fnz_logic32': 'i32'}

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
                self.alloca(name, CASTS[ty])
                self.ty_scopes[-1][name] = CASTS[ty]
                ty2, v = self.eval(parse_expr(d[j + 1:]), CASTS[ty])
                self.emit(f'store {ty2} {v}, ptr %v.{name}, align 8')
            else:
                self.alloca(d[0], CASTS[ty])
                self.ty_scopes[-1][name] = CASTS[ty]
        return None

    def statement(self, t):
        if not t:
            return None
        if self.terminated() and t[0] != 'L_':
            return None   # dead code after an unconditional terminator (rt_udf tails)
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
        c = self.eval_i1(parse_expr(t[i + 1:j]))
        then_b = self.block('if.then')
        end_b = self.block('if.end')
        rest = t[j + 1:]
        has_else = bool(rest) and rest[0] == 'else'
        else_b = self.block('if.else') if has_else else end_b
        self.emit(f'br i1 {c}, label %{then_b}, label %{else_b}')
        self.cur = then_b
        if rest[0] == '{':
            self.statement(rest[:self.match_brace(rest, 0) + 1])
        else:
            self.statement(rest[:self.stmt_end(rest)])
        if not self.terminated() and not self.jump_ended:
            self.enter(end_b)
        self.cur = end_b
        self.jump_ended = False
        if has_else:
            self.cur = else_b
            if rest[0] == '{':
                tail = rest[self.match_brace(rest, 0) + 1:]
            else:
                tail = rest[self.stmt_end(rest) + 1:]
            if tail and tail[0] == 'if':
                self.if_stmt(tail)
            elif tail and tail[0] == '{':
                self.statement(tail[:self.match_brace(tail, 0) + 1])
            elif tail:
                self.statement(tail[:self.stmt_end(tail)])
            self.cur = end_b
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
        flat = ' '.join(t)
        pairs = [(int(a, 16), b) for a, b in re.findall(r'case\s+(0x[0-9a-fA-F]+)ull\s*:\s*goto\s+(L_[0-9a-f]+)\s*;', flat)]
        endb = self.block('sw.end')
        if pairs:
            cases = ', '.join(f'i64 {hex(v_)}: label %{lbl}' for v_, lbl in pairs)
            self.emit(f'switch i64 {v}, label %{endb} [ {cases} ]')
        else:
            self.emit(f'br label %{endb}')
        self.cur = endb
        return None

# ---------------------------------------------------------------- function driver

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
            if tk == '{' and depth == 0 and i == start:
                d2, j = 0, i
                while j < len(t):
                    if t[j] == '{':
                        d2 += 1
                    elif t[j] == '}':
                        d2 -= 1
                        if d2 == 0:
                            break
                    j += 1
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
    except ValueError as ex:
        return None, str(ex)
    out = [f'define void @{name}(ptr %c) {{', 'entry:']
    for lbl, lines in em.blocks.items():
        if lbl != 'entry':
            out.append(f'{lbl}:')
        out.extend(lines)
    out.append('}')
    return '\n'.join(out), sorted(em.used_calls)

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
    helpers = [w for w in wraps if not re.match(r'^f_[0-9a-f]+$', w) and w not in known]
    preamble = [CPU_TYPE, '%Flags = type { i64, i64, i64 }',
                'declare void @rt_dispatch(ptr)', 'declare void @rt_udf(ptr, i64)',
                'declare void @rt_brk(ptr, i64)', 'declare void @rt_svc(ptr, i32, i64)',
                'declare void @rt_spin_gate(ptr, i64)', 'declare void @fnprof_hit(i64, i64, i64)']
    wrappers_c = ['#include "cpu.h"', '#include "simd.h"', '']
    for h in helpers:
        rn = h
        orig = h[:-2] if h.endswith('_w') else h
        if orig.startswith('fc_'):
            preamble.append(f'declare i32 @{rn}(ptr, i32)')
            wrappers_c.append(f'int32_t {rn}(Flags *f, int32_t cc) {{ return {orig}(f, cc); }}')
        elif orig.startswith('fnz_'):
            preamble.append(f'declare i32 @{rn}(ptr)')
            wrappers_c.append(f'int32_t {rn}(Flags *f) {{ return {orig}(f); }}')
        elif orig.endswith('64'):
            preamble.append(f'declare i64 @{rn}(ptr, i64, i64)')
            wrappers_c.append(f'uint64_t {rn}(Flags *f, uint64_t a, uint64_t b) {{ return {orig}(f, a, b); }}')
        else:
            preamble.append(f'declare i32 @{rn}(ptr, i32, i32)')
            wrappers_c.append(f'uint32_t {rn}(Flags *f, uint32_t a, uint32_t b) {{ return {orig}(f, a, b); }}')
    print('\n'.join(preamble))
    print(ll)
    open('ir_wrappers.c', 'w', encoding='utf-8', newline='\n').write('\n'.join(wrappers_c))

if __name__ == '__main__':
    main()
