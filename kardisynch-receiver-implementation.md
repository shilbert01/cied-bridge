# KardiSynch Receiver — Pi Zero W USB Gadget Implementation

## Overview

This document describes the full implementation of a **Pi Zero W** acting as the **receiver node** in the KardiSynch pipeline. It receives device interrogation files from a Pi 3 server over WiFi and presents them to a Windows host PC via USB gadget mode.

Three deployment scenarios are covered, determined by the hospital IT USB policy on the Windows host:

| Scenario | USB Policy | Gadget Mode | Windows Side |
|----------|-----------|-------------|-------------|
| **2** | USB unrestricted | `g_mass_storage` | Drive letter, KardiSynch watches directly |
| **1a** | Mass storage blocked, Ethernet allowed | `g_ether` (RNDIS/CDC ECM) | Mapped network drive via Samba |
| **1c** | Mass storage blocked, HID allowed | `g_hid` | Companion app reassembles files → folder |

In all scenarios KardiSynch watches a local folder. Only the plumbing behind that folder changes.

---

## System Architecture

```
Programmer₁ → USB → Pi Zero W₁ ┐
Programmer₂ → USB → Pi Zero W₂ ├── WiFi ──→ Pi 3 (Server)
Programmer₃ → USB → Pi Zero W₃ ┘        │
                                         │ WiFi
                                         ▼
                                   Pi Zero W (Receiver)
                                         │ USB gadget
                                         ▼
                                   Windows Host PC
                                         │
                                   KardiSynch (folder watch)
```

The receiver Pi Zero W:
- Connects to the Pi 3's WiFi network as a client
- Receives files pushed from the Pi 3 (or pulls them)
- Presents those files to the Windows PC via one of three USB gadget modes
- The OTG port is used exclusively for the gadget connection to Windows

---

## Common Setup (All Scenarios)

### Base OS

Use Raspberry Pi OS Lite (Bookworm or Bullseye). Headless setup.

### Enable the DWC2 OTG Driver

This is required for all gadget modes.

**File: `/boot/firmware/config.txt`** (or `/boot/config.txt` on older images)

Append:
```
dtoverlay=dwc2
```

**File: `/boot/firmware/cmdline.txt`**

Add `modules-load=dwc2` after `rootwait`:
```
... rootwait modules-load=dwc2 ...
```

### Load libcomposite

**File: `/etc/modules`**

Append:
```
libcomposite
```

### WiFi Client Configuration

The receiver Zero connects to the Pi 3's network. Configure via `wpa_supplicant` or NetworkManager depending on your OS version.

**File: `/etc/wpa_supplicant/wpa_supplicant.conf`** (if using wpa_supplicant)
```
country=DE
ctrl_interface=DIR=/var/run/wpa_supplicant GROUP=netdev
update_config=1

network={
    ssid="KardiSynch-Net"
    psk="your-secure-passphrase"
    priority=1
}
```

### File Receiving from Pi 3

Create a staging directory where incoming files land:

```bash
sudo mkdir -p /opt/kardisynch/incoming
sudo chown pi:pi /opt/kardisynch/incoming
```

**Option A: Pi 3 pushes via rsync/scp**

Set up SSH key auth from Pi 3 to receiver Zero. Pi 3 runs:
```bash
rsync -avz /path/to/outgoing/ pi@receiver-zero:/opt/kardisynch/incoming/
```

**Option B: Receiver pulls periodically**

Cron or systemd timer on receiver:
```bash
rsync -avz pi@pi3-server:/path/to/outgoing/ /opt/kardisynch/incoming/
```

**Option C: Pi 3 pushes via HTTP POST**

Run a lightweight HTTP server on the receiver (see `kardisynch-http-receiver.py` below). This is the most event-driven option.

---

## Scenario 2: USB Mass Storage Gadget

USB ports are unrestricted. The Zero presents a FAT32 image as a USB drive to Windows.

### Create the FAT32 Image

```bash
sudo dd if=/dev/zero of=/opt/kardisynch/usbdisk.img bs=1M count=64
sudo mkfs.vfat -F 32 -n KARDISYNCH /opt/kardisynch/usbdisk.img
```

### Gadget Setup Script

**File: `/opt/kardisynch/gadget-mass-storage.sh`**

