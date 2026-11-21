"""Validated operator-owned profiles. Configuration is loaded once per process."""
from copy import deepcopy
import hashlib
import json
import math
import os
from pathlib import Path
import re
import shutil
import sys
from model_gateway.task_v1 import BACKEND_ROLES, ROLE_FIELDS

BACKENDS={'flux3','zimage','moss','moss-edit','h3','h3-image','vpipe-2k','vpipe-h3'}

class Registry:
    def __init__(self,path):
        self.path=Path(path).expanduser().resolve()
        data=json.loads(self.path.read_text())
        if data.get('version')!=1:raise ValueError('Unsupported registry version')
        self.engines=data['engines'];self.profiles=data['profiles']
        for name,e in self.engines.items():
            e.setdefault('supports_process_pause',False)
            if type(e['supports_process_pause']) is not bool:raise ValueError('supports_process_pause must be boolean')
            e.setdefault('contract','legacy')
            if e['contract'] not in ('legacy','gateway.task.v1'):raise ValueError('Unknown engine contract')
            if e.get('type') not in ('builtin','script'):raise ValueError(f'{name}: engine type must be builtin or script')
            if e['type']=='script':
                argv=e.get('argv')
                if not isinstance(argv,list) or not argv or not all(isinstance(a,str) and a for a in argv):raise ValueError(f'{name}: argv must be a nonempty string array')
                for arg in argv:
                    if set(re.findall(r'\{([^{}]+)\}',arg))-{'job_dir','python','config_dir'}:raise ValueError(f'{name}: unsupported argv placeholder')
                if not all(isinstance(k,str) and isinstance(v,str) for k,v in e.get('env',{}).items()):raise ValueError('env must contain strings')
        for name,p in self.profiles.items():
            if not re.fullmatch(r'[a-zA-Z0-9_.-]{1,80}',name):raise ValueError('Invalid profile ID')
            if p.get('engine') not in self.engines:raise ValueError(f'{name}: unknown engine')
            if p.get('kind') not in ('image','video','audio'):raise ValueError(f'{name}: invalid kind')
            if not isinstance(p.get('model_id'),str) or not p['model_id']:raise ValueError(f'{name}: model_id required')
            memory=p.get('available_gib_required')
            if isinstance(memory,bool) or not isinstance(memory,(int,float)) or not math.isfinite(memory) or memory<0:raise ValueError(f'{name}: invalid memory estimate')
            p.setdefault('steps',9);p.setdefault('guidance',None);p.setdefault('name',name);p.setdefault('directory','')
            if type(p['steps']) is not int or not 1<=p['steps']<=60:raise ValueError(f'{name}: steps must be 1..60')
            if p['guidance'] is not None and (type(p['guidance']) not in (int,float) or not 0<=p['guidance']<=10):raise ValueError(f'{name}: guidance must be 0..10')
            if not isinstance(p['directory'],str):raise ValueError(f'{name}: directory must be a string')
            if self.engines[p['engine']]['type']=='builtin':
                if p.get('backend') not in BACKENDS:raise ValueError(f'{name}: unknown builtin backend')
                if not p['directory']:raise ValueError(f'{name}: model directory required')
                if p['kind']!=('video' if p['backend'] in ('h3','vpipe-2k','vpipe-h3') else 'image'):raise ValueError('Builtin kind mismatch')
            elif p.get('backend'):raise ValueError('Script profiles must not set backend')
            p.setdefault('input_roles',BACKEND_ROLES.get(p.get('backend'),[]))
            if not isinstance(p['input_roles'],list) or set(p['input_roles'])-set(ROLE_FIELDS):raise ValueError('Invalid input_roles')
            for key,spec in p.get('options',{}).items():
                if spec.get('type') not in ('string','number','integer','boolean'):raise ValueError(f'{name}: invalid option {key}')
        self.revision=hashlib.sha256(json.dumps(data,sort_keys=True).encode()).hexdigest()

    def validate_options(self,name,options):
        schema=self.profiles[name].get('options',{})
        if options.keys()-schema.keys():raise ValueError('Unsupported options: '+', '.join(sorted(options.keys()-schema.keys())))
        for key,spec in schema.items():
            if key not in options:
                if spec.get('required'):raise ValueError(f'Option required: {key}')
                continue
            value=options[key];kind=spec['type']
            valid={'string':isinstance(value,str),'number':type(value) in (int,float),'integer':type(value) is int,'boolean':type(value) is bool}[kind]
            if not valid:raise ValueError(f'Invalid option type: {key}')
            if kind in ('number','integer') and (not math.isfinite(value) or value<spec.get('minimum',-math.inf) or value>spec.get('maximum',math.inf)):raise ValueError(f'Option out of range: {key}')
            if 'enum' in spec and value not in spec['enum']:raise ValueError(f'Invalid option value: {key}')

    def snapshot(self,name,lab):
        profile=deepcopy(self.profiles[name]);engine=deepcopy(self.engines[profile['engine']])
        directory=Path(profile['directory']).expanduser()
        model_dir=str(directory if directory.is_absolute() else Path(lab)/'models'/directory)
        # Snapshot selected binary paths, never API keys or the whole environment.
        env_names=('MEDIA_IRIS_BIN','MEDIA_VPIPE_BIN','MEDIA_FFMPEG_BIN','MEDIA_FFPROBE_BIN','MEDIA_H3_UPSAMPLER_DIR','MEDIA_H3_REGENERATE_LORA','MEDIA_PIKA_LORA','MEDIA_H3_PIKA_FL2VA_LORA','MEDIA_H3_TURBO_LITE_LORA')
        return {'version':1,'runner_python':os.environ.get('MEDIA_RUNNER_PYTHON',str(Path(lab)/'venv/bin/python') if engine['type']=='builtin' else sys.executable),'profile_id':name,'registry_revision':self.revision,'profile':profile,'engine':engine,'model_dir':model_dir,'lab_root':str(lab),'config_dir':str(self.path.parent),'runtime_env':{k:os.environ[k] for k in env_names if k in os.environ}}

    def probe(self,name,lab):
        p=self.profiles[name];e=self.engines[p['engine']];missing=[]
        snap=self.snapshot(name,lab)
        if not Path(snap['runner_python']).is_file():missing.append(snap['runner_python'])
        if p['directory'] and not Path(snap['model_dir']).is_dir():missing.append(snap['model_dir'])
        if e['type']=='script':
            executable=e['argv'][0].format(job_dir='',python=snap['runner_python'],config_dir=str(self.path.parent))
            if not shutil.which(executable):missing.append(executable)
            for resource in e.get('required_paths',[]):
                target=Path(resource.format(config_dir=str(self.path.parent))).expanduser()
                if not target.exists():missing.append(str(target))
        else:
            b=p['backend'];lab=Path(lab)
            paths={'h3':[lab/'h3-bin/h3'],'h3-image':[lab/'h3-bin/h3-image',lab/'venvs/h3image/bin/python'],'moss-edit':[lab/'venvs/moss-edit/bin/python',lab/'models/Moss-Image-Edit'],'flux3':[Path(os.environ.get('MEDIA_IRIS_BIN',str(Path.home()/'Desktop/gpt/local-model-lab/iris.c/iris')))],'vpipe-2k':[Path(os.environ.get('MEDIA_VPIPE_BIN','/Applications/Vpipe Manager.app/Contents/Helpers/vpipe'))],'vpipe-h3':[Path(os.environ.get('MEDIA_VPIPE_BIN','/Applications/Vpipe Manager.app/Contents/Helpers/vpipe'))]}
            for path in paths.get(b,[lab/'venv/bin/python']):
                if not path.exists():missing.append(str(path))
            if b in ('h3','vpipe-2k','vpipe-h3'):
                for tool in ('FFMPEG','FFPROBE'):
                    binary=os.environ.get('MEDIA_'+tool+'_BIN','/opt/homebrew/bin/'+tool.lower())
                    if not shutil.which(binary):missing.append(binary)
        return {'profile':name,'model_id':p['model_id'],'engine':p['engine'],'available':not missing,'missing':missing,'available_gib_required':p['available_gib_required'],'note':'Static dependency check only; weights, imports and inference require a smoke test.'}
