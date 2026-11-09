"""Exercise the local browser boundary without real NAS credentials."""
from contextlib import contextmanager
import http.client
from http.cookies import SimpleCookie
import io
import json
from pathlib import Path
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch
from urllib.parse import quote, urlencode

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from synofind_dsm import APIError, AuthenticationError, DSMError, SearchResult
from synofind_dsm.desktop import DesktopHTTPServer, IMAGE_LIMIT, TEXT_LIMIT


class FakeState:
    def __init__(self):
        self.connected = True
        self.profile = {"url": "https://nas.example.test:5001", "username": "readonly",
                        "root": "/Wonvy", "smb_host": "10.0.0.10"}
        self.content = b"hello"
        self.calls = []
        self.last_response = None
        self.filename = "合同.txt"

    def status(self):
        return {"configured": True, "connected": self.connected, "remembered": False,
                "can_remember": True, "platform": "windows", "profile": dict(self.profile), "last_error": None}

    def connect(self, profile, password, otp_code=None, remember=False):
        self.calls.append(("connect", profile, password, otp_code, remember))
        if password != "test-password":
            raise AuthenticationError(400)
        self.profile = dict(profile)
        self.connected = True
        return self.status()

    def disconnect(self, forget=False):
        self.calls.append(("disconnect", forget))
        self.connected = False
        return self.status()

    def reconnect(self):
        self.connected = True
        return self.status()

    def list_shares(self):
        return [{"name": "资料", "path": "/资料", "local_root": "", "preview_mode": "web", "preview_executable": ""}]

    def prepare_connection(self, profile, password, otp_code=None):
        self.calls.append(("prepare", profile, password, otp_code))
        return {"selection_id": "synthetic-selection", "shares": self.list_shares(), "profile": dict(profile, root=""), "expires_in": 300}

    def commit_connection(self, selection_id, root, **options):
        self.calls.append(("commit", selection_id, root, options))
        self.profile["root"] = root
        return self.status()

    def cancel_connection(self, selection_id):
        self.calls.append(("cancel", selection_id))

    def select_library(self, root, **options):
        self.calls.append(("select_library", root, options))
        self.profile["root"] = root
        return self.status()

    def stop(self):
        self.connected = False

    def search(self, params):
        self.calls.append(("search", params))
        return SearchResult([self.get_info("/Wonvy/合同.txt")], 1, params["offset"], True)

    def get_info(self, path):
        if not path.startswith("/Wonvy/") or "/../" in path:
            raise DSMError("The remote path is outside allowed_root.")
        return {"path": path, "name": self.filename, "isdir": False,
                "additional": {"size": len(self.content), "time": {"mtime": 1730000000}}}

    @contextmanager
    def open_content(self, path):
        self.calls.append(("open_content", path))
        info = self.get_info(path)
        response = io.BytesIO(self.content)
        response.headers = {"Content-Length": str(len(self.content))}
        self.last_response = response
        try:
            yield info, response
        finally:
            response.close()


class DesktopTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = DesktopHTTPServer(0, FakeState())
        cls.thread = threading.Thread(target=cls.server.serve_forever, kwargs={"poll_interval": .02}, daemon=True)
        cls.thread.start()
        cls.port = cls.server.server_port

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join(2)

    def setUp(self):
        self.server.state = FakeState()
        status, headers, _ = self.request("GET", "/", authenticated=False)
        self.assertEqual(status, 200)
        self.cookie = headers["Set-Cookie"].split(";", 1)[0]
        status, _, body = self.request("GET", "/api/session", csrf=False)
        self.assertEqual(status, 200)
        self.csrf = json.loads(body)["csrf_token"]

    def request(self, method, path, body=None, *, authenticated=True, csrf=True, headers=None):
        selected = {"Host": f"127.0.0.1:{self.port}"}
        if authenticated and hasattr(self, "cookie"):
            selected["Cookie"] = self.cookie
        if csrf and hasattr(self, "csrf"):
            selected["X-SynoFind-CSRF"] = self.csrf
        if isinstance(body, dict):
            body = json.dumps(body).encode()
            selected["Content-Type"] = "application/json"
        selected.update(headers or {})
        connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        try:
            connection.request(method, path, body, selected)
            response = connection.getresponse()
            return response.status, dict(response.getheaders()), response.read()
        finally:
            connection.close()

    def test_web_page_contains_no_dsm_secret_and_security_headers(self):
        status, headers, body = self.request("GET", "/", authenticated=False)
        self.assertEqual(status, 200)
        self.assertIn("HttpOnly", headers["Set-Cookie"])
        self.assertIn("SameSite=Strict", headers["Set-Cookie"])
        self.assertIn("frame-ancestors 'none'", headers["Content-Security-Policy"])
        self.assertNotIn(b"test-password", body)

    @unittest.skipUnless(sys.platform == 'win32', 'Windows local folders')
    def test_local_sources_unified_search_offline_preview_and_removal(self):
        from synofind_dsm.local_index import LocalIndex
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / 'files'
            root.mkdir()
            (root / '合同 Local.txt').write_text('local fixture', encoding='utf-8')
            self.server.local = LocalIndex(Path(directory) / 'state')
            try:
                status, _, _ = self.request('POST', '/api/sources/add', {'root': str(root)}, csrf=False)
                self.assertEqual(status, 403)
                status, _, _ = self.request('POST', '/api/sources/add', {'root': str(root)})
                self.assertEqual(status, 200)
                self.server.local.scan()
                status, _, body = self.request('GET', '/api/search?'+urlencode({'pattern':'合同'}))
                result = json.loads(body)
                self.assertEqual(status, 200)
                self.assertEqual(result['total'], 2)
                self.assertEqual({f['source_kind'] for f in result['files']}, {'local', 'nas'})
                local = next(f for f in result['files'] if f['source_kind'] == 'local')
                self.server.state.connected = False
                status, _, body = self.request('GET', '/api/search?'+urlencode({'pattern':'合同'}))
                self.assertEqual(json.loads(body)['total'], 1)
                status, _, content = self.request('GET', '/api/files/preview?'+urlencode({'path':local['path']}), csrf=False)
                self.assertEqual((status, content), (200, b'local fixture'))
                sid = self.server.local.sources()[0]['id']
                status, _, _ = self.request('POST', '/api/sources/remove', {'id':sid})
                self.assertEqual(status, 200)
                status, _, _ = self.request('GET', '/api/files/info?'+urlencode({'path':local['path']}))
                self.assertEqual(status, 400)
                self.assertTrue((root / '合同 Local.txt').is_file())
            finally:
                self.server.local.close()
                self.server.local = None

    def test_host_header_prevents_dns_rebinding(self):
        status, _, _ = self.request("GET", "/api/session", headers={"Host": "attacker.example"}, csrf=False)
        self.assertEqual(status, 403)

    def test_browser_session_required_for_api(self):
        status, _, _ = self.request("GET", "/api/status", authenticated=False)
        self.assertEqual(status, 401)

    def test_session_does_not_accept_cross_site_fetch(self):
        status, _, _ = self.request("GET", "/api/session", csrf=False,
                                    headers={"Sec-Fetch-Site": "cross-site"})
        self.assertEqual(status, 403)

    def test_write_requires_csrf(self):
        status, _, _ = self.request("POST", "/api/disconnect", {"forget": True}, csrf=False)
        self.assertEqual(status, 403)

    def test_stop_and_replacement_cancel_old_backend_search(self):
        from synofind_dsm.client import SearchCancelled
        started = threading.Event()
        normal = self.server.state.search
        stopped = []
        def slow(params):
            if params['pattern'] == 'slow':
                started.set()
                if params['cancel_event'].wait(3):
                    stopped.append(True)
                    raise SearchCancelled('搜索已停止。')
            return normal(params)
        self.server.state.search = slow
        for replacement in (False, True, 'local'):
            started.clear()
            search_id = 'old-' + str(replacement)
            thread = threading.Thread(target=lambda: self.request('GET', '/api/search?pattern=slow&search_id='+search_id))
            thread.start()
            self.assertTrue(started.wait(2))
            if replacement:
                if replacement == 'local':
                    self.request('GET', '/api/search?source=local&search_id=local-between')
                status, _, body = self.request('GET', '/api/search?pattern=new&search_id=new-search')
                self.assertEqual(status, 200)
                self.assertEqual(json.loads(body)['total'], 1)
            else:
                status, _, body = self.request('POST', '/api/search/cancel', {'search_id':search_id})
                self.assertEqual(status, 200)
                self.assertTrue(json.loads(body)['stopped'])
            thread.join(2)
            self.assertFalse(thread.is_alive())
        self.assertEqual(len(stopped), 3)

    def test_cancel_before_request_arrives_does_not_start_nas(self):
        self.request('POST', '/api/search/cancel', {'search_id':'cancel-first'})
        status, _, body = self.request('GET', '/api/search?pattern=none&search_id=cancel-first')
        self.assertEqual(status, 200)
        self.assertTrue(json.loads(body)['cancelled'])
        self.assertFalse(any(call[0] == 'search' for call in self.server.state.calls))
        self.assertEqual(self.server.state.calls, [])

    def test_cross_origin_request_rejected_even_with_valid_token(self):
        status, _, _ = self.request("POST", "/api/disconnect", {"forget": True},
                                    headers={"Origin": "https://attacker.example"})
        self.assertEqual(status, 403)
        self.assertEqual(self.server.state.calls, [])

    def test_same_origin_connection_does_not_return_password(self):
        body = dict(self.server.state.profile, password="test-password", otp_code="123456", remember=True)
        status, _, response = self.request("POST", "/api/connect", body,
                                           headers={"Origin": f"http://127.0.0.1:{self.port}"})
        self.assertEqual(status, 200)
        self.assertNotIn(b"test-password", response)
        self.assertNotIn(b"123456", response)

    def test_invalid_login_has_user_readable_error(self):
        body = dict(self.server.state.profile, password="invalid-secret")
        status, _, response = self.request("POST", "/api/connect", body)
        self.assertEqual(status, 401)
        self.assertEqual(json.loads(response)["error"]["code"], 400)
        self.assertNotIn(b"invalid-secret", response)

    def test_file_station_error_preserves_stage_without_disclosing_server_text(self):
        error = APIError(408, "SYNO.FileStation.List", "list", "private-server-detail")
        with patch.object(self.server.state, "connect", side_effect=error):
            status, _, response = self.request("POST", "/api/connect", dict(self.server.state.profile, password="test-password"))
        self.assertEqual(status, 502)
        details = json.loads(response)["error"]
        self.assertEqual(details["code"], 408)
        self.assertEqual(details["api"], "SYNO.FileStation.List")
        self.assertEqual(details["method"], "list")
        self.assertIn("路径", details["message"])
        self.assertNotIn(b"private-server-detail", response)

    def test_unknown_diagnostics_are_not_exposed(self):
        error = APIError(407, "private-server-api", "private-server-method", "private-server-detail")
        with patch.object(self.server.state, "connect", side_effect=error):
            status, _, response = self.request("POST", "/api/connect", dict(self.server.state.profile, password="test-password"))
        self.assertEqual(status, 502)
        self.assertNotIn(b"private-server", response)
        self.assertNotIn("api", json.loads(response)["error"])

    def test_two_stage_connection_lists_shares_before_selecting(self):
        body = dict(self.server.state.profile, password="synthetic-password", otp_code="654321")
        status, _, response = self.request("POST", "/api/connection/prepare", body)
        self.assertEqual(status, 200)
        data = json.loads(response)
        self.assertEqual(data["profile"]["root"], "")
        self.assertEqual(data["shares"][0]["path"], "/资料")
        self.assertNotIn(b"synthetic-password", response)
        self.assertNotIn(b"654321", response)
        status, _, response = self.request("POST", "/api/connection/commit", {
            "selection_id": data["selection_id"], "root": "/资料", "remember": True, "local_root": "W:\\", "preview_mode": "web"})
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(response)["profile"]["root"], "/资料")
        self.assertEqual(self.server.state.calls[-1][3]["local_root"], "W:\\")

    def test_share_selection_and_cancel_require_csrf(self):
        for route in ("/api/connection/prepare", "/api/connection/commit", "/api/connection/cancel", "/api/library", "/api/files/reveal", "/api/files/preview-native"):
            with self.subTest(route=route):
                status, _, _ = self.request("POST", route, {}, csrf=False)
                self.assertEqual(status, 403)
        self.assertEqual(self.server.state.calls, [])

    def test_shares_and_existing_library_selection(self):
        status, _, response = self.request("GET", "/api/shares")
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(response)["shares"][0]["path"], "/资料")
        status, _, response = self.request("POST", "/api/library", {"root": "/资料", "preview_mode": "seer", "local_root": "W:\\"})
        self.assertEqual(status, 200)
        self.assertEqual(self.server.state.calls[-1][2]["preview_mode"], "seer")
        status, _, response = self.request("POST", "/api/connection/cancel", {"selection_id": "synthetic-selection"})
        self.assertEqual(status, 200)
        self.assertTrue(json.loads(response)["cancelled"])

    def test_reveal_uses_validated_nas_path_before_launching(self):
        with patch("synofind_dsm.desktop.reveal_local_file") as reveal:
            status, _, _ = self.request("POST", "/api/files/reveal", {"path": "/etc/passwd"})
            self.assertEqual(status, 400)
            reveal.assert_not_called()
            status, _, _ = self.request("POST", "/api/files/reveal", {"path": "/Wonvy/合同.txt"})
            self.assertEqual(status, 200)
            self.assertEqual(reveal.call_args.args[1], "/Wonvy/合同.txt")

    def test_native_preview_uses_saved_tool_settings_not_request_commands(self):
        self.server.state.profile.update(local_root="W:\\", preview_mode="quicklook", preview_executable=r"C:\Tools\QuickLook.exe")
        with patch("synofind_dsm.desktop.resolve_local_path", return_value=(Path(r"W:\合同.txt"), Path("W:/"))), patch("synofind_dsm.native_preview.launch_preview") as launch:
            status, _, _ = self.request("POST", "/api/files/preview-native", {
                "path": "/Wonvy/合同.txt", "executable": "untrusted.exe", "mode": "untrusted"})
        self.assertEqual(status, 200)
        self.assertEqual(launch.call_args.args[1:], ("quicklook", r"C:\Tools\QuickLook.exe"))

    def test_search_filters_and_smb_path(self):
        query = urlencode({"pattern": "合同", "extension": "pdf", "min_size": 30, "limit": 20,
                           "offset": 0, "sort_by": "mtime", "descending": "true"})
        status, _, response = self.request("GET", "/api/search?" + query)
        self.assertEqual(status, 200)
        data = json.loads(response)
        self.assertEqual(data["files"][0]["smb_path"], "\\\\10.0.0.10\\Wonvy\\合同.txt")
        params = self.server.state.calls[0][1]
        self.assertEqual(params["pattern"], "合同")
        self.assertEqual(params["min_size"], 30)
        self.assertTrue(params["descending"])

    def test_invalid_filter_and_duplicate_parameters_rejected(self):
        for query in ("limit=9999", "offset=-1", "min_size=bad", "pattern=a&pattern=b", "descending=oops"):
            with self.subTest(query=query):
                status, _, _ = self.request("GET", "/api/search?" + query)
                self.assertEqual(status, 400)

    def test_busy_requests_fail_fast_but_status_still_works(self):
        with self.server.operation_lock:
            status, _, _ = self.request("GET", "/api/search?pattern=a")
            self.assertEqual(status, 409)
            status, _, _ = self.request("GET", "/api/status")
            self.assertEqual(status, 200)
            status, _, _ = self.request("POST", "/api/shutdown", {})
            self.assertEqual(status, 409)

    def test_download_is_streamed_as_attachment(self):
        self.server.state.content = b"binary\x00" * 400000
        status, headers, body = self.request("GET", "/api/files/download?" + urlencode({"path": "/Wonvy/合同.txt"}), csrf=False)
        self.assertEqual(status, 200)
        self.assertEqual(body, self.server.state.content)
        self.assertIn("attachment", headers["Content-Disposition"])
        self.assertIn("filename*=UTF-8''", headers["Content-Disposition"])
        self.assertTrue(self.server.state.last_response.closed)

    def test_download_rejects_cross_site_navigation(self):
        status, _, _ = self.request("GET", "/api/files/download?path=/Wonvy/a.txt", csrf=False,
                                    headers={"Sec-Fetch-Site": "cross-site"})
        self.assertEqual(status, 403)

    def test_download_outside_root_rejected(self):
        status, _, _ = self.request("GET", "/api/files/download?path=/etc/passwd", csrf=False)
        self.assertEqual(status, 400)

    def test_preview_plain_text_never_executes_markup(self):
        self.server.state.content = b'<script>alert("test")</script>'
        status, headers, body = self.request("GET", "/api/files/preview?path=/Wonvy/a.txt", csrf=False)
        self.assertEqual(status, 200)
        self.assertEqual(headers["Content-Type"], "text/plain; charset=utf-8")
        self.assertEqual(headers["X-Content-Type-Options"], "nosniff")
        self.assertEqual(body, self.server.state.content)

    def test_html_and_svg_not_embedded(self):
        for extension in ("html", "svg"):
            status, _, _ = self.request("GET", f"/api/files/preview?path=/Wonvy/a.{extension}", csrf=False)
            self.assertEqual(status, 415)

    def test_pdf_preview_has_strict_type_and_same_origin_embedding(self):
        self.server.state.content = b"%PDF-1.4\nsynthetic document\n%%EOF"
        status, headers, body = self.request("GET", "/api/files/preview?path=/Wonvy/a.pdf", csrf=False)
        self.assertEqual(status, 200)
        self.assertEqual(headers["Content-Type"], "application/pdf")
        self.assertEqual(headers["X-Frame-Options"], "SAMEORIGIN")
        self.assertEqual(body, self.server.state.content)
        self.server.state.content = b"<html>not a PDF</html>"
        status, _, _ = self.request("GET", "/api/files/preview?path=/Wonvy/a.pdf", csrf=False)
        self.assertEqual(status, 415)

    @unittest.skipUnless(sys.platform == "win32", "Windows mapped folders")
    def test_preview_prefers_mapped_file_and_not_nas_content(self):
        with tempfile.TemporaryDirectory() as directory:
            Path(directory, "合同.txt").write_bytes(b"from mapped folder")
            self.server.state.profile["local_root"] = directory
            status, _, body = self.request("GET", "/api/files/preview?path=" + quote("/Wonvy/合同.txt"), csrf=False)
            self.assertEqual(status, 200)
            self.assertEqual(body, b"from mapped folder")
            self.assertFalse(any(call[0] == "open_content" for call in self.server.state.calls))

    def test_fake_image_content_is_rejected(self):
        self.server.state.content = b"<html>not an image</html>"
        status, _, _ = self.request("GET", "/api/files/preview?path=/Wonvy/a.png", csrf=False)
        self.assertEqual(status, 415)

    def test_preview_has_size_limit(self):
        self.server.state.content = b"a" * (TEXT_LIMIT + 1)
        status, _, _ = self.request("GET", "/api/files/preview?path=/Wonvy/a.txt", csrf=False)
        self.assertEqual(status, 413)
        self.assertTrue(self.server.state.last_response.closed)

    def test_unsupported_media_type_rejected(self):
        status, _, _ = self.request("POST", "/api/connect", b"password=secret",
                                    headers={"Content-Type": "application/x-www-form-urlencoded"})
        self.assertEqual(status, 415)

    def test_static_path_traversal_rejected(self):
        status, _, _ = self.request("GET", "/../credentials.py")
        self.assertEqual(status, 404)
        status, _, _ = self.request("GET", "/vendor/../../credentials.py")
        self.assertEqual(status, 404)


if __name__ == "__main__":
    unittest.main()
