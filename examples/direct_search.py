"""Search DSM directly, then optionally download an explicitly chosen file.

Install the client first: python -m pip install ./clients/python

Required: SYNOLOGY_URL and SYNOLOGY_USERNAME. Password: SYNOLOGY_PASSWORD or a terminal prompt.
Optional: SYNOLOGY_CA_FILE,
SYNOLOGY_CERT_SHA256 (mutually exclusive with SYNOLOGY_CA_FILE),
SYNOLOGY_ROOT (no default), SYNOLOGY_OTP_CODE, SYNOLOGY_PATTERN (default
*合同*). Supply both SYNOLOGY_DOWNLOAD_PATH and SYNOLOGY_DOWNLOAD_TO to download
one explicitly selected file. Without a root this example only lists shares;
set SYNOLOGY_ROOT to an explicitly chosen share and rerun to search.

By default TLS validates the CA and URL hostname/IP. For a private certificate,
provide a trusted PEM CA file, or verify the exact certificate fingerprint via
a DSM certificate export/another trusted channel and supply SYNOLOGY_CERT_SHA256.
A fingerprint trusts only that certificate instead of CA/hostname validation;
never copy an unverified fingerprint from a failed network connection. This
example uses only the Python standard library and the local SynoFind client.
"""

from __future__ import annotations

from dataclasses import asdict
import getpass
import json
import os
import sys

from synofind_dsm import DSMClient, DSMError


def _run() -> int:
    url = os.environ.get("SYNOLOGY_URL", "").strip()
    if not url:
        sys.stderr.write("请先设置 SYNOLOGY_URL 为你的 DSM HTTPS 地址。\n")
        return 2
    root = os.environ.get("SYNOLOGY_ROOT") or None
    remote_path = os.environ.get("SYNOLOGY_DOWNLOAD_PATH")
    destination = os.environ.get("SYNOLOGY_DOWNLOAD_TO")
    if bool(remote_path) != bool(destination):
        sys.stderr.write("下载时请同时设置 SYNOLOGY_DOWNLOAD_PATH 和 SYNOLOGY_DOWNLOAD_TO。\n")
        return 2
    if remote_path and not root:
        sys.stderr.write("下载前请通过 SYNOLOGY_ROOT 明确选择共享目录。\n")
        return 2
    username = os.environ.get("SYNOLOGY_USERNAME", "").strip()
    if not username:
        sys.stderr.write("请先设置 SYNOLOGY_USERNAME。\n")
        return 2
    password = os.environ.get("SYNOLOGY_PASSWORD")
    if password is None:
        if not sys.stdin.isatty():
            sys.stderr.write("非交互环境请先设置 SYNOLOGY_PASSWORD。\n")
            return 2
        password = getpass.getpass("DSM 密码：", stream=sys.stderr)
    if not password:
        sys.stderr.write("密码不能为空。\n")
        return 2
    with DSMClient(
        base_url=url,
        username=username,
        password=password,
        otp_code=os.environ.get("SYNOLOGY_OTP_CODE") or None,
        ca_file=os.environ.get("SYNOLOGY_CA_FILE") or None,
        cert_sha256=os.environ.get("SYNOLOGY_CERT_SHA256") or None,
        allowed_root=root,
        timeout=30,
    ) as client:
        if root is None:
            output = {"shares": client.list_shares()}
        else:
            results = client.search(
                pattern=os.environ.get("SYNOLOGY_PATTERN", "*合同*"),
                limit=20,
                search_timeout=120,
            )
            output = {"search": asdict(results)}
        # No automatic selection of the first match: choose the exact file
        # path after inspecting search results and rerun with both variables.
        if remote_path and destination:
            saved = client.download(remote_path, destination, overwrite=False)
            output["download"] = {"remote_path": remote_path, "path": str(saved)}
    print(json.dumps(output, ensure_ascii=False, indent=2))
    return 0


def main() -> int:
    reconfigure = getattr(sys.stdout, "reconfigure", None)
    if reconfigure is not None:
        reconfigure(encoding="utf-8")
    try:
        return _run()
    except (DSMError, OSError, ValueError) as exc:
        # Keep example output free of request URLs, sessions, and credentials.
        code = getattr(exc, "code", None)
        suffix = f"，API 错误码 {code}" if isinstance(code, int) else ""
        sys.stderr.write(f"调用失败（{type(exc).__name__}{suffix}）。请检查地址、证书、账号权限与文件路径。\n")
        return 1
    except KeyboardInterrupt:
        sys.stderr.write("已取消。\n")
        return 130
    except EOFError:
        sys.stderr.write("未收到密码，已取消。\n")
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
