import pathlib,json,time,os,uuid,threading,subprocess,signal,secrets,re,base64,io,shutil,hashlib,fcntl
from contextlib import asynccontextmanager
from typing import Literal
import sys
from model_gateway.registry import Registry
from model_gateway import download_engine, splash_status
from model_gateway.dsh_callbacks import Callbacks
from model_gateway.downloads import router as download_router
from model_gateway.execution import checked_output
import psutil
from fastapi import FastAPI,Depends,HTTPException,Header,Request
from fastapi.staticfiles import StaticFiles
from fastapi.responses import HTMLResponse,FileResponse,PlainTextResponse,Response,JSONResponse
from pydantic import BaseModel,Field,model_validator
from PIL import Image
from model_gateway import task_v1 as protocol
from pydantic import ValidationError
R=pathlib.Path(os.environ.get('MEDIA_LAB_ROOT',str(pathlib.Path.home()/'LocalModelLab'))).expanduser().resolve()
ROOT=pathlib.Path(__file__).resolve().parent
J=pathlib.Path(os.environ.get('MEDIA_JOBS_DIR',str(pathlib.Path.home()/'Library/Application Support/LocalModelGateway/jobs'))).expanduser().resolve()
if J == (R/'media-jobs').resolve():raise RuntimeError('Clone must use an isolated MEDIA_JOBS_DIR, not legacy media-jobs')
J.mkdir(parents=True,exist_ok=True)
EXECUTION_ENABLED=os.environ.get('MEDIA_EXECUTION_ENABLED','0')=='1'
QUEUE_STATE=J/'queue-state.json'
queue_paused=json.loads(QUEUE_STATE.read_text()).get('paused',False) if QUEUE_STATE.exists() else False
SERVICE_ID=os.environ.get('MEDIA_SERVICE_ID','local-model-gateway')
RUNNER_PYTHON=os.environ.get('MEDIA_RUNNER_PYTHON',str(R/'venv/bin/python'))
lock=threading.RLock();wake=threading.Event();closing=threading.Event();active=None
registry=Registry(os.environ.get('MEDIA_PROFILES_FILE',str(ROOT.parent/'config/profiles.json')))
MODELS=registry.profiles
QUEUE_POLL_S=float(os.environ.get('MEDIA_QUEUE_POLL_S','3'));QUEUE_WAIT_TIMEOUT_S=float(os.environ.get('MEDIA_QUEUE_WAIT_TIMEOUT_S','3600'));JOB_TIMEOUT_S=float(os.environ.get('MEDIA_JOB_TIMEOUT_S','3600'))
TEST_PROBE_SUFFIXES=('/performance_probe.py','/h3_specs_suite.py','/image_compare_suite.py','/stability_remaining.py')
def job_config(d):return d.get('execution',{}).get('profile') or MODELS[d['parameters']['model']]
def waiting_detail(cfg,avail):return {'stage':'waiting_memory','note':f'可用{avail/2**30:.1f}GiB<需{cfg["available_gib_required"]}GiB，排队等待内存','required_gib':cfg['available_gib_required'],'available_gib':round(avail/2**30,2)}
def test_process_running():
 for x in psutil.process_iter(['cmdline']):
  try:
   if any(a.endswith(TEST_PROBE_SUFFIXES) for a in (x.info['cmdline'] or [])):return True
  except (psutil.NoSuchProcess,psutil.AccessDenied):pass
 return False
def is_lan(ip):
 if not ip or ip in('::1','localhost'):return True
 if ip.startswith(('192.168.','10.','127.')):return True
 m=re.match(r'^172\.(\d+)\.',ip)
 return bool(m and 16<=int(m.group(1))<=31)
def authorize(request:Request,authorization:str|None=Header(default=None)):
 if not os.environ.get('MEDIA_API_KEY') and is_lan(request.client.host if request.client else ''):return # 局域网免 key（用户约定 2026-11-03）；非 LAN 源仍需 Bearer
 key=os.environ.get('MEDIA_API_KEY','')
 if not key and (R/'omlx-data/settings.json').is_file():key=json.loads((R/'omlx-data/settings.json').read_text()).get('auth',{}).get('api_key','')
 given=authorization[7:] if authorization and authorization.startswith('Bearer ') else ''
 if not key or not given or not secrets.compare_digest(key,given):raise HTTPException(401,'需要有效 Bearer API 密钥',headers={'WWW-Authenticate':'Bearer'})
def folder(id):
 if not re.fullmatch('[a-f0-9]{32}',id):raise HTTPException(404,'任务不存在')
 p=J/id
 if not (p/'job.json').exists():raise HTTPException(404,'任务不存在')
 return p
def read(p):return json.loads((p/'job.json').read_text())
def write(p,d):
 tmp=p/'job.tmp';tmp.write_text(json.dumps(d,ensure_ascii=False,indent=2));tmp.replace(p/'job.json')
ETA_BASE={'zimage':12,'moss':90,'moss-edit':420,'h3':90,'h3-image':240,'vpipe-2k':110}
def eta_for(d):
 p=d['parameters'];now=time.time();hist=[]
 for q in J.iterdir():
  rj=q/'result.json'
  if not rj.exists():continue
  try:r=json.loads(rj.read_text())
  except Exception:continue
  hp=r.get('parameters') or {}
  if hp.get('model')!=p['model'] or hp.get('kind')!=p['kind']:continue
  t=r.get('total_seconds')
  if not t or not hp.get('width') or not hp.get('height'):continue
  scale=(p['width']*p['height'])/(hp['width']*hp['height'])
  if p['kind']=='video':scale*=max(1,p.get('seconds',5))/max(1,hp.get('seconds',5))
  hist.append(t*scale)
  if len(hist)>=5:break
 est=int(sum(sorted(hist))/len(hist)) if hist else ETA_BASE.get(p['model'],60)
 rem=max(0.0,est-(now-d.get('started_at',now)-d.get('execution_paused_seconds',0)))
 return {'state':'running','est_total_seconds':est,'remaining_seconds':round(rem,1),'history_samples':len(hist)}
def qorder_key(d):return (d.get('queue_rank',1e15),-d.get('bump_at',0),d['created_at'],d['id'])
def queue_wait(d,now=None):
 now=time.time() if now is None else now
 since=d.get('queued_since',d['created_at'])
 return d.get('queue_wait_accumulated_seconds',0)+(max(0,now-since) if since is not None and d['status']=='queued' else 0)
def freeze_wait(d):
 d['queue_wait_accumulated_seconds']=queue_wait(d);d['queued_since']=None

