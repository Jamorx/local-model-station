import pathlib,json,time,sys,subprocess,os
ROOT=pathlib.Path(__file__).resolve().parent;R=pathlib.Path(os.environ.get('MEDIA_LAB_ROOT',str(pathlib.Path.home()/'LocalModelLab'))).expanduser().resolve();D=pathlib.Path(sys.argv[1]);p=json.loads((D/'input.json').read_text());start=time.monotonic()
snapshot=json.loads((D/'execution.json').read_text());original_params=dict(p);p['model']=snapshot['profile']['backend'];MODEL_DIR=pathlib.Path(snapshot['model_dir'])
def stage(s): (D/'stage.json').write_text(json.dumps({'stage':s,'time':time.time()}))
stage('loading')
if p['kind']=='image' and p['model']=='flux3':
 stage('generating')
 cmd=[os.environ.get('MEDIA_IRIS_BIN',str(pathlib.Path.home()/'Desktop/gpt/local-model-lab/iris.c/iris')),'-d',str(MODEL_DIR),'-p',p['prompt'],'-o',str(D/'output.png'),'-W',str(p['width']),'-H',str(p['height']),'-s',str(p['steps']),'-S',str(p['seed'])]
 if p.get('guidance') is not None:cmd+=['-g',str(p['guidance'])]
 subprocess.run(cmd,check=True)
 if not (D/'output.png').is_file():raise RuntimeError('FLUX.3 未生成输出文件')
 result={'file':'output.png','backend':'iris-metal','note':'FLUX.3 Image Klein; BF16; 16 steps default'}
elif p['kind']=='image' and p['model']=='moss-edit':
 inp=D/'input-image.png'
 cmd=[str(R/'venvs/moss-edit/bin/python'),str(ROOT/'moss_edit.py'),'--model-dir',str(MODEL_DIR),'--adapter',str(R/'models/Moss-Image-Edit'),'--input',str(inp),'--prompt',p['prompt'],'--output',str(D/'output.png'),'--width',str(p['width']),'--height',str(p['height']),'--steps',str(p['steps']),'--seed',str(p['seed']),'--guidance',str(p['guidance'] if p.get('guidance') is not None else 3.0),'--strength',str(p['image_strength'] if p.get('image_strength') is not None else 1.0)]
 subprocess.run(cmd,check=True,cwd=str(ROOT));stage('saving')
 result={'file':'output.png','input_image':'input-image.png' if inp.exists() else None,'note':'MOSS-Image Edit LoRA（参考图前缀 t0），非 mflux 路径；steps 默认 50，可用高级参数降到 30 提速'}
elif p['kind']=='image' and p['model']=='h3-image':
 stage('generating')
 edit_imgs=sorted(D.glob('edit-image*.png'));edit_flags=[]
 for k,edit in enumerate(edit_imgs):
  enc=D/f'edit_ref-{k:02d}'
  subprocess.run([str(R/'venvs/h3image/bin/python'),str(ROOT/'h3edit_encode.py'),str(edit),str(enc),str(p['seed'])],check=True)
  edit_flags+=['--edit-image',str(edit),'--edit-latent',str(enc)+'.f32']
 cmd=[str(R/'h3-bin/h3-image'),'--image','--model-dir',str(MODEL_DIR),'-p',p['prompt'],'--width',str(p['width']),'--height',str(p['height']),'--seed',str(p['seed']),'--output',str(D/'latent.f32')]
 cmd+=edit_flags
 subprocess.run(cmd,check=True,cwd=str(R/'h3-bin'))
 stage('decoding')
 dcmd=[str(R/'venvs/h3image/bin/python'),str(ROOT/'h3_image_decode.py'),'--latent',str(D/'latent.f32'),'--out',str(D/'output.png')]
 subprocess.run(dcmd,check=True)
 if not (D/'output.png').is_file():raise RuntimeError('H3-Image 未生成输出文件')
 result={'file':'output.png','backend':'h3-bin h3-image (BF16 SSD streaming) + diffusers VAE MPS','note':'pdd distilled 6 steps, no CFG; small canvases auto-upscale to >=2048^2 for quality; decode via official bridge (latents*std+mean, ImageNet unnorm, 8-bit truncation); 参考图编辑最多16张：任务目录 edit-image-XX.png 按文件名排序依次成为 Picture 1..N，prompt 用 <Picture n> 引用'}
