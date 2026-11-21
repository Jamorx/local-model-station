import json
import time
import unittest
from unittest.mock import patch
import test_media_service as support

class QueueControlTests(unittest.TestCase):
    setUp=support.MediaServiceTests.setUp
    load_module=support.MediaServiceTests.load_module
    enter_client=support.MediaServiceTests.enter_client
    submit=support.MediaServiceTests.submit

    def test_pause_resume_and_global_pause_survive_reload(self):
        a=self.submit();b=self.submit()
        self.assertEqual(self.client.post('/api/jobs/'+a['id']+'/pause').json()['status'],'paused')
        self.assertEqual(self.client.get('/api/queue').json()['order'],[b['id']])
        self.client.post('/api/queue/control',json={'action':'pause'})
        self.assertTrue(self.load_module().queue_paused)
        self.client.post('/api/jobs/'+a['id']+'/resume')
        self.assertIsNone(self.module.read(self.module.J/a['id'])['queued_since'])
        self.client.post('/api/queue/control',json={'action':'resume'})
        self.assertFalse(self.client.get('/health').json()['queue_paused'])
        self.assertFalse(self.client.get('/health').json()['execution_enabled'])
        self.assertIsNotNone(self.module.read(self.module.J/a['id'])['queued_since'])

    def test_reorder_actual_order_and_batch_partial_failure(self):
        ids=[self.submit()['id'] for _ in range(3)]
        self.client.post('/api/jobs/'+ids[2]+'/bump')
        self.assertEqual(self.client.get('/api/queue').json()['order'],[ids[2],ids[0],ids[1]])
        self.client.post('/api/jobs/'+ids[1]+'/bump')
        self.assertEqual(self.client.get('/api/queue').json()['order'],[ids[1],ids[2],ids[0]])
        self.client.post('/api/jobs/'+ids[1]+'/defer')
        self.client.post('/api/jobs/'+ids[0]+'/up')
        self.assertEqual(self.client.get('/api/queue').json()['order'],[ids[0],ids[2],ids[1]])
        result=self.client.post('/api/queue/batch',json={'action':'pause','ids':[ids[0],'missing']}).json()
        self.assertEqual([r['ok'] for r in result['results']],[True,False])
        self.assertEqual(self.client.post('/api/jobs/'+ids[0]+'/bump').status_code,409)
        self.assertEqual(self.client.post('/api/jobs/'+ids[0]+'/cancel').json()['status'],'cancelled')

    def test_manual_pauses_do_not_charge_queue_timeout(self):
        with patch.object(self.module.time,'time',return_value=100):job=self.submit()
        with patch.object(self.module.time,'time',return_value=110):self.client.post('/api/queue/control',json={'action':'pause'})
        with patch.object(self.module.time,'time',return_value=1000):
            self.assertEqual(self.module.queue_wait(self.module.read(self.module.J/job['id'])),10)
            self.client.post('/api/queue/control',json={'action':'resume'})
        with patch.object(self.module.time,'time',return_value=1005):self.client.post('/api/jobs/'+job['id']+'/pause')
        with patch.object(self.module.time,'time',return_value=2000):
            self.assertEqual(self.module.queue_wait(self.module.read(self.module.J/job['id'])),15)
            self.client.post('/api/jobs/'+job['id']+'/resume')
        with patch.object(self.module.time,'time',return_value=2003):self.assertEqual(self.module.queue_wait(self.module.read(self.module.J/job['id'])),18)

    def test_running_builtin_pause_rejected_without_signal(self):
        job=self.submit();root=self.module.J/job['id'];row=self.module.read(root);row.update(status='running',started_at=time.time());self.module.write(root,row)
        with patch.object(self.module.os,'killpg') as signal:
            self.assertEqual(self.client.post('/api/jobs/'+job['id']+'/pause').status_code,409)
            signal.assert_not_called()
        self.assertEqual(self.client.get('/api/jobs/'+job['id']).json()['status'],'running')

    @patch.dict("os.environ", {"MEDIA_MAX_PENDING": "20"})
    def test_paused_jobs_count_towards_queue_capacity(self):
        for _ in range(20):
            id=self.submit()['id'];self.client.post('/api/jobs/'+id+'/pause')
        self.assertEqual(self.client.post('/api/jobs',json={'prompt':'over capacity'}).status_code,429)

    def test_lists_all_history_and_old_paused_tasks(self):
        job=self.submit();self.client.post('/api/jobs/'+job['id']+'/pause')
        import uuid
        for i in range(105):
            id=uuid.uuid4().hex;root=self.module.J/id;root.mkdir()
            self.module.write(root,dict(job,id=id,status='cancelled',created_at=job['created_at']+i+1))
        rows=self.client.get('/api/jobs').json()['data']
        self.assertEqual(len(rows),106)
        self.assertIn(job['id'],[r['id'] for r in rows])
