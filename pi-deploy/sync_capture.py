#!/usr/bin/env python3
"""
Synchronized Camera Capture Service v9.2.5
Multi-camera star topology: one server, N clients.

Changes from v9.2.4:
- Write the PTS file ourselves via TimestampingFileOutput, a FileOutput subclass
  that calls super().outputframe() first (so video recording is never affected)
  then writes the timestamp to a sidecar .pts as a side effect. picamera2's
  start_encoder(pts=...) kwarg is silently ignored on Pi 5 (libav path doesn't
  honor it), so the .pts files were being skipped even though the kwarg was
  accepted.
- Quiet the "force_key_frame unsupported" log line. The Libav-based encoder
  produces a keyframe on frame 0 automatically, so the warning is just noise.

Changes from v9.2.3:
- Runtime monkey-patch for picamera2 + new python3-av incompatibility on Pi 5.
  picamera2's libav_h264_encoder.py sets frame.pict_type = "I" (string), but
  PyAV >=14 requires an integer/enum. We rewrite _encode at import time to
  use the integer value. This fixes cam2 (and any Pi running libcamera 0.7+
  with the newer PyAV) without requiring apt surgery.
- Realization: on Pi 5 there is no hardware H264 encoder, so picamera2 routes
  H264Encoder and LibavH264Encoder through the same Libav code path.

Changes from v9.2.2:
- Default encoder switched to V4L2 H264Encoder (was Libav). SQUEAKSHOT_PREFER_LIBAV=1
  opts back into Libav. (Largely cosmetic on Pi 5; the real fix is the
  pict_type patch in v9.2.4.)
- stop_recording() sleeps 0.25s before counting the PTS file so picamera2 has
  time to flush.

Changes from v9.2.1:
- Drop custom PTSOutput subclass entirely. (Replaced again in v9.2.5 with a
  safer subclass that does not bypass the parent's outputframe.)

Changes from v9.2:
- Fix ACK race: main select() loop now skips client sockets while the control
  worker is busy.

Changes from v9.1:
- Pi 5 / libcamera compatibility (NoiseReductionModeEnum guard, encode="main")
- PTSOutput bypasses FileOutput.outputframe keyframe gate (fixes 0-byte .h264 on Pi 5)
- create_h264_encoder() prefers LibavH264Encoder (software x264 via PyAV) when available,
  falls back to V4L2 H264Encoder. iperiod from FPS. framerate as Fraction.
- Server uses a worker thread for control commands so wait_until + start_encoder do not
  block Picamera2 servicing. control_busy Event prevents recv() race on control socket.
- Initial STATUS send uses blocking socket to avoid BlockingIOError.
- recv_message timeout raised 0.1 -> 5.0 on control reads.

Changes from v9.0:
- START command parses name=filename pairs (no more positional cam1/cam2 assumption)
- Server sends PING every 10s; clients auto-stop recording on >30s server silence
- Client tracks last_server_msg_time for heartbeat health

Usage:
    Server: python3 sync_capture.py server [--config <path>]
    Client: python3 sync_capture.py client --server-ip <IP> --name <name> [--config <path>]
"""

import argparse
import fractions
import queue
import socket
import select
import time
import threading
import os
import json
from datetime import datetime
from picamera2 import Picamera2
from picamera2.encoders import H264Encoder
from picamera2.outputs import FileOutput
from libcamera import controls
import signal
import sys

# Try to import LibavH264Encoder. On Pi 5 with python3-av installed, this gives
# software x264 via PyAV, which is more reliable than the V4L2 H264Encoder.
try:
    from picamera2.encoders import LibavH264Encoder
    _HAS_LIBAV = True
except Exception:
    LibavH264Encoder = None
    _HAS_LIBAV = False