def task_controls(d):
 live=d['status'] in ('queued','running','paused')
 can_suspend=(d.get('execution') or {}).get('engine',{}).get('supports_process_pause',False)
 return {'pause':d['status']=='queued' or (d['status']=='running' and can_suspend and not d.get('cancel_requested')),'resume':d['status']=='paused' or bool(d.get('pause_requested')),'cancel':live,'reorder':d['status']=='queued','process_pause_supported':can_suspend,'pause_note':'运行中挂起保留进程与内存，不让出执行槽；重启后无法续跑' if can_suspend else '此引擎未验证运行中挂起；可暂停排队任务或后续调度'}

def view(p,qpos=None,d=None):
 d=d if d is not None else read(p);d['queue_position']=None;d['bumped']=bool(d.get('bump_at') and d['status'] in ('queued','running'))
 if d['status']=='queued':
  if qpos is not None:d['queue_position']=qpos.get(d['id'])
  else:
   rows=[read(q) for q in J.iterdir() if (q/'job.json').exists() and read(q)['status']=='queued']
   rows.sort(key=qorder_key)
   for i,r in enumerate(rows):
    if r['id']==d['id']:d['queue_position']=i+1;break
 d['controls']=task_controls(d)
 d['queue_wait_seconds']=round(queue_wait(d),3)
 d['has_input_image']=(p/'input-image.png').exists()
 d['edit_image_count']=sum(1 for f in p.iterdir() if f.name.startswith('edit-image'))
 if d['status']=='paused':d['eta']={'state':'task_paused'}
 elif d['status']=='running':d['eta']=eta_for(d)
 elif d['status']=='queued' and queue_paused:d['eta']={'state':'queue_paused'}
 elif d['status']=='queued' and not EXECUTION_ENABLED:
  d['eta']={'state':'execution_paused'}
 elif d['status']=='queued':
  try:avail=psutil.virtual_memory().available
  except Exception:avail=0
  d['eta']={'state':'starting'} if avail>=job_config(d)['available_gib_required']*2**30 else {'state':'waiting_memory'}
 if (p/'stage.json').exists():
  try:
   st=json.loads((p/'stage.json').read_text())
   d['stage']=st.get('stage','')
   d['stage_info']=st
  except Exception:pass
 if d['status']=='paused':
  d['stage_info']=dict(d.get('stage_info') or {},previous_stage=d.get('stage'),stage='paused');d['stage']='paused'
 if d['status']=='succeeded':d['output_url']=f'/api/jobs/{d["id"]}/output';d['result_url']=f'/api/jobs/{d["id"]}/result'
 return d
def submission_fingerprint(payload):
 return hashlib.sha256(json.dumps(payload,sort_keys=True,separators=(',',':'),ensure_ascii=False).encode()).hexdigest()

def accepted_jobs():
 return [read(p) for p in J.iterdir() if (p/'job.json').exists()]

def replay_submission(key,fingerprint):
 if not key:return None
 for job in accepted_jobs():
  if job.get('idempotency_key')==key:
   if job.get('request_fingerprint')!=fingerprint:raise HTTPException(409,'幂等键已用于不同请求，请为新操作使用新键')
   return view(J/job['id'],d=job)
 return None

