"""Build an installation ZIP from tracked files of the tagged checkout."""

import argparse
import os
import re
import subprocess
from pathlib import Path
from zipfile import ZIP_DEFLATED, ZipFile, ZipInfo

import yaml

RUNTIME_DIRS = {"core", "domain", "utils", "templates", "resources", "preview"}
RUNTIME_FILES = {
    "main.py",
    "__init__.py",
    "metadata.yaml",
    "_conf_schema.json",
    "requirements.txt",
    "LICENSE",
    "README.md",
    "MANUAL_MENU.md",
    "menu.example.json",
    "logo.png",
}
REQUIRED_FILES = {"main.py", "metadata.yaml", "templates/base.typ", "core/menu.py"}
VERSION_PATTERN = re.compile(r"v\d+\.\d+\.\d+(?:-[0-9A-Za-z.-]+)?")


def package(source: Path, tag: str, output: Path, notes_dir: Path):
    if not VERSION_PATTERN.fullmatch(tag):
        raise ValueError("Expected a version tag such as v0.2.1 or v0.3.0-beta.1")
    source = source.resolve()
    metadata = yaml.safe_load(
        (source / "metadata.yaml").read_text(encoding="utf-8-sig")
    )
    if metadata["version"] != tag:
        raise ValueError(
            f"Tag {tag} does not match metadata version {metadata['version']}"
        )
    name = metadata["name"]
    if not re.fullmatch(r"astrbot_plugin_[a-z0-9_]+", name):
        raise ValueError("Invalid plugin name")
    tracked = subprocess.check_output(["git", "-C", str(source), "ls-files", "-z"])
    files = []
    for raw in tracked.decode("utf-8").split("\0"):
        if not raw:
            continue
        relative = Path(raw)
        if relative.is_absolute() or ".." in relative.parts:
            raise ValueError("Invalid tracked path")
        if relative.parts[0] in RUNTIME_DIRS or raw in RUNTIME_FILES:
            if relative.suffix in {".pyc", ".pyo"} or "__pycache__" in relative.parts:
                continue
            file = source / relative
            if file.is_symlink() or not file.is_file():
                raise ValueError(f"Missing or unsupported runtime file: {raw}")
            files.append(relative)
    missing = REQUIRED_FILES - {p.as_posix() for p in files}
    if missing:
        raise ValueError(f"Required installation files are missing: {sorted(missing)}")
    output.mkdir(parents=True, exist_ok=True)
    asset = output / f"{name}-{tag}.zip"
    with ZipFile(asset, "w", ZIP_DEFLATED) as archive:
        for relative in sorted(files):
            # Fixed timestamps make reruns reproducible for unchanged contents.
            info = ZipInfo(relative.as_posix(), date_time=(1980, 1, 1, 0, 0, 0))
            info.compress_type = ZIP_DEFLATED
            info.external_attr = 0o100644 << 16
            archive.writestr(info, (source / relative).read_bytes())
    with ZipFile(asset) as archive:
        if archive.testzip() is not None:
            raise ValueError("Installation ZIP validation failed")
    notes = notes_dir / f"{tag}.md"
    title = f"{tag} — {metadata.get('display_name', name)}".replace("\n", " ").replace(
        "\r", " "
    )
    return {
        "title": title,
        "asset": asset.resolve().as_posix(),
        "notes": notes.resolve().as_posix() if notes.is_file() else "",
    }


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--tag", required=True)
    parser.add_argument("--output", type=Path, default=Path("dist"))
    parser.add_argument("--notes-dir", type=Path, default=Path(".github/release-notes"))
    args = parser.parse_args()
    result = package(args.source, args.tag, args.output, args.notes_dir)
    if os.environ.get("GITHUB_OUTPUT"):
        with open(os.environ["GITHUB_OUTPUT"], "a", encoding="utf-8") as output:
            for key, value in result.items():
                output.write(f"{key}={value}\n")
    print(f"Validated installation package: {result['asset']}")


if __name__ == "__main__":
    main()
