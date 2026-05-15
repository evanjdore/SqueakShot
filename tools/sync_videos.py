#!/usr/bin/env python3
"""
SqueakShot Video Synchronization Tool v9.1
Multi-camera offline sync using PTS timestamps.

The matching algorithm lives in controller/sync_lib.py, this tool
imports it so there's only one place to fix bugs.

Usage:
    GUI:   python sync_videos.py
    CLI:   python sync_videos.py --recording mouse001_test --cameras cam0 cam1 cam2
"""

import argparse
import os
import subprocess
import sys
from pathlib import Path

import numpy as np

# Pull the canonical matching algo from the controller package
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "controller"))
import sync_lib  # noqa: E402

load_pts = sync_lib.load_pts
match_n_cameras = sync_lib.match_n_cameras
MATCH_TOLERANCE_US = sync_lib.MATCH_TOLERANCE_US


def sync_recording(recording_name, camera_names, encoded_dir, output_dir, fps=56,
                   log=print):
    """Run sync for one recording across N cameras."""
    encoded_dir = Path(encoded_dir)
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    log(f"Synchronizing {recording_name} across {len(camera_names)} cameras...")

    # Load PTS
    pts_list = []
    for cam in camera_names:
        pts_path = encoded_dir / f"{cam}_{recording_name}.pts"
        if not pts_path.exists():
            log(f"ERROR: missing PTS file {pts_path}")
            return False
        pts_list.append(load_pts(pts_path))
        log(f"  {cam}: {len(pts_list[-1])} frames")

    # Frame-count discrepancy check (warn before silently clipping)
    fc = sync_lib.analyze_frame_counts(pts_list, camera_names)
    if fc["warn"]:
        log(f"  WARNING: frame counts disagree by {fc['discrepancy_frames']} "
            f"({fc['discrepancy_ratio']*100:.1f}%). "
            f"{fc['shortest_camera']} has the fewest frames, sync will clip "
            f"everyone to that length.")

    # Match frames
    matched = match_n_cameras(pts_list)
    n_matched = len(matched[0])
    log(f"  Matched {n_matched} frames across all cameras")

    if n_matched == 0:
        log("ERROR: no frames matched within tolerance")
        return False

    # Sync quality
    ref_rel = pts_list[0] - pts_list[0][0]
    ref_ts = ref_rel[matched[0]]
    all_diffs = []
    for i in range(1, len(pts_list)):
        rel = pts_list[i] - pts_list[i][0]
        ts = rel[matched[i]]
        all_diffs.extend(np.abs(ts - ref_ts).tolist())
    if all_diffs:
        avg_ms = np.mean(all_diffs) / 1000
        max_ms = np.max(all_diffs) / 1000
        log(f"  Timing: avg {avg_ms:.2f} ms, max {max_ms:.2f} ms")

    # Trim each video
    for cam, frames in zip(camera_names, matched):
        in_mp4 = encoded_dir / f"{cam}_{recording_name}.mp4"
        out_mp4 = output_dir / f"{cam}_{recording_name}_synced.mp4"
        out_pts = output_dir / f"{cam}_{recording_name}_synced.pts"

        if not in_mp4.exists():
            log(f"  X {cam}: input MP4 not found at {in_mp4}")
            continue

        start_frame = int(frames[0])
        start_sec = start_frame / fps
        log(f"  {cam}: trim from frame {start_frame}, keep {len(frames)} frames")

        cmd = [
            "ffmpeg", "-y",
            "-i", str(in_mp4),
            "-ss", f"{start_sec:.6f}",
            "-frames:v", str(len(frames)),
            "-c:v", "libx264",
            "-preset", "medium",
            "-crf", "18",
            str(out_mp4),
        ]
        result = subprocess.run(cmd, capture_output=True, text=True)
        if result.returncode == 0:
            log(f"    + Wrote {out_mp4.name}")
            cam_idx = camera_names.index(cam)
            selected = pts_list[cam_idx][frames]
            selected = selected - selected[0]
            np.savetxt(out_pts, selected, fmt="%d")
        else:
            log(f"    X ffmpeg failed: {result.stderr[:300]}")
            return False

    log("Sync complete.")
    return True


