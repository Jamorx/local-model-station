"""Huobao's locally configured provider protocols over the single media queue."""
import base64
from email import policy
from email.parser import BytesParser
import io
import json
import math
import os
from pathlib import Path
import re
import subprocess
import tempfile
import urllib.error
import urllib.parse
import urllib.request

from fastapi import Depends, Header, HTTPException, Request
from fastapi.responses import JSONResponse, Response, StreamingResponse
from pydantic import ValidationError
from PIL import Image

RATIOS={'9:16':(768,1344),'16:9':(1344,768),'1:1':(992,992),'4:3':(1152,864),'3:4':(864,1152),'21:9':(1536,672)}
MAX_BODY=120*1024*1024


def image_size(raw):
    if raw in (None,'','auto'):return 1024,1024
    match=re.fullmatch(r'(\d+)[xX](\d+)',str(raw))
    if not match:raise HTTPException(422,'size 必须为 WxH 或 auto')
    w,h=map(int,match.groups())
    if min(w,h)<1:raise HTTPException(422,'无效图片尺寸')
    scale=min(1,1536/max(w,h),math.sqrt(1572864/(w*h)))
    return max(256,int(w*scale)//32*32),max(256,int(h*scale)//32*32)


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self,*args,**kwargs):raise HTTPException(422,'参考素材重定向不受支持')


def read_reference(url,limit):
    if not isinstance(url,str):raise HTTPException(422,'参考素材 URL 必须是字符串')
    if url.startswith('data:'):
        try:raw=base64.b64decode(url.split(',',1)[1],validate=True)
        except Exception:raise HTTPException(422,'无效 data URL')
    else:
        # Restrict fetches to the explicitly configured Huobao origin and its static files.
        base=os.environ.get('MEDIA_HUOBAO_BASE_URL','http://127.0.0.1:3013').rstrip('/')
        target=urllib.parse.urljoin(base+'/',url)
        parsed=urllib.parse.urlsplit(target);allowed=urllib.parse.urlsplit(base)
        if (parsed.scheme,parsed.netloc)!=(allowed.scheme,allowed.netloc) or not parsed.path.startswith('/static/') or parsed.query or parsed.fragment:
            raise HTTPException(422,'参考素材需为 data URL 或配置的火宝 /static/ 地址')
        try:
            op=urllib.request.build_opener(urllib.request.ProxyHandler({}),NoRedirect())
            with op.open(target,timeout=20) as r:raw=r.read(limit+1)
        except HTTPException:raise
        except Exception:raise HTTPException(422,'无法读取火宝参考素材')
    if not raw or len(raw)>limit:raise HTTPException(422,'参考素材为空或超过大小限制')
    return raw


def normalize_audio(raw):
    with tempfile.TemporaryDirectory() as tmp:
        src=Path(tmp)/'input';dst=Path(tmp)/'audio.wav';src.write_bytes(raw)
        try:subprocess.run([os.environ.get('MEDIA_FFMPEG_BIN','/opt/homebrew/bin/ffmpeg'),'-v','error','-nostdin','-i',str(src),'-vn','-ar','32000','-ac','1',str(dst)],capture_output=True,check=True,timeout=30)
        except Exception:raise HTTPException(422,'参考音频无法解码为 WAV')
        if dst.stat().st_size>6*1024*1024:raise HTTPException(422,'转换后音频超过6MiB')
        return dst.read_bytes()


