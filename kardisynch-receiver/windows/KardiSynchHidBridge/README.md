# KardiSynch HID Bridge (Windows companion)

Reassembles the USB-HID report stream from the Pi Zero receiver
(`../../hid/hid_sender.py`) into whole files in a folder KardiSynch watches, and
ACKs each file back to the Pi so delivery is reliable (unacked files are
redelivered by the hub).

Runs on locked-down clinical PCs where USB **mass-storage and USB-NIC are
blocked but HID is allowed**. No driver install —
Windows' built-in HID class driver is used via [HidSharp](https://www.nuget.org/packages/HidSharp).

## Build

```powershell
dotnet build -c Release
```

## Run

```powershell
# default output C:\KardiSynch\incoming
dotnet run -c Release

# or a custom folder (point KardiSynch's watcher at the same path)
dotnet run -c Release -- "D:\KardiSynch\incoming"
```

Self-contained single exe for deployment:

```powershell
dotnet publish -c Release -r win-x64 --self-contained -o .\publish
```

Install as an always-on service (e.g. with [NSSM](https://nssm.cc/)):

```powershell
nssm install KardiSynchHidBridge "C:\path\to\KardiSynchHidBridge.exe" "C:\KardiSynch\incoming"
nssm start KardiSynchHidBridge
```

## How it matches the Pi

- **Device match:** VID `0x1D6B` / PID `0x0104` — identical to `../../hid/gadget-hid.sh`.
  Change both together if you ever repurpose the IDs.
- **Protocol:** 64-byte reports `FILE_START → FILE_DATA* → FILE_END`, then this app
  sends a `PKT_ACK` (0x05) **output** report with `status` + CRC32. CRC32 is the
  IEEE/reflected variant (matches Python `zlib.crc32`).
- **Atomic writes:** files land as `name.part` then are renamed into place, so
  KardiSynch never imports a half-written file (its watcher moves/deletes what it
  imports, so this folder is a drain).

## Troubleshooting

- **Device not found:** confirm the Pi shows the HID gadget (Device Manager →
  Human Interface Devices, no yellow bang) and that VID/PID match.
- **CRC mismatches:** raise `SEND_DELAY` in the Pi's `receiver.conf` (the host HID
  ring may be dropping reports under load).
- **Files re-arrive:** the Pi didn't get an ACK (app was closed / PC asleep during
  `FILE_END`). Expected — the hub redelivers; the atomic write prevents dupes from
  corrupting an in-progress import.
