import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from model_gateway.registry import Registry
from model_gateway.execution import checked_output
from unittest.mock import patch

ROOT=Path(__file__).resolve().parents[1]

class RegistryTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name)
        self.config={'version':1,'engines':{'test':{'type':'script','argv':['{python}','{config_dir}/engine.py','{job_dir}']}},'profiles':{'test':{'model_id':'test-weights','kind':'audio','engine':'test','available_gib_required':0,'options':{'rate':{'type':'integer','minimum':1,'maximum':10}}}}}
        self.file=self.root/'profiles.json';self.save()
    def save(self):self.file.write_text(json.dumps(self.config))
    def test_reject_bad_config_and_options(self):
        r=Registry(self.file)
        for options in ({'unknown':1},{'rate':True},{'rate':11},{'rate':'2'}):
            with self.assertRaises(ValueError):r.validate_options('test',options)
        self.config['engines']['test']['argv']=['{prompt}'];self.save()
        with self.assertRaises(ValueError):Registry(self.file)
    def test_frozen_snapshot_and_script_execution(self):
        (self.root/'engine.py').write_text("import json,sys\nfrom pathlib import Path\np=Path(sys.argv[1]);(p/'output.wav').write_bytes(b'RIFF-test');(p/'result.json').write_text(json.dumps({'file':'output.wav'}))\n")
        r=Registry(self.file);snap=r.snapshot('test',self.root);job=self.root/'job';job.mkdir()
        (job/'execution.json').write_text(json.dumps(snap));(job/'input.json').write_text(json.dumps({'kind':'audio','model':'test','prompt':'literal $(touch bad)'}))
        self.config['engines']['test']['argv']=['/bad/executable'];self.save()
        self.assertNotEqual(Registry(self.file).revision,snap['registry_revision'])
        subprocess.run([sys.executable,str(ROOT/'model_gateway/runner.py'),str(job)],check=True)
        result=json.loads((job/'result.json').read_text());self.assertEqual(result['registry_revision'],snap['registry_revision']);self.assertTrue(checked_output(job).exists())
    def test_output_containment(self):
        job=self.root/'job';job.mkdir();outside=self.root/'outside.wav';outside.write_bytes(b'test')
        (job/'input.json').write_text('{"kind":"audio"}')
        for file in ('../outside.wav',str(outside),'link.wav'):
            if file=='link.wav':(job/file).symlink_to(outside)
            (job/'result.json').write_text(json.dumps({'file':file}))
            with self.assertRaises(ValueError):checked_output(job)
    def test_probe_missing_executable(self):
        self.config['engines']['test']['argv']=['/nonexistent/engine'];self.save()
        self.assertFalse(Registry(self.file).probe('test',self.root)['available'])
    def test_non_mac_server_refused(self):
        from model_gateway.__main__ import main
        with patch.object(sys,'platform','linux'),patch.object(sys,'argv',['lmg','serve']):
            with self.assertRaises(SystemExit):main()
