#!/usr/bin/env python3
"""Build a reproducible DSM 7 SPK using Python's standard library only."""
from __future__ import annotations

import argparse
import gzip
import hashlib
import io
import json
from pathlib import Path, PurePosixPath
import re
import struct
import tarfile
import zlib

ROOT = Path(__file__).resolve().parents[1]
REQUIRED_SCRIPTS = {
    "preinst", "postinst", "preuninst", "postuninst",
    "preupgrade", "postupgrade", "start-stop-status",
}


def png_icon(size: int) -> bytes:
    """Original folder/search mark; generated without external image assets."""
    rows = bytearray()
    for y in range(size):
        rows.append(0)
        for x in range(size):
            px, py = (x + .5) / size, (y + .5) / size
            # Navy tile, mint folder, cream magnifier.
            r, g, b, a = (19, 40, 67, 255)
            if (.19 < px < .79 and .35 < py < .73) or (.19 < px < .47 and .27 < py < .42):
                r, g, b = 71, 201, 175
            d = ((px - .58) ** 2 + (py - .48) ** 2) ** .5
            if .105 < d < .158 or (.66 < px < .83 and abs((py - .58) - (px - .67)) < .029):
                r, g, b = 249, 249, 241
            corner = min(px, 1 - px), min(py, 1 - py)
            if corner[0] < .12 and corner[1] < .12 and (corner[0] - .12) ** 2 + (corner[1] - .12) ** 2 > .12 ** 2:
                a = 0
            rows.extend((r, g, b, a))

    def chunk(kind: bytes, data: bytes) -> bytes:
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data))

    return b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", size, size, 8, 6, 0, 0, 0)) + chunk(b"IDAT", zlib.compress(bytes(rows), 9)) + chunk(b"IEND", b"")


def check_binary(binary: bytes) -> None:
    if len(binary) < 64 or binary[:6] != b"\x7fELF\x02\x01":
        raise ValueError("Expected a Linux ELF64 little-endian binary, not a Windows executable.")
    if struct.unpack_from("<H", binary, 18)[0] != 62:
        raise ValueError("Expected the x86-64/amd64 target for DS918+.")
    offset, size, count = struct.unpack_from("<Q", binary, 32)[0], struct.unpack_from("<H", binary, 54)[0], struct.unpack_from("<H", binary, 56)[0]
    if size < 56 or offset + size * count > len(binary):
        raise ValueError("Invalid ELF program header table.")
    for i in range(count):
        if struct.unpack_from("<I", binary, offset + size * i)[0] == 3:
            raise ValueError("Binary has a dynamic interpreter; build with CGO_ENABLED=0.")


def tar_bytes(files: dict[str, tuple[bytes, int]]) -> bytes:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w", format=tarfile.USTAR_FORMAT) as archive:
        for name, (content, mode) in sorted(files.items()):
            member = tarfile.TarInfo(name)
            member.size, member.mode, member.mtime = len(content), mode, 0
            member.uid, member.gid, member.uname, member.gname = 0, 0, "root", "root"
            archive.addfile(member, io.BytesIO(content))
    return buf.getvalue()


def text_bytes(path: Path) -> bytes:
    return path.read_text(encoding="utf-8-sig").replace("\r\n", "\n").encode("utf-8")


