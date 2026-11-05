// The player's state for the PC scheme's gameplay contexts (climbing, gliding, swimming, riding, an ability in use),
// read from the game's own objects; nothing here writes game memory.
//
// Two ways to the object the walk starts from, so a table that drifts cannot take the whole feature down:
//  - the game's own GOT chain (the player actor): GOT slot main+0x4562308 -> +0x20 BaseProcLinkBase -> the unit whose id
//    must match the link's, its proc, not while the proc is being deleted. Weeping of the Empire reads it this way
//    (engine/runtime/totk_globals.h, MIT, Copyright (c) 2026 daniel-madrid-07).
//  - the fallback: the player object the player update (main+0x2adef0) keeps in x19, which the playerptr patch site
//    stores into the patch page (+0x120, recomp/patches-v143.txt). Its +0x18 is the same actor.
// The walk from the actor on is the game's own, as the player update does it at main+0x2b0390 and as Weeping's
// totk_player_state.cpp reads it: components (Actor+0x228 in 1.4.3) -> the AS component (+0x158) -> its five slots
// (+0x1f0) -> each slot's verb (+0x28, or +0x4c when +0x28 is 284 Invalid). The ability is the SpecialPower component
// (+0x478): +0x6c mCurrentSpecialPower, and +0x28 the Ultrahand object whose +0x18 mState 4/5 means it holds something.
//
// The verb numbers are 1.0.0's and 1.4.3 keeps them (the game's own per-verb name table names the same verb at every
// index 0..283 in both builds). TOTK_PLAYER_STATE=0 turns the whole thing off; TOTK_PLAYER_STATE_LOG=0 stops the log
// (the log prints only changes, so a quiet session writes nothing).
#include <stdint.h>
#include <stdio.h>
#include <string.h>

#include "cfg.h"
#include "host.h"
#include "input_map.h"
#include "nvn_api.h"   // gfx_counters.frames: the once-per-frame gate
#include "layout.h"
#include "platform/platform.h"
#include "player_state.h"

#define MAIN_SIZE MOD_MAIN_SIZE
#define PAGE_PLAYER 0x120   // player_state_poll's fallback: the player object pointer, from the playerptr site

// 1.4.3 placement. The player update's own walk (main+0x2b0390) uses these same numbers; the ones that moved from 1.0.0
// are marked, from Weeping's games/totk/patches/fields_v143.json (Actor's component array 0x220 -> 0x228).
enum {
    SLOT_PLAYER_ACTOR = 0x18,     // [player object + 0x18] = the actor
    ACTOR_COMPS = 0x228,          // (1.0.0: 0x220)
    COMPS_AS = 0x158, COMPS_POWER = 0x478,
    AS_SLOTS = 0x1f0, SLOT_VERB = 0x28, SLOT_VERB_ALT = 0x4c,
    POWER_CURRENT = 0x6c, POWER_UH_OBJ = 0x28,
    UH_STATE = 0x18,
};
enum { VERB_INVALID = 284 };
enum { POWER_IS_ULTRAHAND = 1 };
enum { UH_GRABBING = 4, UH_GRABBED = 5 };
// the GOT chain to the player actor (Weeping's totk_globals.h): the slot, then the fields it walks
enum { ROOT_LINK = 0x20, LINK_UNIT = 0x8, LINK_ID = 0x10, UNIT_PROC = 0x40, UNIT_ID = 0x48, PROC_STATE = 0x24 };
#define PLAYER_ROOT_SLOT 0x4562308ull

static uint64_t g_verbs[5] = {~0ull, ~0ull, ~0ull, ~0ull, ~0ull};
static int g_power, g_holding, g_from_page;
static uint32_t g_flags;     // last walk's PS_* bits
static uint64_t g_ok_ms;     // when that walk found a player (0: never)

