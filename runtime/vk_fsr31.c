// AMD FidelityFX Super Resolution 3.1 (spatial-temporal upscale) on any Vulkan GPU, through the signed runtime
// amd_fidelityfx_vk.dll (FidelityFX SDK v1.1.4, MIT: runtime/third_party/fidelityfx). The DLL is looked up beside
// totk.exe, then in TOTK_FFX_DIR. Nothing of AMD's is compiled in: the C API headers are in the tree, the entry points
// come from the DLL (ffx_api_loader.h's five functions).
//
// This is the FFX half of the scene pass in vk_dlaa.c: the same color, depth, motion vectors and bias mask that the
// DLSS path hands NGX, at the scene's size; the picture goes to an RGBA16F output at the screen's size and is blitted
// into the scene target by the caller, exactly where DLSS's output goes. Enabling it takes the place of NGX (fsr31 = 1,
// see vk_dlaa_wanted), so an NVIDIA card can run either.
//
// The backend needs the device's core commands by name (its KHR/EXT lookups and its debug labels fail on a 1.3 device
// that enables neither): vk_fsr31_device_proc answers with the KHR name's core command, a no-op for the labels and
// markers, and a log line the first times. Written after Weeping of the Empire's nvn/vulkan/ffx.c (MIT), same API.
#include "vk.h"
#include "vk_fsr31.h"

#include <math.h>
#include <stdbool.h>
#include <stdlib.h>
#include <string.h>

#include "cfg.h"
#include "host.h"
#include "platform/platform.h"

#if defined(_WIN32)
#include <windows.h>
#else
#include <dlfcn.h>
#endif

// The C API headers name Vulkan structs C++-style (no tag): typedefs for C, headers untouched.
typedef struct FfxApiResourceDescription FfxApiResourceDescription;
typedef struct FfxApiResource FfxApiResource;
typedef struct VkQueueInfoFFXAPI VkQueueInfoFFXAPI;
#include "third_party/fidelityfx/include/ffx_api.h"
#include "third_party/fidelityfx/include/ffx_upscale.h"
#include "third_party/fidelityfx/include/vk/ffx_api_vk.h"

static struct {
    int state;   // -1 not tried (the initializer below), 0 unavailable, 1 loaded
    void *lib;
    PfnFfxCreateContext create;
    PfnFfxDestroyContext destroy;
    PfnFfxDispatch dispatch;
    int fails;
} F = {.state = -1};

// ---- loading

static void *lib_open_dir(const char *dir) {
#if defined(_WIN32)
    char p[1024];
    snprintf(p, sizeof p, "%s\\amd_fidelityfx_vk.dll", dir);
    return (void *)LoadLibraryExA(p, NULL, LOAD_WITH_ALTERED_SEARCH_PATH);
#else
    char p[1024];
    snprintf(p, sizeof p, "%s/libamd_fidelityfx_vk.so", dir);
    return dlopen(p, RTLD_NOW | RTLD_LOCAL);
#endif
}

static void *sym(void *lib, const char *name) {
#if defined(_WIN32)
    return (void *)GetProcAddress((HMODULE)lib, name);
#else
    return dlsym(lib, name);
#endif
}

static int ffx_load(void) {
    if (F.state >= 0) return F.state == 1;
    F.state = 0;
    char dir[900] = {0};
#if defined(_WIN32)
    char exe[MAX_PATH];
    DWORD n = GetModuleFileNameA(NULL, exe, MAX_PATH);
    while (n && exe[n - 1] != '\\' && exe[n - 1] != '/') n--;
    if (n) exe[n - 1] = 0;
    F.lib = lib_open_dir(exe);
    if (!F.lib) {
        const char *env = cfg_get("TOTK_FFX_DIR");
        if (env && *env) F.lib = lib_open_dir(env);
    }
#endif
    if (!F.lib) {
        rt_log("fsr31: no amd_fidelityfx_vk.dll beside totk.exe or in TOTK_FFX_DIR");
        return 0;
    }
    F.create = (PfnFfxCreateContext)sym(F.lib, "ffxCreateContext");
    F.destroy = (PfnFfxDestroyContext)sym(F.lib, "ffxDestroyContext");
    F.dispatch = (PfnFfxDispatch)sym(F.lib, "ffxDispatch");
    if (!F.create || !F.destroy || !F.dispatch) {
        rt_log("fsr31: %s lacks ffxCreateContext/ffxDestroyContext/ffxDispatch", "amd_fidelityfx_vk.dll");
        return 0;
    }
    F.state = 1;
    rt_log("fsr31: FidelityFX runtime loaded");
    (void)dir;
    return 1;
}

int vk_fsr31_available(void) { return ffx_load(); }

// ---- the backend's device commands

