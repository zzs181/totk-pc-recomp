// The NGX core, brought up once for every feature that needs it (see vk_ngx.h for why the types are declared here).
#include <stdio.h>
#include <string.h>

#ifdef _WIN32
#include <windows.h>
#endif

#include "host.h"
#include "platform/platform.h"
#include "vk.h"
#include "vk_ngx.h"

typedef NgxResult (*PFN_Init)(unsigned long long app, const wchar_t *path, VkInstance, VkPhysicalDevice, VkDevice,
                              PFN_vkGetInstanceProcAddr, PFN_vkGetDeviceProcAddr, int version, const void *info);
typedef NgxResult (*PFN_ReqExt)(unsigned *ni, const char ***ie, unsigned *nd, const char ***de);
typedef NgxResult (*PFN_Params)(NgxParam **);
typedef NgxResult (*PFN_DestroyParams)(NgxParam *);
typedef NgxResult (*PFN_Create)(VkCommandBuffer, int feature, NgxParam *, NgxHandle **);
typedef NgxResult (*PFN_Create1)(VkDevice, VkCommandBuffer, int feature, NgxParam *, NgxHandle **);
typedef NgxResult (*PFN_Evaluate)(VkCommandBuffer, const NgxHandle *, const NgxParam *, void *callback);
typedef NgxResult (*PFN_Release)(NgxHandle *);

static struct {
    void *core;   // the NGX core library (an HMODULE on Windows)
    PFN_Init init;
    PFN_ReqExt req_ext;
    PFN_Params caps, alloc_params;
    PFN_DestroyParams destroy_params;
    PFN_Create create;
    PFN_Create1 create1;
    PFN_Evaluate evaluate;
    PFN_Release release;
    NgxParam *cap_params;   // the capability parameters, once vk_ngx_caps has asked for them
    int tried, loaded, inited;
    char path[PLAT_PATH_MAX + 32];
} G;

int vk_ngx_load(void) {
    if (G.tried) return G.loaded;
    G.tried = 1;
#ifdef _WIN32
    char dir[MAX_PATH] = {0};
    DWORD n = sizeof dir;
    if (RegGetValueA(HKEY_LOCAL_MACHINE, "SOFTWARE\\NVIDIA Corporation\\Global\\NGXCore", "FullPath", RRF_RT_REG_SZ,
                     NULL, dir, &n) != ERROR_SUCCESS) {
        rt_log("ngx: the driver has no NGX core (registry NGXCore missing)");
        return 0;
    }
    snprintf(G.path, sizeof G.path, "%s\\_nvngx.dll", dir);
    G.core = LoadLibraryA(G.path);
    if (!G.core) {
        rt_log("ngx: cannot load %s (error %lu)", G.path, GetLastError());
        return 0;
    }
#define FN(f, name) G.f = (void *)GetProcAddress((HMODULE)G.core, name)
#else
    // The driver's NGX core; the dynamic loader finds it where the driver put it (/usr/lib, /usr/lib/x86_64-linux-gnu).
    snprintf(G.path, sizeof G.path, "libnvidia-ngx.so.1");
    G.core = plat_lib_open(G.path);
    if (!G.core) {
        rt_log("ngx: cannot load %s (not an NVIDIA driver, or one without NGX)", G.path);
        return 0;
    }
#define FN(f, name) G.f = plat_lib_sym(G.core, name)
#endif
    FN(init, "NVSDK_NGX_VULKAN_Init_Ext2");
    FN(req_ext, "NVSDK_NGX_VULKAN_RequiredExtensions");
    FN(caps, "NVSDK_NGX_VULKAN_GetCapabilityParameters");
    FN(alloc_params, "NVSDK_NGX_VULKAN_AllocateParameters");
    FN(destroy_params, "NVSDK_NGX_VULKAN_DestroyParameters");
    FN(create, "NVSDK_NGX_VULKAN_CreateFeature");
    FN(create1, "NVSDK_NGX_VULKAN_CreateFeature1");   // optional: frame generation only
    FN(evaluate, "NVSDK_NGX_VULKAN_EvaluateFeature");
    FN(release, "NVSDK_NGX_VULKAN_ReleaseFeature");
#undef FN
    if (!G.init || !G.req_ext || !G.caps || !G.alloc_params || !G.create || !G.evaluate || !G.release) {
        rt_log("ngx: the NGX core %s lacks Vulkan entry points", G.path);
        return 0;
    }
    G.loaded = 1;
    rt_log("ngx: core %s", G.path);
    return 1;
}