// Readability, with a small cache: plat_mem_readable is a VirtualQuery (a kernel call), and the walk touches a dozen
// pointers in the same few heap pages. Only the containing page is cached, so a cache hit is never less safe than the
// query it replaces.
static int ok(uint64_t p, uint64_t n) {
    if (p < 0x10000 || p >= (1ull << 47) || (p & 3) || n > 0x1000) return 0;
    static struct { uint64_t lo, hi; } cache[8];
    static int next;
    for (int i = 0; i < 8; i++)
        if (cache[i].hi && p >= cache[i].lo && p + n <= cache[i].hi) return 1;
    if (!plat_mem_readable(p, n)) return 0;
    int slot = next++ & 7;   // (one index for both halves: next++ inside the two subscripts split them)
    cache[slot].lo = p & ~0xfffull;
    cache[slot].hi = (p + n + 0xfff) & ~0xfffull;
    return 1;
}
static uint64_t rd64(uint64_t a) { uint64_t v = 0; memcpy(&v, (const void *)(uintptr_t)a, 8); return v; }
static uint32_t rd32(uint64_t a) { uint32_t v = 0; memcpy(&v, (const void *)(uintptr_t)a, 4); return v; }
static int p64(uint64_t p) { return p >= 0x10000 && p < (1ull << 47) && !(p & 7); }

// The actor through the game's own global: the proc behind the slot, with the link's id checks the game makes.
static uint64_t actor_from_global(void) {
    // Weeping's number is a 1.0.0 offset and 1.4.3 moves it through their private engine map; until our own slot is
    // re-derived, only use it when it lands inside main's image (it does not on 1.4.3: 0x4562308 > main's size).
    if (PLAYER_ROOT_SLOT + 8 > (uint64_t)MAIN_SIZE) return 0;
    uint64_t slot = MOD_MAIN_BASE + PLAYER_ROOT_SLOT;
    if (!ok(slot, 8)) return 0;
    uint64_t root = rd64(slot);
    if (!p64(root) || !ok(root, ROOT_LINK + 8)) return 0;
    uint64_t link = root + ROOT_LINK;
    if (!ok(link, LINK_ID + 4)) return 0;
    int32_t id = (int32_t)rd32(link + LINK_ID);
    uint64_t unit = rd64(link + LINK_UNIT);
    if (id == -1 || !p64(unit) || !ok(unit, UNIT_ID + 4)) return 0;
    if ((int32_t)rd32(unit + UNIT_ID) != id) return 0;
    uint64_t proc = rd64(unit + UNIT_PROC);
    if (!p64(proc) || !ok(proc, PROC_STATE + 4)) return 0;
    uint32_t state = (uint32_t)rd32(proc + PROC_STATE);
    if (state - 7u < 5u) return 0;   // being deleted
    return proc;
}

// The fallback: the object the player update keeps in x19, stored by the patch site.
static uint64_t player_from_page(void) {
    uint8_t *page = rt_patch_page();
    if (!page) return 0;
    uint64_t player;
    memcpy(&player, page + PAGE_PLAYER, sizeof player);
    if (!p64(player) || !ok(player, SLOT_PLAYER_ACTOR + 8)) return 0;
    return rd64(player + SLOT_PLAYER_ACTOR);
}

static int log_on(void);
static void chain_probe(uint64_t actor, uint64_t player);

