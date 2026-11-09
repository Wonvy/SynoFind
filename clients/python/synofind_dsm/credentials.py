"""Per-user desktop settings and Windows DPAPI credential protection.

Only public connection fields and DPAPI ciphertext reach disk. There is no
machine-scope encryption, plaintext fallback, OTP storage, or credential log.
"""

from __future__ import annotations

import base64
import ctypes
from ctypes import wintypes
import hashlib
import hmac
import json
import os
from pathlib import Path, PureWindowsPath
import re
import tempfile
import threading
from typing import Any
from urllib.parse import urlsplit, urlunsplit

from .client import DSMError, _remote_path


DEFAULT_PROFILE = {
    "url": "",
    "username": "",
    "root": "",
    "smb_host": "",
    "local_root": "",
    "preview_mode": "web",
    "preview_executable": "",
}
_MAPPING_DEFAULTS = {"local_root": "", "preview_mode": "web", "preview_executable": ""}
_MAX_SETTINGS_BYTES = 128 * 1024
_MAX_PASSWORD_BYTES = 16 * 1024


def normalize_profile(profile: dict[str, Any], *, allow_unselected: bool = False) -> dict[str, str]:
    """Return only validated, public fields; ignore any credential-shaped keys."""
    if not isinstance(profile, dict):
        raise DSMError("连接设置格式不正确。")
    result = {key: profile.get(key, default) for key, default in DEFAULT_PROFILE.items()}
    if any(not isinstance(value, str) for value in result.values()):
        raise DSMError("连接设置必须是文本。")
    if any(any(ord(char) < 32 or 127 <= ord(char) <= 159 for char in value) for value in result.values()):
        raise DSMError("连接设置不能包含控制字符。")
    url = result["url"]
    try:
        parsed = urlsplit(url)
        port = parsed.port
    except ValueError:
        raise DSMError("群晖 HTTPS 地址不正确。") from None
    if (parsed.scheme != "https" or not parsed.hostname or parsed.username is not None
            or parsed.password is not None or parsed.query or parsed.fragment
            or any(char.isspace() for char in url) or "\\" in url):
        raise DSMError("请输入不含账号、密码和查询参数的 HTTPS 地址。")
    prefix = parsed.path.rstrip("/")
    if prefix and ("%" in prefix or any(part in ("", ".", "..") for part in prefix[1:].split("/"))):
        raise DSMError("群晖 HTTPS 地址路径不正确。")
    host = parsed.hostname.lower()
    authority = f"[{host}]" if ":" in host else host
    if port is not None:
        authority += f":{port}"
    result["url"] = urlunsplit(("https", authority, prefix, "", ""))
    if result["root"] or not allow_unselected:
        root = _remote_path(result["root"])
        if root == "/":
            raise DSMError("请选择一个共享文件夹作为搜索范围。")
        result["root"] = root
    if not result["username"].strip() or len(result["username"]) > 256:
        raise DSMError("请输入有效的群晖用户名（不超过 256 个字符）。")
    if not result["smb_host"]:
        # IPv6 literals use Windows' documented UNC host representation.
        result["smb_host"] = host.replace(":", "-") + ".ipv6-literal.net" if ":" in host else host
    if not re.fullmatch(r"[A-Za-z0-9](?:[A-Za-z0-9.-]{0,251}[A-Za-z0-9])?", result["smb_host"]):
        raise DSMError("文件共享地址应为 IP 地址或主机名，不含路径和端口。")
    if result["preview_mode"] not in {"web", "quicklook", "seer"}:
        raise DSMError("本机预览方式不受支持。")
    for key in ("local_root", "preview_executable"):
        if result[key] and not PureWindowsPath(result[key]).is_absolute():
            raise DSMError("本机映射和预览程序需要使用完整的 Windows 路径。")
    return result


def _entropy(profile: dict[str, str]) -> bytes:
    binding = {key: profile[key] for key in ("url", "username", "root")}
    encoded = json.dumps(binding, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(b"SynoFind.desktop.credentials.v1\x00" + encoded).digest()


class _DataBlob(ctypes.Structure):
    _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_ubyte))]


