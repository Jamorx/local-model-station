"""Discover local Splash listeners and read their readiness without loading a model."""
import json
import os
import re
import sqlite3
from contextlib import closing
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, build_opener, ProxyHandler, HTTPRedirectHandler

import psutil

class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None

CONFIG_FIELDS = (
    "temperature", "top_p", "top_k", "max_context_window", "ttl_seconds",
    "force_sampling", "is_default", "is_pinned",
)
RUNTIME_FLAGS = ("--max-memory", "--max-context", "--kv-format", "--idle-release")


def configured_models(lab_root):
    path = lab_root / "omlx-data/model_settings.json"
    try:
        models = json.loads(path.read_text())["models"]
        return [
            {"id": name, **{key: settings[key] for key in CONFIG_FIELDS if key in settings}}
            for name, settings in models.items()
            if isinstance(settings, dict) and settings.get("external_engine") == "splash"
        ], "available"
    except FileNotFoundError:
        return [], "missing"
    except (OSError, ValueError, KeyError, AttributeError, TypeError):
        return [], "unavailable"


def option(args, flag):
    for index, arg in enumerate(args):
        if arg.startswith(flag + "="):
            return arg.split("=", 1)[1]
        if arg == flag and index + 1 < len(args):
            return args[index + 1]
    return None


def local_listeners():
    for process in psutil.process_iter():
        try:
            args = process.cmdline()
            if "-m" not in args or option(args, "-m") != "server.server":
                continue
            binary = option(args, "--binary")
            if not binary or Path(binary).name != "splash":
                continue
            for connection in process.net_connections(kind="tcp4"):
                if connection.status == psutil.CONN_LISTEN and connection.laddr.ip in {"127.0.0.1", "0.0.0.0"}:
                    yield {
                        "pid": process.pid,
                        "port": connection.laddr.port,
                        "model": option(args, "--model"),
                        **{flag[2:].replace("-", "_"): option(args, flag) for flag in RUNTIME_FLAGS},
                    }
        except psutil.Error:
            continue


def readiness(port):
    request = Request(f"http://127.0.0.1:{port}/ready")
    try:
        with build_opener(ProxyHandler({}), NoRedirect()).open(request, timeout=1) as response:
            return "ready" if response.status == 200 else "unreachable"
    except HTTPError as exc:
        with exc:
            return {503: "unavailable", 401: "protected", 403: "protected"}.get(exc.code, "unreachable")
    except (URLError, OSError):
        return "unreachable"


LOG_NAME = re.compile(r"omlx--(.+)-[0-9a-f]{8}\.log")
REQUEST_LINE = re.compile(
    r"^(\d{2}:\d{2}:\d{2}) (Done|Cancelled) · input ([\d,]+) · cached ([\d,]+)"
    r"(?: · deduplicated [\d,]+)? · output ([\d,]+)(?: ·|$)"
)


def recent_requests(log_dir, standalone_log=None, standalone_model=None):
    """Read a bounded tail and return only counters, never raw log messages."""
    try:
        files = sorted(
            ((path.stat().st_mtime, path) for path in log_dir.glob("omlx--*.log")
             if LOG_NAME.fullmatch(path.name) and path.is_file() and not path.is_symlink()),
            reverse=True,
        )[:4]
    except OSError:
        return []
    if standalone_log:
        files = [(0, standalone_log)] if standalone_log.is_file() else []
    records = []
    for _, path in files:
        try:
            with path.open("rb") as stream:
                start = max(0, stream.seek(0, 2) - 32768)
                stream.seek(start)
                if start:
                    stream.readline()
                lines = stream.read(32768).decode("utf-8", errors="replace").splitlines()
        except OSError:
            continue
        for line in reversed(lines):
            match = REQUEST_LINE.match(line)
            if not match:
                continue
            clock, outcome, prompt, cached, output = match.groups()
            speed = re.search(r" · ([\d.]+) tok/s(?: ·|$)", line)
            records.append({
                "model": standalone_model if standalone_log else LOG_NAME.fullmatch(path.name)[1], "time": clock, "outcome": outcome,
                "prompt_tokens": int(prompt.replace(",", "")),
                "cached_tokens": int(cached.replace(",", "")),
                "output_tokens": int(output.replace(",", "")),
                "output_tps": float(speed[1]) if speed else None,
            })
            if len(records) == 25:
                return records
    return records


