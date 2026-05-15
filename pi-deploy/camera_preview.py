#!/usr/bin/env python3
"""
Camera Preview Server
Runs on a Raspberry Pi to stream a low-res MJPEG preview.
Mutually exclusive with sync_capture (they share the camera).

Usage:
    python3 camera_preview.py
    
Access stream at: http://<pi-ip>:8080/stream.mjpg
"""

import io
import json
import os
import sys
import traceback
from http.server import BaseHTTPRequestHandler, HTTPServer
from threading import Condition

from picamera2 import Picamera2
from picamera2.encoders import JpegEncoder
from picamera2.outputs import FileOutput

# Preview settings (fixed: independent of recording resolution)
PREVIEW_WIDTH = 640
PREVIEW_HEIGHT = 360
PREVIEW_FPS = 15
PORT = 8080


class StreamingOutput(io.BufferedIOBase):
    def __init__(self):
        self.frame = None
        self.condition = Condition()

    def write(self, buf):
        with self.condition:
            self.frame = buf
            self.condition.notify_all()


class StreamingHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path == "/":
            self.send_response(200)
            self.send_header("Content-type", "text/html")
            self.send_header("Access-Control-Allow-Origin", "*")
            self.end_headers()
            html = (
                "<html><body><h1>SqueakShot Preview</h1>"
                '<img src="/stream.mjpg" width="640" height="360" /></body></html>'
            )
            self.wfile.write(html.encode())
        elif self.path == "/stream.mjpg":
            self.send_response(200)
            self.send_header("Age", 0)
            self.send_header("Cache-Control", "no-cache, private")
            self.send_header("Pragma", "no-cache")
            self.send_header(
                "Content-Type", "multipart/x-mixed-replace; boundary=FRAME"
            )
            self.send_header("Access-Control-Allow-Origin", "*")
            self.end_headers()
            try:
                while True:
                    with output.condition:
                        output.condition.wait()
                        frame = output.frame
                    self.wfile.write(b"--FRAME\r\n")
                    self.send_header("Content-Type", "image/jpeg")
                    self.send_header("Content-Length", len(frame))
                    self.end_headers()
                    self.wfile.write(frame)
                    self.wfile.write(b"\r\n")
            except Exception as e:
                print(f"Removed streaming client {self.client_address}: {e}")
        elif self.path == "/snapshot.jpg":
            self.send_response(200)
            self.send_header("Content-type", "image/jpeg")
            self.send_header("Access-Control-Allow-Origin", "*")
            self.end_headers()
            with output.condition:
                output.condition.wait()
                self.wfile.write(output.frame)
        elif self.path == "/status":
            self.send_response(200)
            self.send_header("Content-type", "text/plain")
            self.send_header("Access-Control-Allow-Origin", "*")
            self.end_headers()
            self.wfile.write(
                f"OK {PREVIEW_WIDTH}x{PREVIEW_HEIGHT}@{PREVIEW_FPS}".encode()
            )
        else:
            self.send_error(404)
            self.end_headers()

    def log_message(self, format, *args):
        return

    def do_OPTIONS(self):
        self.send_response(200)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "*")
        self.end_headers()


class StreamingServer(HTTPServer):
    allow_reuse_address = True


def main():
    global output
    print(f"Starting preview server on port {PORT}")
    print(f"  Resolution: {PREVIEW_WIDTH}x{PREVIEW_HEIGHT} @ {PREVIEW_FPS}fps")

    try:
        picam2 = Picamera2()
        config = picam2.create_video_configuration(
            main={"size": (PREVIEW_WIDTH, PREVIEW_HEIGHT), "format": "RGB888"},
            controls={"FrameRate": PREVIEW_FPS},
        )
        picam2.configure(config)
        output = StreamingOutput()
        picam2.start_recording(JpegEncoder(), FileOutput(output))
        print(f"Preview live at http://<this-pi-ip>:{PORT}/stream.mjpg")
        print("Press Ctrl+C to stop\n")
        server = StreamingServer(("", PORT), StreamingHandler)
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nShutting down...")
    except Exception as e:
        print(f"\nERROR: {e}")
        print("\nIf camera is busy, stop the recording service first:")
        print("  sudo systemctl stop squeakshot-record")
        traceback.print_exc()
        sys.exit(1)
    finally:
        try:
            picam2.stop_recording()
        except Exception:
            pass


if __name__ == "__main__":
    main()
