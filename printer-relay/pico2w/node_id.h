#ifndef NODE_ID_H
#define NODE_ID_H

// Stable, per-board unique node identifier, e.g. "relay-pico-9F3A2C" — the
// NODE_ID prefix (config.h) plus the low 3 bytes of the RP2350's unique flash
// ID. This makes every Pico self-identifying in a fleet without per-unit builds:
// it tags netlog lines and the LPR job name so multiple relays are distinguishable.
const char* relay_node_id(void);

#endif // NODE_ID_H
