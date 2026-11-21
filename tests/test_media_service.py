"""Compatibility tests for the copied API; all jobs and models are temporary."""
import base64
import importlib.util
import io
import json
import os
import sys
import tempfile
import unittest
import uuid
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient
from PIL import Image


class MediaServiceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        lab = self.root / 'lab'
        for model in ['MiniMax-H3-Image', 'Z-Image-Turbo', 'MiniMax-H3']:
            (lab / 'models' / model).mkdir(parents=True)
        self.env = {'MEDIA_LAB_ROOT': str(lab), 'MEDIA_JOBS_DIR': str(self.root / 'jobs'),
                    'MEDIA_EXECUTION_ENABLED': '0', 'MEDIA_CANDIDATES_ENABLED': '1', 'MEDIA_SERVICE_ID': 'test-isolated'}
        self.module = self.load_module()
        self.module.registry.probe = lambda name,lab: {'available':True}
        self.client = self.enter_client(self.module)

    def load_module(self):
        name = 'isolated_media_' + uuid.uuid4().hex
        path = Path(__file__).resolve().parents[1] / 'model_gateway' / 'app.py'
        spec = importlib.util.spec_from_file_location(name, path)
        module = importlib.util.module_from_spec(spec)
        sys.modules[name] = module
        self.addCleanup(sys.modules.pop, name, None)
        with patch.dict(os.environ, self.env):
            spec.loader.exec_module(module)
        module.app.dependency_overrides[module.authorize] = lambda: None
        return module

    def enter_client(self, module):
        client = TestClient(module.app)
        client.__enter__()
        self.addCleanup(client.__exit__, None, None, None)
        return client

    def submit(self, **kwargs):
        body = {'kind': 'image', 'model': 'zimage', 'prompt': 'test only', 'width': 512, 'height': 512}
        body.update(kwargs)
        response = self.client.post('/api/jobs', json=body)
        self.assertEqual(response.status_code, 202, response.text)
        return response.json()

    def test_health_and_paused_queue(self):
        health = self.client.get('/health').json()
        self.assertFalse(health['execution_enabled'])
        job = self.submit()
        self.assertEqual(self.client.get('/api/jobs/' + job['id']).json()['status'], 'queued')
        self.assertIsNone(self.module.active)

    def test_cancel_retry_keeps_original(self):
        original = self.submit()
        self.assertEqual(self.client.post('/api/jobs/' + original['id'] + '/cancel').json()['status'], 'cancelled')
        response = self.client.post('/api/jobs/' + original['id'] + '/retry')
        self.assertEqual(response.status_code, 202, response.text)
        retry = response.json()
        self.assertNotEqual(retry['id'], original['id'])
        self.assertEqual(retry['retried_from'], original['id'])
        self.assertEqual(retry['status'], 'queued')
        self.assertEqual(self.client.get('/api/jobs/' + original['id']).json()['status'], 'cancelled')

    def test_edit_image_survives_retry(self):
        stream = io.BytesIO()
        Image.new('RGB', (64, 64), 'red').save(stream, format='PNG')
        encoded = base64.b64encode(stream.getvalue()).decode()
        job = self.submit(model='h3-image', edit_image_base64=encoded)
        source = self.module.J / job['id'] / 'edit-image-00.png'
        self.assertTrue(source.is_file())
        self.client.post('/api/jobs/' + job['id'] + '/cancel')
        response = self.post_retry(job['id'])
        copy = self.module.J / response.json()['id'] / 'edit-image-00.png'
        self.assertEqual(copy.read_bytes(), source.read_bytes())

    def post_retry(self, job_id):
        response = self.client.post('/api/jobs/' + job_id + '/retry')
        self.assertEqual(response.status_code, 202, response.text)
        return response

    def png_b64(self, color):
        stream = io.BytesIO()
        Image.new('RGB', (64, 64), color).save(stream, format='PNG')
        return base64.b64encode(stream.getvalue()).decode()

    def test_multi_edit_images_h3_image(self):
        refs = [self.png_b64(c) for c in ('red', 'green', 'blue')]
        job = self.submit(model='h3-image', edit_images_base64=refs)
        folder = self.module.J / job['id']
        for name in ('edit-image-00.png', 'edit-image-01.png', 'edit-image-02.png'):
            self.assertTrue((folder / name).is_file(), name)
        self.assertEqual(self.module.read(folder)['parameters']['edit_image_count'], 3)
        self.client.post('/api/jobs/' + job['id'] + '/cancel')
        response = self.post_retry(job['id'])
        copy = self.module.J / response.json()['id']
        for name in ('edit-image-00.png', 'edit-image-01.png', 'edit-image-02.png'):
            self.assertEqual((copy / name).read_bytes(), (folder / name).read_bytes())
        too_many = [self.png_b64('red')] * 17
        response = self.client.post('/api/jobs', json={'kind': 'image', 'model': 'h3-image',
            'prompt': 'x', 'width': 512, 'height': 512, 'edit_images_base64': too_many})
        self.assertEqual(response.status_code, 422)
        response = self.client.post('/api/jobs', json={'kind': 'image', 'model': 'zimage',
            'prompt': 'x', 'width': 512, 'height': 512, 'edit_images_base64': refs[:1]})
        self.assertEqual(response.status_code, 422)





    def test_idempotent_concurrent_submit_and_conflict(self):
        from concurrent.futures import ThreadPoolExecutor
        payload = {'prompt': 'one intent'}
        headers = {'Idempotency-Key': 'concurrent-key'}
        with ThreadPoolExecutor(max_workers=4) as pool:
            responses = list(pool.map(lambda _: self.client.post('/api/jobs', json=payload, headers=headers), range(4)))
        self.assertTrue(all(r.status_code == 202 for r in responses))
        self.assertEqual(len({r.json()['id'] for r in responses}), 1)
        self.assertEqual(len(self.module.accepted_jobs()), 1)
        conflict = self.client.post('/api/jobs', json={'prompt': 'different'}, headers=headers)
        self.assertEqual(conflict.status_code, 409)
        other = self.load_module()
        record = other.replay_submission('concurrent-key', self.module.accepted_jobs()[0]['request_fingerprint'])
        self.assertEqual(record['id'], responses[0].json()['id'])





    def finish_candidate(self, job):
        folder = self.module.J / job['id']
        Image.new('RGB', (8, 8), 'blue').save(folder / 'output.png')
        (folder / 'result.json').write_text(json.dumps({'file': 'output.png'}))
        record = self.module.read(folder); record['status'] = 'succeeded'; self.module.write(folder, record)




    def test_huobao_image_protocol_and_unknown_model(self):
        body = {'model':'zimage','prompt':'portrait','size':'1080x1920','n':1,'response_format':'url'}
        first = self.client.post('/v1/images/generations', json=body, headers={'Idempotency-Key':'image-protocol'})
        self.assertEqual(first.status_code, 202, first.text)
        job = self.client.get('/api/jobs/' + first.json()['task_id']).json()
        self.assertEqual((job['parameters']['width'], job['parameters']['height']), (864,1536))
        self.assertEqual(self.client.post('/v1/images/generations',json=body,headers={'Idempotency-Key':'image-protocol'}).json()['task_id'],job['id'])
        self.assertEqual(self.client.get('/v1/images/task/'+job['id']).json()['status'],'processing')
        self.finish_candidate(job)
        result = self.client.get('/v1/images/task/'+job['id']).json()
        self.assertEqual(result['status'],'completed')
        self.assertEqual(self.client.get(result['image_url']).status_code,200)
        self.assertEqual(self.client.get('/api/jobs/'+job['id']+'/output.mp4').status_code,404)
        self.assertTrue(result['image_url'].endswith('/api/jobs/'+job['id']+'/output.png'))
        self.assertEqual(self.client.post('/v1/images/generations',json={**body,'model':'gpt-image-unknown'}).status_code,422)
        self.assertEqual(self.client.post('/v1/images/generations',json={**body,'n':3}).status_code,422)

    def test_huobao_edits_reference_preserved_and_not_silently_dropped(self):
        stream=io.BytesIO();Image.new('RGB',(64,64),'red').save(stream,format='PNG')
        data={'model':'h3-image','prompt':'edit','size':'512x512','n':'1'}
        files=[('image[]',('ref.png',stream.getvalue(),'image/png'))]
        response=self.client.post('/v1/images/edits',data=data,files=files)
        self.assertEqual(response.status_code,202,response.text)
        self.assertTrue((self.module.J/response.json()['id']/'edit-image-00.png').exists())
        self.assertEqual(self.client.post('/v1/images/edits',data=data,files=files+files).status_code,422)

    def test_huobao_video_multimodal_queue_retry_and_runner(self):
        import runpy
        stream=io.BytesIO();Image.new('RGB',(64,64),'blue').save(stream,format='PNG')
        image='data:image/png;base64,'+base64.b64encode(stream.getvalue()).decode()
        video='data:video/mp4;base64,'+base64.b64encode(b'\x00\x00\x00\x18ftypmp42'+b'0'*30).decode()
        audio='data:audio/wav;base64,'+base64.b64encode(b'RIFF'+b'0'*40).decode()
        body={'model':'MiniMax-H3','duration':6,'resolution':'768P','ratio':'9:16','content':[{'type':'text','text':'hello'},{'type':'image_url','role':'last_frame','image_url':{'url':image}},{'type':'video_url','role':'reference_video','video_url':{'url':video}},{'type':'audio_url','role':'reference_audio','audio_url':{'url':audio}}]}
        with patch('model_gateway.huobao_compat.normalize_audio',return_value=b'RIFF'+b'0'*40):
            response=self.client.post('/v2/video_generation',json=body)
        self.assertEqual(response.status_code,202,response.text)
        id=response.json()['task_id'];folder=self.module.J/id
        self.assertEqual(self.client.get('/v2/query/video_generation/'+id).json()['task']['status'],'processing')
        self.client.post('/api/jobs/'+id+'/cancel')
        new=self.client.post('/api/jobs/'+id+'/retry').json();copy=self.module.J/new['id']
        for name in ('last-frame.png','ref-video-00.mp4','ref-audio-00.wav'):
            self.assertEqual((folder/name).read_bytes(),(copy/name).read_bytes())
        runner=Path(__file__).resolve().parents[1]/'model_gateway/builtin_runner.py'
        with patch.object(sys,'argv',[str(runner),str(copy)]),patch('subprocess.run') as run,patch('subprocess.check_output',return_value=b'{"streams":[],"format":{}}'):
            runpy.run_path(str(runner),run_name='__main__')
        cmd=run.call_args.args[0]
        for flag in ('--last-frame','--ref-video','--ref-audio'):self.assertIn(flag,cmd)
        self.assertEqual(cmd[cmd.index('--frames')+1],'144')

    def test_huobao_unsupported_video_does_not_enqueue(self):
        body={'model':'MiniMax-H3','content':[{'type':'text','text':'test'}],'resolution':'2K'}
        self.assertEqual(self.client.post('/v2/video_generation',json=body).status_code,422)
        body.update(resolution='768P',content=[{'type':'image_url','role':'first_frame','image_url':{'url':'http://untrusted.example/image'}}])
        self.assertEqual(self.client.post('/v2/video_generation',json=body).status_code,422)
        self.assertEqual(self.module.accepted_jobs(),[])

    def test_huobao_text_json_and_sse_passthrough(self):
        from unittest.mock import MagicMock
        model='Qwen3.8-Flash-Next-GSQ-RCO-IQ3_S'
        for streaming,raw in [(False,b'{"choices":[{"message":{"content":"OK"}}]}'),(True,b'data: {"choices":[]}\n\ndata: [DONE]\n\n')]:
            response=MagicMock();response.read1.side_effect=[raw,b'']
            opener=MagicMock();opener.open.return_value=response
            with patch('model_gateway.huobao_compat.urllib.request.build_opener',return_value=opener):
                result=self.client.post('/v1/chat/completions',json={'model':model,'messages':[{'role':'user','content':'hi'}],'stream':streaming})
            self.assertEqual(result.status_code,200)
            self.assertEqual(result.content,raw)
            response.close.assert_called_once()
            upstream=json.loads(opener.open.call_args.args[0].data)
            self.assertEqual(upstream['stream'],streaming)
        self.assertEqual(self.client.post('/v1/chat/completions',json={'model':'unknown'}).status_code,422)

    def test_candidate_routes_and_automation_disabled_for_gateway(self):
        self.env['MEDIA_CANDIDATES_ENABLED'] = '0'
        module = self.load_module()
        self.assertFalse(hasattr(module, 'candidate_tick'))
        client = TestClient(module.app)
        self.assertEqual(client.get('/api/candidate-groups').status_code, 404)
        self.assertEqual(client.post('/api/candidate-groups', json={}).status_code, 404)
        self.assertEqual(client.get('/api/compatibility').status_code, 200)

    def test_two_service_instances_cannot_share_job_directory(self):
        other = self.load_module()
        with self.assertRaisesRegex(RuntimeError, 'already owns'):
            with TestClient(other.app):
                pass

    def test_strict_protocol_and_native_fields(self):
        self.assertEqual(self.client.post('/v1/images/generations',json={'prompt':'x','negative_prompt':'y'}).status_code,422)
        self.assertEqual(self.client.post('/v1/images/generations',json={'prompt':'x','response_format':'b64_json'}).status_code,422)
        self.assertEqual(self.client.post('/v2/video_generation',json={'content':[], 'watermark':False}).status_code,422)
        self.assertEqual(self.client.post('/api/jobs',json={'prompt':'x','unknown_option':True}).status_code,422)
        self.assertEqual(self.client.post('/api/validate',json={'prompt':'x'}).status_code,200)
        self.assertEqual(self.module.accepted_jobs(),[])

    def test_configured_key_required_on_lan(self):
        self.module.app.dependency_overrides.clear()
        with patch.dict(os.environ,{'MEDIA_API_KEY':'test-secret'}):
            self.assertEqual(self.client.get('/api/models').status_code,401)
            self.assertEqual(self.client.get('/api/models',headers={'Authorization':'Bearer test-secret'}).status_code,200)

    def test_legacy_directory_is_rejected(self):
        self.env['MEDIA_JOBS_DIR'] = str(Path(self.env['MEDIA_LAB_ROOT']) / 'media-jobs')
        with self.assertRaisesRegex(RuntimeError, 'isolated'):
            self.load_module()


if __name__ == '__main__':
    unittest.main()