elif p['kind']=='image':
 import mlx.core as mx
 if p['model']=='zimage':
  from mflux.models.z_image import ZImageTurbo
  cls,path,q=ZImageTurbo,'Z-Image-Turbo',8
 else:
  from mflux.models.moss_image import MOSSImage
  cls,path,q=MOSSImage,'MOSS-Image-SFT',8
 model=cls(quantize=q,model_path=str(MODEL_DIR));load=time.monotonic()-start;stage('generating');mx.reset_peak_memory();t=time.monotonic()
 args=dict(seed=p['seed'],prompt=p['prompt'],num_inference_steps=p['steps'],width=p['width'],height=p['height'])
 if p.get('guidance') is not None:args['guidance']=p['guidance']
 inp=D/'input-image.png'
 if inp.exists():
  if p['model']=='zimage':args['image_path']=str(inp)
  else:args['image_paths']=[str(inp)]
  if p.get('image_strength') is not None:
   if p['model']=='zimage':args['image_strength']=p['image_strength']
   else:args['strength']=p['image_strength']
 img=model.generate_image(**args);stage('saving');img.save(str(D/'output.png'));result={'file':'output.png','input_image':'input-image.png' if (D/'input-image.png').exists() else None,'load_seconds':load,'generation_seconds':time.monotonic()-t,'mlx_peak_bytes':mx.get_peak_memory(),'memory_scope':'MLX allocation peak, not whole-system memory'}
