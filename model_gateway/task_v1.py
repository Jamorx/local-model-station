"""Gateway Task v1: shared request, persisted task, media and result contracts."""
import base64
import hashlib
import json
import mimetypes
from pathlib import Path
import time
import os
import subprocess
from typing import Literal
from pydantic import BaseModel, Field, model_validator

VERSION='gateway.task.v1'
ROLE_FIELDS={'first_frame':'first_frame_base64','last_frame':'last_frame_base64','input_image':'input_image_base64','edit_image':'edit_images_base64','reference_image':'ref_images_base64','reference_audio':'ref_audios_base64','reference_video':'ref_videos_base64','audio_condition':'ref_audio_base64','draft':'draft_base64'}
SINGLE_FILES={'first-frame.png':'first_frame','last-frame.png':'last_frame','input-image.png':'input_image','edit-image.png':'edit_image','ref-audio.wav':'audio_condition','draft.mp4':'draft'}
PATTERNS={'edit-image-*.png':'edit_image','ref-image-*.png':'reference_image','ref-audio-*.wav':'reference_audio','ref-video-*.mp4':'reference_video'}
ROLE_LIMITS={'reference_image':12,'reference_audio':3,'reference_video':3,'edit_image':16}
BACKEND_ROLES={'h3':['first_frame','last_frame','reference_image','reference_audio','reference_video','audio_condition'],'zimage':['input_image'],'moss-edit':['input_image'],'h3-image':['edit_image'],'vpipe-2k':['draft'],'vpipe-h3':['first_frame','last_frame'],'flux3':[],'moss':[]}

class StrictModel(BaseModel):
    model_config={'extra':'forbid'}

class AssetSource(StrictModel):
    task_id:str=Field(pattern=r'^[a-f0-9]{32}$')
    asset_id:str=Field(pattern=r'^[a-z0-9][a-z0-9_.-]{0,100}$')
    sha256:str|None=Field(default=None,pattern=r'^[a-f0-9]{64}$')

class AssetInput(StrictModel):
    role:Literal['first_frame','last_frame','input_image','edit_image','reference_image','reference_audio','reference_video','audio_condition','draft']
    data_base64:str|None=Field(default=None,min_length=1,max_length=45000000)
    source:AssetSource|None=None
    @model_validator(mode='after')
    def one_source(self):
        if (self.data_base64 is None)==(self.source is None):raise ValueError('Provide exactly one of data_base64 or source')
        return self

class Inputs(StrictModel):
    prompt:str=Field(min_length=1,max_length=7000)
    assets:list[AssetInput]=Field(default_factory=list,max_length=16)

class TaskRequest(StrictModel):
    schema_version:Literal['gateway.task.v1']=VERSION
    profile:str=Field(min_length=1,max_length=80)
    kind:Literal['image','video','audio']
    inputs:Inputs
    parameters:dict=Field(default_factory=dict)
    options:dict=Field(default_factory=dict)
    metadata:dict[str,str]=Field(default_factory=dict,max_length=32)
    derived_from:str|None=Field(default=None,pattern=r'^[a-f0-9]{32}$')

class Artifact(StrictModel):
    url:str|None=None
    path:str=Field(min_length=1,max_length=256)
    kind:Literal['image','video','audio']

class EngineResult(StrictModel):
    schema_version:Literal['gateway.result.v1']='gateway.result.v1'
    artifacts:list[Artifact]=Field(min_length=1,max_length=32)
    metrics:dict=Field(default_factory=dict)
    details:dict=Field(default_factory=dict)

class Progress(StrictModel):
    schema_version:Literal['gateway.progress.v1']='gateway.progress.v1'
    stage:str=Field(min_length=1,max_length=120)
    completed:float|None=Field(default=None,ge=0)
    total:float|None=Field(default=None,gt=0)
    message:str|None=None
    @model_validator(mode='after')
    def bounded(self):
        if self.completed is not None and self.total is not None and self.completed>self.total:raise ValueError('completed exceeds total')
        return self