static void read_state(int verbs[5], int *power, int *holding, int *from_page) {
    for (int i = 0; i < 5; i++) verbs[i] = -1;
    *power = 0, *holding = 0, *from_page = 0;
    uint64_t actor = actor_from_global();
    uint64_t player = 0;
    if (!actor) {
        uint8_t *page = rt_patch_page();
        if (page) memcpy(&player, page + PAGE_PLAYER, sizeof player);
        actor = player_from_page();
        *from_page = actor != 0;
    }
    chain_probe(actor, player);
    if (!actor || !ok(actor, ACTOR_COMPS + 8)) return;
    uint64_t comps = rd64(actor + ACTOR_COMPS);
    if (!p64(comps) || !ok(comps, COMPS_AS + 8)) return;
    uint64_t as = rd64(comps + COMPS_AS);
    if (p64(as) && ok(as, AS_SLOTS + 8 * 5)) {   // five slot pointers live in the AS component itself
        for (int i = 0; i < 5; i++) {
            uint64_t slot = rd64(as + AS_SLOTS + 8ull * i);
            if (!p64(slot) || !ok(slot, SLOT_VERB_ALT + 4)) continue;
            int32_t v = (int32_t)rd32(slot + SLOT_VERB);
            if (v == VERB_INVALID) v = (int32_t)rd32(slot + SLOT_VERB_ALT);
            verbs[i] = v;
        }
    }
    if (!ok(comps, COMPS_POWER + 8)) return;
    uint64_t sp = rd64(comps + COMPS_POWER);
    if (!p64(sp) || !ok(sp, POWER_CURRENT + 4)) return;
    *power = (int32_t)rd32(sp + POWER_CURRENT);
    if (*power == POWER_IS_ULTRAHAND && ok(sp, POWER_UH_OBJ + 8)) {
        uint64_t uh = rd64(sp + POWER_UH_OBJ);
        if (p64(uh) && ok(uh, UH_STATE + 4)) {
            int32_t st = (int32_t)rd32(uh + UH_STATE);
            *holding = st == UH_GRABBING || st == UH_GRABBED;
        }
    }
}

// The stance bits of one walk: the verb sets are Weeping's (totk_player_state.cpp flags_of, 1.0.0 numbering, the same
// numbers on 1.4.3). Land 32 is excluded from AIR: a repeated jump reads Jump 13 then Land 32 and the router's 30 ms
// settle keeps that from flickering the context.
static uint32_t stance_flags(const int v[5], int holding) {
    if (v[0] < 0) return 0;
    const int m = v[0];
    uint32_t f = PS_VALID;
    if (m == 70 || m == 71 || m == 96 || m == 68 || m == 69 || m == 14 || m == 15) f |= PS_CLIMB;
    if (m == 94 || m == 95 || m == 56) f |= PS_GLIDE;
    if (m == 13 || m == 33 || m == 5 || m == 19 || m == 20 || m == 16 || m == 17 || m == 259 || m == 131 || m == 173)
        f |= PS_AIR;
    if (m == 97 || m == 18 || m == 191) f |= PS_SWIM;
    if (m == 24 || m == 26 || m == 27 || m == 132) f |= PS_HORSE;
    if (m == 25 || m == 199 || m == 257) f |= PS_VEHICLE;
    for (int i = 0; i < 5; i++) {
        if (v[i] == 59 || v[i] == 205) f |= PS_AIM;   // UseBow / DrawnBow: the bow in hand
        if (v[i] == 61) f |= PS_ABILITY;              // UseSpecialPower: an ability out
    }
    if (holding) f |= PS_HOLDING;
    // RideJump 27 / RideJumpAttack 132 are also the steering stick's jump-off; count them as the horse only while the
    // horse already was the state (Weeping's mask, before its push).
    if ((f & PS_HORSE) && (m == 27 || m == 132) && !(g_flags & PS_HORSE)) f &= ~PS_HORSE;
    return f;
}

// One line per new player object: every link of the walk, so a chain that breaks says where (TOTK_PLAYER_STATE_LOG).
static void chain_probe(uint64_t actor, uint64_t player) {
    static uint64_t seen;
    if (player == seen || !log_on()) return;
    seen = player;
    uint64_t comps = 0, as = 0, slots = 0, slot0 = 0;
    int verb = -1, alt = -1, okc = 0, oka = 0, okas = 0;
    if (player && ok(player, SLOT_PLAYER_ACTOR + 8)) player += 0;   // (the actor is read by the caller)
    if (actor && ok(actor, ACTOR_COMPS + 8)) comps = rd64(actor + ACTOR_COMPS), okc = p64(comps);
    if (okc && ok(comps, COMPS_AS + 8)) as = rd64(comps + COMPS_AS), oka = p64(as);
    if (oka && ok(as, AS_SLOTS + 8 * 5)) slot0 = rd64(as + AS_SLOTS), okas = p64(slot0), slots = as + AS_SLOTS;
    if (okas && ok(slot0, SLOT_VERB_ALT + 4)) verb = (int32_t)rd32(slot0 + SLOT_VERB), alt = (int32_t)rd32(slot0 + SLOT_VERB_ALT);
    rt_log("player_state: chain player %p actor %p comps %p as %p slots %p slot0 %p verb %d alt %d", (void *)(uintptr_t)player,
           (void *)(uintptr_t)actor, (void *)(uintptr_t)comps, (void *)(uintptr_t)as, (void *)(uintptr_t)slots,
           (void *)(uintptr_t)slot0, verb, alt);
}