def _patch_libav_pict_type():
    """Patch picamera2's LibavH264Encoder._encode for newer python3-av.

    On Pi 5 with libcamera 0.7+ and newer python3-av (av 14+), picamera2's
    libav_h264_encoder.py contains:

        frame.pict_type = "I"

    Newer PyAV requires an enum or integer for pict_type and raises
    'TypeError: an integer is required' on the string form. The Pi 5 has no
    hardware H264 encoder, so picamera2 routes BOTH H264Encoder and
    LibavH264Encoder through this same Libav-based code path on newer
    versions, meaning we cannot avoid this bug by encoder selection alone.

    This function rewrites _encode at import time, substituting the integer
    value for the string. Logs whether the patch was applied so we can verify
    it's running. No-op when picamera2 already has the fix.
    """
    try:
        import inspect, textwrap
        import picamera2.encoders.libav_h264_encoder as _libav_mod
        cls = _libav_mod.LibavH264Encoder
        src = textwrap.dedent(inspect.getsource(cls._encode))
        if 'pict_type = "I"' not in src:
            print("[patch] LibavH264Encoder._encode already correct, no patch needed")
            return False
        # av.video.frame.PictureType.I has integer value 1 (h264/ffmpeg standard).
        # Use the integer directly so we do not require importing the enum here.
        new_src = src.replace('pict_type = "I"', 'pict_type = 1  # patched for new PyAV')
        local_ns = {}
        exec(new_src, cls._encode.__globals__, local_ns)
        cls._encode = local_ns["_encode"]
        print("[patch] LibavH264Encoder._encode patched for newer python3-av")
        return True
    except Exception as e:
        print(f"[patch] Could not patch LibavH264Encoder: {e}. "
              f"If this Pi has new python3-av, recordings may produce 0-byte files.")
        return False


# Apply the patch unconditionally at startup. Harmless if the bug is not present.
if _HAS_LIBAV:
    _patch_libav_pict_type()

# Network ======================================================================
CLIENT_PORT = 5005
CONTROL_PORT = 5006
SYNC_MARGIN = 3.0
STOP_MARGIN = 2.0

# Heartbeat ====================================================================
HEARTBEAT_INTERVAL = 10.0     # server sends PING every N seconds
HEARTBEAT_TIMEOUT = 30.0      # client auto-stops if no msg from server for N seconds

# Defaults =====================================================================
DEFAULTS = {
    "output_width": 1536, "output_height": 864,
    "sensor_width": 2304, "sensor_height": 1296,
    "framerate": 56, "bitrate_mbps": 25,
    "video_dir": os.path.expanduser("~/camera_videos"),
}


def load_settings(config_path=None):
    if config_path is None:
        script_dir = os.path.dirname(os.path.abspath(__file__))
        config_path = os.path.join(script_dir, "camera_settings.json")
    settings = DEFAULTS.copy()
    if os.path.exists(config_path):
        try:
            with open(config_path, "r") as f:
                loaded = json.load(f)
            settings.update(loaded)
            print(f"  Loaded settings from {config_path}")
        except Exception as e:
            print(f"  Warning: could not load {config_path}: {e}")
    else:
        print(f"  No settings file at {config_path}, using defaults")
    if "video_dir" in settings:
        settings["video_dir"] = os.path.expanduser(settings["video_dir"])
    return settings


# Global state =================================================================
running = True
picam2 = None
encoder = None
recording = False
current_output = None
SETTINGS = None


def signal_handler(sig, frame):
    global running
    print("\nShutting down service...")
    running = False
    sys.exit(0)


signal.signal(signal.SIGINT, signal_handler)
signal.signal(signal.SIGTERM, signal_handler)


# Recording state ==============================================================
# Earlier versions tried two approaches that did not work on Pi 5:
#   1. Subclass FileOutput and call FileOutput._write directly, bypassing the
#      parent's outputframe(). This broke recording (0-byte .h264 files).
#   2. Pass pts=<path> to picam2.start_encoder(). The signature accepts it,
#      but picamera2's libav-based encoder path on Pi 5 silently ignores it,
#      so no .pts file gets written.
# This version uses TimestampingFileOutput, which:
#   - Forwards the frame to super().outputframe() FIRST (recording works)
#   - Writes the timestamp to a side-channel file AFTER as a side effect
#   - Uses *args/**kwargs to be robust to outputframe signature differences
#     across picamera2 versions
current_pts_path = None


