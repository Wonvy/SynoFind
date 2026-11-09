"""Contract tests against a local HTTP implementation of the documented DSM API.

All credentials and files here are synthetic. No NAS connection is made.
"""
from __future__ import annotations

import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import tracemalloc
import unittest
from urllib.parse import parse_qs, urlencode, urlsplit
from urllib.request import ProxyHandler, Request, build_opener

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from synofind_dsm import APIError, DSMClient, DSMError, SearchTimeout


AUTH = "SYNO.API.Auth"
SEARCH = "SYNO.FileStation.Search"
LIST = "SYNO.FileStation.List"
DOWNLOAD = "SYNO.FileStation.Download"
PASSWORD = "test-only-password-not-a-real-secret"
SID = "synthetic-session-id"
TOKEN = "synthetic-syno-token"
FILE_PATH = '/Wonvy/项目 甲/合同,+#? "终稿".pdf'
CONTENT = (b"Synthetic download content\x00\xff" * 200_000) + b"end"


def unpack(value):
    try:
        return json.loads(value)
    except (ValueError, TypeError):
        return value


def file_info(path=FILE_PATH, *, isdir=False, size=None):
    return {"path": path, "name": path.rsplit("/", 1)[-1], "isdir": isdir,
            "additional": {"size": len(CONTENT) if size is None else size,
                           "time": {"mtime": 1730000000}, "type": "pdf"}}


class FixtureHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.0"

    def log_message(self, *args):
        pass

    def do_GET(self):
        self.handle_api()

    def do_POST(self):
        self.handle_api()

    def respond(self, value, *, code=200):
        data = json.dumps(value, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def success(self, data):
        self.respond({"success": True, "data": data})

    def error(self, code):
        # A hostile server must not trick the client into echoing session data.
        self.respond({"success": False, "error": {"code": code,
                     "message": PASSWORD + SID + TOKEN}})

    def handle_api(self):
        server = self.server
        body = self.rfile.read(int(self.headers.get("Content-Length", "0")))
        values = parse_qs(urlsplit(self.path).query, keep_blank_values=True)
        values.update(parse_qs(body.decode("utf-8"), keep_blank_values=True))
        raw = {k: v[-1] for k, v in values.items()}
        params = {k: unpack(v) for k, v in raw.items()}
        server.calls.append({"path": self.path, "verb": self.command,
                             "params": params, "raw": raw, "body": body,
                             "cookie": self.headers.get("Cookie"),
                             "token_header": self.headers.get("X-SYNO-TOKEN")})
        api, method = params.get("api"), params.get("method")
        if api == "SYNO.API.Info":
            api_path = server.options.get("api_path", "entry.cgi")
            data = {
                AUTH: {"path": "auth.cgi", "minVersion": 1,
                       "maxVersion": server.options.get("auth_version", 7)},
                SEARCH: {"path": api_path, "minVersion": 1, "maxVersion": 2},
                LIST: {"path": api_path, "minVersion": 1, "maxVersion": 2},
                DOWNLOAD: {"path": api_path, "minVersion": 1, "maxVersion": 2},
            }
            if server.options.get("json_format"):
                for entry in data.values():
                    entry["requestFormat"] = "JSON"
            return self.success(data)
        if server.options.get("require_cookie") and not (api == AUTH and method == "login"):
            # Model a DSM gateway that authenticates before parsing POST fields:
            # a correct _sid body value alone must still fail with 119.
            if self.headers.get("Cookie") != "id=" + server.options.get("sid", SID):
                return self.error(119)
        if server.options.get("require_token") and not (api == AUTH and method == "login"):
            if self.headers.get("X-SYNO-TOKEN") != server.options.get("token", TOKEN):
                return self.error(119)
        if api == AUTH:
            if method == "login":
                if server.options.get("login_error"):
                    return self.error(server.options["login_error"])
                return self.success({"sid": server.options.get("sid", SID), "synotoken": server.options.get("token", TOKEN)})
            if method == "logout":
                if server.options.get("logout_error"):
                    return self.error(106)
                return self.success({})
        if server.options.get("expired"):
            return self.error(106)
        if api == SEARCH:
            if method == "start":
                return self.success({"taskid": "task-synthetic"})
            if method == "list":
                if server.options.get("search_error"):
                    return self.error(403)
                server.polls += 1
                finished = not server.options.get("never_finish") and server.polls >= 2
                entries = [file_info(server.options.get("result_path", FILE_PATH))]
                return self.success({"finished": finished,
                                     "files": entries if params.get("limit", 0) else [],
                                     "total": 7, "offset": params.get("offset", 0)})
            if method in ("stop", "clean"):
                if method == "clean" and server.options.get("clean_error"):
                    return self.error(400)
                return self.success({})
        if api == LIST:
            if method == "list_share":
                if "share_response" in server.options:
                    return self.success(server.options["share_response"])
                shares = server.options.get("shares", [{"name": "Wonvy", "path": "/Wonvy"}, {"name": "Public", "path": "/Public"}])
                offset = params.get("offset", 0)
                limit = min(params.get("limit", 200), server.options.get("share_page_size", 200))
                return self.success({"shares": shares[offset:offset + limit], "total": len(shares), "offset": offset})
            if method == "getinfo":
                path = params["path"]
                if isinstance(path, list):
                    path = path[0]
                return self.success({"files": [file_info(
                    server.options.get("result_path", path),
                    isdir=server.options.get("directory", False),
                    size=server.options.get("file_size"))]})
            return self.success({"files": [file_info()], "total": 1,
                                 "offset": params.get("offset", 0)})
        if api == DOWNLOAD:
            if server.options.get("redirect"):
                self.send_response(302)
                self.send_header("Location", server.options["redirect"])
                self.end_headers()
                return
            if server.options.get("download_error"):
                return self.error(407)
            data = server.options.get("file_content", CONTENT)
            self.send_response(200)
            self.send_header("Content-Type", server.options.get("content_type", "application/octet-stream"))
            if not server.options.get("no_disposition"):
                self.send_header("Content-Disposition", 'attachment; filename="file.bin"')
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            if server.options.get("truncate"):
                data = data[:123]
            try:
                self.wfile.write(data)
            except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
                pass
            return
        self.error(102)


class ClientTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), FixtureHandler)
        cls.server.daemon_threads = True
        cls.thread = threading.Thread(target=cls.server.serve_forever,
                                      kwargs={"poll_interval": 0.02}, daemon=True)
        cls.thread.start()
        cls.url = f"http://127.0.0.1:{cls.server.server_port}"

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join(timeout=2)

    def setUp(self):
        self.server.calls = []
        self.server.options = {}
        self.server.polls = 0

    def client(self, **kwargs):
        kwargs.setdefault("allowed_root", "/Wonvy")
        return DSMClient(self.url, username="synthetic-reader", password=PASSWORD,
                         allow_http=True, timeout=3, **kwargs)

    def methods(self, api):
        return [c["params"]["method"] for c in self.server.calls if c["params"].get("api") == api]

    def test_discovery_is_unauthenticated(self):
        client = self.client()
        info = client.discover()
        self.assertEqual(info[SEARCH]["maxVersion"], 2)
        self.assertEqual(self.methods(AUTH), [])
        self.assertTrue(all(PASSWORD.encode() not in c["body"] for c in self.server.calls))

    def test_unselected_client_can_login_and_list_readonly_shares_in_pages(self):
        self.server.options.update(require_cookie=True, require_token=True, json_format=True, share_page_size=2,
                                   shares=[{"name": name, "path": "/" + name} for name in ("资料 甲", "Public", "ReadOnly", "Photo", "Archive")])
        with self.client(allowed_root=None) as client:
            shares = client.list_shares()
            self.assertIsNone(client.allowed_root)
        self.assertEqual(shares, self.server.options["shares"])
        calls = [call for call in self.server.calls if call["params"].get("method") == "list_share"]
        self.assertEqual([call["params"]["offset"] for call in calls], [0, 2, 4])
        self.assertTrue(all(call["params"]["onlywritable"] is False for call in calls))
        self.assertTrue(all(call["cookie"] == "id=" + SID and call["token_header"] == TOKEN for call in calls))

    def test_no_file_operations_are_permitted_before_root_selection(self):
        with self.client(allowed_root=None) as client:
            count = len(self.server.calls)
            for operation in (lambda: client.search("a", folder="/Wonvy"), lambda: client.list_files("/Wonvy"),
                              lambda: client.get_info(FILE_PATH), lambda: client.download(FILE_PATH, "unused.bin")):
                with self.assertRaises(DSMError):
                    operation()
            with self.assertRaises(DSMError):
                with client.open_content(FILE_PATH):
                    self.fail("An unselected client must not yield file data")
            self.assertEqual(len(self.server.calls), count)
            for root in ("/", "Wonvy", "/Wonvy/../Public", "/Wonvy\\Public"):
                with self.assertRaises(DSMError):
                    client.select_root(root)
            client.select_root("/Wonvy")
            self.assertEqual(client.get_info(FILE_PATH)["path"], FILE_PATH)
            client.select_root("/Public")
            with self.assertRaises(DSMError):
                client.get_info(FILE_PATH)

    def test_sdk_default_has_no_selected_share(self):
        client = DSMClient(self.url, allow_http=True)
        self.assertIsNone(client.allowed_root)
        client.close()

    def test_share_discovery_rejects_nested_traversal_or_repeated_paths(self):
        for shares in ([{"name": "bad", "path": "/"}], [{"name": "bad", "path": "/Public/../private"}],
                       [{"name": "bad", "path": "/Public/nested"}], [{"name": "bad", "path": "Public"}],
                       [{"name": "one", "path": "/Public"}, {"name": "again", "path": "/Public"}]):
            with self.subTest(shares=shares):
                self.server.options["shares"] = shares
                with self.client(allowed_root=None) as client, self.assertRaises(DSMError):
                    client.list_shares()

    def test_share_discovery_rejects_nonprogressing_or_misaligned_pages(self):
        for response in ({"total": 3, "offset": 0, "shares": []}, {"total": 1, "offset": 5, "shares": []}):
            self.server.options["share_response"] = response
            with self.client(allowed_root=None) as client, self.assertRaises(DSMError):
                client.list_shares()

    def test_login_session_and_logout_keep_secrets_out_of_url(self):
        with self.client() as client:
            client.list_files()
        self.assertEqual(self.methods(AUTH), ["login", "logout"])
        for call in self.server.calls:
            for secret in (PASSWORD, SID, TOKEN):
                self.assertNotIn(secret, call["path"])
            if call["params"].get("api") != "SYNO.API.Info":
                self.assertEqual(call["verb"], "POST")
        login = next(c["params"] for c in self.server.calls if c["params"].get("method") == "login")
        self.assertEqual(login["passwd"], PASSWORD)
        self.assertEqual(login["session"], "FileStation")

    def test_cookie_and_token_gateway_accepts_full_authenticated_workflow(self):
        self.server.options.update(require_cookie=True, require_token=True)
        with tempfile.TemporaryDirectory() as directory:
            with self.client() as client:
                client.list_files()
                client.search("合同", poll_interval=0.001)
                client.download(FILE_PATH, Path(directory) / "file.pdf")
                with client.open_content(FILE_PATH) as (_, stream):
                    self.assertEqual(stream.read(3), CONTENT[:3])
        self.assertEqual(self.methods(AUTH), ["login", "logout"])
        self.assertIn("clean", self.methods(SEARCH))
        for call in self.server.calls:
            api, method = call["params"].get("api"), call["params"].get("method")
            if api == "SYNO.API.Info" or (api == AUTH and method == "login"):
                self.assertIsNone(call["cookie"])
                self.assertIsNone(call["token_header"])
            else:
                self.assertEqual(call["cookie"], "id=" + SID)
                self.assertEqual(call["token_header"], TOKEN)
                self.assertEqual(call["raw"]["_sid"], SID)
                self.assertEqual(call["raw"]["SynoToken"], TOKEN)
            self.assertNotIn("?", call["path"])
            for secret in (PASSWORD, SID, TOKEN):
                self.assertNotIn(secret, call["path"])

    def test_gateway_rejects_body_sid_without_cookie_with_119(self):
        self.server.options["require_cookie"] = True
        fields = {"api": LIST, "method": "list", "version": "2", "folder_path": '"/Wonvy"',
                  "_sid": SID, "SynoToken": TOKEN}
        request = Request(self.url + "/webapi/entry.cgi", data=urlencode(fields).encode("ascii"), method="POST")
        with build_opener(ProxyHandler({})).open(request, timeout=3) as response:
            reply = json.load(response)
        self.assertFalse(reply["success"])
        self.assertEqual(reply["error"]["code"], 119)
        self.assertIsNone(self.server.calls[-1]["cookie"])

    def test_gateway_rejects_body_token_without_token_header_with_119(self):
        self.server.options.update(require_cookie=True, require_token=True)
        fields = {"api": LIST, "method": "list", "version": "2", "folder_path": '"/Wonvy"',
                  "_sid": SID, "SynoToken": TOKEN}
        request = Request(self.url + "/webapi/entry.cgi", data=urlencode(fields).encode("ascii"),
                          method="POST", headers={"Cookie": "id=" + SID})
        with build_opener(ProxyHandler({})).open(request, timeout=3) as response:
            reply = json.load(response)
        self.assertFalse(reply["success"])
        self.assertEqual(reply["error"]["code"], 119)
        self.assertEqual(self.server.calls[-1]["cookie"], "id=" + SID)
        self.assertIsNone(self.server.calls[-1]["token_header"])

    def test_optional_token_omits_header_when_not_returned_by_dsm(self):
        self.server.options.update(require_cookie=True, token=None)
        with self.client() as client:
            client.list_files()
        self.assertTrue(all(call["token_header"] is None for call in self.server.calls))

    def test_logout_and_close_do_not_leave_stale_request_cookie(self):
        self.server.options["require_cookie"] = True
        client = self.client()
        client.login()
        client.logout()
        client.discover()
        self.assertIsNone(self.server.calls[-1]["cookie"])
        self.assertIsNone(self.server.calls[-1]["token_header"])
        count = len(self.server.calls)
        with self.assertRaises(DSMError):
            client.list_files()
        self.assertEqual(len(self.server.calls), count)
        client.login("synthetic-reader", PASSWORD)
        self.assertIsNone(self.server.calls[-1]["cookie"])
        self.assertIsNone(self.server.calls[-1]["token_header"])
        client.close()
        count = len(self.server.calls)
        with self.assertRaises(DSMError):
            client.discover()
        self.assertEqual(len(self.server.calls), count)

    def test_failed_logout_still_discards_cookie_state(self):
        self.server.options.update(require_cookie=True, logout_error=True)
        client = self.client()
        client.login()
        with self.assertRaises(DSMError):
            client.logout()
        client.discover()
        self.assertIsNone(self.server.calls[-1]["cookie"])
        client.close()

    def test_cookie_value_rejects_header_injection_without_echoing_session(self):
        for sid in ("synthetic\r\nInjected: secret", "synthetic; other=secret", "synthetic,secret",
                    'synthetic"secret', "synthetic\\secret", "synthetic secret", "synthetic中文"):
            with self.subTest(kind=repr(sid[:10])):
                self.server.options["sid"] = sid
                client = self.client()
                with self.assertRaises(DSMError) as caught:
                    client.login()
                self.assertNotIn(sid, str(caught.exception))
                self.assertIsNone(client._sid)
                self.assertIsNone(self.server.calls[-1]["cookie"])
                client.close()

    def test_foreign_sid_cannot_be_added_as_cookie(self):
        with self.client() as client:
            count = len(self.server.calls)
            with self.assertRaises(DSMError) as caught:
                client._open("entry.cgi", {"api": LIST, "method": "list", "version": "2", "_sid": "foreign-session-secret"})
            self.assertEqual(len(self.server.calls), count)
            self.assertNotIn("foreign-session-secret", str(caught.exception))

    def test_token_header_rejects_controls_and_foreign_values_without_exposure(self):
        for token in ("synthetic\r\nInjected: secret", "synthetic\tsecret", "synthetic中文", "synthetic secret"):
            with self.subTest(kind=repr(token[:10])):
                self.server.options["token"] = token
                client = self.client()
                with self.assertRaises(DSMError) as caught:
                    client.login()
                self.assertNotIn(token, str(caught.exception))
                self.assertIsNone(client._sid)
                self.assertIsNone(self.server.calls[-1]["token_header"])
                client.close()
        self.server.options = {}
        with self.client() as client:
            count = len(self.server.calls)
            with self.assertRaises(DSMError) as caught:
                client._open("entry.cgi", {"api": LIST, "method": "list", "version": "2",
                                          "_sid": SID, "SynoToken": "foreign-token-secret"})
            self.assertEqual(len(self.server.calls), count)
            self.assertNotIn("foreign-token-secret", str(caught.exception))

    def test_cookie_authenticated_error_still_redacts_server_message(self):
        self.server.options.update(require_cookie=True, search_error=True)
        with self.client() as client:
            with self.assertRaises(APIError) as caught:
                client.search("合同")
            self.assertEqual(caught.exception.code, 403)
            for secret in (PASSWORD, SID, TOKEN):
                self.assertNotIn(secret, str(caught.exception))
        self.assertEqual(self.methods(SEARCH)[-2:], ["stop", "clean"])

    def test_auth_v3_fallback(self):
        self.server.options["auth_version"] = 3
        with self.client() as client:
            client.get_info(FILE_PATH)
        login = next(c["params"] for c in self.server.calls if c["params"].get("method") == "login")
        self.assertEqual(int(login["version"]), 3)

    def test_login_error_redacted_and_no_retry(self):
        self.server.options["login_error"] = 400
        with self.assertRaises(DSMError) as caught:
            self.client().login()
        for secret in (PASSWORD, SID, TOKEN):
            self.assertNotIn(secret, str(caught.exception))
        self.assertEqual(self.methods(AUTH), ["login"])

    def test_expired_session_is_not_silently_reauthenticated(self):
        with self.client() as client:
            self.server.options["expired"] = True
            with self.assertRaises(APIError) as caught:
                client.list_files()
            self.assertEqual(caught.exception.code, 106)
        self.assertEqual(self.methods(AUTH).count("login"), 1)

    def test_search_waits_then_fetches_page_and_cleans(self):
        with self.client() as client:
            result = client.search("合同", extension="pdf", min_size=12,
                                   max_size=456, modified_after=1700000000,
                                   modified_before=1800000000, limit=3, offset=2,
                                   sort_by="mtime", descending=True, poll_interval=0.001)
        self.assertEqual(result.total, 7)
        self.assertEqual(result.offset, 2)
        self.assertTrue(result.finished)
        self.assertEqual(result.files[0]["path"], FILE_PATH)
        sequence = self.methods(SEARCH)
        self.assertEqual(sequence[0], "start")
        self.assertEqual(sequence[-1], "clean")
        self.assertNotIn("stop", sequence)
        start = next(c["params"] for c in self.server.calls if c["params"].get("api") == SEARCH and c["params"].get("method") == "start")
        self.assertEqual(start["folder_path"], ["/Wonvy"])
        self.assertEqual(start["pattern"], "合同")
        self.assertEqual(start["size_from"], 12)
        self.assertEqual(start["size_to"], 456)
        self.assertEqual(start["mtime_from"], 1700000000)

    def test_search_timeout_stops_and_cleans(self):
        self.server.options["never_finish"] = True
        with self.client() as client:
            with self.assertRaises(SearchTimeout):
                client.search("合同", search_timeout=0.04, poll_interval=0.001)
        sequence = self.methods(SEARCH)
        self.assertEqual(sequence[-2:], ["stop", "clean"])

    def test_explicit_cancel_stops_and_cleans_nas_task(self):
        from synofind_dsm.client import SearchCancelled
        self.server.options['never_finish'] = True
        cancel = threading.Event()
        with self.client() as client:
            timer = threading.Timer(.15, cancel.set)
            timer.start()
            try:
                with self.assertRaises(SearchCancelled):
                    client.search('合同', cancel_event=cancel, poll_interval=1)
            finally:
                timer.cancel()
        self.assertEqual(self.methods(SEARCH)[-2:], ['stop', 'clean'])

    def test_cancel_before_start_creates_no_nas_search(self):
        from synofind_dsm.client import SearchCancelled
        cancel = threading.Event()
        cancel.set()
        with self.client() as client:
            with self.assertRaises(SearchCancelled):
                client.search('合同', cancel_event=cancel)
        self.assertEqual(self.methods(SEARCH), [])

    def test_search_error_stops_and_cleans(self):
        self.server.options["search_error"] = True
        with self.client() as client:
            with self.assertRaises(APIError):
                client.search("合同")
        self.assertEqual(self.methods(SEARCH)[-2:], ["stop", "clean"])

    def test_cleanup_failure_does_not_mask_search_failure(self):
        self.server.options.update(never_finish=True, clean_error=True)
        with self.client() as client:
            with self.assertRaises(SearchTimeout):
                client.search("合同", search_timeout=0.03, poll_interval=0.001)

    def test_logout_failure_does_not_mask_user_exception(self):
        self.server.options["logout_error"] = True
        with self.assertRaisesRegex(RuntimeError, "consumer error"):
            with self.client():
                raise RuntimeError("consumer error")

    def test_logout_failure_warns_after_success(self):
        self.server.options["logout_error"] = True
        with self.assertWarns(RuntimeWarning):
            with self.client() as client:
                result = client.get_info(FILE_PATH)
        self.assertEqual(result["path"], FILE_PATH)

    def test_cleanup_failure_after_success_is_visible(self):
        self.server.options["clean_error"] = True
        with self.client() as client:
            with self.assertRaises(APIError):
                client.search("合同", poll_interval=0.001)

    def test_search_out_of_scope_result_is_rejected_and_cleaned(self):
        self.server.options["result_path"] = "/homes/private.txt"
        with self.client() as client:
            with self.assertRaises((ValueError, DSMError)):
                client.search("合同", poll_interval=0.001)
        self.assertEqual(self.methods(SEARCH)[-1], "clean")

    def test_special_characters_survive_encoding(self):
        self.server.options["json_format"] = True
        with self.client() as client:
            result = client.get_info(FILE_PATH)
        self.assertEqual(result["path"], FILE_PATH)
        call = next(c for c in self.server.calls if c["params"].get("method") == "getinfo")
        self.assertEqual(call["params"]["path"], [FILE_PATH])

    def test_path_boundary_rejected_before_request(self):
        with self.client() as client:
            for invalid in ("/etc/passwd", "/Wonvy2/file", "/Wonvy/../secret",
                            "Wonvy/file", "/Wonvy\\file", "/Wonvy/a\x00b"):
                with self.subTest(path=invalid):
                    count = len(self.server.calls)
                    with self.assertRaises((ValueError, DSMError)):
                        client.get_info(invalid)
                    self.assertEqual(len(self.server.calls), count)

    def test_out_of_scope_result_is_rejected(self):
        self.server.options["result_path"] = "/homes/private.txt"
        with self.client() as client:
            with self.assertRaises((ValueError, DSMError)):
                client.get_info(FILE_PATH)

    def test_download_stream_matches_and_leaves_no_temp_files(self):
        with tempfile.TemporaryDirectory() as directory:
            dest = Path(directory) / "合同.pdf"
            with self.client() as client:
                result = client.download(FILE_PATH, dest)
            self.assertEqual(Path(result), dest)
            self.assertEqual(hashlib.sha256(dest.read_bytes()).digest(), hashlib.sha256(CONTENT).digest())
            self.assertEqual(list(Path(directory).iterdir()), [dest])

    def test_existing_file_not_overwritten(self):
        with tempfile.TemporaryDirectory() as directory:
            dest = Path(directory) / "keep.txt"
            dest.write_bytes(b"original")
            with self.client() as client:
                with self.assertRaises((FileExistsError, DSMError, ValueError)):
                    client.download(FILE_PATH, dest)
            self.assertEqual(dest.read_bytes(), b"original")
            self.assertEqual(self.methods(DOWNLOAD), [])

    def test_explicit_overwrite(self):
        with tempfile.TemporaryDirectory() as directory:
            dest = Path(directory) / "replace.bin"
            dest.write_bytes(b"old")
            with self.client() as client:
                client.download(FILE_PATH, dest, overwrite=True)
            self.assertEqual(dest.stat().st_size, len(CONTENT))

    def test_large_download_has_bounded_python_memory(self):
        data = b"x" * (32 * 1024 * 1024)
        self.server.options.update(file_content=data, file_size=len(data))
        with tempfile.TemporaryDirectory() as directory:
            with self.client() as client:
                tracemalloc.start()
                try:
                    dest = client.download(FILE_PATH, Path(directory) / "large.bin")
                    _, peak = tracemalloc.get_traced_memory()
                finally:
                    tracemalloc.stop()
            self.assertEqual(Path(dest).stat().st_size, len(data))
            self.assertLess(peak, 12 * 1024 * 1024,
                            "Client buffered too much of a 32 MiB download")

    def test_truncated_download_keeps_original_and_removes_temp(self):
        self.server.options["truncate"] = True
        with tempfile.TemporaryDirectory() as directory:
            dest = Path(directory) / "keep.txt"
            dest.write_bytes(b"original")
            with self.client() as client:
                with self.assertRaises((DSMError, OSError)):
                    client.download(FILE_PATH, dest, overwrite=True)
            self.assertEqual(dest.read_bytes(), b"original")
            self.assertEqual(list(Path(directory).iterdir()), [dest])

    def test_directory_is_not_downloaded_as_zip(self):
        self.server.options["directory"] = True
        with tempfile.TemporaryDirectory() as directory:
            with self.client() as client:
                with self.assertRaises((ValueError, DSMError)):
                    client.download(FILE_PATH, Path(directory) / "no.zip")
            self.assertEqual(self.methods(DOWNLOAD), [])
            self.assertEqual(list(Path(directory).iterdir()), [])

    def test_json_api_error_is_not_saved_as_file(self):
        self.server.options["download_error"] = True
        with tempfile.TemporaryDirectory() as directory:
            with self.client() as client:
                with self.assertRaises(APIError) as caught:
                    client.download(FILE_PATH, Path(directory) / "no.pdf")
            self.assertEqual(caught.exception.code, 407)
            self.assertEqual(list(Path(directory).iterdir()), [])

    def test_json_file_that_looks_like_api_error_is_preserved(self):
        content = b'{"success": false, "error": {"code": 407}}'
        self.server.options.update(file_content=content, file_size=len(content), content_type="application/json")
        with tempfile.TemporaryDirectory() as directory:
            dest = Path(directory) / "response.json"
            with self.client() as client:
                client.download("/Wonvy/response.json", dest)
            self.assertEqual(dest.read_bytes(), content)

    def test_redirect_is_not_followed(self):
        self.server.options["redirect"] = self.url + "/credential-leak"
        with tempfile.TemporaryDirectory() as directory:
            with self.client() as client:
                with self.assertRaises(DSMError):
                    client.download(FILE_PATH, Path(directory) / "file")
            self.assertFalse(any(c["path"].startswith("/credential-leak") for c in self.server.calls))

    def test_untrusted_discovered_api_path_rejected(self):
        for path in ("https://example.com/entry.cgi", "//evil.test/x", "../secret", "entry.cgi?token=leak"):
            with self.subTest(path=path):
                self.server.options["api_path"] = path
                with self.assertRaises((ValueError, DSMError)):
                    with self.client() as client:
                        client.list_files()

    def test_http_requires_explicit_opt_in(self):
        with self.assertRaises((ValueError, DSMError)):
            DSMClient(self.url)

    def test_url_with_credentials_is_rejected(self):
        with self.assertRaises((ValueError, DSMError)):
            DSMClient("https://user:secret@example.com")

    def test_cli_info_is_json_and_does_not_prompt_for_password(self):
        result = subprocess.run([sys.executable, "-m", "synofind_dsm", "--url", self.url,
                                 "--allow-http", "info"], cwd=Path(__file__).resolve().parents[1],
                                capture_output=True, text=True, encoding="utf-8", timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)
        parsed = json.loads(result.stdout)
        self.assertTrue(parsed)
        self.assertEqual(self.methods(AUTH), [])

    def run_cli(self, *arguments, authenticated=True):
        env = dict(os.environ)
        for key in ("SYNOLOGY_USERNAME", "SYNOLOGY_PASSWORD", "SYNOLOGY_OTP_CODE"):
            env.pop(key, None)
        if authenticated:
            env.update(SYNOLOGY_USERNAME="synthetic-reader", SYNOLOGY_PASSWORD=PASSWORD)
        return subprocess.run([sys.executable, "-m", "synofind_dsm", "--url", self.url,
                               "--allow-http", "--root", "/Wonvy", *arguments],
                              cwd=Path(__file__).resolve().parents[1], env=env,
                              input="", capture_output=True, text=True, encoding="utf-8", timeout=10)

    def test_cli_search_json_and_cleanup(self):
        result = self.run_cli("search", "合同", "--poll-interval", "0.001")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout)["files"][0]["path"], FILE_PATH)
        self.assertEqual(self.methods(SEARCH)[-1], "clean")
        self.assertEqual(self.methods(AUTH)[-1], "logout")

    def test_cli_noninteractive_missing_credentials_fails_without_prompt(self):
        result = self.run_cli("search", "合同", authenticated=False)
        self.assertEqual(result.returncode, 2)
        self.assertEqual(result.stdout, "")
        self.assertEqual(self.methods(AUTH), [])

    def test_cli_download_saves_only_selected_file(self):
        with tempfile.TemporaryDirectory() as directory:
            dest = Path(directory) / "file.pdf"
            result = self.run_cli("download", FILE_PATH, str(dest))
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertTrue(json.loads(result.stdout))
            self.assertEqual(dest.read_bytes(), CONTENT)

    def test_cli_error_does_not_disclose_server_secrets(self):
        self.server.options["login_error"] = 400
        result = self.run_cli("search", "合同")
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(result.stdout, "")
        for secret in (PASSWORD, SID, TOKEN):
            self.assertNotIn(secret, result.stderr)
        self.assertNotIn("Traceback", result.stderr)

    def test_cli_timeout_is_distinct_and_cleans(self):
        self.server.options["never_finish"] = True
        result = self.run_cli("search", "合同", "--search-timeout", "0.04", "--poll-interval", "0.001")
        self.assertEqual(result.returncode, 3, result.stderr)
        self.assertEqual(self.methods(SEARCH)[-2:], ["stop", "clean"])


if __name__ == "__main__":
    unittest.main()
