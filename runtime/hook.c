// Host hooks (recomp/patches.py `hook`, gen.py). A hooked guest function keeps its name: its lifted body moves behind
// it and the name becomes `rt_hook_run(slot, c, body)`, so every caller -- a direct call, a tail call, rt_dispatch --
// arrives here with the frame exactly as the call left it. The runtime function bound to that slot decides what
// happens: run the body at once (nothing bound), or take the call itself.
//
// This is the one thing a `site` cannot do: a per-call constant the game computes for one 30 Hz frame. The hook sees
// the caller's registers as they stand (c->x[30] the return address the caller wrote before the call, c->v[] its
// vector registers), so it can act at the call it names and nowhere else, run the body itself, and leave corrected
// values behind for the caller to read. That is how the frame-rate-corrected feel patches work
// (weeping-of-the-empire's totk_effect_sway.cpp, totk_footik_ease.cpp, totk_swim_ease.cpp; MIT).
//
// The slots and their names come from the generated hooks.h; without it (C generated without --patches) there are none
// and every wrapper is a plain body call. rt_hooks_init (runtime/hooks.c) binds each one by name at start, so no guest
// address appears in the runtime and the same hook serves every game version: a build whose table lacks the hook
// simply never binds it, and the calls run the game's own code.
#include "cpu.h"
#include <stddef.h>
#include <string.h>

#if __has_include("hooks.h")
#include "hooks.h"
#endif
#ifndef RT_HOOK_COUNT
#define RT_HOOK_COUNT 0
#endif

#if RT_HOOK_COUNT
static RtHookFn g_hook[RT_HOOK_COUNT];   // written once at start, read on every call of a hooked function
static const char *const g_name[] = {
#define X(i, n) #n,
    RT_HOOKS(X)
#undef X
};
#endif

int rt_hook_slot(const char *name) {
#if RT_HOOK_COUNT
    for (int i = 0; i < RT_HOOK_COUNT; i++)
        if (!strcmp(g_name[i], name)) return i;
#endif
    (void)name;
    return -1;
}

int rt_hook_bind(const char *name, RtHookFn fn) {
    const int i = rt_hook_slot(name);
#if RT_HOOK_COUNT
    if (i >= 0) __atomic_store_n(&g_hook[i], fn, __ATOMIC_RELEASE);
#else
    (void)fn;
#endif
    // Silent on purpose: a build without the hook is the normal case (another game version, another table), and the
    // feature says what it did at its first call. A table that names a hook this build's hooks.c lacks binds nothing.
    return i;
}

void rt_hook_run(int slot, Cpu *c, void (*body)(Cpu *)) {
#if RT_HOOK_COUNT
    const RtHookFn fn = slot >= 0 && slot < RT_HOOK_COUNT ? __atomic_load_n(&g_hook[slot], __ATOMIC_ACQUIRE) : NULL;
    if (fn) {
        fn(c, body);
        return;
    }
#endif
    (void)slot;
    body(c);
}