class JobRequest(BaseModel):
 model_config={"extra":"forbid"}
 derived_from:str|None=Field(default=None,pattern=r'^[a-f0-9]{32}$')
 metadata:dict[str,str]=Field(default_factory=dict,max_length=32)
 options:dict=Field(default_factory=dict)
 last_frame_base64:str|None=Field(default=None,max_length=6000000)
 ref_videos_base64:list[str]|None=Field(default=None,max_length=3)
 ref_audios_base64:list[str]|None=Field(default=None,max_length=3)

 kind:Literal['image','video','audio']='image';model:str='zimage';prompt:str=Field(min_length=1,max_length=7000);width:int=Field(default=1024,ge=256,le=2560);height:int=Field(default=1024,ge=256,le=2560);steps:int|None=Field(default=None,ge=1,le=60);seed:int=Field(default=42,ge=0,le=2147483647);guidance:float|None=Field(default=None,ge=0,le=10);seconds:int=Field(default=5,ge=1,le=15);precision:Literal['default-mixed-int8','row-int8-fc2','bf16-projections']='default-mixed-int8';layers:Literal[45,50]=45;reuse:Literal[1,2]=2;first_frame_base64:str|None=Field(default=None,max_length=6000000);input_image_base64:str|None=Field(default=None,max_length=6000000);edit_image_base64:str|None=Field(default=None,max_length=6000000);edit_images_base64:list[str]|None=Field(default=None,max_length=16);ref_images_base64:list[str]|None=Field(default=None,max_length=12);ref_audio_base64:str|None=Field(default=None,max_length=8000000);image_strength:float|None=Field(default=None,ge=0.1,le=1.0);draft_base64:str|None=Field(default=None,max_length=45000000);frames:int|None=Field(default=None,ge=2,le=48);lora:Literal['open_h3_2k','pika_flex_ref2va','pika_flex_fl2va','turbo_fl2va_lite']|None=Field(default=None);lora_scale:float|None=Field(default=None,ge=0.0,le=2.0)
 @model_validator(mode='after')
 def validate_model(self):
  if self.model not in MODELS:raise ValueError('未知模型配置，请查询 /api/models')
  cfg=MODELS[self.model]
  registry.validate_options(self.model,self.options)
  for role,field in protocol.ROLE_FIELDS.items():
   if getattr(self,field) and role not in cfg['input_roles']:raise ValueError('该引擎不支持素材角色: '+role)
  if registry.engines[cfg['engine']]['type']!='builtin':
   if self.kind!=cfg['kind']:raise ValueError('kind与model不匹配')
   if self.steps is None:self.steps=cfg['steps']
   if self.guidance is None:self.guidance=cfg['guidance']
   return self
  backend=cfg['backend']
  if (self.ref_videos_base64 or self.ref_audios_base64) and backend!='h3':raise ValueError('参考音视频仅用于 h3')
  if self.last_frame_base64 and backend not in ('h3','vpipe-h3'):raise ValueError('尾帧仅用于 h3 / vpipe-h3 视频')
  if self.ref_audio_base64 and self.ref_audios_base64:raise ValueError('单音频与多音频不能同时提供')
  if sum(len(x or []) for x in (self.ref_images_base64,self.ref_videos_base64,self.ref_audios_base64))+bool(self.first_frame_base64)+bool(self.last_frame_base64)+bool(self.ref_audio_base64)>12:raise ValueError('参考素材总数最多12个')
  if self.kind!=cfg['kind']:raise ValueError('kind与model不匹配')
  if self.width%32 or self.height%32:raise ValueError('宽高必须32对齐')
  if backend=='vpipe-2k':
   if not self.draft_base64:raise ValueError('vpipe-2k 需要 draft_base64（低清底片视频，base64）')
   if self.width*self.height>3686400 or max(self.width,self.height)>2560:raise ValueError('vpipe-2k 目标画布最多 2560×1440（3686400像素）')
   if self.frames is None:self.frames=22
   if self.first_frame_base64 or self.ref_images_base64 or self.ref_audio_base64 or self.input_image_base64:raise ValueError('vpipe-2k 只接受 draft_base64，不接受首帧/参考图/参考音频/输入图')
   expected_steps=6 if self.lora=='pika_flex_ref2va' else 3
   if self.steps is not None and self.steps!=expected_steps:raise ValueError(f'当前 vpipe 配置固定使用 {expected_steps} 步；请修改 steps 或选择匹配的 LoRA')
   self.steps=expected_steps
  elif backend=='vpipe-h3':
   if self.draft_base64 or self.frames:raise ValueError('draft_base64/frames 仅用于 vpipe-2k')
   if self.lora not in (None,'pika_flex_fl2va','turbo_fl2va_lite'):raise ValueError('vpipe-h3 的 lora 仅支持 pika_flex_fl2va / turbo_fl2va_lite')
   if self.lora_scale is not None and not self.lora:raise ValueError('lora_scale 需要同时指定 lora')
   if self.ref_images_base64 or self.ref_audio_base64:raise ValueError('vpipe-h3 条件输入目前只支持首帧/尾帧（Ref2VA 请用 h3）')
   if self.seconds>10:raise ValueError('vpipe-h3 单次生成最多 10 秒')
   if max(self.width,self.height)>1536:raise ValueError('该模型宽高最多1536')
   if self.width*self.height>1032192:raise ValueError('超过服务画布上限；视频最多1032192像素')
   if self.lora is None:self.lora='pika_flex_fl2va' if (self.first_frame_base64 or self.last_frame_base64) else None
   expected_vpipe={'pika_flex_fl2va':6,'turbo_fl2va_lite':4}.get(self.lora,16)
   if self.steps is not None and self.steps!=expected_vpipe:raise ValueError(f'当前 vpipe-h3 配置固定使用 {expected_vpipe} 步；请修改 steps 或选择匹配的 LoRA')
   self.steps=expected_vpipe
  else:
   if self.draft_base64 or self.frames:raise ValueError('draft_base64/frames 仅用于 vpipe-2k')
   if self.lora or self.lora_scale is not None:raise ValueError('lora/lora_scale 仅用于 vpipe-2k')
   if max(self.width,self.height)>1536:raise ValueError('该模型宽高最多1536')
   if self.width*self.height>(1032192 if self.kind=='video' else 1572864):raise ValueError('超过服务画布上限；视频最多1032192像素，图像最多1572864像素')
  if backend=='h3-image' and self.steps not in (None,6):raise ValueError('当前 h3-image 引擎固定 6 步')
  if self.steps is None:self.steps=cfg['steps']
  if self.guidance is None:self.guidance=cfg['guidance']
  if self.first_frame_base64 and self.kind!='video':raise ValueError('首帧仅用于h3视频')
  if self.ref_images_base64:
   if self.kind!='video':raise ValueError('参考图仅用于h3视频（Ref2VA）')
   if self.first_frame_base64:raise ValueError('参考图（Ref2VA）与首帧不能同时使用（h3引擎限制）')
   if not all(self.ref_images_base64):raise ValueError('参考图列表含空项')
  if self.input_image_base64:
   if self.kind!='image':raise ValueError('输入图仅用于图像任务')
   if backend not in('zimage','moss-edit'):raise ValueError('仅 zimage / MOSS Edit 支持图生图')
  if self.ref_audio_base64 and self.kind!='video':raise ValueError('参考音频仅用于 h3 视频（Ref2VA a2v 口型）')
  if backend=='moss-edit' and not self.input_image_base64:raise ValueError('MOSS Edit 需要输入图（编辑源图）')
  if (self.edit_image_base64 or self.edit_images_base64) and backend!='h3-image':raise ValueError('edit_image（参考图编辑）仅 h3-image 支持')
  return self