```bash
#!/bin/bash
set -e

GADGET_DIR="/sys/kernel/config/usb_gadget/kardisynch"
IMG="/opt/kardisynch/usbdisk.img"
SERIAL="KS$(hostname | md5sum | head -c 8)"

# Clean up if already configured
if [ -d "$GADGET_DIR" ]; then
    echo "" > "$GADGET_DIR/UDC" 2>/dev/null || true
    rm -f "$GADGET_DIR/configs/c.1/mass_storage.usb0"
    rmdir "$GADGET_DIR/configs/c.1/strings/0x409" 2>/dev/null || true
    rmdir "$GADGET_DIR/configs/c.1" 2>/dev/null || true
    rmdir "$GADGET_DIR/functions/mass_storage.usb0" 2>/dev/null || true
    rmdir "$GADGET_DIR/strings/0x409" 2>/dev/null || true
    rmdir "$GADGET_DIR" 2>/dev/null || true
fi

# Create gadget
mkdir -p "$GADGET_DIR"
cd "$GADGET_DIR"

echo 0x1d6b > idVendor   # Linux Foundation
echo 0x0104 > idProduct  # Multifunction Composite Gadget
echo 0x0100 > bcdDevice
echo 0x0200 > bcdUSB

# Strings
mkdir -p strings/0x409
echo "$SERIAL"       > strings/0x409/serialnumber
echo "Konsilado"     > strings/0x409/manufacturer
echo "KardiSynch"    > strings/0x409/product

# Config
mkdir -p configs/c.1/strings/0x409
echo "Mass Storage"  > configs/c.1/strings/0x409/configuration
echo 250             > configs/c.1/MaxPower

# Mass storage function
mkdir -p functions/mass_storage.usb0
echo 1    > functions/mass_storage.usb0/stall
echo 0    > functions/mass_storage.usb0/lun.0/cdrom
echo 0    > functions/mass_storage.usb0/lun.0/ro
echo 0    > functions/mass_storage.usb0/lun.0/nofua
echo "$IMG" > functions/mass_storage.usb0/lun.0/file

# Link and bind
ln -s functions/mass_storage.usb0 configs/c.1/
ls /sys/class/udc > UDC

echo "Mass storage gadget active, image: $IMG"
```

### Sync-and-Refresh Script

This is the core loop: when new files arrive, unbind the gadget, mount the image, copy files in, unmount, rebind.

**File: `/opt/kardisynch/sync-to-image.sh`**

```bash
#!/bin/bash
set -e

IMG="/opt/kardisynch/usbdisk.img"
INCOMING="/opt/kardisynch/incoming"
MOUNT_POINT="/mnt/kardisynch-img"
GADGET_UDC="/sys/kernel/config/usb_gadget/kardisynch/UDC"

sudo mkdir -p "$MOUNT_POINT"

sync_files() {
    echo "$(date): New files detected, syncing..."

    # 1. Unbind gadget (Windows ejects the drive)
    echo "" | sudo tee "$GADGET_UDC" > /dev/null
    sleep 0.5

    # 2. Mount FAT32 image
    sudo mount -o loop "$IMG" "$MOUNT_POINT"

    # 3. Copy new files
    cp -v "$INCOMING"/* "$MOUNT_POINT"/ 2>/dev/null || true

    # 4. Optionally remove old files beyond retention
    # find "$MOUNT_POINT" -type f -mtime +7 -delete

    # 5. Unmount
    sudo umount "$MOUNT_POINT"
    sync

    # 6. Rebind gadget (Windows re-enumerates, drive reappears)
    ls /sys/class/udc | sudo tee "$GADGET_UDC" > /dev/null

    # 7. Clear incoming staging
    rm -f "$INCOMING"/*

    echo "$(date): Sync complete, gadget rebound"
}

# Watch for new files via inotifywait
if command -v inotifywait &>/dev/null; then
    echo "Watching $INCOMING for new files..."
    while true; do
        inotifywait -q -e close_write -e moved_to "$INCOMING"
        sleep 1  # debounce: wait for all files to land
        if ls "$INCOMING"/* &>/dev/null; then
            sync_files
        fi
    done
else
    echo "inotifywait not found. Install: sudo apt install inotify-tools"
    echo "Falling back to polling..."
    while true; do
        if ls "$INCOMING"/* &>/dev/null 2>&1; then
            sync_files
        fi
        sleep 5
    done
fi
```

### Systemd Services

**File: `/etc/systemd/system/kardisynch-gadget.service`**

```ini
[Unit]
Description=KardiSynch USB Mass Storage Gadget
After=local-fs.target

[Service]
Type=oneshot
RemainAfterExit=yes
ExecStart=/opt/kardisynch/gadget-mass-storage.sh

[Install]
WantedBy=multi-user.target
```

**File: `/etc/systemd/system/kardisynch-sync.service`**

```ini
[Unit]
Description=KardiSynch Sync-to-Image Watcher
After=kardisynch-gadget.service
Requires=kardisynch-gadget.service

[Service]
Type=simple
ExecStart=/opt/kardisynch/sync-to-image.sh
Restart=always
RestartSec=5
User=root

[Install]
WantedBy=multi-user.target
```

