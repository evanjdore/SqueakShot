#!/usr/bin/env python3
"""
SqueakShot Camera Controller v9.2

Changes from v9.1:
- SQUEAKSHOT_PORT env var (default 5000) for Flask UI; avoids AirPlay clash on Mac
- SQUEAKSHOT_PREFLIGHT_CLOCK_SKEW_MS env var (clamped 50..2000, default 100)
- start_sync_services waits up to 60s for cam0:5006 with progress logs
  (libcamera + Picamera2 + Libav cold start can take 30s+)
- START reply timeout 15s -> 60s (first Libav encoder start is slow)
- Encode skips 0-byte .h264 with a clear journalctl hint instead of cryptic
  ffmpeg "Invalid data" error
- ffmpeg called with -hide_banner -loglevel error; failure log shows last 1500
  chars of stderr (real errors, not version banner)
- Server reply already prefixed with ERROR: is logged once (no ERROR: ERROR:)

Changes from v9.0:
- START protocol uses name=filename pairs so renamed cameras work
- Pre-flight check: clock skew + free disk space before recording
- Parallel encoding across cameras
- Sync analyze warns on frame-count discrepancy (silent-clip detection)
- Preview-running state derived from actual port probe, not local flag
- Robust file listing (no ls -lh parsing)
- Config backup on every save
- Persistent rotating log to controller/logs/controller.log
- Matching algorithm moved to sync_lib (deduped)
"""

from flask import Flask, render_template, jsonify, request, send_file, Response
import subprocess
import socket
import os
import json
import time
import shutil
import tempfile
import threading
import logging
import logging.handlers
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path

import sync_lib

app = Flask(__name__)

CONFIG_FILE = "camera_config.json"
LOG_DIR = "logs"

DEFAULT_CONFIG = {
    "cameras": [
        {"name": "cam0", "ip": "", "user": "maiya", "role": "server"},
        {"name": "cam1", "ip": "", "user": "maiya", "role": "client"},
        {"name": "cam2", "ip": "", "user": "maiya", "role": "client"},
    ],
    "video_dir": "/home/maiya/camera_videos",
    "local_video_dir": os.path.join(os.path.expanduser("~"), "SqueakShot_Videos"),
    "camera_settings": {
        "output_width": 1536, "output_height": 864,
        "sensor_width": 2304, "sensor_height": 1296,
        "framerate": 56, "bitrate_mbps": 25,
    },
}

CONTROL_PORT = 5006
PREVIEW_PORT = 8080
CONNECTION_CHECK_INTERVAL = 15

# Flask serving port for the controller UI. macOS AirPlay receiver claims port 5000
# by default but most users keep it disabled; if you collide with anything else,
# set SQUEAKSHOT_PORT before launch (export SQUEAKSHOT_PORT=5050 etc).
SQUEAKSHOT_PORT = int(os.environ.get("SQUEAKSHOT_PORT", "5000"))

# Default 100 ms is fine for a healthy chrony / systemd-timesyncd setup. Some labs
# see a stable but slightly higher offset (Mac controller vs Pi over SSH timing
# measurement is noisy). Set SQUEAKSHOT_PREFLIGHT_CLOCK_SKEW_MS to override.
_clock_env = os.environ.get("SQUEAKSHOT_PREFLIGHT_CLOCK_SKEW_MS")
if _clock_env:
    try:
        PREFLIGHT_CLOCK_SKEW_WARN_MS = max(50, min(2000, int(_clock_env)))
    except ValueError:
        PREFLIGHT_CLOCK_SKEW_WARN_MS = 100
else:
    PREFLIGHT_CLOCK_SKEW_WARN_MS = 100
PREFLIGHT_MIN_FREE_GB = 30


# Persistent log setup
os.makedirs(LOG_DIR, exist_ok=True)
_disk_logger = logging.getLogger("squeakshot")
_disk_logger.setLevel(logging.INFO)
_handler = logging.handlers.RotatingFileHandler(
    os.path.join(LOG_DIR, "controller.log"), maxBytes=2_000_000, backupCount=5
)
_handler.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s"))
_disk_logger.addHandler(_handler)


state = {
    "is_recording": False, "start_time": None,
    "logs": {"server": [], "encode": [], "sync": [], "trim": [], "preview": []},
    "control_socket": None, "service_running": False,
    "animal_id": "", "project_id": "",
    "is_encoding": False, "encode_message": "",
    "is_syncing": False, "sync_message": "",
    "last_preflight": None,
}
state_lock = threading.Lock()

connection_cache = {"cameras": {}, "last_check": 0, "preview_status": {}}
cache_lock = threading.Lock()


def load_config():
    if not os.path.exists(CONFIG_FILE):
        return DEFAULT_CONFIG.copy()
    with open(CONFIG_FILE, "r") as f:
        cfg = json.load(f)
    if "cameras" not in cfg and "cam0_ip" in cfg:
        cfg = _migrate_old_config(cfg)
        save_config(cfg)
        print("[config] Migrated old flat-key config to cameras[] array")
    if "local_video_dir" not in cfg:
        cfg["local_video_dir"] = DEFAULT_CONFIG["local_video_dir"]
    if "cameras" not in cfg:
        cfg["cameras"] = list(DEFAULT_CONFIG["cameras"])
    if "camera_settings" not in cfg:
        cfg["camera_settings"] = dict(DEFAULT_CONFIG["camera_settings"])
    else:
        cs = cfg["camera_settings"]
        if "width" in cs and "output_width" not in cs:
            cs["output_width"] = cs.pop("width")
        if "height" in cs and "output_height" not in cs:
            cs["output_height"] = cs.pop("height")
        if "bitrate" in cs and "bitrate_mbps" not in cs:
            cs["bitrate_mbps"] = cs.pop("bitrate")
        for k, v in DEFAULT_CONFIG["camera_settings"].items():
            cs.setdefault(k, v)
    return cfg


