"""Small, dependency-free client for Synology's documented File Station APIs.

No persistent index, cookies, credentials, or session cache is created. Requests
are POSTed, redirects are refused, and HTTPS certificates are verified.

Official references:
https://kb.synology.com/en-global/DG/DSM_Login_Web_API_Guide/3
https://global.download.synology.com/download/Document/Software/DeveloperGuide/Package/FileStation/All/enu/Synology_File_Station_API_Guide.pdf
"""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
from email.message import Message
import http.client
import json
import math
import os
from pathlib import Path, PurePosixPath
import re
import sys
import tempfile
import time
import warnings
from typing import Any, Iterator, TypedDict
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urlsplit, urlunsplit
from urllib.request import Request

from .transport import make_opener


AUTH_API = "SYNO.API.Auth"
SEARCH_API = "SYNO.FileStation.Search"
LIST_API = "SYNO.FileStation.List"
DOWNLOAD_API = "SYNO.FileStation.Download"
_MAX_JSON_BYTES = 16 * 1024 * 1024
_ERROR_PROBE_BYTES = 64 * 1024
_CHUNK_BYTES = 1024 * 1024
_API_PATH = re.compile(r"[A-Za-z0-9_.-]+(?:/[A-Za-z0-9_.-]+)*\Z")
_COOKIE_VALUE = re.compile(r"[\x21\x23-\x2B\x2D-\x3A\x3C-\x5B\x5D-\x7E]+\Z")
_HEADER_TOKEN = re.compile(r"[\x21-\x7E]+\Z")


class _RequiredAPIInfo(TypedDict):
    path: str
    minVersion: int
    maxVersion: int


class APIInfo(_RequiredAPIInfo, total=False):
    requestFormat: str


@dataclass
class SearchResult:
    files: list[dict[str, Any]]
    total: int
    offset: int
    finished: bool


class DSMError(Exception):
    """A safe-to-display client error; never includes server response bodies."""


class SearchCancelled(DSMError):
    """The caller cancelled a search; its DSM task has been cleaned up."""


class APIError(DSMError):
    def __init__(
        self,
        code: int | None,
        api: str = "",
        method: str = "",
        message: str | None = None,
    ) -> None:
        self.code = code
        self.api = api
        self.method = method
        description = message or "DSM rejected the API request."
        # api and method are selected by this client, never copied from a reply.
        suffix = f" (code {code})" if code is not None else ""
        super().__init__(description + suffix)


class AuthenticationError(APIError):
    """Login failed or the DSM session is no longer valid."""


class SearchTimeout(DSMError):
    """The documented asynchronous search did not finish before its deadline."""


class _PrefixedResponse:
    """Replay a small inspected prefix before continuing the HTTP stream."""

    def __init__(self, response: Any, prefix: bytes) -> None:
        self._response = response
        self._prefix = prefix

    def __getattr__(self, name: str) -> Any:
        return getattr(self._response, name)

    def read(self, amount: int = -1) -> bytes:
        if amount is None or amount < 0:
            prefix, self._prefix = self._prefix, b""
            return prefix + self._response.read()
        prefix, self._prefix = self._prefix[:amount], self._prefix[amount:]
        if len(prefix) == amount:
            return prefix
        return prefix + self._response.read(amount - len(prefix))

    def close(self) -> None:
        self._prefix = b""
        self._response.close()


def _positive_number(value: Any, name: str, *, zero_ok: bool = False) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise DSMError(f"{name} must be a finite number.")
    number = float(value)
    if not math.isfinite(number) or number < 0 or (number == 0 and not zero_ok):
        raise DSMError(f"{name} is outside the allowed range.")
    return number


def _integer(value: Any, name: str, *, minimum: int = 0, maximum: int | None = None) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum or (maximum is not None and value > maximum):
        raise DSMError(f"{name} is outside the allowed integer range.")
    return value


