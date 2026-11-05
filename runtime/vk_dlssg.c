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
}

int vk_dlssg_available(void) { return G.available; }

int vk_dlssg_frames(void) { return 0; }   // the present path learns the multiplier in the next stage

void vk_dlssg_release(void) { G.available = 0; }
