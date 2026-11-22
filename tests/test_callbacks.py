import json
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi import FastAPI
from fastapi.testclient import TestClient

from model_gateway.dsh_callbacks import Callbacks


class CallbackRegistrationTests(unittest.TestCase):
    def test_pending_registrations_merge_and_later_jobs_open_a_new_callback(self):
        with tempfile.TemporaryDirectory() as directory:
            jobs = Path(directory)
            callbacks = Callbacks(jobs, lambda task_id: jobs / task_id,
                                  lambda folder, task: {}, threading.Lock())
            app = FastAPI()
            app.include_router(callbacks.router(lambda: None))
            task_ids = [f"{index:032x}" for index in range(11)]

            def set_status(task_id, status):
                folder = jobs / task_id
                folder.mkdir(exist_ok=True)
                (folder / "job.json").write_text(json.dumps({"id": task_id, "status": status}))

            for task_id in task_ids:
                set_status(task_id, "queued")

            with TestClient(app, client=("127.0.0.1", 50000)) as client, patch.object(
                callbacks, "config", return_value={"url": "http://localhost:3443"}
            ), patch.object(callbacks, "destination", side_effect=lambda body, config: body.session_id), patch.object(
                callbacks, "deliver"
            ) as deliver:
                def register(task_id, session="session-a"):
                    response = client.post("/api/v1/callbacks", json={
                        "target": "dsh-web", "session_id": session, "task_ids": [task_id],
                    })
                    self.assertEqual(response.status_code, 202)
                    return response.json()

                callback_id = register(task_ids[0])["id"]
                for task_id in task_ids[1:10]:
                    self.assertEqual(register(task_id)["id"], callback_id)
                self.assertNotEqual(register(task_ids[0], "session-b")["id"], callback_id)
                endpoint = f"/api/v1/callbacks/{callback_id}"
                self.assertEqual(client.get(endpoint).json()["task_ids"], task_ids[:10])
                listing = client.get("/api/v1/callbacks").json()
                self.assertEqual(listing["total"], 2)
                batch = next(row for row in listing["data"] if row["id"] == callback_id)
                self.assertEqual(set(batch), {"id", "status", "target", "task_ids", "session_id", "updated_at"})
                self.assertEqual(batch["task_ids"], task_ids[:10])
                self.assertEqual(batch["status"], "pending")
                self.assertEqual(batch["session_id"], "session-a")

                callback_path = callbacks.directory / f"{callback_id}.json"
                closing = threading.Event()
                for task_id in task_ids[:9]:
                    set_status(task_id, "succeeded")
                callbacks.process(callback_path, closing, "dsh-web")
                deliver.assert_not_called()

                set_status(task_ids[9], "failed")
                callbacks.process(callback_path, closing, "dsh-web")
                callbacks.process(callback_path, closing, "dsh-web")
                deliver.assert_called_once()
                self.assertEqual(register(task_ids[9]), {"id": callback_id, "status": "delivered"})
                self.assertNotEqual(register(task_ids[10])["id"], callback_id)

    def test_worker_wakes_on_task_completion_and_late_registration(self):
        with tempfile.TemporaryDirectory() as directory:
            jobs = Path(directory)
            callbacks = Callbacks(jobs, lambda task_id: jobs / task_id,
                                  lambda folder, task: {}, threading.Lock())
            app = FastAPI()
            app.include_router(callbacks.router(lambda: None))
            closing = threading.Event()
            started = threading.Event()
            delivered = threading.Event()
            task_id = "a" * 32
            task_folder = jobs / task_id
            task_folder.mkdir()
            job_file = task_folder / "job.json"
            job = {"id": task_id, "status": "queued"}
            job_file.write_text(json.dumps(job))

            original_process = callbacks.process

            def process(*args):
                original_process(*args)
                started.set()

            with TestClient(app, client=("127.0.0.1", 50000)) as client, patch.object(
                callbacks, "config", return_value={"url": "http://localhost:3443"}
            ), patch.object(callbacks, "destination", side_effect=lambda body, config: body.session_id), patch.object(
                callbacks, "process", side_effect=process
            ), patch.object(callbacks, "deliver", side_effect=lambda *args: delivered.set()) as deliver:
                def register(session):
                    response = client.post("/api/v1/callbacks", json={
                        "target": "dsh-web", "session_id": session, "task_ids": [task_id],
                    })
                    self.assertEqual(response.status_code, 202)

                register("session-a")
                worker = threading.Thread(target=callbacks.run, args=(closing, "dsh-web"), daemon=True)
                worker.start()
                try:
                    self.assertTrue(started.wait(2))
                    deliver.assert_not_called()
                    job["status"] = "succeeded"
                    job_file.write_text(json.dumps(job))
                    callbacks.task_finished(task_id)
                    self.assertTrue(delivered.wait(2))

                    delivered.clear()
                    register("session-b")
                    self.assertTrue(delivered.wait(2))
                    self.assertEqual(deliver.call_count, 2)
                finally:
                    closing.set()
                    callbacks.wake_workers()
                    worker.join(2)
                self.assertFalse(worker.is_alive())

    def test_reads_session_ids_from_legacy_targets(self):
        self.assertEqual(Callbacks.session_id({"target": ["http://localhost:3443", "session-a"]}), "session-a")
        self.assertEqual(Callbacks.session_id({
            "agent": "codex", "target": {"command": ["codex", "exec", "resume", "session-b", "-"]},
        }), "session-b")
        self.assertIsNone(Callbacks.session_id({"target": None}))
