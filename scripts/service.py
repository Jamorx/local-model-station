#!/usr/bin/env python3
"""Manage the independent Local Model Gateway LaunchAgent (macOS only)."""
import argparse
import json
import os
from pathlib import Path
import plistlib
import socket
import subprocess
import time
import sys
import urllib.request

REPO = Path(__file__).resolve().parents[1]
LABEL = "local.model-gateway"
PLIST = Path.home() / "Library/LaunchAgents" / (LABEL + ".plist")
DOMAIN = f"gui/{os.getuid()}"
DATA = Path(os.environ.get("MEDIA_DATA_ROOT", str(Path.home() / "Library/Application Support/LocalModelGateway"))).expanduser()


def loaded():
    return subprocess.run(["launchctl", "print", DOMAIN + "/" + LABEL],
                          stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL).returncode == 0


def status():
    result = {"label": LABEL, "loaded": loaded(), "url": "http://127.0.0.1:8777",
              "data_root": str(DATA)}
    try:
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        result["health"] = json.load(opener.open(result["url"] + "/health", timeout=3))
    except Exception as exc:
        result["health_error"] = str(exc)
    return result


def start():
    if loaded():
        return status()
    with socket.socket() as sock:
        if sock.connect_ex(("127.0.0.1", 8777)) == 0:
            raise RuntimeError("Port 8777 already occupied; refusing to replace another process")
    lab = Path(os.environ.get("MEDIA_LAB_ROOT", str(Path.home() / "LocalModelLab"))).resolve()
    python = Path(os.environ.get("MEDIA_API_PYTHON", sys.executable)).absolute()
    if not python.is_file():
        raise RuntimeError("Existing media API Python environment not found")
    DATA.mkdir(parents=True, exist_ok=True)
    config = {
        "Label": LABEL,
        "ProgramArguments": [str(python), "-m", "uvicorn", "model_gateway.app:app", "--host", "0.0.0.0",
                             "--port", "8777", "--workers", "1", "--no-access-log"],
        "WorkingDirectory": str(REPO),
        "EnvironmentVariables": {
            "MEDIA_LAB_ROOT": str(lab), "MEDIA_JOBS_DIR": str(DATA / "jobs"),
            "MEDIA_EXECUTION_ENABLED": os.environ.get("MEDIA_EXECUTION_ENABLED", "0"), "MEDIA_SERVICE_ID": LABEL,
            "MEDIA_RUNNER_PYTHON": os.environ.get("MEDIA_RUNNER_PYTHON",str(lab / "venv/bin/python")), "PYTHONUNBUFFERED": "1",
            "PATH": str(Path(os.environ.get("MEDIA_FFMPEG_BIN", "/opt/homebrew/bin/ffmpeg")).parent) + ":/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin",
            "MEDIA_QUEUE_WAIT_TIMEOUT_S": "21600",
        },
        "RunAtLoad": True,
        "KeepAlive": True,
        "ThrottleInterval": 10,
        "StandardOutPath": str(DATA / "stdout.log"),
        "StandardErrorPath": str(DATA / "stderr.log"),
    }
    for key, value in os.environ.items():
        if key.startswith("MEDIA_"):
            config["EnvironmentVariables"][key] = value
    PLIST.parent.mkdir(parents=True, exist_ok=True)
    PLIST.write_bytes(plistlib.dumps(config))
    PLIST.chmod(0o600)
    subprocess.run(["launchctl", "bootstrap", DOMAIN, str(PLIST)], check=True)
    for _ in range(100):
        current = status()
        if current.get("health", {}).get("service_id") == LABEL:
            return {"started": LABEL, "ready": True, "url": current["url"], "execution_enabled": current["health"]["execution_enabled"]}
        time.sleep(0.1)
    raise RuntimeError("LaunchAgent started but HTTP health did not become ready; inspect ~/Library/Application Support/LocalModelGateway/stderr.log")


def main():
    if sys.platform != "darwin":
        raise SystemExit("首期仅支持 macOS")
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("action", choices=["start", "status", "stop"])
    action = p.parse_args().action
    if action == "start":
        result = start()
    elif action == "stop":
        health = status().get("health", {})
        if health.get("active_job"):
            raise RuntimeError("An inference task is active; stop refused")
        if loaded():
            subprocess.run(["launchctl", "bootout", DOMAIN + "/" + LABEL], check=True)
            for _ in range(100):
                if not loaded():break
                time.sleep(0.1)
            if loaded():raise RuntimeError("LaunchAgent has not stopped")
        result = {"stopped": LABEL, "note": "Task files and plist retained"}
    else:
        result = status()
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
