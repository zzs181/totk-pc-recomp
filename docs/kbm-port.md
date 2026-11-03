# The PC keyboard and mouse scheme

> This patch applies on top of commit `7f38eb6` (PC controls and F1 rows). It does not include the game-screen
> watcher (`screen_watch.c`) or the mouse gyro; contexts are `WORLD` and the host settings menu's `PAUSE`. The keyboard
> keycap prompt set is included (`prompts = keyboard`).


A port of [Weeping of the Empire](https://github.com/daniel-madrid-07/weeping-of-the-empire)'s controls (MIT, v0.1.8.2):
one table of (context, action) rows decides what the keyboard and the mouse ask the game for, instead of the flat
key-to-button table in `docs/controls.md`. It is a second path, off by default, so both can be played and compared.

This branch is 0.3.1. Its input layer differs from the branch the scheme was first ported to (0.3.4) in three places
that the port had to meet; they are listed under "What this branch changes about the port" below.

## Turning it on

`TOTK_KBM_SCHEME=1` in `totk.ini` next to `totk.exe`, or in the environment (the environment wins). Live: the next
input poll switches, and the log says which path ran (`input: PC scheme on: 255 rows, 0 unknown keys, Esc is the
game's menu key`).

With it on, the Controls tab and the `TOTK_BIND_*` lines do nothing: they edit `input_map.c`'s table, which the
scheme does not read. `docs/controls.md` describes the other path. Everything else (the gamepad, its layout, rumble,
the six-axis, the mouse camera, the walk scale of the flat table) is untouched.

## What runs today

- The whole upstream table: 255 rows (`PC_ROW` 217 + `PC_VROW` 38) over 28 of the 34 scopes (`runtime/kbm_scheme_ui.inc`,
  `runtime/kbm_scheme_gameplay.inc`, unchanged but for a `K` prefix on the pad bits).
- Row resolution: a context inherits its group's rows (`WORLD` <- `GAMEPLAY` <- `ROOT`, `PAUSE` <- `MENUS` <- `UI` <-
  `ROOT`) and its own rows replace them, so the world's and the menus' keys never meet.
- The kinds `runtime/kbm_scheme.c` implements: `H` held, `P` press (70 ms), `T` tap or hold (250 ms, then the paired
  row is held), `M` macro, `O` menu operation, `E` claimed elsewhere, `X` no key here, `G` virtual key.
- The walk row: `Alt` scales the left stick to 45 %, `KBM_WALK_SCALE`.
- The contexts the host can tell on its own: `WORLD` (on foot) and `PAUSE` (while the F1 menu is open). The other 26
  scopes with rows are in the table and are selected as soon as something pushes the context (next section).

## What is approximated, and how

The upstream port ends every action on the game's own `PlayerVirtualKey` word and runs a macro for the actions no
single button means. Neither exists here yet, so:

- A hold macro holds its row's pad bits while the key is down: `MAC_ATTACK`, `MAC_SPRINT`, `MAC_BOW_DRAW`, the arms
  (`MAC_PRIMARY`, `MAC_SECONDARY`, `MAC_RAISE`, `MAC_LOWER`, `MAC_PUSH`, `MAC_PULL`, `MAC_ROTATE`), and the six
  ability picks plus the two strips, whose pad bits open a wheel or a strip that the stick then walks and the
  release picks.
- Every other macro pulses its bits for `KBM_PULSE_MS` on the press (the holster, the bow toggle, the arrow
  switching, the whistle).
- A `T` row whose tap is a macro with no pad bits pulses its pair's bits instead, so `F` tapped still does the nearest
  pad action once.
- `PCOP_CONFIRM`, `PCOP_CHOICE_PICK`, `PCOP_BACK`, `PCOP_TAB_PREV`, `PCOP_TAB_NEXT` and `PCOP_SCROLL` become their pad
  buttons. `PCOP_MENU_GOTO` and `PCOP_TAB_GOTO` walk a menu's pages through a cursor hook the port lacks, so they
  pulse their row's own bits (the screen still opens, and the player walks it with the stick).

## What this branch changes about the port

