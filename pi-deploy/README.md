# pi-deploy

Files that get pushed to each Raspberry Pi.

## Files

| File | Purpose |
|------|---------|
| `sync_capture.py` | Main capture service. Server mode for cam0, client mode for cam1+. |
| `camera_preview.py` | MJPEG preview server (port 8080). Mutually exclusive with sync_capture. |
| `services/squeakshot-record.service.template` | systemd unit for the record service. `install.sh` fills in `__USER__` and `__EXEC__`. |
| `services/squeakshot-preview.service.template` | systemd unit for preview. |
| `install.sh` | Deploys everything to all cameras in `../controller/camera_config.json`. |

## How `install.sh` works

For each camera in the config:
1. SCP `sync_capture.py`, `camera_preview.py`, and a Pi-side
   `camera_settings.json` (derived from the controller config's
   `camera_settings` section) to `/home/<user>/`.
2. Substitute placeholders into the service templates:
   - Server: `python3 sync_capture.py server --config ~/camera_settings.json`
   - Client: `python3 sync_capture.py client --server-ip <IP> --name <NAME> --config ~/camera_settings.json`
3. Install both unit files to `/etc/systemd/system/`, reload daemon, enable
   `squeakshot-record` (auto-start on boot). Preview is manual-start only.
4. Drop a sudoers file allowing the user to `sudo systemctl start/stop`
   those two units without a password (the controller calls this over SSH).

The two services declare `Conflicts=` against each other, so starting one
stops the other automatically.

## Manual deployment (advanced)

If `install.sh` doesn't fit your environment, the equivalent manual steps
on one Pi:

```bash
# On the desktop:
scp pi-deploy/sync_capture.py   maiya@cam0:/home/maiya/
scp pi-deploy/camera_preview.py maiya@cam0:/home/maiya/

# Create camera_settings.json on the Pi:
ssh maiya@cam0 'cat > ~/camera_settings.json' <<'EOF'
{
  "output_width": 1536,
  "output_height": 864,
  "sensor_width": 2304,
  "sensor_height": 1296,
  "framerate": 56,
  "bitrate_mbps": 25,
  "video_dir": "/home/maiya/camera_videos"
}
EOF

# Run sync_capture by hand to test:
ssh maiya@cam0 'python3 ~/sync_capture.py server'   # on cam0
ssh maiya@cam1 'python3 ~/sync_capture.py client --server-ip <CAM0-IP> --name cam1'
```

## Settings file (`camera_settings.json` on each Pi)

```json
{
  "output_width": 1536,    // ISP output resolution width
  "output_height": 864,    // ISP output resolution height
  "sensor_width": 2304,    // sensor mode width (preserves FOV)
  "sensor_height": 1296,   // sensor mode height
  "framerate": 56,         // locked framerate (FrameDurationLimits)
  "bitrate_mbps": 25,      // H.264 bitrate
  "video_dir": "/home/maiya/camera_videos"
}
```

The sensor mode at 2304×1296 is the full-FOV 2×2-binned mode for the IMX708
(Camera Module 3). Lowering `output_*` reduces encoder load and bandwidth
without affecting the lens field of view.
