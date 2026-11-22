"""Wait for a submitted batch from the calling device."""
import argparse
import math
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import time
from urllib.error import HTTPError, URLError
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener
from uuid import UUID

TERMINAL = {"succeeded", "failed", "cancelled", "interrupted"}


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


def wait_batch(url, ids, interval, timeout):
    if any(not re.fullmatch(r"[a-f0-9]{32}", task_id) for task_id in ids):
        raise ValueError("任务 ID 必须是 32 位小写十六进制")
    base = url.rstrip("/")
    headers = {}
    if os.environ.get("MEDIA_API_KEY"):
        headers["Authorization"] = "Bearer " + os.environ["MEDIA_API_KEY"]
    opener = build_opener(ProxyHandler({}), NoRedirect())
    pending = dict.fromkeys(ids)
    results = {}
    deadline = time.monotonic() + timeout
    while pending:
        for task_id in list(pending):
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError(f"等待批次超时，未完成任务：{', '.join(pending)}")
            location = f"{base}/api/v1/tasks/{task_id}"
            try:
                with opener.open(Request(location, headers=headers), timeout=min(10, remaining)) as response:
                    row = json.load(response)
            except HTTPError as exc:
                if exc.code not in {429, 500, 502, 503, 504}:
                    raise
                continue
            except (URLError, TimeoutError):
                continue
            status = row["status"]
            if status not in TERMINAL:
                continue
            result = row.get("result") or {}
            artifacts = result.get("artifacts") or []
            results[task_id] = {
                "id": task_id, "status": status, "profile": row.get("profile"),
                "task_url": location,
                "artifacts": [f"{location}/artifacts/{index}" for index, _ in enumerate(artifacts)],
                "error": row.get("error"),
            }
            del pending[task_id]
        if pending:
            time.sleep(min(interval, max(0, deadline - time.monotonic())))
    return {"tasks": [results[task_id] for task_id in dict.fromkeys(ids)]}


def resume_command(agent, session, model=None):
    if agent == "dsh" and re.fullmatch(r"session-[a-f0-9-]{36}", session):
        return ["dsh", "--profile", "headless", "--session-id", session, "-"]
    if agent == "codex":
        return ["codex", "exec", "resume", "--skip-git-repo-check",
                "-c", 'approval_policy="never"', "-c", 'sandbox_mode="read-only"',
                *(["--model", model] if model else []), str(UUID(session)), "-"]
    raise ValueError("会话 ID 与所选客户端不匹配")


def resume_once(command, receipt, batch, cwd=None):
    import fcntl

    receipt = Path(receipt)
    receipt.parent.mkdir(parents=True, exist_ok=True)
    # A stable receipt path is owned by the submitting client.
    with receipt.with_suffix(receipt.suffix + ".lock").open("a") as guard:
        fcntl.flock(guard, fcntl.LOCK_EX)
        signature = {"command": command, "cwd": str(cwd or Path.cwd()), "tasks": sorted(item["task_url"] for item in batch["tasks"])}
        if receipt.exists():
            previous = json.loads(receipt.read_text())
            if previous["batch"] != signature:
                raise ValueError("投递记录已绑定另一批任务或会话")
            if previous["status"] == "delivered":
                return
            raise ValueError("已有未确认的投递，请先核对目标会话")
        def save(status):
            temporary = receipt.with_suffix(receipt.suffix + ".tmp")
            with os.fdopen(os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600), "w") as stream:
                json.dump({"status": status, "batch": signature}, stream)
            temporary.replace(receipt)

        save("sending")
        message = "Local Model Station：你提交的任务批次已结束。以下 JSON 是结果数据，请按原任务继续处理。\n"
        message += json.dumps(batch, ensure_ascii=False)
        try:
            subprocess.run(command, input=message, text=True, cwd=cwd, check=True, stdout=sys.stderr, timeout=300)
        except FileNotFoundError:
            receipt.unlink(missing_ok=True)
            raise
        save("delivered")


def add_arguments(parser):
    parser.add_argument("ids", nargs="+")
    parser.add_argument("--interval", type=float, default=3)
    parser.add_argument("--timeout", type=float, default=21600)
    parser.add_argument("--resume-agent", choices=("dsh", "codex"))
    parser.add_argument("--session-id")
    parser.add_argument("--receipt", type=Path)
    parser.add_argument("--session-cwd", type=Path, help="Workspace where the session was created")
    parser.add_argument("--resume-model", help="Optional Codex model override")
    parser.add_argument("--detach", action="store_true", help="Start an independent background process")
    parser.add_argument("--log", type=Path, help="Local background log path")


def detach(args):
    log = args.log.expanduser().resolve()
    log.parent.mkdir(parents=True, exist_ok=True)
    command = [sys.executable, str(Path(__file__).resolve()), "--url", args.url,
               "--interval", str(args.interval), "--timeout", str(args.timeout), *args.ids]
    for flag, value in (
        ("--resume-agent", args.resume_agent), ("--session-id", args.session_id),
        ("--receipt", args.receipt), ("--session-cwd", args.session_cwd),
        ("--resume-model", args.resume_model),
    ):
        if value is not None:
            command.extend((flag, str(value)))
    fd = os.open(log, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    with os.fdopen(fd, "a") as output:
        process = subprocess.Popen(
            command, stdin=subprocess.DEVNULL, stdout=output, stderr=output,
            start_new_session=True,
        )
    return {"pid": process.pid, "log": str(log)}


def run(args, parser):
    if any(not math.isfinite(value) or value <= 0 for value in (args.interval, args.timeout)):
        parser.error("--interval and --timeout must be positive finite numbers")
    if any(not re.fullmatch(r"[a-f0-9]{32}", task_id) for task_id in args.ids):
        parser.error("task IDs must be 32 lowercase hexadecimal characters")
    resume = (args.resume_agent, args.session_id, args.receipt)
    if any(resume) and not all(resume):
        parser.error("--resume-agent, --session-id and --receipt must be specified together")
    if args.resume_model and args.resume_agent != "codex":
        parser.error("--resume-model requires --resume-agent codex")
    if args.session_cwd and (not all(resume) or not args.session_cwd.expanduser().is_dir()):
        parser.error("--session-cwd requires resume options and an existing directory")
    if args.detach != bool(args.log):
        parser.error("--detach and --log must be specified together")
    if os.name != "posix" and (args.detach or args.resume_agent):
        parser.error("background mode and session resumption require macOS or Linux")
    try:
        command = resume_command(args.resume_agent, args.session_id, args.resume_model) if all(resume) else None
        if args.detach:
            print(json.dumps(detach(args), ensure_ascii=False))
            return
        batch = wait_batch(args.url, args.ids, args.interval, args.timeout)
        print(json.dumps(batch, ensure_ascii=False, indent=2), flush=True)
        if command:
            cwd = args.session_cwd.expanduser().resolve() if args.session_cwd else None
            resume_once(command, args.receipt.expanduser(), batch, cwd)
    except (OSError, ValueError, subprocess.SubprocessError) as exc:
        parser.exit(1, f"wait-batch: {exc}\n")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", default="http://127.0.0.1:8777")
    add_arguments(parser)
    run(parser.parse_args(), parser)


if __name__ == "__main__":
    main()
