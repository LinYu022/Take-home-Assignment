from pathlib import Path
import zipfile
import os
import subprocess

import pytest

from scripts import package_delivery as package


@pytest.fixture
def source(tmp_path, monkeypatch):
    root = tmp_path / "source"; root.mkdir()
    (root / "README.md").write_text("readme", encoding="utf-8")
    (root / "app").mkdir(); (root / "app/main.py").write_text("# source", encoding="utf-8")
    monkeypatch.setattr(package, "ROOT_FILES", ("README.md",))
    monkeypatch.setattr(package, "ROOT_DIRS", ("app",))
    def forbidden(*args, **kwargs):
        pytest.fail("packaging must never delete or unlink existing targets")
    monkeypatch.setattr(package.shutil, "rmtree", forbidden)
    monkeypatch.setattr(Path, "unlink", forbidden)
    return root


def test_package_rejects_repository_ancestors_and_children(source, tmp_path):
    marker = tmp_path / "sentinel"; marker.write_text("keep")
    for target in (source, source / "child", *source.parents):
        with pytest.raises(SystemExit):
            package.build(target, tmp_path / "new.zip", source)
    assert marker.read_text() == "keep" and not (tmp_path / "new.zip").exists()


def test_package_existing_targets_are_unchanged(source, tmp_path):
    output = tmp_path / "existing"; output.mkdir(); (output / "keep").write_bytes(b"keep")
    archive = tmp_path / "existing.zip"; archive.write_bytes(b"existing archive")
    with pytest.raises(SystemExit):
        package.build(output, tmp_path / "new.zip", source)
    with pytest.raises(SystemExit):
        package.build(tmp_path / "new", archive, source)
    assert archive.read_bytes() == b"existing archive"
    assert (output / "keep").read_bytes() == b"keep" and not (tmp_path / "new").exists()


def test_package_rejects_nested_zip_and_missing_source_before_writing(source, tmp_path):
    output = tmp_path / "delivery"
    with pytest.raises(SystemExit):
        package.build(output, output / "nested.zip", source)
    (source / "README.md").rename(source / "missing")
    with pytest.raises(SystemExit):
        package.build(output, tmp_path / "new.zip", source)
    assert not output.exists()


def test_package_new_targets_and_repeat(source, tmp_path):
    output, archive = package.build(tmp_path / "delivery", tmp_path / "delivery.zip", source)
    before = archive.read_bytes()
    with zipfile.ZipFile(archive) as zipped:
        assert "delivery/app/main.py" in zipped.namelist()
        assert "delivery/DELIVERY_MANIFEST.md" in zipped.namelist()
    with pytest.raises(SystemExit):
        package.build(output, archive, source)
    assert archive.read_bytes() == before


def test_package_resolves_aliases_and_rejects_link_targets(source, tmp_path):
    link = tmp_path / "alias"
    if os.name == "nt":
        result = subprocess.run(["cmd", "/c", "mklink", "/J", str(link), str(tmp_path)], capture_output=True)
        if result.returncode:
            pytest.skip("junction creation is unavailable in this restricted Windows test host")
    else:
        link.symlink_to(tmp_path, target_is_directory=True)
    for target in (link, link / "source" / "child", link / "source" / ".."):
        with pytest.raises(SystemExit): package.build(target, tmp_path / "new.zip", source)
    assert (source / "README.md").read_text() == "readme"


def test_package_exclusive_zip_creation_preserves_racing_file(source, tmp_path, monkeypatch):
    output, archive = tmp_path / "delivery", tmp_path / "delivery.zip"
    original = package.zipfile.ZipFile
    def race(path, mode, **kwargs):
        assert mode == "x"
        path.write_bytes(b"created by another writer")
        return original(path, mode, **kwargs)
    monkeypatch.setattr(package.zipfile, "ZipFile", race)
    with pytest.raises(FileExistsError): package.build(output, archive, source)
    assert archive.read_bytes() == b"created by another writer"
    assert (output / "DELIVERY_MANIFEST.md").exists()
