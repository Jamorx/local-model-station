"""macOS gateway entry point and HTTP client."""
import argparse
import json
import os
from pathlib import Path
import sys
import urllib.request
from .registry import Registry


def main():
    parser=argparse.ArgumentParser(description='Local Model Gateway — macOS only (v0.1)')
    parser.add_argument('--url',default='http://127.0.0.1:8777')
    sub=parser.add_subparsers(dest='action',required=True)
    serve=sub.add_parser('serve');serve.add_argument('--host',default='127.0.0.1');serve.add_argument('--port',type=int,default=8777);serve.add_argument('--execute',action='store_true')
    sub.add_parser('doctor');sub.add_parser('models')
    for name in ('queue','queue-pause','queue-resume'):sub.add_parser(name)
    for name in ('submit','validate'):
        p=sub.add_parser(name);p.add_argument('file',type=Path)
        if name=='submit':p.add_argument('--key',required=True,help='Stable key for one logical submission')
    for name in ('status','cancel','retry','draft','pause','resume','bump','defer','up','down'):
        p=sub.add_parser(name);p.add_argument('id')
        if name=='retry':p.add_argument('--key',required=True)
    args=parser.parse_args()
    if args.action=='serve':
        if sys.platform!='darwin':parser.error('首期仅支持 macOS；Windows/Linux 尚不支持')
        os.environ['MEDIA_EXECUTION_ENABLED']='1' if args.execute else '0'
        import uvicorn
        uvicorn.run('model_gateway.app:app',host=args.host,port=args.port,workers=1)
        return
    if args.action=='doctor':
        registry=Registry(os.environ.get('MEDIA_PROFILES_FILE',str(Path(__file__).resolve().parents[1]/'config/profiles.json')))
        lab=Path(os.environ.get('MEDIA_LAB_ROOT',str(Path.home()/'LocalModelLab'))).expanduser()
        result={'platform':sys.platform,'supported':sys.platform=='darwin','python':sys.version.split()[0],'registry_revision':registry.revision,'profiles':[registry.probe(p,lab) for p in registry.profiles]}
        print(json.dumps(result,ensure_ascii=False,indent=2))
        if not result['supported']:raise SystemExit(1)
        return
    route={'models':'/api/models','submit':'/api/jobs','validate':'/api/validate','status':'/api/v1/tasks/','cancel':'/api/v1/tasks/','retry':'/api/v1/tasks/','draft':'/api/v1/tasks/','pause':'/api/v1/tasks/','resume':'/api/v1/tasks/','bump':'/api/v1/tasks/','defer':'/api/jobs/','up':'/api/jobs/','down':'/api/jobs/','queue':'/api/queue','queue-pause':'/api/queue/control','queue-resume':'/api/queue/control'}[args.action]
    data=None;headers={}
    if args.action in ('submit','validate'):
        body=json.loads(args.file.read_text());data=json.dumps(body).encode()
        if body.get('schema_version')=='gateway.task.v1':route='/api/v1/tasks'+('/validate' if args.action=='validate' else '')
    if args.action in ('status','cancel','retry','draft','pause','resume','bump','defer','up','down'):route+=args.id
    if args.action=='draft':route+='/draft'
    if args.action in ('cancel','retry','pause','resume','bump','defer','up','down'):route+='/'+args.action;data=b'{}'
    if args.action in ('queue-pause','queue-resume'):data=json.dumps({'action':args.action.split('-')[1]}).encode()
    if data is not None:headers['Content-Type']='application/json'
    if getattr(args,'key',None):headers['Idempotency-Key']=args.key
    if os.environ.get('MEDIA_API_KEY'):headers['Authorization']='Bearer '+os.environ['MEDIA_API_KEY']
    request=urllib.request.Request(args.url.rstrip('/')+route,data=data,headers=headers)
    opener=urllib.request.build_opener(urllib.request.ProxyHandler({}))
    with opener.open(request,timeout=30) as response:print(json.dumps(json.load(response),ensure_ascii=False,indent=2))

if __name__=='__main__':main()
