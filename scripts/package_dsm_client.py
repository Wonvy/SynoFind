#!/usr/bin/env python3
"""Create a reproducible, source-only DSM API client ZIP from a strict allowlist."""
from __future__ import annotations

import argparse
import ast
import hashlib
from pathlib import Path
import tomllib
import zipfile

ROOT = Path(__file__).resolve().parents[1]
CLIENT_ROOT = ROOT / "clients" / "python"
VERSION = tomllib.loads((CLIENT_ROOT / "pyproject.toml").read_text(encoding="utf-8"))["project"]["version"]
ALLOWED_TOP_FILES = {"README.md", "DESKTOP.md", "pyproject.toml"}
ALLOWED_DIRECTORIES = {"synofind_dsm", "tests", "examples"}
ALLOWED_EXTENSIONS = {".py", ".md", ".toml", ".txt", ".json"}
VENDOR_EXTENSIONS = {".mjs", ".js", ".css", ".bcmap", ".ttf", ".otf", ".pfb", ".wasm", ".icc", ".md", ".txt"}
EXCLUDED_PARTS = {
    "__pycache__", ".pytest_cache", ".mypy_cache", ".ruff_cache", ".git",
    ".venv", "venv", "env", "node_modules", "build", "dist", "work", "outputs",
    "secrets", "credentials", "passwords", "runtime", "logs",
}
SENSITIVE_STEMS = {"credentials", "credential", "password", "passwords", "secrets", "secret", "token", "tokens", "session", "sessions", "config.local"}


def is_link(path: Path) -> bool:
    return path.is_symlink() or getattr(path, "is_junction", lambda: False)()


def allowed_path(relative: Path) -> bool:
    lowered = [part.lower() for part in relative.parts]
    if any(part in EXCLUDED_PARTS or part.startswith(".env") or part.endswith(".egg-info") for part in lowered):
        return False
    if relative.parts[:2] == ("synofind_dsm", "desktop_web"):
        if relative.parts[:3] == ("synofind_dsm", "desktop_web", "vendor"):
            return (relative.suffix.lower() in VENDOR_EXTENSIONS
                    or relative.name.upper().startswith(("LICENSE", "NOTICE")))
        return relative.suffix.lower() in {".html", ".js", ".css"}
    if (relative.suffix.lower() != ".py" and relative.stem.lower() in SENSITIVE_STEMS) or relative.suffix.lower() not in ALLOWED_EXTENSIONS:
        return False
    if len(relative.parts) == 1:
        return relative.name in ALLOWED_TOP_FILES
    return relative.parts[0] in ALLOWED_DIRECTORIES


def source_files(client_root: Path):
    # Walk only known source folders; never descend into linked or excluded trees.
    for name in sorted(ALLOWED_TOP_FILES):
        path = client_root / name
        if not path.is_file() or is_link(path):
            raise ValueError(f"Required client file is missing or linked: {name}")
        yield Path(name), path
    for name in sorted(ALLOWED_DIRECTORIES):
        start = client_root / name
        if not start.exists():
            continue
        stack = [start]
        while stack:
            directory = stack.pop()
            if is_link(directory):
                continue
            for path in sorted(directory.iterdir()):
                relative = path.relative_to(client_root)
                if is_link(path):
                    continue
                if path.is_dir():
                    lowered = path.name.lower()
                    if lowered not in EXCLUDED_PARTS and not lowered.startswith(".env") and not lowered.endswith(".egg-info"):
                        stack.append(path)
                elif path.is_file() and allowed_path(relative):
                    yield relative, path


def package(output: Path, client_root: Path = CLIENT_ROOT, example: Path | None = None) -> dict:
    if is_link(client_root):
        raise ValueError("Client source root must not be a link.")
    entries = {relative.as_posix(): path for relative, path in source_files(client_root)}
    # The shared repository example is copied into the standalone distribution.
    example = example if example is not None else ROOT / "examples" / "direct_search.py"
    if not example.is_file() or is_link(example):
        raise ValueError("Required direct_search.py example is missing or linked.")
    example_name = "examples/direct_search.py"
    if example_name in entries and entries[example_name].read_bytes() != example.read_bytes():
        raise ValueError("Conflicting direct_search.py examples; resolve before packaging.")
    entries[example_name] = example
    for required in (
        "synofind_dsm/__init__.py", "synofind_dsm/__main__.py", "synofind_dsm/cli.py",
        "synofind_dsm/desktop.py", "synofind_dsm/desktop_web/index.html",
        "synofind_dsm/desktop_web/app.js", "synofind_dsm/desktop_web/styles.css",
        "synofind_dsm/desktop_web/vendor/pdf.mjs", "synofind_dsm/desktop_web/vendor/pdf.worker.mjs",
        "synofind_dsm/desktop_web/vendor/PDFJS-LICENSE.txt",
    ):
        if required not in entries:
            raise ValueError(f"Required runtime source is missing: {required}")
    if not any(name.startswith("tests/") and name.endswith(".py") for name in entries):
        raise ValueError("Client tests are missing.")
    if any(not allowed_path(Path(name)) for name in entries):
        raise ValueError("A non-allowlisted path reached the archive.")
    # Catch incomplete source snapshots (for example, a newly imported module
    # which has not been written yet) without executing code or contacting DSM.
    for name, path in entries.items():
        if not name.startswith("synofind_dsm/") or not name.endswith(".py"):
            continue
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8-sig"), filename=name)):
            if not isinstance(node, ast.ImportFrom) or not node.level or not node.module:
                continue
            parent = Path(name).parent
            for _ in range(node.level - 1):
                parent = parent.parent
            imported = (parent / Path(*node.module.split("."))).as_posix()
            if imported + ".py" not in entries and imported + "/__init__.py" not in entries:
                raise ValueError(f"Incomplete source snapshot: {name} imports missing {imported}")
    output.parent.mkdir(parents=True, exist_ok=True)
    prefix = f"SynoFind-DSM-API-Client-{VERSION}/"
    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=9) as archive:
        for name, path in sorted(entries.items()):
            entry = zipfile.ZipInfo(prefix + name, date_time=(1980, 1, 1, 0, 0, 0))
            entry.compress_type = zipfile.ZIP_DEFLATED
            entry.create_system = 3
            entry.external_attr = 0o100644 << 16
            archive.writestr(entry, path.read_bytes())
    with zipfile.ZipFile(output) as archive:
        if archive.testzip() is not None or len(archive.namelist()) != len(entries):
            raise ValueError("ZIP integrity verification failed.")
    checksum = hashlib.sha256(output.read_bytes()).hexdigest()
    output.with_suffix(output.suffix + ".sha256").write_text(f"{checksum}  {output.name}\n", encoding="ascii")
    return {"path": str(output), "files": len(entries), "bytes": output.stat().st_size, "sha256": checksum}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=ROOT / "outputs" / f"SynoFind-DSM-API-Client-{VERSION}.zip")
    result = package(parser.parse_args().output)
    print(f"{result['path']} ({result['files']} files, {result['bytes']} bytes)")
    print(f"SHA256 {result['sha256']}")


if __name__ == "__main__":
    main()