def _migrate_old_config(cfg):
    cameras = []
    if cfg.get("cam0_ip"):
        cameras.append({"name": cfg.get("cam0_name", "cam0") or "cam0",
                        "ip": cfg["cam0_ip"], "user": cfg.get("cam0_user", "maiya"),
                        "role": "server"})
    if cfg.get("cam1_ip"):
        cameras.append({"name": cfg.get("cam1_name", "cam1") or "cam1",
                        "ip": cfg["cam1_ip"], "user": cfg.get("cam1_user", "maiya"),
                        "role": "client"})
    new_cfg = {
        "cameras": cameras,
        "video_dir": cfg.get("video_dir", DEFAULT_CONFIG["video_dir"]),
        "local_video_dir": cfg.get("local_video_dir", DEFAULT_CONFIG["local_video_dir"]),
        "camera_settings": cfg.get("camera_settings", {}),
    }
    cs = new_cfg["camera_settings"]
    if "width" in cs: cs["output_width"] = cs.pop("width")
    if "height" in cs: cs["output_height"] = cs.pop("height")
    if "bitrate" in cs: cs["bitrate_mbps"] = cs.pop("bitrate")
    for k, v in DEFAULT_CONFIG["camera_settings"].items():
        cs.setdefault(k, v)
    return new_cfg


def save_config(cfg):
    """Atomic save with backup of previous version."""
    if os.path.exists(CONFIG_FILE):
        try:
            shutil.copy2(CONFIG_FILE, CONFIG_FILE + ".bak")
        except Exception as e:
            _disk_logger.warning(f"Config backup failed: {e}")
    tmp_path = CONFIG_FILE + ".tmp"
    with open(tmp_path, "w") as f:
        json.dump(cfg, f, indent=2)
    os.replace(tmp_path, CONFIG_FILE)
    _disk_logger.info("Config saved")


def get_cameras(cfg=None):
    if cfg is None:
        cfg = load_config()
    return [c for c in cfg.get("cameras", []) if c.get("ip")]


def get_server_camera(cfg=None):
    cams = get_cameras(cfg)
    for c in cams:
        if c.get("role") == "server":
            return c
    return cams[0] if cams else None


def get_client_cameras(cfg=None):
    cams = get_cameras(cfg)
    return [c for c in cams if c.get("role") == "client"]


def add_log(category, message):
    ts = datetime.now().strftime("%H:%M:%S")
    entry = f"[{ts}] {message}"
    with state_lock:
        if category not in state["logs"]:
            state["logs"][category] = []
        state["logs"][category].append(entry)
        if len(state["logs"][category]) > 200:
            state["logs"][category].pop(0)
    print(f"[{category}] {entry}")
    _disk_logger.info(f"[{category}] {message}")


def ssh_command(ip, user, command, timeout=30):
    try:
        result = subprocess.run(
            ["ssh", "-o", "ConnectTimeout=5", "-o", "StrictHostKeyChecking=no",
             f"{user}@{ip}", command],
            capture_output=True, text=True, timeout=timeout, stdin=subprocess.DEVNULL,
        )
        return result.returncode == 0, result.stdout, result.stderr
    except Exception as e:
        return False, "", str(e)


def scp_download(ip, user, remote_path, local_path, timeout=300):
    try:
        result = subprocess.run(
            ["scp", "-o", "ConnectTimeout=5", "-o", "StrictHostKeyChecking=no",
             f"{user}@{ip}:{remote_path}", local_path],
            capture_output=True, timeout=timeout, stdin=subprocess.DEVNULL,
        )
        if result.returncode == 0:
            return True, ""
        return False, result.stderr.decode() if result.stderr else "Unknown error"
    except Exception as e:
        return False, str(e)


def check_pi_connection(ip, user):
    success, _, _ = ssh_command(ip, user, "echo OK", timeout=10)
    return success


def check_ffmpeg():
    return shutil.which("ffmpeg") is not None


def check_ffprobe():
    return shutil.which("ffprobe") is not None


def get_local_video_dir():
    cfg = load_config()
    local_dir = os.path.expanduser(cfg.get("local_video_dir", DEFAULT_CONFIG["local_video_dir"]))
    os.makedirs(local_dir, exist_ok=True)
    return local_dir


# Pre-flight ===================================================================
def run_preflight():
    """Check clock skew and disk space on every Pi."""
    cfg = load_config()
    cams = get_cameras(cfg)
    video_dir = cfg.get("video_dir", "/home/maiya/camera_videos")
    results = {}
    warnings = []

    def check_one(cam):
        out = {"clock_skew_ms": None, "free_gb": None, "ok": False}
        cmd = f"date +%s.%N && df -B1 --output=avail {video_dir} 2>/dev/null | tail -1"
        t_send = time.time()
        ok, stdout, _ = ssh_command(cam["ip"], cam["user"], cmd, timeout=10)
        t_recv = time.time()
        if not ok or not stdout.strip():
            return cam["name"], out
        lines = stdout.strip().split("\n")
        if len(lines) < 2:
            return cam["name"], out
        try:
            pi_time = float(lines[0])
            controller_at_pi_sample = (t_send + t_recv) / 2
            out["clock_skew_ms"] = (pi_time - controller_at_pi_sample) * 1000
        except Exception:
            pass
        try:
            out["free_gb"] = int(lines[1].strip()) / (1024**3)
        except Exception:
            pass
        out["ok"] = out["clock_skew_ms"] is not None and out["free_gb"] is not None
        return cam["name"], out

    with ThreadPoolExecutor(max_workers=max(1, len(cams))) as pool:
        for name, info in pool.map(check_one, cams):
            results[name] = info

    for name, info in results.items():
        if not info["ok"]:
            warnings.append(f"{name}: pre-flight failed (SSH or df error)")
            continue
        if info["clock_skew_ms"] is not None and abs(info["clock_skew_ms"]) > PREFLIGHT_CLOCK_SKEW_WARN_MS:
            warnings.append(f"{name}: clock off by {info['clock_skew_ms']:.0f} ms, check NTP")
        if info["free_gb"] is not None and info["free_gb"] < PREFLIGHT_MIN_FREE_GB:
            warnings.append(
                f"{name}: only {info['free_gb']:.1f} GB free in {video_dir} (recommended >{PREFLIGHT_MIN_FREE_GB} GB)"
            )

    payload = {"cameras": results, "warnings": warnings,
               "ok": len(warnings) == 0, "timestamp": time.time()}
    with state_lock:
        state["last_preflight"] = payload
    return payload


# Thermal & connection cache ===================================================
THROTTLE_MEANINGS = {
    0: "Under-voltage detected", 1: "Arm frequency capped",
    2: "Currently throttled", 3: "Soft temp limit active",
    16: "Under-voltage has occurred", 17: "Arm frequency capped has occurred",
    18: "Throttling has occurred", 19: "Soft temp limit has occurred",
}