// Diagnostics only: the labels and markers the SDK emits when its own debug support is on.
static VkResult VKAPI_CALL no_label(VkDevice d, const VkDebugUtilsObjectNameInfoEXT *i) { (void)d, (void)i; return VK_SUCCESS; }
static void VKAPI_CALL no_begin(VkCommandBuffer cb, const VkDebugUtilsLabelEXT *l) { (void)cb, (void)l; }
static void VKAPI_CALL no_end(VkCommandBuffer cb) { (void)cb; }
static void VKAPI_CALL no_marker(VkCommandBuffer cb, VkPipelineStageFlagBits st, VkBuffer b, VkDeviceSize o, uint32_t m) {
    (void)cb, (void)st, (void)b, (void)o, (void)m;
}
static void VKAPI_CALL no_marker2(VkCommandBuffer cb, VkPipelineStageFlags2 st, VkBuffer b, VkDeviceSize o, uint32_t m) {
    (void)cb, (void)st, (void)b, (void)o, (void)m;
}

static PFN_vkVoidFunction VKAPI_CALL dev_proc(VkDevice dev, const char *name) {
    static int nl;
    int log = nl < 24;
    PFN_vkVoidFunction f = vkGetDeviceProcAddr(dev, name);
    if (log && nl++ < 24) rt_log("fsr31: dev_proc %s -> %s", name ? name : "(null)", f ? "ok" : "NULL");
    if (f || !name) return f;
    size_t n = strlen(name);
    if (n > 3 && n < 120 && (!strcmp(name + n - 3, "KHR") || !strcmp(name + n - 3, "EXT"))) {
        char core[120];
        memcpy(core, name, n - 3);
        core[n - 3] = 0;
        if ((f = vkGetDeviceProcAddr(dev, core))) return f;
    }
    static const struct { const char *n; PFN_vkVoidFunction f; } noop[] = {
        {"vkSetDebugUtilsObjectNameEXT", (PFN_vkVoidFunction)no_label},
        {"vkCmdBeginDebugUtilsLabelEXT", (PFN_vkVoidFunction)no_begin},
        {"vkCmdEndDebugUtilsLabelEXT", (PFN_vkVoidFunction)no_end},
        {"vkCmdInsertDebugUtilsLabelEXT", (PFN_vkVoidFunction)no_begin},
        {"vkCmdWriteBufferMarkerAMD", (PFN_vkVoidFunction)no_marker},
        {"vkCmdWriteBufferMarker2AMD", (PFN_vkVoidFunction)no_marker2}};
    for (size_t i = 0; i < sizeof noop / sizeof *noop; i++)
        if (!strcmp(name, noop[i].n)) return noop[i].f;
    static int logged;
    if (logged++ < 8) rt_log("fsr31: the SDK asked for %s: not available, NULL", name);
    return NULL;
}

static void backend_desc(struct ffxCreateBackendVKDesc *be) {
    memset(be, 0, sizeof *be);
    be->header.type = FFX_API_CREATE_CONTEXT_DESC_TYPE_BACKEND_VK;
    be->vkDevice = vk_ctx()->device;
    be->vkPhysicalDevice = vk_ctx()->phys;
    be->vkDeviceProcAddr = dev_proc;
}

// ---- resources

static FfxApiResource res_of(VkImage image, VkFormat fmt, uint32_t w, uint32_t h, uint32_t usage) {
    FfxApiResource r;
    memset(&r, 0, sizeof r);
    r.resource = (void *)image;
    r.state = FFX_API_RESOURCE_STATE_UNORDERED_ACCESS;   // GENERAL in the SDK's Vulkan backend, and left so
    r.description.type = FFX_API_RESOURCE_TYPE_TEXTURE2D;
    r.description.format = ffxApiGetSurfaceFormatVK(fmt);
    r.description.width = w;
    r.description.height = h;
    r.description.depth = 1;
    r.description.mipCount = 1;
    r.description.usage = usage | (ffxApiIsDepthFormat(fmt) ? FFX_API_RESOURCE_USAGE_DEPTHTARGET : 0);
    return r;
}

// FFX reports warnings and errors through a callback; a NULL one is called unchecked on a warning (crash at 0).
static void ffx_msg(uint32_t type, const wchar_t *msg) {
    rt_log("fsr31: %s: %ls", type == FFX_API_MESSAGE_TYPE_ERROR ? "error" : "warning", msg ? msg : L"");
}

// ---- the upscale context

static struct {
    ffxContext ctx;
    uint32_t rw, rh, ow, oh, flags;
    uint64_t last_ns;
} S;

static void sr_release(void) {
    if (!S.ctx) return;
    F.destroy(&S.ctx, NULL);
    S.ctx = NULL;
}

