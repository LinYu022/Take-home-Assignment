"""Build a clean, reproducible submission directory and ZIP archive.

The package is assembled from an explicit allowlist. Runtime environments,
credentials, caches, logs, raw datasets, and browser failure output are never
copied even if they happen to exist under the repository root.
"""
from __future__ import annotations

import argparse
import hashlib
import shutil
import zipfile
from datetime import date
from pathlib import Path


ROOT_FILES = (
    ".env.example",
    ".gitignore",
    "AGENTS.md",
    "README.md",
    "requirements.txt",
    "requirements-browser.txt",
    "tool-iterations.md",
)
ROOT_DIRS = ("app", "analysis", "agent-sessions", "docs", "scripts", "tests")
FORBIDDEN_PARTS = {".git", ".runtime", ".venv", ".pytest_cache", "__pycache__", "test-results"}
FORBIDDEN_SUFFIXES = {".parquet", ".mp4", ".mov", ".avi", ".log"}


def _safe_source(path: Path, base: Path) -> bool:
    relative = path.relative_to(base)
    links = [path, *[p for p in path.parents if p != base and base in p.parents]]
    return path.is_file() and not any(p.is_symlink() or p.is_junction() for p in links) and not any(part in FORBIDDEN_PARTS for part in relative.parts) and path.suffix.lower() not in FORBIDDEN_SUFFIXES and path.name != ".env"


def _copy_tree(source: Path, destination: Path) -> list[Path]:
    copied: list[Path] = []
    for path in source.rglob("*"):
        if not _safe_source(path, source):
            continue
        relative = path.relative_to(source)
        target = destination / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(path, target)
        copied.append(target)
    return copied


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def build(output_dir: Path, archive_path: Path, root: Path) -> tuple[Path, Path]:
    root = root.resolve()
    for candidate in (output_dir, archive_path):
        if candidate.is_symlink() or candidate.is_junction():
            raise SystemExit("delivery targets must not be links")
    output_dir = output_dir.resolve()
    archive_path = archive_path.resolve()
    if output_dir == root or root in output_dir.parents or output_dir in root.parents:
        raise SystemExit("output directory must not overlap the repository or its ancestors")
    if archive_path == root or root in archive_path.parents or archive_path in root.parents:
        raise SystemExit("ZIP path must be outside the repository")
    if archive_path == output_dir or output_dir in archive_path.parents:
        raise SystemExit("ZIP must not be inside the output directory")
    if output_dir.exists() or archive_path.exists():
        raise SystemExit("delivery targets must not exist; choose new names (nothing was removed)")
    # Validate the complete allowlist before creating either target.
    for relative in ROOT_FILES:
        if not _safe_source(root / relative, root):
            raise SystemExit(f"required delivery file is missing or forbidden: {relative}")
    for relative in ROOT_DIRS:
        directory = root / relative
        if not directory.is_dir() or directory.is_symlink() or directory.is_junction():
            raise SystemExit(f"required delivery directory is missing: {relative}")
    output_dir.mkdir(parents=True, exist_ok=False)
    copied: list[Path] = []
    for relative in ROOT_FILES:
        source = root / relative
        if not source.is_file() or not _safe_source(source, root):
            raise SystemExit(f"required delivery file is missing or forbidden: {relative}")
        target = output_dir / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, target)
        copied.append(target)
    for relative in ROOT_DIRS:
        source = root / relative
        if not source.is_dir():
            raise SystemExit(f"required delivery directory is missing: {relative}")
        copied.extend(_copy_tree(source, output_dir / relative))

    archive_readme = output_dir / "archive" / "README.md"
    archive_readme.parent.mkdir(parents=True, exist_ok=True)
    archive_readme.write_text(
        "# 过程材料归档\n\n"
        "这里仅保留无法作为运行文件的审核过程材料。它们不参与应用启动，也不代表当前版本的成功验收。\n\n"
        "- `process/audit-current-session.jsonl`：修复前审核回放，保留用于审计对照。\n"
        "- Playwright 截图和 trace 未进入交付包；它们属于本地诊断产物。\n",
        encoding="utf-8",
    )
    copied.append(archive_readme)
    audit = root / ".runtime" / "audit-current-session.jsonl"
    if audit.is_file():
        target = output_dir / "archive" / "process" / audit.name
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(audit, target)
        copied.append(target)

    forbidden = [p for p in output_dir.rglob("*") if p.is_file() and (p.suffix.lower() in FORBIDDEN_SUFFIXES or any(part in FORBIDDEN_PARTS for part in p.relative_to(output_dir).parts) or p.name == ".env")]
    if forbidden:
        raise SystemExit("forbidden files entered delivery: " + ", ".join(str(p.relative_to(output_dir)) for p in forbidden))

    manifest = output_dir / "DELIVERY_MANIFEST.md"
    rows = sorted((p.relative_to(output_dir).as_posix(), p.stat().st_size, _sha256(p)) for p in copied if p.is_file())
    lines = [
        "# Delivery manifest",
        "",
        f"Generated: {date.today().isoformat()}",
        "",
        "This package contains runtime source, tests, configuration templates, formal reports, agent sessions, and required acceptance evidence.",
        "",
        "Excluded by design: credentials, `.env`, raw Parquet/MP4/MOV/AVI, `.runtime`, virtual environments, caches, logs, `.git`, and browser test-results.",
        "",
        "| Path | Bytes | SHA-256 |",
        "| --- | ---: | --- |",
    ]
    lines.extend(f"| `{path}` | {size} | `{digest}` |" for path, size, digest in rows)
    manifest.write_text("\n".join(lines) + "\n", encoding="utf-8")
    copied.append(manifest)

    archive_path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(archive_path, "x", compression=zipfile.ZIP_DEFLATED, compresslevel=9) as archive:
        for path in sorted(output_dir.rglob("*")):
            if path.is_file():
                archive.write(path, Path(output_dir.name) / path.relative_to(output_dir))
    return output_dir, archive_path


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", type=Path)
    parser.add_argument("--zip", dest="archive", type=Path)
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    stamp = date.today().strftime("%Y%m%d")
    output = args.output_dir or root.parent / f"{root.name}-delivery-{stamp}"
    archive = args.archive or root.parent / f"{root.name}-delivery-{stamp}.zip"
    try:
        directory, zipped = build(output, archive, root)
    except OSError as exc:
        raise SystemExit(f"Packaging failed: {exc}. Partial outputs, if any, are preserved at {output} and {archive}; nothing was removed.") from exc
    print(f"delivery_dir={directory}")
    print(f"delivery_zip={zipped}")
    print(f"files={sum(1 for p in directory.rglob('*') if p.is_file())}")
    print(f"zip_bytes={zipped.stat().st_size}")


if __name__ == "__main__":
    main()