def worker():
 global active
 while not closing.is_set():
  p=None;d=None
  with lock:
   now=time.time()
   queued=sorted((q for q in J.iterdir() if (q/'job.json').exists() and read(q)['status']=='queued'),key=lambda q:qorder_key(read(q)))
   if queue_paused:queued=[]
   keep=[]
   for q in queued:
    dd=read(q)
    if QUEUE_WAIT_TIMEOUT_S and queue_wait(dd,now)>QUEUE_WAIT_TIMEOUT_S:
     freeze_wait(dd);dd.update(status='failed',finished_at=now,elapsed_seconds=now-dd['created_at'],error=f'等待可用内存超过{QUEUE_WAIT_TIMEOUT_S:.0f}秒仍不满足（需{job_config(dd)["available_gib_required"]}GiB）；请先在oMLX卸载LLM或释放内存后重新提交')
     write(q,dd)
    else:keep.append(q)
   queued=keep
   if queued:
    avail=psutil.virtual_memory().available;head=queued[0];test_block=test_process_running()
    for q in queued:
     dd=read(q)
     if avail<job_config(dd)['available_gib_required']*2**30:continue
     if test_block:break
     p=q;d=dd;break
    if p is None:
     hd=read(head);hc=job_config(hd)
     detail=waiting_detail(hc,avail) if avail<hc['available_gib_required']*2**30 else {'stage':'waiting_no_test_process','note':'检测到测试推理进程，等待其结束后再启动'}
     sf=head/'stage.json';cur=None
     if sf.exists():
      try:cur=json.loads(sf.read_text())
      except Exception:pass
     if cur!=detail:sf.write_text(json.dumps(detail,ensure_ascii=False))
  if p is None:
   wake.wait(QUEUE_POLL_S);wake.clear();continue
  with lock:
   cur=read(p)
   if cur['status']!='queued' or queue_paused:continue
   (p/'stage.json').unlink(missing_ok=True)
   d=cur;freeze_wait(d);d.update(status='running',started_at=time.time(),stage='preflight');write(p,d)
  proc=None;error=None;rssmax=None;swap0=psutil.swap_memory().used;start=time.monotonic();lowmem=0;paused_start=None;paused_total=0
  try:
   cfg=job_config(d)
   if not (p/'task.json').exists():protocol.persist(p,d)
   with (p/'run.log').open('w') as log:
    proc=subprocess.Popen(['/usr/bin/caffeinate','-is',d.get('execution',{}).get('runner_python',RUNNER_PYTHON),str(ROOT/'runner.py'),str(p)],stdout=log,stderr=log,env=dict(os.environ,HF_HUB_OFFLINE='1',TRANSFORMERS_OFFLINE='1',TOKENIZERS_PARALLELISM='false',PYTHONUNBUFFERED='1'),start_new_session=True)
    with lock:
     active=(d['id'],proc)
     cur=read(p);cur['pid']=proc.pid;write(p,cur)
    while proc.poll() is None:
     with lock:
      current=read(p);cancel=current.get('cancel_requested',False)
      if not cancel and not closing.is_set():
       if current.get('pause_requested') and paused_start is None:
        try:os.killpg(proc.pid,signal.SIGSTOP)
        except ProcessLookupError:continue
        paused_start=time.monotonic()
        current.update(status='paused',paused_from='running',paused_at=time.time());write(p,current)
       elif not current.get('pause_requested') and paused_start is not None:
        try:os.killpg(proc.pid,signal.SIGCONT)
        except ProcessLookupError:continue
        paused_total+=time.monotonic()-paused_start;paused_start=None
        current.update(status='running',execution_paused_seconds=paused_total);current.pop('paused_at',None);current.pop('paused_from',None);write(p,current)
     if cancel or closing.is_set():error='cancelled';break
     if JOB_TIMEOUT_S and time.monotonic()-start-paused_total-(time.monotonic()-paused_start if paused_start is not None else 0)>JOB_TIMEOUT_S:error=f'任务超过{JOB_TIMEOUT_S/60:.0f}分钟上限';break
     vm=psutil.virtual_memory();sw=(psutil.swap_memory().used-swap0)/2**30
     # 内存保护使用持续时间，控制请求唤醒 worker 不应加速低内存判定。
     if vm.available<2*2**30:
      if not lowmem:lowmem=time.monotonic()
      if time.monotonic()-lowmem>=1:error=f'触发内存保护：可用内存连续低于2GiB（现{vm.available/2**30:.1f}GiB，swap增长{sw:.1f}GiB）';break
     else:lowmem=0
     if sw>8:error=f'触发内存保护：swap增长{sw:.1f}GiB超过8GiB';break
     try:
      parent=psutil.Process(proc.pid);children=[parent]+parent.children(recursive=True);rssmax=max(rssmax or 0,sum(c.memory_info().rss for c in children if c.is_running()))
      with lock:
       live=read(p);live['process_tree_rss_peak_bytes']=rssmax;live['memory_note']='1s process-tree RSS sum, not attributable Metal total; image result reports separate MLX peak';write(p,live)
     except (psutil.NoSuchProcess,psutil.AccessDenied):pass
     wake.wait(1);wake.clear()
    if error:
     if paused_start is not None:
      try:os.killpg(proc.pid,signal.SIGCONT)
      except ProcessLookupError:pass
     try:os.killpg(proc.pid,signal.SIGTERM)
     except ProcessLookupError:pass
     try:proc.wait(timeout=10)
     except subprocess.TimeoutExpired:
      try:os.killpg(proc.pid,signal.SIGKILL)
      except ProcessLookupError:pass
      proc.wait()
    elif proc.returncode!=0:error='生成进程失败；查看任务日志'
   if not error:checked_output(p)
  except Exception as e:error=str(e)
  if proc is not None and proc.poll() is None:
   try:
    if paused_start is not None:os.killpg(proc.pid,signal.SIGCONT)
    os.killpg(proc.pid,signal.SIGTERM)
    try:proc.wait(timeout=10)
    except subprocess.TimeoutExpired:os.killpg(proc.pid,signal.SIGKILL);proc.wait()
   except ProcessLookupError:pass
   error=error or '执行器提前结束，已停止子进程'
  if paused_start is not None:paused_total+=time.monotonic()-paused_start
  with lock:
   current=read(p);current.pop('paused_at',None);current.pop('paused_from',None);current.pop('pause_requested',None);current['execution_paused_seconds']=paused_total;current.update(status='cancelled' if error=='cancelled' or current.get('cancel_requested') else 'failed' if error else 'succeeded',finished_at=time.time(),elapsed_seconds=time.monotonic()-start,process_tree_rss_peak_bytes=rssmax,memory_note='1s process-tree RSS sum, not attributable Metal total; image result reports separate MLX peak')
   if error:current['error']=error
   write(p,current);active=None
dsh_callbacks=Callbacks(J,folder,protocol.task_view,lock)
@asynccontextmanager
async def lifespan(app):
 if sys.platform!="darwin":raise RuntimeError("首期仅支持 macOS；Windows/Linux 尚不支持")
 guard=(J/'.service.lock').open('a+')
 try:
  fcntl.flock(guard.fileno(),fcntl.LOCK_EX|fcntl.LOCK_NB)
 except BlockingIOError:
  guard.close();raise RuntimeError('A service already owns this task directory')
 t=None; downloader=None; callback_worker=None
 try:
  closing.clear();wake.clear()
  for p in J.iterdir():
   if (p/'job.json').exists():
    d=read(p)
    if d['status']=='queued' and not (p/'execution.json').exists():
     snapshot=registry.snapshot(d['parameters']['model'],R);(p/'execution.json').write_text(json.dumps(snapshot));d['execution']=snapshot;write(p,d)
    if d['status']=='running' or (d['status']=='paused' and d.get('paused_from')=='running'):d.pop('paused_at',None);d.pop('pause_requested',None);d.update(status='interrupted',error='服务重启中断；请先对账再重试');write(p,d)
  if EXECUTION_ENABLED:
   t=threading.Thread(target=worker,daemon=True);t.start()
  if os.environ.get('MEDIA_DOWNLOADS_ENABLED',str(int(SERVICE_ID=='local.model-gateway')))=='1':
   downloader=download_engine.start()
  callback_worker=threading.Thread(target=dsh_callbacks.run,args=(closing,),daemon=True)
  callback_worker.start()
  yield
 finally:
  if downloader:download_engine.stop(downloader)
  closing.set();wake.set()
  if t:t.join(timeout=15)
  if callback_worker:callback_worker.join(timeout=21)
  fcntl.flock(guard.fileno(),fcntl.LOCK_UN);guard.close()