### Enable

```bash
sudo chmod +x /opt/kardisynch/gadget-mass-storage.sh
sudo chmod +x /opt/kardisynch/sync-to-image.sh
sudo apt install inotify-tools
sudo systemctl daemon-reload
sudo systemctl enable kardisynch-gadget kardisynch-sync
sudo systemctl start kardisynch-gadget kardisynch-sync
```

### Windows Side

Nothing to install. Windows auto-detects a USB mass storage device, assigns a drive letter (e.g. `E:\`). Configure KardiSynch to watch `E:\` (or whatever letter is assigned).

**Note:** KardiSynch must tolerate a ~1-2 second drive disappearance during the rebind cycle. If it uses `FileSystemWatcher`, it should handle the `Error` event and re-attach when the drive reappears. Polling-based watchers handle this naturally.

---

## Scenario 1a: USB Ethernet Gadget (RNDIS/CDC ECM)

Mass storage is blocked but USB Ethernet (RNDIS) is allowed. The Zero appears as a USB network adapter to Windows.

### Gadget Setup Script

**File: `/opt/kardisynch/gadget-ether.sh`**

```bash
#!/bin/bash
set -e

GADGET_DIR="/sys/kernel/config/usb_gadget/kardisynch"
SERIAL="KS$(hostname | md5sum | head -c 8)"

# Clean up if already configured
if [ -d "$GADGET_DIR" ]; then
    echo "" > "$GADGET_DIR/UDC" 2>/dev/null || true
    rm -f "$GADGET_DIR/configs/c.1/rndis.usb0"
    rmdir "$GADGET_DIR/configs/c.1/strings/0x409" 2>/dev/null || true
    rmdir "$GADGET_DIR/configs/c.1" 2>/dev/null || true
    rmdir "$GADGET_DIR/functions/rndis.usb0" 2>/dev/null || true
    rmdir "$GADGET_DIR/strings/0x409" 2>/dev/null || true
    rmdir "$GADGET_DIR" 2>/dev/null || true
fi

# Create gadget
mkdir -p "$GADGET_DIR"
cd "$GADGET_DIR"

echo 0x1d6b > idVendor
echo 0x0104 > idProduct
echo 0x0100 > bcdDevice
echo 0x0200 > bcdUSB

# Windows RNDIS compat
echo 0xEF   > bDeviceClass
echo 0x02   > bDeviceSubClass
echo 0x01   > bDeviceProtocol

# Strings
mkdir -p strings/0x409
echo "$SERIAL"       > strings/0x409/serialnumber
echo "Konsilado"     > strings/0x409/manufacturer
echo "KardiSynch"    > strings/0x409/product

# Config
mkdir -p configs/c.1/strings/0x409
echo "RNDIS Network" > configs/c.1/strings/0x409/configuration
echo 250             > configs/c.1/MaxPower

# RNDIS function
mkdir -p functions/rndis.usb0

# Microsoft OS Descriptors for automatic RNDIS driver loading
echo 1       > os_desc/use
echo 0xcd    > os_desc/b_vendor_code
echo MSFT100 > os_desc/qw_sign

mkdir -p functions/rndis.usb0/os_desc/interface.rndis
echo RNDIS   > functions/rndis.usb0/os_desc/interface.rndis/compatible_id
echo 5162001 > functions/rndis.usb0/os_desc/interface.rndis/sub_compatible_id

# Link and bind
ln -s functions/rndis.usb0 configs/c.1/
ln -s configs/c.1 os_desc/

ls /sys/class/udc > UDC

# Configure the usb0 interface with a static IP
sleep 1
ifconfig usb0 10.55.0.1 netmask 255.255.255.0 up

echo "RNDIS gadget active, Pi is 10.55.0.1 on usb0"
```

### DHCP Server for the USB Link

Install dnsmasq to hand the Windows PC an IP address over the USB Ethernet link.

```bash
sudo apt install dnsmasq
```

**File: `/etc/dnsmasq.d/usb-gadget.conf`**

```
interface=usb0
bind-interfaces
dhcp-range=10.55.0.10,10.55.0.20,255.255.255.0,12h
dhcp-option=3  # no default gateway — don't mess with Windows routing
```

Restart: `sudo systemctl restart dnsmasq`

### Samba Share

Install Samba so Windows can map a network drive over the USB Ethernet link.

```bash
sudo apt install samba
```

**File: `/etc/samba/smb.conf`** — append:

```ini
[kardisynch]
   path = /opt/kardisynch/incoming
   browseable = yes
   read only = yes
   guest ok = yes
   force user = pi
```

Restart: `sudo systemctl restart smbd`

### Systemd Service

**File: `/etc/systemd/system/kardisynch-gadget.service`**

```ini
[Unit]
Description=KardiSynch USB Ethernet Gadget
After=local-fs.target

[Service]
Type=oneshot
RemainAfterExit=yes
ExecStart=/opt/kardisynch/gadget-ether.sh

[Install]
WantedBy=multi-user.target
```

### Windows Side

1. Windows detects a new RNDIS network adapter and gets IP `10.55.0.x` via DHCP.
2. Map the share: `net use Z: \\10.55.0.1\kardisynch /persistent:yes`
3. Configure KardiSynch to watch `Z:\`

No companion app needed. Files arriving in `/opt/kardisynch/incoming` on the Pi are immediately visible in the share.

**Note:** No sync/rebind cycle needed here — the share is live. Files appear as soon as they're written. This is the simplest scenario from KardiSynch's perspective.

---

## Scenario 1c: USB HID Gadget with Companion App

Mass storage and Ethernet are blocked, but HID is allowed (keyboards/mice always work). The Zero presents as a generic HID device and streams file data as HID reports. A small companion app on Windows reassembles them.

### Protocol Design

HID reports are 64 bytes. We define a simple file transfer protocol:

```
Byte 0:    Report ID (0x01)
Byte 1:    Packet type
             0x01 = FILE_START
             0x02 = FILE_DATA
             0x03 = FILE_END
             0x04 = HEARTBEAT
Byte 2-3:  Sequence number (uint16 LE)
Byte 4-63: Payload (60 bytes)

FILE_START payload:
  Bytes 4-7:   File size (uint32 LE)
  Bytes 8-11:  CRC32 of entire file (uint32 LE)
  Bytes 12-63: Filename (null-terminated UTF-8, max 52 chars)

FILE_DATA payload:
  Bytes 4-63:  60 bytes of file content

FILE_END payload:
  Bytes 4-7:   CRC32 of entire file (uint32 LE, for verification)
  Bytes 8-63:  Unused (zeroed)
```

### HID Report Descriptor

**File: `/opt/kardisynch/hid-report-descriptor.bin`** (generated by the setup script)

The descriptor defines a vendor-specific HID device with 64-byte input reports (device→host) and 64-byte output reports (host→device, for ACKs if needed).

### Gadget Setup Script

**File: `/opt/kardisynch/gadget-hid.sh`**

```bash
#!/bin/bash
set -e

GADGET_DIR="/sys/kernel/config/usb_gadget/kardisynch"
SERIAL="KS$(hostname | md5sum | head -c 8)"

# HID report descriptor: vendor-specific, 64-byte input and output reports
# This is a minimal descriptor for a generic HID data pipe
REPORT_DESC_HEX="06d0f10901a1010920150026ff00750895400981029100c0"

# Clean up if already configured
if [ -d "$GADGET_DIR" ]; then
    echo "" > "$GADGET_DIR/UDC" 2>/dev/null || true
    rm -f "$GADGET_DIR/configs/c.1/hid.usb0"
    rmdir "$GADGET_DIR/configs/c.1/strings/0x409" 2>/dev/null || true
    rmdir "$GADGET_DIR/configs/c.1" 2>/dev/null || true
    rmdir "$GADGET_DIR/functions/hid.usb0" 2>/dev/null || true
    rmdir "$GADGET_DIR/strings/0x409" 2>/dev/null || true
    rmdir "$GADGET_DIR" 2>/dev/null || true
fi

# Create gadget
mkdir -p "$GADGET_DIR"
cd "$GADGET_DIR"

echo 0x1d6b > idVendor
echo 0x0104 > idProduct
echo 0x0100 > bcdDevice
echo 0x0200 > bcdUSB

# Strings
mkdir -p strings/0x409
echo "$SERIAL"       > strings/0x409/serialnumber
echo "Konsilado"     > strings/0x409/manufacturer
echo "KardiSynch"    > strings/0x409/product

# Config
mkdir -p configs/c.1/strings/0x409
echo "HID Transfer"  > configs/c.1/strings/0x409/configuration
echo 250             > configs/c.1/MaxPower

# HID function
mkdir -p functions/hid.usb0
echo 0   > functions/hid.usb0/protocol
echo 0   > functions/hid.usb0/subclass
echo 64  > functions/hid.usb0/report_length

# Write report descriptor
echo "$REPORT_DESC_HEX" | xxd -r -p > functions/hid.usb0/report_desc

# Link and bind
ln -s functions/hid.usb0 configs/c.1/
ls /sys/class/udc > UDC

echo "HID gadget active on /dev/hidg0"
```

### Pi-Side HID File Sender Daemon

**File: `/opt/kardisynch/hid-sender.py`**

```python
#!/usr/bin/env python3
"""
HID file sender daemon.
Watches /opt/kardisynch/incoming for new files and streams them
to the Windows host as HID reports via /dev/hidg0.
"""

import os
import sys
import struct
import time
import zlib
import logging
from pathlib import Path
from watchdog.observers import Observer
from watchdog.events import FileSystemEventHandler

INCOMING_DIR = "/opt/kardisynch/incoming"
HID_DEVICE = "/dev/hidg0"
REPORT_SIZE = 64
PAYLOAD_SIZE = 60  # 64 - 4 header bytes
SEND_DELAY = 0.001  # 1ms between reports, ~60KB/s throughput

PKT_FILE_START = 0x01
PKT_FILE_DATA = 0x02
PKT_FILE_END = 0x03
PKT_HEARTBEAT = 0x04

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
log = logging.getLogger("hid-sender")


def make_report(pkt_type: int, seq: int, payload: bytes) -> bytes:
    """Build a 64-byte HID report."""
    header = struct.pack("<BBH", 0x01, pkt_type, seq & 0xFFFF)
    data = payload[:PAYLOAD_SIZE].ljust(PAYLOAD_SIZE, b"\x00")
    return header + data


def send_report(fd, report: bytes):
    """Write a single report to /dev/hidg0."""
    os.write(fd, report)
    time.sleep(SEND_DELAY)


def send_file(filepath: str):
    """Send a single file over HID."""
    path = Path(filepath)
    if not path.is_file():
        return

    data = path.read_bytes()
    file_size = len(data)
    crc = zlib.crc32(data) & 0xFFFFFFFF
    filename = path.name.encode("utf-8")[:52]

    log.info(f"Sending: {path.name} ({file_size} bytes, CRC32={crc:#010x})")

    fd = os.open(HID_DEVICE, os.O_WRONLY)
    try:
        seq = 0

        # FILE_START
        start_payload = struct.pack("<II", file_size, crc) + filename + b"\x00"
        send_report(fd, make_report(PKT_FILE_START, seq, start_payload))
        seq += 1

        # FILE_DATA chunks
        offset = 0
        while offset < file_size:
            chunk = data[offset : offset + PAYLOAD_SIZE]
            send_report(fd, make_report(PKT_FILE_DATA, seq, chunk))
            seq += 1
            offset += PAYLOAD_SIZE

        # FILE_END
        end_payload = struct.pack("<I", crc)
        send_report(fd, make_report(PKT_FILE_END, seq, end_payload))

        log.info(f"Sent {path.name}: {seq + 1} reports")

    finally:
        os.close(fd)

    # Remove from staging after successful send
    path.unlink()


class IncomingHandler(FileSystemEventHandler):
    def on_closed(self, event):
        if not event.is_directory:
            try:
                send_file(event.src_path)
            except Exception as e:
                log.error(f"Failed to send {event.src_path}: {e}")


def main():
    os.makedirs(INCOMING_DIR, exist_ok=True)

    # Send any files already in the directory
    for f in sorted(Path(INCOMING_DIR).iterdir()):
        if f.is_file():
            try:
                send_file(str(f))
            except Exception as e:
                log.error(f"Failed to send {f}: {e}")

    # Watch for new arrivals
    observer = Observer()
    observer.schedule(IncomingHandler(), INCOMING_DIR, recursive=False)
    observer.start()
    log.info(f"Watching {INCOMING_DIR}")

    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        observer.stop()
    observer.join()


if __name__ == "__main__":
    main()
```

### Install Python Dependencies on Pi

```bash
sudo apt install python3-pip
pip3 install watchdog --break-system-packages
```

### Systemd Services

**File: `/etc/systemd/system/kardisynch-gadget.service`**

```ini
[Unit]
Description=KardiSynch USB HID Gadget
After=local-fs.target

[Service]
Type=oneshot
RemainAfterExit=yes
ExecStart=/opt/kardisynch/gadget-hid.sh

[Install]
WantedBy=multi-user.target
```

**File: `/etc/systemd/system/kardisynch-hid-sender.service`**

```ini
[Unit]
Description=KardiSynch HID File Sender
After=kardisynch-gadget.service
Requires=kardisynch-gadget.service

[Service]
Type=simple
ExecStart=/usr/bin/python3 /opt/kardisynch/hid-sender.py
Restart=always
RestartSec=5
User=root

[Install]
WantedBy=multi-user.target
```

### Windows Companion App: KardiSynch-HID-Bridge

A small C# (.NET) application that reads HID reports from the Pi Zero and reassembles files into a watched folder.

**Project: `KardiSynch-HID-Bridge`**

Use the `HidSharp` NuGet package (cross-platform HID library, no driver install needed).

**File: `KardiSynchHidBridge/Program.cs`**

```csharp
using System;
using System.IO;
using System.Linq;
using System.Threading;
using HidSharp;

namespace KardiSynchHidBridge
{
    class Program
    {
        // Must match idVendor/idProduct in gadget-hid.sh
        const int VendorId = 0x1d6b;
        const int ProductId = 0x0104;

        const byte PKT_FILE_START = 0x01;
        const byte PKT_FILE_DATA = 0x02;
        const byte PKT_FILE_END = 0x03;

        static string OutputDir = @"C:\KardiSynch\incoming";

        static void Main(string[] args)
        {
            if (args.Length > 0)
                OutputDir = args[0];

            Directory.CreateDirectory(OutputDir);

            Console.WriteLine($"KardiSynch HID Bridge");
            Console.WriteLine($"Output: {OutputDir}");
            Console.WriteLine($"Looking for device {VendorId:X4}:{ProductId:X4}...");

            while (true)
            {
                try
                {
                    var device = DeviceList.Local
                        .GetHidDevices(VendorId, ProductId)
                        .FirstOrDefault();

                    if (device == null)
                    {
                        Thread.Sleep(1000);
                        continue;
                    }

                    Console.WriteLine($"Connected: {device}");
                    RunReceiveLoop(device);
                }
                catch (Exception ex)
                {
                    Console.WriteLine($"Error: {ex.Message}");
                    Thread.Sleep(2000);
                }
            }
        }

        static void RunReceiveLoop(HidDevice device)
        {
            using var stream = device.Open();
            stream.ReadTimeout = Timeout.Infinite;

            string currentFileName = null;
            uint expectedSize = 0;
            uint expectedCrc = 0;
            MemoryStream fileBuffer = null;

            var report = new byte[65]; // HID reports may have report ID prefix

            while (true)
            {
                int bytesRead = stream.Read(report, 0, report.Length);
                if (bytesRead < 5) continue;

                // Offset: some HID stacks prepend report ID byte
                int offset = (report[0] == 0x01 && bytesRead == 65) ? 1 : 0;

                byte reportId = report[offset];
                byte pktType = report[offset + 1];
                ushort seq = BitConverter.ToUInt16(report, offset + 2);
                var payload = new byte[60];
                Array.Copy(report, offset + 4, payload, 0, 60);

                switch (pktType)
                {
                    case PKT_FILE_START:
                        expectedSize = BitConverter.ToUInt32(payload, 0);
                        expectedCrc = BitConverter.ToUInt32(payload, 4);

                        // Extract filename (null-terminated)
                        int nullIdx = Array.IndexOf<byte>(payload, 0, 8);
                        if (nullIdx < 0) nullIdx = 60;
                        currentFileName = System.Text.Encoding.UTF8.GetString(
                            payload, 8, nullIdx - 8);

                        fileBuffer = new MemoryStream();
                        Console.WriteLine(
                            $"Receiving: {currentFileName} ({expectedSize} bytes)");
                        break;

                    case PKT_FILE_DATA:
                        if (fileBuffer == null) break;
                        int remaining = (int)(expectedSize - fileBuffer.Length);
                        int toWrite = Math.Min(remaining, 60);
                        fileBuffer.Write(payload, 0, toWrite);
                        break;

                    case PKT_FILE_END:
                        if (fileBuffer == null || currentFileName == null) break;

                        var fileData = fileBuffer.ToArray();
                        uint actualCrc = Crc32(fileData);
                        uint endCrc = BitConverter.ToUInt32(payload, 0);

                        if (actualCrc == expectedCrc && actualCrc == endCrc)
                        {
                            var outPath = Path.Combine(OutputDir, currentFileName);
                            File.WriteAllBytes(outPath, fileData);
                            Console.WriteLine(
                                $"Saved: {outPath} ({fileData.Length} bytes, CRC OK)");
                        }
                        else
                        {
                            Console.WriteLine(
                                $"CRC MISMATCH for {currentFileName}: " +
                                $"expected={expectedCrc:X8} actual={actualCrc:X8}");
                        }

                        fileBuffer?.Dispose();
                        fileBuffer = null;
                        currentFileName = null;
                        break;
                }
            }
        }

        static uint Crc32(byte[] data)
        {
            // Standard CRC32 matching Python's zlib.crc32
            uint crc = 0xFFFFFFFF;
            foreach (byte b in data)
            {
                crc ^= b;
                for (int i = 0; i < 8; i++)
                    crc = (crc >> 1) ^ (0xEDB88320 & ~((crc & 1) - 1));
            }
            return crc ^ 0xFFFFFFFF;
        }
    }
}
```

**File: `KardiSynchHidBridge/KardiSynchHidBridge.csproj`**

```xml
<Project Sdk="Microsoft.NET.Sdk">
  <PropertyGroup>
    <OutputType>Exe</OutputType>
    <TargetFramework>net8.0</TargetFramework>
  </PropertyGroup>
  <ItemGroup>
    <PackageReference Include="HidSharp" Version="2.1.0" />
  </ItemGroup>
</Project>
```

**Build and run:**

```powershell
dotnet build -c Release
dotnet run -- "C:\KardiSynch\incoming"
```

Or publish as a self-contained exe for deployment:

```powershell
dotnet publish -c Release -r win-x64 --self-contained -o .\publish
```

### Windows Side

1. Install the companion app (single exe, no driver needed — HID is built into Windows)
2. Run it (or install as a Windows service using NSSM or `sc create`)
3. It writes reassembled files to `C:\KardiSynch\incoming`
4. Configure KardiSynch to watch `C:\KardiSynch\incoming`

---

## HTTP File Receiver (Common, Optional)

If using HTTP POST for file delivery from Pi 3 to receiver Zero (any scenario).

**File: `/opt/kardisynch/http-receiver.py`**

```python
#!/usr/bin/env python3
"""
Lightweight HTTP server that accepts file uploads from the Pi 3.
POST /upload with multipart/form-data.
"""

import os
from http.server import HTTPServer, BaseHTTPRequestHandler
import cgi

INCOMING_DIR = "/opt/kardisynch/incoming"
PORT = 8080


class UploadHandler(BaseHTTPRequestHandler):
    def do_POST(self):
        if self.path != "/upload":
            self.send_response(404)
            self.end_headers()
            return

        content_type = self.headers.get("Content-Type", "")

        if content_type.startswith("multipart/form-data"):
            form = cgi.FieldStorage(
                fp=self.rfile,
                headers=self.headers,
                environ={
                    "REQUEST_METHOD": "POST",
                    "CONTENT_TYPE": content_type,
                },
            )
            file_item = form["file"]
            if file_item.filename:
                filename = os.path.basename(file_item.filename)
                filepath = os.path.join(INCOMING_DIR, filename)
                with open(filepath, "wb") as f:
                    f.write(file_item.file.read())
                self.send_response(200)
                self.end_headers()
                self.wfile.write(f"OK: {filename}\n".encode())
                return

        # Fallback: raw body with filename header
        filename = self.headers.get("X-Filename", "unknown.dat")
        filepath = os.path.join(INCOMING_DIR, filename)
        length = int(self.headers.get("Content-Length", 0))
        with open(filepath, "wb") as f:
            f.write(self.rfile.read(length))
        self.send_response(200)
        self.end_headers()
        self.wfile.write(f"OK: {filename}\n".encode())

    def log_message(self, format, *args):
        pass  # Silence request logging


if __name__ == "__main__":
    os.makedirs(INCOMING_DIR, exist_ok=True)
    server = HTTPServer(("0.0.0.0", PORT), UploadHandler)
    print(f"Listening on :{PORT}")
    server.serve_forever()
```

---

## Scenario Switcher

A single script to switch between gadget modes.

**File: `/opt/kardisynch/set-mode.sh`**

```bash
#!/bin/bash
# Usage: ./set-mode.sh [mass-storage|ether|hid]

MODE="${1:-mass-storage}"
GADGET_DIR="/sys/kernel/config/usb_gadget/kardisynch"

echo "Stopping current services..."
sudo systemctl stop kardisynch-sync 2>/dev/null || true
sudo systemctl stop kardisynch-hid-sender 2>/dev/null || true
sudo systemctl disable kardisynch-sync kardisynch-hid-sender 2>/dev/null || true

# Tear down existing gadget
if [ -d "$GADGET_DIR" ]; then
    echo "" | sudo tee "$GADGET_DIR/UDC" > /dev/null 2>&1 || true
    find "$GADGET_DIR/configs" -type l -delete 2>/dev/null || true
    find "$GADGET_DIR/os_desc" -type l -delete 2>/dev/null || true
    for d in $(find "$GADGET_DIR" -mindepth 1 -type d | sort -r); do
        rmdir "$d" 2>/dev/null || true
    done
    rmdir "$GADGET_DIR" 2>/dev/null || true
fi

case "$MODE" in
    mass-storage)
        echo "Activating: USB Mass Storage (Scenario 2)"
        sudo cp /opt/kardisynch/services/gadget-mass-storage.service \
            /etc/systemd/system/kardisynch-gadget.service
        sudo systemctl daemon-reload
        sudo systemctl restart kardisynch-gadget
        sudo systemctl enable kardisynch-sync
        sudo systemctl start kardisynch-sync
        ;;
    ether)
        echo "Activating: USB Ethernet/RNDIS (Scenario 1a)"
        sudo cp /opt/kardisynch/services/gadget-ether.service \
            /etc/systemd/system/kardisynch-gadget.service
        sudo systemctl daemon-reload
        sudo systemctl restart kardisynch-gadget
        sudo systemctl restart smbd dnsmasq
        ;;
    hid)
        echo "Activating: USB HID Transfer (Scenario 1c)"
        sudo cp /opt/kardisynch/services/gadget-hid.service \
            /etc/systemd/system/kardisynch-gadget.service
        sudo systemctl daemon-reload
        sudo systemctl restart kardisynch-gadget
        sudo systemctl enable kardisynch-hid-sender
        sudo systemctl start kardisynch-hid-sender
        ;;
    *)
        echo "Unknown mode: $MODE"
        echo "Usage: $0 [mass-storage|ether|hid]"
        exit 1
        ;;
