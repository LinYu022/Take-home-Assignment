from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


def _int_env(name: str, default: int) -> int:
    value = os.getenv(name)
    if value is None:
        return default
    try:
        return int(value)
    except ValueError as exc:
        raise ValueError(f"{name} must be an integer") from exc


@dataclass(frozen=True)
class Settings:
    data_root: Path | None
    s3_bucket: str | None
    s3_endpoint: str | None
    s3_region: str
    public_base_url: str
    cache_dir: Path
    max_render_width: int = 1600
    max_render_height: int = 1200
    max_query_rows: int = 256
    max_query_scalars: int = 4096
    max_query_bytes: int = 128_000
    max_view_url_chars: int = 16_384
    allowed_origins: tuple[str, ...] = ()
    max_aggregate_rows: int = 1_000_000
    max_image_bytes: int = 2_097_152
    max_audit_rows: int = 8192
    max_audit_scalars: int = 1_000_000

    def __post_init__(self):
        # The work reservation is proven only within these hard input caps.
        # Operators may reduce them, never allocate an unmeasured larger scan.
        if not 1<=self.max_audit_rows<=8192 or not 1<=self.max_audit_scalars<=1_000_000:
            raise ValueError("Audit limits must be rows 1..8192 and scalars 1..1000000")

    @classmethod
    def from_env(cls) -> "Settings":
        root = os.getenv("DATA_ROOT")
        cache = os.getenv("LEROBOT_CACHE_DIR") or os.path.join(
            os.getenv("TEMP", "."), "lerobot-visualizer-cache"
        )
        return cls(
            data_root=Path(root).expanduser().resolve() if root else None,
            s3_bucket=os.getenv("S3_BUCKET"),
            s3_endpoint=os.getenv("S3_ENDPOINT"),
            s3_region=os.getenv("S3_REGION", "auto"),
            public_base_url=os.getenv("PUBLIC_BASE_URL", "http://127.0.0.1:8000").rstrip("/"),
            cache_dir=Path(cache).expanduser().resolve(),
            max_render_width=_int_env("MAX_RENDER_WIDTH", 1600),
            max_render_height=_int_env("MAX_RENDER_HEIGHT", 1200),
            max_query_rows=_int_env("MAX_QUERY_ROWS", 256),
            max_query_scalars=_int_env("MAX_QUERY_SCALARS", 4096),
            max_query_bytes=_int_env("MAX_QUERY_BYTES", 128_000),
            max_view_url_chars=_int_env("MAX_VIEW_URL_CHARS", 16_384),
            max_aggregate_rows=_int_env("MAX_AGGREGATE_ROWS", 1_000_000),
            max_image_bytes=_int_env("MAX_IMAGE_BYTES", 2_097_152),
            max_audit_rows=_int_env("MAX_AUDIT_ROWS",8192),
            max_audit_scalars=_int_env("MAX_AUDIT_SCALARS",1_000_000),
            allowed_origins=tuple(
                x.strip().rstrip("/")
                for x in os.getenv("MCP_ALLOWED_ORIGINS", "").split(",")
                if x.strip()
            ),
        )

    def validate_source(self) -> None:
        if self.data_root and self.data_root.exists():
            return
        if self.s3_bucket and self.s3_endpoint:
            return
        raise ValueError("Configure DATA_ROOT or S3_BUCKET together with S3_ENDPOINT")

