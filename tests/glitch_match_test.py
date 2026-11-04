"""tools/glitch_match.py on synthetic sequences whose answer is known: one corrupted frame in a still scene, a pane
dropped inside a pan, a camera cut and a fade that must NOT be flagged, a stale frame and its age, plus the shifted
comparison and the command line. numpy and PIL, both already in .venv.

Run: python tests/glitch_match_test.py
"""
import contextlib
import io
import json
import sys
import tempfile
import unittest
from pathlib import Path

import numpy as np
from PIL import Image

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / 'tools'))
import glitch_match as gm  # noqa: E402

W, H = 320, 180          # the sequence's own size; the tool squashes it to 160x90 like the runtime's blit
WIDE = 1000              # the scene the camera moves over, so a pan never wraps
HUD = 6                  # rows/columns of a static HUD the pan leaves alone


def scene(seed, width=W):
    """A frame with structure at the grid's own scale (10 px cells at 160 wide) and fine texture over it, so a pan
    shows in the cell means and an exact shift still cancels it out."""
    rng = np.random.default_rng(seed)
    y, x = np.mgrid[0:H, 0:width]
    img = np.full((H, width, 3), 0.45)
    for _ in range(18):
        cx, cy, r = rng.uniform(0, width), rng.uniform(0, H), rng.uniform(12, 34)
        band = rng.uniform(-0.45, 0.45) * np.exp(-((x - cx) ** 2 + (y - cy) ** 2) / (2 * r * r))
        img += band[:, :, None]
    img += rng.normal(0, 0.05, (H, width, 3))
    return np.clip(img, 0.0, 1.0)


def view(wide, px, hud=0.85):
    """What the camera sees at px, with a static HUD drawn over it (its border does not pan).

    hud is the border's brightness: a sequence varies it a little every frame, the way a counter or a heart does, so a
    stale picture can be told apart from a stale HUD (the age report's last column).
    """
    out = wide[:, px:px + W].copy()
    out[:HUD, :] = hud
    out[-HUD:, :] = hud
    out[:, :HUD] = hud
    out[:, -HUD:] = hud
    return out


def pan(seed, step, count):
    """count frames of a camera scrolling step pixels a frame, the HUD moving on a shade each frame."""
    wide = scene(seed, WIDE)
    return [view(wide, step * i, hud=0.85 - 0.01 * i) for i in range(count)]


def write(dirpath, frames):
    dirpath.mkdir(parents=True, exist_ok=True)
    for i, img in enumerate(frames):
        Image.fromarray((img * 255).astype(np.uint8)).save(dirpath / f'f{i:03d}.png')


def run(dirpath, **kw):
    """glitch_match.main with --quiet, returning (exit code, report)."""
    out = Path(dirpath) / 'report.json'
    argv = [str(dirpath), '--quiet', '--out', str(out)]
    for k, v in kw.items():
        flag = '--' + k.replace('_', '-')
        argv += [flag] if v is True else [flag, str(v)]
    code = gm.main(argv)
    return code, (json.loads(out.read_text(encoding='utf-8')) if out.exists() else None)


def luma_of(img):
    """The same reduction the tool does to a file: an 8-bit squash to 160x90, then Rec. 709 luma."""
    im = Image.fromarray((img * 255).astype(np.uint8)).resize((gm.GM_W, gm.GM_H), Image.BOX)
    return gm.luminance(np.asarray(im, dtype=np.float64))


class Distances(unittest.TestCase):
    def setUp(self):
        self.a = scene(1)

    def test_a_frame_against_itself_is_zero(self):
        sat = gm.integral(luma_of(self.a))
        self.assertEqual(gm.dist(sat, sat), 0.0)
        self.assertEqual(gm.dist_part(sat, sat, True), 0.0)
        self.assertEqual(gm.dist_part(sat, sat, False), 0.0)

    def test_the_measure_is_the_mean_of_the_cell_means(self):
        """dist is the mean of |mean(cell a) - mean(cell b)|, which cells summed by hand must agree with."""
        b = scene(2)
        la, lb = luma_of(self.a), luma_of(b)

        def cell(p, cy, cx):
            return p[cy * 10:(cy + 1) * 10, cx * 10:(cx + 1) * 10].mean()

        want = np.mean([abs(cell(la, cy, cx) - cell(lb, cy, cx)) for cy in range(9) for cx in range(16)])
        self.assertAlmostEqual(gm.dist(gm.integral(la), gm.integral(lb)), float(want), places=9)

    def test_a_shift_is_undone_by_the_same_shift(self):
        """Comparing against the frame moved by dx finds them equal: that is what rescues a pan (gm_dist's dx, dy)."""
        wide = scene(3, WIDE)
        a, b = view(wide, 0), view(wide, 12)      # 12 source pixels = 6 at the read-back's 160 wide
        sat_a, sat_b = gm.integral(luma_of(a)), gm.integral(luma_of(b))
        self.assertLess(gm.dist(sat_a, sat_b, -6, 0), 0.01)
        self.assertGreater(gm.dist(sat_a, sat_b, 0, 0), 0.015)

    def test_best_never_exceeds_the_shiftless_distance(self):
        wide = scene(4, WIDE)
        sat_a, sat_b = gm.integral(luma_of(view(wide, 0))), gm.integral(luma_of(view(wide, 3)))
        self.assertLessEqual(gm.best(sat_a, sat_b, 8, 3), gm.dist(sat_a, sat_b))

    def test_centre_and_edges_are_both_sides_of_the_whole(self):
        a, b = scene(5), scene(6)
        sat_a, sat_b = gm.integral(luma_of(a)), gm.integral(luma_of(b))
        whole = gm.dist(sat_a, sat_b)
        self.assertLess(min(gm.dist_part(sat_a, sat_b, True), gm.dist_part(sat_a, sat_b, False)), whole)
        self.assertGreater(max(gm.dist_part(sat_a, sat_b, True), gm.dist_part(sat_a, sat_b, False)), whole)