def _remote_path(value: Any) -> str:
    if not isinstance(value, str) or not value.startswith("/"):
        raise DSMError("Remote paths must be absolute shared-folder paths.")
    if "\\" in value or any(ord(char) < 32 or 127 <= ord(char) <= 159 for char in value):
        raise DSMError("Remote paths cannot contain backslashes or control characters.")
    if any(part in (".", "..") for part in value.split("/")) or "//" in value:
        raise DSMError("Remote paths cannot contain traversal or ambiguous segments.")
    return str(PurePosixPath(value))


def _is_within(path: str, root: str) -> bool:
    return root == "/" or path == root or path.startswith(root + "/")


def _selected_root(value: str) -> str:
    root = _remote_path(value)
    if root == "/":
        raise DSMError("Select a shared folder, not the DSM root.")
    return root


def _shared_root(value: str) -> str:
    root = _selected_root(value)
    if "/" in root[1:]:
        raise DSMError("DSM returned a path that is not a top-level shared folder.")
    return root


def _safe_api_path(value: Any) -> str:
    if not isinstance(value, str) or not _API_PATH.fullmatch(value):
        raise DSMError("DSM supplied an unsafe API endpoint path.")
    if any(part in (".", "..") for part in value.split("/")):
        raise DSMError("DSM supplied an unsafe API endpoint path.")
    return value