class TimestampingFileOutput(FileOutput):
    """FileOutput that also records per-frame timestamps to a sidecar .pts file.

    The H264 video write goes through the unmodified parent FileOutput, so this
    subclass cannot break video recording (the v9.2.1 lesson). The timestamp
    write happens AFTER the parent's outputframe returns and any failure there
    is non-fatal."""

    def __init__(self, video_path, pts_path):
        super().__init__(video_path)
        self._pts_path = pts_path
        self._pts_file = None
        self._last_ts = None
        self._pts_count = 0

    def start(self):
        super().start()
        try:
            self._pts_file = open(self._pts_path, "w")
        except Exception as e:
            print(f"  WARNING: could not open PTS file {self._pts_path}: {e}")
            self._pts_file = None

    def outputframe(self, frame, *args, **kwargs):
        # Always call the parent FIRST so video recording is never affected.
        super().outputframe(frame, *args, **kwargs)
        # Now extract timestamp without assuming a specific signature.
        timestamp = kwargs.get("timestamp")
        if timestamp is None and len(args) >= 2:
            # Conventional positional order: (frame, keyframe, timestamp, ...)
            timestamp = args[1]
        if timestamp is None or self._pts_file is None:
            return
        # An I-frame is often emitted as multiple H264 packets (SPS, PPS, IDR)
        # with the same timestamp. Dedupe so the PTS file has one line per frame.
        if timestamp == self._last_ts:
            return
        self._last_ts = timestamp
        try:
            self._pts_file.write(f"{int(timestamp)}\n")
            self._pts_count += 1
        except Exception:
            pass

    def stop(self):
        super().stop()
        if self._pts_file is not None:
            try:
                self._pts_file.flush()
                self._pts_file.close()
            except Exception:
                pass
            self._pts_file = None

    @property
    def frame_count(self):
        return self._pts_count


# Camera =======================================================================
def setup_camera():
    global picam2
    out_w = SETTINGS["output_width"]
    out_h = SETTINGS["output_height"]
    sensor_w = SETTINGS["sensor_width"]
    sensor_h = SETTINGS["sensor_height"]
    fps = SETTINGS["framerate"]
    frame_duration = int(1_000_000 / fps)

    print("Initializing camera...")
    picam2 = Picamera2()
    props = picam2.camera_properties
    print(f"  Model: {props.get('Model', 'Unknown')}")

    # NoiseReductionModeEnum may not exist on some libcamera builds (Pi 5 / newer);
    # guard so we do not crash camera init if it is missing.
    _nrm = getattr(controls, "NoiseReductionModeEnum", None)
    _ctrls = {
        "FrameDurationLimits": (frame_duration, frame_duration),
    }
    if _nrm is not None:
        _ctrls["NoiseReductionMode"] = _nrm.Fast

    config = picam2.create_video_configuration(
        main={"size": (out_w, out_h), "format": "YUV420"},
        raw={"size": (sensor_w, sensor_h)},
        controls=_ctrls,
        buffer_count=6,
        encode="main",  # explicitly target the main YUV stream for H264 encoding
    )
    picam2.configure(config)
    try:
        if "AfMode" in picam2.camera_controls:
            picam2.set_controls({"AfMode": controls.AfModeEnum.Manual, "LensPosition": 0.5})
            print("  Focus: Manual (hyperfocal)")
        else:
            print("  Focus: Fixed (no AF motor)")
    except Exception as e:
        print(f"  Focus: Could not configure ({e})")

    print(f"  Sensor mode: {sensor_w}x{sensor_h} (full FOV)")
    print(f"  Output:      {out_w}x{out_h} @ {fps} fps")
    print(f"  Bitrate:     {SETTINGS['bitrate_mbps']} Mbps")
    picam2.start()
    print("  Camera started")


def wait_until(target_time):
    while time.time() < target_time:
        remaining = target_time - time.time()
        if remaining > 0.1:
            time.sleep(0.05)
        elif remaining > 0.01:
            time.sleep(0.001)


