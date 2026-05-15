# Installation Guide

This guide walks through setting up SqueakShot v9.1 from scratch on three Pis
plus a desktop controller.

## Prerequisites

### On every Raspberry Pi

- Raspberry Pi OS Bookworm (64-bit) with a working Camera Module 3
- A static or DHCP-reserved IP on the same network as the desktop
- Python 3.10+ with `picamera2` installed (default on Bookworm)
- SSH server enabled (`sudo raspi-config` → Interface Options → SSH)

Test the camera works at all:
```bash
libcamera-still -o test.jpg
```

Create the video directory:
```bash
mkdir -p ~/camera_videos
```

### On the desktop controller

- Python 3.10+
- FFmpeg and FFprobe on the PATH
- An SSH client that can reach each Pi by IP
- SSH key set up to each Pi (`ssh-copy-id user@cam0-ip`, etc.)

On macOS:
```bash
brew install ffmpeg
```
On Windows: download static FFmpeg builds from ffmpeg.org and add the
`bin/` folder to `Path`.

## Step 1: Run setup

From the top of the SqueakShot folder:
```bash
./setup.sh         # macOS / Linux
setup.bat          # Windows
```

It will ask:
- How many cameras (default 3)
- The IP, username, and name of each one. The first camera is always the
  server; the rest are clients.
- The remote video dir on the Pis (default `/home/<user>/camera_videos`)
- The local output dir on the desktop (default `~/SqueakShot_Videos`)
- Camera resolution, framerate, bitrate (defaults: 1536×864 @ 56 fps, 25 Mbps)

This writes `controller/camera_config.json`.

## Step 2: Verify SSH keys

For each Pi listed in the config:
```bash
ssh maiya@<cam-ip> 'echo OK'
```
If you get a password prompt, run `ssh-copy-id maiya@<cam-ip>` first.

The first SSH connection asks about host fingerprints; accept them or
pre-seed `~/.ssh/known_hosts`.

## Step 3: Deploy capture services to Pis

From the SqueakShot folder:
```bash
cd pi-deploy
./install.sh
```

This loops through every camera in your config and:
1. SCPs `sync_capture.py`, `camera_preview.py`, and a `camera_settings.json`
2. Installs `squeakshot-record.service` and `squeakshot-preview.service`
3. Enables `squeakshot-record` to auto-start on boot
4. Sets up a sudoers rule so the user can `sudo systemctl start/stop` those
   two units without a password (the controller needs this to manage
   services over SSH)

Reboot each Pi, or start the service manually:
```bash
ssh maiya@<cam-ip> 'sudo systemctl start squeakshot-record'
```

## Step 4: Launch the controller

```bash
./SqueakShot.sh          # macOS / Linux
SqueakShot.bat           # Windows
```

The first launch installs Flask and NumPy via `pip` if missing. Open
<http://localhost:5000>.

You should see one status card per camera. If they all show "Online" with
sensible thermal readings, you are good to go.

## Step 5: First recording

1. **Recording tab.** Enter an Animal ID and Project ID (e.g. `Mouse001`,
   `Test01`).
2. Click **Start Recording**. Within a few seconds, all cameras should show
   recording in their per-camera status. The big timer counts up.
3. Click **Stop Recording**. Files land at
   `/home/<user>/camera_videos/<camN>_Mouse001_Test01.h264` (and `.pts`) on
   every Pi.

## Step 6: Encode / Sync / Trim

These run on the desktop, not the Pis.

1. **Encode tab.** Select your recording → **Download from Pis** → after the
   download finishes select it under "Step 2: Encode to MP4" → **Start
   Encoding**.
2. **Sync tab.** Select the encoded recording → **Analyze** to see frame
   counts, matched frames, and quality → **Start Synchronization** to trim
   each camera to the matched window.
3. **Trim tab.** Select a synced recording → scrub to the start and end of
   the behavior bout you care about → **Trim Videos**.

## Step 7: Preview mode

1. **Preview tab.** Click **Start Previews**. The record service stops on
   every Pi, and each Pi spins up an MJPEG server on port 8080. The
   `<img>` tags in the GUI stream live video.
2. Click **Stop Previews** when done. To resume recording, click **Start
   Services** on the Recording tab (or just hit Start Recording, it
   auto-starts the services).

## Troubleshooting

### Camera shows offline after install

```bash
ssh maiya@<cam-ip>
sudo systemctl status squeakshot-record
journalctl -u squeakshot-record -n 50 --no-pager
```

Common causes:
- `picamera2` not installed (`sudo apt install python3-picamera2`)
- Another process owns the camera (`sudo fuser /dev/video*`)
- The settings file is missing or malformed (`cat ~/camera_settings.json`)

### Sync quality is "Fair"

The matching tolerance is 20 ms. Anything worse means frames are dropping
on at least one camera. Look at the per-camera frame counts in the analyze
output. The most common cause is a thermally throttled Pi, check the
thermal cards on the Recording tab.

### Preview doesn't show video

The browser may be blocked from cross-origin requests if you change ports.
The MJPEG server sends `Access-Control-Allow-Origin: *`, so plain `<img>`
embedding should work. If you only see the placeholder, verify:
```bash
ssh maiya@<cam-ip> 'sudo systemctl status squeakshot-preview'
```
and try `curl http://<cam-ip>:8080/status` from the desktop.

### "Already recording" or weird state

Click **Stop Services** on the Recording tab. If that fails:
```bash
ssh maiya@<cam-ip> 'sudo systemctl stop squeakshot-record squeakshot-preview'
```
Then click **Start Services** to come back up cleanly.