def _parse_throttle(hex_val):
    flags = []
    try:
        val = int(hex_val, 16)
        for bit, meaning in THROTTLE_MEANINGS.items():
            if val & (1 << bit):
                flags.append(meaning)
    except Exception:
        pass
    return flags


def _check_one_camera(cam, results, preview_status):
    name = cam["name"]
    ip = cam["ip"]
    user = cam["user"]
    info = {"connected": False, "temp": None, "throttle_flags": []}
    if not ip:
        results[name] = info
        preview_status[name] = False
        return
    info["connected"] = check_pi_connection(ip, user)
    if info["connected"]:
        ok, temp_out, _ = ssh_command(ip, user, "vcgencmd measure_temp", timeout=10)
        if ok and temp_out:
            try:
                info["temp"] = float(temp_out.strip().replace("temp=", "").replace("'C", ""))
            except Exception:
                pass
        ok, t_out, _ = ssh_command(ip, user, "vcgencmd get_throttled", timeout=10)
        if ok and t_out:
            try:
                info["throttle_flags"] = _parse_throttle(t_out.strip().split("=")[1])
            except Exception:
                pass
        preview_status[name] = _check_preview_status(ip)
    else:
        preview_status[name] = False
    results[name] = info


def _check_preview_status(ip):
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.settimeout(2.0)
        s.connect((ip, PREVIEW_PORT))
        s.close()
        return True
    except Exception:
        return False


def update_connection_cache_background():
    cfg = load_config()
    cams = get_cameras(cfg)
    results = {}
    preview_status = {}
    threads = []
    for cam in cams:
        t = threading.Thread(target=_check_one_camera, args=(cam, results, preview_status))
        t.start()
        threads.append(t)
    for t in threads:
        t.join(timeout=15)
    with cache_lock:
        connection_cache["cameras"] = results
        connection_cache["preview_status"] = preview_status
        connection_cache["last_check"] = time.time()


def get_cached_status():
    with cache_lock:
        age = time.time() - connection_cache["last_check"]
        if age > CONNECTION_CHECK_INTERVAL:
            threading.Thread(target=update_connection_cache_background, daemon=True).start()
        return {"cameras": dict(connection_cache["cameras"]),
                "preview_status": dict(connection_cache["preview_status"])}


# Control socket ===============================================================
def send_control_message(sock, message):
    try:
        data = message.encode("utf-8")
        sock.sendall(len(data).to_bytes(4, "big") + data)
        return True
    except Exception as e:
        print(f"send error: {e}")
        return False


def recv_control_message(sock, timeout=10):
    try:
        sock.settimeout(timeout)
        length_data = b""
        while len(length_data) < 4:
            chunk = sock.recv(4 - len(length_data))
            if not chunk:
                return None
            length_data += chunk
        length = int.from_bytes(length_data, "big")
        if length <= 0 or length > 10000:
            return None
        data = b""
        while len(data) < length:
            chunk = sock.recv(length - len(data))
            if not chunk:
                return None
            data += chunk
        return data.decode("utf-8")
    except socket.timeout:
        return None
    except Exception as e:
        print(f"recv error: {e}")
        return None


def connect_to_control_port(ip, timeout=10):
    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.settimeout(timeout)
        sock.connect((ip, CONTROL_PORT))
        msg = recv_control_message(sock, timeout=5)
        if msg and msg.startswith("STATUS:"):
            print(f"Control port connected: {msg}")
            return sock
        sock.close()
        return None
    except Exception as e:
        print(f"control port connect failed: {e}")
        return None


# Service lifecycle ============================================================
def _systemctl(ip, user, action, unit, timeout=10):
    return ssh_command(ip, user, f"sudo systemctl {action} {unit}", timeout=timeout)


def start_sync_services():
    cfg = load_config()
    server = get_server_camera(cfg)
    clients = get_client_cameras(cfg)
    if not server:
        add_log("server", "ERROR: No server camera configured")
        return False
    add_log("server", "Stopping preview services...")
    for c in [server] + clients:
        _systemctl(c["ip"], c["user"], "stop", "squeakshot-preview", timeout=5)
    add_log("server", f"Starting record service on {server['name']}...")
    ok, _, err = _systemctl(server["ip"], server["user"], "start", "squeakshot-record")
    if not ok:
        add_log("server", f"  Server start failed: {err.strip()[:200]}")
        return False
    time.sleep(1.5)
    for c in clients:
        add_log("server", f"Starting record service on {c['name']}...")
        ok, _, err = _systemctl(c["ip"], c["user"], "start", "squeakshot-record")
        if not ok:
            add_log("server", f"  {c['name']} start failed: {err.strip()[:200]}")
    time.sleep(1.5)
    # Camera init (libcamera + Picamera2.start_encoder on first Libav run) can
    # take 30s+ on a Pi 5 from cold start. Retry far longer than v9.1 did, with
    # informative log lines so the UI does not look hung.
    add_log("server", "Connecting to control port (camera init can take 30s+)...")
    max_wait_s = 60
    waited = 0
    while waited < max_wait_s:
        sock = connect_to_control_port(server["ip"], timeout=3)
        if sock:
            with state_lock:
                state["control_socket"] = sock
                state["service_running"] = True
            add_log("server", f"Services ready ({waited}s)")
            return True
        if waited and waited % 10 == 0:
            add_log("server", f"  Still waiting for cam0:{CONTROL_PORT} ({waited}s elapsed)...")
        time.sleep(2)
        waited += 2
    add_log("server", f"ERROR: could not reach control port after {max_wait_s}s. "
                      f"Check 'journalctl -u squeakshot-record -n 50' on {server['name']}.")
    return False


def stop_sync_services():
    cfg = load_config()
    with state_lock:
        if state["control_socket"]:
            try:
                state["control_socket"].close()
            except Exception:
                pass
            state["control_socket"] = None
        state["service_running"] = False
    for c in get_cameras(cfg):
        _systemctl(c["ip"], c["user"], "stop", "squeakshot-record", timeout=5)
    add_log("server", "Services stopped")


