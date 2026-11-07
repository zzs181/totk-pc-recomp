// Frame generation (TOTK_FG=1): our own motion-compensated interpolation between two rendered frames. The present
// path shows a generated frame before the rendered one, so this module owns the pieces that outlive a frame:
// the last rendered pictures (copies: a presented texture may be recycled as soon as the submission retires), the
// camera of those frames, and later the interpolation pass itself. Nothing runs while the setting is off.
#pragma once

#include <stdint.h>

#include "vk.h"

#define VK_FG_SLOTS 3

typedef struct VkFgSlot {
    VkImage image;
    VmaAllocation mem;
    VkImageView view;
    VkFormat format;
    uint32_t w, h;
    uint64_t frame;        // gfx_counters.frames when it was captured
    float vp[16];          // the frame's view-projection (row major), the reference for the next frame's reprojection
    float jitter[2];
    int valid;
} VkFgSlot;

int vk_fg_wanted(void);   // TOTK_FG=1 (read at start)
// Records a copy of the presented picture into the ring (the present's command buffer, before its blit). A slot is
// only reused after VK_FG_SLOTS frames, by which time the submission that wrote it has long retired.
void vk_fg_capture(VkCommandBuffer cb, VkImage src, VkFormat fmt, int w, int h);
// The camera of the frame being rendered, for the capture of the same frame (vk_dlaa.c has the view-projection and
// the jitter). Called by the render thread before the present.
void vk_fg_camera(const float vp[16], float jx, float jy);
// The slot captured for that frame, or NULL (frame generation off, or the frame was not captured). The caller must
// not free it; the ring owns it.
const VkFgSlot *vk_fg_slot(uint64_t frame);
void vk_fg_shutdown(void);
