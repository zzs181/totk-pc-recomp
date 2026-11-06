// DLSS frame generation, the frame-side half: what it needs from NGX is in vk_ngx.c, the motion vectors and depth it
// interpolates between are vk_dlaa.c's, and the extra presents are gfx_present.c's. This file owns the setting, the
// capability check and (from the next stage on) the feature itself.
#include <stdio.h>
#include <string.h>

#ifndef _WIN32
#include <dirent.h>
#endif

#include "cfg.h"
#include "host.h"
#include "platform/platform.h"
#include "vk.h"
#include "vk_dlssg.h"
#include "vk_ngx.h"

static struct {
    int tried, wanted;   // wanted: -1 not read yet
    int available;       // the runtime reports frame generation on this device
} G = {.wanted = -1};

// The present path re-runs NGX's frame generation out of the scene's motion vectors, so the model must be where NGX
// looks for it: next to totk.exe (Windows) or next to totk (Linux). Without it an enabled dlss_fg would only fail at
// feature creation, once a frame is already in flight.
static int model_present(void) {
#ifdef _WIN32
    char exe[1024], path[1024 + 32];
    if (!plat_exe_path(exe, sizeof exe)) return 1;
    char *last = plat_last_sep(exe);
    if (!last) return 1;
    snprintf(path, sizeof path, "%.*s/%s", (int)(last - exe), exe, NGX_DLSSG_MODEL);
    return plat_file_exists(path);
#else
    char exe[1024];
    if (!plat_exe_path(exe, sizeof exe)) return 1;
    char *last = plat_last_sep(exe);
    if (!last) return 1;
    *last = 0;
    DIR *d = opendir(exe);
    if (!d) return 1;
    int found = 0;
    for (struct dirent *e; !found && (e = readdir(d));) found = !strncmp(e->d_name, "libnvidia-ngx-dlssg.so.", 24);
    closedir(d);
    return found;
#endif
}

int vk_dlssg_wanted(void) {
    if (G.wanted >= 0) return G.wanted;
    G.wanted = cfg_bool("TOTK_DLSS_FG") && model_present();
    if (cfg_bool("TOTK_DLSS_FG") && !G.wanted)
        rt_log("dlssg: dlss_fg = 1 but %s is not next to the executable", NGX_DLSSG_MODEL);
    return G.wanted;
}

static void probe_run(void);

void vk_dlssg_init(void) {
    if (G.tried) return;
    G.tried = 1;
    if (!vk_dlssg_wanted()) return;
    if (!vk_ngx_up()) {   // vk_dlaa_init: with FSR 3.1 in place of NGX, or a driver without a core
        rt_log("dlssg: NGX is not up: frame generation off");
        return;
    }
    NgxParam *caps = vk_ngx_caps();
    int avail = 0, driver = 0;
    if (caps) {
        // The capability names differ between DLSS SDK builds; ask both spellings and take either.
        if (!p_geti(caps, "FrameGeneration.Available", &avail)) p_geti(caps, "DLSSG.Available", &avail);
        if (!p_geti(caps, "FrameGeneration.NeedsUpdatedDriver", &driver)) p_geti(caps, "DLSSG.NeedsUpdatedDriver", &driver);
    }
    G.available = avail != 0;
    rt_log("dlssg: frame generation %s%s", avail ? "available" : "NOT available (is " NGX_DLSSG_MODEL " next to the executable?)",
           driver ? "; the driver needs an update" : "");
    if (rt_knob("DLSSG_PROBE", "0")) probe_run();
}

int vk_dlssg_available(void) { return G.available; }

// ---------------------------------------------------------------- feasibility probe (TOTK_DLSSG_PROBE=1)
// Whether NVIDIA's frame-generation model can be driven through NGX's Vulkan entry points without Streamline is the one
// question this stage cannot answer from the outside: Streamline is the documented path, and CreateFeature1's
// frame-generation feature is the undocumented one that OptiScaler and dlssg-to-fsr3 build on. The probe creates a
// 1280x720 feature out of throw-away images, evaluates it once and logs both return codes. Run the game once with
// TOTK_DLSSG_PROBE=1: create and evaluate both OK means the next stages can be built on this path; a create failure
// means the setting can only ever work with an unlocked runtime, and the present-side work keeps its own value.
typedef struct {
    VkImage img;
    VmaAllocation mem;
    VkImageView view;
} ProbeImg;

