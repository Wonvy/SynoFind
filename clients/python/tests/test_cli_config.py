"""Generic CLI configuration: no NAS or share is silently chosen for the user."""
import io
import json
import os
import unittest
from contextlib import redirect_stderr, redirect_stdout
from unittest.mock import patch

from synofind_dsm import cli


class FakeClient:
    instances = []

    def __init__(self, **options):
        self.options = options
        self.closed = False
        self.calls = []
        self.instances.append(self)

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()

    def close(self):
        self.closed = True

    def discover(self):
        self.calls.append("discover")
        return {"SYNO.API.Auth": {"maxVersion": 7}}

    def list_shares(self):
        self.calls.append("shares")
        return [{"name": "Documents", "path": "/Documents"}, {"name": "Photos", "path": "/Photos"}]

    def search(self, **options):
        self.calls.append(("search", options))
        return {"files": [], "total": 0, "offset": 0}


class CLIConfigurationTests(unittest.TestCase):
    def setUp(self):
        FakeClient.instances.clear()

    def invoke(self, arguments, environment=None):
        stdout, stderr = io.StringIO(), io.StringIO()
        with patch.dict(os.environ, environment or {}, clear=True), patch.object(cli, "DSMClient", FakeClient):
            with redirect_stdout(stdout), redirect_stderr(stderr):
                code = cli.main(arguments)
        return code, stdout.getvalue(), stderr.getvalue()

    def test_parser_has_no_personal_address_or_root(self):
        with patch.dict(os.environ, {}, clear=True):
            args = cli.build_parser().parse_args(["info"])
        self.assertIsNone(args.url)
        self.assertIsNone(args.root)

    def test_missing_url_fails_before_credentials_or_client(self):
        with patch.object(cli, "_credentials") as credentials:
            code, output, error = self.invoke(["shares"])
        self.assertEqual(code, 2)
        self.assertEqual(output, "")
        self.assertIn("SYNOLOGY_URL", error)
        credentials.assert_not_called()
        self.assertEqual(FakeClient.instances, [])

    def test_every_file_command_requires_explicit_root_before_credentials(self):
        for command in (["search", "合同"], ["list"], ["stat", "/Documents/a.txt"], ["download", "/Documents/a.txt", "a.txt"]):
            with self.subTest(command=command), patch.object(cli, "_credentials") as credentials:
                code, _, error = self.invoke(["--url", "https://nas.example.com:5001", *command])
                self.assertEqual(code, 2)
                self.assertIn("SYNOLOGY_ROOT", error)
                credentials.assert_not_called()
        self.assertEqual(FakeClient.instances, [])

    def test_info_uses_explicit_url_without_root_or_credentials(self):
        with patch.object(cli, "_credentials") as credentials:
            code, output, error = self.invoke(["--url", "https://nas.example.com:5001", "info"])
        self.assertEqual(code, 0, error)
        self.assertIn("SYNO.API.Auth", json.loads(output))
        credentials.assert_not_called()
        self.assertIsNone(FakeClient.instances[0].options["allowed_root"])
        self.assertTrue(FakeClient.instances[0].closed)

    def test_shares_works_without_root_and_does_not_choose_one(self):
        code, output, error = self.invoke(["shares"], {
            "SYNOLOGY_URL": "https://nas.example.com:5001",
            "SYNOLOGY_USERNAME": "reader", "SYNOLOGY_PASSWORD": "synthetic-password",
        })
        self.assertEqual(code, 0, error)
        self.assertEqual([row["path"] for row in json.loads(output)], ["/Documents", "/Photos"])
        instance = FakeClient.instances[0]
        self.assertIsNone(instance.options["allowed_root"])
        self.assertEqual(instance.calls, ["shares"])
        self.assertTrue(instance.closed)

    def test_environment_address_and_root_enable_search(self):
        code, output, error = self.invoke(["search", "合同"], {
            "SYNOLOGY_URL": "https://nas.example.com:5001", "SYNOLOGY_ROOT": "/Photos",
            "SYNOLOGY_USERNAME": "reader", "SYNOLOGY_PASSWORD": "synthetic-password",
        })
        self.assertEqual(code, 0, error)
        self.assertEqual(json.loads(output)["total"], 0)
        self.assertEqual(FakeClient.instances[0].options["allowed_root"], "/Photos")

    def test_explicit_arguments_override_environment(self):
        code, _, error = self.invoke([
            "--url", "https://other.example.com:5001", "--root", "/Documents", "search", "合同",
        ], {"SYNOLOGY_URL": "https://nas.example.com:5001", "SYNOLOGY_ROOT": "/Photos",
            "SYNOLOGY_USERNAME": "reader", "SYNOLOGY_PASSWORD": "synthetic-password"})
        self.assertEqual(code, 0, error)
        self.assertEqual(FakeClient.instances[0].options["base_url"], "https://other.example.com:5001")
        self.assertEqual(FakeClient.instances[0].options["allowed_root"], "/Documents")

    def test_empty_environment_values_do_not_restore_defaults(self):
        code, _, error = self.invoke(["info"], {"SYNOLOGY_URL": "  "})
        self.assertEqual(code, 2)
        self.assertIn("SYNOLOGY_URL", error)
        code, _, error = self.invoke(["--url", "https://nas.example.com:5001", "list"], {"SYNOLOGY_ROOT": "  "})
        self.assertEqual(code, 2)
        self.assertIn("SYNOLOGY_ROOT", error)
        self.assertEqual(FakeClient.instances, [])


if __name__ == "__main__":
    unittest.main()
