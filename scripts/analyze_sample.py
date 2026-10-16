"""Read-only audit of this assignment's single-episode local snapshot.

Usage: python scripts/analyze_sample.py DATA_ROOT --output-dir docs/evidence
Requires numpy, pyarrow, av, Pillow. No network, credentials or dataset writes.
Not a general LeRobot loader or an agent-facing tool. Output contains aggregate
statistics and three annotated sample frames, never full numeric arrays/video.
"""

import argparse
import hashlib
import json
from collections import Counter
from pathlib import Path

import av
import numpy as np
import pyarrow as pa
import pyarrow.parquet as pq
from PIL import ImageDraw


def distribution(values):
    a = np.asarray(values, dtype=np.float64)
    return dict(zip(["min", "p50", "p95", "p99", "max"],
                    map(float, np.percentile(a, [0, 50, 95, 99, 100]))))


def rotation_xyzw(q):
    x, y, z, w = q
    return np.array([
        [1-2*(y*y+z*z), 2*(x*y-z*w), 2*(x*z+y*w)],
        [2*(x*y+z*w), 1-2*(x*x+z*z), 2*(y*z-x*w)],
        [2*(x*z-y*w), 2*(y*z+x*w), 1-2*(x*x+y*y)],
    ])


def audit(root, output):
    root, output = root.resolve(), output.resolve()
    if root == output or root in output.parents:
        raise ValueError("Output must be outside the read-only dataset directory")
    info = json.loads((root / "meta/info.json").read_text(encoding="utf-8"))
    episodes = pq.read_table(root / "meta/episodes.parquet").to_pylist()
    if len(episodes) != 1:
        raise ValueError("This reconnaissance script supports exactly one episode")
    ep = episodes[0]
    camera = "base_0_camera/rgb/image"
    data_key = info["data_path"].format(
        chunk_index=ep["data/chunk_index"], file_index=ep["data/file_index"])
    video_key = info["video_path"].format(
        video_key=camera, chunk_index=ep[f"videos/{camera}/chunk_index"],
        file_index=ep[f"videos/{camera}/file_index"])
    parquet = pq.ParquetFile(root / data_key)
    if parquet.metadata.num_rows > 10000:
        raise ValueError("Reconnaissance row limit exceeded (10,000)")
    table = parquet.read()
    arrays = {name: np.asarray(table[name].to_pylist()) for name in table.column_names}
    n = table.num_rows
    if n < 2:
        raise ValueError("At least two frames are required")
    ts = arrays["timestamp"].astype(np.float64)
    ns = arrays["timestamp_ns"]
    embedded = ["base_0_camera/position", "base_0_camera/quaternion_xyzw",
                "left_hand/tracks", "right_hand/tracks"]
    report = {"scope": "whole single episode; no population generalization",
              "versions": {"numpy": np.__version__, "pyarrow": pa.__version__, "av": av.__version__},
              "objects": [], "rows": n, "columns": table.num_columns,
              "row_groups": parquet.metadata.num_row_groups, "fields": {},
              "episode_metadata": ep, "tasks": pq.read_table(root / "meta/tasks.parquet").to_pylist()}
    for path in sorted(root.rglob("*")):
        if path.is_file():
            with path.open("rb") as handle:
                digest = hashlib.file_digest(handle, "sha256").hexdigest()
            report["objects"].append({"key": path.relative_to(root).as_posix(),
                "size_bytes": path.stat().st_size,
                "sha256": digest})
    report["total_bytes"] = sum(x["size_bytes"] for x in report["objects"])
    for name, a in arrays.items():
        feature = info["features"][name]
        report["fields"][name] = {"arrow_type": str(table[name].type),
            "declared_dtype": feature["dtype"], "declared_shape": feature["shape"],
            "observed_shape": list(a.shape), "null_rows": table[name].null_count,
            "nonfinite_values": int(np.count_nonzero(~np.isfinite(a)))}
    report["time"] = {
        "last_timestamp_s": float(ts[-1]), "last_timestamp_ns": int(ns[-1]),
        "timestamp_ns_increasing": bool(np.all(np.diff(ns) > 0)),
        "dt_ns_histogram": {str(k): int(v) for k, v in Counter(np.diff(ns)).items()},
        "float32_vs_ns_max_abs_s": float(np.max(np.abs(ts - ns / 1e9))),
        "nominal_fps": info["fps"],
        "ns_implied_fps": float((n - 1) / ((ns[-1] - ns[0]) / 1e9)),
        "last_frame_over_nominal_fps_error_s": float((n-1)/info["fps"] - ns[-1]/1e9),
        "frame_index_contiguous": bool(np.array_equal(arrays["frame_index"], np.arange(n))),
        "global_index_contiguous": bool(np.array_equal(arrays["index"], np.arange(n))),
        "episode_ids": np.unique(arrays["episode_index"]).tolist(),
        "task_ids": np.unique(arrays["task_index"]).tolist(),
        "embedded_timestamp_max_abs_ns": {name: float(np.max(np.abs(arrays[name][:, 0] - ns))) for name in embedded},
        "float32_cast_ns_max_abs_error": float(np.max(np.abs(ns.astype(np.float32).astype(np.float64) - ns))),
    }
    position = arrays["base_0_camera/position"][:, 1:]
    q = arrays["base_0_camera/quaternion_xyzw"][:, 1:]
    report["camera"] = {"translation_axis_min_m": position.min(axis=0).tolist(),
        "translation_axis_max_m": position.max(axis=0).tolist(),
        "step_m": distribution(np.linalg.norm(np.diff(position, axis=0), axis=1)),
        "quaternion_norm_max_abs_error": float(np.max(np.abs(np.linalg.norm(q, axis=1) - 1)))}
    points = {s: arrays[f"{s}_hand/tracks"][:, 1:].reshape(n, 21, 3) for s in ["left", "right"]}
    report["hands"] = {}
    for side, xyz in points.items():
        act = arrays[f"action.{side}_hand_tracks"]
        quat = arrays[f"observation.{side}_keypoints_quaternion"].reshape(n, 21, 4)
        mano = arrays[f"observation.{side}_hand_mano"]
        step = np.linalg.norm(np.diff(xyz, axis=0), axis=2)
        largest = np.unravel_index(np.argmax(step), step.shape)
        report["hands"][side] = {
            "xyz_world_min_m": xyz.min(axis=(0, 1)).tolist(), "xyz_world_max_m": xyz.max(axis=(0, 1)).tolist(),
            "per_frame_mean_joint_step_m": distribution(step.mean(axis=1)),
            "max_individual_joint_step_m": float(step.max()),
            "max_step_transition": [int(largest[0]), int(largest[0]+1)],
            "max_step_joint_index": int(largest[1]),
            "transitions_any_joint_over_0_1m": int(np.count_nonzero(np.max(step, axis=1) > 0.1)),
            "duplicate_consecutive_tracks": int(np.count_nonzero(np.all(xyz[1:] == xyz[:-1], axis=(1, 2)))),
            "action_next_frame_max_abs_error_m": float(np.max(np.abs(act[:-1] - xyz[1:].reshape(n-1, 63)))),
            "terminal_action_all_zero": bool(np.all(act[-1] == 0)),
            "mano_betas_all_zero": bool(np.all(mano[:, 48:58] == 0)),
            "mano_translation_min_m": mano[:, 58:61].min(axis=0).tolist(),
            "mano_translation_max_m": mano[:, 58:61].max(axis=0).tolist(),
            "quaternion_norm_max_abs_error": float(np.max(np.abs(np.linalg.norm(quat, axis=2) - 1))),
        }
    # World pose interpreted as camera-to-world rotation, supported by sample overlays.
    K = np.asarray(info["features"]["base_0_camera/intrinsics"])
    width, height = info["features"]["base_0_camera/resolution"]
    uv_all, front_all = [], []
    for i in range(n):
        world = np.concatenate([points["left"][i], points["right"][i]])
        pc = (world - position[i]) @ rotation_xyzw(q[i])
        ph = pc @ K.T
        uv_all.append(ph[:, :2] / ph[:, 2:])
        front_all.append(pc[:, 2] > 0)
    uv = np.asarray(uv_all)
    front = np.asarray(front_all)
    inside = front & (uv[:, :, 0] >= 0) & (uv[:, :, 0] < width) & (uv[:, :, 1] >= 0) & (uv[:, :, 1] < height)
    report["projection"] = {"all_points_in_front": bool(np.all(front)),
        "points_inside_canvas_histogram": {str(k): int(v) for k, v in Counter(inside.sum(axis=1)).items()},
        "outside_canvas_frame_count": int(np.count_nonzero(~inside.all(axis=1))),
        "outside_canvas_first_20_frames": np.flatnonzero(~inside.all(axis=1))[:20].tolist(),
        "caveat": "inside canvas does not establish visibility, anatomical accuracy or content synchronization"}
    output.mkdir(parents=True, exist_ok=True)
    selected = {0, n//2, n-1}
    pts, keys, padding_samples = [], [], {}
    with av.open(str(root / video_key)) as container:
        stream = container.streams.video[0]
        report["video"] = {"codec": stream.codec_context.name,
            "width": stream.width, "height": stream.height, "pixel_format": stream.pix_fmt,
            "header_frames": stream.frames, "time_base": str(stream.time_base),
            "average_rate": str(stream.average_rate),
            "container_duration_s": container.duration / 1e6 if container.duration else None,
            "stream_duration_s": float(stream.duration * stream.time_base) if stream.duration else None}
        for i, frame in enumerate(container.decode(video=0)):
            if i >= 10000:
                raise ValueError("Decode limit exceeded (10,000)")
            pts.append(float(frame.pts * stream.time_base))
            if frame.key_frame:
                keys.append(i)
            if i in selected:
                img = frame.to_image().convert("RGB")
                rgb = np.asarray(img)
                nonblack_rows = np.flatnonzero(rgb.max(axis=(1, 2)) > 8)
                padding_samples[str(i)] = [int(nonblack_rows[0]), int(nonblack_rows[-1])+1]
                draw = ImageDraw.Draw(img)
                for j, (x, y) in enumerate(uv[i]):
                    if not inside[i, j]:
                        continue
                    color = (40, 255, 100) if j < 21 else (255, 100, 50)
                    draw.ellipse((x-3, y-3, x+3, y+3), fill=color, outline="white")
                draw.text((8, 8), f"frame {i} | {ns[i]/1e9:.6f}s | L green / R orange", fill="white")
                img.save(output / f"overlay-{i:04d}.png")
    video_pts = np.asarray(pts)
    video = report["video"]
    video.update(decoded_frames=len(pts), pts_increasing=bool(np.all(np.diff(video_pts) > 0)),
        pts_step_s=distribution(np.diff(video_pts)),
        keyframe_interval_histogram={str(k): int(v) for k, v in Counter(np.diff(keys)).items()},
        frame_count_matches_rows=len(pts) == n, sample_nonblack_row_intervals=padding_samples)
    if len(pts) == n:
        video["all_frames_max_abs_pts_vs_ns_s"] = float(np.max(np.abs(video_pts - ns / 1e9)))
        video["all_frames_max_abs_pts_vs_float32_s"] = float(np.max(np.abs(video_pts - ts)))
        # The three image samples support this padding interval, not a per-frame segmentation.
        intervals = list(padding_samples.values())
        if all(p == intervals[0] for p in intervals):
            lo, hi = intervals[0]
            in_padding = inside & ((uv[:, :, 1] < lo) | (uv[:, :, 1] >= hi))
            report["projection"].update(sample_supported_content_rows=[lo, hi],
                frames_with_point_in_padding=int(np.count_nonzero(in_padding.any(axis=1))),
                projected_points_in_padding=int(np.count_nonzero(in_padding)))
    (output / "metrics.json").write_text(json.dumps(report, ensure_ascii=False, indent=2, allow_nan=False), encoding="utf-8")
    print(json.dumps({"rows": n, "decoded_frames": len(pts), "report": str(output / "metrics.json"),
                      "overlays": len(selected), "raw_arrays_returned": False}, ensure_ascii=False))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("data_root", type=Path)
    parser.add_argument("--output-dir", required=True, type=Path)
    args = parser.parse_args()
    audit(args.data_root, args.output_dir)