static int probe_img(ProbeImg *o, uint32_t w, uint32_t h, VkFormat f, VkImageUsageFlags usage, VkImageAspectFlags aspect) {
    const VkCtx *c = vk_ctx();
    VkImageCreateInfo ii = {.sType = VK_STRUCTURE_TYPE_IMAGE_CREATE_INFO, .imageType = VK_IMAGE_TYPE_2D, .format = f,
                            .extent = {w, h, 1}, .mipLevels = 1, .arrayLayers = 1, .samples = VK_SAMPLE_COUNT_1_BIT,
                            .tiling = VK_IMAGE_TILING_OPTIMAL, .usage = usage,
                            .initialLayout = VK_IMAGE_LAYOUT_UNDEFINED};
    VmaAllocationCreateInfo ai = {.usage = VMA_MEMORY_USAGE_AUTO_PREFER_DEVICE};
    if (vmaCreateImage(c->vma, &ii, &ai, &o->img, &o->mem, NULL) != VK_SUCCESS) return 0;
    VkImageViewCreateInfo vi = {.sType = VK_STRUCTURE_TYPE_IMAGE_VIEW_CREATE_INFO, .image = o->img,
                                .viewType = VK_IMAGE_VIEW_TYPE_2D, .format = f,
                                .subresourceRange = {aspect, 0, 1, 0, 1}};
    return vkCreateImageView(c->device, &vi, NULL, &o->view) == VK_SUCCESS;
}

static void probe_img_free(ProbeImg *o) {
    const VkCtx *c = vk_ctx();
    if (o->view) vkDestroyImageView(c->device, o->view, NULL);
    if (o->img) vmaDestroyImage(c->vma, o->img, o->mem);
    memset(o, 0, sizeof *o);
}

static NgxResourceVK probe_res(const ProbeImg *o, uint32_t w, uint32_t h, VkFormat f, VkImageAspectFlags aspect, int rw) {
    NgxResourceVK r;
    memset(&r, 0, sizeof r);
    r.r.img = (NgxImageView){o->view, o->img, {aspect, 0, 1, 0, 1}, f, w, h};
    r.type = 0;
    r.rw = rw;
    return r;
}

