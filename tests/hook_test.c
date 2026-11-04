/* tests/hook_test.c: runtime/hook.c, the wrapper a hooked guest function becomes (recomp/patches.py `hook`).
 *
 * The caller's return address is in c->x[30] when the wrapper runs, which is how a hook acts at one call of a function
 * and nowhere else; this drives that, the unbound slot, a hook that takes the call over, and the values the hook
 * leaves for the caller. hooks.h comes from hook_test.py (one slot, "sway").
 *
 * Build and run: python tests/hook_test.py
 */
#include "cpu.h"

#include <stdio.h>
#include <string.h>

static int ran_hook, ran_body;
static uint64_t hook_lr, body_x0;

static void body(Cpu *c) {
    ran_body++;
    body_x0 = c->x[0];
    c->x[1] = 0xbb;          // what the game's function would leave behind
}

/* The shape of a feel patch: the frame-rate correction happens at the one call it names, every other call of the same
 * function is the original. */
static void hook_sway(Cpu *c, void (*orig)(Cpu *)) {
    ran_hook++;
    hook_lr = c->x[30];
    if (c->x[30] != 0x10000) return;         // another caller: it keeps the game's own constants
    orig(c);
    c->v[9] = (__uint128_t)0x3f800000u;      // the corrected per-call constant the caller reads back
}

static int failed;

static void ok(int cond, const char *what) {
    printf("%s  %s\n", cond ? "ok  " : "FAIL", what);
    if (!cond) failed++;
}

int main(void) {
    Cpu c;
    memset(&c, 0, sizeof c);
    c.x[0] = 0x1234;

    ok(rt_hook_slot("sway") == 0, "the generated table declares the slot");
    ok(rt_hook_slot("not-in-this-build") == -1, "an unknown name has no slot");
    ok(rt_hook_bind("sway", hook_sway) == 0, "a hook binds by name");

    /* unbound is not the case here: bind first, then check the other caller */
    c.x[30] = 0x20000;                       // a different call site
    rt_hook_run(0, &c, body);
    ok(ran_hook == 1 && hook_lr == 0x20000, "the hook sees the caller's return address");
    ok(ran_body == 0 && c.x[1] == 0, "a hook that returns takes the call over: the body did not run");

    c.x[30] = 0x10000;                       // the call the hook is for
    rt_hook_run(0, &c, body);
    ok(ran_body == 1 && body_x0 == 0x1234, "the hook runs the body, with the caller's registers");
    ok(c.x[1] == 0xbb, "the body's own writes reach the caller");
    ok((uint64_t)c.v[9] == 0x3f800000u, "what the hook leaves after the body is what the caller reads");

    ok(rt_hook_bind("not-in-this-build", hook_sway) == -1, "binding an unknown name says so");

    ran_hook = 0;
    ok(rt_hook_bind("sway", NULL) == 0, "a bound hook can be taken off again");
    rt_hook_run(0, &c, body);
    ok(ran_body == 2 && ran_hook == 0, "with nothing bound the wrapper is a plain call of the body");

    if (failed)
        printf("\nhook_test: %d failed\n", failed);
    else
        printf("\nhook_test: all passed\n");
    return failed != 0;
}

/* rt_log is the one thing hook.c needs from the host; a test run has no log. */
void rt_log(const char *fmt, ...) { (void)fmt; }
