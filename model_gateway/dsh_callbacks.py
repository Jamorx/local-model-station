"""Server-owned DSH Web subscriptions for existing task batches."""
import hashlib
import json
import os
import secrets
from typing import Annotated
from uuid import NAMESPACE_URL, uuid5

from fastapi import APIRouter, Depends, Header, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field

from .batch_wait import TERMINAL, deliver_web, web_target

TaskId = Annotated[str, Field(pattern=r"^[a-f0-9]{32}$")]


class Subscription(BaseModel):
    model_config = ConfigDict(extra="forbid")
    task_ids: list[TaskId] = Field(min_length=1, max_length=64)
    session_id: str = Field(pattern=r"^session-[a-f0-9-]{36}$")


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

    def config(self):
        return json.loads(self.config_path.read_text())

    def process(self, path):
        with self.lock:
            record = json.loads(path.read_text())
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
            deliver_web(record["target"], message, str(uuid5(NAMESPACE_URL, path.stem)))
        except (OSError, ValueError):
            write(path, {**record, "status": "uncertain"})
            return
        write(path, {**record, "status": "delivered"})

    def run(self, closing):
        while not closing.is_set():
            for path in self.directory.glob("*.json"):
                if closing.is_set():
                    return
                try:
                    self.process(path)
                except (OSError, ValueError, HTTPException, KeyError):
                    continue
            closing.wait(3)

    def router(self, authorize):
        api = APIRouter(prefix="/api/v1/dsh-callbacks", tags=["Batch notifications"],
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
                config = self.config()
                target = web_target(config["url"], body.session_id)
                result_base = config.get("result_base_url", "http://127.0.0.1:8777").rstrip("/")
            except (OSError, KeyError, ValueError, TypeError) as exc:
                raise HTTPException(503, "8777 尚未配置 DSH Web 完成通知") from exc
            task_ids = sorted(set(body.task_ids))
            identifier = hashlib.sha256(json.dumps([target, task_ids], separators=(",", ":")).encode()).hexdigest()
            with self.lock:
                for task_id in task_ids:
                    self.folder(task_id)
                self.directory.mkdir(mode=0o700, exist_ok=True)
                path = self.directory / f"{identifier}.json"
                if not path.exists():
                    write(path, {"id": identifier, "status": "pending", "task_ids": task_ids,
                                 "target": target, "result_base": result_base})
                status = json.loads(path.read_text())["status"]
            return {"id": identifier, "status": status}

        @api.get("/{id}", dependencies=[Depends(authorize_callback)])
        def status(id: str):
            if len(id) != 64 or any(char not in "0123456789abcdef" for char in id):
                raise HTTPException(404, "通知不存在")
            path = self.directory / f"{id}.json"
            if not path.is_file():
                raise HTTPException(404, "通知不存在")
            record = json.loads(path.read_text())
            return {key: record[key] for key in ("id", "status", "task_ids")}

        return api
