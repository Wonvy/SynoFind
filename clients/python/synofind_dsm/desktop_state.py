"""Serialized desktop connection state over the official DSM API client."""

from __future__ import annotations

from contextlib import ExitStack, contextmanager
from dataclasses import dataclass
import os
from pathlib import Path
import secrets
import sys
import threading
import time
from typing import Any, Callable, Iterator

from .client import APIError, AuthenticationError, DSMClient, DSMError, SearchResult, SearchTimeout, SearchCancelled, _shared_root
from .credentials import DEFAULT_PROFILE, SettingsStore, normalize_profile
from .local_paths import LocalPathError, validate_local_profile


_API_METHODS = {
    "SYNO.API.Info": {"query"},
    "SYNO.API.Auth": {"login", "logout", "token"},
    "SYNO.FileStation.List": {"list", "getinfo", "list_share"},
    "SYNO.FileStation.Search": {"start", "list", "stop", "clean"},
    "SYNO.FileStation.Download": {"download"},
}
_SELECTION_TTL = 300


class ConnectionSelectionError(DSMError):
    """Fixed local messages that never contain NAS response text or credentials."""

    def __init__(self, reason: str) -> None:
        messages = {
            "expired": "共享文件夹选择会话已失效或过期，请重新登录获取列表。",
            "unlisted": "请选择当前账号列表中的共享文件夹。",
            "credentials": "当前连接凭据已不可用，请重新连接后切换共享文件夹。",
        }
        super().__init__(messages.get(reason, "无法完成共享文件夹选择，请重新连接。"))


@dataclass(repr=False)
class _PendingConnection:
    selection_id: str
    profile: dict[str, str]
    client: DSMClient | None
    password: str | None
    shares: list[dict[str, str]]
    expires_at: float
    timer: threading.Timer | None = None


def _diagnostics(error: Exception | None) -> tuple[int | None, str | None, str | None]:
    if not isinstance(error, APIError):
        return None, None, None
    code = error.code if type(error.code) is int and 0 <= error.code <= 2 ** 31 - 1 else None
    api = error.api if isinstance(error.api, str) and error.api in _API_METHODS else None
    method = error.method if api and isinstance(error.method, str) and error.method in _API_METHODS[api] else None
    return code, api, method


def _safe_error(error: Exception) -> str:
    if isinstance(error, (ConnectionSelectionError, LocalPathError)):
        return str(error)
    if isinstance(error, SearchTimeout):
        return "搜索超时，请缩小搜索范围后重试。"
    if isinstance(error, APIError):
        code, api, _ = _diagnostics(error)
        if api == "SYNO.API.Auth":
            if error.code in (403, 406):
                return "此账号需要两步验证，请输入当前验证码后重新连接。"
            if error.code == 404:
                return "两步验证码不正确或已过期，请输入新的验证码。"
            if error.code == 400:
                return "群晖账号或密码不正确，请检查后重新连接。"
            if error.code == 401:
                return "群晖账号已停用，请检查账号设置。"
            if error.code == 402:
                return "此账号没有访问 File Station 的权限。"
        common = {
            101: "群晖报告接口缺少 API、method 或 version 参数，需要检查客户端请求格式。",
            102: "此群晖未提供请求的接口，请检查 File Station 是否已安装并运行。",
            103: "群晖不支持请求的接口方法，需要检查接口兼容性。",
            104: "群晖不支持请求的接口版本，需要检查接口兼容性。",
            105: "当前会话无权访问此接口，请检查账号的 File Station 应用权限。",
            119: "群晖未识别当前会话 ID，请重新连接。",
        }
        if code in common:
            return common[code]
        if code in (106, 107):
            return "群晖会话已失效，请重新连接。"
        if api and api.startswith("SYNO.FileStation."):
            file_errors = {
                400: "群晖报告文件操作参数无效，需要检查客户端请求和目录设置。",
                401: "群晖返回了未说明原因的文件操作错误。",
                402: "群晖系统繁忙，请稍后重试。",
                403: "群晖未能识别执行文件操作的用户，请检查账号及目录服务状态。",
                404: "群晖未能识别执行文件操作的用户组，请检查账号及目录服务状态。",
                405: "群晖未能识别执行文件操作的用户和用户组。",
                406: "群晖无法从账号服务器取得用户或用户组信息。",
                407: "群晖不允许此文件操作，请检查共享文件夹及文件权限。",
                408: "找不到指定的共享文件夹或路径，请核对目录名称和大小写。",
            }
            if code in file_errors:
                return file_errors[code]
        return "群晖拒绝了请求，请根据接口名称和错误码排查。"
    return "连接或操作未完成，请检查群晖地址、证书、网络及账号权限。"


