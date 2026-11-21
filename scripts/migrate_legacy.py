#!/usr/bin/env python3
"""One-time idle migration from the old 8777 LaunchAgent; macOS only.

Copies and hashes all job files, leaves the source intact, never touches 8766.
Refuses busy queues, occupied destination directories and unexpected services.
"""
import argparse
import fcntl
import hashlib
import json
import os
from pathlib import Path
import plistlib
import shutil
import subprocess
import sys
import time
import urllib.request

ROOT=Path(__file__).resolve().parents[1]
OLD_LABEL='local.video-production.media-dev'
NEW_LABEL='local.model-gateway'
DOMAIN=f'gui/{os.getuid()}'
OPENER=urllib.request.build_opener(urllib.request.ProxyHandler({}))


def loaded(label):
    return subprocess.run(['launchctl','print',DOMAIN+'/'+label],stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL).returncode==0

def get(path,key=None):
    headers={'Authorization':'Bearer '+key} if key else {}
    with OPENER.open(urllib.request.Request('http://127.0.0.1:8777'+path,headers=headers),timeout=5) as response:return json.load(response)

def hashes(root):
    result={}
    for p in root.rglob('*'):
        if p.is_file() and p.name!='.service.lock':
            with p.open('rb') as stream:result[str(p.relative_to(root))]=hashlib.file_digest(stream,'sha256').hexdigest()
    return result

def main():
    if sys.platform!='darwin':raise SystemExit('macOS only')
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('old_repo',type=Path);args=parser.parse_args()
    plist=Path.home()/'Library/LaunchAgents'/f'{OLD_LABEL}.plist'
    config=plistlib.loads(plist.read_bytes());oldenv=config.get('EnvironmentVariables',{})
    source=Path(oldenv['MEDIA_JOBS_DIR']).resolve()
    if not source.is_relative_to(args.old_repo.resolve()):raise RuntimeError('Old task path is outside the specified old repository')
    dest=Path(os.environ.get('MEDIA_DATA_ROOT',str(Path.home()/'Library/Application Support/LocalModelGateway'))).expanduser()/'jobs'
    if dest.exists():raise RuntimeError('Destination jobs directory exists; refusing to overwrite')
    if loaded(NEW_LABEL):raise RuntimeError('New LaunchAgent already loaded')
    health=get('/health',oldenv.get('MEDIA_API_KEY'))
    if health.get('service_id')!=OLD_LABEL:raise RuntimeError('Unexpected service on 8777')
    if health.get('active_job'):raise RuntimeError('Active inference task; migration refused')
    # Read every persisted row, not the API latest-100 window.
    if any(json.loads(p.read_text()).get('status') in ('queued','running') for p in source.glob('*/job.json')):raise RuntimeError('Queue is not empty; migration refused')
    backup=ROOT/'runtime-data'/('migration-'+time.strftime('%Y%m%d-%H%M%S'));backup.mkdir(parents=True)
    shutil.copy2(plist,backup/plist.name)
    subprocess.run(['launchctl','bootout',DOMAIN+'/'+OLD_LABEL],check=True)
    try:
        for _ in range(100):
            if not loaded(OLD_LABEL):break
            time.sleep(.1)
        if loaded(OLD_LABEL):raise RuntimeError('Old LaunchAgent did not stop')
        with (source/'.service.lock').open('a+') as guard:
            fcntl.flock(guard.fileno(),fcntl.LOCK_EX|fcntl.LOCK_NB)
            # If a client raced the idle check, retain the old service and do not migrate.
            if any(json.loads(p.read_text()).get('status') in ('queued','running') for p in source.glob('*/job.json')):raise RuntimeError('Queue changed during drain check; restore old service')
            before=hashes(source)
            shutil.copytree(source,dest,ignore=shutil.ignore_patterns('.service.lock'))
            after=hashes(dest)
            if before!=after:raise RuntimeError('Copied task files failed SHA-256 verification')
            (backup/'task-hashes.json').write_text(json.dumps(before,indent=2))
        env=dict(os.environ)
        env.update({k:v for k,v in oldenv.items() if k.startswith('MEDIA_') and k not in ('MEDIA_JOBS_DIR','MEDIA_SERVICE_ID','MEDIA_PRODUCTION_ROOT') and not k.startswith(('MEDIA_REVIEW_','MEDIA_CANDIDATES_'))})
        env['MEDIA_EXECUTION_ENABLED']='1';env['MEDIA_API_PYTHON']=sys.executable
        subprocess.run([sys.executable,str(ROOT/'scripts/service.py'),'start'],env=env,check=True)
        health=get('/health',env.get('MEDIA_API_KEY'))
        if health.get('service_id')!=NEW_LABEL:raise RuntimeError('New service identity mismatch')
        result={'status':'migrated','files_verified':len(before),'jobs':len(list(dest.glob('*/job.json'))),'data_root':str(dest.parent),'backup':str(backup),'old_source_retained':True}
        (backup/'result.json').write_text(json.dumps(result,indent=2));print(json.dumps(result,indent=2))
    except Exception:
        # Never run two workers. New service may be live; leave its state untouched for review.
        if not loaded(NEW_LABEL) and not loaded(OLD_LABEL):subprocess.run(['launchctl','bootstrap',DOMAIN,str(plist)],check=True)
        raise

if __name__=='__main__':main()
