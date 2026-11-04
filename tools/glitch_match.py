"""Find the presented frames that revert: a frame that differs from both its neighbours while they match each other.

The detector is the offline half of the one the runtime cannot afford: `TOTK_SHOTS=lo-hi:step` already writes every
step-th present to `<dir>/f<n>.png` (runtime/boot.c), so the judgement can be made after the run, on the files, with no
Vulkan, no read-back ring and no frame cost. It answers the question a static recompilation is worst at: did that patch
put a stale or half-drawn frame on screen, which no one spots by eye in a 240 fps run and which the site coverage
oracle cannot see at all.

The measure is the one weeping-of-the-empire settled on (engine/runtime/nvn/vulkan/glitch_match.h, MIT, Copyright (c)
2026 daniel-madrid-07 at f2c81da): each frame reduced to 160x90 luminance, compared as a 16x9 grid of cell means, and
frame s judged an anomaly when it differs from BOTH neighbours by at least --min while the neighbours match each other
to within a quarter of that. A camera turn makes the neighbours differ as much as the frame does, so when that test
fails the three are compared again at their best whole-pixel shift (+-8 x +-3 read-back pixels a frame, +-16 x +-6
across two), which is what recovers a pane turning see-through inside a 40 px/frame pan. A cut or a menu change stays
on screen (its next frame differs from the one before too) and a fade moves a little each frame, so neither is flagged.

Beyond the verdict the age of a flagged frame is worked out: which older frame it matches best, over the whole grid and
over the centre cells only, and whether its edges are old too. A centre that matches an old frame while the edges match
the current one is an old scene under a current HUD; both old is a whole stale frame.

    python tools/glitch_match.py build/shots --out build/glitch.json
    python tools/glitch_match.py build/shots --min 0.015 --save-dir build/glitch-trios
    python tools/glitch_match.py build/shots --fail-on-anomaly      # in CI, after a scripted run

Frames are taken in the order of the number at the end of their name (f12.png, f13.png, ...), and the run is assumed to
be a contiguous sequence: write the shots with step 1 for a regression run, since a stride makes every pair differ by
more than the threshold. Images are squashed to 160x90 whatever their shape, exactly as the runtime's blit does,
because both sides of a comparison have to be reduced the same way.

Exit is 0 when the tool ran and 1 only with --fail-on-anomaly, so a normal run reports rather than fails: an anomaly is
a frame to look at, not a broken build.

Adapted from weeping-of-the-empire glitch_match.h (MIT). The shift search and the quarter rule are theirs; reading a
PNG sequence instead of a Vulkan read-back ring, the age report and the JSON output are this file's.
"""

import argparse
import json
import re
import shutil
import sys
from collections import deque
from pathlib import Path

import numpy as np
from PIL import Image

GM_W, GM_H = 160, 90          # the read-back image, luminance (glitch_match.h GM_W / GM_H)
GM_CW, GM_CH = 16, 9          # the grid it is compared as (GM_CW / GM_CH)
CW, CH = GM_W // GM_CW, GM_H // GM_CH
DEFAULT_MIN = 0.02            # TOTK_GLITCH_MIN's default: mean absolute luminance difference over the grid, 0..1
DEFAULT_AGE_DEPTH = 6         # the runtime keeps 8 frames, so N-2 .. N-6 are still there to compare against
IDENTITY = 1.0                # what a comparison returns when too few cells overlap to mean anything


def frame_number(path):
    """The number the file is ordered by: the last run of digits in its name, else its name."""
    nums = re.findall(r'\d+', path.stem)
    return (int(nums[-1]), path.name) if nums else (1 << 62, path.name)


def luminance(rgb):
    """Rec. 709 luma of an 0..255 RGB image, in 0..1, left gamma-encoded (the threshold is calibrated on encoded
    frames: dark-ground differences shrink a lot once linearised, and the detector would stop seeing them)."""
    return (0.2126 * rgb[:, :, 0] + 0.7152 * rgb[:, :, 1] + 0.0722 * rgb[:, :, 2]) / 255.0


def luma(path):
    """The frame as 160x90 luminance in 0..1 (the read-back image the runtime compares)."""
    with Image.open(path) as im:
        small = im.convert('RGB').resize((GM_W, GM_H), Image.BOX)
    return luminance(np.asarray(small, dtype=np.float64))


def integral(lum):
    """The summed-area table: (GM_H + 1, GM_W + 1), [y, x] the sum of lum[0..y) x [0..x) (glitch_match.h gm_sum)."""
    return np.pad(np.cumsum(np.cumsum(lum, axis=0), axis=1), ((1, 0), (1, 0)))


