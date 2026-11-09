"""Local-only browser interface for the official DSM API client."""
from __future__ import annotations

import argparse
from contextlib import contextmanager
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import secrets
import re
import sqlite3
import time
import socket
import sys
import threading
from urllib.parse import parse_qs, quote, urlsplit
from urllib.request import ProxyHandler, build_opener
import webbrowser

from .client import APIError, AuthenticationError, DSMError, SearchTimeout, SearchCancelled
from .desktop_state import DesktopState, _diagnostics, _safe_error
from .local_index import LocalIndex, PREFIX
from .local_paths import open_local_content, resolve_local_path, reveal_local_file

VERSION = "0.5.0"
WEB_ROOT = Path(__file__).with_name("desktop_web")
COOKIE = "synofind_desktop"
TEXT_LIMIT = 2 * 1024 * 1024
IMAGE_LIMIT = 10 * 1024 * 1024
PDF_LIMIT = 20 * 1024 * 1024
TEXT_EXTENSIONS = {".txt", ".md", ".csv", ".log", ".json", ".yaml", ".yml", ".toml", ".ini", ".xml", ".py", ".go", ".js", ".ts", ".css"}
IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp"}


class RequestError(Exception):
    def __init__(self, status, message):
        self.status, self.message = status, message


class DesktopHTTPServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = False

    def __init__(self, port, state):
        self.state = state
        self.cookie_token = secrets.token_urlsafe(32)
        self.csrf_token = secrets.token_urlsafe(32)
        self.operation_lock = threading.Lock()
        self.search_guard = threading.Lock()
        self.search_jobs = {}
        self.cancelled_searches = {}
        self.active_search = None
        self.running_nas = None
        super().__init__(("127.0.0.1", port), DesktopHandler)
        self.local = LocalIndex(state.store.data_dir) if hasattr(state, 'store') else None
        if self.local:
            self.local.start()

    def begin_search(self, search_id):
        job = {'cancel': threading.Event(), 'done': threading.Event(), 'error': ''}
        with self.search_guard:
            if search_id in self.cancelled_searches:
                job['cancel'].set()
            elif self.active_search is not None:
                self.active_search['cancel'].set()
            if not job['cancel'].is_set():
                self.active_search = job
                if self.running_nas and not self.running_nas['done'].is_set():
                    self.running_nas['cancel'].set()
            self.search_jobs[search_id] = job
            for key in list(self.search_jobs):
                if len(self.search_jobs) <= 64:
                    break
                if self.search_jobs[key]['done'].is_set():
                    del self.search_jobs[key]
        return job

    def cancel_search(self, search_id):
        if not isinstance(search_id, str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,80}', search_id):
            raise RequestError(400, '搜索标识不正确。')
        with self.search_guard:
            self.cancelled_searches[search_id] = time.monotonic()
            self.cancelled_searches = {key: value for key, value in self.cancelled_searches.items() if time.monotonic()-value < 300}
            job = self.search_jobs.get(search_id)
            if job:
                job['cancel'].set()
        if not job:
            return {'stopped': True, 'error': ''}
        job['done'].wait(.15)
        return {'stopped': job['done'].is_set(), 'error': job['error']}

    def server_close(self):
        with self.search_guard:
            for job in self.search_jobs.values():
                job['cancel'].set()
        if getattr(self, 'local', None):
            self.local.close()
        super().server_close()

    def status(self):
        return dict(self.state.status(), local_sources=self.local.sources() if self.local else [])

    def server_bind(self):
        if hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
            self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        super().server_bind()

    @property
    def url(self):
        return f"http://127.0.0.1:{self.server_port}"


class DesktopHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "SynoFindDesktop"
    sys_version = ""

    def setup(self):
        super().setup()
        self.connection.settimeout(30)

    def log_message(self, *args):
        # Access URLs include private filenames. Do not log them or request bodies.
        pass

    def _security_headers(self, *, pdf=False):
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "SAMEORIGIN" if pdf else "DENY")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Cache-Control", "no-store")
        ancestors = "'self'" if pdf else "'none'"
        self.send_header("Content-Security-Policy", "default-src 'none'; script-src 'self' 'wasm-unsafe-eval'; worker-src 'self' blob:; style-src 'self'; img-src 'self' blob: data:; frame-src 'self' blob:; connect-src 'self'; font-src 'self' blob:; base-uri 'none'; form-action 'self'; frame-ancestors " + ancestors + "; object-src 'none'")
        self.send_header("Permissions-Policy", "camera=(), microphone=(), geolocation=()")

    def _send(self, status, data, content_type="application/json; charset=utf-8", *, cookie=False):
        if not isinstance(data, bytes):
            data = json.dumps(data, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        self.send_response(status)
        self._security_headers(pdf=content_type == "application/pdf")
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        if cookie:
            self.send_header("Set-Cookie", f"{COOKIE}={self.server.cookie_token}; Path=/; HttpOnly; SameSite=Strict")
        self.end_headers()
        self._headers_sent = True
        self.wfile.write(data)

    def _error(self, status, message, code=None, api=None, method=None):
        error = {"message": message}
        if code is not None:
            error["code"] = code
        if api in {"SYNO.API.Auth", "SYNO.API.Info", "SYNO.FileStation.List", "SYNO.FileStation.Search", "SYNO.FileStation.Download"}:
            error["api"] = api
        if method in {"login", "logout", "query", "list", "list_share", "getinfo", "start", "stop", "clean", "download"}:
            error["method"] = method
        self._send(status, {"error": error})

    def _host(self):
        hosts = self.headers.get_all("Host", [])
        allowed = {f"127.0.0.1:{self.server.server_port}", f"localhost:{self.server.server_port}"}
        if len(hosts) != 1 or hosts[0] not in allowed:
            raise RequestError(403, "只能通过本机地址访问。")
        return hosts[0]

    def _authorize(self, *, csrf=True):
        host = self._host()
        origin = self.headers.get("Origin")
        if origin is not None and origin != "http://" + host:
            raise RequestError(403, "已拒绝来自其他网站的请求。")
        if self.headers.get("Sec-Fetch-Site", "none") not in {"same-origin", "none"}:
            raise RequestError(403, "已拒绝来自其他网站的请求。")
        cookies = SimpleCookie()
        try:
            cookies.load(self.headers.get("Cookie", ""))
            token = cookies[COOKIE].value
        except (KeyError, ValueError):
            raise RequestError(401, "请重新打开 SynoFind 本机网页。") from None
        if not secrets.compare_digest(token, self.server.cookie_token):
            raise RequestError(401, "页面会话已失效，请刷新网页。")
        if csrf and not secrets.compare_digest(self.headers.get("X-SynoFind-CSRF", ""), self.server.csrf_token):
            raise RequestError(403, "页面校验失败，请刷新网页。")

    def _body(self):
        if self.headers.get("Transfer-Encoding"):
            raise RequestError(400, "不支持此请求格式。")
        if self.headers.get("Content-Type", "").split(";", 1)[0].strip().lower() != "application/json":
            raise RequestError(415, "请求必须使用 JSON。")
        lengths = self.headers.get_all("Content-Length", [])
        if len(lengths) != 1 or not lengths[0].isdigit():
            raise RequestError(411, "请求缺少有效的长度。")
        length = int(lengths[0])
        if length > 64 * 1024:
            raise RequestError(413, "提交的内容过大。")
        try:
            data = json.loads(self.rfile.read(length))
        except (ValueError, UnicodeError):
            raise RequestError(400, "无法读取提交的内容。") from None
        if not isinstance(data, dict):
            raise RequestError(400, "请求内容格式不正确。")
        return data

    @contextmanager
    def _operation(self):
        if not self.server.operation_lock.acquire(blocking=False):
            raise RequestError(409, "上一个查询或下载正在进行，请稍后再试。")
        try:
            yield
        finally:
            self.server.operation_lock.release()

    def _route(self):
        try:
            parsed = urlsplit(self.path)
            if parsed.scheme or parsed.netloc:
                raise ValueError()
            query = parse_qs(parsed.query, keep_blank_values=True, encoding="utf-8", errors="strict", max_num_fields=30)
            if any(len(value) != 1 for value in query.values()):
                raise ValueError()
            return parsed.path, {key: value[0] for key, value in query.items()}
        except (ValueError, UnicodeError):
            raise RequestError(400, "请求地址格式不正确。") from None

    def _search(self, query):
        search_id = query.get('search_id') or secrets.token_hex(16)
        if not re.fullmatch(r'[A-Za-z0-9_-]{1,80}', search_id):
            raise RequestError(400, '搜索标识不正确。')
        job = self.server.begin_search(search_id)
        try:
            self._search_job(query, job)
        except SearchCancelled:
            self._send(200, {'files': [], 'total': 0, 'offset': 0, 'finished': True, 'cancelled': True})
        except Exception as error:
            if job['cancel'].is_set() and not isinstance(error, (BrokenPipeError, ConnectionResetError, ConnectionAbortedError)):
                job['error'] = '未能确认后台查询已停止，请检查 NAS 连接。'
            raise
        finally:
            job['done'].set()

    @contextmanager
    def _search_operation(self, job):
        deadline = time.monotonic() + 20
        previous = self.server.running_nas
        while previous and not previous['done'].wait(.1):
            if job['cancel'].is_set():
                raise SearchCancelled('搜索已停止。')
            if time.monotonic() >= deadline:
                raise RequestError(409, '仍在等待群晖停止上次查询，请稍后重试。')
        while not self.server.operation_lock.acquire(timeout=.1):
            if job['cancel'].is_set():
                raise SearchCancelled('搜索已停止。')
            running = self.server.running_nas
            if not running or running['done'].is_set() or time.monotonic() >= deadline:
                raise RequestError(409, '另一个文件操作尚未结束，请稍后再试。')
        try:
            if job['cancel'].is_set():
                raise SearchCancelled('搜索已停止。')
            if previous and previous.get('error'):
                self.server.running_nas = None
                raise RequestError(409, '上次 NAS 查询未确认停止，请检查连接后重试。')
            self.server.running_nas = job
            yield
        finally:
            self.server.operation_lock.release()

    def _search_job(self, query, job):
        params = {"pattern": query.get("pattern", ""), "extension": query.get("extension") or None,
                  "limit": 50, "offset": 0, "sort_by": query.get("sort_by", "name"),
                  "descending": query.get("descending", "false") == "true"}
        if query.get("descending", "false") not in {"true", "false"}:
            raise RequestError(400, "排序参数不正确。")
        try:
            for key in ("limit", "offset", "min_size", "max_size", "modified_after", "modified_before"):
                if query.get(key, "") != "":
                    params[key] = int(query[key])
                    if params[key] < 0:
                        raise ValueError()
            if not 1 <= params["limit"] <= 200:
                raise ValueError()
        except ValueError:
            raise RequestError(400, "筛选或分页参数不正确。") from None
        if params['sort_by'] not in {'name', 'size', 'mtime'}:
            raise RequestError(400, '排序方式不正确。')
        if params['offset'] > 10000:
            raise RequestError(400, '请缩小搜索范围后查看更深的结果。')
        scope = query.get('source', 'all')
        local = self.server.local
        sources = local.sources() if local else []
        if scope not in {'all', 'nas', 'local'} and scope not in {item['id'] for item in sources}:
            raise RequestError(400, '请选择现有的数据源。')
        connected = self.server.state.status()['connected']
        use_local = bool(sources) and scope != 'nas'
        use_nas = scope in {'all', 'nas'} and connected
        if scope == 'nas' and not connected:
            raise RequestError(400, '请先连接 NAS，或选择本机数据源。')
        count, offset = params['limit'], params['offset']
        files, total, warning = [], 0, ''
        # Fetch each source prefix, then page their combined ordered result.
        merged = use_local and use_nas
        prefix_params = dict(params, limit=offset+count, offset=0) if merged else params
        prefix_params['cancel_event'] = job['cancel']
        if use_local:
            try:
                files, total = local.search(prefix_params, None if scope in {'all', 'local'} else scope)
            except sqlite3.OperationalError:
                if job['cancel'].is_set():
                    raise SearchCancelled('搜索已停止。') from None
                raise
        if use_nas:
            try:
                with self._search_operation(job):
                    result = self.server.state.search(prefix_params)
                    profile = self.server.state.status()['profile']
                for entry in result.files:
                    item = dict(entry, source_kind='nas', source_name='NAS', display_path=entry['path'])
                    item['smb_path'] = '\\\\' + profile.get('smb_host', '') + item['path'].replace('/', '\\')
                    files.append(item)
                total += result.total
            except (DSMError, RequestError) as error:
                if job["cancel"].is_set() or not use_local:
                    raise
                warning = 'NAS 查询未完成，本次仅显示本机结果。' + (_safe_error(error) if isinstance(error, DSMError) else error.message)
        if merged:
            key = params['sort_by']
            def order(item):
                if key == 'name':
                    return item.get('name', '').casefold()
                extra = item.get('additional', {})
                return (extra.get('size', 0) if key == 'size' else extra.get('time', {}).get('mtime', 0)) or 0
            files.sort(key=order, reverse=params['descending'])
            files = files[offset:offset+count]
        if job['cancel'].is_set():
            raise SearchCancelled('搜索已停止。')
        self._send(200, {'files': files, 'total': total, 'offset': offset, 'finished': True, 'warning': warning})

    def _info(self, path):
        if path.startswith(PREFIX):
            if not self.server.local:
                raise RequestError(404, '本机数据源不可用。')
            return self.server.local.info(path)
        return self.server.state.get_info(path)

    def _file_profile(self, path):
        return self.server.local.profile(path) if path.startswith(PREFIX) and self.server.local else self.server.state.status()['profile']

    @contextmanager
    def _file_operation(self, path):
        if path.startswith(PREFIX):
            yield
        else:
            with self._operation():
                yield

    @contextmanager
    def _preview_content(self, path):
        if path.startswith(PREFIX):
            if not self.server.local:
                raise RequestError(404, '本机数据源不可用。')
            with self.server.local.content(path) as content:
                yield content
            return
        profile = self.server.status()["profile"]
        if profile.get("local_root"):
            info = self.server.state.get_info(path)
            if info.get("isdir"):
                raise RequestError(400, "请选择文件进行预览。")
            with open_local_content(profile, path) as response:
                info = dict(info, additional=dict(info.get("additional", {}), size=int(response.headers["Content-Length"])))
                yield info, response
        else:
            with self.server.state.open_content(path) as content:
                yield content

    def _preview(self, path):
        extension = Path(path).suffix.lower()
        if extension not in TEXT_EXTENSIONS | IMAGE_EXTENSIONS | {".pdf"}:
            raise RequestError(415, "此格式暂不支持网页预览，请使用本机预览工具或打开所在文件夹。")
        limit = PDF_LIMIT if extension == ".pdf" else TEXT_LIMIT if extension in TEXT_EXTENSIONS else IMAGE_LIMIT
        with self._file_operation(path), self._preview_content(path) as (info, response):
            declared = info.get("additional", {}).get("size")
            if isinstance(declared, int) and declared > limit:
                raise RequestError(413, "文件超过网页预览限制，请使用本机预览工具。")
            data = response.read(limit + 1)
            if len(data) > limit:
                raise RequestError(413, "文件超过网页预览限制，请使用本机预览工具。")
            expected = response.headers.get("Content-Length")
            if expected and (not expected.isdigit() or int(expected) != len(data)):
                raise RequestError(502, "文件传输不完整，请重试。")
        if extension == ".pdf":
            if not data.startswith(b"%PDF-"):
                raise RequestError(415, "文件内容不是有效的 PDF。")
            self._send(200, data, "application/pdf")
            return
        if extension in TEXT_EXTENSIONS:
            try:
                if data.startswith((b"\xff\xfe", b"\xfe\xff")):
                    text = data.decode("utf-16")
                else:
                    try:
                        text = data.decode("utf-8-sig")
                    except UnicodeError:
                        text = data.decode("gb18030")
                if "\x00" in text:
                    raise UnicodeError()
            except UnicodeError:
                raise RequestError(415, "无法作为纯文本预览，请使用本机预览工具。") from None
            self._send(200, text.encode("utf-8"), "text/plain; charset=utf-8")
            return
        mime = None
        if data.startswith(b"\x89PNG\r\n\x1a\n"):
            mime = "image/png"
        elif data.startswith(b"\xff\xd8\xff"):
            mime = "image/jpeg"
        elif data.startswith((b"GIF87a", b"GIF89a")):
            mime = "image/gif"
        elif data.startswith(b"RIFF") and data[8:12] == b"WEBP":
            mime = "image/webp"
        elif data.startswith(b"BM"):
            mime = "image/bmp"
        if mime is None:
            raise RequestError(415, "文件内容不是支持的图片格式。")
        self._send(200, data, mime)

    def _download(self, path):
        with self._file_operation(path), (self.server.local.content(path) if path.startswith(PREFIX) and self.server.local else self.server.state.open_content(path)) as (info, response):
            length = response.headers.get("Content-Length")
            if length is not None and not length.isdigit():
                raise RequestError(502, "群晖返回了无效的文件长度。")
            self.send_response(200)
            self._security_headers()
            self.send_header("Content-Type", "application/octet-stream")
            filename = str(info.get("name") or "download")
            self.send_header("Content-Disposition", "attachment; filename=download; filename*=UTF-8''" + quote(filename, safe=""))
            if length is not None:
                self.send_header("Content-Length", length)
            else:
                self.send_header("Connection", "close")
                self.close_connection = True
            self.end_headers()
            self._headers_sent = True
            total = 0
            while True:
                chunk = response.read(1024 * 1024)
                if not chunk:
                    break
                self.wfile.write(chunk)
                total += len(chunk)
            if length is not None and total != int(length):
                self.close_connection = True

    def do_GET(self):
        self._dispatch(False)

    def do_POST(self):
        self._dispatch(True)

    def _dispatch(self, post):
        self._headers_sent = False
        try:
            self._host()
            route, query = self._route()
            if not post and route == "/healthz":
                self._send(200, {"application": "SynoFindDesktop", "version": VERSION})
                return
            if not post and route in {"/", "/index.html", "/app.js", "/styles.css"}:
                name = "index.html" if route in {"/", "/index.html"} else route[1:]
                mime = {"index.html": "text/html; charset=utf-8", "app.js": "text/javascript; charset=utf-8", "styles.css": "text/css; charset=utf-8"}[name]
                self._send(200, (WEB_ROOT / name).read_bytes(), mime, cookie=name == "index.html")
                return
            if not post and route.startswith("/vendor/"):
                vendor_root = (WEB_ROOT / "vendor").resolve()
                asset = (WEB_ROOT / route.lstrip("/")).resolve()
                types = {".mjs": "text/javascript; charset=utf-8", ".js": "text/javascript; charset=utf-8",
                         ".wasm": "application/wasm", ".bcmap": "application/octet-stream", ".pfb": "application/octet-stream",
                         ".ttf": "font/ttf", ".otf": "font/otf", ".icc": "application/octet-stream"}
                if not asset.is_relative_to(vendor_root) or asset.suffix not in types or not asset.is_file():
                    raise RequestError(404, "未找到此页面资源。")
                self._send(200, asset.read_bytes(), types[asset.suffix])
                return
            if not post and route == "/api/session":
                self._authorize(csrf=False)
                self._send(200, {"csrf_token": self.server.csrf_token, "status": self.server.status()})
                return
            content_route = not post and route in {"/api/files/download", "/api/files/preview"}
            self._authorize(csrf=not content_route)
            if post:
                data = self._body()
                if route == '/api/search/cancel':
                    self._send(200, self.server.cancel_search(data.get('search_id', '')))
                elif route in {'/api/sources/add', '/api/sources/remove', '/api/sources/scan'}:
                    if not self.server.local:
                        raise RequestError(400, '本机数据源不可用。')
                    if route.endswith('/add'):
                        self.server.local.add(data.get('root', ''), data.get('preview_mode', 'web'), data.get('preview_executable', ''))
                    elif route.endswith('/remove'):
                        self.server.local.remove(data.get('id', ''))
                    else:
                        self.server.local.wake.set()
                    self._send(200, self.server.status())
                elif route == "/api/connection/prepare":
                    profile = {key: data.get(key, "") for key in ("url", "username", "smb_host")}
                    with self._operation():
                        result = self.server.state.prepare_connection(profile, data.get("password", ""), otp_code=data.get("otp_code") or None)
                    self._send(200, result)
                elif route == "/api/connection/commit":
                    if type(data.get("remember", False)) is not bool:
                        raise RequestError(400, "保存设置格式不正确。")
                    with self._operation():
                        result = self.server.state.commit_connection(data.get("selection_id", ""), data.get("root", ""), remember=data.get("remember", False), smb_host=data.get("smb_host"), **{key: data.get(key) for key in ("local_root", "preview_mode", "preview_executable")})
                    self._send(200, result)
                elif route == "/api/connection/cancel":
                    with self._operation():
                        self.server.state.cancel_connection(data.get("selection_id", ""))
                    self._send(200, {"cancelled": True})
                elif route == "/api/library":
                    with self._operation():
                        result = self.server.state.select_library(data.get("root", ""), smb_host=data.get("smb_host"), **{key: data.get(key) for key in ("local_root", "preview_mode", "preview_executable")})
                    self._send(200, result)
                elif route in {"/api/files/reveal", "/api/files/preview-native"}:
                    path = data.get("path", "")
                    with self._file_operation(path):
                        info = self._info(path)
                        profile = self._file_profile(path)
                        if route == "/api/files/reveal":
                            reveal_local_file(profile, path)
                        else:
                            if info.get("isdir"):
                                raise RequestError(400, "请选择文件进行预览。")
                            from .native_preview import launch_preview
                            local_path, _ = resolve_local_path(profile, path)
                            launch_preview(local_path, profile.get("preview_mode", "web"), profile.get("preview_executable", ""))
                    self._send(200, {"opened": True})
                elif route == "/api/connect":
                    profile = {key: data.get(key, "") for key in ("url", "username", "root", "smb_host", "local_root", "preview_executable")}
                    profile["preview_mode"] = data.get("preview_mode", "web")
                    if type(data.get("remember", False)) is not bool:
                        raise RequestError(400, "保存设置格式不正确。")
                    with self._operation():
                        result = self.server.state.connect(profile, data.get("password", ""), otp_code=data.get("otp_code") or None, remember=data.get("remember", False))
                    self._send(200, result)
                elif route == "/api/reconnect":
                    with self._operation():
                        result = self.server.state.reconnect()
                    self._send(200, result)
                elif route == "/api/disconnect":
                    if type(data.get("forget", False)) is not bool:
                        raise RequestError(400, "连接设置格式不正确。")
                    with self._operation():
                        result = self.server.state.disconnect(forget=data.get("forget", False))
                    self._send(200, result)
                elif route == "/api/shutdown":
                    with self._operation():
                        self._send(200, {"stopped": True})
                        threading.Thread(target=self.server.shutdown, daemon=True).start()
                else:
                    raise RequestError(404, "未找到此功能。")
                return
            if route == "/api/status":
                self._send(200, self.server.status())
            elif route == "/api/shares":
                with self._operation():
                    result = self.server.state.list_shares()
                self._send(200, {"shares": result})
            elif route == "/api/search":
                self._search(query)
            elif route == "/api/files/info":
                with self._file_operation(query.get('path', '')):
                    result = self._info(query.get("path", ""))
                self._send(200, result)
            elif route == "/api/files/download":
                self._download(query.get("path", ""))
            elif route == "/api/files/preview":
                self._preview(query.get("path", ""))
            else:
                raise RequestError(404, "未找到此功能。")
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError, socket.timeout):
            self.close_connection = True
        except Exception as error:
            if self._headers_sent:
                self.close_connection = True
                return
            # Close on error: an early rejection may leave unread request bytes.
            self.close_connection = True
            if isinstance(error, RequestError):
                self._error(error.status, error.message)
            elif isinstance(error, AuthenticationError):
                self._error(401, _safe_error(error), *_diagnostics(error))
            elif isinstance(error, SearchTimeout):
                self._error(504, "群晖查询超时，请缩小范围或稍后重试。")
            elif isinstance(error, APIError):
                self._error(502, _safe_error(error), *_diagnostics(error))
            elif isinstance(error, DSMError):
                self._error(400, str(error))
            else:
                self._error(500, "操作未完成，请检查连接或重新启动 SynoFind。")


