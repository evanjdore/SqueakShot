# SqueakShot Deployment History

Field notes from setting up SqueakShot on the lab Pi 5 cluster. Keep here so the
context is not lost between sessions or machines. Update when you discover
something operationally useful.

---

## Hardware / network

Three Raspberry Pi 5 units with Camera Module 3 (IMX708), running Raspberry Pi
OS Bookworm. All three on the lab Wi-Fi / wired LAN.

Default IPs (update if reassigned):

| Camera | Role   | IP                |
|--------|--------|-------------------|
| cam0   | server | 155.58.221.8      |
| cam1   | client | 155.58.221.7      |
| cam2   | client | 155.58.221.9      |

SSH user on every Pi: `maiya`. Public key from the controller workstation is in
each Pi's `~/.ssh/authorized_keys`. If a new workstation needs access, run
`ssh-copy-id maiya@<ip>` from it first.

---

## Standard deploy + restart loop

After editing any Pi-side file (`sync_capture.py`, `camera_preview.py`,
`camera_settings.json` content via `camera_config.json`):

```bash
cd pi-deploy && ./install.sh && \
for ip in 155.58.221.8 155.58.221.7 155.58.221.9; do
  ssh -n maiya@$ip 'sudo systemctl restart squeakshot-record'
done
```

The `ssh -n` is important: without it, ssh inherits the stdin of the surrounding
`for` loop and can swallow subsequent iterations. Same reason `install.sh` uses
`ssh -n` internally.

---

## python3-av on the Pis

Required for `LibavH264Encoder` (the preferred software x264 path; avoids the
V4L2 0-byte-file issue on Pi 5). Already present on the lab Pi images. If a new
image is missing it:

```bash
ssh -n maiya@<pi_ip> 'sudo apt install -y python3-av'
```

Verify:

```bash
ssh -n maiya@<pi_ip> 'python3 -c "import av; print(av.__version__)"'
```

If `python3-av` is not installed, `sync_capture.py` falls back to the V4L2
`H264Encoder` and prints a warning at recording start. The fallback works on
many Pi 5 setups but has produced 0-byte `.h264` on others.

---

## Flask port (controller side)

Default is **5000**. On macOS, AirPlay Receiver can claim port 5000 (System
Settings -> General -> AirDrop & Handoff -> AirPlay Receiver). Either turn it
off or:

```bash
export SQUEAKSHOT_PORT=5050
./SqueakShot.sh
```

The launcher prints the chosen port on startup. Open the printed URL.

---

## Control port (Pi side)

cam0's `sync_capture.py` listens on **5006** for the controller. If the
controller logs "could not reach control port" for more than 60 s, check on the
Pi:

```bash
ssh -n maiya@<cam0_ip> 'sudo journalctl -u squeakshot-record -n 80 --no-pager'
```

Common causes:

- python3-av missing AND V4L2 encoder failing -> service exits during
  `start_encoder`.
- Camera Module ribbon not seated -> "no cameras available".
- NoiseReductionModeEnum import error on a fresh libcamera -> guarded as of
  v9.2.

---

## Clock skew

Pre-flight measures clock skew per Pi by capturing controller time, SSHing to
the Pi to capture its time, capturing controller time again, and computing the
midpoint. Default warning threshold: 100 ms.

Identical skew on all cams (say +120 ms on every Pi) almost always means the
**controller's clock** is off relative to the Pis, not the other way around.
Pis run chrony out of the box; Macs sometimes drift if AirPort/Wi-Fi flaps.

To check Pi time sync:

```bash
ssh -n maiya@<pi_ip> 'timedatectl' \
  && ssh -n maiya@<pi_ip> 'chronyc tracking 2>/dev/null || systemctl status systemd-timesyncd'
```

On macOS, install `sntp` via Homebrew if needed (it is in the same formula as
ntpd/chrony tooling on some systems; otherwise use the GNU `chrony` package):

```bash
brew install chrony   # provides chronyc and sntp-like client tools
# or compare directly:
date -u
ssh -n maiya@<cam0_ip> 'date -u'
```

If skew is consistent across all Pis and within ~150 ms, raise the threshold
rather than chasing it:

```bash
export SQUEAKSHOT_PREFLIGHT_CLOCK_SKEW_MS=200
```

---

## Cold-start timing budget

On a power-on of all three Pis, the first recording start can take ~45 s end to
end:

| Stage                                          | ~Duration |
|------------------------------------------------|-----------|
| Pi boot to systemd `squeakshot-record` ready   | 20-30 s   |
| First START -> Libav encoder ready on all Pis  | 5-15 s    |
| START reply propagating back to controller     | < 1 s     |

The controller waits up to 60 s for the control port and up to 60 s for the
START reply, so a cold start should not time out as long as the Pis are
booting normally.

For day-to-day recording (Pis already running), each cycle is sub-second.

---

## Files intentionally not committed

- `controller/camera_config.json` (contains real Pi IPs and the `maiya` user)
- `controller/camera_config.json.bak` (auto-rotated backup)
- `controller/logs/` (rotating run log)
- Screenshots of preflight dialogs etc. at repo root (delete or .gitignore)

`controller/camera_config.example.json` IS committed and serves as the template.

---

*Regenerate or amend this file when operational details change.*
