/* 本地媒体实验室 · 共享核心（两页共用） */
const $=id=>document.getElementById(id);
const STATUS_ZH={queued:'排队中',running:'运行中',succeeded:'已完成',failed:'失败',cancelled:'已取消',interrupted:'重启中断'};
const STAGE_ZH={preflight:'预检',loading:'加载模型',generating:'生成中',saving:'保存产物',probing:'视频 ffprobe 分析',completed:'完成',waiting_memory:'等待内存',waiting_no_test_process:'等待测试进程结束'};
const HINTS={flux3:'FLUX.3 Image Klein · Iris Metal · 默认16步。首次使用建议256×256；内存门槛暂设28GiB，耗时待实测。',zimage:'实测 1024² 中位约 8 秒（不含首次加载）。9 步，8bit，需可用 34GiB。',qwen21:'BF16 / 40 步，实测中位约 43 秒。内存占用最高，LLM 常驻时通常需要等待。需可用 50GiB。',moss:'8bit / 50 步，实测中位约 70 秒。需可用 28GiB。','moss-edit':'官方 MPS 路径 + Edit LoRA：输入图必填（编辑源图）。50 步约 10–25 分钟，步数改 30 更快。需可用 30GiB，LLM 常驻时排队等内存。',h3:'512²/5 秒级约 80 秒；720 级 15 秒可达约 40 分钟。原生 1080 不支持。需可用 30GiB。上传参考图（≤12 张有序）自动切换 Ref2VA 参考图生视频，与首帧互斥。'};
const RUNNER_CMD=id=>`由当前服务执行器管理（任务 ${id}）；请勿绕过队列手动运行`;
let CONFIGS={},health=null,connectBusy=false,lastErr='';
const blobCache=new Map();