app=FastAPI(
 title='Local Model Station',version='0.1.0',lifespan=lifespan,
 description='任务与模型 API。批次完成后，提交设备可使用 [DSH / Codex 等待器](/api/help#batch-completion) 恢复会话。',
)
app.include_router(download_router([Depends(authorize)]))
app.include_router(dsh_callbacks.router(authorize))
app.mount('/assets/tabulator-6.6.1',StaticFiles(directory=ROOT/'assets/tabulator'),name='tabulator')
app.mount('/assets/bootstrap-5.3.9',StaticFiles(directory=ROOT/'assets/bootstrap'),name='bootstrap')
NO_STORE={'Cache-Control':'no-store'}
@app.get('/',response_class=HTMLResponse)
def home():return HTMLResponse(content=(ROOT/'studio.html').read_text(),headers=NO_STORE)
@app.get('/studio.js')
def studio_js():return Response(content=(ROOT/'studio.js').read_text(),media_type='text/javascript',headers=NO_STORE)
@app.get('/studio.css')
def studio_css():return Response(content=(ROOT/'studio.css').read_text(),media_type='text/css',headers=NO_STORE)
@app.get('/manage',response_class=HTMLResponse)
def manage_page():return HTMLResponse(content=(ROOT/'studio.html').read_text(),headers=NO_STORE)
@app.get('/api/help',response_class=HTMLResponse)
def help_page():return HTMLResponse(content=(ROOT/'help.html').read_text(),headers=NO_STORE)
@app.get('/api/help.md',response_class=PlainTextResponse)
def help_md():return (ROOT.parent/'docs/API.md').read_text()
@app.get('/api/v1/clients/batch_wait.py',tags=['Client tools'],response_class=FileResponse,
         summary='下载 DSH / Codex 批次等待脚本')
def batch_wait_client():
 """下载仅依赖 Python 标准库的客户端。使用方式见 /api/help。"""
 return FileResponse(ROOT/'batch_wait.py',media_type='text/x-python',
                     filename='batch_wait.py',headers=NO_STORE)
@app.get('/health')
def health():return {'status':'ok','version':'0.1.0','platform':sys.platform,'service_id':SERVICE_ID,'execution_enabled':EXECUTION_ENABLED,'queue_paused':queue_paused,'task_store':'isolated','active_job':active[0] if active else None,'queue_concurrency':1,'offline':True,'offline_scope':'builtin HF environment flags; custom scripts are trusted operator code','memory_percent':psutil.virtual_memory().percent,'swap_used_gib':round(psutil.swap_memory().used/2**30,1),'total_gib':round(psutil.virtual_memory().total/2**30,1),'available_gib':round(psutil.virtual_memory().available/2**30,1),'queue_wait_timeout_s':QUEUE_WAIT_TIMEOUT_S}
@app.get('/api/v1/engines/splash',tags=['Engine status'],dependencies=[Depends(authorize)])
def splash_info():return JSONResponse(splash_status.snapshot(R),headers=NO_STORE)
@app.get('/api/models',dependencies=[Depends(authorize)])
def models():return {'data':[dict(id=k,**v,available=registry.probe(k,R)['available']) for k,v in MODELS.items()]}
@app.post('/api/jobs',status_code=202,dependencies=[Depends(authorize)])
def submit(body:JobRequest,request:Request,idempotency_key:str|None=Header(default=None,min_length=1,max_length=160)):
 fingerprint=submission_fingerprint({'operation':'submit','body':body.model_dump()})
 with lock:
  replay=replay_submission(idempotency_key,fingerprint)
  if replay is not None:return replay
 cfg=MODELS[body.model]
 if not registry.probe(body.model,R)['available']:raise HTTPException(409,registry.probe(body.model,R))
 params=body.model_dump();params.pop('metadata',None);params.pop('derived_from',None);last=params.pop('last_frame_base64');videos=params.pop('ref_videos_base64');audios=params.pop('ref_audios_base64');extra={}
 for name,encoded_file,limit in ([('last-frame.png',last,4)] if last else [])+[(f'ref-video-{i:02d}.mp4',v,32) for i,v in enumerate(videos or [])]+[(f'ref-audio-{i:02d}.wav',v,6) for i,v in enumerate(audios or [])]:
  try:
   raw=base64.b64decode(encoded_file.split(',',1)[-1],validate=True)
   if not raw or len(raw)>limit*1024**2:raise ValueError('素材为空或超过限制')
   if name.endswith('.png'):
    im=Image.open(io.BytesIO(raw));im.verify();im=Image.open(io.BytesIO(raw))
    if im.width*im.height>16000000:raise ValueError('图片过大')
    buf=io.BytesIO();im.convert('RGB').save(buf,format='PNG');raw=buf.getvalue()
   if name.endswith('.wav') and raw[:4]!=b'RIFF':raise ValueError('参考音频须为 WAV')
   if name.endswith('.mp4') and b'ftyp' not in raw[:64]:raise ValueError('参考视频须为 MP4')
   extra[name]=raw
  except Exception as exc:raise HTTPException(422,f'{name}: {exc}')
 encoded=params.pop('first_frame_base64');first=None
 if encoded:
  try:
   raw=base64.b64decode(encoded.split(',',1)[-1],validate=True)
   if len(raw)>4*1024**2:raise ValueError('图像超过4MiB')
   first=Image.open(io.BytesIO(raw));first.verify();first=Image.open(io.BytesIO(raw));
   if first.width*first.height>16000000:raise ValueError('图像超过1600万像素')
   first=first.convert('RGB')
  except Exception:raise HTTPException(422,'首帧必须是有效PNG/JPEG/WebP，最多4MiB与1600万像素')
 ienc=params.pop('input_image_base64');input_img=None
 if ienc:
  try:
   raw=base64.b64decode(ienc.split(',',1)[-1],validate=True)
   if len(raw)>4*1024**2:raise ValueError('图像超过4MiB')
   input_img=Image.open(io.BytesIO(raw));input_img.verify();input_img=Image.open(io.BytesIO(raw));
   if input_img.width*input_img.height>16000000:raise ValueError('图像超过1600万像素')
   input_img=input_img.convert('RGB')
  except Exception:raise HTTPException(422,'输入图必须是有效PNG/JPEG/WebP，最多4MiB与1600万像素')
 editencs=list(params.pop('edit_images_base64',None) or [])
 single=params.pop('edit_image_base64',None)
 if single:editencs=[single]+editencs
 if len(editencs)>16:raise HTTPException(422,'编辑参考图最多16张')
 edit_imgs=[]
 for k,editenc in enumerate(editencs):
  try:
   raw=base64.b64decode(editenc.split(',',1)[-1],validate=True)
   if len(raw)>4*1024**2:raise ValueError('图像超过4MiB')
   edit_img=Image.open(io.BytesIO(raw));edit_img.verify();edit_img=Image.open(io.BytesIO(raw));
   if edit_img.width*edit_img.height>16000000:raise ValueError('图像超过1600万像素')
   edit_imgs.append(edit_img.convert('RGB'))
  except Exception:raise HTTPException(422,f'编辑参考图{k+1}必须是有效PNG/JPEG/WebP，最多4MiB与1600万像素')
 if edit_imgs:params['edit_image_count']=len(edit_imgs)
 refs=params.pop('ref_images_base64',None);ref_imgs=[]
 if refs:
  for k,enc2 in enumerate(refs):
   try:
    raw=base64.b64decode(enc2.split(',',1)[-1],validate=True)
    if len(raw)>4*1024**2:raise ValueError('图像超过4MiB')
    im=Image.open(io.BytesIO(raw));im.verify();im=Image.open(io.BytesIO(raw))
    if im.width*im.height>16000000:raise ValueError('图像超过1600万像素')
    ref_imgs.append(im.convert('RGB'))
   except Exception:raise HTTPException(422,f'参考图{k+1}必须是有效PNG/JPEG/WebP，最多4MiB与1600万像素')
  params['ref_image_count']=len(ref_imgs)
 ra=params.pop('ref_audio_base64',None);ref_a=None
 if ra:
  try:
   raw=base64.b64decode(ra.split(',',1)[-1],validate=True)
   if len(raw)>6*1024**2:raise ValueError('参考音频超过6MiB')
   if raw[:4]!=b'RIFF':raise ValueError('参考音频须为 WAV 格式')
   ref_a=raw
  except HTTPException:raise
  except Exception as e:raise HTTPException(422,f'参考音频无效：{e}')
 denc=params.pop('draft_base64',None);draft=None
 if denc:
  try:
   raw=base64.b64decode(denc.split(',',1)[-1],validate=True)
   if len(raw)>32*1024**2:raise ValueError('底片视频超过32MiB')
   draft=raw
  except Exception as e:raise HTTPException(422,f'底片视频无效：{e}')
 with lock:
  replay=replay_submission(idempotency_key,fingerprint)
  if replay is not None:return replay
  execution=registry.snapshot(body.model,R)
  if sum(read(p)['status'] in ['queued','running','paused'] for p in J.iterdir() if (p/'job.json').exists())>=int(os.environ.get('MEDIA_MAX_PENDING','64')):raise HTTPException(429,'最多%s个未完成任务'%os.environ.get('MEDIA_MAX_PENDING','64'))
  id=uuid.uuid4().hex;p=J/id;p.mkdir();(p/'input.json').write_text(json.dumps(params,ensure_ascii=False));(p/'execution.json').write_text(json.dumps(execution,ensure_ascii=False))
  for name,raw in extra.items():(p/name).write_bytes(raw)
  if first:first.save(p/'first-frame.png')
  if input_img:input_img.save(p/'input-image.png')
  for k,im in enumerate(edit_imgs):im.save(p/f'edit-image-{k:02d}.png')
  for k,im in enumerate(ref_imgs):im.save(p/f'ref-image-{k:02d}.png')
  if ref_a:(p/'ref-audio.wav').write_bytes(ref_a)
  if draft is not None:(p/'draft.mp4').write_bytes(draft)
  d={'id':id,'status':'queued','created_at':time.time(),'queued_since':None if queue_paused else time.time(),'parameters':params,'metadata':body.metadata,'derived_from':body.derived_from,'execution':execution,'idempotency_key':idempotency_key,'request_fingerprint':fingerprint,'client':{'ip':request.client.host if request.client else None,'user_agent':request.headers.get('user-agent','')}};protocol.persist(p,d);write(p,d);wake.set();return view(p)