def _sanitized(error: Exception) -> DSMError:
    if isinstance(error, (ConnectionSelectionError, LocalPathError)):
        return error
    message = _safe_error(error)
    code, api, method = _diagnostics(error)
    if isinstance(error, AuthenticationError):
        return AuthenticationError(code, api or "", method or "", message)
    if isinstance(error, APIError):
        return APIError(code, api or "", method or "", message)
    if isinstance(error, SearchTimeout):
        return SearchTimeout(message)
    return DSMError(message)


class DesktopState:
    """All DSM calls and connection changes share one reentrant operation lock.

    Construction does not access the network. The desktop server should invoke
    ``reconnect`` once in a startup worker. Status reads never cause a login.
    """

    def __init__(self, data_dir: str | os.PathLike[str], client_factory: Callable[..., DSMClient] = DSMClient) -> None:
        self.store = SettingsStore(data_dir)
        self._factory = client_factory
        self._lock = threading.RLock()
        self._pending_lock = threading.RLock()
        self._pending: _PendingConnection | None = None
        self._client: DSMClient | None = None
        self._profile = dict(DEFAULT_PROFILE)
        self._remembered = False
        self._last_error: str | None = None
        self._last_error_code: int | None = None
        self._last_error_api: str | None = None
        self._last_error_method: str | None = None
        self._stopped = False
        try:
            self._profile = self.store.profile
            self._remembered = self.store.remembered
        except DSMError:
            self._last_error = "本机连接设置无法读取，请重新配置连接。"
        self._publish_status()

    def _publish_status(self) -> None:
        # Replace one immutable-by-convention snapshot while the operation lock
        # is held. Status readers never wait behind a long search or download.
        self._status_snapshot = {
                "configured": bool(self._profile["url"] and self._profile["username"].strip() and self._profile["root"]),
                "connected": self._client is not None,
                "remembered": self._remembered,
                "profile": dict(self._profile),
                "last_error": self._last_error,
                "last_error_code": self._last_error_code,
                "last_error_api": self._last_error_api,
                "last_error_method": self._last_error_method,
                "can_remember": self.store.can_remember,
                "platform": "windows" if os.name == "nt" else sys.platform,
        }

    def status(self) -> dict[str, Any]:
        snapshot = self._status_snapshot
        return dict(snapshot, profile=dict(snapshot["profile"]))

    def _record_error(self, error: Exception | None, *, message: str | None = None) -> None:
        self._last_error = message if message is not None else (_safe_error(error) if error is not None else None)
        self._last_error_code, self._last_error_api, self._last_error_method = _diagnostics(error)

    @staticmethod
    def _close(client: DSMClient | None) -> None:
        if client is not None:
            try:
                client.close()
            except Exception:
                # Local session disposal must complete even when DSM is offline.
                pass

    def _new_client(self, profile: dict[str, str], password: str, otp_code: str | None) -> DSMClient:
        candidate = self._factory(profile["url"], username=profile["username"], password=password,
                                  otp_code=otp_code, allowed_root=profile["root"])
        try:
            candidate.login()
            candidate.list_files(folder=profile["root"], limit=1, offset=0)
            return candidate
        except BaseException:
            self._close(candidate)
            raise

    def _clear_pending(self, selection_id: str | None = None, *, keep_client: bool = False) -> None:
        with self._pending_lock:
            pending = self._pending
            if pending is None or (selection_id is not None and pending.selection_id != selection_id):
                return
            self._pending = None
            pending.password = None
            candidate, pending.client = pending.client, None
            if pending.timer is not None:
                pending.timer.cancel()
                pending.timer = None
        if not keep_client:
            self._close(candidate)

    def _expire_pending(self, selection_id: str) -> None:
        # This lock is separate from the active-operation lock so an unrelated
        # long search cannot retain an unused pending password past its expiry.
        with self._pending_lock:
            pending = self._pending
            if pending is None or pending.selection_id != selection_id:
                return
            remaining = pending.expires_at - time.monotonic()
            if remaining > 0:
                # Timer/monotonic clocks can round differently on Windows. An
                # early callback must reschedule rather than abandon expiry.
                timer = threading.Timer(remaining, self._expire_pending, args=(selection_id,))
                timer.daemon = True
                pending.timer = timer
                timer.start()
                return
        self._clear_pending(selection_id)

    def _annotated_shares(self, profile: dict[str, str], shares: list[dict[str, str]]) -> list[dict[str, str]]:
        result = []
        seen: set[str] = set()
        for share in shares:
            path = _shared_root(share.get("path"))
            name = share.get("name")
            if not isinstance(name, str) or not name or path in seen:
                raise DSMError("群晖返回的共享文件夹列表格式不正确。")
            seen.add(path)
            mapping = self.store.mapping_for(dict(profile, root=path))
            result.append({"name": name, "path": path, **mapping})
        return result

    def _library_profile(self, profile: dict[str, str], root: str, *, smb_host: str | None,
                         local_root: str | None, preview_mode: str | None,
                         preview_executable: str | None) -> dict[str, str]:
        selected = dict(profile, root=root)
        selected.update(self.store.mapping_for(selected))
        for key, value in (("smb_host", smb_host), ("local_root", local_root),
                           ("preview_mode", preview_mode), ("preview_executable", preview_executable)):
            if value is not None:
                selected[key] = value
        selected = normalize_profile(selected)
        validate_local_profile(selected)
        return selected

    def prepare_connection(self, profile: dict[str, Any], password: str,
                           otp_code: str | None = None) -> dict[str, Any]:
        with self._lock:
            if self._stopped:
                raise DSMError("本机服务正在关闭，请重新启动。")
            candidate: DSMClient | None = None
            try:
                if not isinstance(profile, dict):
                    raise DSMError("连接设置格式不正确。")
                normalized = normalize_profile(dict(profile, root="", local_root="", preview_mode="web", preview_executable=""),
                                               allow_unselected=True)
                if not isinstance(password, str) or not password or (otp_code is not None and not isinstance(otp_code, str)):
                    raise DSMError("请输入群晖账号密码及有效验证码。")
                candidate = self._factory(normalized["url"], username=normalized["username"], password=password,
                                          otp_code=otp_code, allowed_root=None)
                candidate.login()
                shares = self._annotated_shares(normalized, candidate.list_shares())
            except Exception as error:
                self._close(candidate)
                self._record_error(error)
                self._publish_status()
                raise _sanitized(error) from None
            self._clear_pending()
            selection_id = secrets.token_urlsafe(32)
            pending = _PendingConnection(selection_id, normalized, candidate, password, shares,
                                         time.monotonic() + _SELECTION_TTL)
            timer = threading.Timer(_SELECTION_TTL, self._expire_pending, args=(selection_id,))
            timer.daemon = True
            pending.timer = timer
            with self._pending_lock:
                self._pending = pending
                timer.start()
            self._record_error(None)
            self._publish_status()
            return {"selection_id": selection_id, "shares": [dict(share) for share in shares],
                    "profile": dict(normalized), "expires_in": _SELECTION_TTL}

    def commit_connection(self, selection_id: str, root: str, remember: bool = False,
                          smb_host: str | None = None, local_root: str | None = None,
                          preview_mode: str | None = None, preview_executable: str | None = None) -> dict[str, Any]:
        with self._lock, self._pending_lock:
            if self._stopped:
                raise DSMError("本机服务正在关闭，请重新启动。")
            candidate = None
            previous_root = None
            try:
                pending = self._pending
                if (pending is None or not isinstance(selection_id, str) or not selection_id.isascii()
                        or not secrets.compare_digest(selection_id, pending.selection_id)):
                    raise ConnectionSelectionError("expired")
                if time.monotonic() >= pending.expires_at or pending.client is None or pending.password is None:
                    self._clear_pending(selection_id)
                    raise ConnectionSelectionError("expired")
                if not isinstance(root, str) or root not in {share["path"] for share in pending.shares}:
                    raise ConnectionSelectionError("unlisted")
                if type(remember) is not bool or (remember and not self.store.can_remember):
                    raise DSMError("当前环境不支持此密码保存选项。")
                selected = self._library_profile(pending.profile, root, smb_host=smb_host, local_root=local_root,
                                                 preview_mode=preview_mode, preview_executable=preview_executable)
                candidate = pending.client
                previous_root = candidate.allowed_root
                candidate.select_root(root)
                candidate.list_files(folder=root, limit=1, offset=0)
                if time.monotonic() >= pending.expires_at:
                    self._clear_pending(selection_id)
                    raise ConnectionSelectionError("expired")
                self.store.save(selected, pending.password, remember)
            except Exception as error:
                if candidate is not None:
                    candidate.allowed_root = previous_root
                self._record_error(error)
                self._publish_status()
                raise _sanitized(error) from None
            self._clear_pending(selection_id, keep_client=True)
            previous, self._client = self._client, candidate
            self._profile, self._remembered = selected, remember
            self._close(previous)
            self._record_error(None)
            self._publish_status()
            return self.status()

    def cancel_connection(self, selection_id: str) -> None:
        # An unknown ID must not cancel a newer selection in another tab.
        if isinstance(selection_id, str):
            self._clear_pending(selection_id)

    def list_shares(self) -> list[dict[str, str]]:
        with self._lock:
            try:
                return self._annotated_shares(self._profile, self._invoke("list_shares"))
            except Exception as error:
                self._record_error(error)
                self._publish_status()
                raise _sanitized(error) from None

    def select_library(self, root: str, smb_host: str | None = None, local_root: str | None = None,
                       preview_mode: str | None = None, preview_executable: str | None = None) -> dict[str, Any]:
        with self._lock:
            candidate = None
            previous_root = None
            password = None
            try:
                shares = self._invoke("list_shares")
                if not isinstance(root, str) or root not in {share["path"] for share in shares}:
                    raise ConnectionSelectionError("unlisted")
                selected = self._library_profile(self._profile, root, smb_host=smb_host, local_root=local_root,
                                                 preview_mode=preview_mode, preview_executable=preview_executable)
                saved_profile, password = self.store.load()
                if not password or saved_profile != self._profile:
                    raise ConnectionSelectionError("credentials")
                candidate = self._client
                previous_root = candidate.allowed_root
                candidate.select_root(root)
                candidate.list_files(folder=root, limit=1, offset=0)
                self.store.save(selected, password, self._remembered)
            except Exception as error:
                if candidate is not None:
                    candidate.allowed_root = previous_root
                self._record_error(error)
                self._publish_status()
                raise _sanitized(error) from None
            finally:
                password = None
            self._profile = selected
            self._record_error(None)
            self._publish_status()
            return self.status()

    def connect(self, profile: dict[str, Any], password: str, otp_code: str | None = None,
                remember: bool = False) -> dict[str, Any]:
        with self._lock:
            if self._stopped:
                raise DSMError("本机服务正在关闭，请重新启动。")
            candidate: DSMClient | None = None
            try:
                normalized = normalize_profile(profile)
                if not normalized["username"].strip() or not isinstance(password, str) or not password:
                    raise DSMError("请输入群晖账号和密码。")
                if type(remember) is not bool or (otp_code is not None and not isinstance(otp_code, str)):
                    raise DSMError("连接参数格式不正确。")
                if remember and not self.store.can_remember:
                    raise DSMError("仅 Windows 支持加密保存密码，请选择仅本次连接。")
                validate_local_profile(normalized)
                candidate = self._new_client(normalized, password, otp_code)
                # Save only after login and shared-folder read permission succeed.
                self.store.save(normalized, password, remember)
            except Exception as error:
                self._close(candidate)
                self._record_error(error)
                self._publish_status()
                raise _sanitized(error) from None
            previous, self._client = self._client, candidate
            self._clear_pending()
            self._profile = normalized
            self._remembered = remember
            self._record_error(None)
            self._close(previous)
            self._publish_status()
            return self.status()

    def disconnect(self, forget: bool = False) -> dict[str, Any]:
        with self._lock:
            if type(forget) is not bool:
                raise DSMError("忘记账号选项格式不正确。")
            self._clear_pending()
            previous, self._client = self._client, None
            self._close(previous)
            self.store.clear_memory()
            if forget:
                try:
                    self.store.forget()
                    self._remembered = False
                except DSMError as error:
                    self._record_error(error, message="未能删除本机保存的凭据，请检查本机文件权限。")
                    self._publish_status()
                    raise DSMError(self._last_error) from None
            self._record_error(None)
            self._publish_status()
            return self.status()

    def _reconnect(self, *, automatic: bool = False) -> dict[str, Any]:
        if self._stopped:
            raise DSMError("本机服务正在关闭，请重新启动。")
        if self._client is not None:
            self._record_error(None)
            self._publish_status()
            return self.status()
        try:
            if automatic and not self._remembered:
                raise AuthenticationError(None, "SYNO.API.Auth", "login", "请重新输入账号密码连接。")
            profile, password = self.store.load()
            if automatic and profile != self._profile:
                raise DSMError("保存的连接设置已改变，请重新确认连接。")
            if not password:
                self._record_error(None)
                self._publish_status()
                return self.status()
            candidate = self._new_client(profile, password, None)
        except Exception as error:
            self._record_error(error)
            self._publish_status()
            raise _sanitized(error) from None
        self._client = candidate
        self._profile = profile
        self._remembered = self.store.remembered
        self._record_error(None)
        self._publish_status()
        return self.status()

    def reconnect(self) -> dict[str, Any]:
        """Attempt one saved-credential login; never loop or reuse an old OTP."""
        with self._lock:
            return self._reconnect()

    def _recover_session(self, error: Exception) -> bool:
        if not isinstance(error, APIError) or error.code not in (106, 107):
            return False
        previous, self._client = self._client, None
        self._close(previous)
        self._reconnect(automatic=True)
        return self._client is not None

    def _invoke(self, method: str, *args: Any, **kwargs: Any) -> Any:
        with self._lock:
            if self._client is None:
                raise AuthenticationError(None, "SYNO.API.Auth", "login", "请先连接群晖账号。")
            for attempt in range(2):
                try:
                    result = getattr(self._client, method)(*args, **kwargs)
                    self._record_error(None)
                    self._publish_status()
                    return result
                except Exception as error:
                    if isinstance(error, SearchCancelled):
                        raise
                    if attempt == 0:
                        try:
                            if self._recover_session(error):
                                continue
                        except Exception as recovery_error:
                            error = recovery_error
                    elif isinstance(error, APIError) and error.code in (106, 107):
                        previous, self._client = self._client, None
                        self._close(previous)
                    self._record_error(error)
                    self._publish_status()
                    raise _sanitized(error) from None

    def search(self, params: dict[str, Any]) -> SearchResult:
        if not isinstance(params, dict):
            raise DSMError("搜索条件格式不正确。")
        allowed = {"pattern", "folder", "recursive", "extension", "min_size", "max_size", "modified_after",
                   "modified_before", "limit", "offset", "sort_by", "descending", "search_timeout", "poll_interval", "cancel_event"}
        if set(params) - allowed:
            raise DSMError("搜索条件包含不支持的字段。")
        return self._invoke("search", **params)

    def get_info(self, path: str) -> dict[str, Any]:
        return self._invoke("get_info", path)

    def download(self, path: str, dest: str | os.PathLike[str]) -> Path:
        return self._invoke("download", path, dest)

    @contextmanager
    def open_content(self, path: str) -> Iterator[tuple[dict[str, Any], Any]]:
        """Hold the operation lock until all response bytes have been consumed."""
        with self._lock, ExitStack() as stack:
            if self._client is None:
                raise AuthenticationError(None, "SYNO.API.Auth", "login", "请先连接群晖账号。")
            for attempt in range(2):
                try:
                    content = stack.enter_context(self._client.open_content(path))
                    self._record_error(None)
                    self._publish_status()
                    break
                except Exception as error:
                    if attempt == 0:
                        try:
                            if self._recover_session(error):
                                continue
                        except Exception as recovery_error:
                            error = recovery_error
                    elif isinstance(error, APIError) and error.code in (106, 107):
                        previous, self._client = self._client, None
                        self._close(previous)
                    self._record_error(error)
                    self._publish_status()
                    raise _sanitized(error) from None
            # Exceptions while the caller reads are never retried: bytes may
            # already have reached the browser.
            yield content

    def stop(self) -> None:
        with self._lock:
            self._stopped = True
            self._clear_pending()
            previous, self._client = self._client, None
            self._close(previous)
            self.store.clear_memory()
            self._record_error(None)
            self._publish_status()
