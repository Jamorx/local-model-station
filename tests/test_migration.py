import importlib.util
import json
from pathlib import Path
import plistlib
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT=Path(__file__).resolve().parents[1]

class MigrationTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup);self.root=Path(self.tmp.name)
        spec=importlib.util.spec_from_file_location('migration_test',ROOT/'scripts/migrate_legacy.py');self.m=importlib.util.module_from_spec(spec);spec.loader.exec_module(self.m)
        self.old=self.root/'old';self.jobs=self.old/'jobs';self.jobs.mkdir(parents=True)
        row=self.jobs/'a';row.mkdir();(row/'job.json').write_text(json.dumps({'status':'succeeded'}));(row/'output.wav').write_bytes(b'test-output')
        self.dest=self.root/'destination'
        plist=self.root/'Library/LaunchAgents'/f'{self.m.OLD_LABEL}.plist';plist.parent.mkdir(parents=True);plist.write_bytes(plistlib.dumps({'EnvironmentVariables':{'MEDIA_JOBS_DIR':str(self.jobs)}}))
        self.m.ROOT=self.root/'new';self.m.ROOT.mkdir()
    def invoke(self,active=None):
        states=[{'service_id':self.m.OLD_LABEL,'active_job':active},{'service_id':self.m.NEW_LABEL}]
        with patch.object(Path,'home',return_value=self.root),patch.dict('os.environ',{'MEDIA_DATA_ROOT':str(self.dest)}),patch.object(sys,'argv',['migrate',str(self.old)]),patch.object(self.m,'loaded',return_value=False),patch.object(self.m,'get',side_effect=states),patch.object(self.m.subprocess,'run') as run:
            if active:
                with self.assertRaisesRegex(RuntimeError,'Active inference'):self.m.main()
                run.assert_not_called()
            else:self.m.main()
    def test_busy_migration_does_not_stop_or_copy(self):
        self.invoke(active='active-task');self.assertFalse(self.dest.exists())
    def test_idle_copy_preserves_original_and_hashes(self):
        before=self.m.hashes(self.jobs);self.invoke();self.assertEqual(before,self.m.hashes(self.jobs));self.assertEqual(before,self.m.hashes(self.dest/'jobs'))
        self.assertTrue(list((self.m.ROOT/'runtime-data').glob('*/task-hashes.json')))
    def test_existing_destination_refused(self):
        (self.dest/'jobs').mkdir(parents=True)
        with self.assertRaisesRegex(RuntimeError,'Destination'):self.invoke()
