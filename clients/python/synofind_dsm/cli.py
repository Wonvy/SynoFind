"""Machine-readable CLI for the official Synology File Station API.

Examples (global options precede the command)::

    python -m synofind_dsm --url https://nas.example.com:5001 info
    python -m synofind_dsm --url https://nas.example.com:5001 --user reader shares
    python -m synofind_dsm --url https://nas.example.com:5001 --user reader --root /Documents search '*合同*'
    python -m synofind_dsm --url https://nas.example.com:5001 --user reader --root /Documents list

Set SYNOLOGY_USERNAME and SYNOLOGY_PASSWORD for noninteractive use. A password
is otherwise requested with getpass only when stdin is a terminal. For 2FA,
set SYNOLOGY_OTP_CODE or pass --otp-prompt; secret values are never CLI options.
Use --ca-file with a trusted CA/certificate bundle for a private DSM CA, or
--cert-sha256 with an exact certificate fingerprint verified through a trusted
channel. These options are mutually exclusive. --allow-http is explicit opt-in for
an HTTP endpoint. Date filters use Unix seconds and size filters use bytes.

Exit codes: 0 success, 1 request/filesystem failure, 2 invalid usage or missing
credentials, 3 search timeout, 130 interrupted. Results are JSON on stdout;
prompts and errors are written to stderr. API sessions are closed after a
command, including failed searches or downloads. Set --url/SYNOLOGY_URL for
every command. File operations additionally require --root/SYNOLOGY_ROOT;
neither has a personal default. info and shares do not require a root.
"""

from __future__ import annotations

import argparse
from dataclasses import asdict, is_dataclass
import getpass
import json
import math
import os
from pathlib import Path
import re
import sys
from typing import Any, Sequence

from . import DSMClient, DSMError, SearchTimeout


class UsageError(Exception):
    """Configuration error suitable for a concise stderr message."""


class SafeArgumentParser(argparse.ArgumentParser):
    def __init__(self, *args: Any, **kwargs: Any) -> None:
        kwargs.setdefault("allow_abbrev", False)
        super().__init__(*args, **kwargs)

    def error(self, message: str) -> None:
        # argparse normally echoes unknown arguments, which could accidentally
        # disclose a password entered as an unsupported command-line option.
        if "unrecognized arguments:" in message or "invalid choice:" in message:
            message = (
                "存在未识别的参数。请查看 --help；密码只能通过安全提示或 "
                "SYNOLOGY_PASSWORD 环境变量提供。"
            )
        super().error(message)