1. **`Esc` is the game's key again.** 0.3.1 had no `KEY_ESC`: `Esc` released the cursor and opened the host menu from
   the window message (`ui/menu_win.cpp`, `ui/menu_sdl.cpp`), so the table's ten `Esc` rows could not fire. The port adds
   the id (`input_map.h`, `KEY_ESC`; `input_win.c` and `input_sdl.c` map it), so `Esc` does what upstream's rows say:
   the pause menu in the world, one level back in a menu, the cancel while aiming, the skip of a cutscene. While the
   scheme runs, the host menu keeps `F1` only and `Esc` no longer releases the cursor (`input_kbm_scheme`), which is
   what makes the two uses coexist. With the scheme off, nothing changes: `Esc` opens the host menu as before, and no
   default binding names it.
2. **0.3.1's host chords are the pad's under the scheme.** `Minus+Plus` opens or closes the host menu and `L3+R3` steps
   the performance overlay; both read the merged word by default. Under the scheme the keyboard's `M` is the map and
   `Esc` the pause menu (`Ctrl` and `V` the stick clicks), so `M+Esc` or `Ctrl+V` would fire the host chords. The gates
   take the word the chord is read from (`input_menu_gate`'s last argument) and the poll passes the gamepad's bits
   while the scheme runs: the pad still toggles the menu and the overlay, the keyboard's keys reach the game.
3. **A wheel notch is an event, not a held key.** 0.3.1 has `KEY_WHEEL_UP` / `KEY_WHEEL_DOWN`, and `input.c` used to
   turn a notch into a key held for two polls. Two polls is not a press: at 144 fps it is 14 ms, under one sample of a
   30 Hz menu, and every notch that arrived during those polls merged into the same one, so a quick scroll stepped once.
   Now a notch is held for 70 ms and rests 70 ms, one notch at a time, with the notches of a quick scroll queued (up to
   eight a direction) — upstream's `wheel_read` (`hle_hid.cpp`). The scheme sees each notch as its own press, and so does
   the flat table's `mouse_wheel_up` / `mouse_wheel_down` binding.
4. **The wheel's strips step themselves.** The game's quick select is "hold D-pad right and walk the strip with the right
   stick", and the scheme has no "next weapon" pad action; upstream runs a game-side macro (`MAC_NEXT` / `MAC_PREV`,
   `pc_gameplay.cpp`). Here a notch on `weapon_next` / `weapon_prev` plays that macro at the pad level (`g_step`),
   counted in game frames the way upstream counts it: the d-pad until the strip is in front (6 frames), a 2-frame
   flick of the right stick, 2 frames more on the d-pad, then the release that equips. The frame length is measured
   from the presented frames, so the step plays the same at 30, 60 or 120 fps. With a quick menu already open (the
   player holds its key) the notch only flicks the stick: the strip is open, nothing to press or release.
   `arrow_next` / `arrow_prev` share the step, with D-pad up; the camera's zoom and the ability wheel's pulls still
   pulse their own bits.
5. **Every key is already sampled.** 0.3.1 fills `down[]` from every key id, with no "only the bound keys" mask, so the
   scheme's keys of its own (`Alt`, `G`, `V`, the digits) need no branch in the poll loop. `input.c` adds the scheme's
   branch and nothing else.

## Contexts in this patch

This patch does not include the game-screen watcher. The scheme runs `WORLD`; the host settings menu (F1) selects
`PAUSE`. The game's own pause, map, shop and quick-menu screens are not detected, so their rows are not selected; the
world rows and their group fallbacks apply there. `TOTK_SCREEN_CTX` and `TOTK_SCREENLOG` are not part of this patch.

## What still needs a game hook

1. **The player's state**, to reach `AIM`, `CLIMB`, `AIR`, `GLIDE`, `SWIM`, `ABILITY`, `HORSE`, `VEHICLE`,
   `CONSTRUCT`, `CAMERA`, `SCOPE`. Upstream pushes a `PCP_*` flag word once per game frame (`pc_ctx_player` in its
   `pc_input.h`). Here that would be a patch site that writes the flags into the patch page, read by
   `kbm_scheme_set_ctx`. Until then the scheme stays on foot, and `AIM`'s rows (the bow's draw, the arrow wheel, the
   Scope) are dead weight. The screen is read (above), but the player's stance is not in it.
