// The hooks this build can bind (recomp/patches.py `hook`, runtime/hook.c). One list rather than a registration in each
// hook's own file: a static library drops an object that nothing references, so a hook that only registered itself in a
// constructor would never be linked in and its slot would stay empty.
//
// A name here is the one a patch table's `hook` line carries. A build whose table lacks it binds nothing and says
// nothing (rt_hook_bind returns -1), which is what a game version without that patch should do: the calls run the
// game's own code.
#include "cpu.h"
#include "host.h"

void swim_ease_hook(Cpu *c, void (*body)(Cpu *));   // swim_ease.c

void rt_hooks_init(void) {
    rt_hook_bind("swim_ease", swim_ease_hook);
}
