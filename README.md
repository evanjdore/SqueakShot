<p align="center">
  <img src="docs/SqueakShotIcon2.png" alt="SqueakShot" width="280">
</p>

# SqueakShot

Synchronized multi-camera video recording for the Raspberry Pi, built for
behavioral neuroscience experiments (rodent vocalization + behavior, hence the
name) where several camera angles of the same animal must line up
frame-for-frame.

A set of Raspberry Pi 4 / 5 units — one *server* plus any number of *clients* —
record H.264 video with per-frame microsecond timestamps, all starting at the
same wall-clock instant. A desktop controller (a small Flask web app, currently
**v9.2.5**) drives the whole workflow from one browser UI: previewing,
recording, pulling footage off the Pis, encoding to MP4, frame-matching the
cameras to each other, and trimming to the behavior window.

## What it does

The full pipeline, all from `http://localhost:5000`:

1. **Record** — Enter an animal ID and project ID and hit *Start Recording*.
   Every Pi opens its encoder at the same scheduled moment (see
   [synchronization](#how-synchronization-works)). Footage lands on each Pi's
   SD card as a `.h264` file plus a `.pts` sidecar of per-frame timestamps.
2. **Preview** — Live MJPEG streams from every camera for framing and focus.
   Mutually exclusive with recording, since the cameras can't be shared.
3. **Encode** — Download the raw footage from every Pi in parallel and encode
   each camera to MP4.
4. **Sync** — Analyze the `.pts` files, match frames across all cameras to
   within a 20 ms tolerance, and write per-camera *synced* MP4s that begin on
   the same frame.
5. **Trim** — Scrub to the start and end of the behavior bout on a frame
   preview, then clip every camera together to that exact range.

Pre-flight checks (clock skew and free disk on each Pi), live thermal and
throttling status per camera, and an in-GUI settings deploy round it out.

## What's new

- **v9.2.5 (current)** — `.pts` sidecars are now written reliably on the Pi 5
  via a `TimestampingFileOutput` subclass, since `picamera2`'s
  `start_encoder(pts=...)` is silently ignored on the libav code path. Also
  ships a runtime monkey-patch for the `picamera2` + PyAV ≥14 incompatibility
  (`pict_type` must be an int, not the string `"I"`) that otherwise crashes
  the encoder on newer images. Quiets the bogus "force_key_frame unsupported"
  log line.
- **v9.2** — Fixes 0-byte `.h264` recordings on the Pi 5, an installer bug
  that deployed only the first camera, and server-side control-socket races.
  Adds `SQUEAKSHOT_PORT` and `SQUEAKSHOT_PREFLIGHT_CLOCK_SKEW_MS` environment
  variables (the former dodges macOS AirPlay on port 5000). Full detail in
  [CHANGELOG.md](CHANGELOG.md).
- **Isolated install** — The desktop controller installs into a conda/mamba
  environment (`environment.yml`) that also bundles FFmpeg, so nothing touches
  your system Python and there is no separate FFmpeg download.
- **Deploy settings from the GUI** — The Settings tab can push camera
  resolution/fps/bitrate to every Pi and restart the record service, instead
  of re-running `install.sh`.
- Inline pre-flight results, per-camera preview reload, parallel encode /
  sync / trim, and assorted bug fixes (see `CHANGELOG.md`).

## Hardware

- Two or more Raspberry Pi 4 / 5 running Raspberry Pi OS Bookworm (64-bit)
- A Camera Module 3 (IMX708, Wide variant recommended) on each Pi
- Wired Ethernet between every Pi and the desktop running the controller
  (Wi-Fi works but degrades preview latency and pre-flight clock-skew margin)
- A desktop (Windows / macOS / Linux) for the controller — modest CPU is fine,
  the heavy lifting is FFmpeg encode/sync on a small number of camera streams

For the reference physical rig — a 3D-printed three-camera enclosure built
around a single mouse cage — see the **[hardware build guide](BUILD.md)**. It
covers the full bill of materials, dimensions, wiring, and assembly, and the
printable `.3mf` / editable `.f3d` files live in [`hardware/`](hardware/).

## Installation

See [INSTALL.md](INSTALL.md) for the full walkthrough. The short version:

### Prerequisites

**On each Raspberry Pi** — Raspberry Pi OS Bookworm (64-bit), a working Camera
Module 3, SSH enabled, and `python3-picamera2` + `python3-av` installed (both
ship on standard Bookworm images). On the Pi 5 specifically, `python3-av` is
not optional — without it the recorder falls back to the V4L2 H.264 path,
which has produced 0-byte `.h264` files on some libcamera builds.

**On the desktop** — conda or mamba (recommended, e.g. via Miniforge). The
controller's Python, Flask, NumPy, and FFmpeg all come from the
`environment.yml` conda environment. Without conda you instead need Python
3.10+ and FFmpeg/FFprobe on `PATH` yourself (the launcher will pip-install
Flask + NumPy in that case).

You also need passwordless SSH from the desktop to every Pi
(`ssh-copy-id user@pi-ip`) and the Pis on NTP / chrony so their wall clocks
agree to within ~100 ms (pre-flight check enforces this).

### 1. Configure

```bash
./setup.sh                   # setup.bat on Windows
```

Interactively asks for each camera's name / IP / SSH user, the video
directories, and the resolution / fps / bitrate, then writes
`controller/camera_config.json`.

### 2. Deploy to the Pis

```bash
cd pi-deploy && ./install.sh
```

Copies the capture scripts and a generated `camera_settings.json` to every Pi,
installs the `squeakshot-record` and `squeakshot-preview` systemd services,
and grants passwordless `systemctl` for just those two units.

### 3. Launch the controller

```bash
cd .. && ./SqueakShot.sh     # SqueakShot.bat on Windows
```

On the first run this builds the `squeakshot` conda environment from
`environment.yml` (a minute or two); later runs reuse it. If conda/mamba is
not found, it falls back to installing Flask + NumPy with `pip`. Then open
<http://localhost:5000>.

To build the environment yourself ahead of time:

```bash
conda env create -f environment.yml      # or: mamba env create -f environment.yml
```

For day-to-day use after install, see [QUICKSTART.md](QUICKSTART.md).

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

The controller never talks to client cameras directly: it sends one control
command to the server (cam0), which fans it out to every client. Clients
identify themselves to the server with a `HELLO:<name>` handshake, so cameras
are matched by name rather than connection order.

## How synchronization works

All cameras share a wall-clock reference, so keep the Pis on NTP / chrony. On
*Start Recording* the server schedules a precise start time about 3 seconds in
the future, sends it to every client, waits for ACKs, then everyone busy-waits
to that exact instant before opening their encoder. The `.pts` sidecar next to
each `.h264` records a microsecond timestamp per frame (written by
`TimestampingFileOutput`, a `FileOutput` subclass — `picamera2`'s built-in
`pts=` kwarg is silently ignored on the libav path used by the Pi 5). The
Sync stage uses those timestamps to match frames across cameras offline,
within a 20 ms tolerance.

Heartbeats keep the cluster honest: the server sends `PING` every 10 s during
recording; any client that hears nothing for 30 s auto-stops, so a crashed
server doesn't quietly fill SD cards.

The pre-flight check flags any Pi whose clock is off by more than ~100 ms
(tunable via `SQUEAKSHOT_PREFLIGHT_CLOCK_SKEW_MS`), since skew directly
degrades sync quality.

## Output

- **Raw on Pi**: `~/camera_videos/<camN>_<animal>_<project>.{h264,pts}`
- **Raw pulled to desktop**: `~/SqueakShot_Videos/raw/*.{h264,pts}`
- **Encoded locally**: `~/SqueakShot_Videos/encoded/*.mp4`
- **Synced** (frame-matched across cameras): `~/SqueakShot_Videos/synced/*.mp4`
- **Trimmed** (final clipped clips): `~/SqueakShot_Videos/trimmed/*.mp4`

## Settings that matter

- `output_width` × `output_height`: actual recorded resolution (default 1536×864)
- `sensor_width` × `sensor_height`: leave at 2304×1296 to keep full lens FOV
- `framerate`: 56 fps is the maximum for the binned 2304×1296 sensor mode
  on the IMX708
- `bitrate_mbps`: 25 is plenty for 1536×864; raise for full-res

The ISP downscale trick: the camera reads the same 2304×1296 binned sensor
mode (full FOV) regardless of `output_width`/`output_height`, and the IMX708
ISP scales down before the encoder sees it. So lowering `output_*` reduces
encoder load and bandwidth, but keeps the lens view.

Changing these in the **Settings** tab updates the controller's config; click
**Deploy to Pis** there to copy them to every camera and restart the record
service so they take effect. This is the same operation as re-running
`pi-deploy/install.sh`, but in-place from the GUI.

## Files

```
SqueakShot/
├── SqueakShot.sh / .bat / .command   # launchers
├── setup.sh / .bat                   # interactive config
├── environment.yml                   # conda/mamba env for the controller
├── controller/
│   ├── camera_controller.py          # Flask app
│   ├── sync_lib.py                   # shared N-camera frame-matching algorithm
│   ├── camera_config.example.json    # template config
│   ├── requirements.txt              # pip fallback deps
│   └── templates/controller.html     # web UI
├── pi-deploy/
│   ├── sync_capture.py               # capture service (multi-client server / client)
│   ├── camera_preview.py             # MJPEG preview server
│   ├── install.sh                    # deploys to every camera in config
│   ├── README.md                     # Pi-side files reference + manual deploy notes
│   └── services/
│       ├── squeakshot-record.service.template
│       └── squeakshot-preview.service.template
├── tools/
│   └── sync_videos.py                # standalone offline sync tool (GUI + CLI)
│                                     # — usable without the controller, e.g. for
│                                     # post-hoc resync of older recordings
├── hardware/                         # 3D-print + CAD files for the physical rig
│   ├── print/                        # print-ready .3mf (housing + Pi case)
│   ├── cad/                          # editable Fusion 360 .f3d sources
│   └── README.md                     # hardware file index
├── docs/
│   ├── SqueakShotIcon2.png           # project logo (README header + controller UI)
│   └── images/                       # build-guide renders
├── BUILD.md                          # hardware build guide (BOM, dimensions, assembly)
├── INSTALL.md                        # full installation walkthrough
├── QUICKSTART.md                     # day-to-day usage
├── DEPLOYMENT_HISTORY.md             # field notes
├── CHANGELOG.md
└── README.md
```

## Troubleshooting

**Cameras show as offline.** Check that `ssh user@ip` from your desktop works
without a password (set up keys with `ssh-copy-id`).

**Preview shows "not active".** The record service blocks the camera. Click
"Start Previews" on the Preview tab — it stops the record service first.

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

**Settings changes don't take effect.** Editing settings in the GUI only
updates the controller's config. Use **Deploy to Pis** on the Settings tab (or
re-run `pi-deploy/install.sh`) to push them to the cameras.