def create_h264_encoder(bitrate_bps, fps_num):
    """Create an H264 encoder. Defaults to V4L2 H264Encoder, which works
    reliably across our Pi 5 cameras (verified empirically).

    LibavH264Encoder is available as opt-in via SQUEAKSHOT_PREFER_LIBAV=1, but
    on newer python3-av (e.g. the version that ships with libcamera 0.7+),
    picamera2's libav_h264_encoder.py raises TypeError when it sets
    frame.pict_type = "I" (newer PyAV requires an enum/int, not a string).
    This is an upstream picamera2/PyAV mismatch we cannot fix from this script.

    iperiod set from configured FPS (a keyframe per ~1 second of footage)."""
    iperiod = max(2, int(fps_num))
    prefer_libav = os.environ.get("SQUEAKSHOT_PREFER_LIBAV", "0") == "1"
    if prefer_libav and _HAS_LIBAV:
        framerate = fractions.Fraction(int(fps_num), 1)
        print(f"  Encoder: LibavH264Encoder (PyAV, iperiod={iperiod}, fps={fps_num})")
        return LibavH264Encoder(
            bitrate=bitrate_bps, framerate=framerate, iperiod=iperiod
        )
    print(f"  Encoder: H264Encoder (V4L2, iperiod={iperiod}, fps={fps_num})")
    return H264Encoder(bitrate=bitrate_bps, iperiod=iperiod)


def _count_pts_frames(pts_path):
    """Count frames by counting lines in the PTS file picamera2 wrote."""
    if not pts_path or not os.path.exists(pts_path):
        return 0
    try:
        with open(pts_path) as f:
            return sum(1 for _ in f)
    except Exception:
        return 0


def start_recording(filename):
    global picam2, encoder, recording, current_output, current_pts_path
    os.makedirs(SETTINGS["video_dir"], exist_ok=True)
    video_path = os.path.join(SETTINGS["video_dir"], f"{filename}.h264")
    pts_path = os.path.join(SETTINGS["video_dir"], f"{filename}.pts")
    print(f"Starting recording: {filename}")
    encoder = create_h264_encoder(
        bitrate_bps=SETTINGS["bitrate_mbps"] * 1_000_000,
        fps_num=SETTINGS["framerate"],
    )
    # TimestampingFileOutput wraps stock FileOutput and writes the PTS file as
    # a side effect. The pts= kwarg on start_encoder is silently ignored by
    # picamera2's libav path on Pi 5, so we cannot rely on it.
    current_output = TimestampingFileOutput(video_path, pts_path)
    current_pts_path = pts_path
    picam2.start_encoder(encoder, current_output)
    # Nudge an IDR frame so the first NAL is a keyframe and downstream decoders
    # (ffmpeg, players) can lock on immediately. Wrapped because force_key_frame
    # is not present on every encoder build (the libav-based path lacks it).
    try:
        encoder.force_key_frame()
    except (AttributeError, Exception):
        pass  # silently skip; libav encoder produces a keyframe on frame 0 anyway
    recording = True
    print(f"  Recording to: {video_path}")
    return True


def stop_recording():
    global picam2, encoder, recording, current_output, current_pts_path
    if not recording:
        return 0
    # picam2.stop_encoder() can raise RuntimeError("Encoder already stopped")
    # if the encoder errored internally during start_encoder. Don't let that
    # leak up and crash the whole service.
    try:
        picam2.stop_encoder()
    except RuntimeError as e:
        print(f"  stop_encoder warning: {e}")
    except Exception as e:
        print(f"  stop_encoder unexpected error: {e}")
    # Give picamera2 a moment to flush the PTS file. Without this we sometimes
    # read 0 frames even when the .h264 has real data.
    time.sleep(0.25)
    frame_count = _count_pts_frames(current_pts_path)
    recording = False
    encoder = None
    current_output = None
    current_pts_path = None
    print(f"  Recording stopped. Frames: {frame_count}")
    return frame_count


# Length-prefixed protocol =====================================================
def send_message(sock, message):
    data = message.encode("utf-8")
    sock.sendall(len(data).to_bytes(4, "big") + data)


def recv_message(sock, timeout=None):
    sock.settimeout(timeout)
    try:
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
    except Exception:
        return None


# Server =======================================================================
class ClientConnection:
    def __init__(self, sock, addr, name="unknown"):
        self.sock = sock
        self.addr = addr
        self.name = name

    def fileno(self):
        return self.sock.fileno()


