# SqueakShot

Synchronized multi-camera video recording for the Raspberry Pi, designed for
behavioral neuroscience experiments. Records H.264 with per-frame PTS
timestamps across N cameras with sub-frame synchronization, then provides a
desktop GUI for encoding, frame-matching, and trimming.

## What's new in v9.1

- **N cameras supported** (star topology: cam0 server + N clients)
- **ISP downscaling** at the Pi for lower bandwidth and processing load
  without losing field of view
- **Live preview tab** in the controller GUI (MJPEG streams from each Pi)
- **systemd-based service management** (no more SSH `nohup ... & disown`)
- See [CHANGELOG.md](CHANGELOG.md) for full migration notes

## Hardware

- Two or more Raspberry Pi 4 / 5 with Camera Module 3 (Wide recommended)
- Wired ethernet between all Pis and the desktop running the controller
- A desktop with Python 3.10+ and FFmpeg

## Quick start

```bash
# 1. One-time config (interactive)
./setup.sh                   # or setup.bat on Windows

# 2. Deploy capture scripts + systemd units to every Pi listed in config
cd pi-deploy && ./install.sh

# 3. Launch the controller (auto-installs deps if needed)
cd .. && ./SqueakShot.sh     # or SqueakShot.bat on Windows
```

Open <http://localhost:5000> in your browser.

## Architecture

```
                          ┌─────────────────────┐
                          │   Desktop Controller │
                          │  (Flask, port 5000)  │
                          └──────────┬──────────┘
                                     │ TCP 5006 (control)
                                     │ HTTP 8080 (preview)
                                     │ SSH (deploy / systemctl)
                                     ▼
                          ┌──────────────────────┐
                          │  cam0 (server, Pi)   │◄─── records
                          │  port 5005 ─ clients │
                          │  port 5006 ─ control │
                          │  port 8080 ─ preview │
                          └──┬───────────────┬───┘
                             │ TCP 5005      │
                             ▼               ▼
                       ┌──────────┐     ┌──────────┐
                       │   cam1   │     │   cam2   │
                       │  client  │     │  client  │
                       └──────────┘     └──────────┘
```

All cameras share a wall-clock reference. The server schedules a precise
start time ~3 seconds in the future, sends it to every client, waits for
ACKs, then everyone busy-waits to that exact moment before opening their
encoder. PTS files alongside each `.h264` give per-frame microsecond
timestamps for offline alignment.

## Output

- **Raw on Pi**: `~/camera_videos/<camN>_<animal>_<project>.{h264,pts}`
- **Encoded locally**: `~/SqueakShot_Videos/encoded/*.mp4`
- **Synced** (frame-matched across cameras): `~/SqueakShot_Videos/synced/*.mp4`
- **Trimmed** (final clipped clips): `~/SqueakShot_Videos/trimmed/*.mp4`

## Settings that matter

- `output_width` × `output_height`: actual recorded resolution
- `sensor_width` × `sensor_height`: leave at 2304×1296 to keep full lens FOV
- `framerate`: 56 fps is the maximum for the binned 2304×1296 sensor mode
- `bitrate_mbps`: 25 is plenty for 1536×864; raise for full-res

The ISP downscale trick: the camera reads the same 2304×1296 binned sensor
mode (full FOV) regardless of `output_width`/`output_height`, and the ISP
scales down before the encoder sees it. So lowering `output_*` reduces
encoder load and bandwidth, but keeps the lens view.

## Files

```
SqueakShot/
├── SqueakShot.sh / .bat / .command   # launchers
├── setup.sh / .bat                   # interactive config
├── controller/
│   ├── camera_controller.py          # Flask app
│   ├── camera_config.example.json    # template config
│   ├── requirements.txt
│   ├── templates/controller.html     # web UI
│   └── static/                       # logo etc.
├── pi-deploy/
│   ├── sync_capture.py               # capture service (multi-client server / client)
│   ├── camera_preview.py             # MJPEG preview server
│   ├── install.sh                    # deploys to every camera in config
│   └── services/
│       ├── squeakshot-record.service.template
│       └── squeakshot-preview.service.template
├── tools/
│   └── sync_videos.py                # standalone offline sync tool (GUI + CLI)
├── docs/
│   └── (installation guides etc.)
├── CHANGELOG.md
└── README.md
```

## Troubleshooting

**Cameras show as offline.** Check that `ssh user@ip` from your desktop works
without a password (set up keys with `ssh-copy-id`).

**Preview shows "not active".** The record service blocks the camera. Click
"Start Previews" on the Preview tab, it stops the record service first.

**Recording errors with "Clients did not ack".** Check that the
`squeakshot-record` service is running on every client Pi:
```bash
ssh user@cam1 'sudo systemctl status squeakshot-record'
```
If it shows "Permission denied" on systemctl from the controller, re-run
`pi-deploy/install.sh` to set up the passwordless sudo rule.

**Sync quality is "Fair" or worse.** Check whether one camera is dropping
frames (compare per-camera frame counts in the analysis output). Common
causes: under-volted Pi (check thermal cards in Recording tab), CPU
throttling, SD card too slow.
