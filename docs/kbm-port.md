# The PC keyboard and mouse scheme

> Branch state: the scheme, the game-screen watcher (`screen_watch.c`, 1.4.3), the menu pointer (PR #605), the mouse
> gyro (`TOTK_MOUSE_MOTION_KEY`, `runtime/input.c`) and the keyboard keycap prompt set (`prompts = keyboard`) are in.
> The watcher names the game's screen (title, saves, pause, map, quick menu, Autobuild, camera, scope, shop, recipe,
> choice, dialogue, options, loading) and ZR selects `AIM`; the player's stance (climb, air, glide, swim, horse,
> vehicle) is not read, so those rows still fall back to `GAMEPLAY`.


A port of [Weeping of the Empire](https://github.com/daniel-madrid-07/weeping-of-the-empire)'s controls (MIT, v0.1.8.2):
one table of (context, action) rows decides what the keyboard and the mouse ask the game for, instead of the flat
key-to-button table in `docs/controls.md`. It is a second path, off by default, so both can be played and compared.

This branch is 0.3.1. Its input layer differs from the branch the scheme was first ported to (0.3.4) in three places
that the port had to meet; they are listed under "What this branch changes about the port" below.

## Turning it on

`TOTK_KBM_SCHEME=1` in `totk.ini` next to `totk.exe`, or in the environment (the environment wins). Live: the next
input poll switches, and the log says which path ran (`input: PC scheme on: 263 rows, 0 unknown keys, Esc is the
game's menu key`).

With it on, the Controls tab and the `TOTK_BIND_*` lines do nothing: they edit `input_map.c`'s table, which the
scheme does not read. `docs/controls.md` describes the other path. Everything else (the gamepad, its layout, rumble,
the six-axis, the mouse camera, the walk scale of the flat table) is untouched.

## What runs today

- The whole upstream table, 255 rows, plus the rows this branch adds: the four `AIM` rows that turn the inherited
  strips off while the bow is drawn, the ability's own `E` / `Shift` (the grab and the cancel must survive the stance
  context) and the map's `confirm` / `tab_next` pair (E teleports instead of paging the Pad): 263 rows (`PC_ROW` 223 +
  `PC_VROW` 40) over 28 of the 34 scopes (`runtime/kbm_scheme_ui.inc`,
  `runtime/kbm_scheme_gameplay.inc`, unchanged but for a `K` prefix on the pad bits).
- Row resolution: a context inherits its group's rows (`WORLD` <- `GAMEPLAY` <- `ROOT`, `PAUSE` <- `MENUS` <- `UI` <-
  `ROOT`) and its own rows replace them, so the world's and the menus' keys never meet.
- The kinds `runtime/kbm_scheme.c` implements: `H` held, `P` press (70 ms), `T` tap or hold (250 ms, then the paired
  row is held), `M` macro, `O` menu operation, `E` claimed elsewhere, `X` no key here, `G` virtual key.
- The walk row: `Alt` scales the left stick to 45 %, `KBM_WALK_SCALE`.
- The contexts the host can tell: the game's screen from `runtime/screen_watch.c` (`TITLE`, `SAVES`, `PAUSE`, `MAP`,
  `QUICKMENU`, `CONSTRUCT`, `CAMERA`, `SCOPE`, `SHOP`, `RECIPE`, `CHOICE`, `DIALOGUE`, `OPTIONS`, `LOADING`, `MENUS`),
  `AIM` from a held ZR, `PAUSE` for the F1 menu while it is open, and `WORLD` otherwise. The stance scopes (`CLIMB`,
  `AIR`, `GLIDE`, `SWIM`, `HORSE`, `VEHICLE`) and `CUTSCENE`/`TRANSITION` are in the table but nothing reads the
  player's state yet, so their rows fall back to `GAMEPLAY`.

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