# Recording ====================================================================
def start_recording_thread():
    cfg = load_config()
    server = get_server_camera(cfg)
    clients = get_client_cameras(cfg)
    with state_lock:
        animal_id = state.get("animal_id", "unknown")
        project_id = state.get("project_id", "unknown")
        control_socket = state.get("control_socket")
        service_running = state.get("service_running", False)

    # Build name=filename pairs (server first, then clients in config order)
    pairs = [(server["name"], f"{server['name']}_{animal_id}_{project_id}")]
    for c in clients:
        pairs.append((c["name"], f"{c['name']}_{animal_id}_{project_id}"))

    for name, fn in pairs:
        add_log("server", f"Recording {name} as: {fn}.h264")

    try:
        if not service_running or not control_socket:
            add_log("server", "Services not running, starting...")
            if not start_sync_services():
                with state_lock:
                    state["is_recording"] = False
                return
            with state_lock:
                control_socket = state.get("control_socket")
        if not control_socket:
            add_log("server", "ERROR: no control socket")
            with state_lock:
                state["is_recording"] = False
            return

        # New wire format: name=filename pairs (handles renamed cameras)
        cmd = "START:" + ":".join(f"{name}={fn}" for name, fn in pairs)
        add_log("server", f"Sending START ({len(clients)} clients expected)...")
        if not send_control_message(control_socket, cmd):
            with state_lock:
                state["is_recording"] = False
            return
        # 60s timeout: first START after a cold boot triggers Libav encoder init on
        # every Pi, which can take many seconds. v9.1's 15s timed out spuriously.
        response = recv_control_message(control_socket, timeout=60)
        if response and response.startswith("OK"):
            with state_lock:
                state["start_time"] = time.time()
            add_log("server", "Recording started across all cameras")
        else:
            # Avoid logging "ERROR: ERROR:..." when the server already prefixed.
            msg = response if (response and response.startswith("ERROR:")) else f"ERROR: {response}"
            add_log("server", msg)
            with state_lock:
                state["is_recording"] = False
    except Exception as e:
        add_log("server", f"ERROR: {e}")
        with state_lock:
            state["is_recording"] = False


def stop_recording():
    with state_lock:
        control_socket = state.get("control_socket")
    if not control_socket:
        add_log("server", "ERROR: no control connection, stopping services directly")
        cfg = load_config()
        for c in get_cameras(cfg):
            _systemctl(c["ip"], c["user"], "stop", "squeakshot-record", timeout=5)
        return
    add_log("server", "Stopping recording...")
    try:
        if not send_control_message(control_socket, "STOP"):
            add_log("server", "ERROR: failed to send STOP")
            return
        response = recv_control_message(control_socket, timeout=10)
        if response:
            add_log("server", response)
    except Exception as e:
        add_log("server", f"ERROR during stop: {e}")


# Preview ======================================================================
def start_previews():
    cfg = load_config()
    cams = get_cameras(cfg)
    add_log("preview", "Stopping record services first...")
    for c in cams:
        _systemctl(c["ip"], c["user"], "stop", "squeakshot-record", timeout=5)
    time.sleep(1)
    started = 0
    for c in cams:
        add_log("preview", f"Starting preview on {c['name']}...")
        ok, _, err = _systemctl(c["ip"], c["user"], "start", "squeakshot-preview")
        if ok:
            started += 1
        else:
            add_log("preview", f"  {c['name']} failed: {err.strip()[:200]}")
    add_log("preview", f"Started preview on {started}/{len(cams)} cameras")
    threading.Thread(target=update_connection_cache_background, daemon=True).start()


def stop_previews():
    cfg = load_config()
    for c in get_cameras(cfg):
        _systemctl(c["ip"], c["user"], "stop", "squeakshot-preview", timeout=5)
    add_log("preview", "Preview services stopped")
    threading.Thread(target=update_connection_cache_background, daemon=True).start()


# Flask routes =================================================================
@app.route("/")
def index():
    return render_template("controller.html")


@app.route("/api/config", methods=["GET"])
def api_get_config():
    return jsonify(load_config())


@app.route("/api/config", methods=["POST"])
def api_save_config():
    cfg = request.json
    if "cameras" in cfg:
        cfg["cameras"] = [c for c in cfg["cameras"] if c.get("ip", "").strip()]
    save_config(cfg)
    return jsonify({"success": True})


@app.route("/api/test_connection", methods=["POST"])
def api_test_connection():
    threading.Thread(target=update_connection_cache_background, daemon=True).start()
    time.sleep(3)
    with cache_lock:
        return jsonify({"cameras": dict(connection_cache["cameras"])})


@app.route("/api/preflight", methods=["POST"])
def api_preflight():
    return jsonify(run_preflight())


@app.route("/api/status", methods=["GET"])
def api_status():
    cfg = load_config()
    cams = cfg.get("cameras", [])
    with state_lock:
        is_recording = state["is_recording"]
        start_time = state["start_time"]
        service_running = state.get("service_running", False)
        last_preflight = state.get("last_preflight")
    elapsed = int(time.time() - start_time) if is_recording and start_time else None
    cached = get_cached_status()
    # preview_running derived from actual port probes (not local flag)
    preview_running = any(cached["preview_status"].values()) if cached["preview_status"] else False
    return jsonify({
        "is_recording": is_recording, "elapsed_seconds": elapsed,
        "start_time": start_time, "service_running": service_running,
        "preview_running": preview_running,
        "cameras": cams, "camera_status": cached["cameras"],
        "preview_status": cached["preview_status"],
        "settings": cfg.get("camera_settings", {}),
        "configured": bool(get_cameras(cfg)),
        "ffmpeg_available": check_ffmpeg(),
        "last_preflight": last_preflight,
    })


@app.route("/api/logs", methods=["GET"])
def api_logs():
    with state_lock:
        return jsonify(state["logs"])


@app.route("/api/services/start", methods=["POST"])
def api_start_services():
    with state_lock:
        if state.get("service_running"):
            return jsonify({"error": "Services already running"}), 400
    threading.Thread(target=start_sync_services, daemon=True).start()
    return jsonify({"success": True, "message": "Starting services..."})


@app.route("/api/services/stop", methods=["POST"])
def api_stop_services():
    with state_lock:
        if state.get("is_recording"):
            return jsonify({"error": "Cannot stop services while recording"}), 400
    stop_sync_services()
    return jsonify({"success": True})


