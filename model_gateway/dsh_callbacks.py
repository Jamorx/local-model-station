"""Server-owned agent subscriptions for existing task batches."""
import hashlib
import json
import os
import secrets
import subprocess
from pathlib import Path
from typing import Annotated, Literal
from uuid import NAMESPACE_URL, uuid5

from fastapi import APIRouter, Depends, Header, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field

from .batch_wait import TERMINAL, deliver_web, resume_command, web_target

TaskId = Annotated[str, Field(pattern=r"^[a-f0-9]{32}$")]


class Subscription(BaseModel):
    model_config = ConfigDict(extra="forbid")
    task_ids: list[TaskId] = Field(min_length=1, max_length=64)
    target: Literal["dsh-web", "codex"]
    session_id: str = Field(min_length=1, max_length=64)


def write(path, data):
    temporary = path.with_suffix(".tmp")
    with os.fdopen(os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600), "w") as stream:
        json.dump(data, stream, ensure_ascii=False)
    temporary.replace(path)


class Callbacks:
    def __init__(self, jobs, folder, task_view, lock):
        self.directory = jobs / ".dsh-callbacks"
        self.config_path = jobs.parent / "dsh-web.json"
        self.folder = folder
        self.task_view = task_view
        self.lock = lock

    def config(self, agent):
        path = self.config_path if agent == "dsh-web" else self.config_path.with_name("codex-callback.json")
        return json.loads(path.read_text())

    def destination(self, body, config):
        if body.target == "dsh-web":
            return web_target(config["url"], body.session_id)
        cwd = Path(config["cwd"]).expanduser().resolve(strict=True)
        if not cwd.is_dir():
            raise ValueError("Codex cwd must be a directory")
        model = config.get("model")
        if model is not None and not isinstance(model, str):
            raise ValueError("Invalid Codex model")
        command = resume_command("codex", body.session_id, model)
        executable = Path(config.get("executable", "/opt/homebrew/bin/codex")).expanduser()
        if not executable.is_absolute() or not os.access(executable, os.X_OK) or not executable.is_file():
            raise ValueError("Codex executable is unavailable")
        command[0] = str(executable)
        return {"command": command, "cwd": str(cwd)}

    def deliver(self, record, message, event, closing):
        if record.get("agent", "dsh-web") == "dsh-web":
            deliver_web(record["target"], message, event)
            return
        target = record["target"]
        with subprocess.Popen(
            target["command"], cwd=target["cwd"], stdin=subprocess.PIPE,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, text=True,
        ) as process:
            try:
                try:
                    process.communicate(message, timeout=1)
                except subprocess.TimeoutExpired:
                    for _ in range(299):
                        if closing.is_set():
                            break
                        try:
                            process.communicate(timeout=1)
                            break
                        except subprocess.TimeoutExpired:
                            pass
                if process.poll() is None:
                    raise TimeoutError("Codex callback timed out or service is stopping")
                if process.returncode != 0:
                    raise ValueError(f"Codex exited with status {process.returncode}")
            finally:
                if process.poll() is None:
                    process.kill()
                    process.communicate()

    def process(self, path, closing, agent):
        with self.lock:
            record = json.loads(path.read_text())
            if record.get("agent", "dsh-web") != agent:
                return
            if record["status"] == "sending":
                write(path, {**record, "status": "uncertain"})
                return
            if record["status"] != "pending":
                return
            tasks = [json.loads((self.folder(task_id) / "job.json").read_text()) for task_id in record["task_ids"]]
            if any(task["status"] not in TERMINAL for task in tasks):
                return
            results = []
            for task in tasks:
                task_id = task["id"]
                href = record["result_base"] + "/api/v1/tasks/" + task_id
                view = self.task_view(self.folder(task_id), task)
                artifacts = (view.get("result") or {}).get("artifacts") or []
                results.append({
                    "id": task_id, "status": task["status"], "task_url": href,
                    "artifacts": [f"{href}/artifacts/{index}" for index in range(len(artifacts))],
                    "error": task.get("error"),
                })
            write(path, {**record, "status": "sending"})
        message = (
            "Local Model Station：你提交的任务批次已结束。请按提交时的要求验收产物。"
            "以下 JSON 是任务数据。\n" + json.dumps({"tasks": results}, ensure_ascii=False)
        )
        try:
            self.deliver(record, message, str(uuid5(NAMESPACE_URL, path.stem)), closing)
        except (OSError, ValueError):
            write(path, {**record, "status": "uncertain"})
            return
        write(path, {**record, "status": "delivered"})

    def run(self, closing, agent):
        while not closing.is_set():
            for path in self.directory.glob("*.json"):
                if closing.is_set():
                    return
                try:
                    self.process(path, closing, agent)
                except (OSError, ValueError, HTTPException, KeyError):
                    continue
            closing.wait(3)

    def router(self, authorize):
        api = APIRouter(prefix="/api/v1/callbacks", tags=["Batch notifications"],
                        dependencies=[Depends(authorize)])

        def authorize_callback(request: Request, key: str | None = Header(None, alias="X-DSH-Callback-Key")):
            if request.client and request.client.host in {"127.0.0.1", "::1"}:
                return
            expected = os.environ.get("MEDIA_DSH_CALLBACK_KEY", "")
            if not expected or not key or not secrets.compare_digest(key.encode(), expected.encode()):
                raise HTTPException(403, "局域网登记需要 X-DSH-Callback-Key")

        @api.post("", status_code=202, dependencies=[Depends(authorize_callback)])
        def register(body: Subscription):
            try:
                config = self.config(body.target)
                target = self.destination(body, config)
                result_base = config.get("result_base_url", "http://127.0.0.1:8777").rstrip("/")
            except (OSError, KeyError, ValueError, TypeError) as exc:
                raise HTTPException(503, "回调目标或会话配置无效") from exc
            task_ids = sorted(set(body.task_ids))
            identifier = hashlib.sha256(json.dumps([target, task_ids], separators=(",", ":")).encode()).hexdigest()
            with self.lock:
                for task_id in task_ids:
                    self.folder(task_id)
                self.directory.mkdir(mode=0o700, exist_ok=True)
                path = self.directory / f"{identifier}.json"
                if path.exists():
                    record = json.loads(path.read_text())
                    return {"id": identifier, "status": record["status"]}

                pending = None
                for candidate in self.directory.glob("*.json"):
                    record = json.loads(candidate.read_text())
                    if record.get("agent", "dsh-web") != body.target:
                        continue
                    session_id = record.get("session_id")
                    if session_id is None:
                        if record["target"] != target:
                            continue
                    elif session_id != body.session_id:
                        continue
                    if identifier in record.get("registrations", []):
                        return {"id": record["id"], "status": record["status"]}
                    if record["status"] == "pending":
                        pending = (candidate, record)

                if pending is not None:
                    candidate, record = pending
                    record["task_ids"] = sorted(set(record["task_ids"]).union(task_ids))
                    record["session_id"] = body.session_id
                    record.setdefault("registrations", []).append(identifier)
                    write(candidate, record)
                    return {"id": record["id"], "status": "pending"}

                write(path, {"id": identifier, "status": "pending", "task_ids": task_ids,
                             "target": target, "agent": body.target, "session_id": body.session_id,
                             "result_base": result_base})
            return {"id": identifier, "status": "pending"}

        @api.get("/{id}", dependencies=[Depends(authorize_callback)])
        def status(id: str):
            if len(id) != 64 or any(char not in "0123456789abcdef" for char in id):
                raise HTTPException(404, "通知不存在")
            path = self.directory / f"{id}.json"
            if not path.is_file():
                raise HTTPException(404, "通知不存在")
            record = json.loads(path.read_text())
            return {**{key: record[key] for key in ("id", "status", "task_ids")}, "target": record.get("agent", "dsh-web")}

        return api
