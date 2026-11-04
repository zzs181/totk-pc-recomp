/* tests/swim_ease_test.c: the per-call fix in runtime/swim_ease.h, without the game or the hook.
 *
 * The property that matters is that the hook plus the caller's own step is still the 30 Hz ease: two frames at 60 Hz
 * must land exactly where one frame at 30 Hz does, and the game's own step must be left alone wherever the rate does
 * not ask for a correction.
 *
 * Build and run: python tests/swim_ease_test.py
 */
#include "swim_ease.h"

#include <math.h>
#include <stdio.h>
#include <string.h>

static int failed;

static void ok(int cond, const char *what) {
    printf("%s  %s\n", cond ? "ok  " : "FAIL", what);
    if (!cond) failed++;
}

/* The caller's own step, as the game's fmadd does it: v += (t - v) * k in float. */
static void caller_step(float k, const float t[3], float v[3]) {
    for (int i = 0; i < 3; i++) v[i] = v[i] + (t[i] - v[i]) * k;
}

static int same(const float a[3], const float b[3], float tol) {
    for (int i = 0; i < 3; i++)
        if (fabsf(a[i] - b[i]) > tol) return 0;
    return 1;
}

int main(void) {
    const float k = 0.175f;                     // 1.4.3 main+0x2d0215c
    const float s = 3.0f;                       // the caller's s9 * getter
    const float dir[3] = {0.6f, 0.0f, -0.8f};
    const float start[3] = {1.0f, 2.0f, 3.0f};
    float t[3];
    for (int i = 0; i < 3; i++) t[i] = dir[i] * s;

    float slow[3], fast[3];
    memcpy(slow, start, sizeof slow);
    caller_step(k, t, slow);                    // one frame at 30 fps, the game's own ease

    memcpy(fast, start, sizeof fast);
    for (int n = 0; n < 2; n++) {               // two frames at 60 fps: the hook, then the caller's step
        ok(swim_ease_step(s, k, 0.5f, dir, fast), n == 0 ? "the hook acts at 60 fps" : "the hook acts on the second frame");
        caller_step(k, t, fast);
    }
    ok(same(fast, slow, 1e-5f), "two frames at 60 fps ease as far as one at 30");

    memcpy(fast, start, sizeof fast);
    for (int n = 0; n < 4; n++) {               // and four at 120
        swim_ease_step(s, k, 0.25f, dir, fast);
        caller_step(k, t, fast);
    }
    ok(same(fast, slow, 1e-5f), "four frames at 120 fps ease as far as one at 30");

    for (int i = 0; i < 3; i++) {
        fast[i] = start[i] * 10.0f - 7.0f;      // a different start: the fraction of the way is what must hold
    }
    float once[3], twice[3];
    memcpy(once, fast, sizeof once);
    caller_step(k, t, once);
    memcpy(twice, fast, sizeof twice);
    for (int n = 0; n < 2; n++) {
        swim_ease_step(s, k, 0.5f, dir, twice);
        caller_step(k, t, twice);
    }
    ok(same(once, twice, 1e-4f), "the identity holds from any velocity, not just from rest");

    float keep[3] = {1.0f, 2.0f, 3.0f}, copy[3];
    memcpy(copy, keep, sizeof copy);
    ok(swim_ease_step(s, k, 1.0f, dir, keep) == 0 && memcmp(keep, copy, sizeof keep) == 0,
       "at 30 fps the hook leaves the velocity alone");
    ok(swim_ease_step(s, k, 2.0f, dir, keep) == 0, "slower than 30 fps is left alone too");
    ok(swim_ease_step(s, 1.0f, 0.5f, dir, keep) == 0, "a factor of 1 asks for no correction");
    ok(swim_ease_step(s, 0.0f, 0.5f, dir, keep) == 0, "a factor of 0 asks for no correction");
    ok(swim_ease_step(s, k, 0.009f, dir, keep) == 0, "a ratio below the range the caller was written for is refused");

    if (failed)
        printf("\nswim_ease_test: %d failed\n", failed);
    else
        printf("\nswim_ease_test: all passed\n");
    return failed != 0;
}