def create_spk(binary_path: Path, version: str, build: int, output: Path) -> Path:
    if not re.fullmatch(r"\d+\.\d+\.\d+", version) or build < 1:
        raise ValueError("Version must be X.Y.Z and build must be a positive integer.")
    binary = binary_path.read_bytes()
    check_binary(binary)
    package_dir = ROOT / "packaging"
    payload: dict[str, tuple[bytes, int]] = {"bin/synofind": (binary, 0o755)}
    for path in (package_dir / "payload").rglob("*"):
        if path.is_file():
            relative = path.relative_to(package_dir / "payload").as_posix()
            payload[relative] = (text_bytes(path), 0o755 if relative.startswith("bin/") else 0o644)
    for size in (16, 24, 32, 48, 64, 72, 256):
        payload[f"ui/images/icon_{size}.png"] = (png_icon(size), 0o644)
    gz = io.BytesIO()
    with gzip.GzipFile(fileobj=gz, mode="wb", filename="", mtime=0, compresslevel=9) as stream:
        stream.write(tar_bytes(payload))
    package_tgz = gz.getvalue()
    info = text_bytes(package_dir / "INFO.in").decode().replace("@VERSION@", version).replace("@BUILD@", f"{build:04d}").replace("@CHECKSUM@", hashlib.md5(package_tgz).hexdigest())
    outer: dict[str, tuple[bytes, int]] = {
        "INFO": (info.encode(), 0o644),
        "package.tgz": (package_tgz, 0o644),
        "PACKAGE_ICON.PNG": (png_icon(64), 0o644),
        "PACKAGE_ICON_256.PNG": (png_icon(256), 0o644),
    }
    for directory in ("scripts", "conf", "WIZARD_UIFILES"):
        for path in (package_dir / directory).rglob("*"):
            if path.is_file():
                outer[path.relative_to(package_dir).as_posix()] = (text_bytes(path), 0o755 if directory == "scripts" else 0o644)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_bytes(tar_bytes(outer))
    validation = validate_spk(output)
    checksum = hashlib.sha256(output.read_bytes()).hexdigest()
    output.with_suffix(output.suffix + ".sha256").write_text(f"{checksum}  {output.name}\n", encoding="ascii")
    output.with_suffix(output.suffix + ".manifest.json").write_text(json.dumps(validation, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return output


def read_members(data: bytes, mode: str = "r:") -> dict[str, tuple[bytes, tarfile.TarInfo]]:
    result = {}
    with tarfile.open(fileobj=io.BytesIO(data), mode=mode) as archive:
        for entry in archive.getmembers():
            if not entry.isfile() or entry.name.startswith("/") or ".." in PurePosixPath(entry.name).parts:
                raise ValueError(f"Unsafe or unsupported archive entry: {entry.name}")
            if entry.name in result:
                raise ValueError(f"Duplicate archive entry: {entry.name}")
            stream = archive.extractfile(entry)
            assert stream is not None
            result[entry.name] = (stream.read(), entry)
    return result


def validate_spk(path: Path) -> dict:
    files = read_members(path.read_bytes())
    for key in ("INFO", "package.tgz", "conf/privilege", "conf/resource", "PACKAGE_ICON.PNG", "PACKAGE_ICON_256.PNG", "WIZARD_UIFILES/install_uifile"):
        if key not in files:
            raise ValueError(f"Missing required member {key}")
    info = dict(re.findall(r'^([a-z_]+)="(.*)"$', files["INFO"][0].decode(), re.M))
    if info.get("package") != "synofind" or info.get("arch") != "apollolake" or info.get("os_min_ver") != "7.1-42962":
        raise ValueError("Incorrect DSM package identity or target.")
    if hashlib.md5(files["package.tgz"][0]).hexdigest() != info.get("checksum"):
        raise ValueError("Payload checksum does not match INFO.")
    privilege = json.loads(files["conf/privilege"][0])
    if privilege.get("defaults", {}).get("run-as") != "package" or privilege.get("username") != "synofind":
        raise ValueError("Package must run as the dedicated non-root account.")
    resource = json.loads(files["conf/resource"][0])
    if "systemd-user-unit" not in resource:
        raise ValueError("The user service worker must be registered.")
    for name in REQUIRED_SCRIPTS:
        data, meta = files[f"scripts/{name}"]
        if meta.mode != 0o755 or not data.startswith(b"#!/bin/sh\n") or b"\r" in data or b"\xef\xbb\xbf" in data:
            raise ValueError(f"Invalid executable mode/encoding for {name}")
    wizard = json.loads(files["WIZARD_UIFILES/install_uifile"][0])
    subitems = [entry for page in wizard for item in page["items"] for entry in item.get("subitems", [])]
    if not {"wizard_password", "wizard_port"} <= {i["key"] for i in subitems}:
        raise ValueError("Initial setup wizard must collect password and port.")
    payload = read_members(files["package.tgz"][0], "r:gz")
    if "THIRD_PARTY_NOTICES.txt" not in payload or not payload["THIRD_PARTY_NOTICES.txt"][0].startswith(b"SynoFind - Third-Party Software Notices"):
        raise ValueError("Missing third-party redistribution notices.")
    binary, binary_meta = payload["bin/synofind"]
    check_binary(binary)
    if binary_meta.mode != 0o755 or payload["bin/run-service"][1].mode != 0o755:
        raise ValueError("Runtime programs must be executable.")
    config = json.loads(payload["ui/config"][0])
    if config[".url"][info["dsmappname"]]["url"] != "/webman/3rdparty/synofind/index.html":
        raise ValueError("DSM open entry must use the port-aware launcher.")
    for name, (content, _) in files.items():
        if name.startswith(("scripts/", "conf/", "WIZARD_UIFILES/")) or name == "INFO":
            if b"\r\n" in content or content.startswith(b"\xef\xbb\xbf"):
                raise ValueError(f"DSM metadata must be UTF-8 without BOM and LF: {name}")
    return {
        "package": info["package"], "version": info["version"], "architecture": info["arch"],
        "minimum_dsm": info["os_min_ver"], "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
        "bytes": path.stat().st_size, "binary_bytes": len(binary),
        "outer_files": sorted(files), "payload_files": sorted(payload),
        "validation": "Archive structure, checksum, ELF architecture, non-root metadata, executable permissions, wizard and notice inclusion verified; installation on DSM remains unverified.",
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--binary", type=Path, default=ROOT / "work/build/synofind")
    parser.add_argument("--version", default="0.1.0")
    parser.add_argument("--build", type=int, default=1)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--validate", type=Path)
    args = parser.parse_args()
    if args.validate:
        print(json.dumps(validate_spk(args.validate), ensure_ascii=False, indent=2))
    else:
        output = args.output or ROOT / "outputs" / f"SynoFind-apollolake-{args.version}.spk"
        print(create_spk(args.binary, args.version, args.build, output))


if __name__ == "__main__":
    main()