function keyVal(){return $('key').value.trim()}
function loadKey(){
 if(localStorage.getItem('media-key-remembered')==='1'){$('remember').checked=true;$('key').value=localStorage.getItem('media-key')||''}
 else $('key').value=sessionStorage.getItem('media-key')||'';
}
function initKeyBar(){
 loadKey();
 $('key').oninput=()=>{const v=keyVal();if(!v)return;
  if($('remember').checked){localStorage.setItem('media-key',v);localStorage.setItem('media-key-remembered','1')}
  else sessionStorage.setItem('media-key',v);
  if($('conn-text').textContent==='未连接'&&!connectBusy)connect();};
 $('remember').onchange=()=>{const v=keyVal();
  if($('remember').checked){localStorage.setItem('media-key',v);localStorage.setItem('media-key-remembered','1')}
  else{localStorage.removeItem('media-key');localStorage.removeItem('media-key-remembered');sessionStorage.setItem('media-key',v)}};
}
async function api(path,options={}){
 const key=keyVal();const hdrs={...(options.headers||{})};if(key)hdrs.Authorization='Bearer '+key; // 局域网免 key：无 key 直接请求
 const r=await fetch(path,{...options,headers:hdrs});
 if(r.status===304)return r; // 数据未变化
 if(!r.ok){
  if(r.status===401)throw Error('密钥错误（401）');
  let d;try{d=await r.json()}catch(e){d={detail:await r.text()}}
  throw Error(typeof d.detail==='string'?d.detail:JSON.stringify(d.detail));
 }
 return r;
}
function toast(msg,type='ok'){
 const t=document.createElement('div');t.className='toast '+type;t.textContent=msg;
 $('toasts').append(t);requestAnimationFrame(()=>t.classList.add('show'));
 setTimeout(()=>{t.classList.remove('show');setTimeout(()=>t.remove(),300)},4600);
}
function setConn(on,text){$('conn').className='dot '+(on?'on':'off');$('conn-text').textContent=text}
function fmtT(s){s=Math.max(0,Math.round(s));if(s<60)return s+'秒';const m=Math.floor(s/60);if(m<60)return m+'分'+(s%60)+'秒';return Math.floor(m/60)+'时'+(m%60)+'分'}
function fmtTime(ts){return ts?new Date(ts*1000).toLocaleString('zh-CN',{hour12:false,year:'numeric',month:'2-digit',day:'2-digit',hour:'2-digit',minute:'2-digit',second:'2-digit'}):'–'}
function gb(n){return n?(n/2**30).toFixed(1)+' GiB':'–'}
function esc(s){return String(s??'').replace(/[&<>"]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]))}
function modelOf(j){return CONFIGS[j.parameters&&j.parameters.model]}
function modelName(j){return (modelOf(j)||{}).name||(j.parameters&&j.parameters.model)||'–'}

async function connect(){
 if(connectBusy)return;connectBusy=true;
 try{
  const m=await(await api('/api/models')).json();
  CONFIGS=Object.fromEntries(m.data.map(x=>[x.id,x]));
  setConn(true,'已连接');
  await refreshStatus();
  if(typeof onReady==='function')await onReady();
 }catch(e){setConn(false,e.message)}
 finally{connectBusy=false}
}
async function refreshStatus(){
 try{health=await(await fetch('/health')).json();renderStatus()}catch(e){}
}
function renderStatus(){
 if(!health)return;
 const a=health.available_gib;
 $('avail').textContent=a!=null?a.toFixed(1):'–';
 const fill=$('mem-fill');
 if(a!=null){fill.style.width=Math.min(100,a/96*100)+'%';fill.className=a>=46?'':(a>=34?'mid':'low')}
 const chips=$('model-chips');chips.replaceChildren();
 for(const [id,c] of Object.entries(CONFIGS)){
  const s=document.createElement('span');
  const ok=a!=null&&a>=c.available_gib_required;
  s.className='chip '+(ok?'ok':'wait');
  s.textContent=c.name+' 需'+c.available_gib_required+'G '+(ok?'✓':'⏳');
  chips.append(s);
 }
 $('active-job').textContent=health.active_job?'运行: '+health.active_job.slice(0,8):'空闲';
 $('wait-timeout').textContent='等待超时: '+(health.queue_wait_timeout_s?fmtT(health.queue_wait_timeout_s):'不限');
 if(typeof onStatus==='function')onStatus();
}
let jobsEtag=null;
async function fetchJobs(){
 try{
  const hdrs={};if(jobsEtag)hdrs['If-None-Match']=jobsEtag;
  const r=await api('/api/jobs',{headers:hdrs});
  if(r.status===304){const et=r.headers.get('ETag');if(et)jobsEtag=et;return null} // 数据未变化，跳过渲染
  const d=await r.json();
  const et=r.headers.get('ETag');
  if(et)jobsEtag=et;
  return d.data||[];
 }catch(e){
  jobsEtag=null;
  if(e.message!==lastErr){lastErr=e.message;toast(e.message,'err')}
  return null;
 }
}
/* 就地行同步：只在行集合/顺序变化时新建或移动 DOM 节点；已有行仅更新字段，视频/图片元素不被重建（播放不中断） */
function syncRows(container,desired,createFn,updateFn){
 const have=new Map();
 container.querySelectorAll(':scope > [data-id]').forEach(el=>have.set(el.dataset.id,el));
 const seen=new Set();
 for(const j of desired){
  let el=have.get(j.id);
  if(!el){el=createFn(j);have.set(j.id,el)}
  seen.add(j.id);
  updateFn(el,j);
  container.appendChild(el); // 顺序=desired 顺序；移动已有节点不重置其状态
 }
 have.forEach((el,id)=>{if(!seen.has(id))el.remove()});
}
function metaText(j){
 const parts=[];
 if(j.eta){
  if(j.eta.state==='running')parts.push('预计还需 <b>~'+fmtT(j.eta.remaining_seconds)+'</b>（'+(j.eta.history_samples?'按 '+j.eta.history_samples+' 次历史'+fmtT(j.eta.est_total_seconds)+'估算':'估算约'+fmtT(j.eta.est_total_seconds))+'）');
  else if(j.eta.state==='starting')parts.push('内存已满足，即将开始');
  else if(j.eta.state==='waiting_memory')parts.push('等待内存释放（时间不定）');
 }
 if(j.status==='running'){
  parts.push(STAGE_ZH[j.stage]||j.stage||'运行中');
  if(j.bumped)parts.push('<span class="chip bump">⚡已插队</span>');
  if(j.started_at)parts.push('<span class="elapsed">'+fmtT(Date.now()/1000-j.started_at)+'</span>');
 }else if(j.status==='queued'){
  const si=j.stage_info||{};
  if(j.stage==='waiting_memory'){
   const need=si.required_gib??(modelOf(j)||{}).available_gib_required;
   parts.push('等待内存：需 <b>'+(need??'–')+'</b> / 可用 <b>'+(si.available_gib??'–')+'</b> GiB');
   if(j.queue_position)parts.push('第 '+j.queue_position+' 位');
  }else if(j.stage==='waiting_no_test_process'){
   parts.push('检测到测试推理进程，等待其结束');
   if(j.queue_position)parts.push('第 '+j.queue_position+' 位');
  }else{
   if(j.queue_position)parts.push('第 '+j.queue_position+' 位');
  }
  if(j.bumped)parts.push('<span class="chip bump">已插队</span>');
 }else if(j.status==='succeeded'){
  if(j.elapsed_seconds!=null)parts.push('总耗时 '+fmtT(j.elapsed_seconds));
 }else if(j.error){parts.push('<span class="err">'+esc(j.error)+'</span>')}
 if(j.created_at)parts.push(fmtTime(j.created_at));
 return parts.join(' · ');
}
/* 阶段步骤条：排队 → 加载模型 → 生成 → 保存 → 完成 */
const STEP_ORDER=['waiting','loading','generating','saving','completed'];
function stepIndex(j){
 if(j.status==='queued')return 0;
 const s=j.stage;
 if(s==='loading'||s==='preflight')return 1;
 if(s==='generating')return 2;
 if(s==='saving'||s==='probing')return 3;
 return 4;
}
function stepsHTML(j){
 if(j.status!=='running'&&j.status!=='queued')return '';
 const idx=stepIndex(j);
 const labels=['排队/等待','加载模型','生成中','保存','完成'];
 let cells='';
 STEP_ORDER.forEach((s,i)=>{cells+='<div class="step '+(i<idx?'done':(i===idx&&j.status==='running'?'now':''))+'"></div>'});
 return '<div class="steps">'+cells+'</div><div class="steps-label">'+labels[idx]+'</div>';
}
async function cancelJob(j){
 if(!confirm('取消任务 '+j.id.slice(0,8)+'？运行中的任务会立即终止。'))return;
 try{await api('/api/jobs/'+j.id+'/cancel',{method:'POST'});toast('已请求取消','info');if(typeof onChanged==='function')onChanged()}catch(e){toast(e.message,'err')}
}
async function bumpJob(j){
 try{const d=await(await api('/api/jobs/'+j.id+'/bump',{method:'POST'})).json();toast('已插队：'+modelName(j)+' '+j.id.slice(0,8)+' 已标记为队首，列表顺序保持不变；内存不足时仍会等待','info');if(typeof onChanged==='function')onChanged()}catch(e){toast(e.message,'err')}
}
async function retryJob(j){
 try{
  const d=await(await api('/api/jobs/'+j.id+'/retry',{method:'POST'})).json();
  toast('已重试：'+modelName(j)+' 新任务 '+d.id.slice(0,8)+' 已入队（沿用原参数与输入图）','ok');
  if(typeof onRetried==='function')onRetried(d);
  if(typeof onChanged==='function')onChanged();
 }catch(e){toast(e.message,'err')}
}
/* 产物缩略图（图片走 blob；视频不预取，返回 null） */
async function thumbURL(j){
 if(j.status!=='succeeded'||!j.output_url)return null;
 if(j.parameters.kind==='video')return null;
 let u=blobCache.get(j.id);
 if(!u){
  try{u=URL.createObjectURL(await(await api(j.output_url)).blob());blobCache.set(j.id,u)}catch(e){return null}
 }
 return u;
}
async function download(j){
 try{
  const blob=await(await api(j.output_url)).blob();
  const a=document.createElement('a');
  a.href=URL.createObjectURL(blob);
  a.download=j.id+(j.parameters.kind==='image'?'.png':'.mp4');
  a.click();
  setTimeout(()=>URL.revokeObjectURL(a.href),30000);
 }catch(e){toast(e.message,'err')}
}
/* 日志弹窗（运行中自动 tail） */
let logJob=null,logTimer=null;
function logBodyAtBottom(){const b=$('logbody');return b.scrollHeight-b.scrollTop-b.clientHeight<40}
async function loadLog(){
 if(!logJob)return;
 try{
  const follow=$('log-follow').checked;
  const atBottom=logBodyAtBottom();
  const t=await(await api('/api/jobs/'+logJob.id+'/log')).text();
  const body=$('logbody');
  if(body.dataset.prev!==t)body.textContent=t;
  body.dataset.prev=t;
  if(follow&&atBottom)body.scrollTop=body.scrollHeight;
 }catch(e){}
}
async function openLog(j){
 logJob=j;
 $('log-title').textContent=j.id.slice(0,8)+' · '+(STATUS_ZH[j.status]||j.status);
 $('logmask').hidden=false;
 $('logbody').dataset.prev='';$('logbody').textContent='加载日志…';
 await loadLog();
 clearInterval(logTimer);
 logTimer=setInterval(async()=>{
  if($('logmask').hidden){clearInterval(logTimer);return}
  try{
   const d=await(await api('/api/jobs/'+logJob.id)).json();
   logJob=d;
   $('log-title').textContent=j.id.slice(0,8)+' · '+(STATUS_ZH[d.status]||d.status);
   if($('log-auto').checked&&(d.status==='running'||d.status==='queued'))loadLog();
  }catch(e){}
 },2500);
}
/* 详情弹窗：任务全部信息（参数/设备/进程/内存/耗时/错误/日志/结果原文） */
async function openDetail(j){
 $('det-title').textContent=j.id.slice(0,8)+' · '+(STATUS_ZH[j.status]||j.status);
 const body=$('detbody');body.replaceChildren();
 $('detmask').hidden=false;
 const h3=t=>{const h=document.createElement('h3');h.textContent=t;body.append(h)};
 const table=rows=>{
  const tbl=document.createElement('table');
  for(const [k,v] of rows){
   if(v==null||v==='')continue;
   const tr=document.createElement('tr');
   const th=document.createElement('th');th.textContent=k;
   const td=document.createElement('td');
   if(typeof v==='string'&&v.startsWith('<'))td.innerHTML=v;else td.textContent=v;
   tr.append(th,td);tbl.append(tr);
  }
  body.append(tbl);
 };
 const p=j.parameters||{},c=modelOf(j),cl=j.client||{};
 h3('基本信息');
 table([
  ['任务 ID',j.id],
  ['状态',STATUS_ZH[j.status]||j.status],
  ['类型',p.kind==='video'?'视频':'图片'],
  ['模型',modelName(j)],
  ['当前阶段',STAGE_ZH[j.stage]||j.stage||'–'],
  ['排队位次',j.queue_position?('第 '+j.queue_position+' 位'+(j.bumped?'（已插队）':'')):null],
  ['预计完成',etaText(j)],
  ['提交时间',fmtTime(j.created_at)],
  ['开始时间',fmtTime(j.started_at)],
  ['结束时间',fmtTime(j.finished_at)],
  ['总耗时',j.elapsed_seconds!=null?fmtT(j.elapsed_seconds):null],
 ]);
 h3('生成参数');
 const prows=[
  ['提示词',p.prompt],
  ['画布',p.width+'×'+p.height],
  ['步数',p.steps],
  ['随机种子',p.seed],
  ['引导系数',p.guidance==null?'模型默认':p.guidance],
 ];
 if(p.kind==='video'){
  prows.push(['时长',p.seconds+' 秒'],['精度',p.precision],['层数',p.layers],['复用',p.reuse],['首帧',p.first_frame_base64?'已提供（≤4MiB）':'无'],['参考图',p.ref_image_count?p.ref_image_count+' 张（Ref2VA）':'无']);
 }else{
  prows.push(['图生图',p.image_strength!=null?'启用，强度 '+p.image_strength:(j.has_input_image?'启用（模型默认强度）':'未启用')]);
 }
 table(prows);
 if(j.status==='succeeded'&&j.output_url){
  h3('产物预览（点击放大）');
  const dm=document.createElement('div');dm.className='detail-media';dm.textContent='加载产物中…';body.append(dm);
  (async()=>{
   try{
    let blob=blobCache.get(j.id);
    if(!blob){blob=await(await api(j.output_url)).blob();blobCache.set(j.id,blob)}
    const u=URL.createObjectURL(blob);dm.replaceChildren();
    const el=p.kind==='video'?document.createElement('video'):document.createElement('img');
    if(el.tagName==='VIDEO'){el.controls=true;el.preload='metadata'}
    el.src=u;el.onclick=()=>openLightbox(j);
    dm.append(el);
   }catch(e){dm.remove()}
  })();
  if(p.kind==='image'){
   try{
    const r=await api('/api/jobs/'+j.id+'/input-image');
    const u=URL.createObjectURL(await r.blob());
    const div=document.createElement('div');div.className='detail-inputimg';div.textContent='输入图（图生图）：';
    const im=document.createElement('img');im.src=u;im.onclick=()=>openLightbox({id:j.id+'_in',parameters:{kind:'image',width:p.width,height:p.height},output_url:u});
    div.append(im);body.append(div);
   }catch(e){}
  }
 }
 h3('提交设备');
 table([
  ['来源 IP',cl.ip||'未知（旧任务未记录）'],
  ['浏览器 UA',cl.user_agent||'未知（旧任务未记录）'],
 ]);
 h3('进程');
 table([
  ['运行 PID',j.pid||'尚未启动进程'],
  ['启动命令',j.pid?RUNNER_CMD(j.id):'（排队中，未启动）'],
  ['进程树峰值 RSS',j.process_tree_rss_peak_bytes?gb(j.process_tree_rss_peak_bytes):null],
  ['进程说明',j.pid?'独立进程运行，任务结束即释放模型':'–'],
 ]);
 h3('内存');
 const si=j.stage_info||{};
 table([
  ['启动门槛（需可用）',(c?c.available_gib_required:'–')+' GiB'],
  ['等待时可用',si.available_gib!=null?si.available_gib+' GiB':null],
  ['MLX 分配峰值',null],
 ]);
 try{
  const r=await(await api('/api/jobs/'+j.id+'/result')).json();
  h3('结果与耗时');
  const srows=[
   ['加载模型',r.load_seconds!=null?fmtT(r.load_seconds):null],
   ['生成',r.generation_seconds!=null?fmtT(r.generation_seconds):null],
   ['总耗时',r.total_seconds!=null?fmtT(r.total_seconds):null],
   ['MLX 分配峰值',r.mlx_peak_bytes?gb(r.mlx_peak_bytes):null],
   ['产物',r.file?r.file+'（点击行外“下载”获取）':null],
  ];
  if(r.ffprobe&&r.ffprobe.format)srows.push(['视频时长',r.ffprobe.format.duration+' 秒（ffprobe）']);
  if(r.note)srows.push(['口径说明',r.note]);
  if(r.memory_scope)srows.push(['内存口径',r.memory_scope]);
  table(srows);
  const det=document.createElement('details');
  const sum=document.createElement('summary');sum.textContent='result.json 原文';det.append(sum);
  const pre=document.createElement('pre');pre.textContent=JSON.stringify(r,null,2);det.append(pre);
  body.append(det);
 }catch(e){}
 if(j.error){
  h3('错误');
  const pre=document.createElement('pre');pre.style.background='#f3dcdc';pre.style.color='#8b2f2f';pre.textContent=j.error;body.append(pre);
 }
 h3('运行日志（最后 16KB）');
 const ldet=document.createElement('details');
 const lsum=document.createElement('summary');lsum.textContent='展开日志';ldet.append(lsum);
 const lpre=document.createElement('pre');lpre.className='logpre';lpre.textContent='加载中…';ldet.append(lpre);
 body.append(ldet);
 try{lpre.textContent=await(await api('/api/jobs/'+j.id+'/log')).text()}catch(e){lpre.textContent='暂无日志'}
}
/* 大图 lightbox：图片/视频点击放大 */
let lightboxEl=null;
function ensureLightbox(){
 if(lightboxEl)return lightboxEl;
 lightboxEl=document.createElement('div');
 lightboxEl.id='lightbox';lightboxEl.hidden=true;lightboxEl.innerHTML='<div id="lb-inner"></div>';
 document.body.append(lightboxEl);
 lightboxEl.onclick=e=>{if(e.target===lightboxEl)closeLightbox()};
 document.addEventListener('keydown',e=>{if(e.key==='Escape')closeLightbox()});
 return lightboxEl;
}
async function openLightbox(j){
 const lb=ensureLightbox();lb.hidden=false;
 const inner=lb.querySelector('#lb-inner');inner.replaceChildren();
 const tip=document.createElement('div');tip.className='lb-tip';tip.textContent='加载产物中…';inner.append(tip);
 try{
  let blob=blobCache.get(j.id);
  if(!blob){blob=await(await api(j.output_url)).blob();blobCache.set(j.id,blob)}
  const u=URL.createObjectURL(blob);
  inner.replaceChildren();
  const el=j.parameters.kind==='video'?document.createElement('video'):document.createElement('img');
  if(el.tagName==='VIDEO'){el.controls=true;el.autoplay=true}
  el.src=u;el.className='lb-media';
  inner.append(el);
  const cap=document.createElement('div');cap.className='lb-cap';
  cap.textContent=modelName(j)+' · '+j.id.slice(0,8)+' · '+j.parameters.width+'×'+j.parameters.height+' · 点击空白处或 Esc 关闭';
  inner.append(cap);
 }catch(e){tip.textContent='加载失败：'+e.message}
}
function closeLightbox(){
 if(!lightboxEl||lightboxEl.hidden)return;
 const v=lightboxEl.querySelector('video');if(v)v.pause();
 lightboxEl.hidden=true;lightboxEl.replaceChildren();
}
/* 内联媒体（成功任务）：图片可点击放大；视频带播放器可点击放大 */
async function inlineMedia(j){
 const wrap=document.createElement('div');wrap.className='inline-media';
 try{
  let blob=blobCache.get(j.id);
  if(!blob){blob=await(await api(j.output_url)).blob();blobCache.set(j.id,blob)}
  const u=URL.createObjectURL(blob);
  const el=j.parameters.kind==='video'?document.createElement('video'):document.createElement('img');
  if(el.tagName==='VIDEO'){el.controls=true;el.preload='metadata'}
  el.src=u;el.onclick=()=>openLightbox(j);
  wrap.append(el);
 }catch(e){wrap.remove()}
 return wrap;
}
function etaText(j){
 if(!j.eta)return null;
 if(j.eta.state==='running')return '还需 ~'+fmtT(j.eta.remaining_seconds)+'（'+(j.eta.history_samples?'按 '+j.eta.history_samples+' 次历史估算':'估算')+'）';
 if(j.eta.state==='starting')return '即将开始';
 if(j.eta.state==='waiting_memory')return '等待内存释放（时间不定）';
 return null;
}
/* 状态变化 toast（页面传入上次状态） */
function diffToasts(prevMap,data){
 for(const j of data){
  const prev=prevMap.get(j.id);
  if(prev&&prev!==j.status){
   if(j.status==='succeeded')toast('✅ '+modelName(j)+' '+j.id.slice(0,8)+' 生成完成，可预览/下载');
   else if(j.status==='failed')toast('❌ '+j.id.slice(0,8)+' 失败：'+(j.error||''),'err');
   else if(j.status==='cancelled')toast('⏹ '+j.id.slice(0,8)+' 已取消','info');
   else if(j.status==='interrupted')toast('⚠ '+j.id.slice(0,8)+' 因服务重启中断','err');
  }
  prevMap.set(j.id,j.status);
 }
}
function initSharedModals(){
 $('logmask').hidden=true;$('detmask').hidden=true;
 $('log-close').onclick=()=>{$('logmask').hidden=true;clearInterval(logTimer)};
 $('det-close').onclick=()=>$('detmask').hidden=true;
 $('logmask').onclick=e=>{if(e.target===$('logmask')){$('logmask').hidden=true;clearInterval(logTimer)}};
 $('detmask').onclick=e=>{if(e.target===$('detmask'))$('detmask').hidden=true};
 document.addEventListener('keydown',e=>{if(e.key==='Escape'){$('logmask').hidden=true;$('detmask').hidden=true;clearInterval(logTimer)}});
 setInterval(()=>{document.querySelectorAll('.job[data-started],.mrow[data-started]').forEach(el=>{
  const e2=el.querySelector('.elapsed');
  if(e2)e2.textContent=fmtT(Date.now()/1000-Number(el.dataset.started));
 })},1000);
}
