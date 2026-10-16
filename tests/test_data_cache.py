import hashlib
import io
import os
import threading
from concurrent.futures import ThreadPoolExecutor

import av
import numpy as np
import pytest

from app.data.adapter import DatasetAdapter
from app.data.errors import DataContractError
from app.data.source import LocalObjectStore, S3ObjectStore


KEY = "videos/camera/chunk-000/episode.mp4"


def write_video(path, color):
    path.parent.mkdir(parents=True, exist_ok=True)
    with av.open(str(path), "w") as container:
        stream = container.add_stream("libx264", rate=10)
        stream.width, stream.height, stream.pix_fmt = 64, 48, "yuv420p"
        for _ in range(4):
            pixels = np.full((48, 64, 3), color, dtype=np.uint8)
            for packet in stream.encode(av.VideoFrame.from_ndarray(pixels, format="rgb24")):
                container.mux(packet)
        for packet in stream.encode():
            container.mux(packet)


def rgb_hash(path):
    with av.open(str(path)) as container:
        frames = list(container.decode(video=0))
        assert len(frames) == 4 and frames[0].width == 64 and frames[0].height == 48
        return hashlib.sha256(frames[0].to_ndarray(format="rgb24").tobytes()).hexdigest()


def test_video_cache_isolated_between_sources_with_identical_object_paths(tmp_path):
    roots = [tmp_path / name for name in ("source-a", "source-b")]
    write_video(roots[0] / KEY, (230, 25, 25))
    write_video(roots[1] / KEY, (25, 25, 230))
    cache = tmp_path / "shared-cache"
    paths = [DatasetAdapter(LocalObjectStore(root), cache).materialize(KEY) for root in roots]
    expected = [rgb_hash(root / KEY) for root in roots]
    assert expected[0] != expected[1]
    assert [rgb_hash(path) for path in paths] == expected
    assert paths[0] != paths[1]


def test_new_adapter_detects_same_size_content_change_even_when_mtime_is_preserved(tmp_path):
    root, cache = tmp_path / "source", tmp_path / "cache"
    path = root / KEY
    path.parent.mkdir(parents=True)
    path.write_bytes(b"AAAA")
    before = path.stat()
    a = DatasetAdapter(LocalObjectStore(root), cache).materialize(KEY)
    path.write_bytes(b"BBBB")
    os.utime(path, ns=(before.st_atime_ns, before.st_mtime_ns))
    b = DatasetAdapter(LocalObjectStore(root), cache).materialize(KEY)
    assert a != b and a.read_bytes() == b"AAAA" and b.read_bytes() == b"BBBB"


def test_full_object_paths_do_not_have_flat_filename_collisions(tmp_path):
    root, cache = tmp_path / "source", tmp_path / "cache"
    keys = ["a/b__c.mp4", "a__b/c.mp4"]
    for key, value in zip(keys, (b"one", b"two")):
        path = root / key; path.parent.mkdir(parents=True, exist_ok=True); path.write_bytes(value)
    adapter = DatasetAdapter(LocalObjectStore(root), cache)
    paths = [adapter.materialize(key) for key in keys]
    assert paths[0] != paths[1] and [p.read_bytes() for p in paths] == [b"one", b"two"]


@pytest.mark.parametrize("damage", ["truncated", "same_size", "missing_record", "invalid_record"])
def test_corrupt_cache_is_not_trusted_and_legacy_cache_is_ignored(tmp_path, damage):
    root, cache = tmp_path / "source", tmp_path / "cache"
    path = root / KEY; path.parent.mkdir(parents=True); path.write_bytes(b"correct video")
    cache.mkdir()
    legacy = cache / KEY.replace("/", "__"); legacy.write_bytes(b"wrong legacy")
    adapter = DatasetAdapter(LocalObjectStore(root), cache)
    target = adapter.materialize(KEY)
    assert target != legacy
    if damage == "truncated": target.write_bytes(b"cor")
    elif damage == "same_size": target.write_bytes(b"x" * len(path.read_bytes()))
    elif damage == "missing_record": target.with_suffix(".json").unlink()
    else: target.with_suffix(".json").write_text("not JSON")
    # The same adapter also notices changed cache file fingerprints.
    assert adapter.materialize(KEY).read_bytes() == path.read_bytes()
    assert DatasetAdapter(LocalObjectStore(root), cache).materialize(KEY).read_bytes() == path.read_bytes()
    assert legacy.read_bytes() == b"wrong legacy"


