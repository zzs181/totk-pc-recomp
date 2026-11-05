"""The build window: .venv\\Scripts\\python tools\\build_gui.py

Two jobs: run this checkout's build steps in order and show their output as it lands, or watch a build that another
terminal already started (Windows gives no way to read back another process's console, so the watcher counts the
objects Ninja writes -- that is the same number the build itself is made of).

Settings are the window's own, in build/build_gui.json (build/ is ignored: nothing game-derived leaves it). The ExeFS
and RomFS fields start empty and mean "ask build_game.py", which defaults them to <work dir>/extracted/<version>;
filling them in is what bakes those paths into totk.exe (tools/build_game.py writes rt_default_exefs / rt_default_romfs
into build/<out>/modules.c), so a window opened over a build that exists prefills them from that file.

    --shot PATH    draw the window, save its PNG to PATH and exit: how a change here is checked
"""
import argparse
import json
import re
import sys
import time
from collections import deque
from pathlib import Path

from PySide6.QtCore import Qt, QTimer, QProcess, QProcessEnvironment
from PySide6.QtGui import QColor, QFontDatabase, QPalette
from PySide6.QtWidgets import (QApplication, QCheckBox, QComboBox, QFileDialog, QFrame, QGridLayout, QHBoxLayout,
                               QLabel, QLineEdit, QMainWindow, QPlainTextEdit, QProgressBar, QPushButton, QScrollArea,
                               QSplitter, QTabWidget, QVBoxLayout, QWidget)

ROOT = Path(__file__).resolve().parents[1]
CONFIG = ROOT / 'build' / 'build_gui.json'
PY = ROOT / '.venv' / 'Scripts' / 'python.exe'
PY314 = ROOT / '.venv-py314' / 'Scripts' / 'python.exe'

INK = '#1d1f24'        # the window: warm dark, not the blue-grey of a template admin panel
INK_2 = '#25282f'
EDGE = '#3a3f4a'
TEXT = '#e6e2d9'
DIM = '#9a9488'
GOOD = '#7fd18b'
WARN = '#e8b13c'
BAD = '#ef6c5a'


def apply_theme(app):
    p = QPalette()
    p.setColor(QPalette.Window, QColor(INK))
    p.setColor(QPalette.Base, QColor(INK_2))
    p.setColor(QPalette.AlternateBase, QColor(INK))
    p.setColor(QPalette.Text, QColor(TEXT))
    p.setColor(QPalette.WindowText, QColor(TEXT))
    p.setColor(QPalette.Button, QColor(INK_2))
    p.setColor(QPalette.ButtonText, QColor(TEXT))
    p.setColor(QPalette.Highlight, QColor(WARN))
    p.setColor(QPalette.HighlightedText, QColor(INK))
    p.setColor(QPalette.ToolTipBase, QColor(INK_2))
    p.setColor(QPalette.ToolTipText, QColor(TEXT))
    app.setPalette(p)
    app.setStyle('Fusion')
    app.setStyleSheet(f"""
        QWidget {{ background: {INK}; color: {TEXT}; font-size: 14px; }}
        QTabWidget::pane {{ border: 1px solid {EDGE}; }}
        QTabBar::tab {{ padding: 8px 16px; border: 1px solid {EDGE}; border-bottom: none; }}
        QTabBar::tab:selected {{ background: {INK_2}; color: {WARN}; }}
        QLineEdit, QComboBox {{ background: {INK_2}; border: 1px solid {EDGE}; padding: 4px 6px; }}
        QListWidget {{ background: {INK_2}; border: 1px solid {EDGE}; }}
        QFrame#card {{ background: {INK_2}; border: 1px solid {EDGE}; border-radius: 6px; }}
        QLabel#dim {{ color: {DIM}; }}
        QProgressBar {{ background: {INK_2}; border: 1px solid {EDGE}; text-align: center; color: {TEXT}; }}
        QProgressBar::chunk {{ background: {GOOD}; }}
        QPushButton {{ background: {INK_2}; border: 1px solid {EDGE}; padding: 6px 14px; border-radius: 4px; }}
        QPushButton:hover {{ border-color: {WARN}; }}
        QPushButton#go {{ background: #2b4230; border-color: {GOOD}; color: {GOOD}; font-weight: bold; }}
        QPushButton#stop {{ background: #43251f; border-color: {BAD}; color: {BAD}; }}
        QPushButton:disabled {{ color: {DIM}; border-color: {EDGE}; }}
    """)


