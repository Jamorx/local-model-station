"""Versioned HTTP API for the gateway-owned model download queue."""
from typing import Annotated, Literal

from fastapi import APIRouter, HTTPException, Path, Query
from pydantic import BaseModel, ConfigDict, Field

from model_gateway import download_engine as engine

DownloadSource = Literal["auto", "mirror", "official"]
RepoSlug = Annotated[str, Path(pattern=r"^[A-Za-z0-9_][A-Za-z0-9_.-]{0,199}$")]


class DownloadRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    repo: str = Field(pattern=r"^[A-Za-z0-9_][A-Za-z0-9_.-]*/[A-Za-z0-9_][A-Za-z0-9_.-]*$", max_length=193)
    prefix: str = Field(default="", max_length=512)
    workers: int = Field(default=12, ge=1, le=32)
    verify: Literal["auto", "safetensors", "gguf", "size"] = "auto"
    source: DownloadSource = "auto"
    note: str = Field(default="", max_length=512)


def find(tasks, slug):
    matches = [(name, task) for name, task in tasks.items() if engine.slugify(name) == slug]
    if not matches:
        raise HTTPException(404, "下载任务不存在")
    if len(matches) > 1:
        raise HTTPException(409, "多个历史任务使用相同标识")
    return matches[0]


def require_owner():
    if engine.LOCK is None or engine.STOP.is_set():
        raise HTTPException(503, "网关尚未接管下载队列")


def router(dependencies):
    api = APIRouter(prefix="/api/v1/downloads", tags=["Model downloads"], dependencies=dependencies)

    @api.get("/status")
    def status():
        return {"owns_queue": engine.LOCK is not None and not engine.STOP.is_set()}

    @api.get("/sources")
    def sources():
        return engine.probe_sources()

    @api.get("/search")
    def search(q: Annotated[str, Query(min_length=1, max_length=200)], limit: int = Query(15, ge=1, le=50), source: DownloadSource = "auto"):
        return engine.search_repos(q, limit, source)

    @api.get("/history")
    def history():
        return engine.load_history()

    @api.get("/tasks")
    def tasks():
        rows = engine.load_tasks()
        return [engine.task_view(rows, name, task) for name, task in rows.items()]

    @api.post("/tasks", status_code=201)
    def add(body: DownloadRequest):
        require_owner()
        try:
            _, result = engine.register_task(target="", **body.model_dump())
            return result
        except FileExistsError as exc:
            raise HTTPException(409, str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc
        except (RuntimeError, OSError) as exc:
            raise HTTPException(502, "无法读取仓库或写入下载队列") from exc

    @api.get("/tasks/{slug}")
    def task_status(slug: RepoSlug):
        rows = engine.load_tasks()
        name, task = find(rows, slug)
        return engine.task_view(rows, name, task)

    @api.delete("/tasks/{slug}")
    def remove(slug: RepoSlug):
        require_owner()
        with engine.STATE_LOCK:
            rows = engine.load_tasks()
            name, task = find(rows, slug)
            engine.kill_workers(task)
            del rows[name]
            engine.save_tasks(rows)
        return {"removed": name, "files_retained": True}

    @api.put("/tasks/{slug}/source")
    def switch_source(slug: RepoSlug, source: Literal["mirror", "official"]):
        require_owner()
        with engine.STATE_LOCK:
            rows = engine.load_tasks()
            name, task = find(rows, slug)
            if task.get("done"):
                raise HTTPException(409, "下载任务已完成")
            if not task.get("paused") or engine.workers_running(name):
                raise HTTPException(409, "请先暂停任务并等待下载进程退出")
            task["source"] = source
            engine.LAST_PROBE.pop(name, None)
            engine.save_tasks(rows)
        return {"repo": name, "source": source}

    @api.post("/tasks/{slug}/{action}")
    def control(slug: RepoSlug, action: Literal["pause", "resume", "probe"]):
        require_owner()
        with engine.STATE_LOCK:
            rows = engine.load_tasks()
            name, task = find(rows, slug)
            if task.get("done"):
                raise HTTPException(409, "下载任务已完成")
            if action == "pause":
                task["paused"] = True
                engine.kill_workers(task)
                engine.write_status(task, "paused")
            else:
                engine.LAST_PROBE.pop(name, None)
                if action == "resume":
                    task["paused"] = False
            engine.save_tasks(rows)
        return {"repo": name, "paused": bool(task.get("paused"))}

    return api
