from __future__ import annotations

import hashlib
import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import BinaryIO, Protocol
from urllib.parse import urlsplit

from .errors import DataContractError, DataNotFound


CHUNK_BYTES = 64 * 1024


def identity_digest(value) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def file_signature(path: Path):
    stat = path.stat()
    return (stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns)


def copy_stream(body, output: BinaryIO | None = None, expected_size: int | None = None):
    digest = hashlib.sha256(); size = 0
    while chunk := body.read(CHUNK_BYTES):
        size += len(chunk)
        if expected_size is not None and size > expected_size:
            raise DataContractError("Object length changed during download; restart the service")
        digest.update(chunk)
        if output is not None: output.write(chunk)
    if expected_size is not None and size != expected_size:
        raise DataContractError("Object download was incomplete; retry or restart the service")
    return {"size_bytes": size, "sha256": digest.hexdigest()}


class ObjectStore(Protocol):
    @property
    def source_id(self) -> str: ...
    def list(self, prefix: str = "") -> list[str]: ...
    def head(self, key: str) -> dict[str, int | str]: ...
    def get(self, key: str) -> bytes: ...
    def get_range(self, key: str, start: int, end: int) -> bytes: ...
    def download(self, key: str, output: BinaryIO, expected: dict) -> dict: ...


@dataclass(frozen=True)
class LocalObjectStore:
    root: Path

    @property
    def source_id(self) -> str:
        return identity_digest(["local", os.path.normcase(str(self.root.resolve()))])

    def _path(self, key: str) -> Path:
        relative = Path(key.replace("/", os.sep))
        path = (self.root / relative).resolve()
        if self.root.resolve() not in path.parents and path != self.root.resolve():
            raise DataNotFound("Object is outside DATA_ROOT")
        return path

    def list(self, prefix: str = "") -> list[str]:
        base = self._path(prefix) if prefix else self.root.resolve()
        if base.is_file():
            return [base.relative_to(self.root.resolve()).as_posix()]
        if not base.exists():
            return []
        return sorted(
            p.relative_to(self.root.resolve()).as_posix()
            for p in base.rglob("*")
            if p.is_file()
        )

    def head(self, key: str) -> dict[str, int | str]:
        path = self._path(key)
        if not path.is_file():
            raise DataNotFound(f"Object not found: {key}")
        before = file_signature(path)
        with path.open("rb") as body:
            result = copy_stream(body, expected_size=before[2])
        if file_signature(path) != before:
            raise DataContractError("Local object changed during version inspection; restart the service")
        return {"key": key, **result}

    def download(self, key: str, output: BinaryIO, expected: dict) -> dict:
        path = self._path(key)
        before = file_signature(path)
        with path.open("rb") as body:
            result = copy_stream(body, output, expected["size_bytes"])
        if file_signature(path) != before or result["sha256"] != expected["sha256"]:
            raise DataContractError("Local object version changed; restart the service")
        return result

    def get(self, key: str) -> bytes:
        path = self._path(key)
        if not path.is_file():
            raise DataNotFound(f"Object not found: {key}")
        return path.read_bytes()

    def get_range(self, key: str, start: int, end: int) -> bytes:
        if start < 0 or end < start:
            raise ValueError("Invalid byte range")
        path = self._path(key)
        if not path.is_file():
            raise DataNotFound(f"Object not found: {key}")
        with path.open("rb") as handle:
            handle.seek(start)
            return handle.read(end - start + 1)


class S3ObjectStore:
    """S3-compatible read-only object client.

    This class intentionally exposes no put/delete/copy methods. Credentials are
    read by boto3 from the environment or its standard credential chain and are
    never included in errors or serialized responses.
    """

    def __init__(self, bucket: str, endpoint: str, region: str = "auto") -> None:
        import boto3

        self.bucket = bucket
        parsed = urlsplit(endpoint)
        # Authentication, query strings and fragments never enter cache identity.
        port = parsed.port or (443 if parsed.scheme.lower() == "https" else 80)
        self._source_id = identity_digest(["s3", parsed.scheme.lower(), (parsed.hostname or "").lower(),
                                           port, parsed.path.rstrip("/"), bucket, region])
        self.client = boto3.client("s3", endpoint_url=endpoint, region_name=region)

    @property
    def source_id(self) -> str:
        return self._source_id

    def list(self, prefix: str = "") -> list[str]:
        keys: list[str] = []
        token: str | None = None
        while True:
            args = {"Bucket": self.bucket, "Prefix": prefix, "MaxKeys": 1000}
            if token:
                args["ContinuationToken"] = token
            page = self.client.list_objects_v2(**args)
            keys.extend(item["Key"] for item in page.get("Contents", []))
            if not page.get("IsTruncated"):
                return sorted(keys)
            token = page.get("NextContinuationToken")

    def head(self, key: str) -> dict[str, int | str]:
        try:
            result = self.client.head_object(Bucket=self.bucket, Key=key)
            metadata = {"key": key, "size_bytes": int(result["ContentLength"])}
            if result.get("VersionId") not in (None, "", "null"): metadata["version_id"] = str(result["VersionId"])
            if result.get("ETag"): metadata["etag"] = str(result["ETag"])
            return metadata
        except Exception:
            raise DataContractError("Cannot inspect object version") from None

    def download(self, key: str, output: BinaryIO, expected: dict) -> dict:
        args = {"Bucket": self.bucket, "Key": key}
        if expected.get("version_id"): args["VersionId"] = expected["version_id"]
        elif expected.get("etag"): args["IfMatch"] = expected["etag"]
        body = None
        try:
            result = self.client.get_object(**args); body = result["Body"]
            for wanted, returned in (("version_id", "VersionId"), ("etag", "ETag")):
                if expected.get(wanted) and result.get(returned) != expected[wanted]:
                    raise DataContractError("Object version changed")
            return copy_stream(body, output, expected["size_bytes"])
        except Exception:
            raise DataContractError("Object download failed or version changed; retry or restart the service") from None
        finally:
            if body is not None: body.close()

    def get(self, key: str) -> bytes:
        body = self.client.get_object(Bucket=self.bucket, Key=key)["Body"]
        try: return body.read()
        finally: body.close()

    def get_range(self, key: str, start: int, end: int) -> bytes:
        if start < 0 or end < start:
            raise ValueError("Invalid byte range")
        body = self.client.get_object(
            Bucket=self.bucket, Key=key, Range=f"bytes={start}-{end}"
        )["Body"]
        try: return body.read()
        finally: body.close()


def make_store(settings) -> ObjectStore:
    if settings.data_root:
        return LocalObjectStore(settings.data_root)
    if settings.s3_bucket and settings.s3_endpoint:
        return S3ObjectStore(settings.s3_bucket, settings.s3_endpoint, settings.s3_region)
    raise ValueError("Configure DATA_ROOT or S3_BUCKET/S3_ENDPOINT")

