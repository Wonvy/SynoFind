#!/usr/bin/env python3
"""Search SynoFind or stream a chosen file to disk (Python standard library)."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import sys
import urllib.error
import urllib.parse
import urllib.request


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        # Do not forward a NAS token to another endpoint through a redirect.
        raise urllib.error.HTTPError(req.full_url, code, "Redirect refused", headers, fp)


OPENER = urllib.request.build_opener(NoRedirect)


def request(path: str, token: str, base: str):
    return urllib.request.Request(base + "/api/v1" + path, headers={"Authorization": "Bearer " + token})


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    search = commands.add_parser("search", help="List matching files; does not download.")
    search.add_argument("query")
    search.add_argument("--ext", default="")
    search.add_argument("--page", type=int, default=1)
    download = commands.add_parser("download", help="Download an explicitly chosen file ID.")
    download.add_argument("id")
    download.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    base = os.environ.get("SYNOFIND_URL", "http://10.0.0.10:8787").rstrip("/")
    token = os.environ.get("SYNOFIND_TOKEN", "")
    parsed = urllib.parse.urlsplit(base)
    if parsed.scheme not in ("http", "https") or not parsed.netloc or parsed.username or parsed.password or parsed.query or parsed.fragment:
        parser.error("SYNOFIND_URL must be a base HTTP(S) URL without credentials, query or fragment.")
    if not token:
        parser.error("Set SYNOFIND_TOKEN in your local environment first.")
    try:
        if args.command == "search":
            query = urllib.parse.urlencode({"q": args.query, "ext": args.ext, "page": args.page, "limit": 50})
            with OPENER.open(request("/search?" + query, token, base), timeout=30) as response:
                result = json.load(response)
            print(json.dumps(result, ensure_ascii=False, indent=2))
            return 0
        output: Path = args.output
        if output.exists():
            parser.error("Output already exists; choose another path. Existing files are not overwritten.")
        if not output.parent.is_dir():
            parser.error("Output parent directory does not exist.")
        file_id = urllib.parse.quote(args.id, safe="")
        # Open output exclusively. Incomplete downloads remain clearly labelled .part.
        partial = output.with_name(output.name + ".part")
        with OPENER.open(request("/files/" + file_id + "/content", token, base), timeout=60) as response:
            with partial.open("xb") as destination:
                total = 0
                while chunk := response.read(1024 * 1024):
                    destination.write(chunk)
                    total += len(chunk)
                expected = response.headers.get("Content-Length")
                if expected is not None and total != int(expected):
                    raise OSError("Download is incomplete; retained .part file for inspection.")
        # Atomic no-overwrite publication on ordinary local filesystems. Both names
        # are in the same directory; unsupported filesystems keep the .part safely.
        os.link(partial, output)
        partial.unlink()
        print(f"Saved {total} bytes to {output}")
        return 0
    except urllib.error.HTTPError as error:
        print(f"SynoFind returned HTTP {error.code}.", file=sys.stderr)
        return 1
    except (urllib.error.URLError, OSError, ValueError) as error:
        print(f"Request failed: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