static int sr_create(uint32_t rw, uint32_t rh, uint32_t ow, uint32_t oh, uint32_t flags) {
    sr_release();
    struct ffxCreateBackendVKDesc be;
    backend_desc(&be);
    struct ffxCreateContextDescUpscale cd;
    memset(&cd, 0, sizeof cd);
    cd.header.type = FFX_API_CREATE_CONTEXT_DESC_TYPE_UPSCALE;
    cd.header.pNext = &be.header;
    cd.flags = flags;
    cd.fpMessage = ffx_msg;
    cd.maxRenderSize.width = rw;
    cd.maxRenderSize.height = rh;
    cd.maxUpscaleSize.width = ow;
    cd.maxUpscaleSize.height = oh;
    uint64_t t0 = plat_now_ns();
    rt_log("fsr31: creating context %ux%u -> %ux%u (flags 0x%x, device %p)", rw, rh, ow, oh, (unsigned)flags,
           (void *)vk_ctx()->device);
    ffxReturnCode_t r = F.create(&S.ctx, &cd.header, NULL);
    if (r != FFX_API_RETURN_OK || !S.ctx) {
        S.ctx = NULL;
        F.fails++;
        rt_log("fsr31: upscale context %ux%u -> %ux%u failed (%u)", rw, rh, ow, oh, (unsigned)r);
        return 0;
    }
    S.rw = rw, S.rh = rh, S.ow = ow, S.oh = oh, S.flags = flags, S.last_ns = 0;
    rt_log("fsr31: FSR 3.1 context %ux%u -> %ux%u (flags 0x%x) in %.1f ms", rw, rh, ow, oh, (unsigned)flags,
           (plat_now_ns() - t0) / 1e6);
    return 1;
}

int vk_fsr31_wanted(void) {
    static int on = -1;
    static unsigned seen;
    unsigned ver = cfg_version();
    if (on < 0 || ver != seen) {
        seen = ver;
        on = cfg_bool("TOTK_FSR31") && ffx_load();
    }
    return on;
}

int vk_fsr31_evaluate(VkCommandBuffer cb, VkImage color, VkFormat cfmt, VkImage depth, VkFormat zfmt, VkImage mv,
                      VkImage out, uint32_t w, uint32_t h, uint32_t ow, uint32_t oh, VkImage bias, float jx, float jy,
                      int reset) {
    if (!vk_fsr31_wanted() || F.fails > 3) return 0;
    uint32_t flags = FFX_UPSCALE_ENABLE_AUTO_EXPOSURE | FFX_UPSCALE_ENABLE_DYNAMIC_RESOLUTION |
                     FFX_UPSCALE_ENABLE_HIGH_DYNAMIC_RANGE;
    if (!S.ctx || S.ow != ow || S.oh != oh || S.flags != flags || w > S.rw || h > S.rh) {
        uint32_t rw = (S.ctx && S.ow == ow && S.oh == oh && S.rw > w) ? S.rw : w;
        uint32_t rh = (S.ctx && S.ow == ow && S.oh == oh && S.rh > h) ? S.rh : h;
        if (!sr_create(rw, rh, ow, oh, flags)) return 0;
        reset = 1;
    }
    struct ffxDispatchDescUpscale d;
    memset(&d, 0, sizeof d);
    d.header.type = FFX_API_DISPATCH_DESC_TYPE_UPSCALE;
    d.commandList = (void *)cb;
    d.color = res_of(color, cfmt, w, h, FFX_API_RESOURCE_USAGE_READ_ONLY);
    d.depth = res_of(depth, zfmt, w, h, FFX_API_RESOURCE_USAGE_READ_ONLY);
    d.motionVectors = res_of(mv, VK_FORMAT_R16G16_SFLOAT, w, h, FFX_API_RESOURCE_USAGE_READ_ONLY);
    d.reactive = bias ? res_of(bias, VK_FORMAT_R8_UNORM, w, h, FFX_API_RESOURCE_USAGE_READ_ONLY) : (FfxApiResource){0};
    d.output = res_of(out, VK_FORMAT_R16G16B16A16_SFLOAT, ow, oh, FFX_API_RESOURCE_USAGE_UAV);
    d.jitterOffset.x = jx;
    d.jitterOffset.y = jy;
    d.motionVectorScale.x = 1.0f;   // render pixels, as DLSS gets them
    d.motionVectorScale.y = 1.0f;
    d.renderSize.width = w;
    d.renderSize.height = h;
    d.upscaleSize.width = ow;
    d.upscaleSize.height = oh;
    d.enableSharpening = false;
    uint64_t now = plat_now_ns();
    d.frameTimeDelta = S.last_ns && !reset ? (float)((now - S.last_ns) / 1e6) : 16.67f;
    S.last_ns = now;
    d.preExposure = 1.0f;
    d.reset = reset ? 1 : 0;
    d.cameraNear = 0.1f;
    d.cameraFar = 10000.0f;
    d.cameraFovAngleVertical = 60.0f * 3.14159265f / 180.0f;
    ffxReturnCode_t r = F.dispatch(&S.ctx, &d.header);
    if (r != FFX_API_RETURN_OK) {
        if (F.fails++ < 4) rt_log("fsr31: dispatch failed (%u, %ux%u -> %ux%u)", (unsigned)r, w, h, ow, oh);
        return 0;
    }
    static int once;
    if (!once) once = 1, rt_log("fsr31: 1 frame upscaled (%ux%u -> %ux%u)", w, h, ow, oh);
    return 1;
}

void vk_fsr31_shutdown(void) { sr_release(); }
