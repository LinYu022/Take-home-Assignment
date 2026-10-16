from __future__ import annotations

import io
import json
import csv
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import numpy as np
import pyarrow.parquet as pq

from .errors import DataContractError, DataNotFound
from .source import ObjectStore
from .cache import MaterializedCache


@dataclass(frozen=True)
class FieldInfo:
    name: str
    kind: str
    declared_dtype: str | None
    declared_shape: list[int] | None
    observed_length: int | None
    units: str | None = None
    description: str | None = None
    frame: str | None = None
    units_source: str | None = None
    storage_dtype: str | None = None
    timestamp_prefix: dict | None = None
    component_layout: list | None = None
    observed_length_scope: dict | None = None


@dataclass(frozen=True)
class EpisodeInfo:
    episode_index: int
    length: int
    tasks: list[str]
    start_ns: int
    end_ns: int
    video_start_ns: int
    video_end_ns: int
    frame_period_ns: int
    success: bool | None = None
    success_source: str | None = None
    success_status: str = "missing"

    @property
    def data_start_ns(self) -> int:
        return self.start_ns

    @property
    def data_end_ns(self) -> int:
        return self.end_ns

    def as_dict(self) -> dict[str, Any]:
        return {
            "episode_index": self.episode_index,
            "length": self.length,
            "tasks": list(self.tasks),
            "data_start_ns": self.data_start_ns,
            "data_end_ns": self.data_end_ns,
            "video_start_ns": self.video_start_ns,
            "video_end_ns": self.video_end_ns,
            "frame_period_ns": self.frame_period_ns,
            "frame_period_method": "median adjacent timestamp_ns differences; not a constant interval guarantee",
            "success": self.success, "success_source": self.success_source, "success_status": self.success_status,
            "time_semantics": {
                "data_domain": "[data_start_ns,data_end_ns)",
                "video_domain": "[video_start_ns,video_end_ns)",
            },
        }


