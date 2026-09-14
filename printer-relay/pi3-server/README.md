# Pi3 Print Server Setup

Raspberry Pi 3 acting as WiFi AP, CUPS print server, and NTP time source for the Pi Zero relay.

## Network layout

```
Main LAN (eth0)  192.168.179.110
Relay subnet     10.99.0.0/24
  Pi3 wlan0      10.99.0.1   (AP: ssid=relayap, hidden)
  Pi Zero        10.99.0.2   (static)
```

## Initial setup

Run once on a fresh Raspberry Pi OS install:

```bash
sudo bash setup-cups.sh
```

Then plug in the USB laser printer — it will be detected and configured automatically (see USB auto-configure below). No manual `lpadmin` needed.

## USB printer auto-configure

`setup-cups.sh` installs a udev rule (`/etc/udev/rules.d/99-usb-printer.rules`) that fires whenever a USB printer (class 07) is plugged in. It starts `cups-autoconf-printer.service`, which runs `/usr/local/sbin/cups-autoconf-printer.sh`:

1. Retries `lpinfo -v` until the printer appears (up to 20 seconds)
2. Updates the `LaserPrinter` CUPS queue to the detected USB URI
3. Enables and accepts the queue, sets it as default

This means any USB printer works without site-specific configuration — just plug in and within a few seconds CUPS points at it.

To trigger manually (e.g. after setup-cups.sh on a printer already plugged in):

```bash
sudo /usr/local/sbin/cups-autoconf-printer.sh
lpstat -v   # confirm URI updated
```

To check what happened after a plug-in event:

```bash
journalctl -t printer-autoconf
```

## NTP server (chrony)

The Pi Zero has no RTC and no direct internet access, so it gets the time from the Pi3 via NTP. The Pi3 must serve NTP on the relay subnet — including when it has no internet connection itself.

Install and configure chrony:

```bash
sudo apt-get install -y chrony
sudo tee -a /etc/chrony/chrony.conf <<'EOF'

# Serve local clock to relay subnet even without internet NTP access
local stratum 10
allow 10.99.0.0/24
EOF
sudo systemctl restart chrony
```

`local stratum 10` makes chrony serve its own system clock when no upstream NTP source is reachable. The Pi Zero display daemon queries `10.99.0.1:123` on every 60-second refresh.

Verify:

```bash
ss -ulnp | grep 123          # should show chronyd listening
chronyc tracking             # shows sync status
```

## Printer configuration web UI

`setup-cups.sh` installs `printer-config-server.py` as a systemd service (`printer-config.service`) listening on port 80. It is accessible at:

```
http://10.99.0.1/
```

from any device connected to the relayap WiFi — phone, laptop, anything with a browser.

The page shows the currently active CUPS printer and all detected USB printers, with a **Use** button to switch the `LaserPrinter` queue to a different device. Useful when deploying at a new site with a different printer model.

**Typical workflow at a new site:**
1. Connect the USB printer to the Pi3
2. Connect your phone or laptop to relayap (`RelayPrint#1`)
3. Open `http://10.99.0.1/` — the printer should appear automatically (via udev, see above)
4. If already selected (✓), nothing to do — start printing

If the printer does not appear, refresh the page after a few seconds. If it still doesn't appear, trigger auto-configure manually:

```bash
sudo /usr/local/sbin/cups-autoconf-printer.sh
```

## AP passphrase

Default: `RelayPrint#1` — change in `/etc/hostapd/hostapd.conf` and update
`/etc/wpa_supplicant/wpa_supplicant.conf` on the Pi Zero image accordingly.
