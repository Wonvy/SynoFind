"""Fixed, shell-free handoff to separately installed Windows preview tools.

Official integration references (no third-party program is bundled):
https://github.com/QL-Win/QuickLook/blob/master/QuickLook/App.xaml.cs
https://1218.io/seer/release-note/ (0.4.1: Seer.exe <file>)
https://1218.io/docs/seer/app-compatibility.html (existing Seer instance IPC)
"""
from __future__ import annotations

import ctypes
import os
from pathlib import Path
import subprocess

from .client import DSMError


class NativePreviewError(DSMError):
    """An actionable message that does not expose a local filename."""


_TOOLS = {"quicklook": "QuickLook", "seer": "Seer"}


def _validate_executable(value: str | Path, name: str) -> Path:
    candidate = Path(value)
    expected = (name + ".exe").lower()
    if (not candidate.is_absolute() or candidate.name.lower() != expected
            or any(ord(c) < 32 for c in str(candidate))):
        raise NativePreviewError(f"请指定 {name}.exe 的完整程序路径。")
    try:
        actual = candidate.resolve(strict=True)
        if not actual.is_file() or actual.name.lower() != expected:
            raise OSError()
        return actual
    except (OSError, ValueError, RuntimeError):
        raise NativePreviewError(f"无法读取 {name}.exe，请检查程序路径。") from None


def _find_executable(name: str, configured: str) -> Path:
    if configured:
        return _validate_executable(configured, name)
    # Narrow, documented desktop-install locations only: never PATH/current
    # directory, recursive disk scans, or a WindowsApps execution alias.
    locations = []
    local = os.environ.get("LOCALAPPDATA")
    if local and name == "QuickLook":
        locations.append(Path(local) / "Programs" / name / (name + ".exe"))
    for variable in ("ProgramW6432", "ProgramFiles", "ProgramFiles(x86)"):
        base = os.environ.get(variable)
        if base:
            locations.append(Path(base) / name / (name + ".exe"))
    for candidate in locations:
        try:
            return _validate_executable(candidate, name)
        except NativePreviewError:
            continue
    raise NativePreviewError(f"未找到{name}，请安装并在设置中指定程序。")


def _send_to_running_seer(path: Path, executable: Path) -> bool:
    """Use Seer's documented 5000 WM_COPYDATA command for an existing window.

    Verify that the window belongs to the selected executable before sending a
    filename. A bounded SendMessageTimeout avoids hanging our request thread.
    """
    from ctypes import wintypes

    user32 = ctypes.WinDLL("user32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    find_window = user32.FindWindowW
    find_window.argtypes = (wintypes.LPCWSTR, wintypes.LPCWSTR)
    find_window.restype = wintypes.HWND
    window = find_window("SeerWindowClass", None)
    if not window:
        return False

    get_pid = user32.GetWindowThreadProcessId
    get_pid.argtypes = (wintypes.HWND, ctypes.POINTER(wintypes.DWORD))
    get_pid.restype = wintypes.DWORD
    open_process = kernel32.OpenProcess
    open_process.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
    open_process.restype = wintypes.HANDLE
    query_image = kernel32.QueryFullProcessImageNameW
    query_image.argtypes = (wintypes.HANDLE, wintypes.DWORD, wintypes.LPWSTR,
                           ctypes.POINTER(wintypes.DWORD))
    query_image.restype = wintypes.BOOL
    close_handle = kernel32.CloseHandle
    close_handle.argtypes = (wintypes.HANDLE,)
    close_handle.restype = wintypes.BOOL
    process_id = wintypes.DWORD()
    get_pid(window, ctypes.byref(process_id))
    process = open_process(0x1000, False, process_id.value) if process_id.value else None
    if not process:
        raise NativePreviewError("无法确认已运行的 Seer，请以当前 Windows 用户重新打开预览工具。")
    try:
        image_path = ctypes.create_unicode_buffer(32768)
        length = wintypes.DWORD(len(image_path))
        if not query_image(process, 0, image_path, ctypes.byref(length)):
            raise NativePreviewError("无法确认已运行的 Seer 程序路径。")
        try:
            same_program = Path(image_path.value).resolve(strict=True) == executable
        except (OSError, ValueError, RuntimeError):
            same_program = False
        if not same_program:
            raise NativePreviewError("已运行的 Seer 与设置中的程序不同，请选择正在使用的 Seer.exe。")
    finally:
        close_handle(process)

    class CopyData(ctypes.Structure):
        _fields_ = [("dwData", ctypes.c_size_t), ("cbData", wintypes.DWORD),
                    ("lpData", ctypes.c_void_p)]

    filename = ctypes.create_unicode_buffer(str(path))
    payload = CopyData(5000, ctypes.sizeof(filename), ctypes.cast(filename, ctypes.c_void_p))
    send = user32.SendMessageTimeoutW
    send.argtypes = (wintypes.HWND, wintypes.UINT, ctypes.c_size_t, ctypes.c_ssize_t,
                    wintypes.UINT, wintypes.UINT, ctypes.POINTER(ctypes.c_size_t))
    send.restype = ctypes.c_ssize_t
    result = ctypes.c_size_t()
    if not send(window, 0x004A, 0, ctypes.addressof(payload), 0x0003, 2000, ctypes.byref(result)):
        raise NativePreviewError("Seer 未响应预览请求，请确认它与 SynoFind 使用相同的 Windows 用户权限。")
    return True


def launch_preview(path: Path, mode: str, executable: str = "") -> None:
    """Preview an already authorized local file; never execute a command template."""
    if os.name != "nt":
        raise NativePreviewError("本机预览工具仅支持 Windows。")
    name = _TOOLS.get(mode)
    if name is None:
        raise NativePreviewError("请选择 QuickLook 或 Seer 预览。")
    path = Path(path)
    if not path.is_absolute() or any(ord(c) < 32 for c in str(path)):
        raise NativePreviewError("预览文件必须使用完整的本地路径。")
    if not path.is_file():
        raise NativePreviewError("本地映射中找不到此文件，请检查盘符连接。")
    program = _find_executable(name, executable)
    if mode == "seer" and _send_to_running_seer(path, program):
        return
    # Both programs document a single positional filename. QuickLook itself
    # forwards this argument to its existing instance via its named pipe.
    try:
        subprocess.Popen([str(program), str(path)], shell=False, close_fds=True,
                         cwd=str(program.parent))
    except OSError:
        raise NativePreviewError(f"无法启动 {name}，请检查安装是否完整。") from None