def usage_history(path, model_ids):
    if not model_ids:
        return [], "available"
    if not path.is_file():
        return [], "unavailable"
    placeholders = ",".join("?" for _ in model_ids)
    query = f"""SELECT model_id, SUM(requests), SUM(prompt_tokens), SUM(completion_tokens),
                       SUM(cached_tokens), SUM(completion_tokens) / NULLIF(SUM(generation_seconds), 0)
                FROM model_usage_hourly WHERE model_id IN ({placeholders})
                GROUP BY model_id ORDER BY SUM(requests) DESC"""
    try:
        # Read committed checkpoints without creating SQLite sidecar files.
        with closing(sqlite3.connect(
            path.as_uri() + "?mode=ro&immutable=1", uri=True, timeout=1
        )) as connection:
            columns = ("model", "requests", "prompt_tokens", "output_tokens", "cached_tokens", "output_tps")
            return [dict(zip(columns, row)) for row in connection.execute(query, sorted(model_ids))], "available"
    except sqlite3.Error:
        return [], "unavailable"


NATIVE_METRICS = {
    "splash_requests_completed_total": "completed",
    "splash_prefill_input_tokens_total": "prefill_tokens",
    "splash_decode_output_tokens_total": "output_tokens",
    "splash_cache_reused_tokens_total": "reused_tokens",
    "splash_prefill_tokens_per_second": "prefill_tps",
    "splash_decode_tokens_per_second": "output_tps",
    "splash_memory_current_bytes": "memory_bytes",
}


def native_metrics(port):
    headers = {}
    key = os.environ.get("MEDIA_SPLASH_API_KEY")
    if key and str(port) == os.environ.get("MEDIA_SPLASH_PORT"):
        headers["Authorization"] = f"Bearer {key}"
    request = Request(f"http://127.0.0.1:{port}/metrics", headers=headers)
    try:
        with build_opener(ProxyHandler({}), NoRedirect()).open(request, timeout=1) as response:
            body = response.read(65537)
        if len(body) > 65536:
            return {"status": "unavailable"}
        values = {}
        for line in body.decode().splitlines():
            fields = line.split()
            if len(fields) == 2 and fields[0] in NATIVE_METRICS:
                value = float(fields[1])
                if 0 <= value < float("inf"):
                    values[NATIVE_METRICS[fields[0]]] = value
        return {"status": "available", **values} if values else {"status": "unavailable"}
    except HTTPError as exc:
        with exc:
            return {"status": "protected" if exc.code in (401, 403) else "unavailable"}
    except (OSError, ValueError):
        return {"status": "unavailable"}


def snapshot(lab_root):
    models, config_status = configured_models(lab_root)
    data_dir = lab_root / "omlx-data"
    log_dir = data_dir / "logs/splash"
    ids = {model["id"] for model in models}
    ids.update(match[1] for path in log_dir.glob("omlx--*.log") if (match := LOG_NAME.fullmatch(path.name)))
    history, history_status = usage_history(data_dir / "usage.sqlite3", ids)
    instances = [
        {**instance, "status": readiness(instance["port"]), "metrics": native_metrics(instance["port"])}
        for instance in local_listeners()
    ]
    log = os.environ.get("MEDIA_SPLASH_LOG")
    standalone_model = next(
        (i["model"] for i in instances if str(i["port"]) == os.environ.get("MEDIA_SPLASH_PORT")),
        None,
    )
    return {
        "configured_models": models,
        "config_status": config_status,
        "instances": instances,
        "usage_history": history,
        "history_status": history_status,
        "recent_requests": recent_requests(log_dir, Path(log) if log else None, standalone_model),
        "log_source": "standalone" if log else "omlx",
    }