@app.route("/api/preview/start", methods=["POST"])
def api_preview_start():
    with state_lock:
        if state.get("is_recording"):
            return jsonify({"error": "Cannot start preview while recording"}), 400
    threading.Thread(target=start_previews, daemon=True).start()
    return jsonify({"success": True, "message": "Starting previews..."})


@app.route("/api/preview/stop", methods=["POST"])
def api_preview_stop():
    threading.Thread(target=stop_previews, daemon=True).start()
    return jsonify({"success": True})


@app.route("/api/start", methods=["POST"])
def api_start():
    with state_lock:
        if state["is_recording"]:
            return jsonify({"error": "Already recording"}), 400
        data = request.json or {}
        animal_id = data.get("animal_id", "").strip()
        project_id = data.get("project_id", "").strip()
        force = data.get("force", False)
        if not animal_id or not project_id:
            return jsonify({"error": "animal_id and project_id required"}), 400
        # Sanity: these become protocol field values
        for label, val in (("animal_id", animal_id), ("project_id", project_id)):
            if ":" in val or "=" in val or " " in val:
                return jsonify({"error": f"{label} cannot contain spaces, ':' or '='"}), 400

    if not force:
        preflight = run_preflight()
        if preflight["warnings"]:
            return jsonify({
                "preflight_warnings": preflight["warnings"],
                "preflight": preflight,
                "message": "Pre-flight check found issues. Resubmit with force=true to record anyway.",
            }), 409

    with state_lock:
        state["is_recording"] = True
        state["start_time"] = None
        state["animal_id"] = animal_id
        state["project_id"] = project_id
    threading.Thread(target=start_recording_thread, daemon=True).start()
    return jsonify({"success": True})


@app.route("/api/stop", methods=["POST"])
def api_stop():
    with state_lock:
        if not state["is_recording"]:
            return jsonify({"error": "Not recording"}), 400
        state["is_recording"] = False
        state["start_time"] = None
    stop_recording()
    return jsonify({"success": True})


# Recordings ===================================================================
def _list_recordings_for_camera(cam, video_dir):
    """Single SSH call per camera, robust to filenames with spaces.
    Returns {basename: {'size': str, 'mtime': str}}."""
    cmd = (f"find {video_dir} -maxdepth 1 -name '{cam['name']}_*.h264' "
           f"-printf '%f\\t%s\\t%T@\\n' 2>/dev/null")
    ok, stdout, _ = ssh_command(cam["ip"], cam["user"], cmd, timeout=15)
    files = {}
    if not ok or not stdout.strip():
        return files
    prefix = f"{cam['name']}_"
    for line in stdout.strip().split("\n"):
        parts = line.split("\t")
        if len(parts) != 3:
            continue
        fn, size_bytes, mtime = parts
        if not fn.startswith(prefix) or not fn.endswith(".h264"):
            continue
        basename = fn[len(prefix):-5]
        try:
            sz = int(size_bytes)
            sz_str = f"{sz/1_073_741_824:.2f}G" if sz > 1_073_741_824 else f"{sz/1_048_576:.0f}M"
        except Exception:
            sz_str = "?"
        try:
            ts = datetime.fromtimestamp(float(mtime)).strftime("%Y-%m-%d %H:%M")
        except Exception:
            ts = "?"
        files[basename] = {"size": sz_str, "mtime": ts}
    return files


@app.route("/api/recordings", methods=["GET"])
def api_recordings():
    cfg = load_config()
    cams = get_cameras(cfg)
    if not cams:
        return jsonify({"recordings": [], "error": "No cameras configured"})
    video_dir = cfg["video_dir"]

    per_cam_files = {}
    with ThreadPoolExecutor(max_workers=len(cams)) as pool:
        results = pool.map(lambda c: (c["name"], _list_recordings_for_camera(c, video_dir)), cams)
        for name, files in results:
            per_cam_files[name] = files

    server = get_server_camera(cfg)
    server_files = per_cam_files.get(server["name"], {}) if server else {}

    recordings = []
    for basename, info in server_files.items():
        entry = {"name": basename, "timestamp": info.get("mtime", "?"), "per_camera": {}}
        for c in cams:
            f = per_cam_files.get(c["name"], {}).get(basename)
            entry["per_camera"][c["name"]] = (
                {"present": True, "size": f["size"]} if f
                else {"present": False, "size": "—"}
            )
        recordings.append(entry)
    recordings.sort(key=lambda r: r["timestamp"], reverse=True)
    return jsonify({"recordings": recordings})


@app.route("/api/download_file/<path:name>/<camera>/<extension>", methods=["GET"])
def api_download_file(name, camera, extension):
    cfg = load_config()
    cam = next((c for c in get_cameras(cfg) if c["name"] == camera), None)
    if not cam:
        return jsonify({"error": f"Unknown camera {camera}"}), 400
    remote = f'{cfg["video_dir"]}/{camera}_{name}.{extension}'
    with tempfile.NamedTemporaryFile(delete=False, suffix=f".{extension}") as tmp:
        tmp_path = tmp.name
    ok, err = scp_download(cam["ip"], cam["user"], remote, tmp_path, timeout=120)
    if not ok:
        return jsonify({"error": err}), 500
    return send_file(tmp_path, as_attachment=True,
                     download_name=f"{camera}_{name}.{extension}",
                     mimetype="application/octet-stream")


@app.route("/api/delete/<path:name>", methods=["DELETE"])
def api_delete(name):
    cfg = load_config()
    errors = []
    for c in get_cameras(cfg):
        for ext in [".h264", ".pts"]:
            remote = f'{cfg["video_dir"]}/{c["name"]}_{name}{ext}'
            ok, _, err = ssh_command(c["ip"], c["user"], f"rm -f {remote}", timeout=10)
            if not ok:
                errors.append(f"{c['name']}{ext}: {err}")
    if errors:
        return jsonify({"error": "Partial delete", "details": errors}), 500
    return jsonify({"success": True})