def test_verified_adapter_does_not_hash_or_download_again(tmp_path, monkeypatch):
    root, cache = tmp_path / "source", tmp_path / "cache"
    path = root / KEY; path.parent.mkdir(parents=True); path.write_bytes(b"pinned video")
    adapter = DatasetAdapter(LocalObjectStore(root), cache)
    target = adapter.materialize(KEY)
    def unexpected(*_args, **_kwargs): pytest.fail("verified video must not be rehashed/downloaded per frame")
    monkeypatch.setattr(LocalObjectStore, "head", unexpected)
    monkeypatch.setattr(LocalObjectStore, "download", unexpected)
    assert adapter.materialize(KEY) == target


def test_interrupted_download_does_not_publish_or_fall_back(tmp_path, monkeypatch):
    root, cache = tmp_path / "source", tmp_path / "cache"
    path = root / KEY; path.parent.mkdir(parents=True); path.write_bytes(b"AAAA")
    previous = DatasetAdapter(LocalObjectStore(root), cache).materialize(KEY)
    path.write_bytes(b"BBBB")
    original = LocalObjectStore.download
    def interrupted(_store, _key, output, _expected):
        output.write(b"BB")
        raise RuntimeError("private download failure")
    monkeypatch.setattr(LocalObjectStore, "download", interrupted)
    adapter = DatasetAdapter(LocalObjectStore(root), cache)
    with pytest.raises(DataContractError, match="Cannot materialize verified video"):
        adapter.materialize(KEY)
    assert previous.read_bytes() == b"AAAA" and path.read_bytes() == b"BBBB"
    assert len(list(cache.rglob("*.mp4"))) == 1 and not list(cache.rglob("*.part"))
    monkeypatch.setattr(LocalObjectStore, "download", original)
    assert adapter.materialize(KEY).read_bytes() == b"BBBB"


def test_local_change_during_download_is_rejected(tmp_path, monkeypatch):
    root, cache = tmp_path / "source", tmp_path / "cache"
    path = root / KEY; path.parent.mkdir(parents=True); path.write_bytes(b"AAAA")
    original = LocalObjectStore.download
    def changed(store, key, output, expected):
        path.write_bytes(b"BBBB")
        return original(store, key, output, expected)
    monkeypatch.setattr(LocalObjectStore, "download", changed)
    with pytest.raises(DataContractError): DatasetAdapter(LocalObjectStore(root), cache).materialize(KEY)
    assert not list(cache.rglob("*.mp4")) and not list(cache.rglob("*.part"))


def test_concurrent_adapters_never_publish_partial_files(tmp_path, monkeypatch):
    root, cache = tmp_path / "source", tmp_path / "cache"
    path = root / KEY; path.parent.mkdir(parents=True); path.write_bytes(b"same complete video")
    original = LocalObjectStore.download
    entered, release = threading.Event(), threading.Event()
    def blocked(store, key, output, expected):
        entered.set(); assert release.wait(5)
        return original(store, key, output, expected)
    monkeypatch.setattr(LocalObjectStore, "download", blocked)
    adapters = [DatasetAdapter(LocalObjectStore(root), cache) for _ in range(4)]
    with ThreadPoolExecutor(max_workers=4) as pool:
        futures = [pool.submit(adapter.materialize, KEY) for adapter in adapters]
        try:
            assert entered.wait(5)
            assert not list(cache.rglob("*.mp4")) and not list(cache.rglob("*.json"))
        finally: release.set()
        paths = [future.result() for future in futures]
    assert len(set(paths)) == 1
    assert all(p.read_bytes() == path.read_bytes() for p in paths)
    assert not list(cache.rglob("*.part"))


class FakeS3:
    def __init__(self, data=b"AAAA", version="v1", etag='"etag-1"'):
        self.data, self.version, self.etag = data, version, etag
        self.calls = []
        self.reject_get = False

    def head_object(self, **args):
        self.calls.append(("head", args))
        result = {"ContentLength": len(self.data)}
        if self.version is not None: result["VersionId"] = self.version
        if self.etag is not None: result["ETag"] = self.etag
        return result

    def get_object(self, **args):
        self.calls.append(("get", args))
        if self.reject_get: raise RuntimeError("private conditional failure")
        if "VersionId" in args and args["VersionId"] != self.version: raise RuntimeError("NoSuchVersion")
        if "IfMatch" in args and args["IfMatch"] != self.etag: raise RuntimeError("PreconditionFailed")
        result = {"Body": io.BytesIO(self.data)}
        if self.version is not None: result["VersionId"] = self.version
        if self.etag is not None: result["ETag"] = self.etag
        return result


def s3_store(monkeypatch, client, bucket="test-bucket", endpoint="https://objects.example", region="auto"):
    import boto3
    monkeypatch.setattr(boto3, "client", lambda *_args, **_kwargs: client)
    return S3ObjectStore(bucket, endpoint, region)