def default_data_dir():
    base = Path(os.environ.get("LOCALAPPDATA", str(Path.home() / ".local" / "share")))
    return base / "SynoFind" / "Desktop"


def main(argv=None):
    parser = argparse.ArgumentParser(description="SynoFind 本机网页搜索工具")
    parser.add_argument("--port", type=int, default=8788)
    parser.add_argument("--data-dir", type=Path, default=default_data_dir())
    parser.add_argument("--no-browser", action="store_true")
    args = parser.parse_args(argv)
    if not 0 <= args.port <= 65535:
        parser.error("端口必须在 0 到 65535 之间。")
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    state = DesktopState(args.data_dir)
    try:
        server = DesktopHTTPServer(args.port, state)
    except OSError:
        url = f"http://127.0.0.1:{args.port}"
        try:
            with build_opener(ProxyHandler({})).open(url + "/healthz", timeout=2) as response:
                existing = json.load(response)
            if existing.get("application") != "SynoFindDesktop":
                raise ValueError()
            if not args.no_browser:
                webbrowser.open(url)
            print("SynoFind 已在运行：" + url)
            return 0
        except Exception:
            print("端口已被其他程序占用，请用 --port 指定其他端口。", file=sys.stderr)
            return 1
    print("SynoFind 本机网页：" + server.url, flush=True)
    print("关闭此窗口或在网页中选择“退出应用”即可停止服务。", flush=True)

    def restore():
        try:
            with server.operation_lock:
                state.reconnect()
        except Exception:
            # Safe connection state is surfaced by /api/status.
            pass

    threading.Thread(target=restore, daemon=True).start()
    if not args.no_browser:
        threading.Thread(target=lambda: webbrowser.open(server.url), daemon=True).start()
    try:
        server.serve_forever(poll_interval=0.2)
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
        state.stop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