def reorder_job(id,where):
 with lock:
  p=folder(id);d=read(p)
  if d['status']!='queued':raise HTTPException(409,'仅排队任务可以调整顺序')
  rows=sorted((row for row in accepted_jobs() if row['status']=='queued'),key=qorder_key)
  old=next(i for i,row in enumerate(rows) if row['id']==id)
  row=rows.pop(old)
  index=0 if where=='bump' else len(rows) if where=='defer' else max(0,old-1) if where=='up' else min(len(rows),old+1)
  rows.insert(index,row)
  for i,row in enumerate(rows):
   row['queue_rank']=i
   if row['id']==id:
    if where=='bump':row['bump_at']=time.time()
    else:row.pop('bump_at',None)
   write(J/row['id'],row)
  wake.set();return view(p)

@app.post('/api/jobs/{id}/bump',dependencies=[Depends(authorize)])
def bump(id:str):return reorder_job(id,'bump')

@app.post('/api/jobs/{id}/defer',dependencies=[Depends(authorize)])
def defer_job(id:str):return reorder_job(id,'defer')

@app.post('/api/jobs/{id}/up',dependencies=[Depends(authorize)])
def move_up(id:str):return reorder_job(id,'up')

@app.post('/api/jobs/{id}/down',dependencies=[Depends(authorize)])
def move_down(id:str):return reorder_job(id,'down')

@app.post('/api/jobs/{id}/pause',dependencies=[Depends(authorize)])
def pause_job(id:str):
 with lock:
  p=folder(id);d=read(p)
  if d['status']=='paused' or d.get('pause_requested'):return view(p)
  if d.get('cancel_requested'):raise HTTPException(409,'任务正在取消')
  if d['status']=='queued':
   freeze_wait(d);d.update(status='paused',paused_from='queued',paused_at=time.time())
  elif d['status']=='running':
   if not task_controls(d)['process_pause_supported']:raise HTTPException(409,'此引擎未验证运行中挂起；请暂停后续队列，或取消后编辑重试')
   d['pause_requested']=True
  else:raise HTTPException(409,'该状态不能暂停')
  write(p,d);wake.set();return view(p)

@app.post('/api/jobs/{id}/resume',dependencies=[Depends(authorize)])
def resume_job(id:str):
 with lock:
  p=folder(id);d=read(p)
  if d['status']=='running' and d.get('pause_requested'):
   d['pause_requested']=False
  elif d['status']=='paused':
   if d.get('paused_from')=='running':d['pause_requested']=False
   else:
    d.update(status='queued',queued_since=None if queue_paused else time.time());d.pop('paused_at',None);d.pop('paused_from',None)
  elif d['status'] in ('queued','running'):return view(p)
  else:raise HTTPException(409,'该状态不能恢复，请编辑重试')
  write(p,d);wake.set();return view(p)

class QueueAction(BaseModel):
 action:Literal['pause','resume']
 model_config={'extra':'forbid'}

@app.get('/api/queue',dependencies=[Depends(authorize)])
def queue_status():
 with lock:
  rows=accepted_jobs()
  return {'paused':queue_paused,'execution_enabled':EXECUTION_ENABLED,'active_job':active[0] if active else None,'counts':{state:sum(r['status']==state for r in rows) for state in ('queued','running','paused')},'order':[r['id'] for r in sorted((r for r in rows if r['status']=='queued'),key=qorder_key)]}

