// The player's state for the PC scheme's gameplay contexts (TOTK_PLAYER_STATE, 1.4.3): the five action verbs, the
// selected ability and whether Ultrahand holds an object. Read-only; see player_state.c for the walk.
#pragma once

// Once per presented frame, from the game thread (screen_watch_poll calls it): refreshes the values below.
void player_state_poll(void);
// The verb of action slot 0..4 (-1 none): slot 0 is the body, 1..4 the limbs.
int player_state_verb(int slot);
// mCurrentSpecialPower: 1 Ultrahand, 2 Fuse, 3 Ascend, 4 Recall, 5 Autobuild, 6 Camera; 0 none.
int player_state_power(void);
// 1 while Ultrahand holds an object (its mState 4 Grabbing / 5 Grabbed).
int player_state_holding(void);

// The stance bits of the last walk, for the scheme's gameplay contexts (the same grouping Weeping's TPS_* uses).
enum {
    PS_VALID = 1u << 0,   // a player actor was found
    PS_CLIMB = 1u << 1,   // climbing a wall, a ladder, a waterfall
    PS_AIR = 1u << 2,     // in the air, glider closed
    PS_GLIDE = 1u << 3,   // the paraglider (or a wing) open
    PS_SWIM = 1u << 4,
    PS_AIM = 1u << 5,     // the bow is in hand (UseBow 59 / DrawnBow 205)
    PS_ABILITY = 1u << 8, // an ability is out (UseSpecialPower 61 in a slot)
    PS_HORSE = 1u << 9,
    PS_VEHICLE = 1u << 10,
    PS_HOLDING = 1u << 15,   // Ultrahand holds an object
};
// 0 once the last successful walk is older than 250 ms (a load, the title, a cutscene): the router then keeps WORLD.
uint32_t player_state_flags(uint64_t now_ms);
