# Changelog

## v9.1: Bug fixes, pre-flight checks, parallel pipeline

No breaking config changes. Re-run `pi-deploy/install.sh` to push the updated
`sync_capture.py` and refreshed sudoers rule to every Pi. Controller will use
the new wire format automatically; Pis on v9.0 `sync_capture.py` will reject
the new `START:name=fn:...` format, so both sides must update together.

### Bug fixes

- **Renamed cameras now work.** The `START` command from controller to server
  now carries `name=filename` pairs instead of positional ordering. The server
  matches clients by name in the `clients` dict, so renaming a camera in the
  settings UI no longer breaks recording start.
- **Sudoers rule covers both systemctl paths.** Bookworm sudo may resolve
  `systemctl` to either `/bin/systemctl` or `/usr/bin/systemctl`. The installer
  now writes both variants so service start/stop works regardless.
- **Sync warns on frame-count discrepancy.** The matching algorithm still clips
  to the shortest camera (it has to), but the analyze step now reports a
  warning when one camera has noticeably fewer frames than the others, so
  silent data loss is visible.
- **Robust file listing.** Recordings list switched from `ls -lh` parsing to
  `find -printf` so filenames with unusual characters don't break the parser.

### Robustness

- **Pre-flight check before recording.** Each Pi is queried for clock skew
  (vs. controller) and free disk space in the video directory. Recording is
  blocked with a 409 if any Pi has a clock off by >100 ms or less than 30 GB
  free; the user can override with a "Record anyway" confirmation. A new
  Pre-flight Check button runs the same check on demand.
- **Heartbeat from server to clients.** Server sends `PING` every 10 s during
  the recording loop. If a client doesn't hear from the server for 30 s
  (server crashed, network died), it auto-stops its own recording instead of
  filling the SD card.
- **Recordings list is now parallel.** One SSH call per camera, run in
  parallel via ThreadPoolExecutor. With 3 cameras and 30 recordings, this
  drops from ~90 SSH calls to 3.
- **Parallel encode / sync / trim.** All three stages now process cameras in
  parallel instead of serial. With 3 cameras this is roughly a 3× wall-clock
  speedup on the workstation.

### Architecture

- **Matching algorithm moved to `controller/sync_lib.py`.** Both the controller
  and `tools/sync_videos.py` now import from there; the duplicated copy in
  `sync_videos.py` is gone. New `analyze_frame_counts()` helper backs the
  discrepancy warning.
- **`preview_running` reflects ground truth.** The status flag is now derived
  from the actual port 8080 probe results across cameras, not a local flag.
  If someone starts the preview service on a Pi manually, the UI sees it.
- **Atomic config save with backup.** `camera_config.json` is now written via
  temp file + `os.replace()`, with `.bak` of the previous version preserved.
  Interrupted saves no longer corrupt the file.
- **Persistent rotating log.** Controller now writes to
  `controller/logs/controller.log` (2 MB × 5 files rotating). The in-memory
  log behavior is unchanged.

### UI

- **Trim frame counters update on direct input.** Typing a number into the
  start/end frame fields refreshes the "Selected: N frames" counter
  immediately, not just when you click Set.

### Intentionally skipped

- No auth on the controller / server. Lab network is trusted; revisit if you
  ever expose this to the broader campus VLAN.
- `install.sh` always overwrites the Pi-side `camera_settings.json`. That file
  is derived from the main `camera_config.json`, so manual edits on the Pi
  are by design clobbered when you re-run the installer. Edit the main config
  and re-install.

---

## v9.0: Multi-camera star topology + ISP downscale + clean pipeline

### Breaking changes

**Config schema.** The old flat keys (`cam0_ip`, `cam1_ip`, `cam0_user`, ...) are
gone. The new format uses a `cameras[]` array. Existing configs are
auto-migrated on first load by the controller, so no manual action is required
unless you wrote scripts that read the JSON directly.

Old:
```json
{
  "cam0_ip": "1.2.3.4",
  "cam1_ip": "1.2.3.5",
  "cam0_user": "maiya",
  "camera_settings": {"width": 2304, "height": 1296, "bitrate": 25}
}
```

New:
```json
{
  "cameras": [
    {"name": "cam0", "ip": "1.2.3.4", "user": "maiya", "role": "server"},
    {"name": "cam1", "ip": "1.2.3.5", "user": "maiya", "role": "client"},
    {"name": "cam2", "ip": "1.2.3.6", "user": "maiya", "role": "client"}
  ],
  "camera_settings": {
    "output_width": 1536,
    "output_height": 864,
    "sensor_width": 2304,
    "sensor_height": 1296,
    "bitrate_mbps": 25,
    "framerate": 56
  }
}
```

**Service control.** Pi-side services are now managed via `systemctl` rather
than SSH-ing in and running `nohup ... & disown`. Re-running `pi-deploy/install.sh`
installs `squeakshot-record.service` and `squeakshot-preview.service` on each
Pi and grants the user passwordless sudo for `systemctl start/stop` on those
two units only.

**sync_capture.py CLI.** Client mode now requires `--name`:
```
# old
python3 sync_capture.py client --server-ip 1.2.3.4

# new
python3 sync_capture.py client --server-ip 1.2.3.4 --name cam1
```

### New features

- **Third camera (and more).** Server (cam0) accepts N clients (cam1, cam2, ...).
  Clients identify themselves with a `HELLO:<name>` handshake on connect.
- **ISP downscaling with FOV preservation.** Output resolution defaults to
  1536×864 while the sensor mode stays at 2304×1296. The ISP scales the binned
  sensor stream down before encoding. Result: same field of view, ~55% fewer
  pixels through the encoder, lower bandwidth, lower Pi load.
- **Preview tab.** Camera previews are now visible in the controller GUI.
  Click "Start Previews" on the Preview tab to spin up the MJPEG streams on
  all Pis. The record service is automatically stopped first (the camera
  cannot be shared). Click "Start Recording" to swap back.
- **Per-camera status everywhere.** Recording status, thermal status, and
  recordings list all dynamically render one card per camera based on the
  current config.

### Cleanup

Files removed:
- `tools/encode.py` (dead MJPEG code from a previous pipeline)
- `controller/diagnose.sh` (hardcoded IPs)
- `controller/fix_previews.sh` (hardcoded IPs)
- `controller/camera_preview.py` (duplicate of `pi-deploy/camera_preview.py`)
- `controller/start.sh`, `controller/start_windows.bat`,
  `controller/start_miniforge.bat` (redundant launchers, use the top-level
  `SqueakShot.sh` / `SqueakShot.bat` / `SqueakShot.command`)
- `pi-deploy/services/squeakshot-server.service` and
  `pi-deploy/services/squeakshot-client.service` (replaced by a single
  templated `squeakshot-record.service.template`)

Two parallel sync algorithms collapsed into one. The matching function in
`tools/sync_videos.py` is now the canonical implementation; the controller
calls the same logic.

### Migration

For an existing 2-camera setup:
1. Pull the new repo or unzip on top of the old one.
2. Run `./setup.sh` (or `setup.bat`) and re-enter your camera info. Or just
   launch the controller, it auto-migrates the old config.
3. Re-run `pi-deploy/install.sh` to push the new sync_capture.py and install
   the systemd units.
4. Reboot the Pis (or run `sudo systemctl start squeakshot-record` on each).
