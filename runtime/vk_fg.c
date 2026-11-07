// Frame generation's lasting state (see vk_fg.h): a small ring of the last rendered pictures with their cameras.
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#include "cfg.h"
#include "gfx.h"
#include "nvn_api.h"
#include "host.h"
#include "platform/platform.h"
#include "vk.h"
#include "vk_fg.h"

static struct {
    int wanted;   // -1 not read yet
    VkFgSlot slot[VK_FG_SLOTS];
    float cam_vp[16];
    float cam_jitter[2];
    int cam_ok;
    PlatMutex lock;   // camera written by the render thread, read by the present thread
} F = {.wanted = -1, .lock = PLAT_MUTEX_INIT};

int vk_fg_wanted(void) {
    if (F.wanted >= 0) return F.wanted;
    const char *e = getenv("TOTK_FG");
    F.wanted = e && *e && strcmp(e, "0") != 0;
    if (F.wanted) rt_log("fg: frame generation on (TOTK_FG), capture ring only for now");
    return F.wanted;
}

static void slot_free(VkFgSlot *s) {
    const VkCtx *c = vk_ctx();
    if (s->view) vkDestroyImageView(c->device, s->view, NULL);
    if (s->image) vmaDestroyImage(c->vma, s->image, s->mem);
    memset(s, 0, sizeof *s);
}

static int slot_fit(VkFgSlot *s, uint32_t w, uint32_t h, VkFormat f) {
    if (s->image && s->w == w && s->h == h && s->format == f) return 1;
    slot_free(s);
    const VkCtx *c = vk_ctx();
    VkImageCreateInfo ii = {.sType = VK_STRUCTURE_TYPE_IMAGE_CREATE_INFO, .imageType = VK_IMAGE_TYPE_2D, .format = f,
                            .extent = {w, h, 1}, .mipLevels = 1, .arrayLayers = 1, .samples = VK_SAMPLE_COUNT_1_BIT,
                            .tiling = VK_IMAGE_TILING_OPTIMAL,
                            .usage = VK_IMAGE_USAGE_TRANSFER_DST_BIT | VK_IMAGE_USAGE_SAMPLED_BIT,
                            .initialLayout = VK_IMAGE_LAYOUT_UNDEFINED};
    VmaAllocationCreateInfo ai = {.usage = VMA_MEMORY_USAGE_AUTO_PREFER_DEVICE};
    if (vmaCreateImage(c->vma, &ii, &ai, &s->image, &s->mem, NULL) != VK_SUCCESS) return 0;
    VkImageViewCreateInfo vi = {.sType = VK_STRUCTURE_TYPE_IMAGE_VIEW_CREATE_INFO, .image = s->image,
                                .viewType = VK_IMAGE_VIEW_TYPE_2D, .format = f,
                                .subresourceRange = {VK_IMAGE_ASPECT_COLOR_BIT, 0, 1, 0, 1}};
    if (vkCreateImageView(c->device, &vi, NULL, &s->view) != VK_SUCCESS) {
        slot_free(s);
        return 0;
    }
    s->w = w, s->h = h, s->format = f;
    return 1;
}

static void barrier(VkCommandBuffer cb, VkImage img, VkImageLayout from, VkImageLayout to,
                    VkPipelineStageFlags2 src_stage, VkAccessFlags2 src_access, VkPipelineStageFlags2 dst_stage,
                    VkAccessFlags2 dst_access) {
    VkImageMemoryBarrier2 b = {.sType = VK_STRUCTURE_TYPE_IMAGE_MEMORY_BARRIER_2,
                               .srcStageMask = src_stage, .srcAccessMask = src_access,
                               .dstStageMask = dst_stage, .dstAccessMask = dst_access,
                               .oldLayout = from, .newLayout = to,
                               .image = img, .subresourceRange = {VK_IMAGE_ASPECT_COLOR_BIT, 0, 1, 0, 1}};
    VkDependencyInfo d = {.sType = VK_STRUCTURE_TYPE_DEPENDENCY_INFO, .imageMemoryBarrierCount = 1,
                          .pImageMemoryBarriers = &b};
    vkCmdPipelineBarrier2(cb, &d);
}

