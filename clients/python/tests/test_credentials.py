"""Desktop credential/state tests; every account and password is synthetic."""

from __future__ import annotations

from contextlib import contextmanager
import io
from http.server import ThreadingHTTPServer
import json
import os
from pathlib import Path
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from synofind_dsm import APIError, AuthenticationError, DSMClient, DSMError, SearchResult
from synofind_dsm.credentials import DEFAULT_PROFILE, SettingsStore, _entropy, normalize_profile
from synofind_dsm.desktop_state import DesktopState
from synofind_dsm.desktop_state import _safe_error, _sanitized
import test_client as http_fixture


PASSWORD = "synthetic-password-only-中文-2a090924"
PROFILE = dict(DEFAULT_PROFILE, url="https://nas.example.test:5001", username="synthetic-readonly-user",
               root="/Wonvy", smb_host="10.0.0.10")


class FakeFactory:
    def __init__(self):
        self.clients = []
        self.plans = []

    def __call__(self, url, **kwargs):
        plan = self.plans.pop(0) if self.plans else {}
        client = FakeClient(url, kwargs, plan)
        self.clients.append(client)
        return client


class FakeClient:
    def __init__(self, url, kwargs, plan):
        self.url, self.kwargs, self.plan = url, kwargs, plan
        self.allowed_root = kwargs.get("allowed_root")
        self.logins = self.reads = self.searches = self.closes = self.streams = 0

    def login(self):
        self.logins += 1
        if self.plan.get("login_error"):
            raise self.plan["login_error"]
        if self.plan.get("requires_otp") and self.kwargs.get("otp_code") != "123456":
            raise AuthenticationError(403, "SYNO.API.Auth", "login")
        return self

    def list_files(self, folder=None, limit=100, offset=0):
        self.reads += 1
        if self.plan.get("list_error"):
            raise self.plan["list_error"]
        if folder in self.plan.get("denied_roots", []):
            raise APIError(407, "SYNO.FileStation.List", "list")
        return SearchResult([], 0, offset, True)

    def select_root(self, root):
        self.allowed_root = root

    def list_shares(self):
        if self.plan.get("shares_error"):
            raise self.plan["shares_error"]
        return self.plan.get("shares", [{"name": "Wonvy", "path": "/Wonvy"}, {"name": "Public", "path": "/Public"}])

    def close(self):
        self.closes += 1

    def search(self, **params):
        self.searches += 1
        if self.plan.get("search_error"):
            raise self.plan["search_error"]
        if self.plan.get("entered"):
            self.plan["entered"].set()
            self.plan["release"].wait(3)
        return SearchResult([{"path": "/Wonvy/合同.txt", "isdir": False}], 1, 0, True)

    def get_info(self, path):
        return {"path": path, "isdir": False}

    @contextmanager
    def open_content(self, path):
        self.streams += 1
        if self.plan.get("stream_error"):
            raise self.plan["stream_error"]
        stream = io.BytesIO(b"synthetic file content")
        try:
            yield {"path": path, "isdir": False}, stream
        finally:
            stream.close()


class DSM7FixtureHandler(http_fixture.FixtureHandler):
    def success(self, data):
        # Real DSM 7 discovery advertises JSON for File Station but does not
        # advertise JSON for Auth. Preserve that difference in wire tests.
        if http_fixture.AUTH in data:
            data[http_fixture.AUTH].pop("requestFormat", None)
        return super().success(data)


class SettingsTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.store = SettingsStore(self.directory.name)

    def test_defaults_and_memory_only_password_never_reach_disk(self):
        self.assertEqual(self.store.load(), (DEFAULT_PROFILE, None))
        self.store.save(dict(PROFILE, password=PASSWORD, otp_code="123456", sid="synthetic-sid"), PASSWORD, False)
        contents = self.store.path.read_bytes()
        for secret in (PASSWORD, "123456", "synthetic-sid"):
            self.assertNotIn(secret.encode(), contents)
        self.assertEqual(self.store.load(), (PROFILE, PASSWORD))
        self.assertEqual(SettingsStore(self.directory.name).load(), (PROFILE, None))
        self.assertFalse(self.store.remembered)

    @unittest.skipUnless(os.name == "nt", "Requires real current-user Windows DPAPI")
    def test_real_dpapi_roundtrip_has_only_ciphertext_on_disk(self):
        self.store.save(PROFILE, PASSWORD, True)
        contents = self.store.path.read_bytes()
        self.assertNotIn(PASSWORD.encode(), contents)
        self.assertNotIn(b'"password"', contents)
        self.assertEqual(SettingsStore(self.directory.name).load(), (PROFILE, PASSWORD))
        self.assertTrue(self.store.remembered)
        self.assertEqual(json.loads(contents)["credential"]["kind"], "windows-dpapi-user")

    @unittest.skipUnless(os.name == "nt", "Requires real current-user Windows DPAPI")
    def test_changing_endpoint_username_or_root_cannot_rebind_ciphertext(self):
        self.store.save(PROFILE, PASSWORD, True)
        original = self.store.path.read_bytes()
        for key, value in (("url", "https://different.example.test:5001"),
                           ("username", "different-user"), ("root", "/DifferentShare")):
            with self.subTest(field=key):
                document = json.loads(original)
                document["profile"][key] = value
                self.store.path.write_text(json.dumps(document), encoding="utf-8")
                with self.assertRaises(DSMError):
                    self.store.load()
                # Even editing the public binding cannot bypass DPAPI entropy.
                document["credential"]["binding"] = _entropy(document["profile"]).hex()
                self.store.path.write_text(json.dumps(document), encoding="utf-8")
                with self.assertRaises(DSMError):
                    self.store.load()

    def test_atomic_save_failure_preserves_previous_profile_and_password(self):
        self.store.save(PROFILE, PASSWORD, False)
        original = self.store.path.read_bytes()
        with patch("synofind_dsm.credentials.os.replace", side_effect=PermissionError("synthetic failure")):
            with self.assertRaises(DSMError):
                self.store.save(dict(PROFILE, username="other"), "different-password", False)
        self.assertEqual(self.store.path.read_bytes(), original)
        self.assertEqual(self.store.load(), (PROFILE, PASSWORD))
        self.assertEqual([file.name for file in Path(self.directory.name).iterdir()], ["settings.json"])

    def test_forget_removes_only_this_app_credential_and_keeps_public_profile(self):
        self.store.save(PROFILE, PASSWORD, os.name == "nt")
        unrelated = Path(self.directory.name) / "unrelated.txt"
        unrelated.write_text("keep", encoding="utf-8")
        self.store.forget()
        self.assertEqual(self.store.load(), (PROFILE, None))
        self.assertFalse(self.store.remembered)
        self.assertEqual(unrelated.read_text(encoding="utf-8"), "keep")

    def test_nonwindows_remember_is_rejected_without_fallback(self):
        self.store.save(PROFILE, PASSWORD, False)
        original = self.store.path.read_bytes()
        with patch("synofind_dsm.credentials.os.name", "posix"):
            with self.assertRaises(DSMError):
                self.store.save(PROFILE, PASSWORD, True)
        self.assertEqual(self.store.path.read_bytes(), original)

    def test_insecure_url_rejected_before_any_save(self):
        for url in ("http://nas.test", "https://user:password@nas.test", "https://nas.test/?password=secret"):
            with self.subTest(url=url), self.assertRaises(DSMError):
                self.store.save(dict(PROFILE, url=url), PASSWORD, False)
        self.assertFalse(self.store.path.exists())

    def test_generic_defaults_and_hostname_inference_require_no_online_mapping(self):
        for key in ("url", "username", "root", "smb_host", "local_root", "preview_executable"):
            self.assertEqual(DEFAULT_PROFILE[key], "")
        self.assertEqual(DEFAULT_PROFILE["preview_mode"], "web")
        profile = normalize_profile({"url": "https://nas.example.test:5001", "username": "reader"}, allow_unselected=True)
        self.assertEqual(profile["root"], "")
        self.assertEqual(profile["smb_host"], "nas.example.test")
        with self.assertRaises(DSMError):
            normalize_profile(profile)
        self.store.save(dict(PROFILE, local_root="W:\\"), PASSWORD, False)
        self.assertEqual(self.store.load()[0]["local_root"], "W:\\")

    def test_mappings_are_scoped_to_nas_account_and_share(self):
        first = dict(PROFILE, local_root="W:\\", preview_mode="quicklook", preview_executable="C:\\Tools\\QuickLook.exe")
        second = dict(PROFILE, root="/Public", local_root="P:\\", preview_mode="seer", preview_executable="C:\\Tools\\Seer.exe")
        self.store.save(first, PASSWORD, False)
        self.store.save(second, PASSWORD, False)
        self.assertEqual(self.store.mapping_for(PROFILE)["local_root"], "W:\\")
        self.assertEqual(self.store.mapping_for(dict(PROFILE, root="/Public"))["local_root"], "P:\\")
        self.assertEqual(self.store.mapping_for(dict(PROFILE, username="other"))["local_root"], "")
        self.assertEqual(self.store.mapping_for(dict(PROFILE, url="https://other.example.test"))["local_root"], "")
        self.store.forget()
        self.assertEqual(self.store.mapping_for(PROFILE)["preview_mode"], "quicklook")
        self.assertEqual(self.store.load()[1], None)

    @unittest.skipUnless(os.name == "nt", "Requires real current-user Windows DPAPI")
    def test_legacy_profile_remains_decryptable_when_mapping_fields_are_added(self):
        self.store.save(PROFILE, PASSWORD, True)
        document = json.loads(self.store.path.read_bytes())
        document["profile"] = {key: value for key, value in PROFILE.items() if key in {"url", "username", "root", "smb_host"}}
        document.pop("mappings", None)
        self.store.path.write_text(json.dumps(document), encoding="utf-8")
        loaded, password = SettingsStore(self.directory.name).load()
        self.assertEqual(password, PASSWORD)
        self.assertEqual(loaded, PROFILE)


class StateTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.factory = FakeFactory()
        self.state = DesktopState(self.directory.name, self.factory)
        self.addCleanup(self.state.stop)

    def test_constructor_and_status_never_login(self):
        self.assertFalse(self.state.status()["connected"])
        self.assertEqual(self.factory.clients, [])
        self.state.reconnect()
        self.assertEqual(self.factory.clients, [])

    def test_prepare_only_logs_in_and_lists_shares_without_saving_or_selecting(self):
        prepared = self.state.prepare_connection(PROFILE, PASSWORD, "123456")
        self.assertEqual(prepared["expires_in"], 300)
        self.assertEqual(prepared["profile"]["root"], "")
        self.assertEqual([share["path"] for share in prepared["shares"]], ["/Wonvy", "/Public"])
        self.assertTrue(all(share["local_root"] == "" and share["preview_mode"] == "web" for share in prepared["shares"]))
        self.assertFalse(self.state.store.path.exists())
        self.assertFalse(self.state.status()["configured"])
        self.assertFalse(self.state.status()["connected"])
        self.assertNotIn(PASSWORD, json.dumps(prepared) + json.dumps(self.state.status()))
        self.assertNotIn("123456", json.dumps(prepared) + json.dumps(self.state.status()))
        client = self.factory.clients[0]
        self.assertEqual(client.logins, 1)
        self.assertEqual(client.reads, 0)
        self.assertIsNone(client.allowed_root)

    def test_commit_selected_share_reuses_login_and_clears_pending_secrets(self):
        prepared = self.state.prepare_connection(PROFILE, PASSWORD)
        pending = self.state._pending
        status = self.state.commit_connection(prepared["selection_id"], "/Public")
        self.assertTrue(status["connected"])
        self.assertEqual(status["profile"]["root"], "/Public")
        self.assertEqual(self.factory.clients[0].logins, 1)
        self.assertEqual(self.factory.clients[0].reads, 1)
        self.assertEqual(self.factory.clients[0].allowed_root, "/Public")
        self.assertIsNone(self.state._pending)
        self.assertIsNone(pending.password)
        self.assertIsNone(pending.client)
        self.assertEqual(self.state.store.load()[1], PASSWORD)

    def test_commit_rejects_unlisted_root_and_stale_id_without_touching_saved_connection(self):
        self.state.connect(PROFILE, PASSWORD)
        original = self.state.store.path.read_bytes()
        prepared = self.state.prepare_connection(dict(PROFILE, username="other"), "other-password")
        for selection_id, root in (("stale", "/Public"), (prepared["selection_id"], "/Other"),
                                   (prepared["selection_id"], "/Public/subfolder")):
            with self.subTest(root=root), self.assertRaises(DSMError):
                self.state.commit_connection(selection_id, root)
            self.assertEqual(self.state.store.path.read_bytes(), original)
            self.assertEqual(self.state.status()["profile"], PROFILE)
            self.assertTrue(self.state.status()["connected"])
        self.assertEqual(self.factory.clients[1].reads, 0)

    def test_commit_permission_or_save_failure_preserves_old_session_and_password(self):
        self.state.connect(PROFILE, PASSWORD)
        original = self.state.store.path.read_bytes()
        prepared = self.state.prepare_connection(dict(PROFILE, username="other"), "other-password")
        candidate = self.factory.clients[1]
        candidate.plan["denied_roots"] = ["/Public"]
        with self.assertRaises(APIError):
            self.state.commit_connection(prepared["selection_id"], "/Public")
        self.assertEqual(self.state.store.path.read_bytes(), original)
        self.assertEqual(self.factory.clients[0].closes, 0)
        self.assertIsNone(candidate.allowed_root)
        candidate.plan.clear()
        with patch("synofind_dsm.credentials.os.replace", side_effect=PermissionError("synthetic failure")):
            with self.assertRaises(DSMError):
                self.state.commit_connection(prepared["selection_id"], "/Public")
        self.assertEqual(self.state.store.path.read_bytes(), original)
        self.assertEqual(self.state.store.load()[1], PASSWORD)
        self.assertIsNone(candidate.allowed_root)

    def test_pending_cancel_expiry_and_replacement_clear_only_pending_credentials(self):
        self.state.connect(PROFILE, PASSWORD)
        prepared = self.state.prepare_connection(PROFILE, PASSWORD)
        pending = self.state._pending
        self.state.cancel_connection("unrelated-id")
        self.assertIs(self.state._pending, pending)
        self.state.cancel_connection(prepared["selection_id"])
        self.state.cancel_connection(prepared["selection_id"])
        self.assertIsNone(pending.password)
        self.assertEqual(self.factory.clients[1].closes, 1)
        self.assertTrue(self.state.status()["connected"])
        with patch("synofind_dsm.desktop_state._SELECTION_TTL", 0.04):
            prepared = self.state.prepare_connection(PROFILE, PASSWORD)
        expiring = self.state._pending
        with self.state._lock:
            time.sleep(0.1)
            self.assertIsNone(expiring.password)
        with self.assertRaises(DSMError):
            self.state.commit_connection(prepared["selection_id"], "/Public")
        prepared = self.state.prepare_connection(PROFILE, PASSWORD)
        replaced = self.state._pending
        self.state.prepare_connection(PROFILE, "new-password")
        self.assertIsNone(replaced.password)
        self.state.cancel_connection(prepared["selection_id"])
        self.assertIsNotNone(self.state._pending)

    def test_failed_prepare_keeps_previous_pending_and_active_configuration(self):
        self.state.connect(PROFILE, PASSWORD)
        prepared = self.state.prepare_connection(PROFILE, PASSWORD)
        pending = self.state._pending
        saved = self.state.store.path.read_bytes()
        self.factory.plans.append({"login_error": AuthenticationError(400, "SYNO.API.Auth", "login")})
        with self.assertRaises(AuthenticationError):
            self.state.prepare_connection(PROFILE, "bad-password")
        self.assertIs(self.state._pending, pending)
        self.assertEqual(self.state.store.path.read_bytes(), saved)
        self.state.commit_connection(prepared["selection_id"], "/Public")
        self.assertTrue(self.state.status()["connected"])

    def test_switch_library_reuses_session_and_rolls_back_on_failed_read_or_save(self):
        self.state.connect(PROFILE, PASSWORD)
        client = self.factory.clients[0]
        original = self.state.store.path.read_bytes()
        client.plan["denied_roots"] = ["/Public"]
        with self.assertRaises(APIError):
            self.state.select_library("/Public")
        self.assertEqual(client.allowed_root, "/Wonvy")
        self.assertEqual(self.state.store.path.read_bytes(), original)
        client.plan.clear()
        with patch("synofind_dsm.credentials.os.replace", side_effect=PermissionError("synthetic failure")):
            with self.assertRaises(DSMError):
                self.state.select_library("/Public")
        self.assertEqual(client.allowed_root, "/Wonvy")
        self.assertEqual(self.state.store.path.read_bytes(), original)
        status = self.state.select_library("/Public")
        self.assertEqual(status["profile"]["root"], "/Public")
        self.assertEqual(client.allowed_root, "/Public")
        self.assertEqual(client.logins, 1)
        self.assertEqual(len(self.factory.clients), 1)
        self.assertEqual(self.state.store.load()[1], PASSWORD)

    @unittest.skipUnless(os.name == "nt", "Local Windows mappings and DPAPI")
    def test_switching_shares_restores_own_mapping_and_rebinds_encrypted_password(self):
        map_a, map_b = Path(self.directory.name) / "MapA", Path(self.directory.name) / "MapB"
        map_a.mkdir()
        map_b.mkdir()
        prepared = self.state.prepare_connection(PROFILE, PASSWORD)
        self.state.commit_connection(prepared["selection_id"], "/Wonvy", remember=True, local_root=str(map_a))
        self.state.select_library("/Public")
        self.assertEqual(self.state.status()["profile"]["local_root"], "")
        self.state.select_library("/Public", local_root=str(map_b))
        self.state.select_library("/Wonvy")
        self.assertEqual(self.state.status()["profile"]["local_root"], str(map_a))
        shares = {share["path"]: share for share in self.state.list_shares()}
        self.assertEqual(shares["/Wonvy"]["local_root"], str(map_a))
        self.assertEqual(shares["/Public"]["local_root"], str(map_b))
        self.assertTrue(self.state.status()["remembered"])
        self.assertEqual(SettingsStore(self.directory.name).load()[1], PASSWORD)
        self.assertEqual(self.factory.clients[0].logins, 1)

    def test_stop_clears_pending_password_without_removing_saved_credentials(self):
        self.state.connect(PROFILE, PASSWORD)
        original = self.state.store.path.read_bytes()
        self.state.prepare_connection(PROFILE, "pending-password")
        pending = self.state._pending
        self.state.stop()
        self.assertIsNone(pending.password)
        self.assertIsNone(pending.client)
        self.assertIsNone(self.state._pending)
        self.assertEqual(self.state.store.path.read_bytes(), original)

    @unittest.skipUnless(os.name == "nt", "Windows local mapping validation")
    def test_unavailable_mapping_reports_local_error_without_losing_pending_selection(self):
        prepared = self.state.prepare_connection(PROFILE, PASSWORD)
        missing = str(Path(self.directory.name) / "missing-mapping")
        with self.assertRaises(DSMError) as caught:
            self.state.commit_connection(prepared["selection_id"], "/Public", local_root=missing)
        self.assertIn("映射", str(caught.exception))
        self.assertNotIn("证书", str(caught.exception))
        self.assertEqual(self.state._pending.selection_id, prepared["selection_id"])
        self.assertFalse(self.state.store.path.exists())

    def test_connect_verifies_root_before_save_and_exposes_only_public_fields(self):
        status = self.state.connect(dict(PROFILE, password=PASSWORD, otp_code="123456"), PASSWORD, "123456", False)
        self.assertTrue(status["connected"])
        self.assertTrue(status["configured"])
        self.assertEqual(status["profile"], PROFILE)
        self.assertNotIn(PASSWORD, json.dumps(status))
        self.assertNotIn("123456", json.dumps(status))
        client = self.factory.clients[0]
        self.assertEqual((client.logins, client.reads), (1, 1))
        self.assertNotIn("allow_http", client.kwargs)
        self.assertNotIn("cert_sha256", client.kwargs)

    def test_failed_new_connection_keeps_old_session_and_saved_settings(self):
        self.state.connect(PROFILE, PASSWORD)
        saved = self.state.store.path.read_bytes()
        previous = self.factory.clients[0]
        self.factory.plans.append({"list_error": APIError(403, "SYNO.FileStation.List", "list")})
        with self.assertRaises(DSMError):
            self.state.connect(dict(PROFILE, username="other"), "different-password")
        self.assertTrue(self.state.status()["connected"])
        self.assertEqual(self.state.status()["profile"], PROFILE)
        self.assertEqual(previous.closes, 0)
        self.assertEqual(self.state.store.path.read_bytes(), saved)
        self.assertEqual(self.factory.clients[1].closes, 1)

    def test_file_list_failure_preserves_stage_and_code_without_server_message(self):
        secret = "synthetic-secret-server-message-" + PASSWORD
        self.factory.plans.append({"list_error": APIError(408, "SYNO.FileStation.List", "list", secret)})
        with self.assertRaises(APIError) as caught:
            self.state.connect(PROFILE, PASSWORD)
        self.assertEqual((caught.exception.code, caught.exception.api, caught.exception.method),
                         (408, "SYNO.FileStation.List", "list"))
        status = self.state.status()
        self.assertEqual((status["last_error_code"], status["last_error_api"], status["last_error_method"]),
                         (408, "SYNO.FileStation.List", "list"))
        self.assertNotIn(secret, str(caught.exception) + json.dumps(status))
        self.assertNotIn(PASSWORD, str(caught.exception) + json.dumps(status))
        self.assertNotIn("密码不正确", status["last_error"])
        self.assertIn("目录名称和大小写", status["last_error"])
        self.assertFalse(status["remembered"])
        self.assertEqual(self.factory.clients[0].logins, 1)
        self.assertEqual(self.factory.clients[0].reads, 1)
        self.state.connect(PROFILE, PASSWORD)
        status = self.state.status()
        for key in ("last_error", "last_error_code", "last_error_api", "last_error_method"):
            self.assertIsNone(status[key])

    def test_diagnostic_whitelist_drops_arbitrary_api_and_method_values(self):
        for api, method, expected in ((PASSWORD, PASSWORD, (None, None)),
                                      ("SYNO.FileStation.List", PASSWORD, ("SYNO.FileStation.List", None))):
            with self.subTest(api_known=api != PASSWORD):
                self.factory.plans.append({"list_error": APIError(407, api, method, PASSWORD)})
                with self.assertRaises(APIError) as caught:
                    self.state.connect(PROFILE, PASSWORD)
                status = self.state.status()
                self.assertEqual((status["last_error_api"], status["last_error_method"]), expected)
                self.assertNotIn(PASSWORD, json.dumps(status) + str(caught.exception)
                                 + caught.exception.api + caught.exception.method)

    def test_file_operation_codes_are_not_mistaken_for_login_codes(self):
        explanations = {105: "会话", 407: "文件权限", 408: "路径", 119: "会话 ID",
                        101: "请求格式", 402: "繁忙", 400: "操作参数"}
        for code, fragment in explanations.items():
            with self.subTest(code=code):
                error = APIError(code, "SYNO.FileStation.List", "list", PASSWORD)
                message = _safe_error(error)
                self.assertIn(fragment, message)
                self.assertNotIn("密码不正确", message)
                self.assertNotIn(PASSWORD, message)
        self.assertIn("密码不正确", _safe_error(AuthenticationError(400, "SYNO.API.Auth", "login")))
        sanitized = _sanitized(AuthenticationError(106, "SYNO.FileStation.Search", "list", PASSWORD))
        self.assertEqual((sanitized.api, sanitized.method), ("SYNO.FileStation.Search", "list"))

    @unittest.skipUnless(os.name == "nt", "Requires saved Windows DPAPI credential")
    def test_startup_reconnect_happens_only_when_explicitly_invoked(self):
        self.state.connect(PROFILE, PASSWORD, remember=True)
        self.state.stop()
        restarted = DesktopState(self.directory.name, self.factory)
        self.addCleanup(restarted.stop)
        self.assertEqual(len(self.factory.clients), 1)
        self.assertTrue(restarted.status()["remembered"])
        restarted.reconnect()
        restarted.reconnect()
        self.assertEqual(len(self.factory.clients), 2)
        self.assertTrue(restarted.status()["connected"])

    @unittest.skipUnless(os.name == "nt", "Requires saved Windows DPAPI credential")
    def test_expired_session_reconnects_once_then_retries_operation(self):
        self.factory.plans.append({"search_error": AuthenticationError(106, "SYNO.FileStation.Search", "list")})
        self.state.connect(PROFILE, PASSWORD, remember=True)
        result = self.state.search({"pattern": "合同"})
        self.assertEqual(result.total, 1)
        self.assertEqual(len(self.factory.clients), 2)
        self.assertEqual([client.searches for client in self.factory.clients], [1, 1])
        self.assertEqual([client.logins for client in self.factory.clients], [1, 1])

    @unittest.skipUnless(os.name == "nt", "Requires saved Windows DPAPI credential")
    def test_failed_reconnect_never_loops_or_retries_on_next_operation(self):
        self.factory.plans.extend([
            {"search_error": AuthenticationError(106, "SYNO.FileStation.Search", "list")},
            {"login_error": AuthenticationError(400, "SYNO.API.Auth", "login")},
        ])
        self.state.connect(PROFILE, PASSWORD, remember=True)
        with self.assertRaises(AuthenticationError):
            self.state.search({"pattern": "合同"})
        with self.assertRaises(AuthenticationError):
            self.state.search({"pattern": "合同"})
        self.assertEqual(len(self.factory.clients), 2)
        self.assertFalse(self.state.status()["connected"])

    @unittest.skipUnless(os.name == "nt", "Requires saved Windows DPAPI credential")
    def test_repeated_expired_session_does_not_reconnect_more_than_once(self):
        self.factory.plans.extend([
            {"search_error": AuthenticationError(106, "SYNO.FileStation.Search", "list")},
            {"search_error": AuthenticationError(107, "SYNO.FileStation.Search", "list")},
        ])
        self.state.connect(PROFILE, PASSWORD, remember=True)
        with self.assertRaises(AuthenticationError):
            self.state.search({"pattern": "合同"})
        self.assertEqual(len(self.factory.clients), 2)
        self.assertFalse(self.state.status()["connected"])

    @unittest.skipUnless(os.name == "nt", "Requires saved Windows DPAPI credential")
    def test_otp_is_not_saved_or_reused_for_new_session(self):
        self.factory.plans.append({"requires_otp": True})
        self.state.connect(PROFILE, PASSWORD, "123456", True)
        self.state.disconnect()
        self.factory.plans.append({"requires_otp": True})
        with self.assertRaises(AuthenticationError) as caught:
            self.state.reconnect()
        self.assertEqual(caught.exception.code, 403)
        self.assertIsNone(self.factory.clients[1].kwargs["otp_code"])
        self.assertNotIn(b"123456", self.state.store.path.read_bytes())
        self.assertIn("验证码", self.state.status()["last_error"])

    def test_status_does_not_wait_for_slow_search_and_disconnect_is_serialized(self):
        entered, release, disconnected = threading.Event(), threading.Event(), threading.Event()
        self.factory.plans.append({"entered": entered, "release": release})
        self.state.connect(PROFILE, PASSWORD)
        worker = threading.Thread(target=self.state.search, args=({"pattern": "合同"},))
        worker.start()
        self.assertTrue(entered.wait(1))
        before = time.monotonic()
        self.assertTrue(self.state.status()["connected"])
        self.assertLess(time.monotonic() - before, 0.1)
        closer = threading.Thread(target=lambda: (self.state.disconnect(), disconnected.set()))
        closer.start()
        self.assertFalse(disconnected.wait(0.05))
        release.set()
        worker.join(2)
        closer.join(2)
        self.assertTrue(disconnected.is_set())
        self.assertFalse(self.state.status()["connected"])

    def test_open_content_holds_session_until_response_closed(self):
        self.state.connect(PROFILE, PASSWORD)
        disconnected = threading.Event()
        with self.state.open_content("/Wonvy/a.txt") as (_, stream):
            closer = threading.Thread(target=lambda: (self.state.disconnect(), disconnected.set()))
            closer.start()
            self.assertFalse(disconnected.wait(0.05))
            self.assertEqual(stream.read(), b"synthetic file content")
        closer.join(2)
        self.assertTrue(disconnected.is_set())
        self.assertTrue(stream.closed)

    def test_stream_read_failure_is_not_retried(self):
        self.state.connect(PROFILE, PASSWORD)
        with self.assertRaisesRegex(OSError, "synthetic read failure"):
            with self.state.open_content("/Wonvy/a.txt"):
                raise OSError("synthetic read failure")
        self.assertEqual(len(self.factory.clients), 1)
        self.assertEqual(self.factory.clients[0].streams, 1)


class ContentStreamTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), DSM7FixtureHandler)
        cls.server.daemon_threads = True
        cls.thread = threading.Thread(target=cls.server.serve_forever, kwargs={"poll_interval": 0.02}, daemon=True)
        cls.thread.start()
        cls.url = f"http://127.0.0.1:{cls.server.server_port}"

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join(2)

    def setUp(self):
        self.server.options, self.server.calls, self.server.polls = {}, [], 0

    def client(self):
        return DSMClient(self.url, username=PROFILE["username"], password=PASSWORD, allow_http=True, timeout=3, allowed_root="/Wonvy")

    def test_real_dsm7_discovery_shape_produces_documented_wire_parameters(self):
        self.server.options["json_format"] = True
        with self.client() as client:
            client.list_files("/Wonvy", limit=1, offset=0)
        login = next(call for call in self.server.calls if call["raw"].get("method") == "login")
        self.assertEqual(login["raw"]["passwd"], PASSWORD)
        self.assertEqual(login["raw"]["session"], "FileStation")
        self.assertEqual(login["raw"]["format"], "sid")
        self.assertEqual(login["raw"]["enable_syno_token"], "yes")
        self.assertEqual(login["raw"]["version"], "6")
        listing = next(call for call in self.server.calls if call["raw"].get("api") == http_fixture.LIST)
        # Assert raw form fields instead of relying on the fixture's permissive
        # JSON-or-string decoder, which would miss quotation mistakes.
        self.assertEqual(listing["raw"]["folder_path"], '"/Wonvy"')
        self.assertEqual(listing["raw"]["additional"], '["size","time","perm","type"]')
        self.assertEqual(listing["raw"]["limit"], "1")
        self.assertEqual(listing["raw"]["offset"], "0")
        self.assertEqual(listing["raw"]["version"], "2")
        self.assertEqual(listing["raw"]["method"], "list")
        self.assertEqual(listing["raw"]["_sid"], http_fixture.SID)
        self.assertEqual(listing["raw"]["SynoToken"], http_fixture.TOKEN)
        for call in self.server.calls:
            self.assertEqual(call["verb"], "POST")
            self.assertNotIn("?", call["path"])

    def test_api_error_is_rejected_before_yielding_response(self):
        self.server.options["download_error"] = True
        yielded = False
        with self.client() as client, self.assertRaises(APIError) as caught:
            with client.open_content(http_fixture.FILE_PATH):
                yielded = True
        self.assertFalse(yielded)
        self.assertEqual(caught.exception.code, 407)

    def test_json_attachment_that_looks_like_error_remains_a_file(self):
        body = b'{"success":false,"error":{"code":407}}'
        self.server.options.update(file_content=body, content_type="application/json")
        with self.client() as client:
            with client.open_content(http_fixture.FILE_PATH) as (_, stream):
                result = b"".join(iter(lambda: stream.read(7), b""))
        self.assertEqual(result, body)
        self.assertTrue(stream.closed)

    def test_inspected_json_prefix_is_replayed_without_missing_bytes(self):
        for body in (b'{"message":"a legitimate JSON file"}', b'{"message":"' + b"x" * (90 * 1024) + b'"}'):
            with self.subTest(size=len(body)):
                self.server.options.update(file_content=body, content_type="application/json", no_disposition=True)
                with self.client() as client:
                    with client.open_content(http_fixture.FILE_PATH) as (_, stream):
                        result = b"".join(iter(lambda: stream.read(17 * 1024), b""))
                self.assertEqual(result, body)

    def test_directory_and_wrong_metadata_path_do_not_start_download(self):
        for options in ({"directory": True}, {"result_path": "/Wonvy/wrong-file.txt"}):
            with self.subTest(options=options):
                self.server.options = options
                self.server.calls = []
                with self.client() as client, self.assertRaises(DSMError):
                    with client.open_content(http_fixture.FILE_PATH):
                        self.fail("Invalid file response must not be yielded")
                self.assertFalse(any(call["params"].get("api") == http_fixture.DOWNLOAD for call in self.server.calls))

    def test_consumer_exception_always_closes_response(self):
        with self.client() as client, self.assertRaisesRegex(RuntimeError, "synthetic consumer failure"):
            with client.open_content(http_fixture.FILE_PATH) as (_, stream):
                self.assertTrue(stream.read(3))
                raise RuntimeError("synthetic consumer failure")
        self.assertTrue(stream.closed)


if __name__ == "__main__":
    unittest.main()