def _parse_start_cmd(parts):
    """Parse a START command and return {name: filename, ...} plus the server's
    own name/filename. Accepts the new name=filename format only."""
    pairs = {}
    for token in parts[1:]:
        if "=" not in token:
            return None
        name, fn = token.split("=", 1)
        pairs[name] = fn
    return pairs


def server_mode():
    global running, recording

    print("=" * 60)
    print(f"  SQUEAKSHOT SYNC SERVICE v9.2.5, SERVER")
    print("=" * 60)
    setup_camera()

    client_listen = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    client_listen.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    client_listen.bind(("0.0.0.0", CLIENT_PORT))
    client_listen.listen(8)
    client_listen.setblocking(False)

    control_listen = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    control_listen.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    control_listen.bind(("0.0.0.0", CONTROL_PORT))
    control_listen.listen(1)
    control_listen.setblocking(False)

    print(f"\nListening for clients on port {CLIENT_PORT}")
    print(f"Listening for controller on port {CONTROL_PORT}")
    print("Waiting for connections...\n")

    clients = {}
    clients_lock = threading.Lock()

    # control_holder lets the worker thread close/replace the controller socket
    # without us juggling a local variable across threads.
    control_holder = {"conn": None}

    # control_cmd_queue: main thread reads a command off the controller socket,
    # then hands it to a worker so the main select() loop can keep servicing
    # Picamera2 callbacks while wait_until + start_encoder runs (those calls
    # can block for hundreds of ms each, especially on first Libav startup).
    control_cmd_queue = queue.Queue()

    # control_busy: set before queueing a command, cleared in the worker's
    # finally block. While set, we remove the control socket from the select()
    # read list so the main thread does not race the worker's recv()/send().
    control_busy = threading.Event()

    def control_worker():
        while running:
            try:
                msg = control_cmd_queue.get(timeout=0.5)
            except queue.Empty:
                continue
            try:
                conn = control_holder["conn"]
                if conn is None:
                    continue
                new_conn = _handle_control_command(msg, clients, clients_lock, conn)
                control_holder["conn"] = new_conn
            except Exception as e:
                print(f"[CTRL-WORKER] Error handling {msg!r}: {e}")
            finally:
                control_busy.clear()

    threading.Thread(target=control_worker, daemon=True).start()

    # Heartbeat thread: send PING to all clients every HEARTBEAT_INTERVAL
    def heartbeat_loop():
        while running:
            time.sleep(HEARTBEAT_INTERVAL)
            with clients_lock:
                names = list(clients.keys())
            for name in names:
                with clients_lock:
                    c = clients.get(name)
                if c is None:
                    continue
                try:
                    send_message(c.sock, "PING")
                except Exception:
                    pass  # next select() will detect dead socket

    threading.Thread(target=heartbeat_loop, daemon=True).start()

    _last_progress_log = 0.0

    try:
        while running:
            # While the worker is processing a control command (e.g. waiting for
            # ACKs from all clients after a START), the main thread must NOT read
            # from client sockets either. Otherwise select() here races the
            # worker's recv_message() calls and whichever side wins consumes the
            # ACK, causing spurious "Clients did not ack" errors.
            busy = control_busy.is_set()
            with clients_lock:
                read_socks = [client_listen, control_listen]
                if not busy:
                    read_socks += [c.sock for c in clients.values()]
            # Only listen on the controller socket when the worker is idle. While
            # the worker is processing a command (and possibly sending OK:/ERROR:),
            # we must not recv() on it from the main thread.
            ctl_conn = control_holder["conn"]
            if ctl_conn and not busy:
                read_socks.append(ctl_conn)
            readable, _, _ = select.select(read_socks, [], [], 0.1)

            for sock in readable:
                if sock is client_listen:
                    _accept_client(client_listen, clients, clients_lock)
                elif sock is control_listen:
                    control_holder["conn"] = _accept_controller(
                        control_listen, clients, recording
                    )
                elif ctl_conn and sock is ctl_conn:
                    # Longer timeout (5s) for control reads: lab Wi-Fi / SSH muxing
                    # can stall briefly and 0.1s was too tight.
                    msg = recv_message(ctl_conn, timeout=5.0)
                    if msg is None:
                        print("[CONTROLLER] Disconnected")
                        try:
                            ctl_conn.close()
                        except Exception:
                            pass
                        control_holder["conn"] = None
                    elif msg:
                        # Set BEFORE put() so the next select() iteration already
                        # sees us as busy and skips ctl_conn and the client socks.
                        control_busy.set()
                        control_cmd_queue.put(msg)
                else:
                    _handle_client_message(sock, clients, clients_lock)

            # Periodic progress log: poll the pts file every ~5 seconds while
            # recording. The PTS file is owned by picamera2 now (via the pts=
            # kwarg on start_encoder), and counting lines is cheap.
            now = time.time()
            if recording and current_pts_path and (now - _last_progress_log) > 5.0:
                fc = _count_pts_frames(current_pts_path)
                if fc > 0:
                    print(f"  [RECORDING] Frames: {fc}")
                _last_progress_log = now
    except Exception as e:
        print(f"Server error: {e}")
        import traceback
        traceback.print_exc()
    finally:
        if recording:
            stop_recording()
        with clients_lock:
            for c in clients.values():
                try:
                    c.sock.close()
                except Exception:
                    pass
        if control_holder["conn"]:
            try:
                control_holder["conn"].close()
            except Exception:
                pass
        client_listen.close()
        control_listen.close()
        if picam2:
            picam2.close()


