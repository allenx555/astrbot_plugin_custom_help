import importlib.util
import subprocess
from pathlib import Path
from zipfile import ZipFile

import pytest

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "package_release", ROOT / ".github/scripts/package_release.py"
)
release = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(release)


def source_repo(tmp_path):
    source = tmp_path / "plugin"
    source.mkdir()
    subprocess.run(["git", "init", "-q", str(source)], check=True)
    for name, content in {
        "metadata.yaml": "name: astrbot_plugin_custom_help\nversion: v0.2.1\ndisplay_name: 自定义帮助菜单\n",
        "main.py": "# plugin\n",
        "templates/base.typ": "// template\n",
        "core/menu.py": "# menu\n",
        "resources/fonts/sample.ttf": "font",
        "tests/private_test.py": "test",
        "preview/historical.png": "old screenshot",
        "data/menu.json": "private",
        ".github/workflows/release.yml": "workflow",
    }.items():
        file = source / name
        file.parent.mkdir(parents=True, exist_ok=True)
        file.write_text(content, encoding="utf-8")
    subprocess.run(["git", "-C", str(source), "add", "."], check=True)
    (source / "resources/untracked-secret.txt").write_text("private", encoding="utf-8")
    return source


def test_release_zip_contains_runtime_files_only_and_is_reproducible(tmp_path):
    source = source_repo(tmp_path)
    notes = tmp_path / "notes"
    notes.mkdir()
    (notes / "v0.2.1.md").write_text("Release notes", encoding="utf-8")
    result = release.package(source, "v0.2.1", tmp_path / "dist", notes)
    asset = Path(result["asset"])
    first = asset.read_bytes()
    assert asset.name == "astrbot_plugin_custom_help-v0.2.1.zip"
    assert result["title"] == "v0.2.1"
    assert result["notes"] == (notes / "v0.2.1.md").resolve().as_posix()
    with ZipFile(asset) as archive:
        assert set(archive.namelist()) == {
            "metadata.yaml",
            "main.py",
            "templates/base.typ",
            "core/menu.py",
            "resources/fonts/sample.ttf",
        }
    release.package(source, "v0.2.1", tmp_path / "dist", notes)
    assert asset.read_bytes() == first


@pytest.mark.parametrize("tag", ["v0.3.0", "v0.3.0-beta.1"])
def test_release_title_is_only_the_version_tag(tmp_path, tag):
    source = source_repo(tmp_path)
    metadata = source / "metadata.yaml"
    metadata.write_text(
        metadata.read_text(encoding="utf-8").replace("v0.2.1", tag), encoding="utf-8"
    )
    result = release.package(source, tag, tmp_path / "dist", tmp_path / "notes")
    assert result["title"] == tag


def test_oversized_installation_zip_is_rejected(tmp_path, monkeypatch):
    source = source_repo(tmp_path)
    monkeypatch.setattr(release, "PACKAGE_SIZE_LIMIT", 1)
    with pytest.raises(ValueError, match="16 MiB"):
        release.package(source, "v0.2.1", tmp_path / "dist", tmp_path / "notes")


@pytest.mark.parametrize("tag", ["v0.2.2", "../../v0.2.1", "not-a-version"])
def test_invalid_or_mismatched_tag_cannot_publish(tmp_path, tag):
    source = source_repo(tmp_path)
    with pytest.raises(ValueError):
        release.package(source, tag, tmp_path / "dist", tmp_path / "notes")
    assert not (tmp_path / "dist").exists()
