#!/usr/bin/env python3
"""
Synchronized Camera Capture Service v9.1
Multi-camera star topology: one server, N clients.

Changes from v9.0:
- START command parses name=filename pairs (no more positional cam1/cam2 assumption)
- Server sends PING every 10s; clients auto-stop recording on >30s server silence
- Client tracks last_server_msg_time for heartbeat health

Usage:
    Server: python3 sync_capture.py server [--config <path>]
    Client: python3 sync_capture.py client --server-ip <IP> --name <name> [--config <path>]
"""

import argparse
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


# PTS output ===================================================================
class PTSOutput(FileOutput):
    def __init__(self, video_file, pts_file):
        super().__init__(video_file)
        self.pts_file = open(pts_file, "w")
        self.frame_count = 0
        self.start_time = None

    def outputframe(self, frame, keyframe=True, timestamp=None):
        super().outputframe(frame, keyframe, timestamp)
        if timestamp is not None:
            if self.start_time is None:
                self.start_time = timestamp
            self.pts_file.write(f"{timestamp - self.start_time}\n")
            self.frame_count += 1

    def close(self):
        if self.pts_file:
            self.pts_file.close()
        super().close()


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

    config = picam2.create_video_configuration(
        main={"size": (out_w, out_h), "format": "YUV420"},
        raw={"size": (sensor_w, sensor_h)},
        controls={
            "FrameDurationLimits": (frame_duration, frame_duration),
            "NoiseReductionMode": controls.NoiseReductionModeEnum.Fast,
        },
        buffer_count=6,
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


def start_recording(filename):
    global picam2, encoder, recording, current_output
    os.makedirs(SETTINGS["video_dir"], exist_ok=True)
    video_path = os.path.join(SETTINGS["video_dir"], f"{filename}.h264")
    pts_path = os.path.join(SETTINGS["video_dir"], f"{filename}.pts")
    print(f"Starting recording: {filename}")
    encoder = H264Encoder(bitrate=SETTINGS["bitrate_mbps"] * 1_000_000)
    current_output = PTSOutput(video_path, pts_path)
    picam2.start_encoder(encoder, current_output)
    recording = True
    print(f"  Recording to: {video_path}")
    return True


def stop_recording():
    global picam2, encoder, recording, current_output
    if not recording:
        return 0
    picam2.stop_encoder()
    frame_count = current_output.frame_count if current_output else 0
    if current_output:
        current_output.close()
    recording = False
    encoder = None
    current_output = None
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
    print(f"  SQUEAKSHOT SYNC SERVICE v9.1, SERVER")
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
    control_conn = None
    clients_lock = threading.Lock()

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

    try:
        while running:
            with clients_lock:
                read_socks = [client_listen, control_listen] + [c.sock for c in clients.values()]
            if control_conn:
                read_socks.append(control_conn)
            readable, _, _ = select.select(read_socks, [], [], 0.1)

            for sock in readable:
                if sock is client_listen:
                    _accept_client(client_listen, clients, clients_lock)
                elif sock is control_listen:
                    control_conn = _accept_controller(control_listen, clients, recording)
                elif control_conn and sock is control_conn:
                    msg = recv_message(control_conn, timeout=0.1)
                    if msg is None:
                        print("[CONTROLLER] Disconnected")
                        control_conn.close()
                        control_conn = None
                    elif msg:
                        control_conn = _handle_control_command(msg, clients, clients_lock, control_conn)
                else:
                    _handle_client_message(sock, clients, clients_lock)

            if recording and current_output and current_output.frame_count > 0:
                if current_output.frame_count % 500 == 0:
                    print(f"  [RECORDING] Frames: {current_output.frame_count}")
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
        if control_conn:
            control_conn.close()
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
    conn.setblocking(False)
    print(f"[CONTROLLER] Connected from {addr}")
    state = "RECORDING" if is_recording else "READY"
    names = ",".join(clients.keys()) if clients else ""
    send_message(conn, f"STATUS:{state}:{len(clients)}:{names}")
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
        frames = current_output.frame_count if current_output else 0
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
    print(f"  SQUEAKSHOT SYNC SERVICE v9.1, CLIENT ({camera_name})")
    print("=" * 60)
    print(f"  Server: {server_ip}")
    setup_camera()

    server_conn = None
    last_server_msg_time = time.time()

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

        if recording and current_output and current_output.frame_count > 0:
            if current_output.frame_count % 500 == 0:
                print(f"  [RECORDING] Frames: {current_output.frame_count}")

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