def _dpapi(data: bytes, entropy: bytes, *, protect: bool) -> bytes:
    if os.name != "nt":
        raise DSMError("仅支持在 Windows 当前用户下加密保存密码；可以选择仅本次连接。")
    # The input buffers must stay alive until the native call completes.
    source_buffer = ctypes.create_string_buffer(data)
    entropy_buffer = ctypes.create_string_buffer(entropy)
    source = _DataBlob(len(data), ctypes.cast(source_buffer, ctypes.POINTER(ctypes.c_ubyte)))
    extra = _DataBlob(len(entropy), ctypes.cast(entropy_buffer, ctypes.POINTER(ctypes.c_ubyte)))
    output = _DataBlob()
    try:
        crypt32 = ctypes.WinDLL("crypt32", use_last_error=True)
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        function = crypt32.CryptProtectData if protect else crypt32.CryptUnprotectData
        function.argtypes = [ctypes.POINTER(_DataBlob), ctypes.c_void_p, ctypes.POINTER(_DataBlob),
                             ctypes.c_void_p, ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(_DataBlob)]
        function.restype = wintypes.BOOL
        kernel32.LocalFree.argtypes = [ctypes.c_void_p]
        kernel32.LocalFree.restype = ctypes.c_void_p
        # CRYPTPROTECT_UI_FORBIDDEN only. Deliberately omit LOCAL_MACHINE.
        if not function(ctypes.byref(source), None, ctypes.byref(extra), None, None, 0x1, ctypes.byref(output)):
            raise DSMError("Windows 无法解密或保存此账号凭据，请重新输入密码。")
        return ctypes.string_at(output.pbData, output.cbData)
    except (OSError, AttributeError):
        raise DSMError("Windows 凭据保护功能不可用，未保存密码。") from None
    finally:
        ctypes.memset(source_buffer, 0, len(source_buffer))
        if output.pbData:
            ctypes.memset(output.pbData, 0, output.cbData)
            kernel32.LocalFree(output.pbData)


