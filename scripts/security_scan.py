"""Fail if credentials or raw dataset files are present in the deliverable tree."""
from __future__ import annotations

import re
from pathlib import Path


ROOT = Path(__file__).parents[1]
SECRET_PATTERNS = [
    re.compile(r"AKIA[0-9A-Z]{16}"),
    re.compile(r"aws_secret_access_key\s*[:=]\s*[^\s#]+", re.I),
    re.compile(r"secret_access_key\s*[:=]\s*[^\s#]+", re.I),
]
RAW_SUFFIXES = {".parquet", ".mp4", ".mov", ".avi"}


def main() -> None:
    failures: list[str] = []
    for path in ROOT.rglob("*"):
        if not path.is_file() or any(part in {".git", ".runtime", ".venv", "__pycache__"} for part in path.parts):
            continue
        if path.name == ".env.example":
            continue
        if path.suffix.lower() in RAW_SUFFIXES:
            failures.append(f"raw dataset file: {path.relative_to(ROOT)}")
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue
        for pattern in SECRET_PATTERNS:
            if pattern.search(text):
                failures.append(f"credential pattern in {path.relative_to(ROOT)}")
    if failures:
        raise SystemExit("\n".join(failures))
    print("security scan passed")


if __name__ == "__main__":
    main()