static void probe_run(void) {
    const VkCtx *c = vk_ctx();
    const uint32_t W = 1280, H = 720;
    const VkImageUsageFlags col = VK_IMAGE_USAGE_SAMPLED_BIT | VK_IMAGE_USAGE_COLOR_ATTACHMENT_BIT;
    ProbeImg bb = {0}, depth = {0}, mv = {0}, out[2] = {{0}};
    VkCommandPool pool = VK_NULL_HANDLE;
    VkCommandBuffer cb = VK_NULL_HANDLE;
    VkFence fence = VK_NULL_HANDLE;
    NgxParam *p = NULL;
    NgxHandle *feat = NULL;
    NgxResult cr = 0xBAD00001, ev = 0xBAD00001;
    int n_out = 0;
    rt_log("dlssg: probe %ux%u, creating the feature out of throw-away images", W, H);
    if (!probe_img(&bb, W, H, VK_FORMAT_R8G8B8A8_UNORM, col | VK_IMAGE_USAGE_TRANSFER_DST_BIT, VK_IMAGE_ASPECT_COLOR_BIT) ||
        !probe_img(&depth, W, H, VK_FORMAT_D32_SFLOAT,
                   VK_IMAGE_USAGE_SAMPLED_BIT | VK_IMAGE_USAGE_DEPTH_STENCIL_ATTACHMENT_BIT, VK_IMAGE_ASPECT_DEPTH_BIT) ||
        !probe_img(&mv, W, H, VK_FORMAT_R16G16_SFLOAT, col, VK_IMAGE_ASPECT_COLOR_BIT))
        goto out;
    for (n_out = 0; n_out < 2; n_out++)
        if (!probe_img(&out[n_out], W, H, VK_FORMAT_R8G8B8A8_UNORM,
                       VK_IMAGE_USAGE_SAMPLED_BIT | VK_IMAGE_USAGE_STORAGE_BIT, VK_IMAGE_ASPECT_COLOR_BIT)) {
            n_out = 0;
            goto out;
        }
    VkCommandPoolCreateInfo pi = {.sType = VK_STRUCTURE_TYPE_COMMAND_POOL_CREATE_INFO,
                                  .flags = VK_COMMAND_POOL_CREATE_TRANSIENT_BIT, .queueFamilyIndex = c->gfx_family};
    VkCommandBufferAllocateInfo ai = {.sType = VK_STRUCTURE_TYPE_COMMAND_BUFFER_ALLOCATE_INFO, .commandPool = pool,
                                      .level = VK_COMMAND_BUFFER_LEVEL_PRIMARY, .commandBufferCount = 1};
    VkCommandBufferBeginInfo bi = {.sType = VK_STRUCTURE_TYPE_COMMAND_BUFFER_BEGIN_INFO,
                                   .flags = VK_COMMAND_BUFFER_USAGE_ONE_TIME_SUBMIT_BIT};
    VkFenceCreateInfo fi = {.sType = VK_STRUCTURE_TYPE_FENCE_CREATE_INFO};
    if (vkCreateCommandPool(c->device, &pi, NULL, &pool) != VK_SUCCESS ||
        vkAllocateCommandBuffers(c->device, &ai, &cb) != VK_SUCCESS || vkCreateFence(c->device, &fi, NULL, &fence) != VK_SUCCESS ||
        vkBeginCommandBuffer(cb, &bi) != VK_SUCCESS)
        goto out;
    p = vk_ngx_alloc();
    if (!p) goto out;
    p_setu(p, "Width", W);
    p_setu(p, "Height", H);
    p_setu(p, "OutWidth", W);
    p_setu(p, "OutHeight", H);
    p_setu(p, "CreationNodeMask", 1);
    p_setu(p, "VisibilityNodeMask", 1);
    cr = vk_ngx_create1(cb, NGX_FEATURE_FRAMEGENERATION, p, &feat);
    rt_log("dlssg: probe CreateFeature1 -> %08x%s", (unsigned)cr, feat ? "" : " (no handle)");
    if (!NGX_OK(cr) || !feat) goto out;
    const VkImageAspectFlags ca = VK_IMAGE_ASPECT_COLOR_BIT;
    NgxResourceVK rbb = probe_res(&bb, W, H, VK_FORMAT_R8G8B8A8_UNORM, ca, 0);
    NgxResourceVK rd = probe_res(&depth, W, H, VK_FORMAT_D32_SFLOAT, VK_IMAGE_ASPECT_DEPTH_BIT, 0);
    NgxResourceVK rmv = probe_res(&mv, W, H, VK_FORMAT_R16G16_SFLOAT, ca, 0);
    NgxResourceVK ro[2] = {probe_res(&out[0], W, H, VK_FORMAT_R8G8B8A8_UNORM, ca, 1),
                           probe_res(&out[1], W, H, VK_FORMAT_R8G8B8A8_UNORM, ca, 1)};
    static const float ident[16] = {1, 0, 0, 0, 0, 1, 0, 0, 0, 0, 1, 0, 0, 0, 0, 1};
    static const float zero3[3] = {0, 0, 0};
    p_setp(p, "DLSSG.Backbuffer", &rbb);
    p_setp(p, "DLSSG.Depth", &rd);
    p_setp(p, "DLSSG.MVecs", &rmv);
    p_setp(p, "DLSSG.OutputReal", &ro[1]);            // one image that would be shown as-is (never read here)
    p_setp(p, "DLSSG.OutputInterpolated", &ro[0]);    // one frame between the two rendered ones
    p_setp(p, "DLSSG.ClipToPrevClip", (void *)ident);
    p_setp(p, "DLSSG.PrevClipToClip", (void *)ident);
    p_setf(p, "DLSSG.CameraNear", 0.1f);
    p_setf(p, "DLSSG.CameraFar", 1000.0f);
    p_setf(p, "DLSSG.CameraFOV", 1.0f);
    p_setf(p, "DLSSG.CameraAspectRatio", (float)W / (float)H);
    p_setp(p, "DLSSG.CameraPos", (void *)zero3);
    p_setp(p, "DLSSG.CameraUp", (void *)zero3);
    p_setp(p, "DLSSG.CameraRight", (void *)zero3);
    p_setp(p, "DLSSG.CameraFwd", (void *)zero3);
    p_seti(p, "DLSSG.MultiFrameCount", 2);
    p_seti(p, "DLSSG.MultiFrameIndex", 0);
    p_seti(p, "DLSSG.Reset", 1);
    p_seti(p, "DLSSG.ColorBuffersHDR", 0);
    p_seti(p, "DLSSG.DepthInverted", 0);
    p_setf(p, "DLSSG.MvecScaleX", 1.0f);
    p_setf(p, "DLSSG.MvecScaleY", 1.0f);
    ev = vk_ngx_evaluate(cb, feat, p);
    rt_log("dlssg: probe EvaluateFeature -> %08x", (unsigned)ev);
    vkEndCommandBuffer(cb);
    VkCommandBufferSubmitInfo cbi = {.sType = VK_STRUCTURE_TYPE_COMMAND_BUFFER_SUBMIT_INFO, .commandBuffer = cb};
    VkSubmitInfo2 si = {.sType = VK_STRUCTURE_TYPE_SUBMIT_INFO_2, .commandBufferInfoCount = 1, .pCommandBufferInfos = &cbi};
    if (vkQueueSubmit2(c->gfx_queue, 1, &si, fence) == VK_SUCCESS &&
        vkWaitForFences(c->device, 1, &fence, VK_TRUE, 5000000000ull) == VK_SUCCESS)
        rt_log("dlssg: probe submitted");
out:
    if (feat) vk_ngx_handle_release(feat);
    if (p) vk_ngx_free(p);
    if (fence) vkDestroyFence(c->device, fence, NULL);
    if (pool) vkDestroyCommandPool(c->device, pool, NULL);
    for (int i = 0; i < 2; i++) probe_img_free(&out[i]);
    probe_img_free(&mv);
    probe_img_free(&depth);
    probe_img_free(&bb);
    rt_log("dlssg: probe done: create %08x, evaluate %08x", (unsigned)cr, (unsigned)ev);
}

int vk_dlssg_frames(void) { return 0; }   // the present path learns the multiplier in the next stage

void vk_dlssg_release(void) { G.available = 0; }
