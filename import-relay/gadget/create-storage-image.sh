#!/bin/sh
# Creates the FAT32 backing image for the USB mass storage function.
# Produces a proper partitioned disk (MBR + FAT32 on P1) so that
# picky embedded USB hosts (Abbott programmers, etc.) accept it.
# Run once at initial setup. The image is re-used across reboots.
#
# Identical in spirit to export-relay/gadget/create-storage-image.sh —
# only the config path and default image location differ.
set -eu

CONF=/etc/cied-import/import.conf
[ -f "$CONF" ] || { echo "Missing $CONF"; exit 1; }
# shellcheck source=/dev/null
. "$CONF"

IMG="${DISK_IMAGE:-/var/lib/cied-import/disk.img}"
SIZE_MB="${DISK_SIZE_MB:-256}"

if [ -f "$IMG" ]; then
    echo "Image already exists: $IMG ($(($(wc -c < "$IMG") / 1024 / 1024))MB)"
    exit 0
fi

mkdir -p "$(dirname "$IMG")"

echo "Creating ${SIZE_MB}MB partitioned FAT32 image at $IMG ..."
dd if=/dev/zero of="$IMG" bs=1M count="$SIZE_MB"

# Write MBR + partition table: P1 starts at sector 2048 (1MB aligned), type 0x0C (FAT32 LBA).
python3 - "$IMG" "$SIZE_MB" <<'PYEOF'
import struct, sys
img    = sys.argv[1]
sz_mb  = int(sys.argv[2])
total  = sz_mb * 1024 * 1024 // 512   # total sectors
pstart = 2048                          # 1 MB alignment
psize  = total - pstart

entry = bytearray(16)
entry[0]    = 0x00
entry[1:4]  = b'\xfe\xff\xff'
entry[4]    = 0x0c             # FAT32 with LBA
entry[5:8]  = b'\xfe\xff\xff'
struct.pack_into('<I', entry, 8,  pstart)
struct.pack_into('<I', entry, 12, psize)

with open(img, 'r+b') as f:
    f.seek(446); f.write(bytes(entry))
    f.write(bytes(48))
    f.seek(510); f.write(b'\x55\xaa')

print(f"MBR: P1 type=0x0C start={pstart} size={psize} ({psize//2048} MiB)")
PYEOF

PART_OFFSET=$((2048 * 512))
LOOP=$(losetup -f)
losetup -o "$PART_OFFSET" "$LOOP" "$IMG"
mkdosfs -F 32 -n IMPORT "$LOOP"
losetup -d "$LOOP"

echo "Done: $IMG (${SIZE_MB}MB, MBR + FAT32 on partition 1)"
