/* tests/swim_ease_hook_test.c: runtime/swim_ease.c's hook on a synthetic frame -- the register reads, the gate on the
 * getter's return address, the offsets it writes, and the cases where it must leave the game's own step alone.
 *
 * Everything the host would provide is a stub here: cfg_get/rt_log/frame_step_realtime/frame_step_ratio, and the
 * module base the factor is read from (a fake layout.h, written by swim_ease_test.py, points MOD_MAIN_BASE at a float
 * this file owns). The node and the caller's stack are real buffers, so the writes land in memory the test can read.
 *
 * Run twice: with no argument for the corrections, and with "off" for TOTK_SWIM_EASE=0 (the hook's switch latches on
 * its first gated call, so each case has to be its own process).
 *
 * Build and run: python tests/swim_ease_test.py
 */
#include "cpu.h"
#include "swim_ease.h"

#include <math.h>
#include <stdint.h>
#include <stdio.h>
#include <string.h>

/* ---- what the runtime would provide ---- */
uint64_t g_fake_main_base;                 // MOD_MAIN_BASE in the fake layout.h
static const char *g_env;                  // what cfg_get returns for TOTK_SWIM_EASE
static int g_realtime = 1;
static float g_ratio = 0.5f;

const char *cfg_get(const char *name) {
    (void)name;
    return g_env;
}

void rt_log(const char *fmt, ...) { (void)fmt; }

int frame_step_realtime(void) { return g_realtime; }

float frame_step_ratio(void) { return g_ratio; }

void swim_ease_hook(Cpu *c, void (*body)(Cpu *));   // runtime/swim_ease.c

/* ---- the test ---- */
#define SWIM_CALL 0x2413080u
#define SWIM_FACTOR 0x2d0215cu
#define SWIM_VEL 0x24u
#define SWIM_DIR 0x30u

static int failed, body_calls;

static void ok(int cond, const char *what) {
    printf("%s  %s\n", cond ? "ok  " : "FAIL", what);
    if (!cond) failed++;
}

static void body(Cpu *c) {
    (void)c;
    body_calls++;
}

static void cpu_set(Cpu *c, float s9, float speed) {
    memset(c, 0, sizeof *c);
    c->x[30] = SWIM_CALL;
    c->v[9] = (__uint128_t)(uint32_t)0;
    memcpy(&c->v[9], &s9, 4);
    c->v[0] = (__uint128_t)(uint32_t)0;
    memcpy(&c->v[0], &speed, 4);
}

int main(int argc, char **argv) {
    const int off = argc > 1 && !strcmp(argv[1], "off");
    g_env = off ? "0" : NULL;

    // the factor lives at MOD_MAIN_BASE + 0x2d0215c, as main's .rodata does
    static float factor[2] = {0.175f, 0.0f};
    g_fake_main_base = (uint64_t)(uintptr_t)&factor[0] - SWIM_FACTOR;

    float vel[3] = {1.0f, 2.0f, 3.0f};       // node + 0x24
    float dir[3] = {0.6f, 0.0f, -0.8f};      // the caller's sp + 0x30
    Cpu c;

    cpu_set(&c, 3.0f, 1.0f);                 // s9 = 3, the getter returns 1
    c.x[21] = (uint64_t)(uintptr_t)vel - SWIM_VEL;
    c.sp = (uint64_t)(uintptr_t)dir - SWIM_DIR;

    if (off) {
        float before[3];
        memcpy(before, vel, sizeof before);
        swim_ease_hook(&c, body);
        ok(body_calls == 1, "the game's own function still runs with TOTK_SWIM_EASE=0");
        ok(memcmp(before, vel, sizeof vel) == 0, "and the velocity is left exactly as it was");
        goto done;
    }

    {
        // another caller of the same getter: the hook must not touch anything
        Cpu other = c;
        other.x[30] = 0x241307c;             // the call itself, not its return address
        float before[3];
        memcpy(before, vel, sizeof before);
        body_calls = 0;
        swim_ease_hook(&other, body);
        ok(body_calls == 1 && memcmp(before, vel, sizeof vel) == 0,
           "a call at another address runs the game's function untouched");
    }

    {
        // no real-time step: the ratio would be a guess, so nothing happens
        float before[3];
        memcpy(before, vel, sizeof before);
        g_realtime = 0;
        body_calls = 0;
        swim_ease_hook(&c, body);
        ok(body_calls == 1 && memcmp(before, vel, sizeof vel) == 0,
           "without the real-time step the velocity is left alone");
        g_realtime = 1;
    }

    {
        // the corrected frame: v = t + (v - t) * (1-k)^r, r = 1/2
        float v0[3];
        memcpy(v0, vel, sizeof v0);
        const float k = 0.175f, s = 3.0f * 1.0f;
        body_calls = 0;
        swim_ease_hook(&c, body);
        ok(body_calls == 1, "the game's own function runs at the eased call too");
        int good = 1;
        const double keep = pow(1.0 - (double)k, 0.5) / (1.0 - (double)k);
        for (int i = 0; i < 3; i++) {
            const double t = (double)(dir[i] * s);
            const double want = t + ((double)v0[i] - t) * keep;
            if (fabs((double)vel[i] - want) > 1e-5) good = 0;
        }
        ok(good, "the velocity is pre-set so the caller's step lands on the 30 Hz ease");
    }

    {
        // r = 1 (30 fps): the game's own step is already right
        float before[3];
        memcpy(before, vel, sizeof before);
        g_ratio = 1.0f;
        swim_ease_hook(&c, body);
        ok(memcmp(before, vel, sizeof vel) == 0, "at 30 fps the velocity is left alone");
        g_ratio = 0.5f;
    }

done:
    if (failed)
        printf("\nswim_ease_hook_test: %d failed%s\n", failed, off ? " (off)" : "");
    else
        printf("\nswim_ease_hook_test: all passed%s\n", off ? " (off)" : "");
    return failed != 0;
}