# Encoding (parallel) ==========================================================
@app.route("/api/encode/download", methods=["POST"])
def api_encode_download():
    data = request.json
    name = data.get("name")
    if not name:
        return jsonify({"error": "No recording name"}), 400
    cfg = load_config()
    cams = get_cameras(cfg)
    raw_dir = os.path.join(get_local_video_dir(), "raw")
    os.makedirs(raw_dir, exist_ok=True)
    add_log("encode", f"Downloading {name} from {len(cams)} cameras (parallel)...")

    def download_one(c):
        results = []
        for ext in ["h264", "pts"]:
            remote = f'{cfg["video_dir"]}/{c["name"]}_{name}.{ext}'
            local = os.path.join(raw_dir, f'{c["name"]}_{name}.{ext}')
            ok, err = scp_download(c["ip"], c["user"], remote, local, timeout=300)
            if ok:
                results.append((True, f"{c['name']}_{name}.{ext}", None))
                add_log("encode", f"  {c['name']}_{name}.{ext}")
            else:
                results.append((False, f"{c['name']}_{name}.{ext}", err))
                add_log("encode", f"  FAILED {c['name']}_{name}.{ext}")
        return results

    downloaded, errors = [], []
    with ThreadPoolExecutor(max_workers=len(cams)) as pool:
        for cam_results in pool.map(download_one, cams):
            for ok, fname, err in cam_results:
                (downloaded if ok else errors).append(
                    fname if ok else f"{fname}: {err}"
                )
    if not downloaded:
        return jsonify({"error": "Download failed", "details": errors}), 500
    add_log("encode", f"Download complete: {len(downloaded)} files")
    return jsonify({"success": True, "downloaded": downloaded, "errors": errors})


@app.route("/api/encode/start", methods=["POST"])
def api_encode_start():
    data = request.json
    name = data.get("name")
    fps = data.get("fps", 56)
    delete_h264 = data.get("delete_h264", False)
    if not check_ffmpeg():
        return jsonify({"error": "FFmpeg not found"}), 500
    with state_lock:
        if state["is_encoding"]:
            return jsonify({"error": "Encoding already in progress"}), 400
        state["is_encoding"] = True
        state["encode_message"] = "Starting..."

    cfg = load_config()
    cams = get_cameras(cfg)
    local_dir = get_local_video_dir()
    raw_dir = os.path.join(local_dir, "raw")
    encoded_dir = os.path.join(local_dir, "encoded")
    os.makedirs(encoded_dir, exist_ok=True)
    add_log("encode", f"Encoding {name} @ {fps}fps ({len(cams)} cameras in parallel)")

    def encode_one(c):
        h264 = os.path.join(raw_dir, f'{c["name"]}_{name}.h264')
        mp4 = os.path.join(encoded_dir, f'{c["name"]}_{name}.mp4')
        pts = os.path.join(raw_dir, f'{c["name"]}_{name}.pts')
        if not os.path.exists(h264):
            add_log("encode", f"  SKIP {c['name']}: H264 missing")
            return
        # Catch the empty-recording case explicitly. If the Pi produced a 0-byte
        # .h264, ffmpeg fails with an opaque "Invalid data found" error; flag it
        # with a useful hint instead.
        if os.path.getsize(h264) == 0:
            add_log("encode",
                    f"  SKIP {c['name']}: H264 is 0 bytes. The Pi service likely failed "
                    f"to write any frames. Check 'journalctl -u squeakshot-record -n 100' "
                    f"on {c['name']} for encoder errors.")
            return
        add_log("encode", f"  Encoding {c['name']}...")
        cmd = ["ffmpeg", "-hide_banner", "-loglevel", "error",
               "-y", "-framerate", str(fps), "-i", h264,
               "-c:v", "libx264", "-preset", "medium", "-crf", "18",
               "-r", str(fps), "-movflags", "+faststart", mp4]
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=1800)
        if result.returncode == 0:
            size_mb = os.path.getsize(mp4) / 1_048_576
            add_log("encode", f"  DONE {c['name']} ({size_mb:.1f} MB)")
            if os.path.exists(pts):
                shutil.copy2(pts, os.path.join(encoded_dir, f'{c["name"]}_{name}.pts'))
            if delete_h264:
                os.remove(h264)
                add_log("encode", f"  Deleted {c['name']} H264")
        else:
            # Trim to last 1500 chars of stderr (with -loglevel error, this is real
            # diagnostic content rather than the version banner).
            err_tail = (result.stderr or "").strip()[-1500:]
            add_log("encode", f"  FAILED {c['name']}: {err_tail}")

    def encode_all():
        try:
            with state_lock:
                state["encode_message"] = f"Encoding {len(cams)} cameras in parallel..."
            with ThreadPoolExecutor(max_workers=len(cams)) as pool:
                list(pool.map(encode_one, cams))
            add_log("encode", "Encoding complete!")
        except Exception as e:
            add_log("encode", f"ERROR: {e}")
        finally:
            with state_lock:
                state["is_encoding"] = False
                state["encode_message"] = ""

    threading.Thread(target=encode_all, daemon=True).start()
    return jsonify({"success": True})


@app.route("/api/encode/status", methods=["GET"])
def api_encode_status():
    with state_lock:
        return jsonify({"is_encoding": state["is_encoding"], "message": state["encode_message"]})


@app.route("/api/encode/local_files", methods=["GET"])
def api_encode_local_files():
    local_dir = get_local_video_dir()
    files = {"raw": [], "encoded": [], "synced": [], "trimmed": []}
    for sub in files.keys():
        d = os.path.join(local_dir, sub)
        if not os.path.exists(d):
            continue
        for f in os.listdir(d):
            if f.endswith((".h264", ".mp4", ".pts")):
                fp = os.path.join(d, f)
                sz = os.path.getsize(fp)
                files[sub].append({
                    "name": f, "size": sz,
                    "size_str": f"{sz/1_048_576:.1f} MB" if sz > 1_048_576 else f"{sz/1024:.1f} KB",
                    "path": fp,
                })
    return jsonify(files)


