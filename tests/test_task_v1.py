import base64
import io
import json
import subprocess
import sys
from pathlib import Path
import test_media_service as support
from model_gateway.task_v1 import Progress

# Reuse isolated setup/helpers without rerunning inherited test methods.
import unittest
class TaskV1Tests(unittest.TestCase):
    setUp=support.MediaServiceTests.setUp
    load_module=support.MediaServiceTests.load_module
    enter_client=support.MediaServiceTests.enter_client

    def media(self):
        from PIL import Image
        stream=io.BytesIO();Image.new('RGB',(64,64),'blue').save(stream,format='PNG')
        b64=lambda b:base64.b64encode(b).decode()
        return {'image':b64(stream.getvalue()),'audio':b64(b'RIFF'+b'0'*40),'video':b64(b'\x00\x00\x00\x18ftypmp42'+b'0'*30)}
    def request(self):
        media=self.media()
        return {'schema_version':'gateway.task.v1','profile':'h3','kind':'video','inputs':{'prompt':'source prompt','assets':[{'role':'last_frame','data_base64':media['image']},{'role':'reference_image','data_base64':media['image']},{'role':'reference_audio','data_base64':media['audio']},{'role':'reference_video','data_base64':media['video']}]},'parameters':{'width':768,'height':768,'seconds':13,'seed':123,'steps':20,'precision':'bf16-projections','layers':50,'reuse':1},'options':{},'metadata':{'shot':'SH010'}}
    def post(self,body,key='new'):
        response=self.client.post('/api/v1/tasks',json=body,headers={'Idempotency-Key':key});self.assertEqual(response.status_code,202,response.text);return response.json()
    def test_edit_roundtrip_every_input_and_lineage(self):
        original=self.post(self.request())
        draft=self.client.get('/api/v1/tasks/'+original['id']+'/draft').json()
        self.assertEqual(len(draft['inputs']['assets']),4)
        draft['inputs']['prompt']='edited prompt';draft['parameters']['seed']=999
        edited=self.post(draft,'edit');self.assertEqual(edited['derived_from'],original['id'])
        self.assertEqual(edited['parameters']['seconds'],13);self.assertEqual(edited['parameters']['precision'],'bf16-projections');self.assertEqual(edited['metadata'],{'shot':'SH010'})
        self.assertEqual([a['sha256'] for a in original['inputs']['assets']],[a['sha256'] for a in edited['inputs']['assets']])
        self.assertEqual(self.client.get('/api/v1/tasks/'+original['id']).json()['inputs']['prompt'],'source prompt')
        self.assertEqual(self.post(draft,'edit')['id'],edited['id'])
        draft['parameters']['seed']=1000
        self.assertEqual(self.client.post('/api/v1/tasks',json=draft,headers={'Idempotency-Key':'edit'}).status_code,409)
        for a in edited['inputs']['assets']:self.assertEqual(self.client.get(a['url']).status_code,200)
        self.assertTrue((self.module.J/edited['id']/'task.json').exists())
    def test_removal_replacement_and_profile_capability(self):
        original=self.post(self.request());draft=self.client.get('/api/v1/tasks/'+original['id']+'/draft').json()
        draft['inputs']['assets']=[{'role':'first_frame','data_base64':self.media()['image']}]
        edited=self.post(draft,'replace');self.assertEqual([a['role'] for a in edited['inputs']['assets']],['first_frame'])
        draft['profile']='vpipe-2k'
        self.assertEqual(self.client.post('/api/v1/tasks',json=draft).status_code,422)
        self.assertEqual(len(self.module.accepted_jobs()),2)
    def test_asset_security_and_unknown_fields(self):
        original=self.post(self.request());draft=self.client.get('/api/v1/tasks/'+original['id']+'/draft').json()
        draft['inputs']['assets'][0]['source']['asset_id']='job.json'
        self.assertEqual(self.client.post('/api/v1/tasks',json=draft).status_code,422)
        self.assertEqual(self.client.get('/api/v1/tasks/'+original['id']+'/assets/job.json').status_code,404)
        draft['parameters']['model']='zimage'
        self.assertEqual(self.client.post('/api/v1/tasks',json=draft).status_code,422)
        empty=self.request();empty['inputs']['assets'][0]['data_base64']=''
        self.assertEqual(self.client.post('/api/v1/tasks',json=empty).status_code,422)
        draft['schema_version']='gateway.task.v2'
        self.assertEqual(self.client.post('/api/v1/tasks',json=draft).status_code,422)
    def test_compatibility_converges_and_metrics(self):
        response=self.client.post('/v1/images/generations',json={'model':'zimage','prompt':'legacy'})
        self.assertEqual(response.status_code,202)
        root=self.module.J/response.json()['id'];row=self.module.read(root)
        self.assertEqual(json.loads((root/'task.json').read_text())['schema_version'],'gateway.task.v1')
        row.update(status='failed',created_at=100,started_at=110,finished_at=140,process_tree_rss_peak_bytes=2048,error='test failure');self.module.write(root,row)
        task=self.client.get('/api/v1/tasks/'+row['id']).json()
        self.assertEqual(task['metrics']['queue_seconds'],10);self.assertEqual(task['metrics']['execution_seconds'],30);self.assertEqual(task['metrics']['total_seconds'],40)
        self.assertEqual(task['metrics']['process_tree_rss_peak_bytes'],2048);self.assertEqual(task['error']['code'],'execution_failed')
    def test_v1_script_contract_and_multi_artifacts(self):
        script=self.root/'engine.py';script.write_text("import json,sys\nfrom pathlib import Path\np=Path(sys.argv[1]);t=json.loads((p/'task.json').read_text());assert t['schema_version']=='gateway.task.v1';assert len(t['inputs']['assets'])==1\nfor name in ('one.wav','two.wav'):(p/name).write_bytes(b'RIFF-test')\n(p/'result.json').write_text(json.dumps({'schema_version':'gateway.result.v1','artifacts':[{'kind':'audio','path':'one.wav'},{'kind':'audio','path':'two.wav'}],'metrics':{'inference_seconds':0.1}}))\n")
        self.module.registry.engines['test']={'type':'script','contract':'gateway.task.v1','argv':[sys.executable,str(script),'{job_dir}']}
        self.module.MODELS['speech']={'name':'Speech','engine':'test','model_id':'weights','kind':'audio','directory':'','steps':1,'guidance':None,'input_roles':['reference_audio'],'available_gib_required':0}
        body={'profile':'speech','kind':'audio','inputs':{'prompt':'hello','assets':[{'role':'reference_audio','data_base64':self.media()['audio']}]}}
        task=self.post(body);root=self.module.J/task['id']
        subprocess.run([sys.executable,str(Path(__file__).resolve().parents[1]/'model_gateway/runner.py'),str(root)],check=True)
        row=self.module.read(root);row['status']='succeeded';self.module.write(root,row)
        result=self.client.get('/api/v1/tasks/'+task['id']).json()['result'];self.assertEqual(len(result['artifacts']),2)
        self.assertEqual(self.client.get(result['artifacts'][1]['url']).content,b'RIFF-test')
    def test_progress_bounds_and_model_identity(self):
        with self.assertRaises(ValueError):Progress(stage='generating',completed=4,total=3)
        profiles=self.client.get('/api/models').json()['data'];h3=next(m for m in profiles if m['id']=='h3');vpipe=next(m for m in profiles if m['id']=='vpipe-2k')
        self.assertEqual(h3['model_id'],vpipe['model_id']);self.assertNotEqual(h3['engine'],vpipe['engine']);self.assertNotEqual(h3['input_roles'],vpipe['input_roles'])

    def test_draft_hash_detects_changed_source(self):
        original=self.post(self.request());draft=self.client.get('/api/v1/tasks/'+original['id']+'/draft').json()
        source=draft['inputs']['assets'][0]['source']
        self.assertIn('sha256',source)
        (self.module.J/original['id']/source['asset_id']).write_bytes(b'changed outside API')
        response=self.client.post('/api/v1/tasks',json=draft)
        self.assertEqual(response.status_code,422);self.assertIn('changed',response.text)
        self.assertEqual(len(self.module.accepted_jobs()),1)

    def test_exported_schemas_match_runtime(self):
        runtime=self.client.get('/api/v1/schema').json();root=Path(__file__).resolve().parents[1]/'schemas'
        for name,key in [('task-request-v1','request'),('task-document-v1','task_document'),('engine-result-v1','engine_result'),('progress-v1','progress')]:
            self.assertEqual(json.loads((root/(name+'.schema.json')).read_text()),runtime[key])

    def test_fixed_engine_parameters_not_silently_replaced(self):
        body={'profile':'vpipe-2k','kind':'video','inputs':{'prompt':'remake','assets':[{'role':'draft','data_base64':self.media()['video']}]},'parameters':{'width':1024,'height':1024,'steps':20}}
        self.assertEqual(self.client.post('/api/v1/tasks',json=body).status_code,422)
        body['parameters']['steps']=3
        task=self.post(body);self.assertEqual(task['parameters']['steps'],3)
