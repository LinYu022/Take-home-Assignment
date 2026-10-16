"""Versioned, verified video files; no source paths or credentials on disk."""
from __future__ import annotations

import json
import os
import secrets
import tempfile
import threading
import time
from contextlib import contextmanager
from pathlib import Path

from .errors import DataContractError
from .source import copy_stream, file_signature, identity_digest


@contextmanager
def publication_lock(path):
    """OS-owned lock: safe across adapters/processes, released on process exit."""
    if path.is_symlink(): raise DataContractError("Cache lock must not be a link")
    with path.open("a+b") as handle:
        deadline = time.monotonic() + 30
        while True:
            try:
                if os.name == "nt":
                    import msvcrt
                    handle.seek(0); msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except OSError:
                if time.monotonic() >= deadline:
                    raise DataContractError("Cache publication is busy; retry the request") from None
                time.sleep(.01)
        try:
            # Lock before touching byte zero: Windows byte locks also prevent
            # competing handles from reading it. Locks may extend beyond EOF.
            if os.fstat(handle.fileno()).st_size == 0:
                handle.write(b"\0"); handle.flush()
            yield
        finally:
            if os.name == "nt":
                handle.seek(0); msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


class MaterializedCache:
    def __init__(self, store, directory: Path):
        self.store, self.directory = store, directory
        self.lock = threading.RLock()
        self.snapshots = {}
        self.verified = {}

    def _snapshot(self, key):
        if key not in self.snapshots:
            metadata = dict(self.store.head(key))
            if type(metadata.get("size_bytes")) is not int or metadata["size_bytes"] < 0:
                raise DataContractError("Object size is unavailable")
            # An unversioned store cannot safely share files between adapters.
            if not any(metadata.get(k) for k in ("sha256", "version_id", "etag")):
                metadata["adapter_nonce"] = secrets.token_hex(32)
            self.snapshots[key] = metadata
        return self.snapshots[key]

    @staticmethod
    def _signatures(target, record):
        if target.is_symlink() or record.is_symlink(): return None
        if not target.is_file() or not record.is_file(): return None
        return file_signature(target), file_signature(record)

    def _validate(self, target, record, expected, metadata):
        try:
            signature = self._signatures(target, record)
            if signature is None or record.stat().st_size > 4096: return None
            saved = json.loads(record.read_text(encoding="utf-8"))
            if set(saved) != set(expected) | {"sha256"}: return None
            if any(saved.get(k) != v for k, v in expected.items()): return None
            if metadata.get("sha256") and saved["sha256"] != metadata["sha256"]: return None
            with target.open("rb") as body:
                actual = copy_stream(body, expected_size=metadata["size_bytes"])
            if actual["sha256"] != saved["sha256"]: return None
            return signature if self._signatures(target, record) == signature else None
        except (OSError, ValueError, TypeError, DataContractError):
            return None

    def materialize(self, key: str) -> Path:
        with self.lock:
            known = self.verified.get(key)
            if known and self._signatures(known[0], known[1]) == known[2]: return known[0]
            metadata = self._snapshot(key)
            source = self.store.source_id
            object_id, version = identity_digest(key), identity_digest(metadata)
            root = self.directory.resolve()
            # Short directory grouping keeps Windows paths usable. The complete
            # source digest is still part of the final 256-bit cache key.
            namespace = root / "v2" / source[:16]
            if root not in namespace.resolve().parents:
                raise DataContractError("Cache namespace is outside the cache directory")
            namespace.mkdir(parents=True, exist_ok=True)
            target = namespace / (identity_digest([source, key, version]) + ".mp4")
            record = target.with_suffix(".json")
            expected = dict(cache_schema=2, source=source, object=object_id, version=version,
                            size_bytes=metadata["size_bytes"])
            # The lock covers both files and rechecking an already-published
            # version. Competing writers cannot churn a reader's file identity.
            with publication_lock(target.with_suffix(".lock")):
                signature = self._validate(target, record, expected, metadata)
                if signature is None:
                    self._download(key, target, record, expected, metadata)
                    signature = self._validate(target, record, expected, metadata)
                    if signature is None: raise DataContractError("Downloaded cache object failed integrity validation")
            self.verified[key] = (target, record, signature)
            return target

    def _download(self, key, target, record, expected, metadata):
        temporary = []
        try:
            with tempfile.NamedTemporaryFile(dir=target.parent, prefix="video-", suffix=".part", delete=False) as output:
                temporary.append(Path(output.name))
                actual = self.store.download(key, output, metadata)
                output.flush(); os.fsync(output.fileno())
            # Check independently of the store's claimed checksum/length.
            with temporary[0].open("rb") as body:
                verified = copy_stream(body, expected_size=metadata["size_bytes"])
            if actual != verified or metadata.get("sha256", verified["sha256"]) != verified["sha256"]:
                raise DataContractError("Downloaded object does not match its version")
            with tempfile.NamedTemporaryFile(dir=target.parent, prefix="record-", suffix=".part", delete=False) as output:
                temporary.append(Path(output.name))
                output.write(json.dumps({**expected, "sha256": verified["sha256"]}, separators=(",", ":")).encode())
                output.flush(); os.fsync(output.fileno())
            # A different adapter/process may already have published this version.
            if self._validate(target, record, expected, metadata) is None:
                try:
                    os.replace(temporary[0], target)
                    os.replace(temporary[1], record)
                except OSError:
                    if self._validate(target, record, expected, metadata) is None: raise
        except Exception:
            raise DataContractError("Cannot materialize verified video; retry or restart the service") from None
        finally:
            # These are only this call's unique cache-owned staging files.
            for path in temporary: path.unlink(missing_ok=True)
