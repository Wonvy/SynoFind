"""Read-only Windows folder mappings scoped to the selected NAS share."""
from __future__ import annotations

from contextlib import contextmanager
import ctypes
import ntpath
import os
from pathlib import Path, PurePosixPath, PureWindowsPath
import stat

from .client import DSMError, _remote_path


class LocalPathError(DSMError):
    """A fixed local validation message with no filenames or credentials."""


def _mapping_root(value: str) -> Path:
    if os.name != "nt":
        raise LocalPathError("本地文件夹映射仅支持 Windows。")
    if not isinstance(value, str) or not value or any(ord(c) < 32 for c in value):
        raise LocalPathError("请填写本地映射路径，例如 W:\\。")
    normalized = value.replace("/", "\\")
    drive, tail = ntpath.splitdrive(normalized)
    if drive.startswith("\\\\") and not tail:
        tail = "\\"
        normalized += "\\"
    if (normalized.startswith(("\\\\?\\", "\\\\.\\")) or not drive or not tail.startswith("\\")
            or any(part in (".", "..") for part in tail.split("\\"))
            or ":" in tail):
        raise LocalPathError("本地映射必须是完整盘符路径或 Windows 共享路径。")
    return Path(ntpath.normpath(normalized))


def validate_local_profile(profile: dict[str, str]) -> None:
    mode = profile.get("preview_mode", "web")
    if mode not in {"web", "quicklook", "seer"}:
        raise LocalPathError("请选择网页、QuickLook 或 Seer 预览。")
    value = profile.get("local_root", "")
    if not value:
        if mode != "web":
            raise LocalPathError("使用本机预览工具前，请先设置共享文件夹对应的本地路径。")
    else:
        root = _mapping_root(value)
        try:
            if not root.is_dir():
                raise OSError()
            # Check read access without walking the user's tree.
            with os.scandir(root) as entries:
                next(entries, None)
        except OSError:
            raise LocalPathError("无法读取本地映射文件夹，请确认盘符已连接且路径正确。") from None
    executable = profile.get("preview_executable", "")
    if executable:
        expected = {"quicklook": "quicklook.exe", "seer": "seer.exe"}.get(mode)
        if not expected or Path(executable).name.lower() != expected or not Path(executable).is_absolute() or not Path(executable).is_file():
            raise LocalPathError("请指定所选预览工具的完整程序路径。")


def _within(path: Path, root: Path) -> bool:
    try:
        return os.path.normcase(os.path.commonpath((str(path), str(root)))) == os.path.normcase(str(root))
    except ValueError:
        return False


def resolve_local_path(profile: dict[str, str], remote_path: str) -> tuple[Path, Path]:
    """Return the user-facing mapped path and resolved mapping boundary."""
    selected = _remote_path(profile.get("root", ""))
    remote = _remote_path(remote_path)
    if selected == "/" or not (remote == selected or remote.startswith(selected + "/")):
        raise LocalPathError("此文件不在当前共享文件夹范围内。")
    relative = PurePosixPath(remote).relative_to(PurePosixPath(selected))
    for part in relative.parts:
        if (":" in part or part.endswith((".", " ")) or PureWindowsPath(part).is_reserved()
                or any(c in part for c in '<>"|?*')):
            raise LocalPathError("此文件名称无法通过 Windows 映射路径访问。")
    root = _mapping_root(profile.get("local_root", ""))
    candidate = root.joinpath(*relative.parts)
    try:
        boundary = root.resolve(strict=True)
        actual = candidate.resolve(strict=True)
        if not boundary.is_dir() or not _within(actual, boundary):
            raise LocalPathError("本地映射中的链接指向所选文件夹以外，已拒绝访问。")
        if not actual.is_file() and not actual.is_dir():
            raise LocalPathError("本地映射路径不是普通文件或文件夹。")
        return candidate, boundary
    except OSError:
        raise LocalPathError("本地映射中找不到此文件，请检查盘符连接和映射位置。") from None


