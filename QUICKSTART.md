# Quick Start

Daily use, assuming everything is installed and configured.

## Recording

1. Power on all Pis. The `squeakshot-record` service starts automatically.
2. Launch the controller: `./SqueakShot.sh` (or `SqueakShot.bat`).
3. Open <http://localhost:5000>.
4. Make sure all cameras show **Online**.
5. Recording tab → enter Animal ID and Project ID → **Start Recording**.
6. **Stop Recording** when done.

## Post-processing

After a session, on the Encode tab:

1. Select your recording from the dropdown.
2. **Download from Pis**.
3. Once downloaded, select it under Step 2 and click **Start Encoding**.
4. Switch to the Sync tab, select the same recording, click **Analyze**.
   If sync quality is Good or Excellent, click **Start Synchronization**.
5. Switch to the Trim tab if you want to clip the behavior window.

## Preview (no recording)

Preview tab → **Start Previews** → live MJPEG from each Pi.

To go back to recording: **Stop Previews** → Recording tab → record as normal.

## Files

Default locations on the controller:

```
~/SqueakShot_Videos/
├── raw/        # H.264 + PTS pulled from Pis
├── encoded/    # MP4 + PTS
├── synced/     # frame-matched MP4s
└── trimmed/    # final clipped MP4s
```

On each Pi:

```
~/camera_videos/<camN>_<animal>_<project>.h264
~/camera_videos/<camN>_<animal>_<project>.pts
```

## Common commands

Check service status on a Pi:
```bash
ssh maiya@<cam-ip> 'sudo systemctl status squeakshot-record'
```

Tail logs on a Pi:
```bash
ssh maiya@<cam-ip> 'journalctl -u squeakshot-record -f'
```

Restart a Pi service from the desktop:
```bash
ssh maiya@<cam-ip> 'sudo systemctl restart squeakshot-record'
```

Run the standalone offline sync tool:
```bash
python tools/sync_videos.py
```