def install(app,g):
    auth=[Depends(g['authorize'])]
    def models():
        return [os.environ.get('MEDIA_TEXT_MODEL','Qwen3.8-Flash-Next-GSQ-RCO-IQ3_S')]
    def output_url(request,id,kind):
        base=os.environ.get('MEDIA_PUBLIC_BASE_URL',str(request.base_url).rstrip('/'))
        extension=Path(g['result'](id)['file']).suffix.lstrip('.')
        return base+'/api/jobs/'+id+'/output.'+extension
    def enqueue(payload,request,key):
        try:body=g['JobRequest'](**payload)
        except ValidationError as exc:raise HTTPException(422,str(exc))
        return g['submit'](body,request,key)
    async def body_bytes(request):
        data=bytearray()
        async for chunk in request.stream():
            data.extend(chunk)
            if len(data)>MAX_BODY:raise HTTPException(413,'请求体超过120MiB')
        return bytes(data)
    async def json_body(request):
        try:body=json.loads(await body_bytes(request))
        except (ValueError,UnicodeError):raise HTTPException(422,'请求必须是有效 JSON')
        if not isinstance(body,dict):raise HTTPException(422,'请求必须是 JSON 对象')
        return body
    def image_payload(body,images=None):
        unknown=set(body)-{'model','prompt','size','n','response_format','seed','steps','guidance'}
        if unknown:raise HTTPException(422,'不支持的图片参数: '+', '.join(sorted(unknown)))
        if body.get('response_format','url')!='url':raise HTTPException(422,'异步图片协议只支持 response_format=url')
        model=body.get('model','zimage')
        if model not in g['MODELS'] or g['MODELS'][model]['kind']!='image':raise HTTPException(422,'不支持该本地图像模型，不会自动替换模型')
        if str(body.get('n',1))!='1':raise HTTPException(422,'火宝兼容接口仅支持 n=1')
        w,h=image_size(body.get('size'));payload={'kind':'image','model':model,'prompt':body.get('prompt',''),'width':w,'height':h}
        for field in ('seed','steps','guidance'):
            if field in body:payload[field]=body[field]
        if images:
            if len(images)!=1:raise HTTPException(422,'当前本地图像编辑仅支持一张参考图，不会静默丢弃其余素材')
            field={'zimage':'input_image_base64','moss-edit':'input_image_base64','h3-image':'edit_image_base64'}.get(g['MODELS'][model].get('backend',model))
            if not field:raise HTTPException(422,'该模型未实现参考图编辑')
            payload[field]=base64.b64encode(images[0]).decode()
        return payload

    @app.get('/api/compatibility',dependencies=auth)
    def capabilities():
        return {'huobao':{'text':{'provider':'openai','models':models(),'endpoint':'/v1/chat/completions','stream':True},'image':{'provider':'openai','models':[k for k,v in g['MODELS'].items() if v['kind']=='image'],'endpoints':['/v1/images/generations','/v1/images/edits','/v1/images/task/{id}'],'edit_reference_limit':1},'video':{'provider':'minimax','models':['MiniMax-H3','MiniMax-H3-vpipe'],'endpoints':['/v2/video_generation','/v2/query/video_generation/{id}'],'resolution':['768P'],'unsupported':['2K direct generation','first_frame combined with reference_image','MiniMax-H3-vpipe: reference image/video/audio, duration over 10s']}},'note':'Other cloud providers remain configured directly in Huobao; this is not a cloud-model emulator.'}

    @app.get('/v1/models',dependencies=auth)
    def list_models():
        return {'object':'list','data':[{'id':m,'object':'model','owned_by':'local'} for m in models()+[k for k,v in g['MODELS'].items() if v['kind']=='image']+['MiniMax-H3','MiniMax-H3-vpipe']]}

    @app.post('/v1/images/generations',status_code=202,dependencies=auth)
    async def generate_image(request:Request,idempotency_key:str|None=Header(default=None,min_length=1,max_length=160)):
        payload=image_payload(await json_body(request));job=enqueue(payload,request,idempotency_key)
        return {'task_id':job['id'],'id':job['id'],'size':f"{payload['width']}x{payload['height']}"}

    @app.post('/v1/images/edits',status_code=202,dependencies=auth)
    async def edit_image(request:Request,idempotency_key:str|None=Header(default=None,min_length=1,max_length=160)):
        ct=request.headers.get('content-type','')
        if not ct.startswith('multipart/form-data'):raise HTTPException(422,'需要 multipart/form-data')
        message=BytesParser(policy=policy.default).parsebytes(b'Content-Type: '+ct.encode()+b'\r\nMIME-Version: 1.0\r\n\r\n'+await body_bytes(request))
        fields={};images=[]
        if not message.is_multipart():raise HTTPException(422,'无效 multipart')
        for part in message.iter_parts():
            name=part.get_param('name',header='content-disposition');data=part.get_payload(decode=True) or b''
            if name in ('image','image[]'):
                if len(data)>4*1024*1024:raise HTTPException(422,'参考图片超过4MiB')
                images.append(data)
            elif name:
                try:fields[name]=data.decode('utf8')
                except UnicodeError:raise HTTPException(422,'无效表单编码')
        if not images:raise HTTPException(422,'缺少 image[]')
        job=enqueue(image_payload(fields,images),request,idempotency_key)
        return {'task_id':job['id'],'id':job['id']}

    @app.get('/v1/images/task/{id}',dependencies=auth)
    def image_status(id:str,request:Request):
        job=g['job'](id)
        if job['parameters']['kind']!='image':raise HTTPException(404,'非图片任务')
        if job['status']=='succeeded':return {'status':'completed','image_url':output_url(request,id,'image')}
        if job['status'] in ('failed','cancelled','interrupted'):return {'status':'failed','error':{'message':job.get('error') or job['status']}}
        return {'status':'processing','stage':job.get('stage') or job['status']}

    @app.post('/v2/video_generation',status_code=202,dependencies=auth)
    async def generate_video(request:Request,idempotency_key:str|None=Header(default=None,min_length=1,max_length=160)):
        body=await json_body(request)
        unknown=set(body)-{'model','content','duration','resolution','ratio','seed'}
        if unknown:raise HTTPException(422,'不支持的视频参数: '+', '.join(sorted(unknown)))
        video_model=body.get('model','MiniMax-H3')
        if video_model not in ('MiniMax-H3','MiniMax-H3-vpipe'):raise HTTPException(422,'本地视频模型仅 MiniMax-H3 / MiniMax-H3-vpipe，不会自动替换模型')
        if str(body.get('resolution','768P')).upper()!='768P':raise HTTPException(422,'H3 直接生成仅支持768P；2K需另走vpipe底片重制，不会静默降档')
        content=body.get('content')
        if not isinstance(content,list):raise HTTPException(422,'content 必须是数组')
        texts=[];refs={'first_frame':[],'last_frame':[],'reference_image':[],'reference_video':[],'reference_audio':[]}
        for item in content:
            if not isinstance(item,dict):raise HTTPException(422,'无效 content 元素')
            typ=item.get('type');role=item.get('role')
            if typ=='text':
                if not isinstance(item.get('text'),str):raise HTTPException(422,'text 必须为字符串')
                texts.append(item['text']);continue
            if typ=='audio_url':role=role or 'reference_audio'
            if role not in refs or typ!=('video_url' if role=='reference_video' else 'audio_url' if role=='reference_audio' else 'image_url'):raise HTTPException(422,'不支持的参考素材类型或 role')
            value=item.get(typ)
            if not isinstance(value,dict):raise HTTPException(422,'参考素材缺少 URL')
            refs[role].append(read_reference(value.get('url'),(32 if role=='reference_video' else 6 if role=='reference_audio' else 4)*1024*1024))
        for name,cap in [('first_frame',1),('last_frame',1),('reference_image',9),('reference_video',3),('reference_audio',3)]:
            if len(refs[name])>cap:raise HTTPException(422,f'{name} 最多 {cap} 份')
        ratio=body.get('ratio','16:9')
        if refs['first_frame']:
            try:
                with Image.open(io.BytesIO(refs['first_frame'][0])) as im:ratio=min(RATIOS,key=lambda k:abs(RATIOS[k][0]/RATIOS[k][1]-im.width/im.height))
            except Exception:raise HTTPException(422,'首帧不是有效图片')
        if ratio not in RATIOS:raise HTTPException(422,'不支持该画幅比例')
        w,h=RATIOS[ratio];payload={'kind':'video','model':'vpipe-h3' if video_model=='MiniMax-H3-vpipe' else 'h3','prompt':'\n'.join(texts),'width':w,'height':h,'seconds':body.get('duration',5),'seed':body.get('seed',42)}
        b64=lambda raw:base64.b64encode(raw).decode()
        for name in ('first_frame','last_frame'):
            if refs[name]:payload[name+'_base64']=b64(refs[name][0])
        if refs['reference_image']:payload['ref_images_base64']=[b64(x) for x in refs['reference_image']]
        if refs['reference_video']:payload['ref_videos_base64']=[b64(x) for x in refs['reference_video']]
        if refs['reference_audio']:payload['ref_audios_base64']=[b64(normalize_audio(x)) for x in refs['reference_audio']]
        job=enqueue(payload,request,idempotency_key)
        return {'task_id':job['id']}

    @app.get('/v2/query/video_generation/{id}',dependencies=auth)
    def video_status(id:str,request:Request):
        job=g['job'](id)
        if job['parameters']['kind']!='video':raise HTTPException(404,'非视频任务')
        status=job['status'];task={'id':id,'status':'succeeded' if status=='succeeded' else 'failed' if status in ('failed','cancelled','interrupted') else 'processing'}
        if status=='succeeded':task['content']={'url':output_url(request,id,'video')}
        elif task['status']=='failed':task['error']={'message':job.get('error') or status}
        else:task['stage']=job.get('stage') or status
        return {'task':task}

    @app.post('/v1/chat/completions',dependencies=auth)
    async def chat(request:Request):
        body=await json_body(request)
        if body.get('model') not in models():raise HTTPException(422,'未配置该文本模型')
        base=os.environ.get('MEDIA_TEXT_BASE_URL','http://127.0.0.1:8000/v1').rstrip('/')
        if base==str(request.base_url).rstrip('/')+'/v1':raise HTTPException(503,'文本上游不能指向自身')
        headers={'Content-Type':'application/json'}
        key=os.environ.get('MEDIA_TEXT_API_KEY','')
        settings=g['R']/'omlx-data/settings.json'
        if not key and 'MEDIA_TEXT_BASE_URL' not in os.environ and settings.is_file():key=json.loads(settings.read_text()).get('auth',{}).get('api_key','')
        if key:headers['Authorization']='Bearer '+key
        upstream=urllib.request.Request(base+'/chat/completions',data=json.dumps(body).encode(),headers=headers)
        try:
            # Run blocking header wait outside the event loop.
            from starlette.concurrency import run_in_threadpool
            op=urllib.request.build_opener(urllib.request.ProxyHandler({}))
            response=await run_in_threadpool(lambda:op.open(upstream,timeout=180))
        except urllib.error.HTTPError as exc:raise HTTPException(exc.code,'文本上游拒绝请求')
        except Exception:raise HTTPException(502,'无法连接文本上游')
        def chunks():
            try:
                while True:
                    chunk=response.read1(65536)
                    if not chunk:break
                    yield chunk
            finally:response.close()
        return StreamingResponse(chunks(),media_type='text/event-stream' if body.get('stream') else 'application/json',headers={'Cache-Control':'no-store','X-Accel-Buffering':'no'})