def _opened_path(file) -> Path:
    # Validate the actual opened handle too, closing the link-swap race between
    # resolving a path and reading it. No file content is read before this check.
    import msvcrt
    from ctypes import wintypes
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    function = kernel.GetFinalPathNameByHandleW
    function.argtypes = (wintypes.HANDLE, wintypes.LPWSTR, wintypes.DWORD, wintypes.DWORD)
    function.restype = wintypes.DWORD
    handle = msvcrt.get_osfhandle(file.fileno())
    buffer = ctypes.create_unicode_buffer(32768)
    length = function(handle, buffer, len(buffer), 0)
    if not length or length >= len(buffer):
        raise LocalPathError("无法确认本地文件的实际位置。")
    value = buffer.value
    if value.startswith("\\\\?\\UNC\\"):
        value = "\\\\" + value[8:]
    elif value.startswith("\\\\?\\"):
        value = value[4:]
    return Path(value)


@contextmanager
def open_local_content(profile: dict[str, str], remote_path: str):
    candidate, boundary = resolve_local_path(profile, remote_path)
    try:
        with candidate.open("rb") as source:
            info = os.fstat(source.fileno())
            if not stat.S_ISREG(info.st_mode) or not _within(_opened_path(source), boundary):
                raise LocalPathError("本地文件实际位置超出映射范围，已拒绝访问。")
            source.headers = {"Content-Length": str(info.st_size)}
            yield source
    except OSError:
        raise LocalPathError("无法读取本地文件，请检查映射连接和文件权限。") from None


def _select_shell_item(candidate: Path) -> None:
    """Open the parent and select one filesystem item through the Shell API.

    No process command line is assembled and the selected file is never run.
    This runs on the HTTP worker thread, including that thread's COM lifetime.
    """
    from ctypes import wintypes

    try:
        ole32 = ctypes.WinDLL("ole32", use_last_error=True)
        shell32 = ctypes.WinDLL("shell32", use_last_error=True)
        ole32.CoInitializeEx.argtypes = (ctypes.c_void_p, wintypes.DWORD)
        ole32.CoInitializeEx.restype = ctypes.c_int32
        ole32.CoUninitialize.argtypes = ()
        ole32.CoUninitialize.restype = None
        ole32.CoTaskMemFree.argtypes = (ctypes.c_void_p,)
        ole32.CoTaskMemFree.restype = None
        shell32.SHParseDisplayName.argtypes = (
            wintypes.LPCWSTR, ctypes.c_void_p, ctypes.POINTER(ctypes.c_void_p),
            wintypes.ULONG, ctypes.POINTER(wintypes.ULONG),
        )
        shell32.SHParseDisplayName.restype = ctypes.c_int32
        shell32.SHOpenFolderAndSelectItems.argtypes = (
            ctypes.c_void_p, wintypes.UINT, ctypes.POINTER(ctypes.c_void_p), wintypes.DWORD,
        )
        shell32.SHOpenFolderAndSelectItems.restype = ctypes.c_int32

        pidl = ctypes.c_void_p()
        initialized = False
        try:
            # COINIT_APARTMENTTHREADED. S_OK and S_FALSE both acquire a COM
            # initialization reference and must be balanced on this thread.
            result = ole32.CoInitializeEx(None, 0x2)
            if ctypes.c_int32(result).value < 0:
                raise LocalPathError("无法初始化 Windows 文件定位接口，请重启 SynoFind 后重试。")
            initialized = True
            result = shell32.SHParseDisplayName(str(candidate), None, ctypes.byref(pidl), 0, None)
            if ctypes.c_int32(result).value < 0 or not pidl.value:
                raise LocalPathError("Windows 无法定位此文件，请检查映射连接和文件是否仍然存在。")
            # With cidl=0, pidl identifies the item itself: Windows opens its
            # parent folder and selects it. This also works for directories.
            result = shell32.SHOpenFolderAndSelectItems(pidl, 0, None, 0)
            if ctypes.c_int32(result).value < 0:
                raise LocalPathError("Windows 无法打开文件所在位置，请检查文件资源管理器。")
        finally:
            try:
                if pidl.value:
                    ole32.CoTaskMemFree(pidl)
            finally:
                if initialized:
                    ole32.CoUninitialize()
    except (OSError, AttributeError, ctypes.ArgumentError, TypeError, ValueError):
        raise LocalPathError("无法打开文件所在位置，请检查 Windows 文件资源管理器。") from None


def reveal_local_file(profile: dict[str, str], remote_path: str) -> None:
    candidate, _ = resolve_local_path(profile, remote_path)
    _select_shell_item(candidate)
