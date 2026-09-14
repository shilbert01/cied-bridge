#include "node_id.h"
#include "config.h"
#include "pico/unique_id.h"
#include <stdio.h>

static char _id[48];

const char* relay_node_id(void) {
  if (_id[0]) return _id;
  pico_unique_board_id_t board;
  pico_get_unique_board_id(&board);
  const int n = PICO_UNIQUE_BOARD_ID_SIZE_BYTES;
  snprintf(_id, sizeof(_id), "%s-%02X%02X%02X", NODE_ID,
           board.id[n - 3], board.id[n - 2], board.id[n - 1]);
  return _id;
}