# ---- the steps

NINJA_EDGE = re.compile(r'^\[(\d+)/(\d+)\]')
PROMPT_SETS = [('', '默认提示'), ('keyboard', '键帽提示'), ('xbox', 'Xbox 提示'), ('xbox-label', 'Xbox 字母提示')]


def steps(state):
    """The enabled steps, in run order: each one is (id, 中文名, 可执行文件, 参数, 说明)。"""
    out_dir = state['out'] or f"build/{state['version']}"
    exefs = state['exefs'] or f"build/extracted/{state['version']}/exefs"
    s = []
    if state['lift']:
        s.append(('lift', '重新生成 C（gen.py）', str(PY),
                  [str(ROOT / 'recomp' / 'gen.py'), '--exefs', exefs, '--out', f'generated/{state["version"]}',
                   '--patches', f'recomp/patches-{state["version"]}.txt'],
                  '只在改了 recomp/ 的生成器或补丁表之后需要；只改 runtime/ 不用重lift。'))
    for tag, label in PROMPT_SETS:
        key = 'prompts' + (tag or 'default')
        if not state.get(key):
            continue
        args = [str(ROOT / 'tools' / 'make_romfs_patches.py'), '--version', state['version']]
        if tag:
            args += ['--prompts', tag]
        s.append((key, f'RomFS 补丁 · {label}', str(PY314), args,
                  '生成 build/<version>/romfs-patches*；换了按键提示源码（button_icons.py）才需要重跑。'))
    build_args = [str(ROOT / 'tools' / 'build_game.py'), '--version', state['version'], '--out', out_dir]
    if state['exefs']:
        build_args += ['--exefs', state['exefs']]
    if state['romfs']:
        build_args += ['--romfs', state['romfs']]
    if state['extra']:
        build_args += state['extra'].split()
    s.append(('build', '编译并链接 totk.exe', str(PY), build_args,
              '新增 runtime/ 源码文件必须由 build_game.py 重写 build.ninja，只有 ninja 会漏文件。'))
    if state['shaders']:
        s.append(('shaders', '预编译着色器（prepare_shaders）', str(PY314),
                  [str(ROOT / 'tools' / 'prepare_shaders.py'), out_dir, '--version', state['version']],
                  '换了着色器翻译器或新构筑目录才需要；不跑则首次进游戏边玩边编，会卡到缓存填满。'))
    if state['tests']:
        s.append(('tests', '跑测试', str(PY),
                  [str(ROOT / 'tests' / 'kbm_scheme_test.py')], '键鼠方案的路由用例（-O0 与 -O1 各一遍）。'))
    if state['deploy']:
        s.append(('deploy', '部署到游戏目录', '', [], '先备份 totk.exe.prev，再拷 exe 与已选提示的 romfs-patches*。'))
    return s


def running_builds():
    """[(pid, 命令行)] of the build_game.py processes alive now. Two builds over one --out directory keep rewriting
    each other's objects and neither finishes, so the window refuses to start a third over them."""
    import subprocess
    q = ("Get-CimInstance Win32_Process -Filter \"Name='python.exe'\" | Where-Object { $_.CommandLine -match "
         "'build_game' } | ForEach-Object { $_.ProcessId.ToString() + '|' + $_.CommandLine }")
    try:
        r = subprocess.run(['powershell', '-NoProfile', '-Command', q], capture_output=True, text=True, timeout=25,
                           creationflags=0x08000000)
    except (OSError, subprocess.SubprocessError):
        return []
    out = []
    for line in r.stdout.splitlines():
        pid, _, cmd = line.partition('|')
        if cmd.strip() and 'build_gui.py' not in cmd:
            out.append((pid.strip(), cmd.strip()))
    return out


def out_of(cmd, version):
    """The --out a build_game.py command line names, or the default build/<version> it uses without one."""
    m = re.search(r'--out[= ]"?([^\s"]+)', cmd)
    return m.group(1) if m else f'build/{version}'