class Verdicts(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def flagged(self, frames, **kw):
        write(self.dir, frames)
        code, report = run(self.dir, **kw)
        self.assertEqual(code, 0)
        self.assertEqual(report['frames'], len(frames))
        return report

    def test_a_frame_replaced_in_a_still_scene(self):
        """0..9 still, 10 a different scene, 11..19 still again: only 10 reverts."""
        still = scene(7)
        report = self.flagged([still] * 10 + [scene(99)] + [still] * 9)
        self.assertEqual([a['frame'] for a in report['anomalies']], [10])
        self.assertFalse(report['anomalies'][0]['moved'], 'a still scene needs no shift')

    def test_a_pan_alone_is_not_an_anomaly(self):
        """Every frame is 6 read-back pixels on: the neighbours differ as much as the frame does."""
        report = self.flagged(pan(8, 12, 30))
        self.assertEqual(report['anomalies'], [])

    def test_a_dropped_pane_inside_a_pan(self):
        """The pan's neighbours only match after the camera's move; a frame with its picture gone is still flagged."""
        frames = pan(8, 12, 30)
        frames[16] = np.full((H, W, 3), 0.5)
        report = self.flagged(frames)
        self.assertEqual([a['frame'] for a in report['anomalies']], [16])
        self.assertTrue(report['anomalies'][0]['moved'], 'the pan had to be compensated for')

    def test_a_camera_cut_is_not_an_anomaly(self):
        """A new scene stays: its next frame differs from the one before it too."""
        report = self.flagged([scene(10)] * 10 + [scene(11)] * 10)
        self.assertEqual(report['anomalies'], [])

    def test_a_fade_is_not_an_anomaly(self):
        """Brightness that moves a little every frame is a fade, not a reverted frame."""
        base = scene(12)
        report = self.flagged([np.clip(base * (0.55 + 0.02 * i), 0, 1) for i in range(25)])
        self.assertEqual(report['anomalies'], [])

    def test_a_stale_frame_is_flagged_and_its_age_named(self):
        """Frame 20 is frame 14 over again, HUD included: flagged, and the age report says N-6, HUD stale."""
        frames = pan(13, 12, 40)
        frames[20] = frames[14]
        report = self.flagged(frames)
        self.assertEqual([a['frame'] for a in report['anomalies']], [20])
        age = report['anomalies'][0]['age']
        self.assertEqual(age['whole']['k'], 6, f"expected the frame's own age, got {age}")
        self.assertEqual(age['centre']['k'], 6)
        self.assertLess(age['whole']['d'], 0.01)
        self.assertEqual(age['hud'], 'old too', 'the whole frame is old, HUD and picture alike')
        self.assertIn('edge_prev', age)

    def test_the_threshold_moves_the_verdict(self):
        """The same sequence is quiet when the bar is raised past the difference the frame shows."""
        still = scene(14)
        frames = [still] * 5 + [scene(15)] + [still] * 5
        self.assertEqual([a['frame'] for a in self.flagged(frames)['anomalies']], [5])
        self.assertEqual(self.flagged(frames, min=0.95)['anomalies'], [])


class CommandLine(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        still = scene(16)
        write(self.dir, [still] * 4 + [scene(17)] + [still] * 4)

    def tearDown(self):
        self.tmp.cleanup()

    def test_report_and_exit_code(self):
        out = self.dir / 'r.json'
        self.assertEqual(gm.main([str(self.dir), '--quiet', '--out', str(out)]), 0)
        doc = json.loads(out.read_text(encoding='utf-8'))
        self.assertEqual(doc['threshold'], gm.DEFAULT_MIN)
        self.assertEqual(doc['first'], 'f000.png')
        self.assertEqual(doc['last'], 'f008.png')
        a = doc['anomalies'][0]
        for key in ('frame', 'file', 'score', 'dp', 'dn', 'dnb', 'moved', 'neighbours', 'age'):
            self.assertIn(key, a)
        self.assertEqual(a['frame'], 4)
        self.assertEqual(a['neighbours'], ['f003.png', 'f005.png'])

    def test_the_human_report_names_the_frame_and_its_age(self):
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            gm.main([str(self.dir)])
        text = buf.getvalue()
        self.assertIn('9 frames', text)
        self.assertIn('1 flagged', text)
        self.assertIn('f004.png', text)
        self.assertIn('score', text)

    def test_fail_on_anomaly(self):
        self.assertEqual(gm.main([str(self.dir), '--quiet', '--fail-on-anomaly']), 1)

    def test_every_flagged_frame_is_saved_with_both_neighbours(self):
        saved = self.dir / 'trios'
        self.assertEqual(gm.main([str(self.dir), '--quiet', '--save-dir', str(saved)]), 0)
        self.assertEqual(sorted(p.name for p in saved.iterdir()),
                         ['glitch_000004_frame.png', 'glitch_000004_next.png', 'glitch_000004_prev.png'])

    def test_two_frames_are_not_enough(self):
        """A sequence needs a neighbour on both sides: fewer than three frames is a usage error."""
        small = self.dir / 'small'
        write(small, [scene(18)] * 2)
        with self.assertRaises(SystemExit):
            gm.main([str(small), '--quiet'])


if __name__ == '__main__':
    unittest.main(verbosity=2)