@app.post('/api/queue/control',dependencies=[Depends(authorize)])
def queue_control(body:QueueAction):
 global queue_paused
 with lock:
  paused=body.action=='pause'
  if paused!=queue_paused:
   for row in accepted_jobs():
    if row['status']=='queued':
     if paused:freeze_wait(row)
     else:row['queued_since']=time.time()
     write(J/row['id'],row)
   queue_paused=paused
   tmp=QUEUE_STATE.with_suffix('.tmp');tmp.write_text(json.dumps({'paused':paused,'updated_at':time.time()}));tmp.replace(QUEUE_STATE)
  wake.set();return queue_status()

class BatchAction(BaseModel):
 action:Literal['pause','resume','cancel','bump','defer']
 ids:list[str]=Field(min_length=1,max_length=100)
 model_config={'extra':'forbid'}

@app.post('/api/queue/batch',dependencies=[Depends(authorize)])
def queue_batch(body:BatchAction):
 handlers={'pause':pause_job,'resume':resume_job,'cancel':cancel,'bump':bump,'defer':defer_job}
 results=[]
 # Reversed front insertion preserves the user's relative selection order.
 ids=list(dict.fromkeys(body.ids));ids=ids[::-1] if body.action=='bump' else ids
 with lock:
  for id in ids:
   try:job=handlers[body.action](id);results.append({'id':id,'ok':True,'status':job['status']})
   except HTTPException as exc:results.append({'id':id,'ok':False,'status_code':exc.status_code,'error':exc.detail})
 return {'results':results,'queue':queue_status()}

@app.post('/api/jobs/{id}/retry',status_code=202,dependencies=[Depends(authorize)])
def retry(id:str,request:Request,idempotency_key:str|None=Header(default=None,min_length=1,max_length=160)):
 src=folder(id)
 fingerprint=submission_fingerprint({'operation':'retry','source':id})
 with lock:
  replay=replay_submission(idempotency_key,fingerprint)
  if replay is not None:return replay
  d=read(src)
  if (d.get('idempotency_key') or '').startswith('candidate:'):raise HTTPException(409,'候选任务请使用候选组补齐，不能绕过组预算单独重试')
  if d['status'] not in ('failed','cancelled','interrupted'):raise HTTPException(409,'仅失败/已取消/中断的任务可重试')
  mname=(d.get('parameters') or {}).get('model')
  if mname not in MODELS and not d.get('execution'):raise HTTPException(409,'该任务的模型参数不完整，无法重试')

  execution=d.get('execution') or registry.snapshot(mname,R)
  params=dict(d['parameters']);params.pop('first_frame_base64',None);params.pop('input_image_base64',None);params.pop('ref_images_base64',None)
  if sum(read(x)['status'] in ['queued','running','paused'] for x in J.iterdir() if (x/'job.json').exists())>=int(os.environ.get('MEDIA_MAX_PENDING','64')):raise HTTPException(429,'最多%s个未完成任务'%os.environ.get('MEDIA_MAX_PENDING','64'))
  nid=uuid.uuid4().hex;p=J/nid;p.mkdir()
  for fn in ('first-frame.png','last-frame.png','input-image.png','edit-image.png','ref-audio.wav','draft.mp4'):
   f=src/fn
   if f.exists():shutil.copy(f,p/fn)
  for pattern in ('edit-image-*.png','ref-image-*.png','ref-video-*.mp4','ref-audio-*.wav'):
   for f in src.glob(pattern):shutil.copy(f,p/f.name)
  (p/'input.json').write_text(json.dumps(params,ensure_ascii=False))
  nd={'id':nid,'status':'queued','created_at':time.time(),'queued_since':None if queue_paused else time.time(),'parameters':params,'metadata':d.get('metadata',{}),'execution':execution,'idempotency_key':idempotency_key,'request_fingerprint':fingerprint,'client':d.get('client'),'retried_from':id,'retry_by_ip':request.client.host if request.client else None,'retry_by_ua':request.headers.get('user-agent','')}
  (p/'execution.json').write_text(json.dumps(execution,ensure_ascii=False))
  protocol.persist(p,nd);write(p,nd);wake.set();return view(p)
@app.get('/api/jobs',dependencies=[Depends(authorize)])
def jobs(request:Request):
 with lock:
  rows=[]
  for q in J.iterdir():
   if (q/'job.json').exists():rows.append((q,read(q)))
  qpos={}
  for i,r in enumerate(sorted((d for _,d in rows if d['status']=='queued'),key=qorder_key)):qpos[r['id']]=i+1
  rows.sort(key=lambda x:x[1]['created_at'],reverse=True)
  data=[view(p,qpos,d) for p,d in rows]
 # ETag：排除逐秒波动的 remaining_seconds（按5秒分桶），无实质变化时客户端收 304，前端跳过渲染
 etag_src='|'.join(f"{d['id']}|{d['status']}|{d.get('stage')}|{json.dumps(d.get('stage_info') or {},sort_keys=True,ensure_ascii=False)}|{d.get('queue_position')}|{d.get('bumped')}|{d.get('pid')}|{d.get('started_at')}|{d.get('finished_at')}|{d.get('error')}|{d.get('retried_from')}|{json.dumps(d.get('parameters') or {},sort_keys=True,ensure_ascii=False)}|{(d.get('eta') or {}).get('state')}|{(d.get('eta') or {}).get('est_total_seconds')}|{(d.get('eta') or {}).get('history_samples')}|{int(((d.get('eta') or {}).get('remaining_seconds') or 0)//5)}|{d.get('has_input_image')}|{d.get('pause_requested')}|{d.get('cancel_requested')}|{queue_paused}" for d in data)
 etag='"mjobs-'+hashlib.sha256(etag_src.encode()).hexdigest()[:32]+'"'
 if (request.headers.get('if-none-match') or '').strip('"')==etag.strip('"'):
  return Response(status_code=304,headers={'ETag':etag,'Cache-Control':'no-store'})
 return JSONResponse({'data':data,'etag':etag},headers={'ETag':etag,'Cache-Control':'no-store'})
@app.get('/api/jobs/{id}',dependencies=[Depends(authorize)])
def job(id:str):
 with lock:return view(folder(id))
@app.post('/api/jobs/{id}/cancel',dependencies=[Depends(authorize)])
def cancel(id:str):
 with lock:
  p=folder(id);d=read(p)
  if d['status']=='queued' or (d['status']=='paused' and d.get('paused_from')!='running'):
   if d['status']=='queued':freeze_wait(d)
   d.update(status='cancelled',finished_at=time.time());d.pop('paused_at',None)
  elif d['status'] in ('running','paused'):d['cancel_requested']=True
  write(p,d);wake.set();return view(p)