def _windows(sat, dx, dy):
    """(sums, mask) of the 16x9 cells taken at (x + dx, y + dy), mask false where the window leaves the frame."""
    x0 = np.arange(GM_CW) * CW + dx
    y0 = np.arange(GM_CH) * CH + dy
    okx = (x0 >= 0) & (x0 + CW <= GM_W)
    oky = (y0 >= 0) & (y0 + CH <= GM_H)
    xc = np.clip(x0, 0, GM_W - CW)[None, :]
    yc = np.clip(y0, 0, GM_H - CH)[:, None]
    sums = sat[yc + CH, xc + CW] - sat[yc, xc + CW] - sat[yc + CH, xc] + sat[yc, xc]
    return sums, oky[:, None] & okx[None, :]


def dist(a, b, dx=0, dy=0):
    """Mean absolute difference of the cell means of a against b moved by (dx, dy) pixels; only cells whose window
    lies inside b count, and fewer than half of them is no match at all (glitch_match.h gm_dist)."""
    sa, _ = _windows(a, 0, 0)
    sb, ok = _windows(b, dx, dy)
    n = int(ok.sum())
    if n * 2 < GM_CW * GM_CH:
        return IDENTITY
    return float(np.abs(sa - sb)[ok].sum() / (n * CW * CH))


def dist_part(a, b, centre):
    """The centre cells only (the HUD sits on the frame's edges), or the edges only, without a shift (gm_dist_part)."""
    sa, _ = _windows(a, 0, 0)
    sb, _ = _windows(b, 0, 0)
    ok = np.zeros((GM_CH, GM_CW), dtype=bool)
    ok[2:GM_CH - 2, 2:GM_CW - 2] = True
    if not centre:
        ok = ~ok
    n = int(ok.sum())
    if not n:
        return IDENTITY
    return float(np.abs(sa - sb)[ok].sum() / (n * CW * CH))


def best(a, b, rx, ry):
    """The smallest dist over every shift within +-rx, +-ry (glitch_match.h gm_best)."""
    return min(dist(a, b, dx, dy) for dy in range(-ry, ry + 1) for dx in range(-rx, rx + 1))


def judge(pv, s, nx, minimum):
    """The verdict on s between pv and nx: None, or (score, dp, dn, dnb, moved) (glitch_match.h gm_judge).

    A frame that reverts differs from both neighbours while they match each other. When they do not match, the camera
    may have turned: the three are compared again each at its best shift, and the frame is kept only if that explains
    the neighbours and not it. score is how much more s differs than the neighbours do from each other.
    """
    dp, dn, dnb = dist(s, pv), dist(s, nx), dist(pv, nx)
    dmin = min(dp, dn)
    if dmin < minimum:
        return None
    moved = False
    if dnb * 4 > dmin:
        dp, dn = best(s, pv, 8, 3), best(s, nx, 8, 3)
        dnb = best(pv, nx, 16, 6)
        dmin = min(dp, dn)
        if dmin < minimum or dnb * 4 > dmin:
            return None
        moved = True
    return dmin / max(dnb, 0.002), dp, dn, dnb, moved


def age(s, pv, history, depth):
    """What a flagged frame's picture is: which older frame it matches best, and whether its edges are old too.

    history is [(number, name, sat)] newest first starting at N-2. A centre that matches an old frame while the edges
    match N-1 is an old scene under a current HUD; both old is a whole stale frame. The hud column is advice, not a
    verdict: the edge cells hold scene as well as HUD, so a frame whose picture is old reads 'old too' whenever that
    scene outweighs the HUD in them -- the two distances are reported beside it so the call can be made by eye.
    """
    whole, centre = {'k': 0, 'd': IDENTITY}, {'k': 0, 'd': IDENTITY}
    for k, (_num, _name, sat) in enumerate(history, start=2):
        if k > depth:
            break
        d = dist(s, sat)
        if d < whole['d']:
            whole = {'k': k, 'd': d}
        m = dist_part(s, sat, True)
        if m < centre['d']:
            centre = {'k': k, 'd': m}
    if not history:
        return None
    centre_prev = dist_part(s, pv, True)
    edge_prev = dist_part(s, pv, False)
    old = next((sat for k, (_n, _nm, sat) in enumerate(history, start=2) if k == centre['k']), None)
    edge_old = dist_part(s, old, False) if old is not None and centre['k'] else IDENTITY
    return {
        'whole': {'k': whole['k'], 'd': round(whole['d'], 4)},
        'centre': {'k': centre['k'], 'd': round(centre['d'], 4)},
        'centre_prev': round(centre_prev, 4),
        'edge_prev': round(edge_prev, 4),
        'edge_old': round(edge_old, 4),
        'hud': 'current' if edge_prev <= edge_old else 'old too',
    }


