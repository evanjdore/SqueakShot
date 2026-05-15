# Changelog

## v9.2: Pi 5 recording fixes, deployment fixes, Mac compatibility

This release fixes a critical bug where Pi 5 cameras would create `.h264` files
that were 0 bytes (no data written), plus a stdin bug in the installer that
caused only the first camera to receive updates.

Re-run `pi-deploy/install.sh` to push the new Pi-side service to every camera,
and verify python3-av is installed on each Pi (`sudo apt install python3-av`).
Both sides must update together (the controller's server-deadlock fix lives in
sync_capture.py, not in the controller).

### Critical bug fixes

- **Empty .h264 recordings on Pi 5.** Two interacting causes:
  1. `PTSOutput.outputframe` was calling `FileOutput.outputframe`, which has a
     "first frame must be a keyframe" gate. On some Pi 5 libcamera builds this
     check dropped every NAL unit, producing a 0-byte file. `PTSOutput` now
     calls `FileOutput._write` directly to bypass the gate.
  2. The V4L2 `H264Encoder` on Pi 5 can hand the encoder pipeline zero-length
     buffers. New `create_h264_encoder()` prefers `LibavH264Encoder` (software
     x264 via PyAV) when `python3-av` is installed, with the V4L2 encoder as
     a fallback. `iperiod` is set from FPS, `framerate` is passed as
     `fractions.Fraction` (not float, which broke PyAV with
     "'float' object has no attribute 'numerator'"), and `force_key_frame()` is
     nudged after `start_encoder` to make the first NAL an IDR.

- **install.sh deployed only the first camera.** The `while read` loop reads
  from `cameras.tsv`; every `ssh` call inside the loop inherited that stdin and
  consumed the remaining lines. All `ssh` calls inside the loop now use
  `ssh -n` (stdin from `/dev/null`).

- **NoiseReductionModeEnum missing on newer libcamera.** Setting the control
  unconditionally crashed camera setup on some Pi 5 builds. Now guarded with
  `getattr(controls, "NoiseReductionModeEnum", None)`.

- **encode="main" explicit.** `create_video_configuration` now explicitly
  targets the main YUV stream for H264 encoding instead of relying on the
  default, which has varied across picamera2 versions.

### Server deadlock and control socket races

- **Worker thread for control commands.** The server's main `select()` loop
  used to call `_handle_control_command` directly, blocking Picamera2 servicing
  during `wait_until` and `start_encoder`. Commands now run on a dedicated
  daemon worker thread fed by a queue.
- **`control_busy` Event.** While the worker is processing a command (and
  possibly sending `OK:`/`ERROR:`), the main `select()` loop omits the control
  socket from the read list. This eliminates `[Errno 9] Bad file descriptor`
  and other races where the main thread `recv()`'d concurrently with the
  worker's `send()`.
- **`control_holder` dict.** Replaces a stale `control_conn` local that could
  point at a closed socket when the worker reconnected the controller.
- **Initial STATUS send is blocking.** `_accept_controller` flips the new
  socket to blocking just long enough to send the initial `STATUS:` line, then
  back to non-blocking. Prevents `BlockingIOError` on `sendall` immediately
  after `accept()`.
- **`recv_message` timeout 0.1s -> 5.0s** on control reads. The old value was
  too tight under lab Wi-Fi / SSH muxing.

### Controller (workstation side)

- **`SQUEAKSHOT_PORT` env var** (default 5000) for the Flask UI. Set to
  something else if macOS AirPlay receiver is enabled on port 5000.
- **`SQUEAKSHOT_PREFLIGHT_CLOCK_SKEW_MS` env var** (clamped 50..2000, default
  100). Lets you relax the threshold when controller-vs-Pi skew is stable but
  just over 100 ms.
- **60s wait for cam0:5006** after `systemctl start squeakshot-record`, with
  "Still waiting..." log lines every 10 s. Cold-start camera init plus first
  Libav encoder load can take 30s+.
- **60s timeout on START reply** (was 15s). First Libav startup is slow.
- **Encode pipeline:** skip 0-byte `.h264` with a clear journalctl hint instead
  of opaque ffmpeg error. `ffmpeg -hide_banner -loglevel error`. Failure log
  shows last 1500 chars of stderr.
- **Dedup `ERROR: ERROR:`** when server reply already starts with `ERROR:`.

### Operational notes

- **`python3-av` required on Pis** for the preferred Libav encoder path.
  Already present on standard Raspberry Pi OS images. If missing:
  `sudo apt install python3-av`. Falls back to V4L2 encoder with a log warning
  if unavailable.
- See `DEPLOYMENT_HISTORY.md` for IPs, install flow, Mac sntp path, Chrony vs
  `timedatectl`, and other field notes.


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