# Sync =========================================================================
@app.route("/api/sync/analyze", methods=["POST"])
def api_sync_analyze():
    data = request.json
    name = data.get("name")
    cfg = load_config()
    cams = get_cameras(cfg)
    encoded_dir = os.path.join(get_local_video_dir(), "encoded")
    pts_files = {c["name"]: os.path.join(encoded_dir, f'{c["name"]}_{name}.pts') for c in cams}
    missing = [n for n, p in pts_files.items() if not os.path.exists(p)]
    if missing:
        return jsonify({"error": f"PTS files missing for: {', '.join(missing)}. Encode first."}), 400

    add_log("sync", f"Analyzing {name} ({len(cams)} cameras)...")
    try:
        pts_list = [sync_lib.load_pts(pts_files[c["name"]]) for c in cams]
        camera_names = [c["name"] for c in cams]
        # Detect frame-count discrepancy BEFORE matching (silent-clip warning)
        fc_analysis = sync_lib.analyze_frame_counts(pts_list, camera_names)
        matched = sync_lib.match_n_cameras(pts_list)
        n_matched = len(matched[0]) if matched else 0
        per_cam = []
        for i, c in enumerate(cams):
            relative = pts_list[i] - pts_list[i][0]
            per_cam.append({
                "name": c["name"],
                "total_frames": int(len(pts_list[i])),
                "duration_sec": float(relative[-1] / 1_000_000),
                "matched": int(n_matched),
            })
        avg_ms, max_ms, quality = sync_lib.compute_timing_quality(pts_list, matched)
        for p in per_cam:
            add_log("sync", f"  {p['name']}: {p['total_frames']} frames ({p['duration_sec']:.2f}s)")
        add_log("sync", f"  Matched: {n_matched} frames, quality: {quality}")
        warnings = []
        if fc_analysis["warn"]:
            warnings.append(
                f"Frame counts disagree by {fc_analysis['discrepancy_frames']} "
                f"({fc_analysis['discrepancy_ratio']*100:.1f}%). "
                f"{fc_analysis['shortest_camera']} has the fewest frames, "
                f"sync will clip everyone to that length."
            )
            add_log("sync", f"  WARNING: {warnings[-1]}")
        return jsonify({
            "per_camera": per_cam, "matched_frames": n_matched,
            "avg_timing_diff_ms": avg_ms, "max_timing_diff_ms": max_ms,
            "sync_quality": quality, "warnings": warnings,
            "frame_count_discrepancy": fc_analysis,
        })
    except Exception as e:
        add_log("sync", f"ERROR: {e}")
        return jsonify({"error": str(e)}), 500


@app.route("/api/sync/start", methods=["POST"])
def api_sync_start():
    import numpy as np
    data = request.json
    name = data.get("name")
    fps = data.get("fps", 56)
    if not check_ffmpeg():
        return jsonify({"error": "FFmpeg not found"}), 500
    with state_lock:
        if state["is_syncing"]:
            return jsonify({"error": "Sync already running"}), 400
        state["is_syncing"] = True
        state["sync_message"] = "Starting..."
    cfg = load_config()
    cams = get_cameras(cfg)
    local_dir = get_local_video_dir()
    encoded_dir = os.path.join(local_dir, "encoded")
    synced_dir = os.path.join(local_dir, "synced")
    os.makedirs(synced_dir, exist_ok=True)
    add_log("sync", f"Synchronizing {name}...")

    def sync_thread():
        try:
            pts_list = [sync_lib.load_pts(os.path.join(encoded_dir, f'{c["name"]}_{name}.pts')) for c in cams]
            matched = sync_lib.match_n_cameras(pts_list)
            n = len(matched[0]) if matched else 0
            add_log("sync", f"  Matched {n} frames across {len(cams)} cameras")

            def sync_one(i_c):
                i, c = i_c
                input_mp4 = os.path.join(encoded_dir, f'{c["name"]}_{name}.mp4')
                output_mp4 = os.path.join(synced_dir, f'{c["name"]}_{name}_synced.mp4')
                output_pts = os.path.join(synced_dir, f'{c["name"]}_{name}_synced.pts')
                if not os.path.exists(input_mp4):
                    add_log("sync", f"  SKIP {c['name']}: MP4 missing")
                    return
                frames = matched[i]
                start_frame = int(frames[0])
                start_time = start_frame / fps
                add_log("sync", f"  {c['name']}: trim from frame {start_frame}, keep {len(frames)}")
                cmd = ["ffmpeg", "-y", "-i", input_mp4,
                       "-ss", f"{start_time:.6f}", "-frames:v", str(len(frames)),
                       "-c:v", "libx264", "-preset", "medium", "-crf", "18", output_mp4]
                result = subprocess.run(cmd, capture_output=True, text=True, timeout=1800)
                if result.returncode == 0:
                    add_log("sync", f"  DONE {c['name']}")
                    selected = pts_list[i][frames]
                    selected = selected - selected[0]
                    np.savetxt(output_pts, selected, fmt="%d")
                else:
                    add_log("sync", f"  FAILED {c['name']}: {result.stderr[:200]}")

            with state_lock:
                state["sync_message"] = f"Trimming {len(cams)} cameras in parallel..."
            with ThreadPoolExecutor(max_workers=len(cams)) as pool:
                list(pool.map(sync_one, enumerate(cams)))
            add_log("sync", "Sync complete!")
        except Exception as e:
            add_log("sync", f"ERROR: {e}")
        finally:
            with state_lock:
                state["is_syncing"] = False
                state["sync_message"] = ""

    threading.Thread(target=sync_thread, daemon=True).start()
    return jsonify({"success": True})


@app.route("/api/sync/status", methods=["GET"])
def api_sync_status():
    with state_lock:
        return jsonify({"is_syncing": state["is_syncing"], "message": state["sync_message"]})


