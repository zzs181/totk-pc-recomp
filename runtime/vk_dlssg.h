// DLSS frame generation (vk_dlssg.c): 1..5 interpolated frames between two rendered ones, behind totk.ini dlss_fg
// (TOTK_DLSS_FG). The feature is NVIDIA's, on top of NGX (vk_ngx.c) and the frame's motion vectors and depth
// (vk_dlaa.c); the present path runs the extra presents (gfx_present.c).
#pragma once

#include <stdint.h>

// The setting is on and the frame-generation model is next to the executable. Read at device creation (the NGX
// extensions must be asked for then), so a change needs a restart.
int vk_dlssg_wanted(void);
// After the device exists: asks NGX's capability parameters whether frame generation can run here, and logs it.
void vk_dlssg_init(void);
// 1 when NGX came up and the runtime reports frame generation available.
int vk_dlssg_available(void);
// Presents per rendered frame the frame generator should produce (0: frame generation off this frame).
int vk_dlssg_frames(void);
void vk_dlssg_release(void);