`runtime/screen_watch.c` reads `eui::ScreenMgr` at each pad read (the game thread) and selects the focused screen's
context: `Title_00` is `TITLE`, `Pouch_00`/`Pause*` is `PAUSE`, the save list is `SAVES`, options, shops, dialogue and
choices get their rows, `AppMap_00` is `MAP`, the quick-select strips and the Autobuild list are `QUICKMENU`, the
camera and the scope are their contexts. The map has no focus box, so the input fallback uses the game's Minus toggle
for `MAP`. With no screen up the player's own stance names the context (next section), and `AIM` is selected once the
scheme's ZR has been held steadily for 150 ms. The host settings menu (F1) stays `PAUSE`. `TOTK_SCREEN_WATCH` (default
1) turns the watcher off, and `TOTK_SCREEN_WATCH_LOG` (default 1) logs each change with what chose it
(`screen: Pouch_00 -> pause (screen)`, `screen: (gameplay) -> ability (ability)`).

## The player's stance

`runtime/player_state.c` reads the player once per presented frame and `screen_watch.c` turns the answer into the
context. Two ways start the walk, so a table that drifts cannot take the feature down: the game's own GOT chain to the
player actor, and the fallback the `playerptr` site (`recomp/patches-v143.txt`, main+0x2b0348) leaves in the patch page
-- the player object the player update (main+0x2adef0) keeps in `x19`. From the actor it is the game's own walk:
components (+0x228 in 1.4.3) -> the AS component (+0x158) -> its five action slots (+0x1f0) -> each slot's verb (+0x28,
or +0x4c while +0x28 is 284 Invalid). The ability is the SpecialPower component (+0x478, its current type at +0x6c),
whose Ultrahand object (+0x28) reads 4 Grabbing / 5 Grabbed at +0x18 while it holds something. Nothing writes game
memory. `TOTK_PLAYER_STATE=0` stops the walk and every stance stays `WORLD`; `TOTK_PLAYER_STATE_LOG` (default 1) logs
the verbs, ability and hold once per change.

The verb numbers are 1.0.0's and 1.4.3 keeps them; the sets are upstream's (`flags_of` in Weeping's
`totk_player_state.cpp`). A play session named them here too: Walk 1, Jump 13, Land 32, Crouch 12, CrouchMove 63, Climb
70, ParasailGlide 94, Swim 97, UseBow 59, Drawn 57, UseSpecialPower 61 -- and the player state's own action name at
+0x174 (`Wait`, `Move`, `Jump`, `Land`, `UltraHandWaitLower`, `AutoBuilderWaitLower`, ...) agrees with each. `Land` is
deliberately not in `AIR`, and `RideJump` 27 / `RideJumpAttack` 132 only count as `HORSE` while the horse already was
the state (upstream's mask: the steering stick's jump-off logs 27 as well).

The router, with no screen up and a player read within the last 250 ms: `ABILITY` (verb 61 in any slot) > `HORSE` >
`VEHICLE` > `GLIDE` > `AIM` > `SWIM` > `CLIMB` > `AIR` > `WORLD` -- Weeping's `from_player` order, so aiming from the
saddle, a vehicle or the glider keeps those rows. A change between two gameplay contexts must hold the same answer for
30 ms (the verbs flicker at a transition); a screen always wins at once.

