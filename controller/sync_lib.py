"""
Shared synchronization primitives.

This is the canonical implementation of the N-camera frame matching algorithm.
Both controller/camera_controller.py and tools/sync_videos.py import from here
so there is only one place to fix bugs.
"""

import numpy as np


MATCH_TOLERANCE_US = 20_000  # 20 ms, frames further apart than this won't match
FRAME_COUNT_DISCREPANCY_WARN = 0.05  # warn if any camera has >5% fewer frames than the longest


def load_pts(path):
    """Load a PTS file (one microsecond timestamp per line)."""
    return np.loadtxt(path, dtype=np.int64)


def match_n_cameras(pts_list):
    """Match frames across N cameras to the shortest one.

    Returns a list of index arrays, one per camera, all the same length.
    A frame is matched only if every camera has a frame within
    MATCH_TOLERANCE_US of the reference timestamp.

    The shortest sequence is used as reference. This is intentional:
    if one camera dropped frames, we can't sync to frames it doesn't have.
    But callers should check `analyze_frame_counts()` to detect when this
    is happening so they can warn the user before silently clipping.
    """
    relative = [p - p[0] for p in pts_list]
    common_end = min(r[-1] for r in relative)
    trimmed = [r[r <= common_end] for r in relative]

    ref_idx = min(range(len(trimmed)), key=lambda i: len(trimmed[i]))
    ref_pts = trimmed[ref_idx]

    matched = [[] for _ in pts_list]
    for ref_frame_idx, t in enumerate(ref_pts):
        per_cam_idx = []
        ok = True
        for cam_i, r in enumerate(relative):
            if cam_i == ref_idx:
                per_cam_idx.append(ref_frame_idx)
                continue
            idx = int(np.argmin(np.abs(r - t)))
            if abs(int(r[idx]) - int(t)) > MATCH_TOLERANCE_US:
                ok = False
                break
            per_cam_idx.append(idx)
        if ok:
            for cam_i, frame_i in enumerate(per_cam_idx):
                matched[cam_i].append(frame_i)

    return [np.array(m, dtype=np.int64) for m in matched]


def analyze_frame_counts(pts_list, camera_names=None):
    """Return a dict describing per-camera frame counts and any discrepancy.

    Used to warn the user when one camera dropped a lot of frames and the
    sync algorithm is about to silently clip everyone else.
    """
    counts = [len(p) for p in pts_list]
    max_count = max(counts)
    min_count = min(counts)
    discrepancy_ratio = (max_count - min_count) / max_count if max_count > 0 else 0
    short_idx = counts.index(min_count)

    return {
        "counts": counts,
        "max_count": max_count,
        "min_count": min_count,
        "discrepancy_frames": max_count - min_count,
        "discrepancy_ratio": discrepancy_ratio,
        "warn": discrepancy_ratio > FRAME_COUNT_DISCREPANCY_WARN,
        "shortest_camera": camera_names[short_idx] if camera_names else f"camera index {short_idx}",
        "longest_count": max_count,
    }


def compute_timing_quality(pts_list, matched_indices):
    """Return (avg_diff_ms, max_diff_ms, quality_label) across matched frames."""
    if not matched_indices or len(matched_indices[0]) == 0:
        return 0.0, 0.0, "No matches"

    ref_rel = pts_list[0] - pts_list[0][0]
    ref_ts = ref_rel[matched_indices[0]]
    diffs = []
    for i in range(1, len(pts_list)):
        rel = pts_list[i] - pts_list[i][0]
        ts = rel[matched_indices[i]]
        diffs.extend(np.abs(ts - ref_ts).tolist())

    if not diffs:
        return 0.0, 0.0, "Excellent"

    avg_ms = float(np.mean(diffs)) / 1000
    max_ms = float(np.max(diffs)) / 1000
    if max_ms < 5:
        quality = "Excellent"
    elif max_ms < 10:
        quality = "Good"
    elif max_ms < 20:
        quality = "Fair"
    else:
        quality = "Poor"
    return avg_ms, max_ms, quality