@pytest.mark.parametrize("difference", ["endpoint", "bucket", "region"])
def test_s3_sources_are_isolated(tmp_path, monkeypatch, difference):
    a, b = FakeS3(), FakeS3(b"BBBB")
    first = s3_store(monkeypatch, a)
    second = s3_store(monkeypatch, b, **{difference: {"endpoint": "https://another.example", "bucket": "another-bucket", "region": "another-region"}[difference]})
    x = DatasetAdapter(first, tmp_path).materialize(KEY)
    y = DatasetAdapter(second, tmp_path).materialize(KEY)
    assert x != y and x.read_bytes() == b"AAAA" and y.read_bytes() == b"BBBB"


@pytest.mark.parametrize("versioned", [True, False])
def test_s3_object_version_changes_and_stable_versions_reuse_cache(tmp_path, monkeypatch, versioned):
    client = FakeS3(version="v1" if versioned else None)
    store = s3_store(monkeypatch, client)
    a = DatasetAdapter(store, tmp_path).materialize(KEY)
    assert DatasetAdapter(store, tmp_path).materialize(KEY) == a
    assert len([call for call in client.calls if call[0] == "get"]) == 1
    get = next(args for method, args in client.calls if method == "get")
    assert get.get("VersionId") == ("v1" if versioned else None)
    assert get.get("IfMatch") == (None if versioned else '"etag-1"')
    client.data, client.etag = b"BBBB", '"etag-2"'
    if versioned: client.version = "v2"
    b = DatasetAdapter(store, tmp_path).materialize(KEY)
    assert a != b and a.read_bytes() == b"AAAA" and b.read_bytes() == b"BBBB"
    assert {name for name, _ in client.calls} == {"head", "get"}


@pytest.mark.parametrize("version", [None, "null"])
def test_unversioned_s3_does_not_share_cache_between_adapters(tmp_path, monkeypatch, version):
    client = FakeS3(version=version, etag=None)
    store = s3_store(monkeypatch, client)
    a = DatasetAdapter(store, tmp_path).materialize(KEY)
    client.data = b"BBBB"
    b = DatasetAdapter(store, tmp_path).materialize(KEY)
    assert a != b and b.read_bytes() == b"BBBB"


def test_s3_conditional_failure_does_not_return_old_video(tmp_path, monkeypatch):
    client = FakeS3(version=None)
    store = s3_store(monkeypatch, client)
    old = DatasetAdapter(store, tmp_path).materialize(KEY)
    client.etag, client.data, client.reject_get = '"etag-2"', b"BBBB", True
    with pytest.raises(DataContractError): DatasetAdapter(store, tmp_path).materialize(KEY)
    assert old.read_bytes() == b"AAAA"
    gets = [args for method, args in client.calls if method == "get"]
    assert gets[-1]["IfMatch"] == '"etag-2"' and not list(tmp_path.rglob("*.part"))


def test_cache_records_and_errors_do_not_contain_source_secrets(tmp_path, monkeypatch):
    client = FakeS3()
    # Client construction is mocked: no credentials or network are consulted.
    store = s3_store(monkeypatch, client, endpoint="https://user:private-value@objects.example?token=private-value")
    adapter = DatasetAdapter(store, tmp_path)
    target = adapter.materialize(KEY)
    records = "".join(path.read_text() for path in tmp_path.rglob("*.json"))
    assert "private-value" not in str(target) + records and "objects.example" not in records
    client.data, client.version, client.reject_get = b"BBBB", "v2", True
    with pytest.raises(DataContractError) as raised: DatasetAdapter(store, tmp_path).materialize(KEY)
    assert "private-value" not in str(raised.value)


@pytest.mark.parametrize("fault", ["version", "etag", "truncated", "oversized"])
def test_s3_inconsistent_download_is_rejected_and_body_closed(tmp_path, monkeypatch, fault):
    client = FakeS3()
    original = client.get_object
    bodies = []
    def inconsistent(**args):
        result = original(**args)
        if fault in ("version", "etag"):
            result["VersionId" if fault == "version" else "ETag"] = "different"
        else:
            result["Body"].close()
            result["Body"] = io.BytesIO(b"AAA" if fault == "truncated" else b"AAAAA")
        bodies.append(result["Body"])
        return result
    monkeypatch.setattr(client, "get_object", inconsistent)
    with pytest.raises(DataContractError):
        DatasetAdapter(s3_store(monkeypatch, client), tmp_path).materialize(KEY)
    assert bodies and all(body.closed for body in bodies)
    assert not list(tmp_path.rglob("*.mp4")) and not list(tmp_path.rglob("*.part"))
