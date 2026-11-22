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
