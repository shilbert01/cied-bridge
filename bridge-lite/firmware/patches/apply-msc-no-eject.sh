#!/usr/bin/env bash
# Patch esp_tinyusb managed component for CIED Bridge Lite no-eject USB.
#
# Changes to tusb_msc_storage.c:
#   1. _msc_storage_read_sector — block SDMMC reads when VFS is locally
#      mounted (write_sector already has this guard; without it, USB host
#      READ10 commands hit the SDMMC bus concurrently with FatFS)
#   2. tud_msc_test_unit_ready_cb — BECOMING_READY (ASC 04/01) instead of
#      MEDIUM_NOT_PRESENT (ASC 3A) when VFS is mounted; UNIT_ATTENTION
#      (ASC 28/00) delivered after unmount so host re-reads the filesystem
#   3. tud_msc_write10_cb — synchronous writes instead of deferred
#      single-buffer (deferred mechanism loses writes when two WRITE10
#      commands arrive close together, e.g. during FAT32 file deletion)
#   4. tinyusb_msc_signal_media_change() — sets UNIT_ATTENTION flag
#
# Run after 'idf.py fullclean' or whenever managed_components are re-downloaded:
#   cd firmware && bash patches/apply-msc-no-eject.sh

set -euo pipefail
cd "$(dirname "$0")/.."

SRC="managed_components/espressif__esp_tinyusb/tusb_msc_storage.c"
HDR="managed_components/espressif__esp_tinyusb/include/tusb_msc_storage.h"

if [ ! -f "$SRC" ]; then
    echo "ERROR: $SRC not found — run 'idf.py reconfigure' first" >&2
    exit 1
fi

if grep -q 's_media_changed' "$SRC"; then
    echo "Patch already applied."
    exit 0
fi

python3 << 'PYEOF'
import sys

src = "managed_components/espressif__esp_tinyusb/tusb_msc_storage.c"
hdr = "managed_components/espressif__esp_tinyusb/include/tusb_msc_storage.h"

with open(src) as f:
    c = f.read()

# 1. Add media_changed flag + signal function
c = c.replace(
    'static tinyusb_msc_storage_handle_s *s_storage_handle;',
    '''static tinyusb_msc_storage_handle_s *s_storage_handle;

static volatile bool s_media_changed = false;

void tinyusb_msc_signal_media_change(void)
{
    s_media_changed = true;
}''', 1)

# 2. Block reads when FAT is locally mounted
c = c.replace(
    '    assert(s_storage_handle);\n'
    '    size_t sector_size = tinyusb_msc_storage_get_sector_size();\n'
    '    return (s_storage_handle->read)(sector_size, lba, offset, size, dest);',
    '    assert(s_storage_handle);\n'
    '    if (s_storage_handle->is_fat_mounted) {\n'
    '        return ESP_ERR_INVALID_STATE;\n'
    '    }\n'
    '    size_t sector_size = tinyusb_msc_storage_get_sector_size();\n'
    '    return (s_storage_handle->read)(sector_size, lba, offset, size, dest);', 1)

# 3. Rewrite tud_msc_test_unit_ready_cb
old_tur = (
    'bool tud_msc_test_unit_ready_cb(uint8_t lun)\n'
    '{\n'
    '    (void) lun;\n'
    '    bool result = false;\n'
    '\n'
    '    if (s_storage_handle->is_fat_mounted) {\n'
    '        tud_msc_set_sense(lun, SCSI_SENSE_NOT_READY, SCSI_CODE_ASC_MEDIUM_NOT_PRESENT, SCSI_CODE_ASCQ);\n'
    '        result = false;\n'
    '    } else {\n'
    '        if (tinyusb_msc_storage_unmount() != ESP_OK) {\n'
    '            ESP_LOGW(TAG, "tud_msc_test_unit_ready_cb() unmount Fails");\n'
    '        }\n'
    '        result = true;\n'
    '    }\n'
    '    return result;\n'
    '}')