class SettingsStore:
    """One atomic settings file prevents profile/credential pair mismatches."""

    def __init__(self, data_dir: str | os.PathLike[str]) -> None:
        self.data_dir = Path(data_dir)
        self.path = self.data_dir / "settings.json"
        self._lock = threading.RLock()
        self._memory_password: str | None = None
        self._memory_binding: bytes | None = None

    @property
    def can_remember(self) -> bool:
        return os.name == "nt"

    def _read(self) -> dict[str, Any]:
        try:
            with self.path.open("rb") as source:
                payload = source.read(_MAX_SETTINGS_BYTES + 1)
        except FileNotFoundError:
            return {"version": 1, "profile": dict(DEFAULT_PROFILE)}
        except OSError:
            raise DSMError("无法读取本机连接设置。") from None
        try:
            if len(payload) > _MAX_SETTINGS_BYTES:
                raise ValueError
            document = json.loads(payload)
            if not isinstance(document, dict) or document.get("version") != 1:
                raise ValueError
            profile = document.get("profile")
            if profile == DEFAULT_PROFILE and "credential" not in document:
                document["profile"] = dict(DEFAULT_PROFILE)
            else:
                document["profile"] = normalize_profile(profile)
            return document
        except (ValueError, UnicodeError, TypeError):
            raise DSMError("本机连接设置损坏，请重新配置连接。") from None

    @property
    def profile(self) -> dict[str, str]:
        with self._lock:
            return dict(self._read()["profile"])

    @property
    def remembered(self) -> bool:
        with self._lock:
            document = self._read()
            credential = document.get("credential")
            return bool(isinstance(credential, dict) and credential.get("kind") == "windows-dpapi-user"
                        and isinstance(credential.get("ciphertext"), str) and credential["ciphertext"]
                        and credential.get("binding") == _entropy(document["profile"]).hex())

    def load(self) -> tuple[dict[str, str], str | None]:
        with self._lock:
            document = self._read()
            profile = dict(document["profile"])
            binding = _entropy(profile)
            credential = document.get("credential")
            if credential is None:
                password = self._memory_password if self._memory_binding == binding else None
                return profile, password
            if (not isinstance(credential, dict) or credential.get("kind") != "windows-dpapi-user"
                    or not isinstance(credential.get("binding"), str)
                    or not hmac.compare_digest(credential["binding"], binding.hex())
                    or not isinstance(credential.get("ciphertext"), str)):
                raise DSMError("已保存密码与当前连接设置不匹配，请重新输入密码。")
            try:
                ciphertext = base64.b64decode(credential["ciphertext"], validate=True)
                plaintext = _dpapi(ciphertext, binding, protect=False)
                decoded = json.loads(plaintext)
                if (not isinstance(decoded, dict) or decoded.get("version") != 1
                        or decoded.get("binding") != binding.hex()
                        or not isinstance(decoded.get("password"), str) or not decoded["password"]):
                    raise ValueError
                return profile, decoded["password"]
            except (ValueError, UnicodeError, TypeError):
                raise DSMError("无法读取已加密保存的密码，请重新输入。") from None

    @staticmethod
    def _mappings(document: dict[str, Any]) -> dict[str, dict[str, str]]:
        mappings = document.get("mappings", {})
        if not isinstance(mappings, dict):
            return {}
        checked: dict[str, dict[str, str]] = {}
        for key, value in mappings.items():
            if not isinstance(key, str) or not re.fullmatch(r"[0-9a-f]{64}", key) or not isinstance(value, dict):
                continue
            mapping = {field: value.get(field, default) for field, default in _MAPPING_DEFAULTS.items()}
            if (any(not isinstance(item, str) or any(ord(char) < 32 or 127 <= ord(char) <= 159 for char in item)
                    for item in mapping.values()) or mapping["preview_mode"] not in {"web", "quicklook", "seer"}):
                continue
            checked[key] = mapping
        return checked

    def mapping_for(self, profile: dict[str, Any]) -> dict[str, str]:
        normalized = normalize_profile(profile)
        with self._lock:
            document = self._read()
            key = _entropy(normalized).hex()
            mappings = self._mappings(document)
            if key in mappings:
                return dict(mappings[key])
            # Migrate the current mapping from pre-mapping-table settings.
            current = document["profile"]
            if current["url"] and _entropy(current).hex() == key:
                return {field: current.get(field, default) for field, default in _MAPPING_DEFAULTS.items()}
            return dict(_MAPPING_DEFAULTS)

    def _write(self, document: dict[str, Any]) -> None:
        temporary: Path | None = None
        try:
            self.data_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
            payload = json.dumps(document, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
            if len(payload) > _MAX_SETTINGS_BYTES:
                raise DSMError("本机连接设置超出大小限制。")
            with tempfile.NamedTemporaryFile(mode="wb", dir=self.data_dir, prefix=".settings-", suffix=".tmp", delete=False) as target:
                temporary = Path(target.name)
                target.write(payload)
                target.flush()
                os.fsync(target.fileno())
            os.replace(temporary, self.path)
            temporary = None
        except (OSError, ValueError, UnicodeError):
            raise DSMError("无法保存本机连接设置；原设置保持不变。") from None
        finally:
            if temporary is not None:
                try:
                    temporary.unlink(missing_ok=True)
                except OSError:
                    pass

    def save(self, profile: dict[str, Any], password: str, remember: bool) -> None:
        normalized = normalize_profile(profile)
        if not normalized["username"].strip() or not isinstance(password, str) or not password:
            raise DSMError("请输入群晖账号和密码。")
        if type(remember) is not bool:
            raise DSMError("记住账号选项格式不正确。")
        try:
            if len(password.encode("utf-8")) > _MAX_PASSWORD_BYTES:
                raise DSMError("密码长度超出客户端限制。")
        except UnicodeError:
            raise DSMError("密码包含无效字符。") from None
        with self._lock:
            binding = _entropy(normalized)
            document: dict[str, Any] = {"version": 1, "profile": normalized}
            try:
                previous = self._read()
                mappings = self._mappings(previous)
                current = previous["profile"]
                if current["url"] and current["root"]:
                    mappings.setdefault(_entropy(current).hex(),
                                        {field: current.get(field, default) for field, default in _MAPPING_DEFAULTS.items()})
            except DSMError:
                # An explicitly verified new connection may replace damaged
                # settings; never reuse credentials from that damaged file.
                mappings = {}
            mappings[binding.hex()] = {field: normalized[field] for field in _MAPPING_DEFAULTS}
            document["mappings"] = mappings
            if remember:
                plaintext = json.dumps({"version": 1, "binding": binding.hex(), "password": password},
                                       ensure_ascii=False, separators=(",", ":")).encode("utf-8")
                ciphertext = _dpapi(plaintext, binding, protect=True)
                document["credential"] = {"kind": "windows-dpapi-user", "binding": binding.hex(),
                                          "ciphertext": base64.b64encode(ciphertext).decode("ascii")}
            self._write(document)
            self._memory_password = None if remember else password
            self._memory_binding = None if remember else binding

    def clear_memory(self) -> None:
        with self._lock:
            self._memory_password = None
            self._memory_binding = None

    def forget(self) -> None:
        with self._lock:
            document = self._read()
            self._write({"version": 1, "profile": document["profile"], "mappings": self._mappings(document)})
            self.clear_memory()
