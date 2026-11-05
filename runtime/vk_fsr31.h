// AMD FidelityFX Super Resolution 3.1 on the scene pass (vk_fsr31.c). When fsr31 = 1 it takes the place of NGX: the
// same color, depth, motion vectors and bias mask, upscaled into an RGBA16F image at the screen's size.
#pragma once
#include "vk.h"

int vk_fsr31_available(void);   // the DLL beside totk.exe or in TOTK_FFX_DIR loaded
int vk_fsr31_wanted(void);      // fsr31 = 1 and the DLL loaded
int vk_fsr31_evaluate(VkCommandBuffer cb, VkImage color, VkFormat cfmt, VkImage depth, VkFormat zfmt, VkImage mv,
                      VkImage out, uint32_t w, uint32_t h, uint32_t ow, uint32_t oh, VkImage bias, float jx, float jy,
                      int reset);
void vk_fsr31_shutdown(void);
