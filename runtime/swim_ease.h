// The per-call fix for one swim frame, without the game: runtime/swim_ease.c calls it with the caller's registers, and
// tests/swim_ease_test.c drives it directly.
//
// The game eases the velocity toward the target once per frame with a fixed factor k and no dt: v += (t - v) * k. That
// is a step of (1 - k) of the remaining distance per frame, and a frame at a higher rate is a shorter step in game
// time. To keep the 30 Hz ease, the hook moves v first so that the caller's own step lands on t + (v - t) * (1-k)^r:
//   v' = t + (v - t) * keep,  keep = (1-k)^r / (1-k) = (1-k)^(r-1)
// where r is the frame's 30 Hz ratio. Two calls at r = 1/2 then ease exactly as far as one call at r = 1.
//
// Returns 0 where the game's own step is right as it stands: r >= 1 (30 fps and below), or a factor or rate outside
// the range the caller was written for.
#pragma once
#include <math.h>

static inline int swim_ease_step(float s, float k, float r, const float dir[3], float v[3]) {
    if (!(r > 0.01f && r < 1.0f) || !(k > 0.0f && k < 1.0f)) return 0;
    const double keep = pow(1.0 - (double)k, (double)r) / (1.0 - (double)k);   // the caller's step x (1-k)
    for (int i = 0; i < 3; i++) {
        const double t = (double)(dir[i] * s);   // (the caller's float product)
        v[i] = (float)(t + ((double)v[i] - t) * keep);
    }
    return 1;
}