class StoredAsset(StrictModel):
    id:str
    role:str
    path:str
    kind:Literal['image','audio','video']
    media_type:str
    bytes:int=Field(ge=0)
    sha256:str=Field(pattern=r'^[a-f0-9]{64}$')
    url:str
    width:int|None=None
    height:int|None=None
    probe:dict|None=None
    probe_status:str|None=None

class Selection(StrictModel):
    model_id:str|None=None
    engine_id:str|None=None
    registry_revision:str|None=None

class ResolvedInputs(StrictModel):
    prompt:str
    assets:list[StoredAsset]

class TaskDocument(StrictModel):
    schema_version:Literal['gateway.task.v1']=VERSION
    id:str
    profile:str
    kind:Literal['image','video','audio']
    selection:Selection
    inputs:ResolvedInputs
    parameters:dict
    options:dict
    metadata:dict[str,str]
    derived_from:str|None=None


def asset_files(directory):
    root=Path(directory).resolve();files={}
    for name,role in SINGLE_FILES.items():
        if (root/name).is_file():files[name]=(root/name,role)
    for pattern,role in PATTERNS.items():
        for path in sorted(root.glob(pattern)):files[path.name]=(path,role)
    for path,_ in files.values():
        if not path.resolve().is_relative_to(root):raise ValueError('Input asset escapes task directory')
    return files


def assets(directory, task_id):
    result=[]
    for name,(path,role) in asset_files(directory).items():
        mime=mimetypes.guess_type(name)[0] or 'application/octet-stream'
        item={'id':name,'role':role,'path':name,'kind':mime.split('/')[0],'media_type':mime,'bytes':path.stat().st_size,'sha256':hashlib.sha256(path.read_bytes()).hexdigest(),'url':f'/api/v1/tasks/{task_id}/assets/{name}'}
        if item['kind']=='image':
            from PIL import Image
            try:
                with Image.open(path) as image:item.update(width=image.width,height=image.height)
            except Exception:pass
        if item['kind'] in ('audio','video'):
            try:
                probe=subprocess.run([os.environ.get('MEDIA_FFPROBE_BIN','/opt/homebrew/bin/ffprobe'),'-v','error','-show_entries','stream=index,codec_type,codec_name,width,height,sample_rate,channels,r_frame_rate:format=duration,size,format_name','-of','json',str(path)],capture_output=True,timeout=4)
                item['probe']=json.loads(probe.stdout) if probe.returncode==0 else None
                item['probe_status']='ok' if item['probe'] else 'unavailable'
            except (OSError,ValueError,subprocess.TimeoutExpired):item['probe_status']='unavailable'
        result.append(item)
    return result


def contract(directory,job):
    params=dict(job['parameters']);prompt=params.pop('prompt');profile=params.pop('model');kind=params.pop('kind');options=params.pop('options',{})
    params.pop('ref_image_count',None)
    snap=job.get('execution',{})
    return {'schema_version':VERSION,'id':job['id'],'profile':profile,'kind':kind,'selection':{'model_id':snap.get('profile',{}).get('model_id'),'engine_id':snap.get('profile',{}).get('engine'),'registry_revision':snap.get('registry_revision')},'inputs':{'prompt':prompt,'assets':assets(directory,job['id'])},'parameters':params,'options':options,'metadata':job.get('metadata',{}),'derived_from':job.get('derived_from') or job.get('retried_from')}


def persist(directory,job):
    root=Path(directory);task=TaskDocument(**contract(root,job)).model_dump(exclude_none=True);tmp=root/'task.tmp';tmp.write_text(json.dumps(task,ensure_ascii=False,indent=2));tmp.replace(root/'task.json');return task


def draft(directory,job):
    path=Path(directory)/'task.json'
    task=json.loads(path.read_text()) if path.exists() else contract(directory,job)
    return {'schema_version':VERSION,'profile':task['profile'],'kind':task['kind'],'inputs':{'prompt':task['inputs']['prompt'],'assets':[{'role':a['role'],'source':{'task_id':job['id'],'asset_id':a['id'],'sha256':a['sha256']}} for a in task['inputs']['assets']]},'parameters':task['parameters'],'options':task['options'],'metadata':task['metadata'],'derived_from':job['id']}