def _accept_client(listen_sock, clients, clients_lock):
    conn, addr = listen_sock.accept()
    conn.setblocking(False)
    conn.settimeout(2.0)
    try:
        msg = recv_message(conn, timeout=2.0)
        if msg and msg.startswith("HELLO:"):
            name = msg.split(":", 1)[1].strip()
            with clients_lock:
                if name in clients:
                    print(f"[CLIENT] {name} reconnecting, closing old socket")
                    try:
                        clients[name].sock.close()
                    except Exception:
                        pass
                clients[name] = ClientConnection(conn, addr, name)
            conn.setblocking(False)
            send_message(conn, "WELCOME")
            print(f"[CLIENT] {name} connected from {addr}")
        else:
            print(f"[CLIENT] Connection from {addr} sent no HELLO, closing")
            conn.close()
    except Exception as e:
        print(f"[CLIENT] Handshake error from {addr}: {e}")
        try:
            conn.close()
        except Exception:
            pass


def _accept_controller(listen_sock, clients, is_recording):
    conn, addr = listen_sock.accept()
    print(f"[CONTROLLER] Connected from {addr}")
    state = "RECORDING" if is_recording else "READY"
    names = ",".join(clients.keys()) if clients else ""
    # Send the initial STATUS while the socket is blocking. send_message uses
    # sendall, which on a non-blocking socket can raise BlockingIOError if the
    # send buffer is not immediately ready (this can happen right after accept()
    # under load). Flip to non-blocking after the handshake completes.
    conn.setblocking(True)
    try:
        send_message(conn, f"STATUS:{state}:{len(clients)}:{names}")
    finally:
        conn.setblocking(False)
    return conn


def _handle_client_message(sock, clients, clients_lock):
    """Read a message from a client. PONG responses are silently consumed.
    Disconnects remove the client from the dict."""
    with clients_lock:
        client = next((c for c in clients.values() if c.sock is sock), None)
    if not client:
        return
    msg = recv_message(sock, timeout=0.1)
    if msg is None:
        print(f"[CLIENT] {client.name} disconnected")
        try:
            sock.close()
        except Exception:
            pass
        with clients_lock:
            clients.pop(client.name, None)
    elif msg == "PONG":
        pass  # heartbeat response, ignore
    elif msg:
        print(f"[CLIENT/{client.name}] {msg}")


