"""Loopback HTTP entry point for the public read-only monitoring preview."""
import argparse
import os
from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import RedirectResponse

from .splash_status import snapshot
from .task_v1 import TaskRequest

app = FastAPI(title="Local Model Station", version="0.1.0")


@app.get("/", include_in_schema=False)
def home():
    return RedirectResponse("/docs")


@app.get("/health")
def health():
    return {"status": "ok", "edition": "monitor-preview"}


@app.get("/api/v1/engines/splash")
def splash_status():
    root = Path(os.environ.get("MEDIA_LAB_ROOT", str(Path.home() / "LocalModelLab"))).expanduser().resolve()
    return snapshot(root)


@app.post("/api/v1/tasks/validate")
def validate_task(task: TaskRequest):
    return {"valid": True, "schema_version": task.schema_version}


def main():
    import uvicorn

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=8777)
    args = parser.parse_args()
    uvicorn.run(app, host="127.0.0.1", port=args.port)


if __name__ == "__main__":
    main()
