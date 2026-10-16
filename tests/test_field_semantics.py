import json
from dataclasses import replace

import pyarrow as pa
import pyarrow.parquet as pq
import pytest
from fastapi.testclient import TestClient
from PIL import Image

from app.config import Settings
from app.data.adapter import DatasetAdapter
from app.data.source import LocalObjectStore
from app.main import create_app
from app.renderer import compose_view
from app.viewstate import normalize_view, validation_context


MANO_DESCRIPTION = "left-hand MANO: global_orient_axis_angle(3) + hand_pose_axis_angle(45) + betas(10) + transl(3)"


@pytest.fixture
def semantic_dataset(tmp_path):
    root = tmp_path / "source"
    (root / "meta").mkdir(parents=True)
    (root / "data").mkdir()
    features = {
        "base_0_camera/intrinsics": [[48, 0, 32], [0, 48, 24], [0, 0, 1]],
        "base_0_camera/resolution": [64, 48],
        "base_0_camera/position": {"dtype": "float32", "shape": [4], "description": "[ts_ns,x,y,z] head-camera position (m)", "frame": "world"},
        "base_0_camera/quaternion_xyzw": {"dtype": "float32", "shape": [5], "description": "[ts_ns,x,y,z,w] head-camera orientation", "frame": "world"},
        "observation.left_hand_mano": {"dtype": "float32", "shape": [61], "description": MANO_DESCRIPTION, "frame": "camera"},
        "explicit_unit": {"unit": "cm", "units": "mm", "description": "explicit unit wins over (m)"},
        "plural_units": {"units": "mm"},
    }
    info = dict(codebase_version="v3.0", fps=10, total_videos=0,
                data_path="data/{file_index}.parquet", features=features)
    (root / "meta/info.json").write_text(json.dumps(info), encoding="utf-8")
    points = [value for _ in range(21) for value in (0., 0., 1.)]
    table = pa.Table.from_pylist([{
        "timestamp_ns": i * 100_000_000, "frame_index": i,
        "base_0_camera/position": [i * 100_000_000, 1., 2., 3.],
        "base_0_camera/quaternion_xyzw": [i * 100_000_000, 0., 0., 0., 1.],
        "action.left_hand_tracks": points, "action.right_hand_tracks": points,
        "left_hand/tracks": [i * 100_000_000, *points],
        "right_hand/tracks": [i * 100_000_000, *points],
        "observation.left_hand_mano": list(map(float, range(61))),
        "explicit_unit": 1., "plural_units": 1., "unknown": 1.,
    } for i in range(4)])
    pq.write_table(table, root / "data/0.parquet")
    pq.write_table(pa.Table.from_pylist([dict(episode_index=0, length=4, tasks=["semantic fixture"],
                                             **{"data/chunk_index": 0, "data/file_index": 0})]), root / "meta/episodes.parquet")
    pq.write_table(pa.table({"task_index": [0], "task": ["semantic fixture"]}), root / "meta/tasks.parquet")
    settings = Settings(data_root=root, s3_bucket=None, s3_endpoint=None, s3_region="auto",
                        public_base_url="http://127.0.0.1:8000", cache_dir=tmp_path / "cache")
    return root, settings, features


def assert_semantics(fields, features):
    catalog = {field["name"]: field for field in fields}
    for name in ("base_0_camera/position", "base_0_camera/quaternion_xyzw", "observation.left_hand_mano"):
        assert catalog[name].get("description") == features[name]["description"]
        assert catalog[name].get("frame") == features[name]["frame"]
    assert catalog["base_0_camera/position"]["units"] == "m"
    assert catalog["observation.left_hand_mano"]["observed_length"] == 61


def test_catalog_preserves_original_description_frame_and_units(semantic_dataset):
    root, settings, features = semantic_dataset
    adapter = DatasetAdapter(LocalObjectStore(root), settings.cache_dir)
    assert_semantics(adapter.manifest()["fields"], features)


def test_unit_provenance_and_missing_metadata(semantic_dataset):
    root, settings, _ = semantic_dataset
    fields = {f.name: f for f in DatasetAdapter(LocalObjectStore(root), settings.cache_dir).fields()}
    assert (fields["base_0_camera/position"].units, fields["base_0_camera/position"].units_source) == ("m", "description")
    assert (fields["explicit_unit"].units, fields["explicit_unit"].units_source) == ("cm", "metadata.unit")
    assert (fields["plural_units"].units, fields["plural_units"].units_source) == ("mm", "metadata.units")
    assert fields["unknown"].description is fields["unknown"].frame is fields["unknown"].units is None
    assert fields["observation.left_hand_mano"].units is None


def test_manifest_and_mcp_preserve_semantics_and_raw_mano_indices(semantic_dataset):
    _root, settings, features = semantic_dataset
    with TestClient(create_app(settings)) as client:
        assert_semantics(client.get("/api/manifest").json()["fields"], features)
        response = client.post("/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                               "params": {"name": "describe_dataset", "arguments": {}}}).json()
        assert_semantics(response["result"]["structuredContent"]["manifest"]["fields"], features)
        for index in (0, 2, 3, 47, 48, 57, 58, 60):
            result = client.post("/mcp", json={"jsonrpc": "2.0", "id": index + 2, "method": "tools/call",
                                 "params": {"name": "query_series", "arguments": {
                                     "episode": 0, "field": "observation.left_hand_mano", "component": index,
                                     "start_frame": 0, "end_frame": 4, "limit": 4}}}).json()
            assert result["result"]["structuredContent"]["values"] == [float(index)] * 4


@pytest.mark.parametrize("selector,unit", [
    ("base_0_camera/position", "m"), ("base_0_camera/position[1]", "m"),
    ("base_0_camera/position[0]", "ns"), ("base_0_camera/quaternion_xyzw[0]", "ns"),
    ("observation.left_hand_mano", "Unknown unit"), ("observation.left_hand_mano[0]", "Unknown unit"),
])
def test_rendered_units_do_not_label_timestamps_or_mano_as_meters(semantic_dataset, selector, unit):
    root, settings, _ = semantic_dataset
    adapter = DatasetAdapter(LocalObjectStore(root), settings.cache_dir)
    state = normalize_view({"series": [selector], "overlays": []}, **validation_context(adapter, settings))
    _, manifest = compose_view(adapter, state, Image.new("RGB", (64, 48)))
    assert manifest["series"][0]["unit"] == unit


def test_translation_unit_in_mixed_mano_description_is_not_a_whole_vector_unit(semantic_dataset):
    root, settings, _ = semantic_dataset
    path = root / "meta/info.json"
    info = json.loads(path.read_text())
    info["features"]["observation.left_hand_mano"]["description"] += "; transl in (m)"
    path.write_text(json.dumps(info))
    field = next(f for f in DatasetAdapter(LocalObjectStore(root), settings.cache_dir).fields()
                 if f.name == "observation.left_hand_mano")
    assert field.units is None and field.units_source is None


def test_added_semantics_still_obey_complete_tool_response_limit(semantic_dataset):
    _root, settings, _ = semantic_dataset
    with TestClient(create_app(replace(settings, max_query_bytes=768))) as client:
        result = client.post("/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                             "params": {"name": "describe_dataset", "arguments": {}}}).json()
        assert result["result"]["isError"] is True
        assert len(result["result"]["content"][0]["text"].encode()) <= 768
        error=json.loads(result["result"]["content"][0]["text"])["error"]
        assert error["code"] == "QueryLimitError"
        assert error["constraint"] == "complete tool result exceeds 768 bytes"