elif p['kind']=='video' and p['model']=='vpipe-2k':
 stage('probing_draft')
 draft=D/'draft.mp4'
 if not draft.is_file():raise RuntimeError('缺少底片 draft.mp4')
 ff=os.environ.get('MEDIA_FFPROBE_BIN','/opt/homebrew/bin/ffprobe')
 fp=json.loads(subprocess.check_output([ff,'-v','error','-show_streams','-show_format','-of','json',str(draft)]))
 has_audio=any(s['codec_type']=='audio' for s in fp['streams'])
 total_dur=float(fp['format']['duration'])
 req=int(p.get('frames') or 0)
 frames=min(req if req else max(2,round(total_dur*24)),48)
 dur_s=frames/24
 tpl=json.loads((ROOT/'assets/vpipe-2k.vpipeline').read_text())
 st={s['id']:s for s in tpl['stages']}
 st['model-select']['config']['hf_dir']=str(MODEL_DIR)
 st['latent-upscale']['config']['hf_dir']=os.environ.get('MEDIA_H3_UPSAMPLER_DIR',str(pathlib.Path.home()/'vpipe/sandbox/h3-2k-test/assets/latent_upsampler'))
 st['load-draft']['config'].update(input_url=str(draft),enable_audio=False)
 st['draft-head']['config'].update(start=0,end=frames)
 fw,fh=(1024,1792) if p['height']>=p['width'] else (1792,1024)
 st['draft-fit']['config'].update(width=fw,height=fh)
 st['vae-encode']['config']['posterior_seed']=p['seed']
 st['latent-upscale']['config'].update(width=p['width'],height=p['height'],frames=frames)
 st['text-prompt']['config']['text']=p['prompt']
 LORAS={'open_h3_2k':os.environ.get('MEDIA_H3_REGENERATE_LORA',str(pathlib.Path.home()/'vpipe/sandbox/h3-2k-test/assets/lora/open_h3_regenerate_2k_lora.safetensors')),'pika_flex_ref2va':os.environ.get('MEDIA_PIKA_LORA',str(pathlib.Path.home()/'Desktop/dsh/video/comfyui/models/loras/pika_h3_flex_ref2va_v1_bf16.safetensors'))}
 st['minimax-h3-model-config']['config'].update(lora=LORAS.get(p.get('lora') or 'open_h3_2k'),lora_scale=p.get('lora_scale') if p.get('lora_scale') is not None else 1.0)
 st['generate-video']['config'].update(width=p['width'],height=p['height'],frames=frames,seed=p['seed'],steps=p['steps'])
 wav=D/'audio-track.wav'
 if has_audio:subprocess.run([os.environ.get('MEDIA_FFMPEG_BIN','/opt/homebrew/bin/ffmpeg'),'-v','error','-y','-i',str(draft),'-t',f'{dur_s:.6f}','-ar','48000','-ac','2',str(wav)],check=True)
 else:subprocess.run([os.environ.get('MEDIA_FFMPEG_BIN','/opt/homebrew/bin/ffmpeg'),'-v','error','-y','-f','lavfi','-i','anullsrc=r=48000:cl=stereo','-t',f'{dur_s:.6f}','-ar','48000','-ac','2',str(wav)],check=True)
 st['load-audio']['config'].update(input_url=str(wav),duration_s=dur_s)
 st['save-video']['config']['output_url']=str(D/'output.mp4')
 (D/'job.vpipeline').write_text(json.dumps(tpl,ensure_ascii=False))
 stage('generating')
 subprocess.run([os.environ.get('MEDIA_VPIPE_BIN','/Applications/Vpipe Manager.app/Contents/Helpers/vpipe'),'--memory-cap-mb','40960','--launch',str(D/'job.vpipeline')],check=True)
 if not (D/'output.mp4').is_file():raise RuntimeError('vpipe 未生成 output.mp4')
 stage('probing')
 probe=json.loads(subprocess.check_output([ff,'-v','error','-show_streams','-show_format','-of','json',str(D/'output.mp4')]))
 result={'file':'output.mp4','ffprobe':probe,'note':'H3 latent 2K 重制（vpipe steps=3 regenerate LoRA）：保持底片身份与构图，小文字/条码区域会被重生，不建议用于含文字特写'}
