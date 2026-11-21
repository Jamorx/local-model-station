import contextlib,io,json,sys,unittest
from unittest.mock import patch,MagicMock
from model_gateway.__main__ import main

class QueueCliTests(unittest.TestCase):
    def test_queue_and_task_commands(self):
        for action,route,method in [('queue','/api/queue','GET'),('queue-pause','/api/queue/control','POST'),('queue-resume','/api/queue/control','POST'),('pause','/api/v1/tasks/test/pause','POST'),('resume','/api/v1/tasks/test/resume','POST'),('bump','/api/v1/tasks/test/bump','POST'),('defer','/api/jobs/test/defer','POST'),('up','/api/jobs/test/up','POST'),('down','/api/jobs/test/down','POST')]:
            with self.subTest(action=action):
                response=MagicMock();response.__enter__.return_value=io.BytesIO(b'{}');opener=MagicMock();opener.open.return_value=response
                args=['lmg',action]+([] if action.startswith('queue') else ['test'])
                with patch.object(sys,'argv',args),patch('urllib.request.build_opener',return_value=opener),contextlib.redirect_stdout(io.StringIO()):main()
                request=opener.open.call_args.args[0]
                self.assertEqual(request.full_url,'http://127.0.0.1:8777'+route);self.assertEqual(request.get_method(),method)
                if action.startswith('queue-'):self.assertEqual(json.loads(request.data),{'action':action.split('-')[1]})
