"""Real Windows path boundary checks, without opening any external application."""
import ctypes
import os
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from synofind_dsm.client import DSMError
from synofind_dsm.local_paths import open_local_content, resolve_local_path, reveal_local_file, validate_local_profile


@unittest.skipUnless(os.name == "nt", "Windows folder mappings")
class LocalPathsTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        self.root = Path(self.folder.name)
        self.profile = {"root": "/资料", "local_root": str(self.root), "preview_mode": "web", "preview_executable": ""}
        self.file = self.root / "子目录" / "合同 & 样本.txt"
        self.file.parent.mkdir()
        self.file.write_bytes(b"synthetic local content")
        self.remote = "/资料/子目录/合同 & 样本.txt"

    def test_real_opened_handle_stays_in_mapping(self):
        validate_local_profile(self.profile)
        with open_local_content(self.profile, self.remote) as response:
            self.assertEqual(response.read(), b"synthetic local content")
            self.assertEqual(response.headers["Content-Length"], "23")
        self.assertTrue(response.closed)

    def test_handle_outside_mapping_is_rejected_before_read(self):
        with patch("synofind_dsm.local_paths._opened_path", return_value=self.root.parent / "outside.txt"):
            with self.assertRaises(DSMError):
                with open_local_content(self.profile, self.remote):
                    self.fail("an escaped file must not be exposed")

    def test_remote_boundary_traversal_and_windows_aliases_are_rejected(self):
        for remote in ("/其他/a.txt", "/资料/../outside.txt", "/资料/a.txt:secret", "/资料/a.", "/资料/CON", "/资料/子目录\\a"):
            with self.subTest(remote=remote), self.assertRaises(DSMError):
                resolve_local_path(self.profile, remote)

    def test_missing_map_and_relative_map_are_rejected(self):
        for value in ("", "W:relative", "relative", "\\\\.\\pipe\\something"):
            with self.subTest(value=value), self.assertRaises(DSMError):
                resolve_local_path(dict(self.profile, local_root=value), self.remote)

    def test_mapping_read_only_validation_does_not_require_native_tool(self):
        validate_local_profile(dict(self.profile, local_root="", preview_mode="web"))
        with self.assertRaises(DSMError):
            validate_local_profile(dict(self.profile, local_root="", preview_mode="quicklook"))

    def shell_mock(self, *, initialize=0, parse=0, opened=0, pidl=0x1234):
        calls = []

        def initialize_com(*args):
            calls.append("initialize")
            return initialize

        def parse_name(name, bind_context, output, requested, attributes):
            calls.append("parse")
            ctypes.cast(output, ctypes.POINTER(ctypes.c_void_p))[0] = pidl
            return parse

        def open_selection(*args):
            calls.append("open")
            return opened

        ole = SimpleNamespace(
            CoInitializeEx=Mock(side_effect=initialize_com),
            CoTaskMemFree=Mock(side_effect=lambda *_: calls.append("free")),
            CoUninitialize=Mock(side_effect=lambda: calls.append("uninitialize")),
        )
        shell = SimpleNamespace(SHParseDisplayName=Mock(side_effect=parse_name),
                                SHOpenFolderAndSelectItems=Mock(side_effect=open_selection))
        loader = patch("synofind_dsm.local_paths.ctypes.WinDLL",
                       side_effect=lambda name, **_: {"ole32": ole, "shell32": shell}[name])
        return loader, ole, shell, calls

    def test_reveal_passes_literal_unicode_path_to_shell_api_and_balances_resources(self):
        loader, ole, shell, calls = self.shell_mock()
        with loader:
            reveal_local_file(self.profile, self.remote)
        ole.CoInitializeEx.assert_called_once_with(None, 0x2)
        self.assertEqual(shell.SHParseDisplayName.call_args.args[0], str(self.file))
        arguments = shell.SHOpenFolderAndSelectItems.call_args.args
        self.assertEqual(arguments[0].value, 0x1234)
        self.assertEqual(arguments[1:], (0, None, 0))
        self.assertEqual(ole.CoTaskMemFree.call_args.args[0].value, 0x1234)
        self.assertEqual(calls, ["initialize", "parse", "open", "free", "uninitialize"])

    def test_reveal_directory_selects_directory_in_its_parent(self):
        loader, _, shell, _ = self.shell_mock()
        with loader:
            reveal_local_file(self.profile, "/资料/子目录")
        self.assertEqual(shell.SHParseDisplayName.call_args.args[0], str(self.file.parent))
        self.assertEqual(shell.SHOpenFolderAndSelectItems.call_args.args[1:], (0, None, 0))

    def test_reveal_already_initialized_com_still_balances_reference(self):
        loader, ole, _, calls = self.shell_mock(initialize=1)
        with loader:
            reveal_local_file(self.profile, self.remote)
        ole.CoUninitialize.assert_called_once_with()
        self.assertEqual(calls[-2:], ["free", "uninitialize"])

    def test_reveal_com_failure_does_not_parse_or_uninitialize(self):
        loader, ole, shell, calls = self.shell_mock(initialize=0x80010106)
        with loader, self.assertRaises(DSMError) as caught:
            reveal_local_file(self.profile, self.remote)
        self.assertEqual(calls, ["initialize"])
        shell.SHParseDisplayName.assert_not_called()
        ole.CoTaskMemFree.assert_not_called()
        ole.CoUninitialize.assert_not_called()
        self.assertNotIn(str(self.file), str(caught.exception))

    def test_reveal_parse_failure_frees_any_returned_pidl_and_uninitializes(self):
        loader, ole, shell, calls = self.shell_mock(parse=0x80004005)
        with loader, self.assertRaises(DSMError) as caught:
            reveal_local_file(self.profile, self.remote)
        self.assertEqual(calls, ["initialize", "parse", "free", "uninitialize"])
        shell.SHOpenFolderAndSelectItems.assert_not_called()
        ole.CoTaskMemFree.assert_called_once()
        self.assertNotIn(str(self.file), str(caught.exception))

    def test_reveal_null_pidl_success_is_rejected(self):
        loader, ole, shell, calls = self.shell_mock(pidl=None)
        with loader, self.assertRaises(DSMError):
            reveal_local_file(self.profile, self.remote)
        self.assertEqual(calls, ["initialize", "parse", "uninitialize"])
        shell.SHOpenFolderAndSelectItems.assert_not_called()
        ole.CoTaskMemFree.assert_not_called()

    def test_reveal_open_hresult_failure_is_visible_and_resources_are_freed(self):
        loader, _, _, calls = self.shell_mock(opened=0x80004005)
        with loader, self.assertRaises(DSMError) as caught:
            reveal_local_file(self.profile, self.remote)
        self.assertEqual(calls, ["initialize", "parse", "open", "free", "uninitialize"])
        self.assertNotIn(str(self.file), str(caught.exception))

    def test_reveal_checks_mapping_boundary_before_loading_native_api(self):
        with patch("synofind_dsm.local_paths.ctypes.WinDLL") as native:
            with self.assertRaises(DSMError):
                reveal_local_file(self.profile, "/其他/secret.txt")
        native.assert_not_called()

    def test_reveal_native_loading_errors_do_not_expose_path(self):
        with patch("synofind_dsm.local_paths.ctypes.WinDLL", side_effect=OSError(str(self.file))):
            with self.assertRaises(DSMError) as caught:
                reveal_local_file(self.profile, self.remote)
        self.assertNotIn(str(self.file), str(caught.exception))


if __name__ == "__main__":
    unittest.main()