def _handle_control_command(msg, clients, clients_lock, control_conn):
    global recording

    print(f"[CONTROLLER] Command: {msg}")
    parts = msg.split(":")
    cmd = parts[0]

    if cmd == "START":
        if recording:
            send_message(control_conn, "ERROR:Already recording")
            return control_conn

        pairs = _parse_start_cmd(parts)
        if pairs is None or not pairs:
            send_message(
                control_conn,
                "ERROR:Invalid START format. Expected name=filename pairs."
            )
            return control_conn

        # Identify which entry is the server (= our own role).
        # Convention: the first pair is the server (controller emits it first).
        # We don't have our own name baked in, so just take the first as server.
        # That matches how the controller builds the message.
        first_name = list(pairs.keys())[0]
        server_filename = pairs[first_name]
        client_pairs = {name: fn for name, fn in list(pairs.items())[1:]}

        # Verify all named clients are actually connected (by name, not position!)
        with clients_lock:
            connected = set(clients.keys())
        missing = [n for n in client_pairs.keys() if n not in connected]
        if missing:
            send_message(
                control_conn, f"ERROR:Clients not connected: {','.join(missing)}"
            )
            return control_conn

        start_time = time.time() + SYNC_MARGIN

        # Send START to each named client
        for name, fn in client_pairs.items():
            with clients_lock:
                c = clients.get(name)
            if c is None:
                continue
            c.sock.setblocking(False)
            send_message(c.sock, f"START:{start_time}:{fn}")

        # Collect ACKs from those clients by name
        acked = set()
        deadline = time.time() + 5.0
        while len(acked) < len(client_pairs) and time.time() < deadline:
            for name in client_pairs.keys():
                if name in acked:
                    continue
                with clients_lock:
                    c = clients.get(name)
                if c is None:
                    continue
                msg2 = recv_message(c.sock, timeout=0.2)
                if msg2 == "ACK":
                    acked.add(name)
                elif msg2 == "PONG":
                    pass
                elif msg2 is not None and msg2 != "":
                    print(f"  [{name}] Unexpected: {msg2}")

        if len(acked) < len(client_pairs):
            still_missing = set(client_pairs.keys()) - acked
            send_message(
                control_conn,
                f"ERROR:Clients did not ack: {','.join(sorted(still_missing))}",
            )
            return control_conn

        print(f"  All clients acked. Waiting for sync time (T={start_time:.3f})...")
        wait_until(start_time)
        start_recording(server_filename)
        send_message(control_conn, "OK:Recording started")
        return control_conn

    elif cmd == "STOP":
        if not recording:
            send_message(control_conn, "ERROR:Not recording")
            return control_conn
        stop_time = time.time() + STOP_MARGIN
        with clients_lock:
            client_names = list(clients.keys())
        for name in client_names:
            with clients_lock:
                c = clients.get(name)
            if c is None:
                continue
            try:
                send_message(c.sock, f"STOP:{stop_time}")
            except Exception as e:
                print(f"  [{name}] Stop send failed: {e}")
        deadline = time.time() + 3.0
        for name in client_names:
            with clients_lock:
                c = clients.get(name)
            if c is None:
                continue
            remaining = max(0.0, deadline - time.time())
            recv_message(c.sock, timeout=remaining)
        print(f"  Waiting for sync stop time (T={stop_time:.3f})...")
        wait_until(stop_time)
        frame_count = stop_recording()
        send_message(control_conn, f"OK:Stopped with {frame_count} frames")
        return control_conn

    elif cmd == "STATUS":
        state_str = "RECORDING" if recording else "READY"
        frames = _count_pts_frames(current_pts_path) if recording else 0
        with clients_lock:
            names = ",".join(clients.keys()) if clients else ""
            count = len(clients)
        send_message(control_conn, f"STATUS:{state_str}:{count}:{names}:{frames}")
        return control_conn

    else:
        send_message(control_conn, f"ERROR:Unknown command {cmd}")
        return control_conn