class Log(QPlainTextEdit):
    """The live output: soft-wrapped so nothing is cut off, colour by line kind, and a full copy kept for the filter."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setReadOnly(True)
        self.setLineWrapMode(QPlainTextEdit.WidgetWidth)
        f = QFontDatabase.systemFont(QFontDatabase.FixedFont)
        f.setPointSize(10)
        self.setFont(f)
        self.lines = []
        self.errors_only = False

    def append(self, text, kind='out'):
        self.lines.append((text, kind))
        if self.errors_only and kind == 'out':
            return
        at_bottom = self.verticalScrollBar().value() >= self.verticalScrollBar().maximum() - 4
        color = {'out': TEXT, 'cmd': WARN, 'run': DIM, 'err': BAD, 'warn': WARN, 'ok': GOOD}[kind]
        self.appendHtml(f'<span style="color:{color}">{html(text)}</span>')
        if at_bottom or kind in ('err', 'ok'):
            self.verticalScrollBar().setValue(self.verticalScrollBar().maximum())

    def redraw(self):
        self.clear()
        for text, kind in list(self.lines):
            color = {'out': TEXT, 'cmd': WARN, 'err': BAD, 'warn': WARN, 'ok': GOOD}[kind]
            self.appendHtml(f'<span style="color:{color}">{html(text)}</span>')
        self.verticalScrollBar().setValue(self.verticalScrollBar().maximum())

    def tail(self, n=40):
        return [t for t, k in self.lines[-n:] if k in ('err', 'warn')]


def html(text):
    return text.replace('&', '&amp;').replace('<', '&lt;').replace('>', '&gt;')


ERR = re.compile(r'\b(error|errors|failed|FAILED|traceback|ninja: build stopped|undefined reference)\b', re.I)
WARNISH = re.compile(r'\b(warning|warn)\b|跳过|skipped', re.I)


class Main(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle('TotK 构筑窗口')
        self.cfg = self.load_cfg()
        self.proc = None
        self.queue = []
        self.step_of = {}
        self.done_steps = 0
        self.total_steps = 0
        self.started_at = 0
        self.step_started_at = 0
        self.buffer = ''
        self.central = QWidget()
        self.setCentralWidget(self.central)
        root = QVBoxLayout(self.central)
        root.setContentsMargins(12, 12, 12, 12)
        root.addWidget(self.head())
        self.tabs = QTabWidget()
        self.tabs.addTab(self.run_tab(), '构筑（自己跑）')
        self.tabs.addTab(self.watch_tab(), '监视（接管已在跑的）')
        split = QSplitter(Qt.Vertical)
        split.addWidget(self.tabs)
        split.addWidget(self.log_box())
        split.setStretchFactor(0, 3)
        split.setStretchFactor(1, 2)
        split.setSizes([420, 300])
        root.addWidget(split, 1)
        root.addWidget(self.status())
        self.resize(1180, 760)
        self.prefill()
        QTimer(self, timeout=self.tick, interval=200).start()
        self.watch_timer = QTimer(self, timeout=self.scan, interval=1000)

    # ---- settings row

    def head(self):
        box = QFrame()
        box.setObjectName('card')
        g = QGridLayout(box)
        g.setContentsMargins(10, 8, 10, 8)
        self.version = QComboBox()
        self.version.addItems(['v143', 'v121', 'v110'])
        self.version.setCurrentText(self.cfg.get('version', 'v143'))
        self.out = QLineEdit(self.cfg.get('out', ''))
        self.out.setPlaceholderText('空＝build/<版本>，例如 build/v143-kbmmain')
        self.exefs = QLineEdit(self.cfg.get('exefs', ''))
        self.exefs.setPlaceholderText('空＝用 build_game.py 的默认（extracted/<版本>）')
        self.romfs = QLineEdit(self.cfg.get('romfs', ''))
        self.romfs.setPlaceholderText('同上')
        self.game = QLineEdit(self.cfg.get('game', ''))
        self.game.setPlaceholderText('部署目标：放着 totk.exe 的文件夹')
        self.extra = QLineEdit(self.cfg.get('extra', ''))
        self.extra.setPlaceholderText('附加给 build_game.py 的参数，如 --prompts-note 1')
        for r, (label, w) in enumerate([('版本', self.version), ('输出目录', self.out), ('ExeFS', self.exefs),
                                         ('RomFS', self.romfs), ('游戏目录', self.game), ('额外参数', self.extra)]):
            g.addWidget(QLabel(label), r, 0)
            g.addWidget(w, r, 1)
            if r in (2, 3, 4):
                g.addWidget(QPushButton('…', clicked=lambda _, k=r: self.pick(k)), r, 2)
        g.setColumnStretch(1, 1)
        return box

    def pick(self, row):
        start = [self.out, self.exefs, self.romfs, self.game][row - 2].text() or str(ROOT / 'build')
        d = QFileDialog.getExistingDirectory(self, '选目录', start)
        if d:
            [self.out, self.exefs, self.romfs, self.game][row - 2].setText(Path(d).as_posix())

    def prefill(self):
        """A build dir that exists says which ExeFS and RomFS its exe was baked with: read them back. A saved path
        that no longer exists is dropped first -- build_game.py stops on a missing --exefs."""
        for field in (self.exefs, self.romfs):
            text = field.text().strip()
            if text and not Path(text).is_dir():
                self.log.append(f'上次的 {text} 已经不在了，清空该字段。', 'warn')
                field.clear()
        if self.exefs.text() and self.romfs.text():
            return
        out = self.state()['out']
        mods = ROOT / out / 'modules.c'
        if not mods.is_file():
            return
        try:
            text = mods.read_text(encoding='utf-8', errors='replace')
        except OSError:
            return
        for key, field in (('rt_default_exefs', self.exefs), ('rt_default_romfs', self.romfs)):
            m = re.search(rf'const char {key}\[\] = "([^"]*)"', text)
            if m and not field.text():
                field.setText(m.group(1).replace('\\', '/'))

    # ---- the run tab

    def run_tab(self):
        w = QWidget()
        h = QHBoxLayout(w)
        left = QVBoxLayout()
        self.step_boxes = []
        for tag, label, hint in [
            ('lift', '重新生成 C（gen.py）', '只在改了 recomp/ 生成器或补丁表之后需要，耗时最长'),
            ('promptsdefault', 'RomFS 补丁 · 默认提示', '只改了 60fps 补丁源码时需要'),
            ('promptskeyboard', 'RomFS 补丁 · 键帽提示', '键帽键位图（button_icons.py）变了时需要'),
            ('promptsxbox', 'RomFS 补丁 · Xbox 提示', ''),
            ('promptsxbox-label', 'RomFS 补丁 · Xbox 字母提示', ''),
            ('build', '编译并链接 totk.exe', '改了 runtime/ 或加了新源文件必须走这条'),
            ('shaders', '预编译着色器（prepare_shaders）', '新构筑目录或换了翻译器才需要，否则首进游戏会卡'),
            ('tests', '跑测试', 'kbm_scheme_test：-O0 与 -O1 各一遍'),
            ('deploy', '部署到游戏目录', '会先备份 totk.exe.prev'),
        ]:
            box = QCheckBox(label)
            default = {'build': True, 'promptskeyboard': True, 'deploy': True}.get(tag, False)
            box.setChecked(bool(self.cfg.get(tag, default)))
            if hint:
                box.setToolTip(hint)
            box.setStyleSheet('QCheckBox::indicator { width: 16px; height: 16px; }')
            self.step_boxes.append((tag, box))
            left.addWidget(box)
        left.addStretch(1)
        holder = QWidget()
        holder.setLayout(left)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setWidget(holder)
        scroll.setMinimumWidth(320)
        h.addWidget(scroll, 2)
        right = QVBoxLayout()
        self.step_bar = QProgressBar()
        self.step_bar.setFormat('当前步骤 %p%')
        self.all_bar = QProgressBar()
        self.all_bar.setFormat('总体 %p%')
        right.addWidget(self.step_bar)
        right.addWidget(self.all_bar)
        self.clock = QLabel('未开始')
        self.clock.setObjectName('dim')
        right.addWidget(self.clock)
        btns = QHBoxLayout()
        self.go = QPushButton('开始构筑')
        self.go.setObjectName('go')
        self.go.clicked.connect(self.start)
        self.stop = QPushButton('停止')
        self.stop.setObjectName('stop')
        self.stop.setEnabled(False)
        self.stop.clicked.connect(self.abort)
        btns.addWidget(self.go)
        btns.addWidget(self.stop)
        right.addLayout(btns)
        right.addStretch(1)
        h.addLayout(right, 3)
        return w

    # ---- the watch tab

    def watch_tab(self):
        w = QWidget()
        v = QVBoxLayout(w)
        row = QHBoxLayout()
        self.watch_dir = QLineEdit(self.cfg.get('watch', ''))
        self.watch_dir.setPlaceholderText('已在跑的 --out 目录，如 build/v143-kbmmain')
        row.addWidget(self.watch_dir, 1)
        row.addWidget(QPushButton('浏览…', clicked=self.watch_browse))
        self.watch_go = QPushButton('开始监视')
        self.watch_go.clicked.connect(self.toggle_watch)
        row.addWidget(self.watch_go)
        v.addLayout(row)
        self.watch_bar = QProgressBar()
        self.watch_bar.setFormat('%p%')
        v.addWidget(self.watch_bar)
        self.watch_info = QLabel('填一个 build 下的输出目录，窗口就按它的 .o 计数推进度；不动 3 分钟会提示可能是卡住或已结束。')
        self.watch_info.setObjectName('dim')
        self.watch_info.setWordWrap(True)
        v.addWidget(self.watch_info)
        v.addStretch(1)
        self.watch_hist = deque(maxlen=120)
        return w

    def watch_browse(self):
        d = QFileDialog.getExistingDirectory(self, '选构建输出目录', str(ROOT / 'build'))
        if d:
            self.watch_dir.setText(Path(d).as_posix())

    def toggle_watch(self):
        if self.watch_timer.isActive():
            self.watch_timer.stop()
            self.watch_go.setText('开始监视')
            return
        self.watch_hist.clear()
        self.watch_timer.start()
        self.watch_go.setText('停止监视')
        self.scan()

    def scan(self):
        rel = self.watch_dir.text().strip() or self.state()['out']
        out = Path(rel) if Path(rel).is_absolute() else ROOT / rel
        ninja = out / 'build.ninja'
        if not ninja.is_file():
            self.watch_info.setText(f'{out.as_posix()} 里没有 build.ninja：这个目录还没生成过。')
            return
        total = sum(1 for line in ninja.read_text(encoding='utf-8', errors='replace').splitlines()
                    if line.startswith('build ') and line.split(':', 1)[0].endswith('.o'))
        done = len(list((out / 'obj').rglob('*.o'))) if (out / 'obj').is_dir() else 0
        exe = out / 'totk.exe'
        newest, newest_age = None, None
        if (out / 'obj').is_dir():
            files = [(f.stat().st_mtime, f) for f in (out / 'obj').rglob('*.o')]
            if files:
                mt, newest = max(files)
                newest_age = time.time() - mt
        now = time.time()
        self.watch_hist.append((now, done))
        rate = 0.0
        if len(self.watch_hist) >= 4:
            t0, d0 = self.watch_hist[0]
            rate = (done - d0) / max(1e-6, now - t0)
        self.watch_bar.setMaximum(max(1, total))
        self.watch_bar.setValue(done)
        left = (total - done) / rate if rate > 0 and total > done else None
        bits = [f'已编 {done}/{total} 个对象']
        if newest:
            bits.append(f'最近 {newest.name}（{newest_age:.0f} 秒前）')
        if done >= total and not exe.is_file():
            bits.append('对象已齐，正在链接')
        else:
            bits.append(f'速率 {rate:.1f}/秒' if rate else '速率 0/秒')
        if left is not None:
            bits.append(f'预计还剩 {left:.0f} 秒' if left < 3600 else f'预计还剩 {left / 60:.0f} 分钟')
        if exe.is_file():
            bits.append(f'totk.exe 已在（{exe.stat().st_size / 2**20:.1f} MiB）')
        if newest_age is not None and newest_age > 180 and not (exe.is_file() and done >= total):
            bits.append('3 分钟没有新对象：可能已结束，也可能卡住了')
        self.watch_info.setText(' · '.join(bits))

    # ---- the log

    def log_box(self):
        w = QWidget()
        v = QVBoxLayout(w)
        v.setContentsMargins(0, 0, 0, 0)
        bar = QHBoxLayout()
        bar.addWidget(QLabel('实时输出'))
        self.err_only = QCheckBox('只看错误')
        self.err_only.toggled.connect(self.filter_log)
        bar.addWidget(self.err_only)
        bar.addWidget(QPushButton('清空', clicked=self.clear_log))
        bar.addStretch(1)
        v.addLayout(bar)
        self.log = Log()
        v.addWidget(self.log, 1)
        return w

    def filter_log(self):
        self.log.errors_only = self.err_only.isChecked()
        self.log.redraw()

    def clear_log(self):
        self.log.lines.clear()
        self.log.clear()

    def status(self):
        self.status_bar = QLabel('就绪')
        self.status_bar.setObjectName('dim')
        return self.status_bar

    # ---- running

    def state(self):
        s = {
            'version': self.version.currentText(),
            'out': self.out.text().strip() or f'build/{self.version.currentText()}',
            'exefs': self.exefs.text().strip(),
            'romfs': self.romfs.text().strip(),
            'game': self.game.text().strip(),
            'extra': self.extra.text().strip(),
        }
        for tag, box in self.step_boxes:
            s[tag] = box.isChecked()
        s['tests'] = s.get('tests', False)
        s['deploy'] = s.get('deploy', False)
        return s

    def start(self):
        if self.proc:
            return
        self.cfg_update()
        st = self.state()
        mine = (ROOT / st['out']).resolve().as_posix().lower()
        for pid, cmd in running_builds():
            if (ROOT / out_of(cmd, st['version'])).resolve().as_posix().lower() == mine:
                self.log.append(f'已经有构建在跑同一个输出目录（PID {pid}）：{cmd[:110]}', 'err')
                self.log.append('同一个 --out 起两个进程会互相重编，两个都出不来 exe。等它跑完，或先停掉那个 PID。',
                                'warn')
                return
        self.queue = steps(st)
        if not self.queue:
            self.log.append('没勾任何步骤。', 'warn')
            return
        self.total_steps, self.done_steps = len(self.queue), 0
        self.started_at = time.time()
        self.log.append(f'共 {len(self.queue)} 步：' + ' → '.join(l for _, l, *_ in self.queue), 'run')
        self.go.setEnabled(False)
        self.stop.setEnabled(True)
        self.run_next()

    def run_next(self):
        if not self.queue:
            self.finish()
            return
        sid, label, exe, args, _ = self.queue.pop(0)
        if sid == 'deploy':
            self.deploy()
            self.done_steps += 1
            self.run_next()
            return
        self.step_started_at = time.time()
        self.step_of = {'id': sid, 'label': label}
        self.step_bar.setValue(0)
        self.log.append('$ ' + ' '.join([Path(exe).with_suffix('').name, *args]), 'cmd')
        self.proc = QProcess(self)
        self.proc.setProcessChannelMode(QProcess.MergedChannels)
        self.proc.setWorkingDirectory(str(ROOT))
        env = QProcessEnvironment.systemEnvironment()
        env.insert('PYTHONPATH', '')   # an injected sitecustomize (the hosted shell's audit hook) blocks the build
        self.proc.setProcessEnvironment(env)
        self.proc.readyReadStandardOutput.connect(self.on_output)
        self.proc.finished.connect(self.on_finished)
        self.proc.start(exe, args)

    def on_output(self, bytes_=None):
        self.buffer += self.proc.readAllStandardOutput().data().decode('utf-8', 'replace')
        while '\n' in self.buffer:
            line, self.buffer = self.buffer.split('\n', 1)
            self.emit_line(line)

    def emit_line(self, line):
        line = line.rstrip('\r')
        if not line.strip():
            return
        m = NINJA_EDGE.match(line)
        if m:
            self.step_bar.setMaximum(int(m.group(2)))
            self.step_bar.setValue(int(m.group(1)))
        kind = 'out'
        if ERR.search(line):
            kind = 'err'
        elif WARNISH.search(line):
            kind = 'warn'
        self.log.append(line, kind)

    def on_finished(self, code, status):
        self.on_output()
        secs = time.time() - self.step_started_at
        label = self.step_of.get('label', '步骤')
        if code == 0:
            self.log.append(f'✓ {label} 完成，{secs:.1f} 秒', 'ok')
            self.done_steps += 1
        else:
            self.log.append(f'✗ {label} 失败（退出码 {code}），{secs:.1f} 秒', 'err')
            self.queue.clear()
        self.proc.deleteLater()
        self.proc = None
        self.run_next()

    def abort(self):
        if self.proc:
            self.proc.kill()
            self.log.append('已请求停止。', 'warn')

    def finish(self):
        failed = any(k == 'err' for _, k in self.log.lines)
        self.log.append(f'结束：{"有失败步骤" if failed else "全部通过"}，共 {time.time() - self.started_at:.1f} 秒',
                        'err' if failed else 'ok')
        self.go.setEnabled(True)
        self.stop.setEnabled(False)
        self.all_bar.setValue(self.all_bar.maximum())

    # ---- deploy

    def deploy(self):
        import shutil
        st = self.state()
        game = Path(st['game'])
        out = Path(st['out']) if Path(st['out']).is_absolute() else ROOT / st['out']
        src = out / 'totk.exe'
        if not st['game'] or not game.is_dir():
            self.log.append('部署：游戏目录没填或不存在，跳过。', 'warn')
            return
        if not src.is_file():
            self.log.append(f'部署：{src.as_posix()} 不存在，跳过。', 'err')
            return
        dst = game / 'totk.exe'
        try:
            with open(dst, 'r+b'):
                pass
        except OSError:
            self.log.append('部署：totk.exe 正被占用（游戏在跑），先退出游戏。', 'err')
            return
        if dst.is_file():
            shutil.copy2(dst, game / 'totk.exe.prev')
            self.log.append(f'部署：旧 exe 备份到 {game / "totk.exe.prev"}', 'run')
        shutil.copy2(src, dst)
        self.log.append(f'部署：{src.name} → {dst.as_posix()}（{dst.stat().st_size / 2**20:.1f} MiB）', 'ok')
        for d in sorted(out.glob('romfs-patches*')):
            if not (d / 'manifest.tsv').is_file():
                continue
            shutil.copytree(d, game / d.name, dirs_exist_ok=True)
            self.log.append(f'部署：{d.name} → {game.as_posix()}', 'ok')

    # ---- settings persistence

    def cfg_update(self):
        st = self.state()
        st['watch'] = self.watch_dir.text()
        for k in ('promptsdefault', 'promptskeyboard', 'promptsxbox', 'promptsxbox-label'):
            st[k] = st.get(k, False)
        try:
            CONFIG.parent.mkdir(exist_ok=True)
            CONFIG.write_text(json.dumps(st, ensure_ascii=False, indent=1), encoding='utf-8')
        except OSError as e:
            self.log.append(f'设置存不下：{e}', 'warn')

    def load_cfg(self):
        try:
            return json.loads(CONFIG.read_text(encoding='utf-8'))
        except (OSError, ValueError):
            return {}

    def tick(self):
        if self.total_steps:
            done, total = self.done_steps, self.total_steps
            cur = self.step_bar.value() / max(1, self.step_bar.maximum()) if self.proc else 0.0
            self.all_bar.setMaximum(total * 100)   # whole percents would sit at 0 for the whole first step
            self.all_bar.setValue(done * 100 + int(cur * 100))
            self.status_bar.setText(f'{self.step_of.get("label", "")} · 第 {min(done + 1, total)}/{total} 步'
                                    if self.proc else f'{done}/{total} 步已完成')
        else:
            self.all_bar.setMaximum(100)
        if self.started_at and self.proc:
            self.clock.setText(f'本步 {time.time() - self.step_started_at:.1f} 秒 · 累计 {time.time() - self.started_at:.1f} 秒')


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--shot', type=Path, help='draw the window, save its PNG here and exit')
    ap.add_argument('--watch', help='start on the watch tab, watching this build output dir')
    ap.add_argument('--run', action='store_true', help='start the checked steps as soon as the window opens')
    a = ap.parse_args()
    app = QApplication([])
    apply_theme(app)
    win = Main()
    win.show()
    if a.run:
        win.start()
    if a.watch:
        win.watch_dir.setText(a.watch)
        win.tabs.setCurrentIndex(1)
        win.toggle_watch()
    if a.shot:
        def grab():
            if not a.watch:   # with --watch the real scan numbers are already on screen
                win.log.append('$ python tools/build_game.py --version v143 --out build/v143-kbmmain', 'cmd')
                win.log.append('[312/536] CC runtime/input.c', 'out')
                win.log.append('FAILED: obj/runtime/foo.o', 'err')
                win.log.append('✓ 编译并链接 totk.exe 完成，84.2 秒', 'ok')
                win.step_bar.setMaximum(536)
                win.step_bar.setValue(312)
                win.all_bar.setMaximum(3)
                win.all_bar.setValue(1)
                win.watch_bar.setMaximum(536)
                win.watch_bar.setValue(312)
                win.watch_info.setText('已编 312/536 个对象 · 最近 main_0207.o（2 秒前） · 速率 1.8/秒 · 预计还剩 124 秒')
            win.grab().save(str(a.shot))
            win.tabs.setCurrentIndex(1)
            QApplication.processEvents()
            win.grab().save(str(a.shot.with_name(a.shot.stem + '-watch' + a.shot.suffix)))
            app.quit()
        QTimer.singleShot(900, grab)
    sys.exit(app.exec())


if __name__ == '__main__':
    main()