class DatasetAdapter:
    """Thin, format-aware adapter for the assignment's LeRobot v3 snapshot."""

    def __init__(self, store: ObjectStore, cache_dir: Path):
        self.store = store
        self.cache_dir = cache_dir
        self._info: dict[str, Any] | None = None
        self._episodes: list[EpisodeInfo] | None = None
        self._tasks: list[dict[str, Any]] | None = None
        self._episode_rows: list[dict[str, Any]] | None = None
        self._tables: dict[int, Any] = {}
        self._video_paths: dict[tuple[int, str], str] = {}
        self._materialized = MaterializedCache(store, cache_dir)

    def _json(self, key: str) -> dict[str, Any]:
        try:
            return json.loads(self.store.get(key).decode("utf-8"))
        except Exception as exc:
            raise DataContractError(f"Cannot read metadata object {key}") from exc

    @property
    def info(self) -> dict[str, Any]:
        if self._info is None:
            self._info = self._json("meta/info.json")
        return self._info

    @property
    def tasks(self) -> list[dict[str, Any]]:
        if self._tasks is None:
            try:
                table = pq.read_table(io.BytesIO(self.store.get("meta/tasks.parquet")))
                self._tasks = table.to_pylist()
            except Exception as exc:
                raise DataContractError("Cannot read meta/tasks.parquet") from exc
        return self._tasks

    @property
    def episodes(self) -> list[EpisodeInfo]:
        if self._episodes is not None:
            return self._episodes
        try:
            rows = pq.read_table(io.BytesIO(self.store.get("meta/episodes.parquet"))).to_pylist()
        except Exception as exc:
            raise DataContractError("Cannot read meta/episodes.parquet") from exc
        result: list[EpisodeInfo] = []
        for row in rows:
            index = int(row["episode_index"])
            table = self._load_table(index, row)
            ts = np.asarray(table["timestamp_ns"].to_pylist(), dtype=np.int64)
            if len(ts) != int(row["length"]) or len(ts) == 0:
                raise DataContractError(f"Episode {index} length does not match data")
            if np.any(np.diff(ts) <= 0):
                raise DataContractError(f"Episode {index} timestamp_ns is not strictly increasing")
            task_names = [str(x) for x in row.get("tasks", [])]
            periods = np.diff(ts)
            period = int(np.median(periods)) if len(periods) else 1
            video_prefix = "videos/base_0_camera/rgb/image/"
            video_start = int(round(float(row.get(video_prefix + "from_timestamp", ts[0] / 1e9)) * 1e9))
            video_end = int(round(float(row.get(video_prefix + "to_timestamp", (ts[-1] + period) / 1e9)) * 1e9))
            success, source, status = self._success(index)
            result.append(EpisodeInfo(index, len(ts), task_names, int(ts[0]), int(ts[-1]) + period, video_start, video_end, period, success, source, status))
        self._episodes = result
        self._episode_rows = rows
        return result

    def _success(self, index):
        key = "meta/custom_metadata.csv"
        try:
            size=int(self.store.head(key)["size_bytes"])
            if size>1024*1024: return None,None,"metadata_limit"
            raw=self.store.get_range(key,0,size-1) if size else b""
        except DataNotFound:
            return None, None, "missing"
        except Exception:
            return None, None, "unavailable"
        if len(raw) > 1024 * 1024: return None, None, "metadata_limit"
        try:
            rows = [r for r in csv.DictReader(io.StringIO(raw.decode("utf-8-sig"))) if r.get("episode_index") == str(index)]
            if len(rows) != 1: return None, None, "missing" if not rows else "ambiguous"
            value = rows[0].get("success", "").lower()
            if value not in {"true", "false"}: return None, None, "invalid"
            return value == "true", f"{key}:episode_index={index}:success", "metadata_declaration"
        except (ValueError, UnicodeError):
            return None, None, "invalid"

    def _data_key(self, row: dict[str, Any]) -> str:
        template = self.info.get("data_path")
        if not template:
            raise DataContractError("info.json has no data_path")
        try:
            return template.format(
                chunk_index=row["data/chunk_index"], file_index=row["data/file_index"]
            )
        except KeyError as exc:
            raise DataContractError("Episode metadata lacks data path indices") from exc

    def _load_table(self, episode: int, row: dict[str, Any] | None = None):
        if episode in self._tables:
            return self._tables[episode]
        row = row or next((x for x in (self._episode_rows or pq.read_table(io.BytesIO(self.store.get("meta/episodes.parquet"))).to_pylist()) if int(x["episode_index"]) == episode), None)
        if row is None:
            raise DataNotFound(f"Episode {episode} not found")
        key = self._data_key(row)
        try:
            table = pq.read_table(io.BytesIO(self.store.get(key)))
        except Exception as exc:
            raise DataContractError(f"Cannot read episode data for episode {episode}") from exc
        self._validate_table(table, episode)
        self._tables[episode] = table
        return table

    def _validate_table(self, table, episode: int) -> None:
        if "timestamp_ns" not in table.column_names or "frame_index" not in table.column_names:
            raise DataContractError(f"Episode {episode} lacks required time fields")
        timestamps = np.asarray(table["timestamp_ns"].to_pylist(), dtype=np.int64)
        frames = np.asarray(table["frame_index"].to_pylist(), dtype=np.int64)
        if len(timestamps) == 0 or np.any(np.diff(timestamps) <= 0):
            raise DataContractError(f"Episode {episode} has invalid timestamps")
        if not np.array_equal(frames, np.arange(len(frames))):
            raise DataContractError(f"Episode {episode} frame_index is not contiguous")
        for name in table.column_names:
            values = table[name].to_pylist()
            for value in values:
                arr = np.asarray(value if isinstance(value, list) else [value])
                if not np.all(np.isfinite(arr.astype(np.float64))):
                    raise DataContractError(f"Episode {episode} field {name} contains non-finite values")

    def fields(self) -> list[FieldInfo]:
        self.episodes  # validate the data before advertising it
        features = self.info.get("features", {})
        table = self._tables[self.episodes[0].episode_index]
        fields: list[FieldInfo] = []
        for name in table.column_names:
            feature = features.get(name, {})
            if not isinstance(feature, dict): feature = {}
            units, units_source = None, None
            for key in ("unit", "units"):
                if isinstance(feature.get(key), str) and feature[key].strip():
                    units, units_source = feature[key], "metadata." + key
                    break
            description = feature.get("description")
            # A mixed MANO description may mention translation meters without
            # giving a single unit to all 61 components. Recognize only the
            # explicitly documented timestamp-plus-XYZ layout here.
            if (units is None and isinstance(description, str)
                    and description.lstrip().startswith("[ts_ns,x,y,z]") and "(m)" in description):
                units, units_source = "m", "description"
            observed = None
            arrow_type = str(table[name].type)
            if arrow_type.startswith("list"):
                first = table[name][0].as_py() if len(table[name]) else []
                observed = len(first)
            prefixed = name in {"base_0_camera/position", "base_0_camera/quaternion_xyzw", "left_hand/tracks", "right_hand/tracks"} and isinstance(description, str) and "ts_ns" in description
            layout = None
            if prefixed:
                layout = [{"start": 0, "end": 1, "meaning": "timestamp", "unit": "ns", "source": "metadata.description"},
                          {"start": 1, "end": observed, "meaning": "payload", "unit": units, "source": "metadata.description"}]
            elif "mano" in name.lower() and isinstance(description, str) and all(x in description for x in ("45", "10")):
                layout = [dict(start=a, end=b, meaning=m, unit=None, source="metadata.description") for a,b,m in
                          ((0,3,"global_orient"),(3,48,"hand_pose"),(48,58,"betas"),(58,61,"translation"))]
            fields.append(FieldInfo(
                name=name,
                kind="scalar" if observed is None else "vector",
                declared_dtype=feature.get("dtype"),
                declared_shape=feature.get("shape"),
                observed_length=observed,
                units=units,
                description=description,
                frame=feature.get("frame"),
                units_source=units_source,
                storage_dtype=arrow_type,
                timestamp_prefix={"present": prefixed, "source": "metadata.description"} if isinstance(description, str) else {"present": None, "source": None},
                component_layout=layout,
                observed_length_scope={"kind": "sampled", "episode": self.episodes[0].episode_index, "row_offset": 0, "rows_checked": 1},
            ))
        return fields

    def manifest(self) -> dict[str, Any]:
        eps = self.episodes
        return {
            "codebase_version": self.info.get("codebase_version"),
            "robot_type": self.info.get("robot_type"),
            "fps": self.info.get("fps"),
            "total_episodes": len(eps),
            "total_frames": sum(e.length for e in eps),
            "total_tasks": len(self.tasks),
            "total_videos": int(self.info.get("total_videos", 0)),
            "episodes": [e.as_dict() for e in eps],
            "tasks": self.tasks,
            "fields": [f.__dict__ for f in self.fields()],
        }

    def table(self, episode: int):
        return self._load_table(episode)

    def row(self, episode: int, frame: int) -> dict[str, Any]:
        table = self.table(episode)
        if frame < 0 or frame >= table.num_rows:
            raise DataNotFound(f"Frame {frame} is outside episode {episode}")
        return table.slice(frame, 1).to_pylist()[0]

    def frame_for_timestamp(self, episode: int, timestamp_ns: int, strict: bool = True) -> int:
        ts = np.asarray(self.table(episode)["timestamp_ns"].to_pylist(), dtype=np.int64)
        info = next((x for x in self.episodes if x.episode_index == episode), None)
        if info is None:
            raise DataNotFound(f"Episode {episode} not found")
        if strict and (timestamp_ns < info.data_start_ns or timestamp_ns >= info.data_end_ns):
            raise DataNotFound("timestamp is outside the data time domain")
        index = int(np.searchsorted(ts, timestamp_ns, side="left"))
        return min(index, len(ts) - 1)

    def video_key(self, episode: int, camera: str = "base_0_camera/rgb/image") -> str:
        rows = pq.read_table(io.BytesIO(self.store.get("meta/episodes.parquet"))).to_pylist()
        row = next((x for x in rows if int(x["episode_index"]) == episode), None)
        if row is None:
            raise DataNotFound(f"Episode {episode} not found")
        template = self.info.get("video_path")
        if not template:
            raise DataContractError("info.json has no video_path")
        try:
            key = template.format(
                video_key=camera,
                chunk_index=row[f"videos/{camera}/chunk_index"],
                file_index=row[f"videos/{camera}/file_index"],
            )
        except KeyError as exc:
            raise DataContractError(f"Episode has no video metadata for {camera}") from exc
        self._video_paths[(episode, camera)] = key
        return key

    def materialize(self, key: str) -> Path:
        return self._materialized.materialize(key)

