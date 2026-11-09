"""Native preview invocation uses fixed arguments and bounded, verified IPC."""
import ctypes
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from synofind_dsm import native_preview as native


@unittest.skipUnless(os.name == "nt", "Windows preview integration")
class NativePreviewTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="synofind-preview-")
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.file = self.base / "合同 & $(test); café.pdf"
        self.file.write_bytes(b"not executed")
        self.quicklook = self.base / "QuickLook.exe"
        self.seer = self.base / "Seer.exe"
        self.quicklook.write_bytes(b"test fixture")
        self.seer.write_bytes(b"test fixture")
        self.popen = self.enterContext(patch.object(native.subprocess, "Popen"))

    def test_quicklook_special_characters_are_one_argument_without_shell(self):
        native.launch_preview(self.file, "quicklook", str(self.quicklook))
        self.popen.assert_called_once_with(
            [str(self.quicklook.resolve()), str(self.file)], shell=False,
            close_fds=True, cwd=str(self.base.resolve()))
        self.assertEqual(self.file.read_bytes(), b"not executed")

    def test_seer_first_instance_fixed_cli(self):
        with patch.object(native, "_send_to_running_seer", return_value=False) as send:
            native.launch_preview(self.file, "seer", str(self.seer))
        send.assert_called_once_with(self.file, self.seer.resolve())
        self.popen.assert_called_once_with([str(self.seer.resolve()), str(self.file)],
                                          shell=False, close_fds=True, cwd=str(self.base.resolve()))

    def test_seer_existing_instance_does_not_launch_another_process(self):
        with patch.object(native, "_send_to_running_seer", return_value=True):
            native.launch_preview(self.file, "seer", str(self.seer))
        self.popen.assert_not_called()

    def test_unknown_mode_and_wrong_program_are_rejected(self):
        for mode, program in (("custom", self.quicklook), ("web", self.quicklook),
                              ("quicklook", self.seer), ("seer", self.quicklook)):
            with self.subTest(mode=mode, program=program.name), self.assertRaises(native.NativePreviewError):
                native.launch_preview(self.file, mode, str(program))
        self.popen.assert_not_called()

    def test_relative_or_missing_program_is_rejected(self):
        for program in ("QuickLook.exe", str(self.base / "gone" / "QuickLook.exe"),
                        str(self.quicklook) + " --custom"):
            with self.subTest(program=program), self.assertRaises(native.NativePreviewError):
                native.launch_preview(self.file, "quicklook", program)
        self.popen.assert_not_called()

    def test_program_must_be_a_regular_file(self):
        self.quicklook.unlink()
        self.quicklook.mkdir()
        with self.assertRaises(native.NativePreviewError):
            native.launch_preview(self.file, "quicklook", str(self.quicklook))
        self.popen.assert_not_called()

    def test_relative_missing_or_control_character_file_is_rejected(self):
        for value in (Path("test.pdf"), self.base / "missing.pdf", self.base / "bad\nname.pdf"):
            with self.subTest(value=str(value)), self.assertRaises(native.NativePreviewError):
                native.launch_preview(value, "quicklook", str(self.quicklook))
        self.popen.assert_not_called()

    def test_no_path_or_current_directory_discovery(self):
        with patch.dict(os.environ, {"PATH": str(self.base)}, clear=True):
            with self.assertRaisesRegex(native.NativePreviewError, "未找到QuickLook"):
                native.launch_preview(self.file, "quicklook")
        self.popen.assert_not_called()

    def test_quicklook_local_programs_discovery(self):
        exe = self.base / "Programs" / "QuickLook" / "QuickLook.exe"
        exe.parent.mkdir(parents=True)
        exe.write_bytes(b"test fixture")
        with patch.dict(os.environ, {"LOCALAPPDATA": str(self.base)}, clear=True):
            native.launch_preview(self.file, "quicklook")
        self.assertEqual(self.popen.call_args.args[0][0], str(exe.resolve()))

    def test_launch_failure_is_fixed_safe_message(self):
        self.popen.side_effect = OSError("sensitive filename")
        with self.assertRaisesRegex(native.NativePreviewError, "无法启动 QuickLook") as raised:
            native.launch_preview(self.file, "quicklook", str(self.quicklook))
        self.assertNotIn("sensitive", str(raised.exception))

    def _win32(self, *, exists=True, same_program=True, sent=True, query=True):
        user = Mock()
        kernel = Mock()
        user.FindWindowW.return_value = 123 if exists else 0
        def get_pid(window, result):
            result._obj.value = 456
            return 789
        user.GetWindowThreadProcessId.side_effect = get_pid
        kernel.OpenProcess.return_value = 321
        def query_image(process, flags, buffer, size):
            buffer.value = str(self.seer if same_program else self.quicklook)
            return query
        kernel.QueryFullProcessImageNameW.side_effect = query_image
        user.SendMessageTimeoutW.return_value = 1 if sent else 0
        return user, kernel

    def test_existing_seer_ipc_has_expected_payload_and_timeout(self):
        user, kernel = self._win32()
        def inspect(window, message, sender, pointer, flags, timeout, output):
            from ctypes import wintypes
            class CopyData(ctypes.Structure):
                _fields_ = [("dwData", ctypes.c_size_t), ("cbData", wintypes.DWORD),
                            ("lpData", ctypes.c_void_p)]
            data = CopyData.from_address(pointer)
            self.assertEqual((window, message, sender, flags, timeout), (123, 0x004A, 0, 3, 2000))
            self.assertEqual(data.dwData, 5000)
            self.assertEqual(ctypes.wstring_at(data.lpData), str(self.file))
            self.assertEqual(data.cbData, ctypes.sizeof(ctypes.create_unicode_buffer(str(self.file))))
            return 1
        user.SendMessageTimeoutW.side_effect = inspect
        with patch.object(native.ctypes, "WinDLL", side_effect=[user, kernel]):
            self.assertTrue(native._send_to_running_seer(self.file, self.seer.resolve()))
        kernel.CloseHandle.assert_called_once_with(321)

    def test_absent_seer_window_uses_cli_path(self):
        user, kernel = self._win32(exists=False)
        with patch.object(native.ctypes, "WinDLL", side_effect=[user, kernel]):
            self.assertFalse(native._send_to_running_seer(self.file, self.seer.resolve()))
        user.SendMessageTimeoutW.assert_not_called()
        kernel.OpenProcess.assert_not_called()

    def test_seer_window_of_other_executable_never_receives_filename(self):
        user, kernel = self._win32(same_program=False)
        with patch.object(native.ctypes, "WinDLL", side_effect=[user, kernel]):
            with self.assertRaisesRegex(native.NativePreviewError, "程序不同"):
                native._send_to_running_seer(self.file, self.seer.resolve())
        user.SendMessageTimeoutW.assert_not_called()
        kernel.CloseHandle.assert_called_once_with(321)

    def test_seer_unverified_process_never_receives_filename(self):
        for open_ok, query_ok in ((False, True), (True, False)):
            user, kernel = self._win32(query=query_ok)
            if not open_ok:
                kernel.OpenProcess.return_value = None
            with self.subTest(open_ok=open_ok, query_ok=query_ok):
                with patch.object(native.ctypes, "WinDLL", side_effect=[user, kernel]):
                    with self.assertRaises(native.NativePreviewError):
                        native._send_to_running_seer(self.file, self.seer.resolve())
                user.SendMessageTimeoutW.assert_not_called()

    def test_seer_ipc_timeout_does_not_fallback_to_unverified_launch(self):
        user, kernel = self._win32(sent=False)
        with patch.object(native.ctypes, "WinDLL", side_effect=[user, kernel]):
            with self.assertRaisesRegex(native.NativePreviewError, "未响应"):
                native.launch_preview(self.file, "seer", str(self.seer))
        self.popen.assert_not_called()


if __name__ == "__main__":
    unittest.main()
