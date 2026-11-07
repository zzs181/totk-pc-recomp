# Profile-guided optimisation

The game code is compiled from generated C: millions of branches whose taken/not-taken mix depends on the scene. Without
a profile the compiler guesses (loops taken, `if` not taken, every function equally likely), lays the code out for that
guess and inlines by size. With a profile it knows which of the generated functions carry the frame, how each branch
actually goes, and where the cold paths are — the same source, faster code, no behaviour change. On 1.4.3 the fight
bench ran 12-15% faster per frame with a profile, a fight outside the training about 6% (docs/building.md).

Two builds are involved: a **training** build that counts (`--pgo gen`), and the **optimised** build that reads the
counts (`--pgo FILE`). A profile only fits the generated C it was trained on: functions are matched by name and
control-flow hash, so a function whose C changes loses its counts without a warning. Retrain after a lifter or generator
change, from a clean `recomp/` (`tools/pgo.py merge` refuses a dirty one).

## The loop

```powershell
# 1. Instrumented build (needs llvm-mingw, below): build/v143-pgogen/totk.exe
python tools/build_game.py --version v143 --out build/v143-pgogen --toolchain toolchain\llvm-mingw-20261020\llvm-mingw-20261020-ucrt-x86_64 --pgo gen

# 2. Play it, 20-30 minutes through the scenes that should be fast. Counters land in
#    build/v143-pgogen/pgo/*.profraw: written every 60 s and at exit, and the runs
#    merge into the same file on their own (%m), so nothing to set per run.

# 3. Merge the runs into one profile: build/pgo/pgo-v143.profdata (+ .json record)
python tools/pgo.py merge build/v143-pgogen --version v143

# 4. Optimised build (MSYS2 clang is fine here: no profile runtime needed)
python tools/build_game.py --version v143 --pgo build/pgo/pgo-v143.profdata
```

Step 4 reads the profile through the same `--pgo` flag; every generated and runtime object then depends on the
.profdata (build_game.py's ninja rules), so a changed profile rebuilds everything it feeds. Leaving `--out` at the
default `build/<version>` means the play build in that folder is the optimised one — deploy from there as usual.

`python tools/pgo.py show` prints what the profile holds: the number of functions, the total count and the hottest
functions. A useful profile has the game's functions at the top; a profile trained on the title screen alone does not.
`--top N` lists more or fewer.

## What to play during training

Train on what should be fast, in one or a few runs: 20-30 minutes through a town, a field, a fight, the Depths and a
shrine covers most of the hot code. Menus, cutscenes and the title screen barely matter. Run at the frame rate and
resolution you care about — the counters are branch and function counts, so a session at your target fps in a busy
town says more than twice as long at the title screen.

Close the game normally when a run is done. The counters are written every 60 seconds and when the process exits
(runtime/platform/win32.c's flush thread and plat_exit), so a crash costs at most a minute of training instead of the
session; killing the process with Task Manager costs whatever came after the last write. Several runs merge into one
profile, and every run counts the same. `tools/run_game.py` scripted runs add to the same file (docs/building.md).

## Toolchain: only the training build is special

The instrumented build links clang's profiling runtime, `libclang_rt.profile`, which MSYS2's UCRT64 clang does not
ship. build_game.py probes this with a trivial link before compiling, so a broken toolchain fails in seconds. Two ways
to have the runtime:

- **llvm-mingw** (the release toolchain, what make_release.py pins): download the version from
  `LLVM_VERSION`/`LLVM_URL` in tools/make_release.py, check the sha256 there, and unzip to
  `toolchain\llvm-mingw-<date>\llvm-mingw-<date>-ucrt-x86_64\` (the double nesting is what tools/pgo.py globs). Pass
  `--toolchain toolchain\llvm-mingw-<date>\llvm-mingw-<date>-ucrt-x86_64` to the training build. Its bin/ also carries
  llvm-profdata, which tools/pgo.py finds on its own.

- Copy the file MSYS2's clang64 package does ship into the directory the UCRT64 clang looks in. The training build
  then uses the same compiler as the build it optimises:

  ```powershell
  $r = "C:\msys64\ucrt64\lib\clang\23\lib\x86_64-w64-windows-gnu"
  New-Item -ItemType Directory -Force $r | Out-Null
  Copy-Item "C:\msys64\clang64\lib\clang\23\lib\windows\libclang_rt.profile-x86_64.a" "$r\libclang_rt.profile.a"
  ```

  Then the training build needs no `--toolchain`; give `LLVM_PROFDATA` to tools/pgo.py or point `--toolchain` at any
  tree whose bin/ has llvm-profdata.

A Windows llvm-mingw build also needs `--deps` (or `toolchain/deps-mingw`): the Vulkan, SDL3, Opus, libvpx, VMA and
zstd headers and import libraries MSYS2 would supply, plus llvm-mingw's libc++/libc++abi/libunwind (the shape is
`build_deps()` in tools/make_release.py).

## Training record and releases

`tools/pgo.py merge` writes `build/pgo/pgo-<version>.profdata` and a `.json` beside it (the game version, the
`recomp/` git tree the training ran on, when it merged, how many raw files). `tools/make_release.py` picks the
newest profile from that folder, warns when its tree differs from the one it is building, compiles the release's
runtime library with it and ships the profile in the release's `data/` for the installer to build with. A profile
whose functions changed since training still works: the changed functions compile without profile data and only lose
the gain.
