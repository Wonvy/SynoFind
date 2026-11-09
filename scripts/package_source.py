#!/usr/bin/env python3
"""Package source from a narrow allowlist; exclude local tools, credentials and data."""
from __future__ import annotations

import argparse
import hashlib
from pathlib import Path
import shutil
import zipfile

ROOT = Path(__file__).resolve().parents[1]
SOURCE_DIRS = {"cmd", "internal", "web", "packaging", "scripts", "docs", "examples"}
SOURCE_FILES = {"README.md", "LICENSE", "go.mod", "go.sum", ".gitignore", ".gitattributes"}
EXCLUDED_PARTS = {"node_modules", ".git", "__pycache__", "dist", "runtime", "work", "outputs", ".tools", ".cache"}
EXCLUDED_SUFFIXES = {".exe", ".db", ".sqlite", ".sqlite3", ".pyc", ".spk", ".zip", ".pem", ".key", ".pfx", ".p12"}


def source_files(root: Path):
    for item in sorted(root.iterdir()):
        if item.is_symlink() or item.is_junction():
            continue
        if item.is_file() and item.name in SOURCE_FILES:
            yield item
        elif item.is_dir() and item.name in SOURCE_DIRS:
            for path in sorted(item.rglob("*")):
                relative = path.relative_to(root)
                if any(part in EXCLUDED_PARTS or part.startswith(".env") for part in relative.parts):
                    continue
                if path.is_symlink() or path.is_junction() or not path.is_file() or path.suffix in EXCLUDED_SUFFIXES:
                    continue
                # Do not archive through a directory link/junction to external data.
                if any(parent.is_symlink() or parent.is_junction() for parent in path.parents if parent != root and root in parent.parents):
                    continue
                yield path


def package(output: Path):
    output.parent.mkdir(parents=True, exist_ok=True)
    files = list(source_files(ROOT))
    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=9) as archive:
        for path in files:
            relative = path.relative_to(ROOT).as_posix()
            entry = zipfile.ZipInfo("SynoFind/" + relative, date_time=(1980, 1, 1, 0, 0, 0))
            entry.compress_type = zipfile.ZIP_DEFLATED
            entry.create_system = 3
            entry.external_attr = (0o100755 if relative.startswith("packaging/scripts/") or relative == "packaging/payload/bin/run-service" else 0o100644) << 16
            archive.writestr(entry, path.read_bytes())
    checksum = hashlib.sha256(output.read_bytes()).hexdigest()
    output.with_suffix(output.suffix + ".sha256").write_text(f"{checksum}  {output.name}\n", encoding="ascii")
    # Convenient user-facing copies, with relative links preserved.
    for name in ("install.md", "api.md", "openapi.json", "testing.md", "THIRD_PARTY_NOTICES.txt"):
        target = output.parent / "docs" / name
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(ROOT / "docs" / name, target)
    target = output.parent / "examples" / "fetch_file.py"
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(ROOT / "examples" / "fetch_file.py", target)
    print(f"{output} ({len(files)} source files, {output.stat().st_size} bytes)")
    print(f"SHA256 {checksum}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=ROOT / "outputs/SynoFind-0.1.0-source.zip")
    package(parser.parse_args().output)