int vk_ngx_check_params(void) {
    NgxParam *p = NULL;
    if (!NGX_OK(G.alloc_params(&p)) || !p) return 0;
    int i = 0;
    unsigned u = 0;
    float f = 0;
    void *v = NULL;
    p_seti(p, "TotK.Test.I", -12345);
    p_setu(p, "TotK.Test.U", 4000000000u);
    p_setf(p, "TotK.Test.F", 1.25f);
    p_setp(p, "TotK.Test.P", (void *)p);
    int ok = p_geti(p, "TotK.Test.I", &i) && i == -12345 && p_getu(p, "TotK.Test.U", &u) && u == 4000000000u &&
             p_getf(p, "TotK.Test.F", &f) && f == 1.25f && p_getp(p, "TotK.Test.P", &v) && v == (void *)p;
    if (G.destroy_params) G.destroy_params(p);
    if (!ok) rt_log("ngx: parameter layout check failed (i=%d u=%u f=%g p=%p)", i, u, f, v);
    return ok;
}

#ifndef _WIN32
// UTF-8 to UTF-32 wchar_t, without the C library's locale (the runtime never sets one, so mbstowcs would stop at the
// first non-ASCII byte). Returns 0 on malformed input or when out has no room.
static int utf8_to_wide(const char *in, wchar_t *out, size_t cap) {
    const unsigned char *u = (const unsigned char *)in;
    size_t n = 0;
    while (*u) {
        unsigned c = *u++, more = c < 0x80 ? 0 : c >= 0xF0 ? 3 : c >= 0xE0 ? 2 : c >= 0xC0 ? 1 : 4;
        if (more == 4) return 0;
        if (more) c &= 0x3F >> more;
        for (unsigned k = 0; k < more; k++, u++) {
            if ((*u & 0xC0) != 0x80) return 0;
            c = c << 6 | (*u & 0x3F);
        }
        if (n + 1 >= cap) return 0;
        out[n++] = (wchar_t)c;
    }
    out[n] = 0;
    return 1;
}
#endif

int vk_ngx_init(void) {
    if (G.inited) return 1;
    if (!vk_ngx_load()) return 0;
#ifdef _WIN32
    wchar_t wdir[MAX_PATH];
    DWORD len = GetModuleFileNameW(NULL, wdir, MAX_PATH);
    while (len && wdir[len - 1] != L'\\') len--;
    wdir[len] = 0;
#else
    // The executable's folder, where NGX looks for its model libraries and writes its logs, as a wchar_t string:
    // 32-bit code points here, as the Linux core expects.
    char dir[PLAT_PATH_MAX];
    wchar_t wdir[PLAT_PATH_MAX];
    char *cut = plat_exe_path(dir, sizeof dir) ? plat_last_sep(dir) : NULL;
    if (!cut || !utf8_to_wide((cut[1] = 0, dir), wdir, PLAT_PATH_MAX)) {
        rt_log("ngx: cannot name the executable's folder for NGX");
        return 0;
    }
#endif
    // 231313132: the application id NVIDIA's DLSS samples use for builds without an NVIDIA-issued id
    const VkCtx *c = vk_ctx();
    NgxResult r = G.init(231313132ull, wdir, c->instance, c->phys, c->device, vkGetInstanceProcAddr,
                         vkGetDeviceProcAddr, NGX_VERSION_API, NULL);
    if (!NGX_OK(r)) {
        rt_log("ngx: NGX init failed (%08x)", (unsigned)r);
        return 0;
    }
    if (!vk_ngx_check_params()) return 0;
    G.inited = 1;
    return 1;
}

int vk_ngx_up(void) { return G.inited; }

NgxParam *vk_ngx_caps(void) {
    if (G.cap_params) return G.cap_params;
    if (!G.inited) return NULL;
    NgxParam *p = NULL;
    if (NGX_OK(G.caps(&p)) && p) G.cap_params = p;
    return G.cap_params;
}

NgxParam *vk_ngx_alloc(void) {
    NgxParam *p = NULL;
    if (!G.loaded || !NGX_OK(G.alloc_params(&p))) return NULL;
    return p;
}

void vk_ngx_free(NgxParam *p) {
    if (p && G.destroy_params) G.destroy_params(p);
}

NgxResult vk_ngx_required_exts(unsigned *ni, const char ***ie, unsigned *nd, const char ***de) {
    if (!G.loaded) return 0xBAD00001;
    return G.req_ext(ni, ie, nd, de);
}

NgxResult vk_ngx_create(VkCommandBuffer cb, int feature, NgxParam *p, NgxHandle **out) {
    if (!G.loaded) return 0xBAD00001;
    return G.create(cb, feature, p, out);
}

NgxResult vk_ngx_create1(VkCommandBuffer cb, int feature, NgxParam *p, NgxHandle **out) {
    if (!G.loaded || !G.create1) return 0xBAD00001;
    return G.create1(vk_ctx()->device, cb, feature, p, out);
}

NgxResult vk_ngx_evaluate(VkCommandBuffer cb, const NgxHandle *h, const NgxParam *p) {
    if (!G.loaded) return 0xBAD00001;
    return G.evaluate(cb, h, p, NULL);
}

void vk_ngx_handle_release(NgxHandle *h) {
    if (h && G.loaded) G.release(h);
}
