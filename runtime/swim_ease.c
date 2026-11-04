// Link's swim velocity keeps the Switch's ease at any frame rate (TOTK_SWIM_EASE=0: the game's own, per frame).
//
// ExecutePlayerSwimMove (main+0x2412fb4 in 1.4.3) eases the node's velocity toward the stick's target once per FRAME
// with a fixed factor and no dt:
//   s = s9 * <the actor's swim speed getter>(this)      (0x2413078, and 0x2413084 once the getter returns)
//   v[i] += (s * dir[i] - v[i]) * k                     (0x24130ac .. 0x24130b4; k = the f32 0.175 at 0x2d0215c)
// where v = node+0x24 (0x241308c) and dir = the caller's sp+0x30 (0x2413080, 0x2413094). The velocity settles in 170 ms
// at 30 fps and 43 ms at 120, so at a high frame rate swim starts, stops and turns snap instead of easing.
//
// The hook pre-sets v = t + (v - t) * q / (1 - k), q = (1 - k)^r (r the frame's 30 Hz ratio), t = s * dir as the caller
// computes it, so the caller's own step lands on t + (v - t) * q: the 30 Hz ease in game time. It acts at one call
// only, the getter's return address 0x2413080; every other call of the getter is the original. At 30 fps and below
// (r >= 1), and without the real-time step whose ratio it corrects by, the original exactly.
//
// The shape and the math are weeping-of-the-empire's totk_swim_ease.cpp (MIT, f2c81da), whose addresses are 1.0.0: the
// getter main+0x16e4f90, its return address main+0x1d25ac0, the factor main+0x377671c. The 1.4.3 addresses above were
// re-found with tools/version_port.py and the image itself: the getter and the call are the same instruction words as
// 1.0.0's (0xfc1e0fe8, 0x2d460be1) and the factor is the same bytes (3333333e), at the offsets this file reads.
#include "cfg.h"
#include "host.h"
#include "layout.h"
#include "swim_ease.h"

#include <string.h>

#define SWIM_CALL 0x2413080u    // main+0x2413080: the return address the eased call leaves
#define SWIM_FACTOR 0x2d0215cu  // main+0x2d0215c: f32 0.175, the caller's per-call factor
#define SWIM_VEL 0x24u          // node: the eased velocity, 3 floats
#define SWIM_DIR 0x30u          // the caller's sp: the stick direction, 3 floats

static int on = -1;   // TOTK_SWIM_EASE, read once at the call the hook is for

// A guest float lane of the vector registers: the low 32 bits of c->v[n], read as the float they are (not converted
// from an integer).
static float vf(const Cpu *c, int n) {
    const uint32_t bits = (uint32_t)c->v[n];
    float v;
    memcpy(&v, &bits, sizeof v);
    return v;
}

// A guest float at an address: guest addresses are host addresses (host.h), and main's .rodata is mapped.
static float gf32(uint64_t a) {
    float v;
    memcpy(&v, (const void *)(uintptr_t)a, sizeof v);
    return v;
}

void swim_ease_hook(Cpu *c, void (*body)(Cpu *)) {
    const uint64_t lr = c->x[30], node = c->x[21], sp = c->sp;
    const float s9 = vf(c, 9);   // the caller's stick x state factor (callee-saved, so it survives the getter)
    body(c);
    if (lr != SWIM_CALL) return;
    if (on < 0) {
        const char *e = cfg_get("TOTK_SWIM_EASE");
        on = !(e && !strcmp(e, "0"));
        rt_log("swim ease: the swim velocity is %s", on ? "eased in game time" : "eased per frame, as the game (TOTK_SWIM_EASE=0)");
    }
    if (!on) return;
    // The ratio comes from the step the game is stepping, which is only measured with the real-time step on. With the
    // shipping whole-vsync step there is no measurement, and guessing one would ease the swim at a rate the game is not
    // running at; leave it alone (TOTK_REALTIME=1 is the default above 30 fps).
    if (!frame_step_realtime()) return;
    const float r = frame_step_ratio();
    if (!(r > 0.01f && r < 1.0f)) return;   // 30 fps and below, or no real-time step: the game's own
    const float k = gf32(MOD_MAIN_BASE + SWIM_FACTOR);
    const float s = s9 * vf(c, 0);   // s0 is the getter's result: the caller's fmul s0, s9, s0
    float dir[3], v[3];
    for (int i = 0; i < 3; i++) {
        dir[i] = *(const float *)(uintptr_t)(sp + SWIM_DIR + 4 * (uint64_t)i);
        v[i] = *(const float *)(uintptr_t)(node + SWIM_VEL + 4 * (uint64_t)i);
    }
    if (!swim_ease_step(s, k, r, dir, v)) return;
    for (int i = 0; i < 3; i++) *(float *)(uintptr_t)(node + SWIM_VEL + 4 * (uint64_t)i) = v[i];
}
