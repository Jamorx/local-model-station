'use strict';
const $=id=>document.getElementById(id), esc=v=>String(v??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const labels={queued:'排队中',paused:'已暂停',running:'运行中',succeeded:'已完成',failed:'失败',cancelled:'已取消',interrupted:'已中断'};
const views={
 overview:['工作总览','查看近期任务和本机的执行资源。'],
 tasks:['任务队列','筛选任务、调整顺序和查看产物。'],
 models:['模型与能力','按类型或引擎查找可用模型。'],
 splash:['Splash 管理','查看模型状态、用量与近期请求。'],
 downloads:['模型下载','搜索仓库，管理下载进度与续传。']
};
const requestedView=()=>Object.hasOwn(views,location.hash.slice(1))?location.hash.slice(1):location.pathname==='/manage'?'tasks':'overview';
let queueState=null,jobs=[],models=[],health=null,view=requestedView(),busy=false,selected=new Set(),lastSync=null;
const badge=j=>`<span class="status ${esc(j.status)}">${esc(j.cancel_requested?'取消中':j.pause_requested&&j.status==='running'?'暂停中':j.status==='paused'&&j.paused_from==='running'&&!j.pause_requested?'恢复中':labels[j.status]||j.status)}</span>`;
const date=t=>t?new Date(t*1000).toLocaleString('zh-CN',{month:'2-digit',day:'2-digit',hour:'2-digit',minute:'2-digit',hour12:false}):'—';
const engineLabel=id=>id==='h3-c'?'h3.c':id;
const modelName=j=>{const m=j.execution?.profile||models.find(m=>m.id===j.parameters?.model);return m?`${m.name||m.model_id} · ${engineLabel(m.engine)}`:j.parameters?.model||'未知模型'};
function toast(msg){const t=document.createElement('div');t.className='toast show';t.textContent=msg;$('toasts').append(t);setTimeout(()=>t.remove(),5000)}
async function api(path,body,key){const r=await fetch(path,{cache:'no-store',...(body!==undefined?{method:'POST',headers:{'Content-Type':'application/json',...(key?{'Idempotency-Key':key}:{})},body:JSON.stringify(body)}:{})});if(!r.ok){let d;try{d=await r.json()}catch{throw Error(`请求失败 (${r.status})`)}throw Error(typeof d.detail==='string'?d.detail:JSON.stringify(d.detail||d))}return r}
function showView(v,{navigate=false}={}){
 view=v;
 for(const name of Object.keys(views))$(name+'-view').hidden=name!==v;
 for(const button of document.querySelectorAll('nav [data-view]')){
  const selected=button.dataset.view===v;
  button.classList.toggle('active',selected);
  if(selected)button.setAttribute('aria-current','page');else button.removeAttribute('aria-current');
 }
 const [title,description]=views[v];
 $('crumb').textContent=title;$('page-title').textContent=title;$('page-desc').textContent=description;
 document.title=title+' · Local Model Station';
 if(v==='downloads'||v==='splash')requestAnimationFrame(redrawVisibleGrids);
 if(navigate&&requestedView()!==v){history.pushState(null,'','#'+v);window.scrollTo(0,0)}
}
function memoryNote(j){const required=models.find(m=>m.id===j.parameters?.model)?.available_gib_required;if(!required)return '';const available=Number(health?.available_gib||0),gap=Math.max(0,required-available);return `需 ${required} GiB · 可用 ${available.toFixed(1)} GiB${j.status==='queued'?(health?.execution_enabled===false?' · 执行暂停':gap>0?` · 等待释放 ${gap.toFixed(1)} GiB`:' · 等待调度'):''}`}
function memoryPanel(){if(!health)return;const h=health;const percent=h.memory_percent;$('memory-available').textContent=`可用内存 ${h.available_gib} GiB`;$('memory-percent').textContent=percent==null?'—':percent+'%';$('memory-fill').style.width=(percent??0)+'%';$('memory-gauge').setAttribute('aria-valuenow',String(percent??0));$('telemetry').title=`内存使用率 ${percent??'—'}% · 可用 ${h.available_gib} GiB · 物理内存 ${h.total_gib} GiB · 交换占用 ${h.swap_used_gib??'—'} GiB`;const m=models.find(m=>m.id===$('form-model').value);if(m){const gap=Math.max(0,m.available_gib_required-h.available_gib);$('model-hint').textContent=`所需可用内存 ${m.available_gib_required} GiB / 当前 ${h.available_gib} GiB / ${gap>0?'缺口 '+gap.toFixed(1)+' GiB，提交后等待内存':'内存满足启动条件'}`}}
function render(){const count=s=>jobs.filter(j=>j.status===s).length;$('nav-count').textContent=count('queued')+count('running')+count('paused');$('metrics').innerHTML=[['全部任务',jobs.length,'包含所有历史记录','▤'],['等待执行',count('queued'),'等待执行许可或资源释放','◷'],['正在运行',count('running'),'当前占用执行资源的任务','↗'],['已完成',count('succeeded'),'生成成功，可查看与下载','✓']].map(([name,n,hint,icon])=>`<article class="card metric"><div class="metric-top">${name}<span class="metric-icon">${icon}</span></div><strong>${n}</strong><small>${hint}</small></article>`).join('');
if(health){const enabled=queueState?.execution_enabled&&!queueState.paused;$('memory').textContent=Number(health.available_gib).toFixed(1);$('total-memory').textContent=health.total_gib?Number(health.total_gib).toFixed(0)+' GiB':'未报告';$('memory-bar').style.width=health.total_gib?Math.max(0,Math.min(100,health.available_gib/health.total_gib*100))+'%':'0%';$('submit-note').textContent=enabled?'按队列依次执行':'仅加入队列，生成暂不执行'}
$('model-count').textContent=models.filter(m=>m.available).length+' / '+models.length;
$('recent').innerHTML=jobs.length?jobs.slice(0,5).map(j=>`<div class="recent-row" role="button" tabindex="0" data-detail="${j.id}"><span class="type-icon">${j.parameters?.kind==='video'?'▷':'▧'}</span><div class="recent-title"><b>${esc(j.parameters?.prompt||'未命名任务')}</b><small>${esc(modelName(j))} · ${date(j.created_at)}</small></div><div>${badge(j)}<small class="memory-note">${esc(memoryNote(j))}</small></div></div>`).join(''):'<div class="empty">提交第一个本地生成任务。<p>图片、视频与音频任务共用队列，进度会显示在这里。</p><button class="btn btn-primary primary new-task">＋ 创建第一个任务</button></div>';
renderCatalog();memoryPanel();renderQueue();renderTable()}
const kindNames={image:'图片',video:'视频',audio:'音频'};
function renderCatalog(){
 const query=$('catalog-query').value.trim().toLowerCase(), kind=$('catalog-kind').value;
 const visible=models.filter(m=>(!kind||m.kind===kind)&&[m.name,m.model_id,m.engine,m.id].some(value=>String(value??'').toLowerCase().includes(query)));
 $('catalog-count').textContent=`${visible.length} 个模型配置`;
 const markup=visible.map(m=>`<article class="card model-card catalog-card">
  <div class="catalog-meta"><span class="chip">${esc(kindNames[m.kind]||m.kind)}</span><span class="chip ${m.available?'chip-ready':''}">${m.available?'依赖可用':'依赖缺失'}</span></div>
  <h2>${esc(m.name||m.model_id)}</h2><p class="engine-tag">${esc(m.model_id)}</p>
  <dl><div><dt>引擎</dt><dd>${esc(engineLabel(m.engine))}</dd></div><div><dt>Profile</dt><dd>${esc(m.id)}</dd></div><div><dt>可用内存要求</dt><dd>${esc(m.available_gib_required)} GiB</dd></div></dl>
  <button class="btn btn-outline-secondary" data-model="${esc(m.id)}" ${m.available?'':'disabled'}>使用此模型 <span aria-hidden="true">→</span></button>
 </article>`).join('')||'<div class="card panel empty">没有匹配的模型配置。</div>';
 if($('models').innerHTML!==markup&&!$('models').contains(document.activeElement))$('models').innerHTML=markup;
}
$('catalog-query').oninput=renderCatalog;$('catalog-kind').onchange=renderCatalog;
function filtered(){const q=$('search').value.toLowerCase();const list=jobs.filter(j=>(!$('status-filter').value||j.status===$('status-filter').value)&&(!$('kind-filter').value||j.parameters?.kind===$('kind-filter').value)&&(!q||[j.id,j.parameters?.prompt,modelName(j)].join(' ').toLowerCase().includes(q)));if($('sort-order').value==='queue')list.sort((a,b)=>{const rank=j=>j.status==='running'||(j.status==='paused'&&j.paused_from==='running')?0:j.status==='queued'?1:j.status==='paused'?2:3;return rank(a)-rank(b)||(a.queue_position??1e9)-(b.queue_position??1e9)||b.created_at-a.created_at});return list}
function operationButtons(j){const c=j.controls||{};return `${j.status==='running'&&!c.process_pause_supported?`<button class="btn btn-outline-secondary" disabled title="${esc(c.pause_note)}">暂停</button>`:''}${c.pause&&!j.pause_requested?`<button class="btn btn-outline-secondary" data-action="pause" data-id="${j.id}" title="${esc(j.status==='running'?c.pause_note:'暂停等待，不占用执行资源')}">暂停</button>`:''}${c.resume?`<button class="btn btn-outline-secondary" data-action="resume" data-id="${j.id}">恢复</button>`:''}${c.reorder?`<button class="btn btn-outline-secondary" data-action="bump" data-id="${j.id}" title="移到队首；不抢占当前运行任务">置顶</button><details class="row-more"><summary>排序</summary><div><button class="btn btn-outline-secondary" data-action="up" data-id="${j.id}">上移</button><button class="btn btn-outline-secondary" data-action="down" data-id="${j.id}">下移</button><button class="btn btn-outline-secondary" data-action="defer" data-id="${j.id}">移到队尾</button></div></details>`:''}${c.cancel?`<button class="btn btn-outline-secondary" data-action="cancel" data-id="${j.id}" ${j.cancel_requested?'disabled':''}>取消</button>`:''}`}
let queueConnected=false, queueChanging=false, queueRevision=0;
function renderQueue(){
 const q=queueState, enabled=Boolean(q?.execution_enabled&&!q.paused);
 const toggle=$('notice');
 toggle.setAttribute('aria-checked',String(enabled));toggle.classList.toggle('online',enabled);
 toggle.disabled=!queueConnected||!q?.execution_enabled||queueChanging;
 toggle.textContent=queueChanging?'切换中':!queueConnected?'未连接':!q?.execution_enabled?'执行未启用':q.paused?'队列暂停':'队列启用';
 toggle.title=enabled?'暂停后续调度，当前任务继续':'恢复后续调度';
 $('queue-toggle').disabled=toggle.disabled;
 if(!q)return;
 $('queue-toggle').textContent=q.paused?'恢复后续调度':'暂停后续调度';
 $('queue-summary').textContent=`运行 ${q.counts.running} · 排队 ${q.counts.queued} · 暂停 ${q.counts.paused}`;$('queue-explanation').textContent=q.paused?'后续调度已暂停，当前运行任务继续。':!q.execution_enabled?'预览模式：执行未启用，恢复调度也不会启动模型。':'按队列和内存条件运行；置顶不会抢占当前任务。'}
function renderTable(){const list=filtered();selected=new Set([...selected].filter(id=>jobs.some(j=>j.id===id&&['queued','running','paused'].includes(j.status))));$('jobs-body').innerHTML=list.map(j=>`<tr><td><input class="form-check-input" type="checkbox" data-select="${j.id}" aria-label="选择任务 ${j.id.slice(0,8)}" ${selected.has(j.id)?'checked':''} ${['queued','running','paused'].includes(j.status)?'':'disabled'}></td><td><div class="task-title">${esc(j.parameters?.prompt||'未命名任务')}</div><small>${esc(projectLabel(j))}</small><br><small>${j.id.slice(0,12)} · ${j.parameters?.width||'—'} × ${j.parameters?.height||'—'}</small></td><td>${esc(modelName(j))}</td><td>${badge(j)}<small>${j.queue_position?'队列 #'+j.queue_position:j.status==='paused'?(j.paused_from==='running'?'挂起中 · 内存保留':'等待手动恢复'):''}</small><small class="memory-note">${esc(memoryNote(j))}</small></td><td>${date(j.created_at)}</td><td><div class="task-actions"><button class="btn btn-outline-secondary" data-detail="${j.id}">详情</button>${operationButtons(j)}<button class="btn btn-outline-secondary" data-action="retry" data-id="${j.id}">${['failed','cancelled','interrupted'].includes(j.status)?'编辑重试':'复制编辑'}</button></div></td></tr>`).join('');$('tasks-empty').hidden=!!list.length;$('table-count').textContent=`显示 ${list.length} 项 · 共加载 ${jobs.length} 条记录`;$('bulkbar').hidden=!selected.size;$('selection-count').textContent=`已选 ${selected.size} 项`;
 const selectable=list.filter(j=>['queued','running','paused'].includes(j.status));$('select-all').checked=!!selectable.length&&selectable.every(j=>selected.has(j.id));$('select-all').indeterminate=selectable.some(j=>selected.has(j.id))&&!$('select-all').checked;
 for(const action of ['pause','resume','cancel','bump','defer'])$('bulk-'+action).disabled=![...selected].some(id=>{const j=jobs.find(j=>j.id===id);return j?.controls?.[['bump','defer'].includes(action)?'reorder':action]});
}
async function refresh(){if(busy)return;const revision=queueRevision;busy=true;$('refresh').disabled=true;try{const [h,m,j,q]=await Promise.all(['/health','/api/models','/api/jobs','/api/queue'].map(async p=>(await api(p)).json()));health=h;if(!queueChanging&&revision===queueRevision)queueState=q;models=m.data;jobs=j.data.sort((a,b)=>b.created_at-a.created_at);lastSync=new Date();$('updated').textContent='更新于 '+lastSync.toLocaleTimeString('zh-CN',{hour12:false});queueConnected=true;render()}catch(e){queueConnected=false;renderQueue();$('updated').textContent=lastSync?'数据可能过期 · '+lastSync.toLocaleTimeString():'尚未获取数据';}finally{busy=false;$('refresh').disabled=false}}
function modelOptions(id){const kind=$('form-kind').value;$('form-model').innerHTML=models.filter(m=>m.kind===kind).map(m=>`<option value="${m.id}" ${m.available?'':'disabled'}>${esc(m.name)} · ${esc(engineLabel(m.engine))} · ${esc(m.id)}${m.available?'':'（依赖缺失）'}</option>`).join('');if(id&&models.some(m=>m.id===id&&m.kind===kind))$('form-model').value=id;else if(kind==='image'&&models.some(m=>m.id==='zimage'))$('form-model').value='zimage';updateModel()}
let editDraft=null, editAssets=[];
const roleLabels={first_frame:'首帧',last_frame:'尾帧',input_image:'输入图片',edit_image:'编辑参考图',reference_image:'参考图片',reference_audio:'参考音频',audio_condition:'音频条件',reference_video:'参考视频',draft:'底片视频'};
const primaryParams=['width','height','seed','steps','seconds','frames'];
function updateModel(){
 const select=$('form-model');
 $('model-choices').innerHTML=[...select.options].map(option=>{
  const model=models.find(m=>m.id===option.value);
  return `<button class="btn btn-outline-secondary" type="button" data-model-choice="${esc(option.value)}" aria-pressed="${option.selected}" ${option.disabled?'disabled':''}><strong>${esc(model?.name||option.textContent)}</strong>${model?`<small>${esc(engineLabel(model.engine))} · ${esc(model.id)}${option.disabled?' · 依赖缺失':''}</small>`:''}</button>`;
 }).join('');
 const m=models.find(m=>m.id===select.value);if(!m)return;
 $('form-steps').value=m.steps||9;$('seconds-label').hidden=m.kind!=='video';$('frames-label').hidden=m.backend!=='vpipe-2k';
 $('upload-label').hidden=true;$('form-file').required=false;
 $('form-width').max=$('form-height').max=m.backend==='vpipe-2k'?2560:1536;
 $('asset-role').innerHTML=(m.input_roles||[]).map(role=>`<option value="${role}">${esc(roleLabels[role])}</option>`).join('');
 $('asset-upload').disabled=!(m.input_roles||[]).length;
 $('engine-description').textContent=`模型 ${m.model_id} / 引擎 ${engineLabel(m.engine)} / 配置 ${m.id}。切换引擎后会重新校验全部输入；已有素材不会自动删除。`;
 renderEditAssets();memoryPanel();
}
function applyDraft(draft){
 editDraft=structuredClone(draft);editAssets=structuredClone(draft.inputs.assets||[]);
 $('form-kind').value=draft.kind;modelOptions(draft.profile);
 if(!models.some(m=>m.id===draft.profile)){const opt=new Option(draft.profile+'（历史配置已移除，请选择现有配置）',draft.profile);$('form-model').add(opt);$('form-model').value=draft.profile;updateModel();}
 $('form-prompt').value=draft.inputs.prompt;
 for(const key of primaryParams)$('form-'+key).value=draft.parameters[key]??({width:1024,height:1024,seed:42,steps:9,seconds:5,frames:22}[key]);
 $('form-options').value=JSON.stringify(draft.options||{},null,2);
 $('form-extra').value=JSON.stringify(Object.fromEntries(Object.entries(draft.parameters).filter(([k])=>!primaryParams.includes(k))),null,2);
 $('form-metadata').value=JSON.stringify(draft.metadata||{},null,2);
 renderEditAssets();
}
let editMode='simple';
const simpleProfiles={image:['zimage','flux3','moss'],video:['h3','vpipe-h3'],audio:['qwen3-tts']};
const promptExamples={image:['描述想生成的图片','例如：雨后的小巷，路灯倒映在积水里。'],video:['描述画面和动作','例如：一只猫跃上窗台，镜头缓慢推近。'],audio:['输入要朗读的文字','例如：欢迎收听今天的节目。']};
function simpleModel(kind){
 return simpleProfiles[kind]?.map(id=>models.find(m=>m.id===id&&m.kind===kind&&m.available)).find(Boolean);
}
function updateEditor(){
 const kind=$('form-kind').value;
 for(const button of $('kind-choices').querySelectorAll('button'))button.setAttribute('aria-pressed',String(button.dataset.kind===kind));
 const advanced=editMode==='advanced', model=simpleModel(kind);
 $('advanced-fields').hidden=!advanced;$('advanced-fields').disabled=!advanced;
 for(const button of document.querySelectorAll('[data-edit-mode]'))button.setAttribute('aria-pressed',String(button.dataset.editMode===editMode));
 $('prompt-label').textContent=advanced?'提示词 / 文本':promptExamples[kind][0];
 $('form-prompt').placeholder=promptExamples[kind][1];
 $('simple-profile-note').hidden=advanced;
 $('simple-profile-note').textContent=model?`自动使用 ${model.name||model.model_id}`:'当前类型没有可用的简单模式模型，可在复杂编辑中检查配置。';
}
function setEditMode(mode){
 editMode=mode;$('create-dialog').dataset.mode=mode;updateEditor();
}
$('kind-choices').onclick=e=>{
 const button=e.target.closest('[data-kind]');
 if(!button||$('form-kind').value===button.dataset.kind)return;
 $('form-kind').value=button.dataset.kind;$('form-kind').dispatchEvent(new Event('change'));
};
$('model-choices').onclick=e=>{
 const button=e.target.closest('[data-model-choice]');
 if(!button||button.disabled||$('form-model').value===button.dataset.modelChoice)return;
 $('form-model').value=button.dataset.modelChoice;updateModel();
 [...$('model-choices').querySelectorAll('button')].find(item=>item.dataset.modelChoice===button.dataset.modelChoice)?.focus();
};
for(const button of document.querySelectorAll('[data-edit-mode]'))button.onclick=()=>setEditMode(button.dataset.editMode);
function create(id){
 if(!models.length){toast('模型列表尚未加载，请先刷新');return}
 const m=models.find(m=>m.id===(id||'zimage'))||models[0];editAssets=[];editDraft=null;
 let prompt='';try{prompt=localStorage.getItem('frame-prompt-draft')||''}catch{}
 applyDraft({schema_version:'gateway.task.v1',profile:m.id,kind:m.kind,inputs:{prompt,assets:[]},parameters:{width:m.kind==='video'?768:1024,height:m.kind==='video'?768:1024,seed:42,steps:m.steps||9,seconds:5},options:{},metadata:{}});
 $('form-error').textContent='';submitAttempt=null;setEditMode(id?'advanced':'simple');$('create-dialog').showModal();$('form-prompt').focus();
}
async function editRetry(id){
 try{const draft=await(await api(`/api/v1/tasks/${id}/draft`)).json();applyDraft(draft);$('form-error').textContent='';submitAttempt=null;$('detail-dialog').close();setEditMode('advanced');$('create-dialog').showModal();$('form-prompt').focus();toast('已载入全部参数与参考素材。编辑后将创建新任务，原任务保持不变。')}catch(e){toast(e.message)}
}
function assetKind(role){return ['draft','reference_video'].includes(role)?'video':['reference_audio','audio_condition'].includes(role)?'audio':'image'}
function preview(kind,url){return kind==='image'?`<a href="${esc(url)}" target="_blank" rel="noopener"><img loading="lazy" src="${esc(url)}" alt="参考图片"></a>`:`<${kind} controls preload="metadata" src="${esc(url)}"></${kind}>`}
function renderEditAssets(){
 const roles=models.find(m=>m.id===$('form-model').value)?.input_roles||[];
 $('edit-assets').innerHTML=editAssets.map((asset,i)=>{const url=asset._preview||`/api/v1/tasks/${asset.source.task_id}/assets/${encodeURIComponent(asset.source.asset_id)}`;return `<article class="asset-card"><b>${esc(roleLabels[asset.role])}</b>${preview(assetKind(asset.role),url)}<small>${esc(asset._name||asset.source?.asset_id||'新素材')}</small>${roles.includes(asset.role)?'':'<p class="form-error">所选引擎不支持此素材角色，请移除或换引擎。</p>'}<button class="btn btn-outline-secondary" type="button" data-remove-asset="${i}">移除</button></article>`}).join('')||'<p class="muted">没有参考素材。</p>';
}
$('edit-assets').onclick=e=>{const b=e.target.closest('[data-remove-asset]');if(b){editAssets.splice(Number(b.dataset.removeAsset),1);renderEditAssets()}};
$('asset-upload').onchange=async e=>{try{const role=$('asset-role').value;if(!role)throw Error('请选择支持的素材角色');for(const file of e.target.files){const kind=assetKind(role),max=kind==='video'?32:kind==='audio'?6:4;if(file.size>max*1024*1024)throw Error(`${file.name} 超过 ${max} MiB`);const data=await file64(file);editAssets.push({role,data_base64:data,_preview:`data:${file.type};base64,${data}`,_name:file.name})}renderEditAssets()}catch(err){toast(err.message)}finally{e.target.value=''}};
const retryKeys=new Map();let submitAttempt=null;
const newRequestKey=()=>Array.from(crypto.getRandomValues(new Uint8Array(24)),b=>b.toString(16).padStart(2,'0')).join('');
let actionBusy=new Set();
async function action(id,type){if(type==='retry'){await editRetry(id);return}if(actionBusy.has(id))return;if(type==='cancel'&&!confirm('取消此任务？已生成的进度不能恢复，但输入会保留，可编辑重试。'))return;actionBusy.add(id);try{await api(`/api/jobs/${id}/${type}`,{});toast({pause:'暂停请求已提交',resume:'恢复请求已提交',cancel:'取消请求已提交',bump:'已移到队首',defer:'已移到队尾',up:'已上移',down:'已下移'}[type]);await refresh();if($('detail-dialog').open)await details(id)}catch(e){toast(e.message)}finally{actionBusy.delete(id)}}
async function toggleQueue(){
 if(!queueConnected||!queueState?.execution_enabled||queueChanging)return;
 queueChanging=true;queueRevision++;renderQueue();
 try{
  queueState=await(await api('/api/queue/control',{action:queueState.paused?'resume':'pause'})).json();
  if(health)health.queue_paused=queueState.paused;
  render();
  toast(queueState.paused?'后续调度已暂停，当前任务继续':'后续调度已恢复');
 }catch(error){toast(error.message)}
 finally{queueChanging=false;renderQueue()}
}
$('queue-toggle').onclick=toggleQueue;$('notice').onclick=toggleQueue;
async function bulk(action){const supported=[...selected].filter(id=>{const j=jobs.find(j=>j.id===id);return j?.controls?.[['bump','defer'].includes(action)?'reorder':action]});if(!supported.length)return;if(action==='cancel'&&!confirm(`取消选中的 ${supported.length} 个任务？输入会保留，但已生成进度不能恢复。`))return;try{const data=await(await api('/api/queue/batch',{action,ids:supported})).json();const failed=data.results.filter(r=>!r.ok);data.results.filter(r=>r.ok).forEach(r=>selected.delete(r.id));$('batch-result').textContent=`处理成功 ${data.results.length-failed.length} 项${failed.length?'；失败：'+failed.map(r=>r.id.slice(0,8)+' '+r.error).join('；'):''}${selected.size?'；未处理 / 不支持的选择仍保留':''}`;await refresh()}catch(e){toast(e.message)}}
const duration=s=>s==null?'未记录':s<60?s.toFixed(2)+' 秒':`${Math.floor(s/60)} 分 ${(s%60).toFixed(1)} 秒`;
const bytes=n=>n==null?'未记录':n>=1024**3?(n/1024**3).toFixed(2)+' GiB':(n/1024**2).toFixed(2)+' MiB';
async function details(id){
 try{
 const task=await(await api(`/api/v1/tasks/${id}`)).json(),m=task.metrics,t=task.timestamps;
 const cells=[['任务 ID',id],['模型',task.selection.model_id||'历史未记录'],['引擎',engineLabel(task.selection.engine_id)||'历史未记录'],['配置',task.profile],['创建',date(t.created_at)],['开始',date(t.started_at)],['结束',date(t.finished_at)],['排队耗时',duration(m.queue_seconds)],['执行经过时间',duration(m.execution_seconds)],['有效执行时间',duration(m.active_execution_seconds)],['运行中暂停时长',duration(m.execution_paused_seconds)],['总耗时',duration(m.total_seconds)],['进程树 RSS 峰值',bytes(m.process_tree_rss_peak_bytes)],['MLX 分配峰值',bytes(m.mlx_peak_bytes??task.result?.metrics?.mlx_peak_bytes)],['PID',task.pid??'未记录'],['来源任务',task.derived_from||'独立提交']];
 $('detail-body').innerHTML=`<div class="detail-actions">${badge(task)}<button class="btn btn-primary primary" id="edit-retry">重试 / 编辑后提交</button><button class="btn btn-outline-secondary" id="detail-refresh">刷新详情</button>${operationButtons(task)}</div><div class="detail-grid">${cells.map(([label,value])=>`<div><span>${esc(label)}</span>${esc(value)}</div>`).join('')}</div><p class="muted">${esc(m.memory_note||'该任务没有进程峰值记录。')} RSS 包含共享页，不等于独占 Metal 显存。</p><p class="muted">${esc(task.controls?.pause_note||'')}</p><h3>输入提示词</h3><p class="detail-prompt">${esc(task.inputs.prompt)}</p><h3>全部参考素材（${task.inputs.assets.length}）</h3><div class="asset-grid">${task.inputs.assets.map(a=>`<article class="asset-card"><b>${esc(roleLabels[a.role])}</b>${preview(a.kind,a.url)}<a href="${esc(a.url)}" download>${esc(a.id)}</a><small>${bytes(a.bytes)}${a.width?' · '+a.width+' × '+a.height:''}</small><details><summary>素材信息</summary><pre>${esc(JSON.stringify(a,null,2))}</pre></details></article>`).join('')||'<p class="muted">无参考素材</p>'}</div><h3>生成参数</h3><pre>${esc(JSON.stringify({parameters:task.parameters,options:task.options,metadata:task.metadata},null,2))}</pre><h3>进度与错误</h3><pre>${esc(JSON.stringify({progress:task.progress,error:task.error},null,2))}</pre>${task.status==='succeeded'&&task.result?`<h3>生成结果</h3><div class="asset-grid">${task.result.artifacts.map(a=>`<div class="asset-card">${preview(a.kind,a.url)}<a href="${esc(a.url)}" download>${esc(a.path)}</a></div>`).join('')}</div>`:''}<details><summary>结果与性能数据</summary><pre>${esc(JSON.stringify(task.result,null,2))}</pre></details><details><summary>执行快照与调用来源</summary><pre>${esc(JSON.stringify({execution:task.execution,client:task.client},null,2))}</pre></details><h3>执行日志</h3><pre id="task-log">正在读取…</pre>`;
 if(!$('detail-dialog').open)$('detail-dialog').showModal();$('edit-retry').onclick=()=>editRetry(id);$('detail-refresh').onclick=()=>details(id);
 const text=await(await api(task.log_url)).text();if($('task-log'))$('task-log').textContent=text||'暂无日志';
 }catch(e){toast(e.message)}
}
async function file64(file){return new Promise((resolve,reject)=>{const r=new FileReader();r.onload=()=>resolve(r.result.split(',')[1]);r.onerror=()=>reject(Error('读取素材失败'));r.readAsDataURL(file)})}
$('create-form').onsubmit=async e=>{
 e.preventDefault();const button=$('submit-button');if(button.disabled)return;button.disabled=true;button.textContent='提交中…';$('form-error').textContent='';
 try{
 const kind=$('form-kind').value,prompt=$('form-prompt').value.trim();
 if(!prompt)throw Error('请输入要生成的内容');
 let body;
 if(editMode==='simple'){
  const model=simpleModel(kind);
  if(!model)throw Error('当前类型没有可用的简单模式模型');
  body={schema_version:'gateway.task.v1',profile:model.id,kind,inputs:{prompt,assets:[]},
   parameters:kind==='video'?{width:768,height:768,seconds:5}:{},options:{},metadata:{}};
 }else{
 const profile=$('form-model').value,m=models.find(m=>m.id===profile);if(!m)throw Error('请选择当前存在的模型 / 引擎配置');
 const parameters=JSON.parse($('form-extra').value||'{}');
 if(!parameters||Array.isArray(parameters)||typeof parameters!=='object')throw Error('高级参数必须是 JSON 对象');
 for(const key of ['width','height','seed','steps'])parameters[key]=Number($('form-'+key).value);
 // Preserve every original parameter, including null values, unless explicitly edited.
 if(m.kind==='video')parameters.seconds=Number($('form-seconds').value);else if(editDraft?.parameters?.seconds!==undefined)parameters.seconds=editDraft.parameters.seconds;
 if(m.backend==='vpipe-2k')parameters.frames=Number($('form-frames').value);else if(editDraft?.parameters?.frames!==undefined)parameters.frames=editDraft.parameters.frames;
 body={schema_version:'gateway.task.v1',profile,kind:m.kind,inputs:{prompt:$('form-prompt').value,assets:editAssets.map(a=>({role:a.role,...(a.source?{source:a.source}:{data_base64:a.data_base64})}))},parameters,options:JSON.parse($('form-options').value||'{}'),metadata:JSON.parse($('form-metadata').value||'{}'),derived_from:editDraft?.derived_from||null};
 }
 const encoded=JSON.stringify(body);if(!submitAttempt||submitAttempt.encoded!==encoded)submitAttempt={encoded,key:newRequestKey()};
 await api('/api/v1/tasks',body,submitAttempt.key);submitAttempt=null;$('create-dialog').close();toast('新任务已加入队列，全部输入已保存');showView('tasks',{navigate:true});await refresh();
 }catch(e){$('form-error').textContent=e.message}finally{button.disabled=false;button.textContent='加入队列'}
};
document.addEventListener('click',e=>{const v=e.target.closest('[data-view]');if(v){showView(v.dataset.view,{navigate:true});refreshSplash();refreshDownloads()}if(e.target.closest('.new-task'))create();const m=e.target.closest('[data-model]');if(m)create(m.dataset.model);const d=e.target.closest('[data-detail]');if(d)details(d.dataset.detail);const a=e.target.closest('[data-action]');if(a)action(a.dataset.id,a.dataset.action);if(e.target.closest('[data-close]'))e.target.closest('dialog').close()});
document.addEventListener('keydown',e=>{if((e.key==='Enter'||e.key===' ')&&e.target.matches('.recent-row')){e.preventDefault();details(e.target.dataset.detail)}});
$('jobs-body').onchange=e=>{if(e.target.dataset.select){e.target.checked?selected.add(e.target.dataset.select):selected.delete(e.target.dataset.select);renderTable()}};$('select-all').onchange=e=>{filtered().filter(j=>['queued','running','paused'].includes(j.status)).forEach(j=>e.target.checked?selected.add(j.id):selected.delete(j.id));renderTable()};$('select-visible').onclick=()=>{filtered().filter(j=>['queued','running','paused'].includes(j.status)).forEach(j=>selected.add(j.id));renderTable()};$('clear-selection').onclick=()=>{selected.clear();renderTable()};
for(const action of ['pause','resume','cancel','bump','defer'])$('bulk-'+action).onclick=()=>bulk(action);
for(const id of ['search','status-filter','kind-filter','sort-order'])$(id).addEventListener('input',renderTable);$('refresh').onclick=()=>{refresh();refreshSplash();refreshDownloads()};$('form-kind').onchange=()=>{const size=$('form-kind').value==='video'?768:1024;$('form-width').value=size;$('form-height').value=size;modelOptions();updateEditor()};$('form-model').onchange=updateModel;$('form-prompt').oninput=()=>{try{localStorage.setItem('frame-prompt-draft',$('form-prompt').value)}catch{}};
let splashLoading=false;
const tokenCount=value=>value==null?'—':Number(value).toLocaleString('zh-CN');
const tokenRate=value=>value==null?'—':Number(value).toFixed(1);
const dataGrids=new Map();
function redrawVisibleGrids(){
 for(const [id,state] of dataGrids){
  if(state.built&&$(id).getClientRects().length)state.table.redraw(true);
 }
}
window.addEventListener('resize',()=>requestAnimationFrame(redrawVisibleGrids));
function renderDataGrid(id,headings,rows,empty,{paginate=false}={}){
 const target=$(id), signature=JSON.stringify([rows,empty]);
 const previous=dataGrids.get(id);
 if(previous?.signature===signature)return;
 if(!rows.length){
  previous?.table.destroy();
  dataGrids.delete(id);target.textContent=empty;
  return;
 }
 const data=rows.map((row,index)=>Object.assign({},row,{rowIndex:index}));
 if(previous){
  previous.signature=signature;
  const replace=async()=>{
   const page=paginate?previous.table.getPage():null;
   await previous.table.replaceData(data);
   if(page&&previous.table.getPage()!==page)await previous.table.setPage(Math.min(page,previous.table.getPageMax()));
  };
  if(previous.built)replace();else previous.table.on('tableBuilt',replace);
  return;
 }
 target.textContent='';target.classList.add('gateway-grid');
 const columns=headings.map((title,index)=>({
  title,field:String(index),minWidth:index===0?132:104,responsive:index===0?0:1,
  widthGrow:index===0?2:1,formatter:'textarea',headerSort:paginate,
  ...(paginate&&index===2?{sorter:'number'}:{})
 }));
 const table=new Tabulator(target,{
  index:'rowIndex', data, columns, layout:'fitColumns', reactiveData:false,
  responsiveLayout:'collapse', responsiveLayoutCollapseStartOpen:true,
  responsiveLayoutCollapseUseFormatters:false,
  responsiveLayoutCollapseFormatter(fields){
   if(!fields.length)return '';
   const details=document.createElement('dl');details.className='grid-row-details';
   for(const {title,value} of fields){
    const label=document.createElement('dt'), text=document.createElement('dd');
    label.textContent=title;text.textContent=value??'—';details.append(label,text);
   }
   return details;
  },
  ...(paginate?{
   pagination:true,paginationSize:8,locale:'zh-cn',
   langs:{'zh-cn':{pagination:{first:'首页',last:'末页',prev:'上一页',next:'下一页',
    first_title:'第一页',last_title:'最后一页',prev_title:'上一页',next_title:'下一页'}}}
  }:{})
 });
 const state={table,signature,built:false};
 table.on('tableBuilt',()=>{state.built=true});
 dataGrids.set(id,state);
}
function clearDataGrid(id){renderDataGrid(id,[],[],'')}
async function refreshSplash(){
 if(view!=='splash'||splashLoading)return;
 splashLoading=true;
 const target=$('splash-status');
 try{
  const data=await (await api('/api/v1/engines/splash')).json();
  if(view!=='splash')return;
  const statuses={ready:'就绪',unavailable:'服务在线，模型未就绪',protected:'状态端点需要认证',unreachable:'探测失败'};
  const instanceRows=data.instances.map(i=>`<p><strong>${esc(statuses[i.status])}</strong> · 127.0.0.1:${esc(i.port)} · PID ${esc(i.pid)}<br>${esc(i.model||'模型 ID 未探测到')} · 空闲释放 ${esc(i.idle_release??'—')} · KV ${esc(i.kv_format??'—')} · 内存预算 ${esc(i.max_memory && /^\d+$/.test(i.max_memory)? (Number(i.max_memory)/2**30).toFixed(1)+' GiB': i.max_memory??'—')}</p>`);
  const configRows=data.configured_models.map(m=>`<p>${esc(m.id)} · temperature ${esc(m.temperature??'—')} · top_p ${esc(m.top_p??'—')} · top_k ${esc(m.top_k??'—')} · 上下文 ${esc(m.max_context_window??'—')} · oMLX TTL ${esc(m.ttl_seconds==null?'—':m.ttl_seconds+' 秒')}</p>`);
  target.innerHTML=`${instanceRows.join('')||'<p>未发现 Splash 监听实例。</p>'}<h3>oMLX 已保存的配置</h3>${configRows.join('')||(data.config_status==='available'?'<p>没有配置为 Splash 的模型。</p>':'<p>oMLX 模型配置文件缺失或无法读取。</p>')}`;
  const native=data.instances.filter(i=>i.metrics?.status==='available');
  renderDataGrid('splash-native',['模型','完成请求','预填充 Token','输出 Token','复用 Token','预填充 tok/s','输出 tok/s','引擎内存'],native.map(i=>[
   i.model||`127.0.0.1:${i.port}`,tokenCount(i.metrics.completed),tokenCount(i.metrics.prefill_tokens),
   tokenCount(i.metrics.output_tokens),tokenCount(i.metrics.reused_tokens),tokenRate(i.metrics.prefill_tps),
   tokenRate(i.metrics.output_tps),i.metrics.memory_bytes==null?'—':(i.metrics.memory_bytes/2**30).toFixed(1)+' GiB'
  ]),data.instances.some(i=>i.metrics?.status==='protected')?'Splash 指标需要实例凭据。':'暂无 Splash 原生指标。');
  $('splash-omlx-history').hidden=data.history_status!=='available'||!data.usage_history.length;
  $('splash-log-source').textContent=data.log_source==='standalone'?'独立 Splash 日志，最多 25 条完成或取消记录。':'oMLX 托管的 Splash 日志，最多 25 条完成或取消记录。';
  renderDataGrid('splash-history',['模型','请求','提示 Token','输出 Token','已缓存 Token','缓存占比','输出 tok/s'],data.usage_history.map(m=>[
   m.model,tokenCount(m.requests),tokenCount(m.prompt_tokens),tokenCount(m.output_tokens),tokenCount(m.cached_tokens),
   m.prompt_tokens?(m.cached_tokens/m.prompt_tokens*100).toFixed(1)+'%':'—',tokenRate(m.output_tps)
  ]),data.history_status==='available'?'暂无匹配的 oMLX 历史。':'oMLX 历史数据库不可读。');
  renderDataGrid('splash-requests',['日志时间','模型','状态','输入 Token','输出 Token','已缓存 Token','输出 tok/s'],data.recent_requests.map(r=>[
   r.time,r.model,r.outcome==='Done'?'完成':'取消',tokenCount(r.prompt_tokens),tokenCount(r.output_tokens),tokenCount(r.cached_tokens),tokenRate(r.output_tps)
  ]),'暂无可读取的 Splash 请求记录。');
 }catch(error){target.textContent='Splash 状态读取失败：'+error.message;clearDataGrid('splash-native');$('splash-omlx-history').hidden=true;clearDataGrid('splash-requests');$('splash-log-source').textContent=''}
 finally{splashLoading=false}
}
const downloadsAPI='/api/v1/downloads';
let downloadingView=false, downloadOwner=false;
const downloadSources={mirror:'HF Mirror',official:'Hugging Face 官方'};
$('download-probe').onclick=async()=>{
 const button=$('download-probe'), target=$('download-source-status');
 button.disabled=true;target.textContent='检测中…';
 try{
  const sources=await(await api(downloadsAPI+'/sources')).json();
  target.textContent=sources.map(item=>downloadSources[item.source]+' '+(item.reachable?item.milliseconds+' ms':'不可达')).join(' · ');
 }catch(error){target.textContent=error.message}
 finally{button.disabled=false}
};
async function refreshDownloads(){
 if(view!=='downloads'||downloadingView||$('download-tasks').contains(document.activeElement))return;
 downloadingView=true;
 try{
  const [state,tasks,history]=await Promise.all(['/status','/tasks','/history'].map(async path=>(await api(downloadsAPI+path)).json()));
  if(view!=='downloads')return;
  downloadOwner=state.owns_queue;
  $('download-status').textContent=downloadOwner?'8777 正在管理下载队列':'下载队列尚未由 8777 接管';
  $('download-submit').disabled=!downloadOwner||$('download-add').dataset.submitting==='1';
  const ordered=[...tasks].sort((a,b)=>(a.queue_pos??Infinity)-(b.queue_pos??Infinity));
  const phases={queued:'排队中',running:'下载中',complete:'已完成',paused:'已暂停','probing-clear':'准备下载','bucket-empty-waiting':'等待下载窗口'};
  const completedExpanded=$('download-completed')?.open;
  const taskMarkup=task=>{
   const actions=task.done?['remove']:[task.paused?'resume':'pause','probe',...(task.paused?['source']:[]),'remove'];
   return `<div class="download-task"><div><strong>${esc(task.repo)}</strong><small>${esc(task.paused?'已暂停':phases[task.phase]||task.phase)} · ${esc(downloadSources[task.source])}${task.queue_pos?' · 队列第 '+task.queue_pos+' 位':''} · ${esc(bytes(task.have_bytes))} / ${esc(bytes(task.total_bytes))}${task.rate_MBps==null?'':' · '+esc(task.rate_MBps)+' MB/s'}${task.eta_minutes==null?'':' · 约 '+esc(task.eta_minutes)+' 分钟'}</small>${task.done?'':`<div class="download-progress"><progress max="100" value="${esc(Math.min(100,task.pct))}" aria-label="下载进度"></progress><span>${esc(task.pct)}%</span></div>`}</div><div class="download-actions">${actions.map(action=>`<button class="btn btn-outline-secondary" type="button" data-download-action="${action}" data-slug="${esc(task.slug)}" data-source="${esc(task.source)}" ${downloadOwner?'':'disabled'}>${({pause:'暂停',resume:'续传',probe:'重探',source:'切换源',remove:'移除'})[action]}</button>`).join('')}</div></div>`;
  };
  const active=ordered.filter(task=>!task.done), completed=ordered.filter(task=>task.done);
  $('download-tasks').innerHTML=(active.map(taskMarkup).join('')||'<p>没有待下载的任务。</p>')+
   (completed.length?`<details id="download-completed" ${completedExpanded?'open':''}><summary>已完成的队列记录（${completed.length}）</summary>${completed.map(taskMarkup).join('')}</details>`:'');
  renderDataGrid('download-history',['仓库','完成时间','文件','校验'],history.map(row=>[row.repo,row.finished_at,row.files??'—',row.verify]),'暂无完成记录。',{paginate:true});
 }catch(error){
  downloadOwner=false;$('download-submit').disabled=true;
  $('download-status').textContent='读取下载队列失败：'+error.message;
 }finally{downloadingView=false}
}
$('download-search').onsubmit=async e=>{
 e.preventDefault();const button=e.submitter;if(button)button.disabled=true;
 const q=new FormData(e.currentTarget).get('q').trim();
 if(!q){if(button)button.disabled=false;return}
 $('download-results').textContent='搜索中…';
 try{
  const results=await(await api(downloadsAPI+'/search?'+new URLSearchParams({q,source:$('download-source').value}))).json();
  $('download-results').innerHTML=results.map(row=>`<div class="download-task"><div><strong>${esc(row.id)}</strong><small>${esc(row.downloads.toLocaleString())} 次下载 · ${esc(row.tags.join(' · '))}</small></div><button class="btn btn-outline-secondary" type="button" data-repo="${esc(row.id)}">选用</button></div>`).join('')||'未找到匹配模型。';
 }catch(error){$('download-results').textContent=error.message}
 finally{if(button)button.disabled=false}
};
$('download-results').onclick=e=>{
 const button=e.target.closest('[data-repo]');if(!button)return;
 $('download-repo').value=button.dataset.repo;$('download-repo').focus();$('download-repo').scrollIntoView({block:'nearest'});
};
$('download-add').onsubmit=async e=>{
 e.preventDefault();const form=e.currentTarget;
 if(!downloadOwner||form.dataset.submitting==='1')return;
 form.dataset.submitting='1';$('download-submit').disabled=true;
 try{
  const body=Object.fromEntries(new FormData(form));body.repo=body.repo.trim();body.workers=Number(body.workers);body.source=$('download-source').value;
  const result=await(await api(downloadsAPI+'/tasks',body)).json();
  toast(result.enough_disk?'已加入下载队列':'已入队，目标磁盘可用空间可能不足');form.reset();
 }catch(error){toast(error.message)}
 finally{delete form.dataset.submitting;$('download-submit').disabled=!downloadOwner;await refreshDownloads()}
};
$('download-tasks').onclick=async e=>{
 const button=e.target.closest('[data-download-action]');
 if(!button||!downloadOwner)return;
 const action=button.dataset.downloadAction, path=downloadsAPI+'/tasks/'+encodeURIComponent(button.dataset.slug);
 if(action==='remove'&&!confirm('移除该下载任务？已下载文件会保留。'))return;
 button.disabled=true;
 try{
  if(action==='remove'){
   const response=await fetch(path,{method:'DELETE'});
   if(!response.ok)throw Error((await response.json()).detail||'移除失败');
  }else if(action==='source'){
   const source=button.dataset.source==='official'?'mirror':'official';
   const response=await fetch(path+'/source?'+new URLSearchParams({source}),{method:'PUT'});
   if(!response.ok)throw Error((await response.json()).detail||'切换失败');
   toast('已切换到 '+downloadSources[source]+'，可继续下载');
  }else await api(path+'/'+action,{});
  button.blur();await refreshDownloads();
 }catch(error){toast(error.message)}
 finally{button.disabled=false}
};
window.addEventListener('popstate',()=>{showView(requestedView());refreshSplash();refreshDownloads()});
showView(view);refresh();refreshSplash();refreshDownloads();setInterval(()=>{if(!document.hidden){refresh();refreshSplash();refreshDownloads()}},5000);

function projectLabel(j){return j.execution?`${j.execution.profile.model_id} / ${j.execution.profile.engine}`:'历史任务'}
