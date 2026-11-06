## What this fixes

Hero's Path replay on the map runs at a speed that follows the frame rate: at 60 fps the walk is 2x, at 144 fps 4.8x, at 240 fps 8x the speed it has on the Switch, and the crossed-points walk was reported to blow past points at high frame rates (#330's family).

The cause is a per-frame step that never meets the frame time. The map's footprint player (`main+0x2159640`, called once per frame from `UIMapScreen`'s update) advances its float index `+0xc40` by a step read from the 3 x 4 table at `main+0x2d1711c` — row = the replay speed (`+0xc44` if under 4, else 0), column = the zoom level (`+0x2b4` if under 4, else 0):

| site | what it does |
|---|---|
| `main+0x2159e2c` `ldr s0, [x10]` | loads the step |
| `main+0x2159ef4` `fadd s9, s9, s0` | index += step, forward |
| `main+0x2159e34` `fsub` on the same pair | index -= step, backward |

One step per frame, no dt. The Switch's 30 fps is baked into the table.

The stick scroll is **not** affected on 1.4.3: its target step multiplies the time scale, which moved from `+0x8` (1.0.0) to `+0x10` on 1.4.3 — Nintendo fixed that half and left the replay. So this PR is deliberately narrow: one site, the replay only.

## The patch

```asm
ldr  s0, [x10]        ; the step from the table (original)
ldr  s1, [x10, #0]    ; s1 = the page's 30 Hz ratio (realtime step)
fmov s2, #1.0
fminnm s1, s1, s2     ; k = min(ratio, 1.0)
fmul s0, s0, s1       ; step *= k
```

Floored at 1.0 on purpose: a frame slower than 30 Hz keeps the Switch's step, because a larger one could carry the crossed-points walk past a point it should stop at.

Register discipline (checked across the whole function, not just the site): `s1` holds nothing past the site — the `scvtf` a few instructions on writes it on both paths; `s2` is dead until `main+0x215a054`; `x10` is the table entry's address, free once the load has read it.

`set heropath TOTK_HEROPATH_REALTIME 1`, and like `cloth` the set is switched off when the real-time step is off (`frame_rate.c`), since the page's ratio is only maintained then. The toggle is in the frame-rate menu, four languages. On versions whose patch table has no `heropath` set the whole thing is inert (`rt_patch_set` returns -1).

## Verification

- `patches.check` against the 1.4.3 NSO: the site's original word is `bd400140`, as in the table.
- `recomp/gen.py` over 1.4.3 with this table: 0 unsupported; the emitted site is `if (rt_patch_on[12]) { … FOP_MINNM(1,1,2); FOP_MUL(0,0,1); }` with the untouched `ldr` in the else branch, and both uses of `s0` (forward `+ step`, backward `- step`) sit after the site.
- `clang -fsyntax-only` on `frame_rate.c`, `cfg.c`, `ui_text.c`, `ui/menu.cpp`.
- **Not** verified in game: I have no way to run the map here. The numbers in the table come from the per-frame step and the frame step being independent, not from a capture; a capture of the replay at 30/60/144 fps would settle it, and I would welcome one before this merges.

Scoped to 1.4.3 because that is the version I could read. The same function and table exist on the other versions under their own addresses; a `site` line per version's table would make it work there too.

Refs #330 (the replay is one of the mechanics that outrun their timing above 30/60 fps). #35, #44 and #604 are different code paths — climbing, Ultrahand's vertical speed and water flow all move through their own step code, not this table.
