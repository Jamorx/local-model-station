"""Exercise the real queue and subprocess lifecycle using a tiny fake engine."""
import importlib.util
import json
import os
from pathlib import Path
import sys
import tempfile
import time
import unittest
from unittest.mock import patch
from fastapi.testclient import TestClient

ROOT=Path(__file__).resolve().parents[1]

class QueueTests(unittest.TestCase):
    def test_success_cancel_failure_and_retry_snapshot(self):
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp)
            engine=root/'engine.py'
            engine.write_text("import sys,json,time\nfrom pathlib import Path\np=Path(sys.argv[1]);x=json.loads((p/'input.json').read_text())\nif x['prompt']=='wait':time.sleep(30)\nif x['prompt']=='fail':raise RuntimeError('expected failure')\n(p/'output.wav').write_bytes(b'RIFF-test');(p/'result.json').write_text(json.dumps({'file':'output.wav'}))\n")
            config={'version':1,'engines':{'test':{'type':'script','argv':[sys.executable,str(engine),'{job_dir}']}},'profiles':{'fake':{'kind':'audio','model_id':'fake','engine':'test','available_gib_required':0}}}
            registry=root/'profiles.json';registry.write_text(json.dumps(config))
            env={'MEDIA_PROFILES_FILE':str(registry),'MEDIA_JOBS_DIR':str(root/'jobs'),'MEDIA_EXECUTION_ENABLED':'1','MEDIA_RUNNER_PYTHON':sys.executable,'MEDIA_QUEUE_POLL_S':'0.02','MEDIA_API_KEY':''}
            with patch.dict(os.environ,env):
                spec=importlib.util.spec_from_file_location('test_queue_app',ROOT/'model_gateway/app.py');module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
                module.app.dependency_overrides[module.authorize]=lambda:None
                # Stable resource conditions; subprocess behavior stays real.
                module.test_process_running=lambda:False
                with patch.object(module.psutil,'virtual_memory',return_value=type('VM',(),{'available':32*2**30,'total':64*2**30,'percent':50})()),TestClient(module.app) as client:
                    def wait(id,states):
                        deadline=time.monotonic()+12
                        while time.monotonic()<deadline:
                            job=client.get('/api/jobs/'+id).json()
                            if job['status'] in states:return job
                            time.sleep(.05)
                        self.fail(str(job))
                    def submit(prompt):
                        r=client.post('/api/jobs',json={'kind':'audio','model':'fake','prompt':prompt});self.assertEqual(r.status_code,202,r.text);return r.json()['id']
                    good=submit('hello');wait(good,{'succeeded'});self.assertEqual(client.get('/api/jobs/'+good+'/output').content,b'RIFF-test')
                    slow=submit('wait');wait(slow,{'running'});client.post('/api/jobs/'+slow+'/cancel');wait(slow,{'cancelled'})
                    failed=submit('fail');wait(failed,{'failed'})
                    # Configuration edits cannot change accepted executions or retries.
                    config['engines']['test']['argv']=['/missing'];registry.write_text(json.dumps(config))
                    retry=client.post('/api/jobs/'+failed+'/retry').json();self.assertEqual(retry['execution'],client.get('/api/jobs/'+failed).json()['execution']);wait(retry['id'],{'failed'})
                    self.assertEqual(client.post('/api/jobs',json={'model':'fake','kind':'audio','prompt':'x','typo':1}).status_code,422)
                    self.assertEqual(client.get('/api/production/series').status_code,404)