void vk_fg_camera(const float vp[16], float jx, float jy) {
    if (!vk_fg_wanted()) return;
    plat_mutex_lock(&F.lock);
    memcpy(F.cam_vp, vp, sizeof F.cam_vp);
    F.cam_jitter[0] = jx, F.cam_jitter[1] = jy;
    F.cam_ok = 1;
    plat_mutex_unlock(&F.lock);
}

void vk_fg_capture(VkCommandBuffer cb, VkImage src, VkFormat fmt, int w, int h) {
    if (!vk_fg_wanted() || !src || w <= 0 || h <= 0) return;
    uint64_t frame = __atomic_load_n(&gfx_counters.frames, __ATOMIC_RELAXED);
    VkFgSlot *s = &F.slot[frame % VK_FG_SLOTS];
    if (!slot_fit(s, (uint32_t)w, (uint32_t)h, fmt)) return;
    barrier(cb, src, VK_IMAGE_LAYOUT_GENERAL, VK_IMAGE_LAYOUT_TRANSFER_SRC_OPTIMAL,
            VK_PIPELINE_STAGE_2_ALL_COMMANDS_BIT, VK_ACCESS_2_MEMORY_WRITE_BIT, VK_PIPELINE_STAGE_2_TRANSFER_BIT,
            VK_ACCESS_2_TRANSFER_READ_BIT);
    VkImageCopy copy = {.srcSubresource = {VK_IMAGE_ASPECT_COLOR_BIT, 0, 0, 1},
                        .dstSubresource = {VK_IMAGE_ASPECT_COLOR_BIT, 0, 0, 1},
                        .extent = {(uint32_t)w, (uint32_t)h, 1}};
    vkCmdCopyImage(cb, src, VK_IMAGE_LAYOUT_TRANSFER_SRC_OPTIMAL, s->image, VK_IMAGE_LAYOUT_TRANSFER_DST_OPTIMAL, 1, &copy);
    barrier(cb, src, VK_IMAGE_LAYOUT_TRANSFER_SRC_OPTIMAL, VK_IMAGE_LAYOUT_GENERAL, VK_PIPELINE_STAGE_2_TRANSFER_BIT,
            VK_ACCESS_2_TRANSFER_READ_BIT, VK_PIPELINE_STAGE_2_ALL_COMMANDS_BIT, 0);
    barrier(cb, s->image, VK_IMAGE_LAYOUT_TRANSFER_DST_OPTIMAL, VK_IMAGE_LAYOUT_GENERAL,
            VK_PIPELINE_STAGE_2_TRANSFER_BIT, VK_ACCESS_2_TRANSFER_WRITE_BIT, VK_PIPELINE_STAGE_2_ALL_COMMANDS_BIT,
            VK_ACCESS_2_MEMORY_READ_BIT);
    plat_mutex_lock(&F.lock);
    memcpy(s->vp, F.cam_vp, sizeof s->vp);
    s->jitter[0] = F.cam_jitter[0], s->jitter[1] = F.cam_jitter[1];
    int have_cam = F.cam_ok;
    plat_mutex_unlock(&F.lock);
    s->frame = frame;
    s->valid = have_cam;   // a picture without the camera of its frame cannot be reprojected
}

const VkFgSlot *vk_fg_slot(uint64_t frame) {
    for (int i = 0; i < VK_FG_SLOTS; i++)
        if (F.slot[i].valid && F.slot[i].frame == frame) return &F.slot[i];
    return NULL;
}

void vk_fg_shutdown(void) {
    for (int i = 0; i < VK_FG_SLOTS; i++) slot_free(&F.slot[i]);
    F.cam_ok = 0;
}