2. **The camera veneer**, the direct mouse look: this patch includes it as the `mousedirect` set
   (`recomp/patches-v143.txt`, docs/mousedirect.md), which adds the mouse to the 1.4.3 PlayerCamera's processed stick
   after the dead zone and curve.
3. **Keyboard keycap prompts**: included as `prompts = keyboard`, which redraws the game's prompts with the PC scheme's
   keys (Kenney's CC0 "Input Prompts"; `tools/make_romfs_patches.py --prompts keyboard`). The action prompt's
   highlighted face button (`Nt_KeyTexA/B/X/Y_00`) is painted as its keycap, and its one-letter diamond panes become
   `E`, `S`, `_` and `L`: the layout's `Normal_00` font has no keycap glyphs, so those three stay text. The game reads
   its fonts once, so this takes a restart after the folder is written.
4. **Motion mechanics**: a narrow mouse-to-six-axis feed is included (`TOTK_MOUSE_MOTION_KEY`, default `k`): holding the
   key feeds the mouse to the virtual controller's six-axis for the gyro shrines, with the camera paused. The full
   mouse gyro aiming path is not part of this patch.

## What changes for the player

| Key | This repository (docs/controls.md) | The PC scheme |
|---|---|---|
| Left mouse | attack (Y) | attack, and with the bow in hand the draw |
| Right mouse | shield and lock-on (ZL) | the same, and with `E` held a parry, with `Space` and a direction a dodge |
| F | bow (ZR) | tapped: put the bow away or take it out; held 250 ms: draw, release to shoot |
| Q | abilities (L) | put away / take out the weapon |
| 1 2 3 4 | quick select | weapon, shield, material and bow strips |
| 4 5 6 7 8 9 | — | Ultrahand, Fuse, Ascend, Recall, Autobuild, camera |
| I | — | the sheet (the game's pause menu), on `PCOP_MENU_GOTO`'s bits |
| V | — | the Scope while aiming; `M` is the map elsewhere |
| Alt | — | walk (the stick at 45 %) |
| Esc | menu (the F1 menu) | the game's pause menu, and one level back in a menu; the host menu is F1's |
| Wheel | — | the weapon strip, one notch a weapon (a step of its own: D-pad right, a right-stick flick, the release that equips); the arrows while aiming and the camera's zoom still pulse their d-pad bits |

`Shift` is still run and `Space` still jumps; `Ctrl` still crouches.

## Deciding what to keep

Play both, then settle each line below. The switch makes this free: nothing is removed by trying the scheme.

| Question | What to watch | If the answer is no |
|---|---|---|
| Is the scheme's layout better on a keyboard? | the first ten minutes on foot: attack, shield, bow, the strips | keep `docs/controls.md`'s table |
| Does tap-or-hold on `F` earn its keep? | a fight where the bow comes out and goes away often | keep `F` = ZR |
| Are the ability digits (4-9) better than the `Q` wheel? | Ultrahand and Recall in a shrine | drop the six `ability` rows |
| Is the wheel worth binding? | switching arrows while aiming | drop `arrow_next` / `arrow_prev` |
| Does holding `Alt` to walk feel right? | a horse, a ledge, a photo | `TOTK_WALK_KEY` in the flat table does the same |

Keep the answer in this file: change the row in `runtime/kbm_scheme_*.inc` (they are data, not code), or turn the
whole scheme off with `TOTK_KBM_SCHEME=0`.

## Checks

`tests\kbm_scheme_test.py` builds `tests\kbm_scheme_test.c` against `runtime\kbm_scheme.c` and runs the cases the port
must hold: the table's size (255), the key names (0 unknown), the inherited and overridden rows, a `T` row's tap and its
hold, the walk scale, the menu operations' bits, an `X` row that kills an inherited key, and `Esc`'s three readings
(pause menu, back, the aim's cancel). It runs in about a second and needs no game.

`totk.exe --probe-cfg` still passes with `KBM_SCHEME` in the settings table: the key is registered before it is read,
which is what keeps `cfg_value("TOTK_KBM_SCHEME")` out of `rt_fatal`.