def to_legacy(body,folder,allowed_parameters):
    unknown=set(body.parameters)-set(allowed_parameters)
    if unknown:raise ValueError('Unsupported parameters: '+', '.join(sorted(unknown)))
    payload=dict(body.parameters,model=body.profile,kind=body.kind,prompt=body.inputs.prompt,options=body.options,metadata=body.metadata,derived_from=body.derived_from)
    if body.derived_from:folder(body.derived_from)
    counts={}
    for asset in body.inputs.assets:
        counts[asset.role]=counts.get(asset.role,0)+1
        if counts[asset.role]>ROLE_LIMITS.get(asset.role,1):raise ValueError('Too many assets for role: '+asset.role)
        encoded=asset.data_base64
        if asset.source:
            available=asset_files(folder(asset.source.task_id))
            if asset.source.asset_id not in available:raise ValueError('Source input asset does not exist')
            path,source_role=available[asset.source.asset_id]
            # Same media type may change role; arbitrary task files can never be referenced.
            target_kind='video' if asset.role in ('draft','reference_video') else 'audio' if asset.role in ('audio_condition','reference_audio') else 'image'
            if mimetypes.guess_type(path.name)[0].split('/')[0]!=target_kind:raise ValueError('Asset media kind does not match role')
            raw=path.read_bytes()
            if asset.source.sha256 and hashlib.sha256(raw).hexdigest()!=asset.source.sha256:raise ValueError('Source asset changed since draft creation')
            encoded=base64.b64encode(raw).decode()
        field=ROLE_FIELDS[asset.role]
        if asset.role in ROLE_LIMITS:payload.setdefault(field,[]).append(encoded)
        else:payload[field]=encoded
    return payload


def task_view(directory,job):
    root=Path(directory)
    task=json.loads((root/'task.json').read_text()) if (root/'task.json').exists() else contract(root,job)
    now=time.time();created=job['created_at'];started=job.get('started_at');finished=job.get('finished_at')
    result=json.loads((root/'result.json').read_text()) if (root/'result.json').exists() else None
    metrics={'queue_seconds':round((started or finished or now)-created,3),'execution_seconds':round((finished or now)-started,3) if started else None,'total_seconds':round((finished or now)-created,3),'process_tree_rss_peak_bytes':job.get('process_tree_rss_peak_bytes'),'mlx_peak_bytes':(result or {}).get('mlx_peak_bytes'),'memory_note':job.get('memory_note')}
    paused=job.get('execution_paused_seconds',0)+(max(0,(finished or now)-job['paused_at']) if job.get('paused_from')=='running' and job.get('paused_at') else 0)
    metrics['execution_paused_seconds']=round(paused,3)
    metrics['active_execution_seconds']=round(max(0,metrics['execution_seconds']-paused),3) if started else None
    normalized_result=None
    if result is not None:
        raw_artifacts=result.get('artifacts') or [{'path':result['file'],'kind':task['kind']}]
        normalized_result={'schema_version':'gateway.result.v1','artifacts':[dict(a,url=f'/api/v1/tasks/{job["id"]}/artifacts/{i}') for i,a in enumerate(raw_artifacts)],'metrics':dict(result.get('metrics',{}),**{k:result[k] for k in ('total_seconds','load_seconds','generation_seconds','mlx_peak_bytes') if k in result}),'details':result}
    return dict(task,status=job['status'],timestamps={key:job.get(key) for key in ('created_at','started_at','finished_at')},metrics=metrics,progress=job.get('stage_info') or {'stage':job.get('stage',job['status'])},error={'code':'execution_failed' if job['status']=='failed' else 'interrupted' if job['status']=='interrupted' else 'cancelled','message':job['error']} if job.get('error') else None,controls=job.get('controls'),pause_requested=job.get('pause_requested',False),cancel_requested=job.get('cancel_requested',False),paused_from=job.get('paused_from'),queue_wait_seconds=job.get('queue_wait_seconds'),result=normalized_result,execution=job.get('execution'),client=job.get('client'),pid=job.get('pid'),queue_position=job.get('queue_position'),output_url=job.get('output_url'),log_url=f'/api/jobs/{job["id"]}/log')