new_tur = (
    'bool tud_msc_test_unit_ready_cb(uint8_t lun)\n'
    '{\n'
    '    (void) lun;\n'
    '\n'
    '    if (s_storage_handle->is_fat_mounted) {\n'
    '        tud_msc_set_sense(lun, SCSI_SENSE_NOT_READY, 0x04, 0x01);\n'
    '        return false;\n'
    '    }\n'
    '\n'
    '    if (s_media_changed) {\n'
    '        s_media_changed = false;\n'
    '        tud_msc_set_sense(lun, SCSI_SENSE_UNIT_ATTENTION, 0x28, 0x00);\n'
    '        return false;\n'
    '    }\n'
    '\n'
    '    if (tinyusb_msc_storage_unmount() != ESP_OK) {\n'
    '        ESP_LOGW(TAG, "tud_msc_test_unit_ready_cb() unmount Fails");\n'
    '    }\n'
    '    return true;\n'
    '}')

if old_tur not in c:
    print("ERROR: could not find original tud_msc_test_unit_ready_cb", file=sys.stderr)
    sys.exit(1)
c = c.replace(old_tur, new_tur, 1)

# 4. Synchronous writes (replace deferred single-buffer mechanism)
old_w10 = (
    'int32_t tud_msc_write10_cb(uint8_t lun, uint32_t lba, uint32_t offset, uint8_t *buffer, uint32_t bufsize)\n'
    '{\n'
    '    assert(bufsize <= MSC_STORAGE_BUFFER_SIZE);\n'
    '    // Copy data to the buffer\n'
    '    memcpy((void *)s_storage_handle->storage_buffer.data_buffer, buffer, bufsize);\n'
    '    s_storage_handle->storage_buffer.lba = lba;\n'
    '    s_storage_handle->storage_buffer.offset = offset;\n'
    '    s_storage_handle->storage_buffer.bufsize = bufsize;\n'
    '\n'
    '    // Defer execution of the write to the TinyUSB task\n'
    '    usbd_defer_func(_write_func, NULL, false);\n'
    '\n'
    '    // Return the number of bytes accepted\n'
    '    return bufsize;\n'
    '}')

new_w10 = (
    'int32_t tud_msc_write10_cb(uint8_t lun, uint32_t lba, uint32_t offset, uint8_t *buffer, uint32_t bufsize)\n'
    '{\n'
    '    assert(bufsize <= MSC_STORAGE_BUFFER_SIZE);\n'
    '    memcpy((void *)s_storage_handle->storage_buffer.data_buffer, buffer, bufsize);\n'
    '    esp_err_t err = _msc_storage_write_sector(lba, offset, bufsize,\n'
    '                        (const void *)s_storage_handle->storage_buffer.data_buffer);\n'
    '    if (err != ESP_OK) {\n'
    '        ESP_LOGE(TAG, "write10 failed: lba=%lu err=0x%x", lba, err);\n'
    '        return 0;\n'
    '    }\n'
    '    return bufsize;\n'
    '}')

if old_w10 not in c:
    print("ERROR: could not find original tud_msc_write10_cb", file=sys.stderr)
    sys.exit(1)
c = c.replace(old_w10, new_w10, 1)

with open(src, 'w') as f:
    f.write(c)

# 5. Patch header — declare signal function
with open(hdr) as f:
    h = f.read()

h = h.replace(
    'bool tinyusb_msc_storage_in_use_by_usb_host(void);',
    'bool tinyusb_msc_storage_in_use_by_usb_host(void);\n'
    '\n'
    'void tinyusb_msc_signal_media_change(void);', 1)

with open(hdr, 'w') as f:
    f.write(h)

print(f"Patched {src} and {hdr}")
PYEOF

echo "Done — no-eject USB (read guard + BECOMING_READY + UNIT_ATTENTION + sync writes)"