def scan(paths, minimum, depth, on_frame=None):
    """Judge every frame that has both neighbours, keeping only the frames the verdict and the age report need."""
    window = deque(maxlen=depth + 3)      # (number, path, sat), oldest first; holds N-2 .. N-depth
    anomalies = []
    for i, path in enumerate(paths):
        sat = integral(luma(path))
        window.append((frame_number(path)[0], path, sat))
        if on_frame:
            on_frame(i + 1, len(paths))
        if len(window) < 3:
            continue
        pv, s, nx = window[-3], window[-2], window[-1]
        verdict = judge(pv[2], s[2], nx[2], minimum)
        if verdict is None:
            continue
        score, dp, dn, dnb, moved = verdict
        history = [(n, p, t) for n, p, t in list(window)[:-3]][::-1]   # N-2, N-3, ..., N-depth
        anomalies.append({
            'frame': s[0],
            'file': s[1].name,
            'score': round(score, 2),
            'dp': round(dp, 4), 'dn': round(dn, 4), 'dnb': round(dnb, 4),
            'moved': moved,
            'neighbours': [pv[1].name, nx[1].name],
            'age': age(s[2], pv[2], history, depth),
        })
    return anomalies


def describe(a):
    """One flagged frame as one or two lines: the verdict, then what the frame's age says."""
    head = (f'frame {a["file"]} (n {a["frame"]}) score {a["score"]:.1f}: differs {a["dp"]:.3f} / {a["dn"]:.3f} '
            f'from its neighbours, which differ {a["dnb"]:.3f}')
    if a['moved']:
        head += ' (compared after the camera\'s move)'
    out = [head]
    ag = a['age']
    if ag and ag['whole']['k']:
        out.append(f'    age: whole closest to N-{ag["whole"]["k"]} ({ag["whole"]["d"]:.3f}), '
                   f'centre to N-{ag["centre"]["k"]} ({ag["centre"]["d"]:.3f}, vs N-1 {ag["centre_prev"]:.3f}); '
                   f'edges vs N-1 {ag["edge_prev"]:.3f}, vs N-{ag["centre"]["k"]} {ag["edge_old"]:.3f} '
                   f'({"HUD current" if ag["hud"] == "current" else "HUD old too"})')
    return out


def main(argv=None):
    ap = argparse.ArgumentParser(description='Find the presented frames that revert, from a TOTK_SHOTS sequence.')
    ap.add_argument('dir', type=Path, help='the folder TOTK_SHOTS wrote its f<n>.png into')
    ap.add_argument('--pattern', default='*.png', help='which files are frames (default: *.png)')
    ap.add_argument('--min', type=float, default=DEFAULT_MIN,
                    help=f'mean absolute difference a frame must reach on both sides (default {DEFAULT_MIN})')
    ap.add_argument('--age-depth', type=int, default=DEFAULT_AGE_DEPTH,
                    help=f'how far back the age report looks (default {DEFAULT_AGE_DEPTH}, the runtime keeps 8)')
    ap.add_argument('--out', type=Path, help='write the report as JSON here')
    ap.add_argument('--save-dir', type=Path, help='copy each flagged frame with both neighbours here')
    ap.add_argument('--limit', type=int, help='stop after this many flagged frames')
    ap.add_argument('--fail-on-anomaly', action='store_true', help='exit 1 when a frame is flagged (for CI)')
    ap.add_argument('--quiet', action='store_true', help='say nothing but the count')
    args = ap.parse_args(argv)

    paths = sorted(args.dir.glob(args.pattern), key=frame_number)
    if len(paths) < 3:
        ap.error(f'{args.dir} holds {len(paths)} frame(s) matching {args.pattern}; three are needed')

    def progress(done, total):
        if not args.quiet and sys.stderr.isatty():
            sys.stderr.write(f'\r{done}/{total} frames')
            sys.stderr.flush()

    anomalies = scan(paths, args.min, args.age_depth, progress)
    if not args.quiet and sys.stderr.isatty():
        sys.stderr.write('\r' + ' ' * 24 + '\r')

    if args.limit:
        anomalies = anomalies[:args.limit]

    if not args.quiet:
        print(f'{len(paths)} frames, threshold {args.min:.3f}: {len(anomalies)} flagged')
        for a in anomalies:
            for line in describe(a):
                print(line)

    if args.save_dir and anomalies:
        args.save_dir.mkdir(parents=True, exist_ok=True)
        by_name = {p.name: p for p in paths}
        for a in anomalies:
            for role, name in zip(('prev', 'frame', 'next'), (a['neighbours'][0], a['file'], a['neighbours'][1])):
                src = by_name.get(name)
                if src:
                    shutil.copy2(src, args.save_dir / f'glitch_{a["frame"]:06d}_{role}{src.suffix}')

    if args.out:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        doc = {
            'threshold': args.min,
            'frames': len(paths),
            'first': paths[0].name,
            'last': paths[-1].name,
            'anomalies': anomalies,
        }
        args.out.write_text(json.dumps(doc, indent=2) + '\n', encoding='utf-8')

    return 1 if (args.fail_on_anomaly and anomalies) else 0


if __name__ == '__main__':
    sys.exit(main())