esac

echo "Mode set to: $MODE"
```

---

## Directory Structure on Receiver Pi Zero W

```
/opt/kardisynch/
├── incoming/                    # Staging dir for files from Pi 3
├── usbdisk.img                  # FAT32 image (scenario 2 only)
├── gadget-mass-storage.sh       # Gadget setup: mass storage
├── gadget-ether.sh              # Gadget setup: RNDIS ethernet
├── gadget-hid.sh                # Gadget setup: HID
├── sync-to-image.sh             # Sync watcher (scenario 2)
├── hid-sender.py                # HID file sender daemon (scenario 1c)
├── http-receiver.py             # Optional HTTP file receiver
├── set-mode.sh                  # Mode switcher
└── services/                    # Service file templates
    ├── gadget-mass-storage.service
    ├── gadget-ether.service
    └── gadget-hid.service
```

---

## Testing Checklist

### All Scenarios
- [ ] `dwc2` overlay loads on boot (`lsmod | grep dwc2`)
- [ ] `libcomposite` loads on boot (`lsmod | grep libcomposite`)
- [ ] WiFi connects to Pi 3 network on boot
- [ ] Files arrive in `/opt/kardisynch/incoming/` from Pi 3

### Scenario 2 (Mass Storage)
- [ ] `usbdisk.img` exists and is valid FAT32
- [ ] Gadget binds successfully (`cat /sys/kernel/config/usb_gadget/kardisynch/UDC`)
- [ ] Windows detects USB drive and assigns letter
- [ ] Placing a file in `incoming/` triggers sync cycle
- [ ] File appears on Windows drive after rebind
- [ ] KardiSynch picks up the file

### Scenario 1a (Ethernet)
- [ ] Windows detects RNDIS adapter
- [ ] Windows gets IP from dnsmasq (`ipconfig` shows 10.55.0.x)
- [ ] `\\10.55.0.1\kardisynch` is accessible from Windows Explorer
- [ ] `net use Z: \\10.55.0.1\kardisynch` works
- [ ] Files in `incoming/` are visible on Z:\
- [ ] KardiSynch picks up files from Z:\

### Scenario 1c (HID)
- [ ] Windows Device Manager shows HID device (no yellow bang)
- [ ] `hid-sender.py` runs without errors
- [ ] Companion app connects to HID device
- [ ] Test file sent from Pi appears in `C:\KardiSynch\incoming\`
- [ ] CRC matches (no corruption)
- [ ] KardiSynch picks up the file

---

## Performance Notes

| Scenario | Throughput | Latency (file available to KardiSynch) |
|----------|-----------|----------------------------------------|
| 2 (mass storage) | Disk speed (fast) | ~2s (rebind cycle) |
| 1a (ethernet) | ~5-10 MB/s USB 2.0 | <1s (immediate via share) |
| 1c (HID) | ~60 KB/s | file_size / 60KB + overhead |

For typical device interrogation files (10-500 KB), all scenarios deliver within seconds. HID is the slowest but still sub-10s for 500KB files.

---

## Security Considerations

- WiFi between Pi 3 and receiver Zero should use WPA2/3 with a strong PSK
- Samba share (1a) is read-only and guest-accessible — acceptable since the USB Ethernet link is point-to-point with no routing to the hospital network
- HID companion app (1c) validates CRC32 — not cryptographic, but sufficient for data integrity over a direct USB link
- Consider adding a shared HMAC key between Pi 3 and receiver for file authenticity if the WiFi network is shared