def _positive_float(value: str) -> float:
    try:
        number = float(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("必须为正数") from exc
    if not math.isfinite(number) or number <= 0:
        raise argparse.ArgumentTypeError("必须为有限的正数")
    return number


def _nonnegative_int(value: str) -> int:
    try:
        number = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("必须为非负整数") from exc
    if number < 0:
        raise argparse.ArgumentTypeError("必须为非负整数")
    return number


def _positive_int(value: str) -> int:
    number = _nonnegative_int(value)
    if number == 0:
        raise argparse.ArgumentTypeError("必须大于 0")
    return number


def build_parser() -> argparse.ArgumentParser:
    parser = SafeArgumentParser(
        prog="python -m synofind_dsm",
        description="SynoFind：通过群晖官方 File Station API 搜索并获取文件。",
        epilog=(
            "全局选项放在子命令前。结果为 stdout JSON，提示/错误写入 stderr。"
            "密码使用终端安全输入或 SYNOLOGY_PASSWORD；用户名可使用 "
            "SYNOLOGY_USERNAME。info 无需登录。"
        ),
    )
    parser.add_argument("--url", default=os.environ.get("SYNOLOGY_URL"), help="必填 DSM 地址，或设置 SYNOLOGY_URL；无默认地址")
    parser.add_argument("--user", default=os.environ.get("SYNOLOGY_USERNAME"), help="DSM 用户名，默认读取 SYNOLOGY_USERNAME")
    parser.add_argument("--root", default=os.environ.get("SYNOLOGY_ROOT"), help="文件操作必填共享目录，或设置 SYNOLOGY_ROOT；info/shares 无需此项")
    trust = parser.add_mutually_exclusive_group()
    trust.add_argument("--ca-file", metavar="PEM", help="受信任的 CA/证书 PEM 文件；校验证书链与主机名")
    trust.add_argument("--cert-sha256", metavar="HEX", help="通过 DSM 导出证书等可信渠道核对的 SHA-256 指纹；仅接受该证书，不自动信任")
    parser.add_argument("--timeout", type=_positive_float, default=30, metavar="SECONDS", help="单次 HTTP 请求超时秒数（默认：30）")
    parser.add_argument("--allow-http", action="store_true", help="明确允许 HTTP 地址；HTTP 不加密传输，不影响 HTTPS 证书验证")
    parser.add_argument("--otp-prompt", action="store_true", help="在终端安全输入两步验证码；未指定时读取 SYNOLOGY_OTP_CODE")
    commands = parser.add_subparsers(dest="command", required=True, metavar="{info,shares,search,list,stat,download}")

    commands.add_parser("info", help="查询公开 API 能力，无需用户名或密码")
    commands.add_parser("shares", help="登录后列出账户可见的共享文件夹，无需 --root")

    search = commands.add_parser("search", help="创建并等待官方搜索任务，返回一页结果")
    search.add_argument("pattern", nargs="?", default="", help="文件名模式，支持 * 和 ?；例如 '*合同*'，默认不限制名称")
    search.add_argument("--folder", help="搜索起点，默认 --root")
    search.add_argument("--no-recursive", action="store_true", help="只搜索当前目录")
    search.add_argument("--extension", help="扩展名筛选，如 pdf 或 pdf,docx")
    search.add_argument("--min-size", type=_nonnegative_int, help="最小文件大小，单位字节")
    search.add_argument("--max-size", type=_nonnegative_int, help="最大文件大小，单位字节")
    search.add_argument("--modified-after", type=_nonnegative_int, metavar="UNIX_SECONDS", help="修改时间下限，Unix 时间戳秒数")
    search.add_argument("--modified-before", type=_nonnegative_int, metavar="UNIX_SECONDS", help="修改时间上限，Unix 时间戳秒数")
    search.add_argument("--limit", type=_positive_int, default=100, help="本页最多返回条数（默认：100）")
    search.add_argument("--offset", type=_nonnegative_int, default=0, help="结果偏移量（默认：0）")
    search.add_argument("--sort-by", choices=("name", "size", "mtime"), default="name", help="排序字段（默认：name）")
    search.add_argument("--descending", action="store_true", help="按指定字段倒序排列")
    search.add_argument("--search-timeout", type=_positive_float, default=120, metavar="SECONDS", help="等待搜索完成的最长秒数（默认：120）")
    search.add_argument("--poll-interval", type=_positive_float, default=0.5, metavar="SECONDS", help="查询任务进度的间隔秒数（默认：0.5）")

    listing = commands.add_parser("list", help="列出指定目录的一页文件")
    listing.add_argument("--folder", help="要列出的目录，默认 --root")
    listing.add_argument("--limit", type=_positive_int, default=100, help="本页最多返回条数（默认：100）")
    listing.add_argument("--offset", type=_nonnegative_int, default=0, help="结果偏移量（默认：0）")

    stat = commands.add_parser("stat", help="获取一个文件或目录的信息")
    stat.add_argument("remote_path", help="File Station 路径，例如 /Documents/资料/合同.pdf")

    download = commands.add_parser("download", help="流式下载一个明确指定的文件")
    download.add_argument("remote_path", help="File Station 文件路径")
    download.add_argument("destination", type=Path, help="本地目标文件路径")
    download.add_argument("--overwrite", action="store_true", help="允许覆盖已存在的本地目标文件")
    return parser


def _credentials(args: argparse.Namespace) -> tuple[str, str, str | None]:
    username = (args.user or "").strip()
    if not username:
        if not sys.stdin.isatty():
            raise UsageError("非交互环境请设置 SYNOLOGY_USERNAME，或提供 --user。")
        sys.stderr.write("DSM 用户名：")
        sys.stderr.flush()
        username = sys.stdin.readline().strip()
        if not username:
            raise UsageError("用户名不能为空。")

    password = os.environ.get("SYNOLOGY_PASSWORD")
    if password is None:
        if not sys.stdin.isatty():
            raise UsageError("非交互环境请设置 SYNOLOGY_PASSWORD；密码不支持命令行参数。")
        password = getpass.getpass("DSM 密码：", stream=sys.stderr)
    if not password:
        raise UsageError("密码不能为空。")

    otp = os.environ.get("SYNOLOGY_OTP_CODE") or None
    if args.otp_prompt:
        if not sys.stdin.isatty():
            raise UsageError("--otp-prompt 需要交互终端；非交互环境请设置 SYNOLOGY_OTP_CODE。")
        otp = getpass.getpass("DSM 两步验证码：", stream=sys.stderr).strip()
        if not otp:
            raise UsageError("两步验证码不能为空。")
    return username, password, otp


def _json_default(value: Any) -> Any:
    if is_dataclass(value) and not isinstance(value, type):
        return asdict(value)
    if isinstance(value, Path):
        return str(value)
    raise TypeError(f"无法将 {type(value).__name__} 序列化为 JSON")


def _write_json(value: Any) -> None:
    json.dump(value, sys.stdout, ensure_ascii=False, indent=2, default=_json_default)
    sys.stdout.write("\n")


def _safe_message(error: BaseException, secrets: Sequence[str | None]) -> str:
    message = str(error) or type(error).__name__
    for secret in sorted((item for item in secrets if item), key=len, reverse=True):
        message = message.replace(secret, "[已隐藏]")
    message = re.sub(r"(?i)(https?://)[^/@\s]+:[^/@\s]+@", r"\1[已隐藏]@", message)
    message = re.sub(r"(?i)((?:_sid|synotoken|password|passwd|otp_code)=)[^&\s]+", r"\1[已隐藏]", message)
    return message


def _run_command(client: DSMClient, args: argparse.Namespace) -> Any:
    if args.command == "shares":
        return client.list_shares()
    if args.command == "search":
        return client.search(
            pattern=args.pattern,
            folder=args.folder,
            recursive=not args.no_recursive,
            extension=args.extension,
            min_size=args.min_size,
            max_size=args.max_size,
            modified_after=args.modified_after,
            modified_before=args.modified_before,
            limit=args.limit,
            offset=args.offset,
            sort_by=args.sort_by,
            descending=args.descending,
            search_timeout=args.search_timeout,
            poll_interval=args.poll_interval,
        )
    if args.command == "list":
        return client.list_files(folder=args.folder, limit=args.limit, offset=args.offset)
    if args.command == "stat":
        return client.get_info(args.remote_path)
    if args.command == "download":
        path = client.download(args.remote_path, args.destination, overwrite=args.overwrite)
        return {"path": str(path), "remote_path": args.remote_path}
    raise UsageError("未知子命令。")


def main(argv: Sequence[str] | None = None) -> int:
    # Windows redirected stdout may otherwise use a legacy code page. Keep
    # JSON UTF-8 even when a result contains characters outside that code page.
    reconfigure = getattr(sys.stdout, "reconfigure", None)
    if reconfigure is not None:
        reconfigure(encoding="utf-8")
    parser = build_parser()
    args = parser.parse_args(argv)
    secrets: list[str | None] = []
    try:
        if not args.url or not args.url.strip():
            raise UsageError("请通过 --url 或 SYNOLOGY_URL 指定你的 DSM 地址。")
        if args.command in {"search", "list", "stat", "download"} and (not args.root or not args.root.strip()):
            raise UsageError("请先用 shares 查看共享文件夹，再通过 --root 或 SYNOLOGY_ROOT 明确指定访问范围。")
        # Reject inconsistent filters before prompting for credentials or making
        # any network request.
        if args.command == "search":
            if args.min_size is not None and args.max_size is not None and args.min_size > args.max_size:
                raise UsageError("--min-size 不能大于 --max-size。")
            if args.modified_after is not None and args.modified_before is not None and args.modified_after > args.modified_before:
                raise UsageError("--modified-after 不能晚于 --modified-before。")

        options: dict[str, Any] = {
            "base_url": args.url,
            "ca_file": args.ca_file,
            "cert_sha256": args.cert_sha256,
            "timeout": args.timeout,
            "allowed_root": args.root,
            "allow_http": args.allow_http,
        }
        if args.command == "info":
            client = DSMClient(**options)
            try:
                result = client.discover()
            finally:
                client.close()
        else:
            username, password, otp = _credentials(args)
            secrets = [password, otp]
            options.update(username=username, password=password, otp_code=otp)
            with DSMClient(**options) as client:
                result = _run_command(client, args)
        _write_json(result)
        return 0
    except UsageError as exc:
        sys.stderr.write(f"参数错误：{_safe_message(exc, secrets)}\n")
        return 2
    except SearchTimeout as exc:
        sys.stderr.write(f"搜索超时：{_safe_message(exc, secrets)}\n")
        return 3
    except (DSMError, OSError, ValueError) as exc:
        sys.stderr.write(f"操作失败：{_safe_message(exc, secrets)}\n")
        return 1
    except KeyboardInterrupt:
        sys.stderr.write("已取消。\n")
        return 130
    except Exception as exc:
        # Do not dump a traceback that could contain an authenticated request.
        sys.stderr.write(f"客户端发生意外错误（{type(exc).__name__}），请检查版本和配置。\n")
        return 1
