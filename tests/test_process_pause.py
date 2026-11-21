"""Real process-group suspension on an explicitly opted-in CPU test engine."""
import importlib.util,json,os,sys,tempfile,time,unittest
from pathlib import Path
from unittest.mock import patch
from fastapi.testclient import TestClient

class ProcessPauseTests(unittest.TestCase):
    def test_suspend_resume_cancel_and_queue_drain(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);engine=root/'engine.py'
            engine.write_text("import sys,time,json\nfrom pathlib import Path\np=Path(sys.argv[1])\nfor i in range(500):\n (p/'heartbeat').write_text(str(i));time.sleep(.02)\n(p/'output.wav').write_bytes(b'RIFF-test');(p/'result.json').write_text(json.dumps({'file':'output.wav'}))\n")
            config=root/'profiles.json';config.write_text(json.dumps({'version':1,'engines':{'cpu':{'type':'script','supports_process_pause':True,'argv':[sys.executable,str(engine),'{job_dir}']}},'profiles':{'cpu':{'kind':'audio','model_id':'test','engine':'cpu','available_gib_required':0}}}))
            env={'MEDIA_PROFILES_FILE':str(config),'MEDIA_JOBS_DIR':str(root/'jobs'),'MEDIA_RUNNER_PYTHON':sys.executable,'MEDIA_EXECUTION_ENABLED':'1','MEDIA_QUEUE_POLL_S':'.02','MEDIA_API_KEY':''}
            with patch.dict(os.environ,env):
                spec=importlib.util.spec_from_file_location('pause_app',Path(__file__).resolve().parents[1]/'model_gateway/app.py');app=importlib.util.module_from_spec(spec);spec.loader.exec_module(app);app.app.dependency_overrides[app.authorize]=lambda:None;app.test_process_running=lambda:False
                with patch.object(app.psutil,'virtual_memory',return_value=type('VM',(),{'available':32*2**30,'total':64*2**30,'percent':50})()),TestClient(app.app) as client:
                    def wait(id,state):
                        end=time.monotonic()+8
                        while time.monotonic()<end:
                            row=client.get('/api/jobs/'+id).json()
                            if row['status']==state:return row
                            time.sleep(.03)
                        self.fail(str(row))
                    def submit():
                        r=client.post('/api/jobs',json={'kind':'audio','model':'cpu','prompt':'heartbeat'});self.assertEqual(r.status_code,202,r.text);return r.json()['id']
                    client.post('/api/queue/control',json={'action':'pause'});first=submit();second=submit();time.sleep(.1)
                    self.assertEqual(client.get('/api/jobs/'+first).json()['status'],'queued')
                    client.post('/api/queue/control',json={'action':'resume'});wait(first,'running')
                    heartbeat=root/'jobs'/first/'heartbeat'
                    end=time.monotonic()+4
                    while not heartbeat.exists() and time.monotonic()<end:time.sleep(.02)
                    self.assertTrue(heartbeat.exists())
                    client.post('/api/queue/control',json={'action':'pause'})
                    client.post('/api/jobs/'+first+'/pause');wait(first,'paused')
                    time.sleep(.1);value=heartbeat.read_text()
                    app.JOB_TIMEOUT_S=time.time()-client.get('/api/jobs/'+first).json()['started_at']+1
                    time.sleep(1.2);self.assertEqual(value,heartbeat.read_text());self.assertEqual(client.get('/api/jobs/'+first).json()['status'],'paused')
                    client.post('/api/jobs/'+first+'/resume');wait(first,'running');time.sleep(.15);self.assertNotEqual(value,heartbeat.read_text())
                    client.post('/api/jobs/'+first+'/pause');wait(first,'paused')
                    client.post('/api/jobs/'+first+'/cancel');wait(first,'cancelled')
                    self.assertEqual(client.get('/api/jobs/'+second).json()['status'],'queued')
                    task=client.get('/api/v1/tasks/'+first).json();self.assertGreater(task['metrics']['execution_paused_seconds'],0)
                    self.assertLess(task['metrics']['active_execution_seconds'],task['metrics']['execution_seconds'])
                    client.post('/api/jobs/'+second+'/cancel')
