#!/usr/bin/env python3
"""Register an existing batch for delivery to its DSH Web session."""
import argparse
import json
import os
import re
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", default=os.environ.get("LMG_URL", "http://127.0.0.1:8777"))
    parser.add_argument("--session-id", default=os.environ.get("DSH_SESSION_ID"))
    parser.add_argument("task_ids", nargs="+")
    args = parser.parse_args()
    parts = urlsplit(args.url)
    if (parts.scheme not in {"http", "https"} or not parts.hostname
            or parts.username or parts.password or parts.query or parts.fragment):
        parser.error("--url must be a trusted HTTP(S) gateway URL")
    if not args.session_id or not re.fullmatch(r"session-[a-f0-9-]{36}", args.session_id):
        parser.error("provide the current session with --session-id or DSH_SESSION_ID")
    task_ids = list(dict.fromkeys(args.task_ids))
    if len(task_ids) > 64 or any(not re.fullmatch(r"[a-f0-9]{32}", item) for item in task_ids):
        parser.error("provide up to 64 task IDs of 32 lowercase hexadecimal characters")

    headers = {"Content-Type": "application/json"}
    if os.environ.get("MEDIA_API_KEY"):
        headers["Authorization"] = "Bearer " + os.environ["MEDIA_API_KEY"]
    if os.environ.get("MEDIA_DSH_CALLBACK_KEY"):
        headers["X-DSH-Callback-Key"] = os.environ["MEDIA_DSH_CALLBACK_KEY"]
    payload = {"target": "dsh-web", "session_id": args.session_id, "task_ids": task_ids}
    request = Request(args.url.rstrip("/") + "/api/v1/callbacks",
                      data=json.dumps(payload).encode(), headers=headers, method="POST")
    opener = build_opener(ProxyHandler({}), NoRedirect())
    try:
        with opener.open(request, timeout=15) as response:
            result = json.load(response)
    except HTTPError as exc:
        with exc:
            detail = exc.read(1024).decode(errors="replace")
        parser.exit(1, f"callback registration failed (HTTP {exc.code}): {detail}\n")
    except (URLError, OSError, ValueError) as exc:
        parser.exit(1, f"callback registration was not confirmed: {exc}\n")
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