static int log_on(void) {
    const char *v = cfg_get("TOTK_PLAYER_STATE_LOG");
    return !(v && !strcmp(v, "0"));
}

void player_state_poll(void) {
    static int inited;
    static uint64_t done_frame = ~0ull;
    static int early = 3;   // the first polls log whatever they find, so a walk that never finds the player says so
    if (!inited) {
        inited = 1;
        const char *v = cfg_get("TOTK_PLAYER_STATE");
        if (v && !strcmp(v, "0")) { g_verbs[0] = 0; g_ok_ms = 0; return; }
    }
    const char *off = cfg_get("TOTK_PLAYER_STATE");
    if (off && !strcmp(off, "0")) { g_ok_ms = 0, g_flags = 0; return; }
    // once per presented frame: the walk is cheap, but not free, and nothing in it changes between two pad reads
    uint64_t frame = gfx_counters.frames;
    if (frame == done_frame) return;
    done_frame = frame;
    int verbs[5], power, holding, from_page;
    read_state(verbs, &power, &holding, &from_page);
    const uint32_t flags = stance_flags(verbs, holding);
    if (flags) g_ok_ms = plat_now_ms();
    g_flags = flags;
    int changed = 0;
    for (int i = 0; i < 5; i++)
        if ((uint64_t)verbs[i] != g_verbs[i]) changed = 1;
    if (power != g_power || holding != g_holding) changed = 1;
    if (*g_verbs == ~0ull && verbs[0] < 0) changed = 0;   // still "no player": do not log every frame
    static int had_player;
    if (!had_player && verbs[0] >= 0) had_player = 1, early = 2;   // the first walk that finds the player
    if (early > 0) early--, changed = 1;
    if (changed && log_on()) {
        uint8_t *page = rt_patch_page();
        uint64_t pageptr = 0;
        if (page) memcpy(&pageptr, page + PAGE_PLAYER, sizeof pageptr);
        uint64_t root = 0;
        if (ok(MOD_MAIN_BASE + PLAYER_ROOT_SLOT, 8)) root = rd64(MOD_MAIN_BASE + PLAYER_ROOT_SLOT);
        rt_log("player_state: verbs %d,%d,%d,%d,%d power %d holding %d%s  (root %p page %p frame %llu)", verbs[0],
               verbs[1], verbs[2], verbs[3], verbs[4], power, holding, from_page ? " page-path" : "",
               (void *)(uintptr_t)root, (void *)(uintptr_t)pageptr, (unsigned long long)gfx_counters.frames);
    }
    if (!changed) return;
    for (int i = 0; i < 5; i++) g_verbs[i] = (uint64_t)verbs[i];
    g_power = power, g_holding = holding, g_from_page = from_page;
}

int player_state_verb(int slot) { return slot >= 0 && slot < 5 && g_verbs[slot] != ~0ull ? (int)(int32_t)g_verbs[slot] : -1; }
int player_state_power(void) { return g_power; }
int player_state_holding(void) { return g_holding; }

uint32_t player_state_flags(uint64_t now_ms) {
    if (!g_ok_ms || now_ms - g_ok_ms >= 250) return 0;
    return g_flags;
}