elif p['kind']=='video' and p['model']=='vpipe-h3':
 stage('building_pipeline')
 tpl=json.loads((ROOT/'assets/vpipe-h3.vpipeline').read_text())
 st={s['id']:s for s in tpl['stages']}
 st['model-select']['config']['hf_dir']=str(MODEL_DIR)
 first=D/'first-frame.png';last=D/'last-frame.png'
 if last.is_file() and not first.is_file():raise RuntimeError('vpipe-h3 提供尾帧时必须同时提供首帧')
 frames=round(p['seconds']*24)
 LORAS={'pika_flex_fl2va':os.environ.get('MEDIA_H3_PIKA_FL2VA_LORA',str(pathlib.Path.home()/'Desktop/dsh/video/comfyui/models/loras/pika_h3_flex_fl2va_v1_bf16.safetensors')),'turbo_fl2va_lite':os.environ.get('MEDIA_H3_TURBO_LITE_LORA',str(pathlib.Path.home()/'Desktop/dsh/video/comfyui/models/loras/pika_h3_flex_turbo_4step_fl2va_lite_r32.safetensors'))}
 lora=LORAS.get(p.get('lora'))
 if p.get('lora') and not pathlib.Path(lora).is_file():raise RuntimeError('LoRA 文件不存在: '+str(lora))
 for key,img in (('first',first),('last',last)):
  if img.is_file():st['load-image-'+key]['config']['url']=[str(img)];st[key+'-frame']['config'].update(width=p['width'],height=p['height'])
  else:
   drop=('load-image-'+key,key+'-frame','vae-encode-'+key)
   tpl['stages']=[s for s in tpl['stages'] if s['id'] not in drop]
   for s in tpl['stages']:
    for x in s.get('iports') or []:
     if x['src'] in drop:x['src']=''
 st={s['id']:s for s in tpl['stages']}
 st['text-prompt']['config']['text']=p['prompt']
 if p.get('lora')=='pika_flex_fl2va' and first.is_file():st['diffusion-conditioner']['iports']=[{'src':'text-prompt','oport':0},{'src':'','oport':0},{'src':'model-select','oport':0},{'src':'first-frame','oport':0}]
 mc=st['minimax-h3-model-config']['config']
 if lora:mc['lora']=lora;mc['lora_scale']=p['lora_scale'] if p.get('lora_scale') is not None else 1.0
 st['generate-video']['config'].update(width=p['width'],height=p['height'],frames=frames,seed=p['seed'],steps=p['steps'])
 st['save-video']['config']['output_url']=str(D/'output.mp4')
 (D/'job.vpipeline').write_text(json.dumps(tpl,ensure_ascii=False))
 stage('generating')
 subprocess.run([os.environ.get('MEDIA_VPIPE_BIN','/Applications/Vpipe Manager.app/Contents/Helpers/vpipe'),'--memory-cap-mb','40960','--launch',str(D/'job.vpipeline')],check=True)
 if not (D/'output.mp4').is_file():raise RuntimeError('vpipe 未生成 output.mp4')
 stage('probing')
 ff=os.environ.get('MEDIA_FFPROBE_BIN','/opt/homebrew/bin/ffprobe')
 probe=json.loads(subprocess.check_output([ff,'-v','error','-show_streams','-show_format','-of','json',str(D/'output.mp4')]))
 result={'file':'output.mp4','ffprobe':probe,'note':'H3 生成（vpipe FL2VA：首/尾帧 VAE 条件+文本；pika_flex_fl2va 6步 / turbo_fl2va_lite 4步 / 无LoRA 16步），自带同步生成音轨'}
else:
 stage('generating');cmd=[str(R/'h3-bin/h3'),'--profile','-d',str(MODEL_DIR),'-p',p['prompt'],'--width',str(p['width']),'--height',str(p['height']),'--frames',str(round(p['seconds']*24)),'--steps',str(p['steps']),'--layers',str(p['layers']),'--reuse',str(p['reuse']),'--seed',str(p['seed']),'-o',str(D/'output.mp4')]
 if (D/'first-frame.png').exists():cmd+=['--first-frame',str(D/'first-frame.png')]
 if (D/'last-frame.png').exists():cmd+=['--last-frame',str(D/'last-frame.png')]
 for media in sorted(D.glob('ref-video-*.mp4')):cmd+=['--ref-video',str(media)]
 for media in sorted(D.glob('ref-audio-*.wav')):cmd+=['--ref-audio',str(media)]
 for k in range(12):
  rf=D/f'ref-image-{k:02d}.png'
  if rf.exists():cmd+=['--ref-image',str(rf)]
 ra=D/'ref-audio.wav'
 if ra.exists():cmd+=['--ref-audio',str(ra)]
 if p['precision']=='row-int8-fc2':cmd+=['--use-int8-row-fc2']
 if p['precision']=='bf16-projections':cmd+=['--use-slower-bf16-mlp','--use-slower-bf16-qkv','--use-slower-bf16-attention-output']
 subprocess.run(cmd,check=True,cwd=str(R/'h3-bin'));stage('probing');probe=json.loads(subprocess.check_output([os.environ.get('MEDIA_FFPROBE_BIN','/opt/homebrew/bin/ffprobe'),'-v','error','-show_streams','-show_format','-of','json',str(D/'output.mp4')]))
 result={'file':'output.mp4','ffprobe':probe,'note':'actual duration/frame count from ffprobe; mixed INT8 or BF16 projection fallback, not whole-model quantization'}
result['total_seconds']=time.monotonic()-start;result['parameters']=original_params;tmp=D/'result.tmp';tmp.write_text(json.dumps(result,ensure_ascii=False,indent=2));tmp.replace(D/'result.json');stage('completed')