class DSMClient:
    """Synchronous File Station client. Instances are not thread-safe.

    Constructor credentials are held only until the first login attempt, then
    discarded. Retrying a failed login requires explicitly passing credentials.
    ``allowed_root`` is a File Station path such as ``/Documents``, not a NAS
    local filesystem path. With no selected root, only login and share discovery
    are available; select a scope before requesting files.
    """

    def __init__(
        self,
        base_url: str,
        *,
        username: str | None = None,
        password: str | None = None,
        otp_code: str | None = None,
        ca_file: str | os.PathLike[str] | None = None,
        cert_sha256: str | None = None,
        timeout: float = 30,
        allowed_root: str | None = None,
        allow_http: bool = False,
    ) -> None:
        if not isinstance(base_url, str) or any(char.isspace() or ord(char) < 32 for char in base_url):
            raise DSMError("The DSM base URL is invalid.")
        try:
            parsed = urlsplit(base_url)
            _ = parsed.port
        except ValueError:
            raise DSMError("The DSM base URL is invalid.") from None
        if parsed.scheme not in ("https", "http") or not parsed.hostname or parsed.username is not None or parsed.password is not None or parsed.query or parsed.fragment:
            raise DSMError("Use a DSM HTTP(S) base URL without credentials, query, or fragment.")
        if parsed.scheme == "http" and not allow_http:
            raise DSMError("HTTP requires explicit allow_http=True; HTTPS is the default.")
        prefix = parsed.path.rstrip("/")
        if prefix and ("%" in prefix or "\\" in prefix or any(part in (".", "..", "") for part in prefix[1:].split("/"))):
            raise DSMError("The DSM base URL contains an unsafe path prefix.")
        self.base_url = urlunsplit((parsed.scheme, parsed.netloc, prefix, "", ""))
        self.allowed_root = None if allowed_root is None else _selected_root(allowed_root)
        self.timeout = _positive_number(timeout, "timeout")
        for value in (username, password, otp_code):
            if value is not None and not isinstance(value, str):
                raise DSMError("Credentials must be strings.")
        if parsed.scheme == "http" and cert_sha256 is not None:
            raise DSMError("A certificate fingerprint requires an HTTPS URL.")
        try:
            self._opener = make_opener(ca_file, cert_sha256)
        except (OSError, ValueError, TypeError):
            raise DSMError("Could not load the HTTPS certificate configuration.") from None
        self._api_info: dict[str, APIInfo] = {}
        self._sid: str | None = None
        self._synotoken: str | None = None
        self._pending_username = username
        self._pending_password = password
        self._pending_otp = otp_code
        self._closed = False

    def _ensure_open(self) -> None:
        if self._closed:
            raise DSMError("The DSM client is closed.")

    def _path(self, value: str) -> str:
        if self.allowed_root is None:
            raise DSMError("Select a shared folder before searching or reading files.")
        path = _remote_path(value)
        if not _is_within(path, self.allowed_root):
            raise DSMError("The remote path is outside allowed_root.")
        return path

    def select_root(self, root: str) -> None:
        """Set the explicit file scope; this does not grant NAS permissions."""
        self._ensure_open()
        self.allowed_root = _selected_root(root)

    def _open(self, endpoint: str, fields: dict[str, str], *, timeout: float | None = None) -> Any:
        self._ensure_open()
        url = self.base_url + "/webapi/" + _safe_api_path(endpoint)
        try:
            body = urlencode(fields, encoding="utf-8", errors="strict").encode("ascii")
            req = Request(url, data=body, method="POST", headers={
                "Content-Type": "application/x-www-form-urlencoded; charset=utf-8",
                "Accept-Encoding": "identity",
                "User-Agent": "SynoFind-DSM-Python/0.5.0",
            })
            if "_sid" in fields:
                sid = fields["_sid"]
                if (not isinstance(sid, str) or sid != self._sid
                        or not _COOKIE_VALUE.fullmatch(sid)):
                    raise DSMError("The DSM session cannot be safely attached to this request.")
                # DSM supports the id cookie as an alternative session carrier.
                # Some DSM gateways do not recognize a SID from the POST body.
                # Keep this header request-local: no cookie jar, URL, or disk.
                req.add_header("Cookie", "id=" + sid)
                if "SynoToken" in fields:
                    token = fields["SynoToken"]
                    if (not isinstance(token, str) or token != self._synotoken
                            or not _HEADER_TOKEN.fullmatch(token)):
                        raise DSMError("The DSM security token cannot be safely attached to this request.")
                    req.add_header("X-SYNO-TOKEN", token)
            response = self._opener.open(req, timeout=self.timeout if timeout is None else timeout)
            if not 200 <= response.status < 300:
                response.close()
                raise DSMError("DSM returned an unexpected HTTP status.")
            return response
        except HTTPError as error:
            code = error.code
            error.close()
            if 300 <= code < 400:
                raise DSMError("DSM redirected the request; redirects are refused to protect credentials.") from None
            raise DSMError(f"DSM returned HTTP status {code}.") from None
        except (URLError, OSError, http.client.HTTPException, ValueError, UnicodeError):
            raise DSMError("Could not complete the DSM connection; check its address, certificate, and availability.") from None

    def _failure(self, reply: dict[str, Any], api: str, method: str) -> None:
        raw_error = reply.get("error")
        code = raw_error.get("code") if isinstance(raw_error, dict) else None
        if isinstance(code, bool) or not isinstance(code, int):
            code = None
        if api == AUTH_API or code in (106, 107):
            if code in (106, 107):
                self._sid = None
                self._synotoken = None
            message = "DSM authentication failed."
            if code in (403, 406) and api == AUTH_API:
                message = "DSM requires a two-factor authentication code."
            elif code == 404 and api == AUTH_API:
                message = "DSM rejected the two-factor authentication code."
            elif code in (106, 107):
                message = "The DSM session expired or was interrupted; sign in again explicitly."
            raise AuthenticationError(code, api, method, message)
        raise APIError(code, api, method)

    def _decode_reply(self, payload: bytes, api: str, method: str) -> dict[str, Any]:
        try:
            reply = json.loads(payload)
        except (ValueError, UnicodeError):
            raise DSMError("DSM returned an invalid JSON API response.") from None
        if not isinstance(reply, dict) or type(reply.get("success")) is not bool:
            raise DSMError("DSM returned an invalid API response envelope.")
        if reply["success"] is False:
            self._failure(reply, api, method)
        data = reply.get("data", {})
        if not isinstance(data, dict):
            raise DSMError("DSM returned an invalid API data object.")
        return data

    def _json_request(self, endpoint: str, fields: dict[str, str], api: str, method: str, *, timeout: float | None = None) -> dict[str, Any]:
        try:
            with self._open(endpoint, fields, timeout=timeout) as response:
                payload = response.read(_MAX_JSON_BYTES + 1)
                if len(payload) > _MAX_JSON_BYTES:
                    raise DSMError("DSM returned an API response larger than the client limit.")
        except (OSError, http.client.HTTPException):
            raise DSMError("The DSM API response could not be read completely.") from None
        return self._decode_reply(payload, api, method)

    def discover(self) -> dict[str, APIInfo]:
        """Retrieve and validate the documented APIs without logging in."""
        self._ensure_open()
        data = self._json_request("entry.cgi", {
            "api": "SYNO.API.Info", "version": "1", "method": "query",
            "query": ",".join((AUTH_API, SEARCH_API, LIST_API, DOWNLOAD_API)),
        }, "SYNO.API.Info", "query")
        discovered: dict[str, APIInfo] = {}
        for name in (AUTH_API, SEARCH_API, LIST_API, DOWNLOAD_API):
            if name not in data:
                continue
            value = data[name]
            if not isinstance(value, dict):
                raise DSMError("DSM returned invalid API discovery information.")
            path = _safe_api_path(value.get("path"))
            low = _integer(value.get("minVersion"), "API minimum version", minimum=1)
            high = _integer(value.get("maxVersion"), "API maximum version", minimum=low)
            info: APIInfo = {"path": path, "minVersion": low, "maxVersion": high}
            request_format = value.get("requestFormat")
            if request_format is not None:
                if not isinstance(request_format, str) or request_format not in ("JSON", ""):
                    raise DSMError("DSM advertised an unsupported API parameter format.")
                info["requestFormat"] = request_format
            discovered[name] = info
        self._api_info = discovered
        return {name: dict(info) for name, info in discovered.items()}  # type: ignore[return-value]

    def _info(self, api: str) -> tuple[APIInfo, int]:
        self._ensure_open()
        if not self._api_info:
            self.discover()
        info = self._api_info.get(api)
        if info is None:
            raise DSMError("The required documented API is not available on this DSM installation.")
        versions = (6, 3, 7, 5, 4) if api == AUTH_API else (2,)
        for version in versions:
            if info["minVersion"] <= version <= info["maxVersion"]:
                return info, version
        raise DSMError("DSM does not support a documented API version used by this client.")

    def _fields(self, api: str, method: str, params: dict[str, Any], *, authenticated: bool = True) -> tuple[str, dict[str, str]]:
        info, version = self._info(api)
        fields = {"api": api, "version": str(version), "method": method}
        json_format = info.get("requestFormat") == "JSON"
        for name, value in params.items():
            if value is None:
                continue
            if json_format or isinstance(value, (list, dict, bool)):
                fields[name] = json.dumps(value, ensure_ascii=False, separators=(",", ":"))
            else:
                fields[name] = str(value)
        if authenticated:
            if not self._sid:
                raise AuthenticationError(None, api, method, "Sign in to DSM before making this request.")
            fields["_sid"] = self._sid
            if self._synotoken:
                fields["SynoToken"] = self._synotoken
        return info["path"], fields

    def _call(self, api: str, method: str, params: dict[str, Any] | None = None, *, authenticated: bool = True, timeout: float | None = None) -> dict[str, Any]:
        endpoint, fields = self._fields(api, method, params or {}, authenticated=authenticated)
        return self._json_request(endpoint, fields, api, method, timeout=timeout)

    def login(self, username: str | None = None, password: str | None = None, otp_code: str | None = None) -> DSMClient:
        """Log in once, using Auth v6 where available; never retry a password."""
        self._ensure_open()
        if self._sid:
            if any(value is not None for value in (username, password, otp_code)):
                raise AuthenticationError(None, AUTH_API, "login", "Log out before changing DSM credentials.")
            return self
        account = username if username is not None else self._pending_username
        secret = password if password is not None else self._pending_password
        otp = otp_code if otp_code is not None else self._pending_otp
        self._pending_username = self._pending_password = self._pending_otp = None
        try:
            if not isinstance(account, str) or not account or not isinstance(secret, str) or not secret:
                raise AuthenticationError(None, AUTH_API, "login", "A DSM username and password are required.")
            if otp is not None and not isinstance(otp, str):
                raise AuthenticationError(None, AUTH_API, "login", "The two-factor code must be a string.")
            _, version = self._info(AUTH_API)
            params: dict[str, Any] = {"account": account, "passwd": secret, "session": "FileStation", "format": "sid"}
            if otp is not None:
                params["otp_code"] = otp
            if version >= 6:
                params["enable_syno_token"] = "yes"
            data = self._call(AUTH_API, "login", params, authenticated=False)
            sid = data.get("sid")
            synotoken = data.get("synotoken")
            if (not isinstance(sid, str) or not _COOKIE_VALUE.fullmatch(sid)
                    or (synotoken is not None and (not isinstance(synotoken, str)
                        or (synotoken and not _HEADER_TOKEN.fullmatch(synotoken))))):
                raise AuthenticationError(None, AUTH_API, "login", "DSM did not return a valid login session.")
            self._sid, self._synotoken = sid, synotoken
            return self
        finally:
            # Python strings cannot be wiped reliably; do not retain them on the
            # client or write them to a cache, log, URL, or exception message.
            account = secret = otp = None

    def logout(self) -> None:
        if self._closed or not self._sid:
            return
        try:
            self._call(AUTH_API, "logout", {"session": "FileStation"})
        finally:
            self._sid = self._synotoken = None

    def close(self) -> None:
        if self._closed:
            return
        try:
            self.logout()
        finally:
            self._pending_username = self._pending_password = self._pending_otp = None
            self._sid = self._synotoken = None
            self._closed = True
            self._opener.close()

    def __enter__(self) -> DSMClient:
        try:
            return self.login()
        except BaseException:
            try:
                self.close()
            except Exception:
                pass
            raise

    def __exit__(self, exc_type: Any, exc_value: Any, traceback: Any) -> bool:
        try:
            self.close()
        except DSMError:
            if exc_type is None:
                warnings.warn(
                    "The operation succeeded, but DSM logout failed; the local session was cleared.",
                    RuntimeWarning,
                    stacklevel=2,
                )
        except Exception:
            if exc_type is None:
                raise
        return False

    def _result(self, data: dict[str, Any], *, folder: str, offset: int, finished: bool | None = None) -> SearchResult:
        files = data.get("files")
        if not isinstance(files, list):
            raise DSMError("DSM returned an invalid file list.")
        checked: list[dict[str, Any]] = []
        for entry in files:
            if not isinstance(entry, dict):
                raise DSMError("DSM returned invalid file metadata.")
            path = self._path(entry.get("path"))
            if not _is_within(path, folder):
                raise DSMError("DSM returned a file outside the requested folder.")
            checked.append(dict(entry, path=path))
        total = _integer(data.get("total"), "result total")
        response_offset = _integer(data.get("offset", offset), "result offset")
        complete = data.get("finished", finished)
        if type(complete) is not bool:
            raise DSMError("DSM returned an invalid search completion state.")
        return SearchResult(checked, total, response_offset, complete)

    def search(
        self, pattern: str = "", *, folder: str | None = None,
        recursive: bool = True, extension: str | None = None,
        min_size: int | None = None, max_size: int | None = None,
        modified_after: int | float | None = None, modified_before: int | float | None = None,
        limit: int = 100, offset: int = 0, sort_by: str = "name",
        descending: bool = False, search_timeout: float = 120,
        poll_interval: float = 0.5,
        cancel_event=None,
    ) -> SearchResult:
        """Search filenames using DSM glob patterns, not full-path substrings.

        Date filters are Unix timestamps in seconds. This waits for the task to
        finish, retrieves only the requested page, and cleans temporary results.
        It makes no guarantee about DSM's internal indexing or query latency.
        """
        scope = self._path(self.allowed_root if folder is None else folder)
        _integer(limit, "limit", minimum=1)
        _integer(offset, "offset")
        duration = _positive_number(search_timeout, "search_timeout")
        interval = _positive_number(poll_interval, "poll_interval", zero_ok=True)
        if not isinstance(pattern, str) or "\x00" in pattern:
            raise DSMError("The search pattern must be a string without NUL characters.")
        if type(recursive) is not bool or type(descending) is not bool:
            raise DSMError("recursive and descending must be booleans.")
        if extension is not None and (not isinstance(extension, str) or "\x00" in extension):
            raise DSMError("The extension filter must be a string without NUL characters.")
        if sort_by not in {"name", "size", "user", "group", "mtime", "atime", "ctime", "crtime", "posix", "type"}:
            raise DSMError("The search sort field is not supported by File Station.")
        for value, name in ((min_size, "min_size"), (max_size, "max_size")):
            if value is not None:
                _integer(value, name)
        if min_size is not None and max_size is not None and min_size > max_size:
            raise DSMError("min_size cannot exceed max_size.")
        for value, name in ((modified_after, "modified_after"), (modified_before, "modified_before")):
            if value is not None:
                _positive_number(value, name, zero_ok=True)
        if modified_after is not None and modified_before is not None and modified_after > modified_before:
            raise DSMError("modified_after cannot exceed modified_before.")
        deadline = time.monotonic() + duration

        def remaining() -> float:
            if cancel_event is not None and cancel_event.is_set():
                raise SearchCancelled("搜索已停止。")
            left = deadline - time.monotonic()
            if left <= 0:
                raise SearchTimeout("The DSM search exceeded search_timeout.")
            return min(self.timeout, left, 5) if cancel_event is not None else min(self.timeout, left)

        task: str | None = None
        finished = False
        try:
            started = self._call(SEARCH_API, "start", {
                "folder_path": [scope], "recursive": recursive, "pattern": pattern,
                "extension": extension, "size_from": min_size, "size_to": max_size,
                "mtime_from": None if modified_after is None else int(modified_after),
                "mtime_to": None if modified_before is None else int(modified_before),
            }, timeout=remaining())
            candidate = started.get("taskid")
            if not isinstance(candidate, str) or not candidate:
                raise DSMError("DSM did not return a valid search task ID.")
            task = candidate
            while True:
                state = self._call(SEARCH_API, "list", {"taskid": task, "limit": 0, "offset": 0}, timeout=remaining())
                if type(state.get("finished")) is not bool:
                    raise DSMError("DSM returned an invalid search completion state.")
                finished = state["finished"]
                if finished:
                    break
                left = deadline - time.monotonic()
                if left <= 0:
                    raise SearchTimeout("The DSM search exceeded search_timeout.")
                if cancel_event is not None:
                    if cancel_event.wait(min(interval, left)):
                        raise SearchCancelled("搜索已停止。")
                else:
                    time.sleep(min(interval, left))
            data = self._call(SEARCH_API, "list", {
                "taskid": task, "limit": limit, "offset": offset,
                "sort_by": sort_by, "sort_direction": "desc" if descending else "asc",
                "additional": ["size", "time", "owner", "perm", "type"],
            }, timeout=remaining())
            if cancel_event is not None and cancel_event.is_set():
                raise SearchCancelled("搜索已停止。")
            return self._result(data, folder=scope, offset=offset, finished=True)
        except DSMError:
            if time.monotonic() >= deadline:
                raise SearchTimeout("The DSM search exceeded search_timeout.") from None
            raise
        finally:
            primary_failure = sys.exc_info()[0] is not None
            cleanup_failure: Exception | None = None
            if task is not None:
                if not finished:
                    try:
                        self._call(SEARCH_API, "stop", {"taskid": [task]}, timeout=min(self.timeout, 5))
                    except Exception as error:
                        cleanup_failure = error
                try:
                    self._call(SEARCH_API, "clean", {"taskid": [task]}, timeout=min(self.timeout, 5))
                except Exception as error:
                    cleanup_failure = error
            if cleanup_failure is not None and cancel_event is not None and cancel_event.is_set():
                raise DSMError("无法确认群晖搜索任务已停止或清理，请检查 NAS 连接后重试。") from None
            if cleanup_failure is not None and not primary_failure:
                raise cleanup_failure

    def get_info(self, path: str) -> dict[str, Any]:
        remote = self._path(path)
        data = self._call(LIST_API, "getinfo", {"path": [remote], "additional": ["size", "time", "perm", "type"]})
        files = data.get("files")
        if not isinstance(files, list) or len(files) != 1 or not isinstance(files[0], dict):
            raise DSMError("DSM did not return metadata for the requested file.")
        returned_path = self._path(files[0].get("path"))
        if returned_path != remote:
            raise DSMError("DSM returned metadata for a different file.")
        return dict(files[0], path=returned_path)

    def list_shares(self) -> list[dict[str, str]]:
        """List the account's visible shared folders, including read-only ones."""
        offset = 0
        shares: list[dict[str, str]] = []
        seen: set[str] = set()
        while True:
            data = self._call(LIST_API, "list_share", {
                "offset": offset, "limit": 200, "sort_by": "name",
                "sort_direction": "asc", "onlywritable": False,
            })
            total = _integer(data.get("total"), "shared-folder total", maximum=100000)
            returned_offset = _integer(data.get("offset", offset), "shared-folder offset")
            page = data.get("shares")
            if returned_offset != offset or not isinstance(page, list):
                raise DSMError("DSM returned an invalid shared-folder page.")
            if not page and offset < total:
                raise DSMError("DSM shared-folder pagination did not make progress.")
            for entry in page:
                if not isinstance(entry, dict):
                    raise DSMError("DSM returned invalid shared-folder metadata.")
                path = _shared_root(entry.get("path"))
                name = entry.get("name")
                if (not isinstance(name, str) or not name or any(ord(char) < 32 or 127 <= ord(char) <= 159 for char in name)
                        or path in seen):
                    raise DSMError("DSM returned invalid or repeated shared-folder metadata.")
                seen.add(path)
                shares.append({"name": name, "path": path})
            offset += len(page)
            if offset >= total:
                return shares

    def list_files(self, folder: str | None = None, limit: int = 100, offset: int = 0) -> SearchResult:
        scope = self._path(self.allowed_root if folder is None else folder)
        _integer(limit, "limit", minimum=1)
        _integer(offset, "offset")
        data = self._call(LIST_API, "list", {
            "folder_path": scope, "limit": limit, "offset": offset,
            "additional": ["size", "time", "perm", "type"],
        })
        return self._result(data, folder=scope, offset=offset, finished=True)

    @contextmanager
    def open_content(self, path: str) -> Iterator[tuple[dict[str, Any], Any]]:
        """Open one file's response without storing it on disk.

        The caller must use ``with`` and read bounded chunks. JSON API errors
        are rejected before yielding; a JSON file served as an attachment is
        preserved verbatim. No Range support or mid-stream retry is implied.
        """
        remote = self._path(path)
        info = self.get_info(remote)
        if info.get("isdir") is not False:
            raise DSMError("Only individual files can be opened; directories are refused.")
        endpoint, fields = self._fields(DOWNLOAD_API, "download", {"path": [remote], "mode": "download"})
        response = self._open(endpoint, fields)
        try:
            stream = response
            content_type = response.headers.get("Content-Type", "").split(";", 1)[0].strip().lower()
            disposition = Message()
            disposition["Content-Disposition"] = response.headers.get("Content-Disposition", "")
            is_file = disposition.get_content_disposition() == "attachment" or bool(disposition.get_filename())
            if (content_type == "application/json" or content_type.endswith("+json")) and not is_file:
                try:
                    prefix = response.read(_ERROR_PROBE_BYTES + 1)
                except (OSError, http.client.HTTPException):
                    raise DSMError("The DSM file response could not be read completely.") from None
                if len(prefix) <= _ERROR_PROBE_BYTES:
                    try:
                        envelope = json.loads(prefix)
                    except (ValueError, UnicodeError):
                        envelope = None
                    if (isinstance(envelope, dict) and envelope.get("success") is False
                            and isinstance(envelope.get("error"), dict)):
                        self._failure(envelope, DOWNLOAD_API, "download")
                stream = _PrefixedResponse(response, prefix)
            yield info, stream
        finally:
            response.close()

    def download(self, remote_path: str, destination: str | os.PathLike[str], *, overwrite: bool = False) -> Path:
        """Stream one regular file to a temporary sibling, then publish it.

        This does not request or promise HTTP Range support. A real JSON file
        carrying file Content-Disposition is never interpreted as an API error.
        """
        remote = self._path(remote_path)
        if type(overwrite) is not bool:
            raise DSMError("overwrite must be a boolean.")
        try:
            dest = Path(destination)
            if dest.is_dir():
                raise DSMError("The download destination must be a file path.")
            if not overwrite and os.path.lexists(dest):
                raise DSMError("The destination already exists; use overwrite=True explicitly to replace it.")
            if not dest.parent.is_dir():
                raise DSMError("The download destination directory does not exist.")
        except (OSError, ValueError, TypeError):
            raise DSMError("The local download destination is invalid.") from None
        info = self.get_info(remote)
        if info.get("isdir") is not False:
            raise DSMError("Only individual files can be downloaded; directories are refused.")
        endpoint, fields = self._fields(DOWNLOAD_API, "download", {"path": [remote], "mode": "download"})
        temporary: Path | None = None
        try:
            with self._open(endpoint, fields) as response:
                content_type = response.headers.get("Content-Type", "").split(";", 1)[0].strip().lower()
                disposition = Message()
                disposition["Content-Disposition"] = response.headers.get("Content-Disposition", "")
                is_file_response = disposition.get_content_disposition() == "attachment" or bool(disposition.get_filename())
                maybe_api_error = (content_type == "application/json" or content_type.endswith("+json")) and not is_file_response
                raw_length = response.headers.get("Content-Length")
                length: int | None = None
                if raw_length is not None:
                    if not re.fullmatch(r"[0-9]+", raw_length.strip()):
                        raise DSMError("DSM returned an invalid download length.")
                    length = int(raw_length)
                count = 0
                probe = bytearray()
                with tempfile.NamedTemporaryFile(mode="wb", prefix=".synofind-", suffix=".part", dir=dest.parent, delete=False) as output:
                    temporary = Path(output.name)
                    while True:
                        chunk = response.read(_CHUNK_BYTES)
                        if not chunk:
                            break
                        output.write(chunk)
                        count += len(chunk)
                        if maybe_api_error and len(probe) < _ERROR_PROBE_BYTES + 1:
                            probe.extend(chunk[:_ERROR_PROBE_BYTES + 1 - len(probe)])
                    output.flush()
                    os.fsync(output.fileno())
                if length is not None and count != length:
                    raise DSMError("The file download was truncated; no destination file was replaced.")
                if maybe_api_error and count <= _ERROR_PROBE_BYTES:
                    try:
                        envelope = json.loads(probe)
                    except (ValueError, UnicodeError):
                        envelope = None
                    if isinstance(envelope, dict) and envelope.get("success") is False and isinstance(envelope.get("error"), dict):
                        self._failure(envelope, DOWNLOAD_API, "download")
            if overwrite:
                os.replace(temporary, dest)
            else:
                # A hard link publishes the completed file atomically and refuses
                # an existing target even if it appeared during the download.
                os.link(temporary, dest)
                temporary.unlink()
            temporary = None
            return dest
        except FileExistsError:
            raise DSMError("The destination already exists; it was not overwritten.") from None
        except (OSError, ValueError, http.client.HTTPException):
            raise DSMError("The file download could not be completed; temporary output was discarded.") from None
        finally:
            if temporary is not None:
                try:
                    temporary.unlink(missing_ok=True)
                except OSError:
                    pass