# Trim =========================================================================
@app.route("/api/trim/video_info", methods=["POST"])
def api_trim_video_info():
    data = request.json
    name = data.get("name")
    cfg = load_config()
    server = get_server_camera(cfg)
    synced_dir = os.path.join(get_local_video_dir(), "synced")
    server_mp4 = os.path.join(synced_dir, f'{server["name"]}_{name}_synced.mp4')
    if not os.path.exists(server_mp4):
        return jsonify({"error": "Synced video not found"}), 400
    if not check_ffprobe():
        return jsonify({"error": "FFprobe not found"}), 500
    try:
        cmd = ["ffprobe", "-v", "error", "-select_streams", "v:0",
               "-show_entries", "stream=width,height,r_frame_rate,nb_frames,duration",
               "-of", "json", server_mp4]
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
        if result.returncode != 0:
            return jsonify({"error": "ffprobe failed"}), 500
        info = json.loads(result.stdout)
        stream = info.get("streams", [{}])[0]
        fps_str = stream.get("r_frame_rate", "56/1")
        if "/" in fps_str:
            num, den = map(int, fps_str.split("/"))
            fps = num / den if den else 56
        else:
            fps = float(fps_str)
        return jsonify({
            "width": int(stream.get("width", 0)),
            "height": int(stream.get("height", 0)),
            "fps": fps,
            "total_frames": int(stream.get("nb_frames", 0)),
            "duration": float(stream.get("duration", 0)),
        })
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/trim/frame/<path:name>/<int:frame_num>")
def api_trim_frame(name, frame_num):
    cfg = load_config()
    server = get_server_camera(cfg)
    synced_dir = os.path.join(get_local_video_dir(), "synced")
    server_mp4 = os.path.join(synced_dir, f'{server["name"]}_{name}_synced.mp4')
    if not os.path.exists(server_mp4):
        return jsonify({"error": "Video not found"}), 404
    try:
        cmd = ["ffprobe", "-v", "error", "-select_streams", "v:0",
               "-show_entries", "stream=r_frame_rate",
               "-of", "default=noprint_wrappers=1:nokey=1", server_mp4]
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=10)
        fps_str = result.stdout.strip()
        if "/" in fps_str:
            num, den = map(int, fps_str.split("/"))
            fps = num / den if den else 56
        else:
            fps = float(fps_str) if fps_str else 56
        ts = frame_num / fps
        cmd = ["ffmpeg", "-y", "-ss", f"{ts:.6f}", "-i", server_mp4,
               "-vframes", "1", "-f", "image2pipe", "-vcodec", "mjpeg", "-"]
        result = subprocess.run(cmd, capture_output=True, timeout=30)
        if result.returncode == 0 and result.stdout:
            return Response(result.stdout, mimetype="image/jpeg")
        return jsonify({"error": "Frame extract failed"}), 500
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/trim/start", methods=["POST"])
def api_trim_start():
    data = request.json
    name = data.get("name")
    start_frame = data.get("start_frame", 0)
    end_frame = data.get("end_frame")
    fps = data.get("fps", 56)
    if not check_ffmpeg():
        return jsonify({"error": "FFmpeg not found"}), 500
    cfg = load_config()
    cams = get_cameras(cfg)
    local_dir = get_local_video_dir()
    synced_dir = os.path.join(local_dir, "synced")
    trimmed_dir = os.path.join(local_dir, "trimmed")
    os.makedirs(trimmed_dir, exist_ok=True)
    add_log("trim", f"Trimming {name} ({start_frame}-{end_frame}), parallel across {len(cams)} cameras")

    def trim_one(c):
        input_mp4 = os.path.join(synced_dir, f'{c["name"]}_{name}_synced.mp4')
        output_mp4 = os.path.join(trimmed_dir, f'{c["name"]}_{name}_final.mp4')
        if not os.path.exists(input_mp4):
            add_log("trim", f"  SKIP {c['name']}: not found")
            return
        start_time = start_frame / fps
        num_frames = end_frame - start_frame + 1
        add_log("trim", f"  Trimming {c['name']}...")
        cmd = ["ffmpeg", "-y", "-i", input_mp4,
               "-ss", f"{start_time:.6f}", "-frames:v", str(num_frames),
               "-c:v", "libx264", "-preset", "medium", "-crf", "18", output_mp4]
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=1800)
        if result.returncode == 0:
            sz = os.path.getsize(output_mp4) / 1_048_576
            add_log("trim", f"  DONE {c['name']} ({sz:.1f} MB)")
        else:
            add_log("trim", f"  FAILED {c['name']}: {result.stderr[:200]}")

    def trim_all():
        try:
            with ThreadPoolExecutor(max_workers=len(cams)) as pool:
                list(pool.map(trim_one, cams))
            add_log("trim", "Trim complete!")
        except Exception as e:
            add_log("trim", f"ERROR: {e}")

    threading.Thread(target=trim_all, daemon=True).start()
    return jsonify({"success": True})


# Utility ======================================================================
@app.route("/api/local_dir", methods=["GET", "POST"])
def api_local_dir():
    if request.method == "POST":
        data = request.json
        new_path = os.path.expanduser((data.get("path", "") or "").strip())
        if not new_path:
            return jsonify({"error": "Path required"}), 400
        try:
            os.makedirs(new_path, exist_ok=True)
            for sub in ["raw", "encoded", "synced", "trimmed"]:
                os.makedirs(os.path.join(new_path, sub), exist_ok=True)
        except Exception as e:
            return jsonify({"error": str(e)}), 400
        cfg = load_config()
        cfg["local_video_dir"] = new_path
        save_config(cfg)
        return jsonify({"success": True, "path": new_path})
    return jsonify({"path": get_local_video_dir()})


@app.route("/api/open_folder", methods=["POST"])
def api_open_folder():
    data = request.json
    folder = data.get("folder", "")
    base_dir = data.get("base_dir", "")
    target = (os.path.join(os.path.expanduser(base_dir), folder) if base_dir
              else os.path.join(get_local_video_dir(), folder) if folder
              else get_local_video_dir())
    os.makedirs(target, exist_ok=True)
    try:
        import platform
        if platform.system() == "Windows":
            os.startfile(target)
        elif platform.system() == "Darwin":
            subprocess.run(["open", target])
        else:
            subprocess.run(["xdg-open", target])
        return jsonify({"success": True})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


# Main =========================================================================
if __name__ == "__main__":
    print("=" * 50)
    print("SqueakShot Camera Controller v9.2")
    print("=" * 50)
    cfg = load_config()
    print(f"Cameras configured: {len(get_cameras(cfg))}")
    for c in get_cameras(cfg):
        print(f"  {c['name']} ({c['role']}), {c['user']}@{c['ip']}")
    print(f"FFmpeg: {check_ffmpeg()}")
    print(f"Local video dir: {get_local_video_dir()}")
    print(f"Disk log: {os.path.abspath(os.path.join(LOG_DIR, 'controller.log'))}")
    print(f"Clock skew threshold: {PREFLIGHT_CLOCK_SKEW_WARN_MS} ms")
    print("=" * 50)
    print(f"Web interface: http://localhost:{SQUEAKSHOT_PORT}")
    print("(set SQUEAKSHOT_PORT to override the default 5000)")
    print("=" * 50)
    _disk_logger.info(f"Controller started on port {SQUEAKSHOT_PORT}")
    threading.Thread(target=update_connection_cache_background, daemon=True).start()
    app.run(host="0.0.0.0", port=SQUEAKSHOT_PORT, debug=False, threaded=True)