# ============================================================================
# Tkinter GUI
# ============================================================================
def run_gui():
    import tkinter as tk
    from tkinter import filedialog, ttk, scrolledtext, messagebox

    root = tk.Tk()
    root.title("SqueakShot Sync v9.1")
    root.geometry("780x600")

    # Top frame: select encoded dir
    top = tk.Frame(root, padx=12, pady=10)
    top.pack(fill="x")

    tk.Label(top, text="Encoded folder:").grid(row=0, column=0, sticky="w")
    enc_var = tk.StringVar(value=str(Path.home() / "SqueakShot_Videos" / "encoded"))
    tk.Entry(top, textvariable=enc_var, width=60).grid(row=0, column=1, padx=5)
    tk.Button(top, text="Browse",
              command=lambda: enc_var.set(filedialog.askdirectory()
                                          or enc_var.get())).grid(row=0, column=2)

    tk.Label(top, text="Output folder:").grid(row=1, column=0, sticky="w", pady=5)
    out_var = tk.StringVar(value=str(Path.home() / "SqueakShot_Videos" / "synced"))
    tk.Entry(top, textvariable=out_var, width=60).grid(row=1, column=1, padx=5)
    tk.Button(top, text="Browse",
              command=lambda: out_var.set(filedialog.askdirectory()
                                          or out_var.get())).grid(row=1, column=2)

    tk.Label(top, text="FPS:").grid(row=2, column=0, sticky="w")
    fps_var = tk.IntVar(value=56)
    tk.Entry(top, textvariable=fps_var, width=10).grid(row=2, column=1, sticky="w", padx=5)

    # Recordings list (detected from cam0_*.mp4 files in encoded dir)
    list_frame = tk.Frame(root, padx=12)
    list_frame.pack(fill="both", expand=True)
    tk.Label(list_frame, text="Recordings (found in encoded folder):").pack(anchor="w")
    listbox = tk.Listbox(list_frame, height=8, font=("Monaco", 10))
    listbox.pack(fill="both", expand=True, pady=5)

    log_widget = scrolledtext.ScrolledText(root, height=12, font=("Monaco", 9))
    log_widget.pack(fill="both", expand=True, padx=12, pady=10)

    def log(msg):
        log_widget.insert("end", str(msg) + "\n")
        log_widget.see("end")
        root.update_idletasks()

    def find_cameras_for(recording):
        cams = set()
        enc = Path(enc_var.get())
        for p in enc.glob(f"cam*_{recording}.pts"):
            cams.add(p.name.split("_", 1)[0])
        return sorted(cams)

    def refresh_list():
        listbox.delete(0, "end")
        enc = Path(enc_var.get())
        if not enc.is_dir():
            log(f"Encoded folder does not exist: {enc}")
            return
        # Find any cam0_*.pts files; recording name is whatever comes after cam0_
        seen = set()
        for p in enc.glob("cam0_*.pts"):
            name = p.stem[5:]  # strip "cam0_"
            if name in seen:
                continue
            seen.add(name)
            cams = find_cameras_for(name)
            listbox.insert("end", f"{name}  ({', '.join(cams)})")
        log(f"Found {listbox.size()} recordings.")

    def run_sync():
        sel = listbox.curselection()
        if not sel:
            messagebox.showwarning("No selection", "Pick a recording first.")
            return
        text = listbox.get(sel[0])
        name = text.split("  ")[0]
        cams = find_cameras_for(name)
        if not cams:
            log(f"No PTS files found for {name}")
            return
        log_widget.delete("1.0", "end")
        sync_recording(name, cams, enc_var.get(), out_var.get(),
                       fps=fps_var.get(), log=log)

    btns = tk.Frame(root, padx=12, pady=8)
    btns.pack(fill="x")
    tk.Button(btns, text="Refresh", command=refresh_list).pack(side="left", padx=5)
    tk.Button(btns, text="Sync Selected", command=run_sync,
              bg="#10b981", fg="white").pack(side="left", padx=5)

    refresh_list()
    root.mainloop()


def main():
    parser = argparse.ArgumentParser(description="Sync videos across N cameras")
    parser.add_argument("--recording", help="Recording name (sans cam prefix and extension)")
    parser.add_argument("--cameras", nargs="+", help="Camera names (e.g. cam0 cam1 cam2)")
    parser.add_argument("--encoded-dir", default=str(Path.home() / "SqueakShot_Videos" / "encoded"))
    parser.add_argument("--output-dir", default=str(Path.home() / "SqueakShot_Videos" / "synced"))
    parser.add_argument("--fps", type=int, default=56)
    parser.add_argument("--gui", action="store_true", help="Force GUI")

    args = parser.parse_args()

    if args.gui or (not args.recording and not args.cameras):
        run_gui()
        return

    if not args.recording or not args.cameras:
        parser.error("--recording and --cameras both required for CLI mode")

    ok = sync_recording(
        args.recording, args.cameras, args.encoded_dir, args.output_dir, fps=args.fps
    )
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
