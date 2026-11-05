// The NGX core's Vulkan entry points, shared by everything that runs an NVIDIA feature: DLSS super resolution
// (vk_dlaa.c) and DLSS frame generation (vk_dlssg.c). NGX is loaded at run time from the driver's core: on Windows
// _nvngx.dll, found through HKLM\SOFTWARE\NVIDIA Corporation\Global\NGXCore; on Linux libnvidia-ngx.so.1, which the
// NVIDIA driver installs on the library path. No NVIDIA SDK code or header is compiled in: the few types used are
// declared here from the SDK's documented ABI, the way the Metroid Dread recomp's dlss.c reaches the core (read for
// behaviour only).
//
// The core's parameter object is a C++ class whose virtual functions are called through its vtable: the Set overloads
// in slots 0-7 and the Get overloads in 8-15. MSVC (the Windows core) lays out each group of overloads in reverse
// declaration order, the Itanium C++ ABI (the Linux core, built with GCC) in declaration order. vk_ngx_check_params
// checks the layout with a set-then-get round trip before anything relies on it.
#pragma once

#include <stdint.h>
#include <wchar.h>

#include "vk.h"

typedef int NgxResult;   // NVSDK_NGX_Result: 1 success, 0xBAD00000 | n failures
#define NGX_OK(r) (((unsigned)(r) & 0xFFF00000u) != 0xBAD00000u)
#define NGX_VERSION_API 0x15

// NVSDK_NGX_Feature
#define NGX_FEATURE_SUPERSAMPLING 1
#define NGX_FEATURE_FRAMEGENERATION 12

// The feature model libraries NGX loads from the executable's folder (the core resolves them itself; the runtime only
// checks that they are there, to say so in a log line instead of failing later).
#ifdef _WIN32
#define NGX_DLSS_MODEL "nvngx_dlss.dll"
#define NGX_DLSSG_MODEL "nvngx_dlssg.dll"
#else
#define NGX_DLSS_MODEL "libnvidia-ngx-dlss.so.<version>"
#define NGX_DLSSG_MODEL "libnvidia-ngx-dlssg.so.<version>"
#endif

typedef struct NgxParam NgxParam;
typedef struct NgxHandle NgxHandle;
typedef struct { void *slot[17]; } NgxParamVtbl;
struct NgxParam { const NgxParamVtbl *vt; };
typedef struct {   // NVSDK_NGX_ImageViewInfo_VK
    VkImageView view;
    VkImage image;
    VkImageSubresourceRange range;
    VkFormat format;
    unsigned width, height;
} NgxImageView;
typedef struct {   // NVSDK_NGX_Resource_VK
    union {
        NgxImageView img;
        struct { VkBuffer buffer; unsigned bytes; } buf;
    } r;
    int type;      // 0 image view, 1 buffer
    _Bool rw;
} NgxResourceVK;

typedef void (*NgxSetI)(NgxParam *, const char *, int);
typedef void (*NgxSetU)(NgxParam *, const char *, unsigned);
typedef void (*NgxSetF)(NgxParam *, const char *, float);
typedef void (*NgxSetP)(NgxParam *, const char *, void *);
typedef NgxResult (*NgxGetI)(const NgxParam *, const char *, int *);
typedef NgxResult (*NgxGetU)(const NgxParam *, const char *, unsigned *);
typedef NgxResult (*NgxGetF)(const NgxParam *, const char *, float *);
typedef NgxResult (*NgxGetP)(const NgxParam *, const char *, void **);
// The SDK declares Set(u64), Set(float), Set(double), Set(u32), Set(int), Set(ID3D11Resource *), Set(ID3D12Resource *),
// Set(void *), then the Get overloads in the same order, then Reset.
#ifdef _WIN32
enum { S_P = 0, S_D12, S_D11, S_I, S_U32, S_D, S_F, S_U64, G_P, G_D12, G_D11, G_I, G_U32, G_D, G_F, G_U64 };
#else
enum { S_U64 = 0, S_F, S_D, S_U32, S_I, S_D11, S_D12, S_P, G_U64, G_F, G_D, G_U32, G_I, G_D11, G_D12, G_P };
#endif
static inline void p_seti(NgxParam *p, const char *n, int v) { ((NgxSetI)p->vt->slot[S_I])(p, n, v); }
static inline void p_setu(NgxParam *p, const char *n, unsigned v) { ((NgxSetU)p->vt->slot[S_U32])(p, n, v); }
static inline void p_setf(NgxParam *p, const char *n, float v) { ((NgxSetF)p->vt->slot[S_F])(p, n, v); }
static inline void p_setp(NgxParam *p, const char *n, void *v) { ((NgxSetP)p->vt->slot[S_P])(p, n, v); }
static inline int p_geti(const NgxParam *p, const char *n, int *v) {
    return NGX_OK(((NgxGetI)p->vt->slot[G_I])(p, n, v));
}
static inline int p_getu(const NgxParam *p, const char *n, unsigned *v) {
    return NGX_OK(((NgxGetU)p->vt->slot[G_U32])(p, n, v));
}
static inline int p_getf(const NgxParam *p, const char *n, float *v) {
    return NGX_OK(((NgxGetF)p->vt->slot[G_F])(p, n, v));
}
static inline int p_getp(const NgxParam *p, const char *n, void **v) {
    return NGX_OK(((NgxGetP)p->vt->slot[G_P])(p, n, v));
}

// Loads the driver's core and resolves its Vulkan entry points. Idempotent; 1 with the core loaded.
int vk_ngx_load(void);
// NVSDK_NGX_VULKAN_Init_Ext2 with this executable's folder (where NGX finds its model libraries and writes its logs),
// then the parameter-layout check. Idempotent; 1 when NGX is up.
int vk_ngx_init(void);
int vk_ngx_check_params(void);
NgxParam *vk_ngx_caps(void);   // the capability parameters, NULL until vk_ngx_init succeeds
NgxParam *vk_ngx_alloc(void);  // a fresh parameter object (the caller keeps and fills it)
void vk_ngx_free(NgxParam *p);
// NVSDK_NGX_VULKAN_RequiredExtensions, passed through as the core returns it.
NgxResult vk_ngx_required_exts(unsigned *ni, const char ***ie, unsigned *nd, const char ***de);
NgxResult vk_ngx_create(VkCommandBuffer cb, int feature, NgxParam *p, NgxHandle **out);
// The frame-generation feature is created through CreateFeature1, which also takes the device; 0 when the core has no
// such entry point (frame generation is unavailable, DLSS is not).
NgxResult vk_ngx_create1(VkCommandBuffer cb, int feature, NgxParam *p, NgxHandle **out);
NgxResult vk_ngx_evaluate(VkCommandBuffer cb, const NgxHandle *h, const NgxParam *p);
void vk_ngx_handle_release(NgxHandle *h);
int vk_ngx_up(void);   // the core is loaded and initialised
