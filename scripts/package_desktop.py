#!/usr/bin/env python3
"""Package the Windows app and a source-only rebuild archive; exclude local state."""
from __future__ import annotations

import argparse
import hashlib
import http.cookiejar
import importlib.metadata
import json
import os
from pathlib import Path
import pyexpat
import queue
import re
import shutil
import socket
import ssl
import struct
import subprocess
import sys
import tempfile
import threading
import time
import tomllib
from urllib.parse import urlsplit
from urllib.request import HTTPCookieProcessor, ProxyHandler, Request, build_opener, urlopen
import zipfile
import zlib

ROOT = Path(__file__).resolve().parents[1]
CLIENT = ROOT / "clients" / "python"
VERSION = tomllib.loads((CLIENT / "pyproject.toml").read_text(encoding="utf-8"))["project"]["version"]
EXCLUDED = {"__pycache__", ".git", ".pytest_cache", ".mypy_cache", ".ruff_cache", ".venv", "venv", "env", "build", "dist", "work", "outputs", "node_modules", "runtime", "logs", "credentials", "secrets"}
SENSITIVE_STEMS = {"credential", "credentials", "password", "passwords", "secret", "secrets", "token", "tokens", "session", "sessions", "settings", "config.local"}
SOURCE_SUFFIXES = {".py", ".toml", ".md", ".txt", ".html", ".css", ".js", ".json"}
VENDOR_SUFFIXES = {".mjs", ".js", ".css", ".bcmap", ".ttf", ".otf", ".pfb", ".wasm", ".icc", ".md", ".txt"}


def vendor_asset(relative: Path) -> bool:
    return (relative.parts[:3] == ("synofind_dsm", "desktop_web", "vendor")
            and (relative.suffix.lower() in VENDOR_SUFFIXES
                 or relative.name.upper().startswith(("LICENSE", "NOTICE"))))


def linked(path: Path) -> bool:
    return path.is_symlink() or getattr(path, "is_junction", lambda: False)()


def write_zip(output: Path, prefix: str, entries: dict[str, bytes]) -> str:
    output.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=9) as archive:
        for name, data in sorted(entries.items()):
            if name.startswith(("/", "\\")) or ".." in Path(name).parts:
                raise ValueError("Invalid archive member")
            item = zipfile.ZipInfo(prefix + "/" + name, (1980, 1, 1, 0, 0, 0))
            item.compress_type = zipfile.ZIP_DEFLATED
            item.create_system = 3
            item.external_attr = (0o100755 if name.endswith(".exe") else 0o100644) << 16
            archive.writestr(item, data)
    with zipfile.ZipFile(output) as archive:
        if archive.testzip() is not None:
            raise ValueError("ZIP integrity verification failed")
    digest = hashlib.sha256(output.read_bytes()).hexdigest()
    output.with_suffix(output.suffix + ".sha256").write_text(f"{digest}  {output.name}\n", encoding="ascii")
    print(f"{output.name}: {len(entries)} files, {output.stat().st_size} bytes, SHA256 {digest}")
    return digest