@app.get('/api/jobs/{id}/output',dependencies=[Depends(authorize)])
def output(id:str):
 p=folder(id);d=read(p)
 if d['status']!='succeeded':raise HTTPException(409,'任务尚未成功')
 f=checked_output(p);return FileResponse(f,filename=f'{id}{f.suffix}')
@app.get('/api/jobs/{id}/result',dependencies=[Depends(authorize)])
def result(id:str):
 p=folder(id)
 if not (p/'result.json').exists():raise HTTPException(409,'尚无结果')
 return json.loads((p/'result.json').read_text())
@app.get('/api/jobs/{id}/input-image',dependencies=[Depends(authorize)])
def input_image(id:str):
 p=folder(id);f=p/'input-image.png'
 if not f.exists():raise HTTPException(409,'该任务没有输入图')
 return FileResponse(f)
@app.get('/api/jobs/{id}/log',response_class=PlainTextResponse,dependencies=[Depends(authorize)])
def logs(id:str):
 p=folder(id);f=p/'run.log'
 if not f.exists():return '尚无日志'
 with f.open('rb') as stream:stream.seek(max(0,f.stat().st_size-16000));return stream.read().decode(errors='replace')

from model_gateway.huobao_compat import install as install_huobao_compat
install_huobao_compat(app, globals())

@app.get('/api/jobs/{id}/output.{extension}',dependencies=[Depends(authorize)])
def named_output(id:str,extension:str):
 d=read(folder(id))
 expected=checked_output(folder(id)).suffix.lstrip('.') if d['status']=='succeeded' else {'image':'png','video':'mp4','audio':'wav'}[d['parameters']['kind']]
 if extension!=expected:raise HTTPException(404,'产物类型不匹配')
 return output(id)

@app.get('/api/models/{profile}/probe',dependencies=[Depends(authorize)])
def probe(profile:str):
 if profile not in MODELS:raise HTTPException(404,'未知配置')
 return registry.probe(profile,R)

@app.post('/api/validate',dependencies=[Depends(authorize)])
def validate(body:JobRequest):
 return {'valid':True,'parameters':body.model_dump(exclude={k for k in type(body).model_fields if k.endswith('_base64')}),'probe':registry.probe(body.model,R),'execution':registry.snapshot(body.model,R)}

@app.get('/api/engines',dependencies=[Depends(authorize)])
def engines():
 return {'data':[{'id':name,'type':engine['type'],'profiles':[key for key,value in MODELS.items() if value['engine']==name]} for name,engine in registry.engines.items()]}

# All callers converge on JobRequest validation, one queue and task.json v1.
V1_PARAMETERS=set(JobRequest.model_fields)-{'kind','model','prompt','options','metadata','derived_from'}-{key for key in JobRequest.model_fields if key.endswith('_base64')}

@app.post('/api/v1/tasks',status_code=202,dependencies=[Depends(authorize)])
def submit_v1(body:protocol.TaskRequest,request:Request,idempotency_key:str|None=Header(default=None,min_length=1,max_length=160)):
 try:
  payload=protocol.to_legacy(body,folder,V1_PARAMETERS)
  validated=JobRequest(**payload)
 except (ValueError,ValidationError) as exc:raise HTTPException(422,str(exc))
 accepted=submit(validated,request,idempotency_key)
 return protocol.task_view(folder(accepted['id']),accepted)

@app.post('/api/v1/tasks/validate',dependencies=[Depends(authorize)])
def validate_v1(body:protocol.TaskRequest):
 try:normalized=JobRequest(**protocol.to_legacy(body,folder,V1_PARAMETERS))
 except (ValueError,ValidationError) as exc:raise HTTPException(422,str(exc))
 return {'valid':True,'profile':body.profile,'selection':registry.snapshot(body.profile,R),'parameters':normalized.model_dump(exclude={k for k in JobRequest.model_fields if k.endswith('_base64')})}

@app.get('/api/v1/tasks/{id}',dependencies=[Depends(authorize)])
def task_v1(id:str):
 with lock:return protocol.task_view(folder(id),view(folder(id)))

@app.get('/api/v1/tasks/{id}/draft',dependencies=[Depends(authorize)])
def draft_v1(id:str):
 with lock:return protocol.draft(folder(id),read(folder(id)))

@app.get('/api/v1/tasks/{id}/assets/{asset_id}',dependencies=[Depends(authorize)])
def input_asset_v1(id:str,asset_id:str):
 available=protocol.asset_files(folder(id))
 if asset_id not in available:raise HTTPException(404,'输入素材不存在')
 return FileResponse(available[asset_id][0])

@app.post('/api/v1/tasks/{id}/cancel',dependencies=[Depends(authorize)])
def cancel_v1(id:str):
 cancel(id);return task_v1(id)

@app.post('/api/v1/tasks/{id}/retry',status_code=202,dependencies=[Depends(authorize)])
def retry_v1(id:str,request:Request,idempotency_key:str|None=Header(default=None,min_length=1,max_length=160)):
 accepted=retry(id,request,idempotency_key);return task_v1(accepted['id'])

@app.get('/api/v1/schema',dependencies=[Depends(authorize)])
def task_schema():
 return {'request':protocol.TaskRequest.model_json_schema(),'task_document':protocol.TaskDocument.model_json_schema(),'engine_result':protocol.EngineResult.model_json_schema(),'progress':protocol.Progress.model_json_schema()}

@app.get('/api/v1/tasks/{id}/artifacts/{index}',dependencies=[Depends(authorize)])
def artifact_v1(id:str,index:int):
 from model_gateway.execution import checked_file
 root=folder(id);row=read(root)
 if row['status']!='succeeded':raise HTTPException(409,'任务尚未成功')
 data=json.loads((root/'result.json').read_text())
 items=data.get('artifacts') or [{'path':data['file'],'kind':row['parameters']['kind']}]
 if index<0 or index>=len(items):raise HTTPException(404,'产物不存在')
 item=items[index]
 return FileResponse(checked_file(root,item['path'],item['kind']))

@app.post('/api/v1/tasks/{id}/pause',dependencies=[Depends(authorize)])
def pause_v1(id:str):
 pause_job(id);return task_v1(id)

@app.post('/api/v1/tasks/{id}/resume',dependencies=[Depends(authorize)])
def resume_v1(id:str):
 resume_job(id);return task_v1(id)

@app.post('/api/v1/tasks/{id}/bump',dependencies=[Depends(authorize)])
def bump_v1(id:str):
 bump(id);return task_v1(id)