The ability context is also where the mouse changes hands: while Ultrahand holds an object `input.c` feeds the mouse to
the pad's right stick instead of the camera's direct look, because the held object reads the stick and the direct look
only reaches `PlayerCamera`; and with the rotate key (`R`, or `G`) held the scheme takes the mouse counts itself and
steps the d-pad, one step per 40 counts (upstream's `MAC_ROTATE`), so the object rotates instead of drifting.

## What still needs a game hook

1. **The camera veneer**, the direct mouse look: this patch includes it as the `mousedirect` set
   (`recomp/patches-v143.txt`, docs/mousedirect.md), which adds the mouse to the 1.4.3 PlayerCamera's processed stick
   after the dead zone and curve.
2. **Keyboard keycap prompts**: included as `prompts = keyboard`, which redraws the game's prompts with the PC scheme's
   keys (Kenney's CC0 "Input Prompts"; `tools/make_romfs_patches.py --prompts keyboard`). The mouse buttons are the one
   set Kenney's does not fit: its whole-mouse outline shades the button it means, and at HUD size that shade is a blur,
   so the left and right buttons are our own caps reading `LMB` and `RMB` in the game's text font. The d-pad is the
   other: Kenney draws it, and its up+down / left+right pairs, as four blank caps with nothing in them, so it is the
   keyboard's W A S D here, the four keys where a keyboard has them. The stick clicks are ours too (`CTRL` on the left
   one, the crouch, and `MMB` on the right, the scope and the menus' wheel click), which the game draws as an ellipse
   pressed from above. A menu gets its own set: its layouts carry the button's code point as text, so they name spare
   ones (`MENU_LAYOUTS`) and the font draws the UI context's key there. `--binds <totk.ini>` names the keys the player
   actually bound instead of the scheme's defaults (`tools/button_icons.py` KEY_LEGENDS covers the rest of the
   keyboard, so any key a bind names can be drawn). The action
   prompt's highlighted face button (`Nt_KeyTexA/B/X/Y_00`) is painted as its keycap, and its one-letter diamond panes
   become `E`, `S`, `_` and `L`: the layout's `Normal_00` font has no keycap glyphs, so those three stay text. The game
   reads its fonts once, so this takes a restart after the folder is written.
3. **Motion mechanics**: a narrow mouse-to-six-axis feed is included (`TOTK_MOUSE_MOTION_KEY`, default `k`): holding the
   key feeds the mouse to the virtual controller's six-axis for the gyro shrines, with the camera paused. The full
   mouse gyro aiming path is not part of this patch.
4. **The Keyboard & mouse tab** (`runtime/ui/kbm_page.cpp`): F1's tab lists every scheme action with one key cell, for
   all contexts or for one context. A captured key is written to `kbm_binds` as `action=key` or `context.action=key`;
   the context entry wins over the global one, and Delete clears it. While the throw key is held the wheel is the
   material page's own gesture (`runtime/kbm_scheme.c`, `g_mat`): the first notch takes D-pad up and **keeps it** for as
   long as the throw key is held -- the page only stays open while up is held -- and every later notch flicks the right
   stick inside that hold to walk the list. Releasing the throw key drops both, which is the game's own confirm; no A is
   ever pressed here. Two earlier attempts read as broken on purpose-built screenshots: pulsing up closed the page
   between notches, and walking the list with D-pad left / right did nothing the page answers to.
5. **The menu pointer** (`runtime/menu_cursor.c`, Wintego's PR #605 adapted from Weeping's `totk_menu_cursor.cpp`):
   on by default (`TOTK_MENU_CURSOR`). Its **read** path is the port's: the ScreenMgr/BoxCursor chain, the screen's
   layout name, its node list, the pointer's hit test and the OS pointer release in menus. Its **write** path is not:
   the three stores `moveBoxCursor` makes leave 1.4.3's box where it is, because eui recomputes the cursor from its own
   layout graph every frame. What the box does obey is the d-pad, so the hover is reached by pressing the direction that
   closes the gap -- one press per box move, 90 ms for the animation to land, and the drive stops after three presses
   that move nothing (so keys and the pad keep a menu they were navigated with, and a screen that ignores the d-pad is
   named once in the log). Verified on 1.4.3 with the save list's eight rows: hovering the fifth, the first and the
   fourth walks the box down, up and down again and stops on the pointer's own row, and a left click on a row is A on it
   (the read confirmation opens on the row clicked); a right click is B. `TOTK_MENU_CURSOR_LOG=1` prints the pointer's
   numbers, the node count, the hit result and every press the drive makes.

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