# Client =======================================================================
def client_mode(server_ip, camera_name):
    global running, recording

    print("=" * 60)
    print(f"  SQUEAKSHOT SYNC SERVICE v9.2.5, CLIENT ({camera_name})")
    print("=" * 60)
    print(f"  Server: {server_ip}")
    setup_camera()

    server_conn = None
    last_server_msg_time = time.time()
    _last_progress_log = 0.0

    while running:
        if not server_conn:
            try:
                print(f"\nConnecting to server at {server_ip}:{CLIENT_PORT}...")
                server_conn = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                server_conn.connect((server_ip, CLIENT_PORT))
                send_message(server_conn, f"HELLO:{camera_name}")
                welcome = recv_message(server_conn, timeout=5.0)
                if welcome == "WELCOME":
                    print(f"  Identified as {camera_name}, server welcomed")
                    last_server_msg_time = time.time()
                else:
                    print(f"  Unexpected handshake response: {welcome}")
                    server_conn.close()
                    server_conn = None
                    time.sleep(3)
                    continue
            except ConnectionRefusedError:
                print(f"  Connection refused, retrying in 3 seconds...")
                server_conn = None
                time.sleep(3)
                continue
            except Exception as e:
                print(f"  Error: {e}, retrying in 3 seconds...")
                if server_conn:
                    try:
                        server_conn.close()
                    except Exception:
                        pass
                server_conn = None
                time.sleep(3)
                continue

        msg = recv_message(server_conn, timeout=1.0)

        # Heartbeat timeout check: if we're recording but server has been
        # silent for too long, assume server is gone and stop to save data.
        if recording and (time.time() - last_server_msg_time) > HEARTBEAT_TIMEOUT:
            print(f"  WARNING: No server message in {HEARTBEAT_TIMEOUT}s while recording")
            print(f"  Auto-stopping to preserve data")
            stop_recording()
            # Drop the connection too: likely dead
            try:
                server_conn.close()
            except Exception:
                pass
            server_conn = None
            continue

        if msg is None:
            # Probe connection health
            try:
                server_conn.send(b"")
            except Exception:
                print("\nServer disconnected, will reconnect...")
                if recording:
                    print("  Auto-stopping recording on disconnect")
                    stop_recording()
                try:
                    server_conn.close()
                except Exception:
                    pass
                server_conn = None
            continue

        # Got a message: server is alive
        last_server_msg_time = time.time()

        if not msg:
            continue

        if msg == "PING":
            # Heartbeat: respond and continue
            try:
                send_message(server_conn, "PONG")
            except Exception:
                pass
            continue

        print(f"[SERVER] {msg}")
        parts = msg.split(":")
        cmd = parts[0]

        if cmd == "START":
            start_time = float(parts[1])
            filename = parts[2]
            send_message(server_conn, "ACK")
            wait_time = start_time - time.time()
            print(f"  Starting in {wait_time:.3f}s")
            wait_until(start_time)
            start_recording(filename)

        elif cmd == "STOP":
            stop_time = float(parts[1])
            send_message(server_conn, "ACK")
            wait_time = stop_time - time.time()
            print(f"  Stopping in {wait_time:.3f}s")
            wait_until(stop_time)
            frame_count = stop_recording()
            print(f"  Stopped with {frame_count} frames")

            print(f"  Stopping in {wait_time:.3f}s")
            wait_until(stop_time)
            frame_count = stop_recording()
            print(f"  Stopped with {frame_count} frames")

        # Periodic progress log (every ~5s while recording)
        now = time.time()
        if recording and current_pts_path and (now - _last_progress_log) > 5.0:
            fc = _count_pts_frames(current_pts_path)
            if fc > 0:
                print(f"  [RECORDING] Frames: {fc}")
            _last_progress_log = now

    if recording:
        stop_recording()
    if server_conn:
        try:
            server_conn.close()
        except Exception:
            pass
    if picam2:
        picam2.close()


# Entry point ==================================================================
def main():
    global SETTINGS
    parser = argparse.ArgumentParser(description="SqueakShot sync capture v9.1")
    parser.add_argument("mode", choices=["server", "client"])
    parser.add_argument("--server-ip", help="Server IP (required for client mode)")
    parser.add_argument("--name", default="cam1",
                        help="Camera name for client mode (default: cam1)")
    parser.add_argument("--config", help="Path to camera_settings.json")
    args = parser.parse_args()

    if args.mode == "client" and not args.server_ip:
        parser.error("--server-ip is required for client mode")

    SETTINGS = load_settings(args.config)
    os.makedirs(SETTINGS["video_dir"], exist_ok=True)

    if args.mode == "server":
        server_mode()
    else:
        client_mode(args.server_ip, args.name)


if __name__ == "__main__":
    main()