def source_entries() -> dict[str, bytes]:
    result: dict[str, bytes] = {}
    stack = [CLIENT]
    while stack:
        directory = stack.pop()
        if linked(directory):
            continue
        for path in sorted(directory.iterdir()):
            if linked(path):
                continue
            name = path.name.lower()
            if name in EXCLUDED or name.startswith(".env") or name.endswith(".egg-info"):
                continue
            if path.is_dir():
                stack.append(path)
            elif path.is_file() and (path.suffix.lower() in SOURCE_SUFFIXES or vendor_asset(path.relative_to(CLIENT))) and not (path.suffix.lower() != ".py" and path.stem.lower() in SENSITIVE_STEMS):
                # Only package known client source, tests, documentation and assets.
                relative = path.relative_to(CLIENT)
                if len(relative.parts) > 1 and relative.parts[0] not in {"synofind_dsm", "tests", "examples"}:
                    continue
                if len(relative.parts) == 1 and relative.name not in {"README.md", "DESKTOP.md", "pyproject.toml"}:
                    continue
                result[path.relative_to(ROOT).as_posix()] = path.read_bytes()
    for relative in ("README.md", "scripts/build_desktop.ps1", "scripts/package_desktop.py", "examples/direct_search.py"):
        path = ROOT / relative
        if not path.is_file() or linked(path):
            raise ValueError(f"Required source is missing or linked: {relative}")
        result[relative] = path.read_bytes()
    for relative in ("clients/python/synofind_dsm/desktop.py", "clients/python/synofind_dsm/desktop_web/index.html", "clients/python/synofind_dsm/desktop_web/app.js", "clients/python/synofind_dsm/desktop_web/styles.css", "clients/python/synofind_dsm/desktop_web/vendor/pdf.mjs", "clients/python/synofind_dsm/desktop_web/vendor/pdf.worker.mjs", "clients/python/synofind_dsm/desktop_web/vendor/PDFJS-LICENSE.txt", "clients/python/DESKTOP.md"):
        if relative not in result:
            raise ValueError(f"Desktop source is incomplete: {relative}")
    return result


def upstream_text(url: str, cache: Path) -> str:
    if not cache.exists():
        with urlopen(url, timeout=30) as response:
            data = response.read(256 * 1024 + 1)
        if len(data) > 256 * 1024:
            raise ValueError("Unexpectedly large upstream license")
        cache.parent.mkdir(parents=True, exist_ok=True)
        cache.write_bytes(data)
    return cache.read_text(encoding="utf-8-sig")


def runtime_notices() -> str:
    pieces = ["SynoFind Windows - Bundled Runtime Notices\n\nThe app bundles the Python runtime and PyInstaller bootloader. The client itself has no third-party Python runtime dependencies. Upstream texts are retained below.\n"]
    pieces.append("SQLite: public domain. https://www.sqlite.org/copyright.html\n")
    python_license = Path(sys.base_prefix) / "LICENSE.txt"
    if not python_license.exists():
        python_license = Path(sys.base_prefix) / "LICENSE"
    pieces.append(f"Python {sys.version.split()[0]}\n" + python_license.read_text(encoding="utf-8-sig"))
    distribution = importlib.metadata.distribution("pyinstaller")
    copying = [distribution.locate_file(item) for item in distribution.files or [] if str(item).endswith("COPYING.txt")]
    if len(copying) != 1:
        raise ValueError("Cannot locate PyInstaller upstream license")
    pieces.append(f"PyInstaller {distribution.version}\n" + copying[0].read_text(encoding="utf-8-sig"))
    match = re.match(r"OpenSSL (3\.\d+\.\d+)", ssl.OPENSSL_VERSION)
    if not match:
        raise ValueError("Review bundled OpenSSL version before collecting its license")
    openssl_version = match.group(1)
    cache = ROOT / "work" / "desktop-build" / "licenses"
    openssl_url = f"https://raw.githubusercontent.com/openssl/openssl/openssl-{openssl_version}/LICENSE.txt"
    pieces.append(f"{ssl.OPENSSL_VERSION}\nSource: {openssl_url}\n" + upstream_text(openssl_url, cache / f"openssl-{openssl_version}.txt"))
    zlib_url = f"https://raw.githubusercontent.com/madler/zlib/v{zlib.ZLIB_VERSION}/LICENSE"
    pieces.append(f"zlib {zlib.ZLIB_VERSION}\nSource: {zlib_url}\n" + upstream_text(zlib_url, cache / f"zlib-{zlib.ZLIB_VERSION}.txt"))
    expat_version = pyexpat.EXPAT_VERSION.removeprefix("expat_")
    expat_url = f"https://raw.githubusercontent.com/libexpat/libexpat/R_{expat_version.replace('.', '_')}/expat/COPYING"
    pieces.append(f"Expat {expat_version}\nSource: {expat_url}\n" + upstream_text(expat_url, cache / f"expat-{expat_version}.txt"))
    # The standard library's _lzma module is collected by PyInstaller. Match its
    # Windows dependency to this exact CPython release's official build recipe.
    python_version = sys.version.split()[0]
    recipe_url = f"https://raw.githubusercontent.com/python/cpython/v{python_version}/PCbuild/python.props"
    recipe = upstream_text(recipe_url, cache / f"cpython-{python_version}-python.props")
    match = re.search(r"xz-([0-9.]+)", recipe)
    if not match:
        raise ValueError("Review CPython's liblzma dependency before collecting its license")
    xz_version = match.group(1)
    xz_url = f"https://raw.githubusercontent.com/python/cpython-source-deps/xz-{xz_version}/COPYING"
    pieces.append(f"liblzma from XZ Utils {xz_version}; command-line XZ tools are not bundled.\nSource: {xz_url}\n" + upstream_text(xz_url, cache / f"xz-{xz_version}.txt"))
    vendor_root = CLIENT / "synofind_dsm" / "desktop_web" / "vendor"
    if not vendor_root.is_dir():
        raise ValueError("Bundled PDF.js vendor assets are missing")
    pdf_source = (vendor_root / "pdf.mjs").read_text(encoding="utf-8")
    pdf_version = re.search(r"pdfjsVersion\s*=\s*([^\s*]+)", pdf_source)
    pieces.append("PDF.js from the official pdfjs-dist npm package; browser PDF viewer resources.\n"
                  + (f"Version: {pdf_version.group(1)}\n" if pdf_version else "")
                  + "Source: https://github.com/mozilla/pdf.js\nPackage: https://www.npmjs.com/package/pdfjs-dist\n")
    for path in sorted(vendor_root.rglob("*")):
        if path.is_file() and (path.name.upper().startswith(("LICENSE", "NOTICE")) or path.name == "PDFJS-LICENSE.txt"):
            if linked(path):
                raise ValueError("Vendor notices must not be linked")
            pieces.append(f"Bundled browser resource: {path.relative_to(vendor_root).as_posix()}\n"
                          + path.read_text(encoding="utf-8-sig"))
    return ("\n\n" + "=" * 78 + "\n\n").join(pieces).replace("\r\n", "\n")


