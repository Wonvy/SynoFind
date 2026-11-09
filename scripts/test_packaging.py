"""Packaging regression tests, including unsafe/wrong-target build rejection."""
import hashlib
import io
import json
from pathlib import Path
import struct
import tempfile
import unittest
from urllib.parse import urljoin

import build_spk


def elf_fixture():
    # A test-only structural ELF; never emitted as a user-facing package.
    data = bytearray(120)
    data[:6] = b"\x7fELF\x02\x01"
    struct.pack_into("<H", data, 18, 62)
    struct.pack_into("<Q", data, 32, 64)
    struct.pack_into("<H", data, 54, 56)
    struct.pack_into("<H", data, 56, 1)
    struct.pack_into("<I", data, 64, 1)
    return data


class PackageTests(unittest.TestCase):
    def test_launcher_is_independent_of_dsm_page_base(self):
        config = json.loads((build_spk.ROOT / "packaging/payload/ui/config").read_text(encoding="utf-8"))
        target = config[".url"]["com.synofind.web"]["url"]
        expected = "https://nas.example:5001/webman/3rdparty/synofind/index.html"
        for base in ("https://nas.example:5001/", "https://nas.example:5001/webman/index.cgi"):
            self.assertEqual(urljoin(base, target), expected)

    def test_wrong_platform_and_dynamic_binaries_are_rejected(self):
        with self.assertRaises(ValueError):
            build_spk.check_binary(b"MZ" + bytes(120))
        binary = elf_fixture()
        struct.pack_into("<H", binary, 18, 183)
        with self.assertRaises(ValueError):
            build_spk.check_binary(binary)
        binary = elf_fixture()
        struct.pack_into("<I", binary, 64, 3)
        with self.assertRaises(ValueError):
            build_spk.check_binary(binary)

    def test_build_is_reproducible_and_wizard_has_no_default_password(self):
        with tempfile.TemporaryDirectory() as temp:
            folder = Path(temp)
            binary = folder / "synofind"
            binary.write_bytes(elf_fixture())
            one = build_spk.create_spk(binary, "0.1.0", 1, folder / "first.spk")
            two = build_spk.create_spk(binary, "0.1.0", 1, folder / "second.spk")
            self.assertEqual(one.read_bytes(), two.read_bytes())
            outer = build_spk.read_members(one.read_bytes())
            wizard = json.loads(outer["WIZARD_UIFILES/install_uifile"][0])
            fields = [sub for page in wizard for item in page["items"] for sub in item.get("subitems", [])]
            password = next(item for item in fields if item["key"] == "wizard_password")
            self.assertNotIn("defaultValue", password)
            self.assertEqual(password["validator"]["minLength"], 12)

    def test_payload_corruption_is_detected(self):
        with tempfile.TemporaryDirectory() as temp:
            folder = Path(temp)
            binary = folder / "synofind"
            binary.write_bytes(elf_fixture())
            output = build_spk.create_spk(binary, "0.1.0", 1, folder / "test.spk")
            original = build_spk.read_members(output.read_bytes())
            damaged = {name: (content, meta.mode) for name, (content, meta) in original.items()}
            data, mode = damaged["package.tgz"]
            damaged["package.tgz"] = (data + b"corrupt", mode)
            output.write_bytes(build_spk.tar_bytes(damaged))
            with self.assertRaisesRegex(ValueError, "checksum"):
                build_spk.validate_spk(output)

    def test_path_escape_is_rejected_without_extraction(self):
        malicious = build_spk.tar_bytes({"../outside": (b"no", 0o644)})
        with self.assertRaises(ValueError):
            build_spk.read_members(malicious)


if __name__ == "__main__":
    unittest.main()