def check_exe(data: bytes) -> None:
    if len(data) < 256 or data[:2] != b"MZ":
        raise ValueError("Expected a Windows PE executable")
    pe_offset = struct.unpack_from("<I", data, 0x3C)[0]
    if pe_offset + 94 > len(data) or data[pe_offset:pe_offset + 4] != b"PE\0\0":
        raise ValueError("Invalid PE header")
    machine = struct.unpack_from("<H", data, pe_offset + 4)[0]
    optional_magic = struct.unpack_from("<H", data, pe_offset + 24)[0]
    subsystem = struct.unpack_from("<H", data, pe_offset + 24 + 68)[0]
    if machine != 0x8664 or optional_magic != 0x20B or subsystem != 3:
        raise ValueError("Expected a Windows x64 console executable")


def smoke_test_exe(executable: Path) -> dict:
    """Exercise bundled assets and clean shutdown with empty, isolated local data."""
    build_root = ROOT / "work" / "desktop-build"
    build_root.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="smoke-", dir=build_root) as directory:
        environment = {key: value for key, value in os.environ.items()
                       if not key.startswith("SYNOLOGY_") and key != "PYTHONPATH"}
        process = subprocess.Popen(
            [str(executable.resolve()), "--no-browser", "--port", "0", "--data-dir", directory],
            cwd=directory, env=environment, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, encoding="utf-8", errors="replace",
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        lines: queue.Queue[str] = queue.Queue()

        def drain():
            assert process.stdout is not None
            for line in process.stdout:
                lines.put(line)

        reader = threading.Thread(target=drain, daemon=True)
        reader.start()
        opener = build_opener(ProxyHandler({}), HTTPCookieProcessor(http.cookiejar.CookieJar()))
        url = ""
        try:
            deadline = time.monotonic() + 40
            while time.monotonic() < deadline and not url:
                try:
                    line = lines.get(timeout=0.2)
                except queue.Empty:
                    if process.poll() is not None:
                        raise RuntimeError("Desktop executable exited before opening its local server")
                    continue
                match = re.search(r"http://127\.0\.0\.1:\d+", line)
                if match:
                    url = match.group(0)
            if not url:
                raise RuntimeError("Timed out waiting for the desktop executable")
            with opener.open(url + "/healthz", timeout=5) as response:
                health = json.load(response)
            if health != {"application": "SynoFindDesktop", "version": VERSION}:
                raise RuntimeError("Unexpected bundled desktop version")
            asset_root = CLIENT / "synofind_dsm" / "desktop_web"
            runtime_assets = [("/", "index.html"), ("/app.js", "app.js"), ("/styles.css", "styles.css")]
            for asset in sorted((asset_root / "vendor").rglob("*")):
                if asset.is_file() and asset.suffix.lower() in {".mjs", ".js", ".wasm", ".bcmap", ".pfb", ".ttf", ".otf", ".icc"}:
                    relative = asset.relative_to(asset_root).as_posix()
                    runtime_assets.append(("/" + relative, relative))
            for route, filename in runtime_assets:
                with opener.open(url + route, timeout=5) as response:
                    actual = response.read()
                expected = (asset_root / filename).read_bytes()
                if actual != expected:
                    raise RuntimeError(f"Bundled desktop resource is missing or stale: {filename}")
            with opener.open(url + "/api/session", timeout=5) as response:
                session = json.load(response)
            if session["status"]["connected"]:
                raise RuntimeError("Empty smoke-test state unexpectedly has a DSM connection")
            port = urlsplit(url).port
            second_directory = Path(directory) / "second-instance"
            second = subprocess.run(
                [str(executable.resolve()), "--no-browser", "--port", str(port), "--data-dir", str(second_directory)],
                cwd=directory, env=environment, capture_output=True, text=True, encoding="utf-8", errors="replace",
                timeout=25, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
            if second.returncode != 0 or url not in second.stdout:
                raise RuntimeError("A second desktop instance did not reuse the running app")
            request = Request(url + "/api/shutdown", data=b"{}", method="POST", headers={
                "Content-Type": "application/json", "Origin": url,
                "X-SynoFind-CSRF": session["csrf_token"],
            })
            with opener.open(request, timeout=5) as response:
                if json.load(response).get("stopped") is not True:
                    raise RuntimeError("Desktop shutdown was not acknowledged")
            if process.wait(timeout=15) != 0:
                raise RuntimeError("Desktop executable did not exit successfully")
            try:
                with socket.create_connection(("127.0.0.1", urlsplit(url).port), timeout=0.5):
                    raise RuntimeError("Local desktop port is still accepting connections after exit")
            except OSError:
                pass
            reader.join(timeout=1)
            if process.stdout is not None:
                process.stdout.close()
            # Restart the same packaged app on the same port and data directory.
            # This checks release/rebind, not access to real DSM credentials.
            process = subprocess.Popen(
                [str(executable.resolve()), "--no-browser", "--port", str(port), "--data-dir", directory],
                cwd=directory, env=environment, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                text=True, encoding="utf-8", errors="replace",
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
            reader = threading.Thread(target=drain, daemon=True)
            reader.start()
            deadline = time.monotonic() + 30
            restarted = False
            while time.monotonic() < deadline:
                if process.poll() is not None:
                    raise RuntimeError("Desktop executable exited while restarting")
                try:
                    with opener.open(url + "/healthz", timeout=1) as response:
                        restarted = json.load(response).get("application") == "SynoFindDesktop"
                    if restarted:
                        break
                except OSError:
                    pass
                time.sleep(0.1)
            if not restarted:
                raise RuntimeError("Desktop executable did not restart on its original port")
            with opener.open(url + "/", timeout=5) as response:
                response.read()
            with opener.open(url + "/api/session", timeout=5) as response:
                session = json.load(response)
            request = Request(url + "/api/shutdown", data=b"{}", method="POST", headers={
                "Content-Type": "application/json", "Origin": url,
                "X-SynoFind-CSRF": session["csrf_token"],
            })
            with opener.open(request, timeout=5) as response:
                response.read()
            if process.wait(timeout=15) != 0:
                raise RuntimeError("Restarted desktop app did not exit successfully")
            try:
                with socket.create_connection(("127.0.0.1", port), timeout=0.5):
                    raise RuntimeError("Restarted desktop port was not released")
            except OSError:
                pass
            report = {"version": VERSION, "executable_sha256": hashlib.sha256(executable.read_bytes()).hexdigest(),
                      "isolated_state": True, "bundled_assets_match_source": True,
                      "bundled_runtime_assets_checked": len(runtime_assets),
                      "shutdown_exit_code": 0, "port_released": True,
                      "restart_passed": True, "second_instance_reuses_running_app": True,
                      "nas_authentication_tested": False}
            print("Desktop executable smoke test passed: bundled assets, empty state, shutdown, restart, second instance and port release.")
            return report
        finally:
            if process.poll() is None:
                # Stop only the child process tree created by this test, including
                # PyInstaller's onefile child; never touch a user's running app.
                subprocess.run(["taskkill", "/PID", str(process.pid), "/T", "/F"],
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False)
                process.wait(timeout=10)
            if process.stdout is not None:
                reader.join(timeout=1)
                process.stdout.close()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--exe", type=Path, default=ROOT / "work/desktop-build/SynoFind.exe")
    parser.add_argument("--output-dir", type=Path, default=ROOT / "outputs")
    parser.add_argument("--source-only", action="store_true")
    parser.add_argument("--verify-exe", action="store_true", help="Run a no-browser smoke test with empty temporary state")
    args = parser.parse_args()
    sources = source_entries()
    if not args.source_only:
        binary = args.exe.read_bytes()
        check_exe(binary)
        if args.verify_exe:
            report = smoke_test_exe(args.exe)
            args.output_dir.mkdir(parents=True, exist_ok=True)
            (args.output_dir / f"SynoFind-Windows-{VERSION}-verification.json").write_text(
                json.dumps(report, indent=2) + "\n", encoding="utf-8")
        entries = {
            "SynoFind.exe": binary,
            "使用说明.txt": (CLIENT / "DESKTOP.md").read_bytes(),
            "THIRD_PARTY_NOTICES.txt": runtime_notices().encode("utf-8"),
        }
        entries["SHA256SUMS.txt"] = "".join(f"{hashlib.sha256(data).hexdigest()}  {name}\n" for name, data in sorted(entries.items())).encode("utf-8")
        write_zip(args.output_dir / f"SynoFind-Windows-{VERSION}.zip", f"SynoFind-Windows-{VERSION}", entries)
        standalone = args.output_dir / "SynoFind.exe"
        if not standalone.exists() or standalone.read_bytes() != binary:
            shutil.copyfile(args.exe, standalone)
        standalone.with_suffix(".exe.sha256").write_text(f"{hashlib.sha256(binary).hexdigest()}  SynoFind.exe\n", encoding="ascii")
        (args.output_dir / f"SynoFind-Windows-{VERSION}-README.txt").write_bytes(entries["使用说明.txt"])
        (args.output_dir / f"SynoFind-Windows-{VERSION}-NOTICES.txt").write_bytes(entries["THIRD_PARTY_NOTICES.txt"])
        sources["THIRD_PARTY_NOTICES.txt"] = entries["THIRD_PARTY_NOTICES.txt"]
    write_zip(args.output_dir / f"SynoFind-Windows-{VERSION}-source.zip", f"SynoFind-{VERSION}", sources)


if __name__ == "__main__":
    main()
