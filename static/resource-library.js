'use strict';
(() => {
const $ = id => document.getElementById(id);
const API = '/api/resource-library';
const PAGE_SIZE = 100;
const FILE_DRAG_TYPE='application/x-devhelper-file-items';
const fileOps={clipboard:null,sequence:0,busy:false,pending:null,jobs:new Map(),drag:null};
const textViewer={editor:null,model:null,observer:null,loader:null,wrap:true,disposable:null};
const selection = new Set();
const folderSelection = new Set();
const explorer = {path:'',breadcrumbs:[],history:[],index:-1,focusId:'',anchorId:'',selectionAssets:new Map(),preview:null,previewRequest:0,zoom:1};
const cloudExplorer = {entries:[],selection:new Set(),focusId:'',anchorId:'',history:[],index:-1,previews:new Map(),previewJobs:new Map()};

const kindLabels = { '':'全部资源', image:'图片', model:'模型', material:'材质', shader:'Shader', spine:'Spine', animation:'动画', audio:'音乐音效', font:'字体', scene:'场景', prefab:'预制体', code:'代码片段', document:'文档', video:'视频', archive:'压缩包', metadata:'元数据', file:'其他文件' };
const kindIcons = {image:'image',model:'cube',material:'cube',shader:'spark',spine:'spark',animation:'spark',audio:'audio',font:'file',scene:'cube',prefab:'cube',code:'file',video:'image'};
const states = {queued:'等待中',pending:'等待中',running:'进行中',completed:'已完成','completed-with-errors':'部分完成',failed:'失败',cancelled:'已取消',cancelling:'正在取消',interrupted:'已中断',incomplete:'未完成',skipped:'已跳过',retrying:'重试中',unknown:'需核对'};
let sortField='name',sortOrder='asc';
let entries=[],projectItems=[],current=null,kind='',offset=0,total=0,view='list',assetRequest=0,detailRequest=0,oauthState='',cloudOffset=0,cloudHasMore=false,cloudScopeRoot='',table,cloudTable,tableReady,cloudTableReady,modelConfig={},currentTab='library',actionWork=null,jobTimer=null,scanTimer=null,scanBusy=false,activeAiJobs=[],aiPollBusy=false,cloudRequest=0;
const backupState={assetIds:[],plan:null,input:null,request:0,submitting:false};
const catalogState={status:null,snapshots:[],jobs:[],selected:'',request:0,busy:false,polling:false,knownActive:new Set(),fetching:new Map()};
function node(tag,text,className){const el=document.createElement(tag);if(text!==undefined)el.textContent=String(text);if(className)el.className=className;return el;}
function icon(name){const el=document.createElementNS('http://www.w3.org/2000/svg','svg');el.setAttribute('class','icon');el.setAttribute('aria-hidden','true');const use=document.createElementNS('http://www.w3.org/2000/svg','use');use.setAttribute('href','#i-'+name);el.append(use);return el;}
function button(text,work,className=''){const el=node('button',text,className);el.type='button';if(work)el.onclick=()=>run(work);return el;}
function bytes(value){let n=Number(value)||0;if(n<1024)return n+' B';for(const unit of ['KB','MB','GB','TB']){n/=1024;if(n<1024||unit==='TB')return n.toFixed(n<10?1:0)+' '+unit;}}
function time(value,short=false){if(!value)return '—';const v=typeof value==='number'?new Date(value<1e12?value*1000:value):new Date(value);return Number.isNaN(v.getTime())?'—':v.toLocaleString('zh-CN',short?{month:'2-digit',day:'2-digit',hour:'2-digit',minute:'2-digit'}:{year:'numeric',month:'2-digit',day:'2-digit',hour:'2-digit',minute:'2-digit'});}
function badge(text,cls=''){return node('span',text,'badge '+cls);}
function stateOf(job){return job.state||job.status||'pending';}
function running(job){return ['queued','pending','running','cancelling','retrying'].includes(stateOf(job));}
function safeUrl(value,prefix='/api/'){try{const url=new URL(value,location.origin);return url.origin===location.origin&&url.pathname.startsWith(prefix)?url.href:'';}catch{return '';}}
function notice(text,error=false){$('notice-text').textContent=text;$('notice').hidden=!text;$('notice').classList.toggle('error',error);}
async function api(path,data){const response=await fetch(path,data===undefined?{cache:'no-store'}:{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(data)});let value;try{value=await response.json();}catch{throw new Error('服务返回了无法读取的响应，请刷新重试。');}if(!response.ok){const error=new Error(value.error||value.message||'操作未完成（'+response.status+'）');error.status=response.status;error.details=value;throw error;}return value;}
async function run(work){try{return await work();}catch(error){notice(error.message||'操作未完成',true);return null;}}
function disabledWhile(el,work){return async()=>{el.disabled=true;try{await work();}finally{el.disabled=false;}};}
function tab(name){currentTab=name;for(const key of ['library','cloud','history']){$(key).hidden=key!==name;$('tab-'+key).setAttribute('aria-selected',String(key===name));}if(name==='cloud')run(async()=>{const c=await loadCloudConfig();if(c.authorized)await loadCloud();else{$('cloud-table').hidden=true;$('cloud-empty').hidden=false;$('cloud-empty').querySelector('h2').textContent='先连接你的百度网盘';$('cloud-empty').querySelector('p').textContent='点击「账号与授权」，填写应用信息并完成百度账号授权。';}});if(name==='history')run(loadJobs);updateFileCommands();if(name==='library'&&table)requestAnimationFrame(()=>table.redraw(true));}
function openInspector(show=true){$('inspector').hidden=!show;$('toggle-inspector').setAttribute('aria-pressed',String(show));if(table)requestAnimationFrame(()=>table.redraw(true));}
function projectName(){return projectItems.find(p=>p.id===$('project').value)?.name||'全部工程';}
function selectedInfo(){
  const count=selection.size,folders=folderSelection.size,allCount=count+folders;$('selected').textContent=allCount?'已选 '+allCount+' 项'+(folders?'（'+count+' 个文件）':''):'未选择项目';$('clear').disabled=!allCount;$('backup').disabled=!count||count>100;$('backup').title=count>100?'每次最多上传100条，可分批处理':'预览分类并上传选中资源到网盘';
  const all=entries.length>0&&entries.every(a=>isSelected(a.id));$('select-page').checked=all;$('select-page').indeterminate=!all&&entries.some(a=>isSelected(a.id));const canStart=$('task-scope').value==='project'?!!$('project').value:!!count;for(const id of ['embed','classify','both'])$(id).disabled=!canStart;$('open-selected').disabled=allCount!==1;updateFileCommands();
}
function toggleSelection(id,checked){
  const asset=entries.find(a=>a.id===id)||explorer.selectionAssets.get(id),target=isFolder(asset)?folderSelection:selection;
  if(checked&&selection.size>=500&&!target.has(id)&&target===selection){notice('单次最多选择500个文件。更多资源可选择「当前整个工程」批量处理。',true);return false;}
  checked?target.add(id):target.delete(id);if(checked&&asset)explorer.selectionAssets.set(id,asset);selectedInfo();syncSelection();return true;
}
function syncSelection(){
  if(table)table.getRows().forEach(row=>{const id=row.getData().id,el=row.getElement();el.classList.toggle('tabulator-selected',isSelected(id));el.classList.toggle('file-focused',id===explorer.focusId);el.setAttribute('aria-selected',String(isSelected(id)));const input=el.querySelector('input.asset-check');if(input)input.checked=isSelected(id);el.classList.toggle('cut-entry',isCutEntry(row.getData(),'local'));});
  $('asset-grid').querySelectorAll('.asset-card').forEach(card=>{card.classList.toggle('selected',isSelected(card.dataset.id));card.classList.toggle('file-focused',card.dataset.id===explorer.focusId);card.setAttribute('aria-selected',String(isSelected(card.dataset.id)));card.querySelector('input').checked=isSelected(card.dataset.id);const asset=entries.find(item=>item.id===card.dataset.id);card.classList.toggle('cut-entry',isCutEntry(asset,'local'));});
}
function isFolder(asset){return !!asset&&(asset.isDir===true||asset.type==='folder');}
function isSelected(id){return selection.has(id)||folderSelection.has(id);}
function clearSelection(){selection.clear();folderSelection.clear();explorer.selectionAssets.clear();current=null;++detailRequest;$('detail').hidden=true;$('detail-placeholder').hidden=false;$('detail-placeholder').textContent='选中文件后点击「属性」，查看标签和依赖。';selectedInfo();syncSelection();}
function focusFiles(id){explorer.focusId=id||'';$('asset-content').focus({preventScroll:true});syncSelection();}
function selectEntry(asset,event={}){
  if(!asset)return;const additive=!!(event.ctrlKey||event.metaKey),range=!!event.shiftKey;
  if(range&&explorer.anchorId){const a=entries.findIndex(item=>item.id===explorer.anchorId),b=entries.findIndex(item=>item.id===asset.id);if(a>=0&&b>=0){if(!additive){selection.clear();folderSelection.clear();explorer.selectionAssets.clear();}for(const item of entries.slice(Math.min(a,b),Math.max(a,b)+1))toggleSelection(item.id,true);}else toggleSelection(asset.id,true);}
  else{if(!additive){selection.clear();folderSelection.clear();explorer.selectionAssets.clear();}toggleSelection(asset.id,additive?!isSelected(asset.id):true);explorer.anchorId=asset.id;}
  focusFiles(asset.id);selectedInfo();syncSelection();if(!$('inspector').hidden)run(()=>isFolder(asset)?folderProperties(asset):loadDetail(asset.id));
}
function selectedEntry(){const ids=[...selection,...folderSelection];return ids.length===1?(entries.find(a=>a.id===ids[0])||explorer.selectionAssets.get(ids[0])):null;}
function selectionClick(event,asset){if(event.target.closest('input,button,a'))return;selectEntry(asset,event);}
function remoteOnly(asset){return asset?.storage?.state==='remote-only';}
function storageLabel(asset){return remoteOnly(asset)?'云端未下载':asset?.storage?.state==='cached'?'已下载缓存':'本机可用';}
function thumb(asset,className='file-thumb'){
  if(!isFolder(asset)&&!remoteOnly(asset)&&asset.kind==='image'&&asset.thumbnailUrl){const image=node('img',undefined,className);image.src=safeUrl(asset.thumbnailUrl,API+'/assets/');image.loading='lazy';image.decoding='async';image.alt='';image.onerror=()=>image.replaceWith(thumb({...asset,thumbnailUrl:null},className));return image;}
  const placeholder=node('span',undefined,className+' file-thumb-placeholder'+(isFolder(asset)?' folder-thumb':''));placeholder.append(icon(isFolder(asset)?'folder':kindIcons[asset.kind]||'file'));return placeholder;
}
function fileCell(asset){const wrap=node('div',undefined,'file-cell');wrap.append(thumb(asset));const text=node('div',undefined,'file-description');const name=node('span',asset.name||asset.path,'file-name');name.title=asset.path||asset.name||'';text.append(name);wrap.append(text);return wrap;}
function aiStatus(asset){const row=node('div',undefined,'file-status');if(asset.storage)row.append(badge(storageLabel(asset),remoteOnly(asset)?'cloud-only':'ready'));const embeddings=asset.embeddings||[];const hasVector=asset.hasEmbedding??(asset.embeddingStatus==='ready'||embeddings.length>0);const annotations=asset.annotations||[];const ai=annotations.find(a=>a.source==='ai'),manual=validAnnotation(asset,'manual'),hasManual=annotationHasLabels(manual);if(hasVector)row.append(badge('向量','ready'));if(hasManual)row.append(badge('人工标注','ready'));if(ai||asset.hasAiAnnotation)row.append(badge(ai?.stale?'分类过期':'AI 分类',ai?.stale?'stale':'ai'));if(!hasVector&&!hasManual&&!ai&&!asset.hasAiAnnotation)row.append(node('span','待整理','hint'));if(typeof asset.score==='number')row.append(badge((asset.score*100).toFixed(0)+'%','score'));return row;}
function buildTables(){
  table=new Tabulator('#asset-table',{data:[],height:'100%',layout:'fitColumns',index:'id',rowHeight:36,placeholder:'',selectableRows:false,movableColumns:true,headerVisible:true,columnDefaults:{headerSort:false},columns:[
    {title:'',field:'_select',width:33,minWidth:33,maxWidth:33,resizable:false,formatter:cell=>{const asset=cell.getRow().getData(),box=node('input',undefined,'asset-check');box.type='checkbox';box.checked=isSelected(asset.id);box.setAttribute('aria-label','选择 '+asset.name);box.onclick=e=>e.stopPropagation();box.onchange=()=>{explorer.focusId=asset.id;explorer.anchorId=asset.id;if(!toggleSelection(asset.id,box.checked))box.checked=false;};return box;}},
    {title:'名称',field:'name',minWidth:200,widthGrow:5,formatter:cell=>fileCell(cell.getRow().getData())},
    {title:'修改日期',field:'modifiedAt',width:137,formatter:cell=>node('span',time(cell.getValue(),true),'file-date')},
    {title:'类型',field:'kind',width:100,formatter:cell=>isFolder(cell.getRow().getData())?'文件夹':kindLabels[cell.getValue()]||cell.getValue()||'文件'},
    {title:'大小',field:'bytes',width:90,hozAlign:'right',formatter:cell=>isFolder(cell.getRow().getData())?'':bytes(cell.getValue())},
    {title:'状态',field:'_status',minWidth:112,widthGrow:1,formatter:cell=>isFolder(cell.getRow().getData())?node('span',cell.getRow().getData().childCount!==undefined?cell.getRow().getData().childCount+' 项':'','hint'):aiStatus(cell.getRow().getData())}
  ],rowFormatter:row=>{const asset=row.getData(),el=row.getElement();el.classList.toggle('tabulator-selected',isSelected(asset.id));el.classList.toggle('file-focused',asset.id===explorer.focusId);el.setAttribute('aria-selected',String(isSelected(asset.id)));el.title=asset.path||asset.name||'';installFileDrag(el,asset,'local');}});
  tableReady=new Promise(resolve=>table.on('tableBuilt',resolve));
  table.on('rowClick',(event,row)=>selectionClick(event,row.getData()));
  table.on('rowDblClick',(event,row)=>{if(!event.target.closest('input,button,a'))run(()=>openEntry(row.getData()));});
  table.on('rowContext',(event,row)=>{event.preventDefault();showContext(event,row.getData(),'library');});
  table.on('headerClick',(event,column)=>{const mapping={name:'name',bytes:'size',kind:'kind',modifiedAt:'modified'},field=mapping[column.getField()];if(!field)return;if($('search-mode').value==='semantic'){notice('语义搜索按相关程度排列。切换关键词搜索后可按列排序。');return;}sortOrder=sortField===field&&sortOrder==='asc'?'desc':'asc';sortField=field;offset=0;table.getColumns().forEach(col=>{const active=col===column;col.getElement().classList.toggle('header-sorted',active);col.getElement().setAttribute('aria-sort',active?(sortOrder==='asc'?'ascending':'descending'):'none');col.getElement().dataset.sortOrder=active?sortOrder:'';});run(loadAssets);});
  cloudTable=new Tabulator('#cloud-table',{data:[],height:'100%',layout:'fitColumns',index:'path',rowHeight:36,placeholder:'',selectableRows:false,columnDefaults:{headerSort:false},columns:[
    {title:'名称',field:'name',minWidth:220,widthGrow:5,formatter:cell=>{const f=cell.getRow().getData();return fileCell({...f,id:f.path,type:f.isDir?'folder':'file'});}},
    {title:'修改日期',field:'modifiedAt',width:145,formatter:cell=>time(cell.getValue(),true)},
    {title:'类型',field:'extension',width:115,formatter:cell=>cell.getRow().getData().isDir?'文件夹':cell.getValue()||'文件'},
    {title:'大小',field:'bytes',width:100,hozAlign:'right',formatter:cell=>cell.getRow().getData().isDir?'':bytes(cell.getValue())}
  ],rowFormatter:row=>{const f=row.getData(),el=row.getElement();el.classList.toggle('tabulator-selected',cloudExplorer.selection.has(f.path));el.classList.toggle('file-focused',cloudExplorer.focusId===f.path);el.setAttribute('aria-selected',String(cloudExplorer.selection.has(f.path)));installFileDrag(el,f,'netdisk');}});
  cloudTableReady=new Promise(resolve=>cloudTable.on('tableBuilt',resolve));
  cloudTable.on('rowClick',(event,row)=>selectCloud(row.getData(),event));cloudTable.on('rowDblClick',(event,row)=>run(()=>cloudAction(row.getData())));cloudTable.on('rowContext',(event,row)=>{event.preventDefault();showContext(event,row.getData(),'cloud');});
}
function renderGrid(){const root=$('asset-grid');root.replaceChildren();for(const asset of entries){const card=node('article',undefined,'asset-card'+(isSelected(asset.id)?' selected':'')+(asset.id===explorer.focusId?' file-focused':''));card.dataset.id=asset.id;card.setAttribute('aria-label',asset.name);card.setAttribute('aria-selected',String(isSelected(asset.id)));const check=node('input',undefined,'card-check');check.type='checkbox';check.checked=isSelected(asset.id);check.setAttribute('aria-label','选择 '+asset.name);check.onclick=e=>e.stopPropagation();check.onchange=()=>{explorer.focusId=asset.id;explorer.anchorId=asset.id;if(!toggleSelection(asset.id,check.checked))check.checked=false;};const preview=node('div',undefined,'card-preview');preview.append(thumb(asset,''));const body=node('div',undefined,'card-body'),name=node('span',asset.name,'file-name');name.title=asset.path||asset.name;body.append(name);const meta=node('div',undefined,'card-meta');meta.append(node('span',isFolder(asset)?'文件夹':kindLabels[asset.kind]||asset.kind),node('span',isFolder(asset)?'':bytes(asset.bytes)));body.append(meta);if(!isFolder(asset))body.append(aiStatus(asset));card.append(check,preview,body);installFileDrag(card,asset,'local');card.onclick=e=>selectionClick(e,asset);card.ondblclick=e=>{if(!e.target.closest('input'))run(()=>openEntry(asset));};card.oncontextmenu=e=>{e.preventDefault();showContext(e,asset,'library');};root.append(card);}}
function setView(next){view=next;$('asset-table').hidden=next!=='list';$('asset-grid').hidden=next!=='grid';$('view-list').setAttribute('aria-pressed',String(next==='list'));$('view-grid').setAttribute('aria-pressed',String(next==='grid'));if(next==='list')table.redraw(true);else renderGrid();}
function renderCategories(){for(const [value,label] of Object.entries(kindLabels)){if(['metadata','file','archive','document','video'].includes(value))continue;const el=button(label,async()=>{await navigateLocal({projectId:$('project').value,path:'',kind:value,query:'',mode:'text'});});el.dataset.kind=value;el.prepend(icon(value?kindIcons[value]||'file':'library'));$('categories').append(el);}renderCategoryState();}
function renderCategoryState(){$('categories').querySelectorAll('button').forEach(el=>el.classList.toggle('active',el.dataset.kind===kind));$('kind-heading').textContent=kindLabels[kind]||kind;}
async function loadProjects(){const result=await api(API+'/projects');projectItems=result.items||[];const keep=$('project').value;$('project').replaceChildren(new Option('全部工程',''));for(const p of projectItems)$('project').add(new Option(p.name,p.id));$('project').value=projectItems.some(p=>p.id===keep)?keep:'';$('project-summary').textContent=projectItems.length+' 个工程';$('project-heading').textContent=projectName();$('manifest').disabled=!$('project').value;$('scan').disabled=!$('project').value;selectedInfo();}
async function loadAssets(){
  const request=++assetRequest;$('asset-loading').hidden=false;$('asset-content').setAttribute('aria-busy','true');
  try{
    const payload={mode:$('search-mode').value,query:$('query').value.trim(),limit:PAGE_SIZE,offset},searching=!!payload.query||!!kind||payload.mode==='semantic';if($('project').value)payload.projectId=$('project').value;if(kind)payload.kind=kind;
    if(payload.mode==='text'){payload.sort=sortField;payload.order=sortOrder;}if(payload.mode==='semantic'&&!payload.query)throw new Error('输入想找的素材描述，再开始语义搜索。');
    const browsePayload={path:explorer.path,sort:sortField,order:sortOrder,limit:PAGE_SIZE,offset};if(payload.projectId)browsePayload.projectId=payload.projectId;const result=await api(API+(searching?'/search':'/browse'),searching?payload:browsePayload);if(request!==assetRequest)return;
    entries=result.items||[];total=payload.mode==='semantic'?Math.min(result.total??entries.length,200):(result.total??entries.length);explorer.breadcrumbs=Array.isArray(result.breadcrumbs)?result.breadcrumbs:[];if(typeof result.path==='string'&&!searching)explorer.path=result.path;
    if(!entries.some(item=>item.id===explorer.focusId))explorer.focusId='';await tableReady;await table.replaceData(entries);if(request!==assetRequest)return;if(view==='grid')renderGrid();
    const noData=!entries.length;$('asset-empty').hidden=!noData;$('asset-table').hidden=noData||view!=='list';$('asset-grid').hidden=noData||view!=='grid';const hasProjects=projectItems.length>0;
    $('empty-title').textContent=searching?'没有找到这些资源':hasProjects?'此文件夹为空':'添加你的第一个工程';$('empty-description').textContent=searching?(payload.mode==='semantic'?'先为该工程生成向量，或换一个描述试试。':'换个关键词或选择其他资源类型。'):hasProjects?'返回上级目录浏览，或扫描更新工程索引。':'Unity、Cocos、Spine 或任何素材目录，都可以从这里开始整理。';$('empty-add').hidden=searching||hasProjects;
    $('count').textContent=Number(total).toLocaleString('zh-CN')+' 项'+(searching?(payload.mode==='semantic'?' · 按语义相似度':' · 搜索结果'):'');$('page').textContent=entries.length?(offset+1)+'–'+(offset+entries.length):'0';$('previous').disabled=offset===0;$('next').disabled=offset+entries.length>=total||entries.length<PAGE_SIZE;
    $('sort-hint').textContent=payload.mode==='semantic'?'按相关程度排列 · 双击打开':'单击选择 · 双击打开 · 右键更多操作';renderNavigation();selectedInfo();syncSelection();if(view==='list'&&entries.length)requestAnimationFrame(()=>table.redraw(true));
  }finally{if(request===assetRequest){$('asset-loading').hidden=true;$('asset-content').setAttribute('aria-busy','false');}}
}
function fileScope(){return currentTab==='cloud'?'netdisk':'local';}
function operationEntries(scope=fileScope()){
  if(scope==='netdisk')return cloudExplorer.entries.filter(item=>cloudExplorer.selection.has(item.path));
  return [...new Set([...selection,...folderSelection])].map(id=>entries.find(item=>item.id===id)||explorer.selectionAssets.get(id)).filter(Boolean);
}
function managementRef(asset,scope){
  if(scope==='netdisk'){if(asset?.canManage===false||!asset?.path||cloudPathValue(asset.path)==='')return null;return {path:asset.path,...(asset.fsId?{fsId:String(asset.fsId)}:{})};}
  if(asset?.canManage!==true||!asset.projectId||!asset.path||asset.storage?.state==='remote-only'||asset.storage?.state==='cached')return null;return {projectId:asset.projectId,path:asset.path};
}
function destinationRef(scope=fileScope(),folder=null){
  if(scope==='netdisk')return {path:folder?.path??$('remote-dir').value};
  const projectId=folder?.projectId||$('project').value,path=folder?.path??explorer.path,project=projectItems.find(item=>item.id===projectId);if(!projectId||project?.remoteBacked===true||project?.source==='cloud'||project?.canManage===false)return null;return {projectId,path};
}
function managedSelection(scope=fileScope()){const selected=operationEntries(scope);return selected.length>0&&selected.every(asset=>!!managementRef(asset,scope));}
function itemKey(scope,item){return scope+':'+(item.projectId||'')+':'+item.path;}
function isCutEntry(asset,scope){const clipboard=fileOps.clipboard,ref=managementRef(asset,scope);return !!ref&&clipboard?.scope===scope&&clipboard.operation==='move'&&clipboard.items.some(item=>itemKey(scope,item)===itemKey(scope,ref));}
function updateFileCommands(){
  for(const scope of ['local','netdisk']){const prefix=scope==='local'?'file':'cloud',chosen=operationEntries(scope),manageable=managedSelection(scope),target=destinationRef(scope);for(const action of ['copy','cut','delete'])$(prefix+'-'+action).disabled=fileOps.busy||!manageable;$(prefix+'-rename').disabled=fileOps.busy||!manageable||!chosen.length;$(prefix+'-paste').disabled=fileOps.busy||!target||!fileOps.clipboard||fileOps.clipboard.scope!==scope;const clip=fileOps.clipboard;$(prefix+'-clipboard').textContent=clip?.scope===scope?(clip.operation==='move'?'待移动 ':'已复制 ')+clip.items.length+' 项，打开目标文件夹后粘贴':'';}
}
function setFileClipboard(operation,scope=fileScope()){
  const chosen=operationEntries(scope);if(!chosen.length||!chosen.every(asset=>managementRef(asset,scope)))throw new Error(scope==='local'?'请只选择可管理的本机文件或文件夹；工程根目录与云索引不可剪切或复制。':'请先选择应用目录中的文件或文件夹。');
  fileOps.clipboard={id:++fileOps.sequence,scope,operation,items:chosen.map(asset=>managementRef(asset,scope)),entries:chosen.map(asset=>({...asset}))};syncSelection();syncCloudSelection();updateFileCommands();notice((operation==='move'?'已剪切 ':'已复制 ')+chosen.length+' 项。打开目标目录后粘贴。');
}
async function pasteFiles(scope=fileScope(),folder=null){const clipboard=fileOps.clipboard,target=destinationRef(scope,folder);if(!clipboard)throw new Error('请先选择文件并复制或剪切。');if(clipboard.scope!==scope)throw new Error('本机和百度网盘分别管理，请在相同位置粘贴。');if(!target)throw new Error('请打开一个可管理的工程目录作为目标。');await prepareFileOperation({scope,operation:clipboard.operation,items:clipboard.items.map(item=>({...item})),destination:target},clipboard.entries,clipboard.id);}
function renameFile(scope=fileScope()){
  const chosen=operationEntries(scope);if(!chosen.length||!chosen.every(asset=>managementRef(asset,scope)))throw new Error('请选择可管理的文件或文件夹。');const asset=chosen[0],batch=chosen.length>1;
  showAction({title:batch?'批量重命名':'重命名',description:batch?'已选 '+chosen.length+' 项。支持 {name} 原名称（不含扩展名）、{index} 从 1 开始的序号、{ext} 扩展名（含点，文件夹为空）。下一步预览每项新名称。':'更改名称，同名项目不会被覆盖。',label:batch?'名称模板':'新名称',value:batch?'{name}_{index}{ext}':asset.name,submit:batch?'预览重命名':'重命名',work:async name=>{if(!name||!batch&&name===asset.name)return;if(/[\\/\0\r\n]/.test(name)||name==='.'||name==='..')throw new Error('名称不能包含目录分隔符。');await prepareFileOperation({scope,operation:'rename',items:chosen.map(item=>managementRef(item,scope)),name},chosen);}});
  setTimeout(()=>{$('action-value').focus();const value=$('action-value').value,last=value.lastIndexOf('.');$('action-value').setSelectionRange?.(0,last>0&&!batch&&!isFolder(asset)?last:value.length);},0);
}
async function deleteFiles(scope=fileScope()){const chosen=operationEntries(scope);if(!chosen.length||!chosen.every(asset=>managementRef(asset,scope)))throw new Error('请只选择可管理的文件或文件夹。');await prepareFileOperation({scope,operation:'trash',items:chosen.map(asset=>managementRef(asset,scope))},chosen);}
function operationItemCount(pending){return pending.plan?.items?.length||pending.chosen.length;}
function operationName(value){return {copy:'复制',move:'移动',rename:'重命名',trash:'删除',restore:'恢复'}[value]||'文件操作';}
function operationPath(value){if(typeof value==='string')return value;if(value&&typeof value==='object')return [value.projectName||projectItems.find(item=>item.id===value.projectId)?.name||value.projectId,value.path||value.name].filter(Boolean).join(' / ');return '';}
function planTarget(item){return operationPath(item.destination||item.target||item.targetPath||item.destinationPath||item.to||'');}
function planSource(item){return operationPath(item.source||item.item||item.sourcePath||item.path||item.from||'');}
function showOperationPlan(plan,payload,chosen,{failed=false,error=''}={}){
  const label=operationName(payload.operation),planned=Array.isArray(plan?.items)&&plan.items.length?plan.items:chosen.map(asset=>({name:asset.name,source:{path:asset.path},isDir:isFolder(asset)})),count=planned.length,companions=planned.filter(item=>item.companionOf).length;
  $('file-operation-title').textContent=failed?'无法'+label+'所选项目':label+' '+count+' 项';const destination=operationPath(payload.destination);
  $('file-operation-description').textContent=failed?error:payload.operation==='rename'?(chosen.length>1?'按模板「'+payload.name+'」重命名 '+chosen.length+' 项，请核对下列原名称和新名称。':'将「'+chosen[0].name+'」重命名为「'+payload.name+'」。'):label+' '+count+' 个文件或文件夹'+(destination?'到「'+destination+'」':'')+'。';
  const root=$('file-operation-items');root.replaceChildren();planned.forEach((item,index)=>{const row=node('div',undefined,'operation-preview-item');row.setAttribute('role','listitem');row.append(icon(item.isDir||item.isDirectory?'folder':'file'));const copy=node('div');const name=item.name||chosen[index]?.name||planSource(item).split('/').pop()||'项目';copy.append(node('strong',name),node('span',planSource(item)||chosen[index]?.path||'','operation-source'));if(item.companionOf)copy.append(badge('关联 Unity 元文件'));const target=planTarget(item);if(target)copy.append(node('span','→ '+target,'operation-target'));row.append(copy);root.append(row);});
  const warnings=[];for(const conflict of Array.isArray(plan?.conflicts)?plan.conflicts:[])warnings.push(typeof conflict==='string'?conflict:conflict.message||conflict.path||'存在同名项目');if(companions)warnings.push('已包含 '+companions+' 个关联的 Unity .meta 文件，避免资源引用失配。');if(payload.operation==='trash')warnings.push(payload.scope==='netdisk'?'移至百度网盘回收站，保留策略以百度为准。':'移入 DevHelper 回收站，完成后可以撤销删除。');if(payload.operation==='move')warnings.push('移动完成后，源目录中的项目会移到目标目录。');if(error&&!failed)warnings.push(error);$('file-operation-warning').textContent=warnings.join('\n');$('file-operation-execute').textContent=label;$('file-operation-execute').classList.toggle('danger',payload.operation==='trash');$('file-operation-execute').hidden=failed||!!plan?.conflicts?.length;$('file-operation-execute').disabled=fileOps.busy;
}
async function prepareFileOperation(payload,chosen,clipboardId=null){
  if(fileOps.busy)return;fileOps.busy=true;updateFileCommands();notice('正在检查所选文件和目标目录…');
  try{
    const plan=await api(API+'/file-ops/plan',payload);notice('');if(!plan.planId||!plan.planDigest)throw new Error('服务未返回完整操作预览，请重试。');fileOps.pending={plan,payload,chosen,clipboardId};showOperationPlan(plan,payload,chosen);
    if(plan.conflicts?.length||['move','trash'].includes(payload.operation)||(payload.operation==='rename'&&chosen.length>1)){$('file-operation-dialog').showModal();return;}
    await executeFilePlan();
  }catch(error){showOperationPlan(error.details||null,payload,chosen,{failed:true,error:error.message});fileOps.pending=null;if(!$('file-operation-dialog').open)$('file-operation-dialog').showModal();throw error;}
  finally{fileOps.busy=false;$('file-operation-execute').disabled=false;updateFileCommands();}
}
async function executeFilePlan(){
  const pending=fileOps.pending;if(!pending)return;fileOps.busy=true;$('file-operation-execute').disabled=true;updateFileCommands();
  try{const job=await api(API+'/file-ops/execute',{planId:pending.plan.planId,planDigest:pending.plan.planDigest}),id=String(job.jobId||job.id||'');if(!id)throw new Error('未返回文件操作任务编号。');fileOps.pending=null;$('file-operation-dialog').close();fileOps.jobs.set(id,{id,pending,job,timer:null,polling:false});renderFileJob(job,pending);await pollFileJob(id);}
  catch(error){$('file-operation-warning').textContent=error.status===409?'目录或文件已有变化，请关闭后重新操作。不会覆盖现有文件。':error.message;throw error;}
  finally{fileOps.busy=false;$('file-operation-execute').disabled=false;updateFileCommands();}
}
function fileJobItems(job){return job.result?.items||job.items||[];}
function renderFileJob(job,pending){
  const label=operationName(pending.payload.operation),items=fileJobItems(job);$('file-operation-status').hidden=false;$('file-operation-status-title').textContent=label+' · '+(states[stateOf(job)]||stateOf(job));$('file-operation-status-summary').textContent=(job.error||'')||(running(job)?'正在处理 '+operationItemCount(pending)+' 项，可继续浏览其他目录。':stateOf(job)==='completed'?'已完成 '+operationItemCount(pending)+' 项。':'请查看每个项目的处理结果，未成功的剪切项目仍保留。');
  const root=$('file-operation-status-items');root.replaceChildren();const rows=items.length?items:pending.chosen.map(asset=>({name:asset.name,path:asset.path,state:stateOf(job)}));
  rows.forEach((item,index)=>{const row=node('div',undefined,'operation-result-item'),text=node('div');text.append(node('strong',item.name||pending.chosen[index]?.name||planSource(item)||'项目'),node('span',item.error||planTarget(item)||planSource(item),'operation-result-path'));if(item.warning)text.append(node('span',item.warning,'operation-result-path'));if(item.recovery?.message)text.append(node('span',item.recovery.message,'operation-result-path'));row.append(text,badge(states[item.state]||item.state||'等待中',item.state==='completed'?'ready':item.state==='failed'?'stale':''));if(item.trashId&&pending.payload.scope==='local'&&item.state==='completed')row.append(button('撤销删除',()=>restoreFile(item.trashId,{...pending.chosen[index],...item}),'text-button'));root.append(row);});
}
async function pollFileJob(id){
  const active=fileOps.jobs.get(id);if(!active||active.polling)return;active.polling=true;clearTimeout(active.timer);
  try{const job=await api(API+'/file-ops/jobs/'+encodeURIComponent(id));active.job=job;renderFileJob(job,active.pending);if(running(job)){active.timer=setTimeout(()=>run(()=>pollFileJob(id)),1100);return;}
    if(active.pending.payload.operation==='move'&&fileOps.clipboard?.id===active.pending.clipboardId){
      const clipboard=fileOps.clipboard,scope=clipboard.scope,receipts=fileJobItems(job),sourceRef=item=>{const source=item.source;if(scope==='netdisk')return typeof source==='string'?{path:source,...(item.fsId?{fsId:String(item.fsId)}:{})}:null;return source&&typeof source==='object'&&source.projectId&&source.path?{projectId:source.projectId,path:source.path}:null;};
      const outcomes=new Map(receipts.map(item=>{const ref=sourceRef(item);return ref?[itemKey(scope,ref),item]:null;}).filter(Boolean));
      const retained=clipboard.items.filter(item=>outcomes.get(itemKey(scope,item))?.state!=='completed');
      for(const item of receipts){const ref=sourceRef(item);if(ref&&item.state!=='completed'&&!retained.some(entry=>itemKey(scope,entry)===itemKey(scope,ref)))retained.push(ref);}
      if(stateOf(job)==='completed')retained.length=0;
      const retryEntries=retained.map(ref=>{const existing=clipboard.entries.find(asset=>{const candidate=managementRef(asset,scope);return candidate&&itemKey(scope,candidate)===itemKey(scope,ref);});if(existing)return existing;const receipt=outcomes.get(itemKey(scope,ref))||{};return {...ref,name:receipt.name||ref.path.split('/').pop(),isDir:!!(receipt.isDir||receipt.isDirectory),type:receipt.isDir||receipt.isDirectory?'folder':'file',canManage:true,id:'file-retry:'+itemKey(scope,ref)};});
      fileOps.clipboard=retained.length?{...clipboard,items:retained,entries:retryEntries}:null;
    }
    if(!active.refreshed){active.refreshed=true;if(currentTab==='library'){clearSelection();await loadAssets();}else if(currentTab==='cloud'){cloudExplorer.selection.clear();await loadCloud();}syncSelection();syncCloudSelection();updateFileCommands();}
  }catch(error){$('file-operation-status-summary').textContent='暂时无法读取进度：'+error.message;active.timer=setTimeout(()=>run(()=>pollFileJob(id)),2500);}
  finally{active.polling=false;}
}
async function restoreFile(trashId,asset){const job=await api(API+'/file-ops/restore',{trashId}),id=String(job.jobId||job.id||'');if(!id)throw new Error('未返回恢复任务编号。');const pending={payload:{scope:'local',operation:'restore'},chosen:[asset],clipboardId:null};fileOps.jobs.set(id,{id,pending,job,timer:null,polling:false});await pollFileJob(id);}
function addFileContextCommands(add,scope,asset){
  const chosen=operationEntries(scope),canManage=chosen.length>0&&chosen.every(item=>managementRef(item,scope));add('剪切',()=>setFileClipboard('move',scope),'Ctrl+X',!canManage);add('复制',()=>setFileClipboard('copy',scope),'Ctrl+C',!canManage);const destination=isFolder(asset)?destinationRef(scope,asset):destinationRef(scope);add('粘贴'+(isFolder(asset)?'到此文件夹':''),()=>pasteFiles(scope,isFolder(asset)?asset:null),'Ctrl+V',!destination||fileOps.clipboard?.scope!==scope);add('重命名',()=>renameFile(scope),'F2',!canManage||!chosen.length);add('删除',()=>deleteFiles(scope),'Delete',!canManage);
}
function installFileDrag(element,asset,scope){
  element.draggable=!!managementRef(asset,scope);element.classList.toggle('cut-entry',isCutEntry(asset,scope));
  element.ondragstart=event=>{if(!managementRef(asset,scope)){event.preventDefault();return;}if(scope==='local'&&!isSelected(asset.id))selectEntry(asset,{});if(scope==='netdisk'&&!cloudExplorer.selection.has(asset.path))selectCloud(asset,{});const chosen=operationEntries(scope);if(!chosen.every(item=>managementRef(item,scope))){event.preventDefault();return;}const token=String(++fileOps.sequence)+'-'+Date.now();fileOps.drag={token,scope,entries:chosen.map(item=>({...item})),items:chosen.map(item=>managementRef(item,scope))};event.dataTransfer.setData(FILE_DRAG_TYPE,token);event.dataTransfer.effectAllowed='copyMove';};
  element.ondragend=()=>{fileOps.drag=null;document.querySelectorAll('.file-drop-target').forEach(item=>item.classList.remove('file-drop-target'));};
  if(isFolder(asset)){element.ondragover=event=>fileDragOver(event,scope,asset,element);element.ondragleave=()=>element.classList.remove('file-drop-target');element.ondrop=event=>fileDrop(event,scope,asset,element);}
}
function ownDrag(event,scope){return !!fileOps.drag&&fileOps.drag.scope===scope&&Array.from(event.dataTransfer?.types||[]).includes(FILE_DRAG_TYPE);}
function fileDragOver(event,scope,folder=null,element=null){if(!ownDrag(event,scope)){if(Array.from(event.dataTransfer?.types||[]).includes('Files')){event.preventDefault();event.dataTransfer.dropEffect='none';}return;}if(!destinationRef(scope,folder))return;event.preventDefault();event.stopPropagation();event.dataTransfer.dropEffect=event.ctrlKey||event.altKey||event.metaKey?'copy':'move';element?.classList.add('file-drop-target');}
function fileDrop(event,scope,folder=null,element=null){
  element?.classList.remove('file-drop-target');if(!ownDrag(event,scope)){if(Array.from(event.dataTransfer?.types||[]).includes('Files')){event.preventDefault();notice('外部文件请使用上传入口；拖放不会移动或删除原文件。');}return;}const drag=fileOps.drag;if(event.dataTransfer.getData(FILE_DRAG_TYPE)!==drag.token)return;const target=destinationRef(scope,folder);if(!target)return;event.preventDefault();event.stopPropagation();fileOps.drag=null;const operation=event.ctrlKey||event.altKey||event.metaKey?'copy':'move';run(()=>prepareFileOperation({scope,operation,items:drag.items,destination:target},drag.entries));
}

function localLocation(){return {projectId:$('project').value,path:explorer.path,kind,query:$('query').value.trim(),mode:$('search-mode').value};}
function sameLocation(a,b){return !!a&&['projectId','path','kind','query','mode'].every(k=>(a[k]||'')===(b[k]||''));}
function pushLocalHistory(value){if(sameLocation(explorer.history[explorer.index],value))return;explorer.history=explorer.history.slice(0,explorer.index+1);explorer.history.push({...value});if(explorer.history.length>100)explorer.history.shift();explorer.index=explorer.history.length-1;}
async function navigateLocal(value,{history=true}={}){
  closeContext();$('project').value=value.projectId||'';explorer.path=normalizeRelative(value.path||'');kind=value.kind||'';$('query').value=value.query||'';$('search-mode').value=value.mode||'text';offset=0;clearSelection();explorer.focusId='';explorer.anchorId='';current=null;++detailRequest;$('detail').hidden=true;$('detail-placeholder').hidden=false;$('detail-placeholder').textContent='选中文件后点击「属性」，查看标签和依赖。';renderCategoryState();$('project-heading').textContent=projectName();$('manifest').disabled=!$('project').value;$('scan').disabled=!$('project').value;$('address-input').hidden=true;$('address-breadcrumbs').hidden=false;if(history)pushLocalHistory(localLocation());await loadAssets();
}
function normalizeRelative(value){const path=String(value).replace(/\\/g,'/').replace(/^\/+|\/+$/g,'');if(path&&path.split('/').some(part=>part==='..'||part==='.'||!part))throw new Error('请输入工程内的文件夹路径。');return path;}
function localUpLocation(){if(explorer.path)return {...localLocation(),path:explorer.path.split('/').slice(0,-1).join('/'),query:'',kind:'',mode:'text'};return {projectId:'',path:'',query:'',kind:'',mode:'text'};}
async function navigateHistory(delta){const next=explorer.index+delta;if(next<0||next>=explorer.history.length)return;explorer.index=next;await navigateLocal(explorer.history[next],{history:false});}
function addressText(){return $('project').value?[projectName(),explorer.path].filter(Boolean).join('/'):'全部工程';}
function renderNavigation(){
  $('nav-back').disabled=explorer.index<=0;$('nav-forward').disabled=explorer.index>=explorer.history.length-1;$('nav-up').disabled=!$('project').value&&!explorer.path;
  const root=$('address-breadcrumbs');root.replaceChildren();const add=(name,locationValue)=>{if(root.childElementCount)root.append(icon('chevron'));const el=button(name,()=>navigateLocal(locationValue),'breadcrumb-button');el.title=name;root.append(el);};add('全部工程',{projectId:'',path:'',query:'',mode:'text',kind:''});
  if($('project').value){add(projectName(),{projectId:$('project').value,path:'',query:'',mode:'text',kind:''});let path='';for(const part of explorer.path.split('/').filter(Boolean)){path=[path,part].filter(Boolean).join('/');const target=path;add(part,{projectId:$('project').value,path:target,query:'',mode:'text',kind:''});}}
  if($('query').value.trim()||kind||$('search-mode').value==='semantic')root.append(node('span','搜索结果','address-search-state'));$('address-input').value=addressText();$('address-form').title=addressText();
}
function editAddress(){closeContext();$('address-input').value=addressText();$('address-input').hidden=false;$('address-breadcrumbs').hidden=true;$('address-input').focus();$('address-input').select();}
async function submitAddress(){
  const raw=$('address-input').value.trim().replace(/\\/g,'/').replace(/^\/+|\/+$/g,'');if(!raw||raw==='全部工程'){await navigateLocal({projectId:'',path:''});return;}
  const candidates=[...projectItems].sort((a,b)=>b.name.length-a.name.length),project=candidates.find(item=>raw===item.name||raw.startsWith(item.name+'/')||raw===item.id||raw.startsWith(item.id+'/'));
  if(project){const head=raw===project.id||raw.startsWith(project.id+'/')?project.id:project.name;await navigateLocal({projectId:project.id,path:raw.slice(head.length).replace(/^\//,'')});}
  else if($('project').value)await navigateLocal({projectId:$('project').value,path:raw});else throw new Error('路径请以左侧已有工程名称开始，例如「工程名/Assets/Textures」。');
}
async function openEntry(asset){
  if(!asset)return;closeContext();if(isFolder(asset)){await navigateLocal({projectId:asset.projectId||asset.id,path:asset.path||'',query:'',kind:'',mode:'text'});return;}
  await showPreview(asset);
}
function textLanguage(asset){
  const extension=String(asset.extension||asset.name?.split('.').pop()||'').replace(/^\./,'').toLowerCase(),map={txt:'plaintext',md:'markdown',markdown:'markdown',js:'javascript',mjs:'javascript',cjs:'javascript',ts:'typescript',tsx:'typescript',jsx:'javascript',json:'json',jsonc:'json',py:'python',cs:'csharp',lua:'lua',shader:'cpp',cginc:'cpp',hlsl:'cpp',glsl:'cpp',c:'c',h:'c',cc:'cpp',cpp:'cpp',hpp:'cpp',kt:'kotlin',java:'java',xml:'xml',html:'html',htm:'html',svg:'xml',css:'css',scss:'scss',less:'less',yaml:'yaml',yml:'yaml',toml:'ini',ini:'ini',sh:'shell',bash:'shell',zsh:'shell',bat:'bat',cmd:'bat',ps1:'powershell',sql:'sql',rs:'rust',go:'go',swift:'swift',php:'php',rb:'ruby',r:'r',csv:'plaintext',meta:'yaml',atlas:'plaintext',prefab:'yaml',unity:'yaml',mat:'yaml',asset:'yaml'};return map[extension]||'plaintext';
}
function disposeTextViewer(){textViewer.observer?.disconnect();textViewer.observer=null;textViewer.disposable?.dispose();textViewer.disposable=null;textViewer.editor?.dispose();textViewer.editor=null;textViewer.model?.dispose();textViewer.model=null;$('preview-editor-status').hidden=true;}
function loadMonaco(){
  if(window.monaco?.editor)return Promise.resolve(window.monaco);if(textViewer.loader)return textViewer.loader;
  textViewer.loader=new Promise((resolve,reject)=>{
    window.MonacoEnvironment={getWorkerUrl:()=>new URL('/static/vendor/monaco/vs/base/worker/workerMain.js',location.origin).href};
    const start=()=>{try{window.require.config({paths:{vs:'/static/vendor/monaco/vs'},preferScriptTags:true});window.require(['vs/editor/editor.main'],()=>resolve(window.monaco),reject);}catch(error){reject(error);}};
    if(window.require?.config){start();return;}const script=node('script');script.src='/static/vendor/monaco/vs/loader.js';script.async=true;script.onload=start;script.onerror=()=>reject(new Error('本地代码查看器资源未能加载。'));document.head.append(script);
  }).catch(error=>{textViewer.loader=null;throw error;});return textViewer.loader;
}
async function renderTextViewer(text,asset,request,truncated){
  const root=$('preview-content');root.replaceChildren();const host=node('div',undefined,'code-editor-host');host.setAttribute('aria-label','只读代码预览：'+asset.name);root.append(host);const language=textLanguage(asset);$('preview-editor-status').hidden=false;$('preview-editor-status').textContent=language+' · UTF-8 · 只读'+(truncated?' · 前 1 MB':'');
  try{
    const monaco=await loadMonaco();if(request!==explorer.previewRequest||!$('preview-dialog').open)return;
    if(monaco.languages.typescript){for(const defaults of [monaco.languages.typescript.javascriptDefaults,monaco.languages.typescript.typescriptDefaults])defaults?.setDiagnosticsOptions({noSemanticValidation:true,noSyntaxValidation:true,noSuggestionDiagnostics:true});}
    const registered=monaco.languages.getLanguages().some(item=>item.id===language)?language:'plaintext';textViewer.model=monaco.editor.createModel(text,registered,monaco.Uri.parse('inmemory://devhelper/'+request+'/'+encodeURIComponent(asset.name||'file.txt')));
    textViewer.editor=monaco.editor.create(host,{model:textViewer.model,theme:'vs-dark',readOnly:true,domReadOnly:true,contextmenu:true,lineNumbers:'on',folding:true,showFoldingControls:'always',minimap:{enabled:false},wordWrap:textViewer.wrap?'on':'off',fontFamily:'Cascadia Code, SFMono-Regular, Consolas, monospace',fontSize:13,lineHeight:22,scrollBeyondLastLine:false,smoothScrolling:false,renderValidationDecorations:'off',automaticLayout:false,links:false,stickyScroll:{enabled:false},padding:{top:15,bottom:20},selectionHighlight:true,occurrencesHighlight:'off',ariaLabel:'只读资源代码，使用 Control 或 Command 加 F 搜索'});
    const updateStatus=()=>{const position=textViewer.editor?.getPosition();$('preview-editor-status').textContent=registered+' · UTF-8 · 只读'+(position?' · 行 '+position.lineNumber+'，列 '+position.column:'')+(truncated?' · 前 1 MB':'');};textViewer.disposable=textViewer.editor.onDidChangeCursorPosition(updateStatus);updateStatus();
    const layout=()=>textViewer.editor?.layout();if(typeof ResizeObserver==='function'){textViewer.observer=new ResizeObserver(layout);textViewer.observer.observe(host);}requestAnimationFrame(layout);
  }catch(error){if(request!==explorer.previewRequest||!$('preview-dialog').open)return;const fallback=node('pre',text+(truncated?'\n\n…文件较大，仅预览前 1 MB。':''),'preview-text');root.replaceChildren(fallback);$('preview-editor-status').textContent=language+' · UTF-8 · 只读文本（代码查看器暂未加载）';}
}
function toggleTextWrap(){textViewer.wrap=!textViewer.wrap;textViewer.editor?.updateOptions({wordWrap:textViewer.wrap?'on':'off'});$('preview-wrap').setAttribute('aria-pressed',String(textViewer.wrap));}

function previewType(asset){
  if(typeof asset.previewKind==='string')return ['image','audio','video','pdf','text'].includes(asset.previewKind)?asset.previewKind:'none';
  const ext=String(asset.extension||asset.name?.split('.').pop()||'').replace(/^\./,'').toLowerCase(),mime=String(asset.previewMime||asset.mimeType||'').split(';')[0].toLowerCase();
  if(['png','jpg','jpeg','webp','gif','bmp','avif'].includes(ext)||/^image\/(png|jpeg|webp|gif|bmp|avif)$/.test(mime))return 'image';
  if(['mp4','webm','m4v','mov','ogv'].includes(ext)||/^video\//.test(mime))return 'video';
  if(['mp3','wav','m4a','aac','ogg','flac','opus','aiff'].includes(ext)||/^audio\//.test(mime))return 'audio';
  if(ext==='pdf'||mime==='application/pdf')return 'pdf';
  if(['txt','md','json','csv','log','yaml','yml','toml','ini','xml','cs','py','js','ts','lua','shader','glsl','hlsl','cginc','c','cpp','h','hpp','kt','java','css','meta','atlas'].includes(ext))return 'text';return 'none';
}
function previewFiles(){const items=explorer.preview?.asset.scope==='netdisk'?cloudExplorer.entries.filter(file=>!file.isDir).map(netdiskAsset):entries.filter(asset=>!isFolder(asset));return explorer.preview?.type==='image'?items.filter(asset=>previewType(asset)==='image'):items;}
function updatePreviewNavigation(){const items=previewFiles(),index=items.findIndex(a=>a.id===explorer.preview?.asset.id);$('preview-previous').disabled=index<=0;$('preview-next').disabled=index<0||index>=items.length-1;}
async function movePreview(delta){const items=previewFiles(),index=items.findIndex(a=>a.id===explorer.preview?.asset.id),next=items[index+delta];if(next)await showPreview(next);}
function previewMessage(title,description){const wrap=node('div',undefined,'preview-message');wrap.append(icon('file'),node('h2',title),node('p',description));$('preview-content').replaceChildren(wrap);return wrap;}
async function showPreview(initial){try{await renderFilePreview(initial);}catch(error){if(explorer.preview?.asset.id===initial.id&&$('preview-dialog').open)previewMessage('无法打开文件',error.message||'请稍后重试。');throw error;}}
async function renderFilePreview(initial){
  disposeTextViewer();const request=++explorer.previewRequest;explorer.preview={asset:initial,type:previewType(initial),pendingFetch:false};explorer.zoom=1;closeContext();if(!$('preview-dialog').open)$('preview-dialog').showModal();renderPreviewHeader(initial);previewMessage('正在打开…','');
  const asset=initial.scope==='netdisk'?initial:await api(API+'/get',{assetId:initial.id});if(request!==explorer.previewRequest||!$('preview-dialog').open)return;explorer.preview.asset=asset;explorer.preview.type=previewType(asset);renderPreviewHeader(asset);const type=explorer.preview.type;
  if(remoteOnly(asset)){
    const fetching=asset.scope==='netdisk'?cloudExplorer.previewJobs.has(String(asset.fsId)):catalogState.fetching.has(asset.id),wrap=previewMessage(fetching?'正在下载原素材…':'此素材保存在百度网盘',asset.storage?.canFetch?'只下载当前文件，完成后直接预览。分类与向量无需重新生成。':'云端索引尚未关联可下载的原素材。');
    if(asset.storage?.canFetch){const el=button(fetching?'下载中…':'下载并打开',()=>asset.scope==='netdisk'?fetchNetdiskAsset(asset):fetchPreviewAsset(asset),'preview-primary');el.disabled=fetching;wrap.append(el);}return;
  }
  const previewPrefix=asset.scope==='netdisk'?'/api/netdisk/jobs/':API+'/assets/';const url=safeUrl(asset.previewUrl||API+'/assets/'+encodeURIComponent(asset.id)+'/preview',previewPrefix);if(!url)throw new Error('无法读取安全预览地址。');const root=$('preview-content');root.replaceChildren();root.dataset.type=type;
  if(type==='image'){
    const stage=node('div',undefined,'preview-image-stage'),img=node('img');img.src=url;img.alt=asset.name;img.draggable=false;img.onload=()=>{if(request===explorer.previewRequest)updateImageZoom();};img.onerror=()=>{if(request===explorer.previewRequest)previewMessage('无法预览这张图片','可以下载原文件，或使用电脑中的应用打开。');};stage.append(img);root.append(stage);stage.addEventListener('wheel',event=>{if(event.ctrlKey||event.metaKey){event.preventDefault();setImageZoom(explorer.zoom*(event.deltaY<0?1.15:1/1.15));}},{passive:false});
  }else if(type==='video'||type==='audio'){
    const media=node(type);media.controls=true;media.preload='metadata';media.src=url;media.setAttribute('aria-label',asset.name);if(type==='video')media.playsInline=true;else{const cover=node('div',undefined,'audio-preview-cover');cover.append(icon('audio'),node('span',asset.name));root.append(cover);}media.onerror=()=>{if(request===explorer.previewRequest&&$('preview-dialog').open){const help=node('p','当前浏览器无法解码此格式，可下载或用本机应用打开。','preview-media-error');root.append(help);}};root.append(media);
  }else if(type==='pdf'){
    const frame=node('iframe');frame.src=asset.scope==='netdisk'?safeUrl('/api/netdisk/jobs/'+encodeURIComponent(asset.jobId)+'/preview','/api/netdisk/jobs/'):safeUrl(API+'/assets/'+encodeURIComponent(asset.id)+'/preview',API+'/assets/');frame.title=asset.name;frame.className='pdf-preview-frame';const fallback=node('a','无法显示时下载原文件','pdf-preview-fallback');fallback.href=safeUrl(asset.contentUrl||(API+'/assets/'+encodeURIComponent(asset.id)+'/content'),asset.scope==='netdisk'?'/api/netdisk/jobs/':API+'/assets/');fallback.download=asset.name||'';root.append(frame,fallback);
  }else if(type==='text'){
    previewMessage('正在读取文本…','最多显示 1 MB，不执行文件内容。');const response=await fetch(url,{cache:'no-store'});if(!response.ok)throw new Error('此格式不支持安全文本预览，请下载原文件。');const typeHeader=response.headers.get('content-type')||'',raw=await response.text();if(request!==explorer.previewRequest||!$('preview-dialog').open)return;let text=raw;if(typeHeader.includes('application/json')){try{const value=JSON.parse(raw);if(typeof value.text==='string')text=value.text;}catch{/* JSON files themselves remain plain text. */}}const truncated=response.headers.get('X-Preview-Truncated')==='true'||text.length>1048576;await renderTextViewer(text.slice(0,1048576),asset,request,truncated);
  }else{
    const wrap=previewMessage('此文件使用电脑应用打开',asset.name+' · '+bytes(asset.bytes));if(asset.canOpenNative)wrap.append(button('在本机应用中预览',()=>nativeAction(asset,'open'),'preview-primary'));if(asset.scope==='netdisk'){const save=node('a','下载原文件','preview-primary');save.href=safeUrl(asset.contentUrl,'/api/netdisk/jobs/');save.download=asset.name||'';wrap.append(save,button('查看属性',async()=>{$('preview-dialog').close();cloudProperties(asset);},'preview-secondary'));}else wrap.append(button('查看属性',async()=>{$('preview-dialog').close();await loadDetail(asset.id);},'preview-secondary'));
  }
}
function netdiskAsset(file){
  const cached=cloudExplorer.previews.get(String(file.fsId));if(cached)return cached;return {...file,scope:'netdisk',id:'netdisk:'+String(file.fsId),kind:previewType(file)==='image'?'image':previewType(file)==='video'?'video':previewType(file)==='audio'?'audio':'file',storage:{state:'remote-only',canFetch:true}};
}
function previewMatches(asset){return !!explorer.preview&&explorer.preview.asset.scope===asset.scope&&explorer.preview.asset.id===asset.id&&$('preview-dialog').open;}
function renderNetdiskDownload(asset,job){
  if(!previewMatches(asset))return;const percent=progressPercent(job),wrap=previewMessage('正在下载所选文件…',asset.name+' · '+bytes(asset.bytes));wrap.append(node('span',(states[stateOf(job)]||stateOf(job))+(percent?' · '+Math.round(percent)+'%':'')+'，完成后自动打开。关闭窗口后下载继续。'));
  const track=node('div',undefined,'progress-track'),bar=node('span');bar.style.width=percent+'%';track.append(bar);wrap.append(track);
  wrap.append(button('取消下载',async()=>{await api('/api/netdisk/cancel',{jobId:job.jobId});await pollNetdiskPreview(String(asset.fsId));},'preview-secondary'));
}
async function fetchNetdiskAsset(asset){
  const key=String(asset.fsId),active=cloudExplorer.previewJobs.get(key);if(active){await pollNetdiskPreview(key);return;}const wrap=previewMessage('正在创建下载任务…','只下载 '+asset.name+'。');wrap.append(node('span','完成后将在这里打开。'));
  try{const job=await api('/api/netdisk/download',{fsId:key,name:asset.name}),jobId=String(job.jobId||job.id||'');if(!jobId)throw new Error('未能读取网盘下载任务编号。');cloudExplorer.previewJobs.set(key,{jobId,asset,timer:null,failures:0,polling:false});renderNetdiskDownload(asset,{...job,jobId});await pollNetdiskPreview(key);}
  catch(error){if(previewMatches(asset)){const message=previewMessage('下载未完成',error.message);message.append(button('重试',()=>fetchNetdiskAsset(asset),'preview-primary'));}throw error;}
}
async function pollNetdiskPreview(key){
  const active=cloudExplorer.previewJobs.get(key);if(!active||active.polling)return;active.polling=true;clearTimeout(active.timer);
  try{
    const job=await api('/api/netdisk/jobs/'+encodeURIComponent(active.jobId));active.failures=0;
    if(running(job)){renderNetdiskDownload(active.asset,{...job,jobId:active.jobId});active.timer=setTimeout(()=>run(()=>pollNetdiskPreview(key)),1400);return;}
    cloudExplorer.previewJobs.delete(key);
    if(stateOf(job)==='completed'){
      if(!job.contentUrl)throw new Error('下载已完成，但服务没有返回文件地址。请在任务与传输中查看。');
      const ready={...active.asset,jobId:active.jobId,contentUrl:job.contentUrl,previewUrl:job.previewUrl||'/api/netdisk/jobs/'+encodeURIComponent(active.jobId)+'/preview',previewKind:job.previewKind||previewType(active.asset),previewMime:job.previewMime,storage:{state:'cached',canFetch:false}};
      cloudExplorer.previews.set(key,ready);if(previewMatches(active.asset))await showPreview(ready);else notice(active.asset.name+' 已下载，双击可打开。');
    }else if(previewMatches(active.asset)){
      const message=previewMessage(stateOf(job)==='cancelled'?'下载已取消':'下载未完成',job.error||states[stateOf(job)]||'请稍后重试。');message.append(button('重新下载',()=>fetchNetdiskAsset(active.asset),'preview-primary'));
    }
  }catch(error){
    if(cloudExplorer.previewJobs.has(key)&&++active.failures<=5){active.timer=setTimeout(()=>run(()=>pollNetdiskPreview(key)),2000);if(previewMatches(active.asset))previewMessage('暂时无法读取下载进度','下载可能仍在后台进行，正在重新连接…');}
    else{cloudExplorer.previewJobs.delete(key);if(previewMatches(active.asset)){const message=previewMessage('无法读取下载结果',error.message);message.append(button('查看任务',async()=>{$('preview-dialog').close();tab('history');},'preview-primary'));}notice(error.message,true);}
  }finally{active.polling=false;}
}

function renderPreviewHeader(asset){
  $('preview-title').textContent=asset.name||'文件预览';$('preview-meta').textContent=(kindLabels[asset.kind]||asset.extension||'文件')+' · '+bytes(asset.bytes);$('preview-path').textContent=asset.path||asset.name||'';const text=previewType(asset)==='text';$('preview-find').hidden=!text;$('preview-wrap').hidden=!text;$('preview-editor-status').hidden=!text;$('preview-key-hint').hidden=text;const image=previewType(asset)==='image';for(const id of ['preview-zoom-out','preview-zoom','preview-zoom-in'])$(id).hidden=!image;$('preview-zoom').textContent='适应';$('preview-download').hidden=remoteOnly(asset);if(!remoteOnly(asset))$('preview-download').href=safeUrl(asset.contentUrl||API+'/assets/'+encodeURIComponent(asset.id)+'/content',asset.scope==='netdisk'?'/api/netdisk/jobs/':API+'/assets/');else $('preview-download').removeAttribute('href');updatePreviewNavigation();
}
function updateImageZoom(){const image=$('preview-content').querySelector('.preview-image-stage img');if(!image)return;image.style.transform=explorer.zoom===1?'':'scale('+explorer.zoom+')';image.classList.toggle('zoomed',explorer.zoom>1);$('preview-zoom').textContent=explorer.zoom===1?'适应':Math.round(explorer.zoom*100)+'%';}
function setImageZoom(value){explorer.zoom=Math.min(8,Math.max(.2,value));updateImageZoom();}
async function fetchPreviewAsset(asset){
  if(!asset.storage?.canFetch||catalogState.fetching.has(asset.id))return;catalogState.fetching.set(asset.id,'creating');explorer.preview.pendingFetch=true;const wrap=previewMessage('正在下载并校验…','只下载当前素材，完成后自动打开。');wrap.append(node('span','可关闭预览继续浏览，下载在后台进行。'));
  try{const job=await api(API+'/catalog/fetch',{assetId:asset.id}),id=String(job.jobId||job.id||'');if(!id)throw new Error('未能读取下载任务编号。');catalogState.fetching.set(asset.id,id);rememberCatalogJob(job);await pollCatalogJobs();}catch(error){catalogState.fetching.delete(asset.id);if(explorer.preview?.asset.id===asset.id){explorer.preview.pendingFetch=false;const message=previewMessage('下载未完成',error.message);message.append(button('重试',()=>fetchPreviewAsset(asset),'preview-primary'));}throw error;}
}
async function nativeAction(asset,action){if(!(action==='reveal'?asset.canReveal:asset.canOpenNative))throw new Error('此文件当前不可使用这项本机操作。');const result=await api(API+'/assets/'+encodeURIComponent(asset.id)+'/'+action,{});notice(result.message||(action==='reveal'?'已在系统文件管理器中显示。':'已在本机应用中预览。'));}
function downloadAsset(asset){if(remoteOnly(asset))return showPreview(asset);const a=node('a');a.href=safeUrl(asset.contentUrl||API+'/assets/'+encodeURIComponent(asset.id)+'/content',API+'/assets/');a.download=asset.name||'';document.body.append(a);a.click();a.remove();}
async function copyPath(value){try{await navigator.clipboard.writeText(value);notice('路径已复制。');}catch{showAction({title:'复制相对路径',description:'选中下面的路径后复制。',label:'文件路径',value,submit:'关闭',work:async()=>{}});$('action-value').focus();$('action-value').select();}}
function closeContext(){const menu=$('file-context-menu');menu.hidden=true;menu.replaceChildren();}
function showContext(event,asset,scope){
  closeContext();if(scope==='library'&&!isSelected(asset.id))selectEntry(asset,{});if(scope==='cloud'&&!cloudExplorer.selection.has(asset.path))selectCloud(asset,{});const menu=$('file-context-menu');
  const add=(label,work,shortcut='',disabled=false)=>{const el=button('',()=>{closeContext();return work();},'context-command');el.setAttribute('role','menuitem');el.disabled=disabled;el.append(node('span',label));if(shortcut)el.append(node('kbd',shortcut));menu.append(el);};
  if(scope==='cloud'){add(asset.isDir?'打开文件夹':'打开',()=>cloudAction(asset),'Enter');addFileContextCommands(add,'netdisk',asset);menu.append(node('hr'));add('复制路径',()=>copyPath(asset.path));add('属性',()=>cloudProperties(asset));}
  else{add(isFolder(asset)?'打开文件夹':'打开',()=>openEntry(asset),'Enter');addFileContextCommands(add,'local',asset);menu.append(node('hr'));if(!isFolder(asset)){add('全屏预览',()=>showPreview(asset));add(remoteOnly(asset)?'下载并打开':'下载原文件',()=>remoteOnly(asset)?showPreview(asset):downloadAsset(asset));}add('复制相对路径',()=>copyPath(asset.path||asset.name));if(!isFolder(asset)&&(asset.canOpenNative||asset.canReveal)){menu.append(node('hr'));if(asset.canOpenNative)add('在本机应用中预览',()=>nativeAction(asset,'open'));if(asset.canReveal)add('在系统文件管理器中显示',()=>nativeAction(asset,'reveal'));}menu.append(node('hr'));add('属性',()=>isFolder(asset)?folderProperties(asset):loadDetail(asset.id),'Alt+Enter');}
  menu.hidden=false;menu.style.left='0';menu.style.top='0';const box=menu.getBoundingClientRect();menu.style.left=Math.max(8,Math.min(event.clientX,innerWidth-box.width-8))+'px';menu.style.top=Math.max(8,Math.min(event.clientY,innerHeight-box.height-8))+'px';menu.querySelector('button:not(:disabled)')?.focus({preventScroll:true});
}
function folderProperties(asset){openInspector();current=null;$('detail').hidden=true;$('detail-placeholder').hidden=false;$('detail-placeholder').replaceChildren(node('strong',asset.name),node('p','类型：文件夹'),node('p','路径：'+(asset.path||'工程根目录')),node('p','工程：'+(asset.projectName||asset.name||projectName())),node('p',asset.childCount!==undefined?'包含 '+asset.childCount+' 项':''));}
function cloudProperties(file){showAction({title:'网盘文件属性',description:(file.isDir?'文件夹':'文件')+' · '+(file.isDir?'':bytes(file.bytes))+' · 修改于 '+time(file.modifiedAt),label:'网盘路径',value:file.path,submit:'关闭',work:async()=>{}});}
function syncCloudSelection(){if(cloudTable)cloudTable.getRows().forEach(row=>{const f=row.getData(),el=row.getElement();el.classList.toggle('tabulator-selected',cloudExplorer.selection.has(f.path));el.classList.toggle('file-focused',f.path===cloudExplorer.focusId);el.setAttribute('aria-selected',String(cloudExplorer.selection.has(f.path)));el.classList.toggle('cut-entry',isCutEntry(f,'netdisk'));});updateFileCommands();}
function selectCloud(file,event={}){
  const additive=event.ctrlKey||event.metaKey;if(event.shiftKey&&cloudExplorer.anchorId){const a=cloudExplorer.entries.findIndex(f=>f.path===cloudExplorer.anchorId),b=cloudExplorer.entries.findIndex(f=>f.path===file.path);if(!additive)cloudExplorer.selection.clear();if(a>=0&&b>=0)cloudExplorer.entries.slice(Math.min(a,b),Math.max(a,b)+1).forEach(f=>cloudExplorer.selection.add(f.path));}
  else{if(!additive)cloudExplorer.selection.clear();if(additive&&cloudExplorer.selection.has(file.path))cloudExplorer.selection.delete(file.path);else cloudExplorer.selection.add(file.path);cloudExplorer.anchorId=file.path;}
  cloudExplorer.focusId=file.path;$('cloud-table').focus({preventScroll:true});syncCloudSelection();
}
function cloudPathValue(value){let path=String(value||'').replace(/\/+$/,'');if(path===cloudScopeRoot)path='';return path;}
function pushCloudHistory(path){path=cloudPathValue(path);if(cloudExplorer.history[cloudExplorer.index]===path)return;cloudExplorer.history=cloudExplorer.history.slice(0,cloudExplorer.index+1);cloudExplorer.history.push(path);if(cloudExplorer.history.length>100)cloudExplorer.history.shift();cloudExplorer.index=cloudExplorer.history.length-1;}
async function navigateCloud(path,{history=true}={}){closeContext();$('remote-dir').value=cloudPathValue(path);cloudOffset=0;cloudExplorer.selection.clear();cloudExplorer.focusId='';cloudExplorer.anchorId='';if(history)pushCloudHistory($('remote-dir').value);await loadCloud();}
async function cloudHistory(delta){const index=cloudExplorer.index+delta;if(index<0||index>=cloudExplorer.history.length)return;cloudExplorer.index=index;await navigateCloud(cloudExplorer.history[index],{history:false});}
function parentCloudPath(){let path=cloudPathValue($('remote-dir').value);if(path.startsWith(cloudScopeRoot+'/'))path=path.slice(cloudScopeRoot.length+1);const parent=path.split('/').filter(Boolean).slice(0,-1).join('/');return parent;}
function renderCloudNavigation(){
  $('cloud-back').disabled=cloudExplorer.index<=0;$('cloud-forward').disabled=cloudExplorer.index>=cloudExplorer.history.length-1;$('cloud-up').disabled=!cloudPathValue($('remote-dir').value);const root=$('cloud-breadcrumbs');root.replaceChildren();const base=button('应用目录',()=>navigateCloud(''),'breadcrumb-button');base.prepend(icon('cloud'));root.append(base);let path=cloudPathValue($('remote-dir').value);if(path.startsWith(cloudScopeRoot+'/'))path=path.slice(cloudScopeRoot.length+1);let sofar='';for(const part of path.split('/').filter(Boolean)){sofar=[sofar,part].filter(Boolean).join('/');const target=sofar;root.append(icon('chevron'),button(part,()=>navigateCloud(target),'breadcrumb-button'));}
}
function inputTarget(target){return !!target?.closest('input,textarea,select,[contenteditable="true"],[role="textbox"]');}
function keyboardFiles(event){
  const editable=inputTarget(event.target);if($('preview-dialog').open){if(event.target.closest('.monaco-editor,.code-editor-host'))return;if(event.key==='Escape'){event.preventDefault();$('preview-dialog').close();return;}if(!editable&&!event.target.closest('video,audio')){if(event.key==='ArrowLeft'){event.preventDefault();run(()=>movePreview(-1));}else if(event.key==='ArrowRight'){event.preventDefault();run(()=>movePreview(1));}}return;}
  if([...document.querySelectorAll('dialog[open]')].length||editable)return;
  if(!$('file-context-menu').hidden){if(event.key==='Escape'){event.preventDefault();closeContext();return;}if(['ArrowDown','ArrowUp','Home','End'].includes(event.key)){event.preventDefault();const controls=[...$('file-context-menu').querySelectorAll('button:not(:disabled)')],index=controls.indexOf(document.activeElement),next=event.key==='Home'?0:event.key==='End'?controls.length-1:(index+(event.key==='ArrowDown'?1:-1)+controls.length)%controls.length;controls[next]?.focus();return;}}
  if(currentTab!=='library'&&currentTab!=='cloud')return;const local=currentTab==='library',root=local?$('asset-content'):$('cloud-table'),active=root.contains(event.target)||event.target===document.body;
  if((event.ctrlKey||event.metaKey)&&event.key.toLowerCase()==='l'&&local){event.preventDefault();editAddress();return;}
  if(event.altKey&&['ArrowLeft','ArrowRight','ArrowUp'].includes(event.key)){event.preventDefault();if(local)run(()=>event.key==='ArrowUp'?navigateLocal(localUpLocation()):navigateHistory(event.key==='ArrowLeft'?-1:1));else run(()=>event.key==='ArrowUp'?navigateCloud(parentCloudPath()):cloudHistory(event.key==='ArrowLeft'?-1:1));return;}
  if(!active)return;const items=local?entries:cloudExplorer.entries,focus=local?explorer.focusId:cloudExplorer.focusId,id=local?'id':'path',focused=items.find(a=>a[id]===focus),selected=local?selectedEntry():items.find(a=>cloudExplorer.selection.has(a.path));
  const command=event.ctrlKey||event.metaKey;if(command&&['c','x','v'].includes(event.key.toLowerCase())){event.preventDefault();const letter=event.key.toLowerCase();run(()=>letter==='v'?pasteFiles(local?'local':'netdisk'):setFileClipboard(letter==='x'?'move':'copy',local?'local':'netdisk'));return;}
  if(event.key==='F2'){event.preventDefault();run(()=>renameFile(local?'local':'netdisk'));return;}if(event.key==='Delete'){event.preventDefault();run(()=>deleteFiles(local?'local':'netdisk'));return;}
  if(event.key==='Escape'){event.preventDefault();if(local)clearSelection();else{cloudExplorer.selection.clear();syncCloudSelection();}return;}
  if(event.key==='Enter'){event.preventDefault();const asset=focused||selected;if(asset)run(()=>event.altKey?(local?isFolder(asset)?folderProperties(asset):loadDetail(asset.id):cloudProperties(asset)):local?openEntry(asset):cloudAction(asset));return;}
  if(event.key===' '){event.preventDefault();if(focused){if(local){explorer.anchorId=focused.id;toggleSelection(focused.id,!isSelected(focused.id));}else selectCloud(focused,{ctrlKey:true});}return;}
  if((event.ctrlKey||event.metaKey)&&event.key.toLowerCase()==='a'){event.preventDefault();if(local){for(const asset of items)toggleSelection(asset.id,true);}else{items.forEach(asset=>cloudExplorer.selection.add(asset.path));syncCloudSelection();}return;}
  if(['ArrowUp','ArrowDown','Home','End'].includes(event.key)){event.preventDefault();let index=items.findIndex(a=>a[id]===focus);index=event.key==='Home'?0:event.key==='End'?items.length-1:Math.max(0,Math.min(items.length-1,index+(event.key==='ArrowDown'?1:-1)));const asset=items[index];if(asset){if(event.ctrlKey||event.metaKey){if(local)focusFiles(asset.id);else{cloudExplorer.focusId=asset.path;syncCloudSelection();}}else if(local)selectEntry(asset,event);else selectCloud(asset,event);if(local&&view==='grid')$('asset-grid').querySelectorAll('.asset-card')[index]?.scrollIntoView({block:'nearest'});else (local?table:cloudTable).scrollToRow(asset[id],'nearest',false).catch(()=>{});}return;}
  if(event.key==='Backspace'){event.preventDefault();run(()=>local?navigateLocal(localUpLocation()):navigateCloud(parentCloudPath()));}
}

function fact(label,value){const wrap=node('div');wrap.append(node('dt',label),node('dd',value||'—'));return wrap;}
function annotationIsCurrent(asset,annotation){return !!annotation&&!annotation.stale&&typeof asset.sha256==='string'&&annotation.sha256===asset.sha256;}
function validAnnotation(asset,source){return (asset.annotations||[]).find(annotation=>annotation.source===source&&annotationIsCurrent(asset,annotation))||null;}
function annotationHasLabels(annotation){return !!annotation&&((typeof annotation.label==='string'&&!!annotation.label.trim())||(Array.isArray(annotation.categories)&&annotation.categories.some(tag=>typeof tag==='string'&&!!tag.trim())));}
function manualNote(asset,purpose){const value={source:'manual',purpose},manual=validAnnotation(asset,'manual');if(manual)for(const field of ['label','categories','ocrText','evidence','confidence'])if(Object.prototype.hasOwnProperty.call(manual,field))value[field]=Array.isArray(manual[field])?[...manual[field]]:manual[field];return value;}
function renderDetailAnnotations(asset){
  const root=$('detail-labels');root.replaceChildren();const annotations=asset.annotations||[],manual=validAnnotation(asset,'manual'),ai=validAnnotation(asset,'ai');
  for(const annotation of [manual,ai].filter(Boolean)){const cls=annotation.source==='manual'?'ready':'ai';root.append(badge(annotation.source==='manual'?(annotationHasLabels(annotation)?'人工标注':'人工备注'):'AI 分类',cls));for(const tag of Array.isArray(annotation.categories)?annotation.categories:[])root.append(badge(tag,cls));if(annotation.label)root.append(badge(annotation.label,cls));}
  for(const annotation of annotations.filter(annotation=>['manual','ai'].includes(annotation.source)&&!annotationIsCurrent(asset,annotation))){root.append(badge(annotation.source==='manual'?'人工旧分类（已失效）':'AI 旧分类（已失效）','stale'));}
  $('detail-purpose').textContent=manual?.purpose||ai?.purpose||(manual||ai?'尚未填写用途说明。':'尚未进行分类。可添加用途备注或选中资源进行 AI 分类。');$('purpose').value=manual?.purpose||'';
}
async function saveNote(){if(!current)return;const id=current.id,annotation=manualNote(current,$('purpose').value);await api(API+'/annotations',{assetId:id,annotation});notice('用途备注已保存。');if(current?.id===id)await loadDetail(id);}
async function loadDetail(id){const request=++detailRequest;openInspector();$('detail-placeholder').hidden=false;$('detail-placeholder').textContent='正在读取资源…';$('detail').hidden=true;const asset=await api(API+'/get',{assetId:id});if(request!==detailRequest)return;current=asset;$('detail-placeholder').hidden=true;$('detail').hidden=false;$('detail-title').textContent=asset.name;$('detail-kind').textContent=kindLabels[asset.kind]||asset.kind;$('detail-path').textContent=asset.path;renderAssetStorage(asset);$('detail-preview').replaceChildren();if(!remoteOnly(asset)&&asset.kind==='image'&&asset.thumbnailUrl){const img=node('img');img.src=safeUrl(asset.thumbnailUrl,API+'/assets/');img.alt=asset.name;img.onerror=()=>img.replaceWith(icon('image'));$('detail-preview').append(img);}else if(remoteOnly(asset)){const placeholder=node('div',undefined,'remote-preview');placeholder.append(icon(kindIcons[asset.kind]||'file'),node('span',asset.storage?.canFetch?'原素材在网盘，下载后可预览':'此索引尚未关联已上传的原素材'));$('detail-preview').append(placeholder);}else $('detail-preview').append(icon(kindIcons[asset.kind]||'file'));$('detail-facts').replaceChildren(fact('文件大小',bytes(asset.bytes)),fact('文件格式',asset.extension||asset.mimeType),fact('图片尺寸',asset.image?.width?asset.image.width+' × '+asset.image.height:'—'),fact('修改时间',time(asset.modifiedAt,true)));$('detail-palette').replaceChildren();for(const color of (asset.image?.palette||[]).slice(0,8)){const value=typeof color==='string'?color:color.hex||color.color;if(!/^#[a-fA-F0-9]{6}$/.test(value||''))continue;const swatch=node('span',undefined,'color-swatch');swatch.style.backgroundColor=value;swatch.title=value;$('detail-palette').append(swatch);}renderDetailAnnotations(asset);$('detail-vectors').replaceChildren();for(const vector of asset.embeddings||[]){const p=node('p',vector.model+' · '+vector.dimensions+' 维');$('detail-vectors').append(p);}if(!asset.embeddings?.length)$('detail-vectors').append(node('p','尚未生成描述向量。'));renderVectorSearch(asset);$('detail-ocr').textContent=asset.ocr?.text||'尚未识别文字。';$('ocr').disabled=asset.kind!=='image'||remoteOnly(asset);$('detail-hashes').replaceChildren(node('p','MD5  '+asset.md5,'hash'),node('p','SHA-256  '+asset.sha256,'hash'));$('details-json').textContent=JSON.stringify(asset,null,2);$('related').replaceChildren();$('related').hidden=true;}
async function loadRelated(action,args){if(!current)return;const id=current.id;$('related').hidden=false;$('related').replaceChildren(node('p','正在读取…','hint'));const result=await api(API+'/'+action,{assetId:id,...args});if(current?.id!==id)return;const root=$('related');root.replaceChildren();const items=result.items||(result.nodes||[]).filter(a=>a.id!==id);if(!items.length)root.append(node('p','没有找到关联资源。','hint'));for(const asset of items.slice(0,50)){const el=button('',()=>loadDetail(asset.id),'related-item');el.append(node('span',asset.name||asset.path),typeof asset.score==='number'?badge((asset.score*100).toFixed(0)+'%','score'):icon('chevron'));el.title=asset.path||'';root.append(el);}if(result.unresolved?.length)root.append(node('p',result.unresolved.length+' 条引用未能唯一定位，未自动猜测。','hint'));if(result.truncated)root.append(node('p','关联结果较多，仅展示部分。','hint'));}
async function scan(projectId){if(!projectId)throw new Error('请先选择一个工程。');if(scanBusy)return;scanBusy=true;$('scan').disabled=true;let task;try{task=await api(API+'/scan',{projectId});}catch(error){scanBusy=false;$('scan').disabled=false;throw error;}notice('开始扫描 '+projectName()+'…');async function poll(){try{const job=await api(API+'/jobs/'+encodeURIComponent(task.jobId||task.id));if(running(job)){notice('正在扫描：'+Number(job.progress?.discovered||job.progress?.indexed||0).toLocaleString('zh-CN')+' 个文件');scanTimer=setTimeout(()=>run(poll),1100);}else{scanBusy=false;$('scan').disabled=false;notice(stateOf(job)==='completed'?'工程资源索引已更新。':stateOf(job)==='completed-with-errors'?'索引已更新，部分文件未能分析。':job.error||'扫描已结束。',!['completed','completed-with-errors'].includes(stateOf(job)));await loadAssets();}}catch(error){scanBusy=false;$('scan').disabled=false;throw error;}}await poll();}
function showAction({title,description,label,value='',submit,work}){$('action-title').textContent=title;$('action-description').textContent=description;$('action-label').textContent=label;$('action-value').value=value;$('action-submit').textContent=submit;actionWork=work;$('action-dialog').showModal();}
async function startAi(kindValue){const body={kind:kindValue,force:$('force-ai').checked};if($('task-scope').value==='project'){if(!$('project').value)throw new Error('请先选择一个工程。');body.projectId=$('project').value;}else{if(!selection.size)throw new Error('请先选择要处理的资源。');body.assetIds=[...selection];}const result=await api(API+'/ai/jobs',body);notice('已创建'+(kindValue==='embed'?'向量生成':kindValue==='classify'?'AI 分类':'向量与分类')+'任务，可继续浏览其他资源。');await pollAi();return result;}
function jobCounts(job){const p=job.progress&&typeof job.progress==='object'?job.progress:{};return {total:p.total??p.totalCount??job.totalCount??job.total??job.assetCount??job.items?.length??0,completed:job.completedCount??job.completed??p.succeeded??p.completed??p.completedCount??0,skipped:job.skippedCount??job.skipped??p.skipped??p.skippedCount??0,failed:job.failedCount??job.failed??p.failed??p.failedCount??0,cancelled:job.cancelledCount??job.cancelled??p.cancelled??p.cancelledCount??0};}
function progressPercent(job){if(typeof job.progress==='number')return Math.min(100,Math.max(0,job.progress));const c=jobCounts(job),processed=job.progress?.processed??(c.completed+c.skipped+c.failed+c.cancelled);return c.total?Math.min(100,Math.max(0,processed*100/c.total)):0;}
function aiKind(kindValue){return {embed:'生成向量',classify:'AI 分类',both:'向量与 AI 分类'}[kindValue]||'资源整理';}
function countsText(job){const c=jobCounts(job),processed=job.progress?.processed??(c.completed+c.skipped+c.failed+c.cancelled);return (job.kind==='both'?'已处理 '+processed+' / '+c.total+' 步（'+(job.assetCount??Math.ceil(c.total/2))+' 个资源）':'已处理 '+processed+' / '+c.total+' 个资源')+'，成功 '+c.completed+(c.skipped?'，跳过 '+c.skipped:'')+(c.failed?'，失败 '+c.failed:'')+(c.cancelled?'，取消 '+c.cancelled:'')+(job.progress?.interrupted?'，中断 '+job.progress.interrupted:'');}
async function pollAi(){if(aiPollBusy)return;aiPollBusy=true;try{const value=await api(API+'/ai/jobs');const items=value.items||value.jobs||[];const wasActive=activeAiJobs.length;activeAiJobs=items.filter(running);$('active-count').hidden=!activeAiJobs.length;$('active-count').textContent=activeAiJobs.length;$('active-task').hidden=!activeAiJobs.length;const job=activeAiJobs[0];if(job){$('active-task-title').textContent=aiKind(job.kind)+' · '+(states[stateOf(job)]||stateOf(job));$('active-task-summary').textContent=countsText(job);$('active-task-bar').style.width=progressPercent(job)+'%';$('active-task-cancel').onclick=()=>run(async()=>{await api(API+'/ai/jobs/'+encodeURIComponent(job.jobId||job.id)+'/cancel',{});await pollAi();});}if(currentTab==='history')renderAiJobs(items);if(wasActive&&!activeAiJobs.length){notice('资源整理任务已结束，可在任务页查看完成、跳过与失败数量。');if(currentTab==='library')await loadAssets();if(current)await loadDetail(current.id);}}finally{aiPollBusy=false;}}
function renderAiJobs(items){const root=$('ai-jobs');root.replaceChildren();if(!items.length)root.append(node('p','选中素材或整个工程，即可创建向量与分类任务。','job-empty'));for(const job of items){const wrap=node('article',undefined,'job-item');const head=node('div',undefined,'row spread');head.append(node('strong',aiKind(job.kind),'job-title'),badge(states[stateOf(job)]||stateOf(job),stateOf(job)==='completed'?'ready':stateOf(job)==='failed'?'stale':''));wrap.append(head,node('p',countsText(job)+' · '+time(job.createdAt),'job-meta'));if(job.model||job.embeddingModel)wrap.append(node('p',job.model||job.embeddingModel,'job-meta'));const track=node('div',undefined,'progress-track'),bar=node('span');bar.style.width=progressPercent(job)+'%';track.append(bar);wrap.append(track);if(job.error)wrap.append(node('p',job.error,'job-error'));const errors=(job.items||[]).filter(a=>a.error);for(const item of errors.slice(0,3))wrap.append(node('p',(item.name||item.assetId||'资源')+'：'+item.error,'job-error'));const actions=node('div',undefined,'job-actions');if(running(job))actions.append(button('取消任务',async()=>{await api(API+'/ai/jobs/'+encodeURIComponent(job.jobId||job.id)+'/cancel',{});await pollAi();},'text-button'));else if(jobCounts(job).failed||['failed','interrupted','cancelled','incomplete'].includes(stateOf(job)))actions.append(button('重试未完成资源',async()=>{await api(API+'/ai/jobs/'+encodeURIComponent(job.jobId||job.id)+'/retry',{});await pollAi();},'text-button'));wrap.append(actions);root.append(wrap);}}
const fallbackPresets={embedding:{openai:{baseUrl:'https://api.openai.com/v1',model:'text-embedding-3-small',batchSize:16},siliconflow:{baseUrl:'https://api.siliconflow.cn/v1',model:'BAAI/bge-m3',batchSize:16},dashscope:{baseUrl:'',model:'',batchSize:16,note:'请填写百炼控制台为你的地域与工作空间提供的 API 地址。'}},classification:{deepseek:{baseUrl:'https://api.deepseek.com',model:'',batchSize:4,mode:'chat'},openai:{baseUrl:'https://api.openai.com/v1',model:'',batchSize:4},siliconflow:{baseUrl:'https://api.siliconflow.cn/v1',model:'',batchSize:4,mode:'chat'}}};
function updateModeHint(){const visual=$('classification-mode').value==='responses-vision';$('classification-mode-hint').textContent=visual?'图片会压缩成预览图，通过二进制文件上传后用于视觉分类；其他格式使用描述与文本。':'仅发送资源描述、依赖摘要与可读文本，不发送图片像素。';}
function updateModelSummary(){const embedding=modelConfig.embedding||{},classification=modelConfig.classification||{};$('model-dot').classList.toggle('ready',embedding.configured||classification.configured);const names=[];if(embedding.configured)names.push('描述向量：'+embedding.model);if(classification.configured)names.push('分类：'+classification.model+(classification.mode==='responses-vision'?'（发送图片预览）':'（发送描述与文本）'));$('model-summary').textContent=names.length?names.join('；')+'。开始任务即发送到所选 API。':'配置模型 API，即可直接批量生成描述向量与 AI 分类。';}
async function loadModelConfig(){modelConfig=await api(API+'/ai/config');for(const kindValue of ['embedding','classification']){const c=modelConfig[kindValue]||{};for(const field of ['baseUrl','model','batchSize','dimensions']){const el=$(kindValue+'-'+field);if(el)el.value=c[field]??(field==='batchSize'?(kindValue==='embedding'?16:4):'');}$(kindValue+'-apiKey').value='';$(kindValue+'-apiKey').placeholder=c.hasApiKey?'密钥已保存，留空保留':'填写模型 API Key';$(kindValue+'-clearKey').checked=false;$(kindValue+'-status').textContent=c.configured?'已配置':'未配置';$(kindValue+'-status').classList.toggle('ready',!!c.configured);$(kindValue+'-preset').value='custom';}for(const group of ['embedding','classification']){const presets=(modelConfig.presets||[]).filter(p=>p.kind===group);if(presets.length){$(group+'-preset').replaceChildren(new Option('自定义 OpenAI 兼容服务','custom'));for(const preset of presets)$(group+'-preset').add(new Option(preset.name,preset.id));}}if($('classification-mode'))$('classification-mode').value=modelConfig.classification?.mode||'chat';$('embedding-test-result').textContent='';$('classification-test-result').textContent='';updateModeHint();updateModelSummary();}
function providerPayload(kindValue){const baseUrl=$(kindValue+'-baseUrl').value.trim(),model=$(kindValue+'-model').value.trim(),apiKey=$(kindValue+'-apiKey').value.trim(),clearKey=$(kindValue+'-clearKey').checked;if(!baseUrl&&!model&&!apiKey&&!clearKey&&!modelConfig[kindValue]?.configured)return null;const value={baseUrl,model,batchSize:Number($(kindValue+'-batchSize').value)|| (kindValue==='embedding'?16:4)};if(apiKey)value.apiKey=apiKey;if(clearKey)value.clearKey=true;if(kindValue==='embedding'&&$('embedding-dimensions').value)value.dimensions=Number($('embedding-dimensions').value);else if(kindValue==='embedding')value.dimensions=null;if(kindValue==='classification')value.mode=$('classification-mode').value;return value;}
async function saveModels(onlyKind){const payload={};for(const k of (onlyKind?[onlyKind]:['embedding','classification'])){const value=providerPayload(k);if(value)payload[k]=value;}if(!Object.keys(payload).length)throw new Error('请至少填写一个模型 API 的地址、模型与密钥。');modelConfig=await api(API+'/ai/config',payload);for(const k of Object.keys(payload)){$(k+'-apiKey').value='';$(k+'-clearKey').checked=false;$(k+'-apiKey').placeholder=modelConfig[k]?.hasApiKey?'密钥已保存，留空保留':'填写模型 API Key';$(k+'-status').textContent=modelConfig[k]?.configured?'已配置':'未配置';$(k+'-status').classList.toggle('ready',!!modelConfig[k]?.configured);}updateModelSummary();$('model-save-status').textContent='配置已保存';}
async function testModel(kindValue){const resultEl=$(kindValue+'-test-result');resultEl.classList.remove('error');resultEl.textContent='正在保存配置并连接 API…';try{await saveModels(kindValue);const result=await api(API+'/ai/test',{kind:kindValue});resultEl.textContent=result.ok?'连接成功 · '+result.model+(result.dimensions?' · '+result.dimensions+' 维':'')+(result.imagePixelsSent?' · 支持二进制图片预览':''):result.error||'测试未通过';resultEl.classList.toggle('error',!result.ok);}catch(error){resultEl.textContent=error.message;resultEl.classList.add('error');}}
function applyPreset(kindValue){const value=$(kindValue+'-preset').value;if(value==='custom')return;const fromServer=(modelConfig.presets||[]).find(p=>p.id===value&&p.kind===kindValue),preset=fromServer||fallbackPresets[kindValue][value];if(!preset)return;for(const field of ['baseUrl','model','batchSize'])if(preset[field]!==undefined)$(kindValue+'-'+field).value=preset[field];if(kindValue==='embedding')$('embedding-dimensions').value='';if(kindValue==='classification'){if(preset.mode)$('classification-mode').value=preset.mode;updateModeHint();}$(kindValue+'-test-result').textContent=preset.note||'';$('model-save-status').textContent='';}
async function loadCloudConfig(){const config=await api('/api/netdisk/status');cloudScopeRoot=config.scopeRoot||'';$('auth-status').textContent=config.authorized?'已连接':config.configured?'等待授权':'尚未配置';$('auth-status').classList.toggle('ready',!!config.authorized);$('scope').textContent=config.scopeRoot?'文件范围：'+config.scopeRoot:'连接你的百度网盘，备份游戏工程与素材。';for(const id of ['clientId','appName','redirectUri'])$(id).value=config[id]||(id==='redirectUri'?'oob':'');$('clientSecret').value='';$('clientSecret').placeholder=config.hasClientSecret?'SecretKey 已保存，留空保留':'填写 SecretKey';return config;}
async function loadCloud(){
  const request=++cloudRequest;pushCloudHistory($('remote-dir').value);renderCloudNavigation();$('cloud-empty').querySelector('h2').textContent='这里还没有文件';$('cloud-empty').querySelector('p').textContent='上传文件，或在资源库中选择素材后备份到这里。';await cloudTableReady;
  const result=await api('/api/netdisk/files?'+new URLSearchParams({path:$('remote-dir').value,start:cloudOffset,limit:100}));if(request!==cloudRequest)return;
  const files=(result.items||result.list||[]).map(f=>({...f,name:f.name||f.server_filename||f.path,fsId:String(f.fsId||f.fs_id),isDir:!!(f.isDir||f.isdir),bytes:f.bytes??f.size??0,modifiedAt:f.modifiedAt||f.server_mtime||f.local_mtime||0,extension:(f.name||f.server_filename||'').includes('.')?(f.name||f.server_filename).split('.').pop():''}));cloudExplorer.entries=files;
  $('cloud-table').hidden=!files.length;await cloudTable.replaceData(files);if(files.length)requestAnimationFrame(()=>cloudTable.redraw(true));$('cloud-empty').hidden=!!files.length;$('cloud-count').textContent=files.length?(cloudOffset+1)+'–'+(cloudOffset+files.length)+' 项 · 单击选择 · 双击打开':'当前目录为空';cloudHasMore=result.hasMore??files.length===100;$('cloud-previous').disabled=cloudOffset===0;$('cloud-next').disabled=!cloudHasMore;syncCloudSelection();renderCloudNavigation();
}
async function cloudAction(file){if(file.isDir){await navigateCloud(file.path);}else await showPreview(netdiskAsset(file));}
async function uploadFiles(files){for(let i=0;i<files.length;i++){notice('正在接收 '+(i+1)+' / '+files.length+'：'+files[i].name);const query=new URLSearchParams({name:files[i].name,remotePath:[$('remote-dir').value.replace(/\/$/,''),files[i].name].filter(Boolean).join('/')});const response=await fetch('/api/netdisk/upload?'+query,{method:'POST',headers:{'Content-Type':'application/octet-stream'},body:files[i]});const value=await response.json();if(!response.ok)throw new Error(value.error||'文件接收失败。');}notice(files.length+' 个文件已排队，传输记录会显示云端保存结果。');tab('history');}
function renderTransferJobs(items){const root=$('jobs');root.replaceChildren();if(!items.length)root.append(node('p','还没有传输任务。上传或下载文件后会显示在这里。','job-empty'));for(const job of items){const wrap=node('article',undefined,'job-item'),head=node('div',undefined,'row spread');head.append(node('strong',job.name||job.jobId,'job-title'),badge(states[stateOf(job)]||stateOf(job),stateOf(job)==='completed'?'ready':''));wrap.append(head,node('p',(job.operation==='upload'?'上传到网盘':job.operation==='download'?'下载到 Mac':job.operation)+' · '+Math.round(progressPercent(job))+'% · '+time(job.createdAt),'job-meta'));if(running(job)){const track=node('div',undefined,'progress-track'),bar=node('span');bar.style.width=progressPercent(job)+'%';track.append(bar);wrap.append(track);}if(job.error)wrap.append(node('p',job.error,'job-error'));const actions=node('div',undefined,'job-actions');if(job.contentUrl){const a=node('a','保存到电脑','text-button');a.href=safeUrl(job.contentUrl);a.download='';actions.append(a);}if(running(job))actions.append(button('取消',async()=>{await api('/api/netdisk/cancel',{jobId:job.jobId});await loadJobs();},'text-button'));wrap.append(actions);root.append(wrap);}}
function renderBackups(items){const root=$('backups');root.replaceChildren();if(!items.length)root.append(node('p','在素材列表选择资源，点击「分类上传」预览云端分类目录。','job-empty'));for(const job of items){const wrap=node('article',undefined,'job-item'),head=node('div',undefined,'row spread');head.append(node('strong',(job.completedCount||0)+' / '+job.fileCount+' 个资源','job-title'),badge(states[stateOf(job)]||stateOf(job),stateOf(job)==='completed'?'ready':''));wrap.append(head,node('p',(job.layout==='classified'?'分类归档 · ':'任务快照 · ')+job.remoteDirectory,'job-meta'));const parts=[];if(job.failedCount)parts.push('失败 '+job.failedCount);if(job.cancelledCount)parts.push('取消 '+job.cancelledCount);if(job.skippedCount)parts.push('跳过 '+job.skippedCount);if(job.manifestState)parts.push('资源清单 '+(states[job.manifestState]||job.manifestState));if(parts.length)wrap.append(node('p',parts.join('，'),'job-meta'));if(job.error)wrap.append(node('p',job.error,'job-error'));const catalogState=job.catalogSyncState;if(catalogState){const catalogText={queued:'相关工程索引等待同步。',running:'相关工程索引正在同步。',completed:'相关工程索引已同步到网盘。',failed:'相关工程索引同步失败。',interrupted:'相关工程索引同步已中断。'}[catalogState]||'相关工程索引状态：'+(states[catalogState]||catalogState);wrap.append(node('p',catalogText,'job-meta'));if(['failed','interrupted'].includes(catalogState)&&job.catalogSyncError)wrap.append(node('p',job.catalogSyncError,'job-error'));}else if(job.catalogSyncJobId)wrap.append(node('p','相关工程索引任务已创建，请查看任务状态。','job-meta'));if(job.catalogSyncJobId)wrap.append(button('查看云索引任务',openCatalog,'text-button'));root.append(wrap);}}
async function loadJobs(){const results=await Promise.allSettled([api(API+'/ai/jobs'),api('/api/netdisk/jobs'),api(API+'/backups'),api(API+'/catalog/jobs')]);if(results[0].status==='fulfilled')renderAiJobs(results[0].value.items||[]);else $('ai-jobs').replaceChildren(node('p',results[0].reason.message,'job-error'));if(results[1].status==='fulfilled')renderTransferJobs(results[1].value.items||[]);else $('jobs').replaceChildren(node('p',results[1].reason.message,'job-error'));if(results[2].status==='fulfilled')renderBackups(results[2].value.items||[]);else $('backups').replaceChildren(node('p',results[2].reason.message,'job-error'));if(results[3].status==='fulfilled'){catalogState.jobs=results[3].value.items||[];renderCatalogJobs();}else $('catalog-history-jobs').replaceChildren(node('p',results[3].reason.message,'job-error'));}
function backupStatus(text,error=false){$('backup-status').textContent=text;$('backup-status').classList.toggle('error',error);}
function invalidateBackupPlan(message='目录或布局已更改，请重新预览。'){
  ++backupState.request;backupState.plan=null;backupState.input=null;$('backup-plan').hidden=true;$('backup-submit').disabled=true;$('backup-preview').disabled=backupState.submitting;
  backupStatus(message);$('backup-layout-hint').textContent=$('backup-layout').value==='snapshot'?'保留旧版布局，文件会放入独立的备份任务目录；下面的预览显示该目录内的相对路径。':'每个素材放入一个主分类目录，其他标签与依赖关系保存在资源清单中。';
}
function openBackup(){
  if(backupState.submitting||!selection.size||selection.size>100)return;backupState.assetIds=[...selection];$('backup-directory').disabled=false;$('backup-layout').disabled=false;$('backup-layout').value='classified';
  $('backup-selection').textContent='已选择 '+backupState.assetIds.length+' 个本地素材 · 每次最多100个';invalidateBackupPlan('先预览，再确认上传。预览不会创建云端目录。');$('backup-dialog').showModal();
}
function renderBackupPlan(plan){
  const summary=plan.summary;$('backup-summary').textContent=summary.fileCount+' 个文件 · '+bytes(summary.totalBytes);$('backup-destination').textContent='目标目录：'+plan.remoteDirectory+(plan.layout==='snapshot'?' / 独立备份任务目录':'');
  const categories=$('backup-categories');categories.replaceChildren();for(const category of summary.categories||[])categories.append(badge((category.label||category.id)+' · '+category.fileCount+' 个 · '+bytes(category.bytes)));
  $('backup-review').hidden=!summary.needsReviewCount;$('backup-review').textContent=summary.needsReviewCount+' 个素材待复核。预览采用现有分类与文件分析结果，不会自动调用 AI。';
  const list=$('backup-mapping');list.replaceChildren();const sources={manual:'人工分类',ai:'AI 分类',analysis:'文件分析'};
  for(const item of plan.items){const row=node('div',undefined,'backup-mapping-item');row.setAttribute('role','listitem');const source=node('div',undefined,'backup-source');source.append(node('strong',item.name||item.relativeSourcePath,'backup-file-name'),node('span',[item.projectName,item.relativeSourcePath].filter(Boolean).join(' / '),'backup-source-path'));const labels=node('div',undefined,'backup-item-labels');labels.append(badge(item.categoryLabel||item.primaryCategory),node('span',sources[item.classificationSource]||'分类依据未注明','hint'));if(item.needsReview)labels.append(badge('待复核','stale'));source.append(labels);const destination=node('div',undefined,'backup-target');destination.append(icon('folder'),node('code',item.archiveRelativePath));row.append(source,destination);list.append(row);}
  $('backup-plan').hidden=false;
}
async function previewBackup(){
  if(backupState.submitting||!$('backup-form').reportValidity())return;
  invalidateBackupPlan('正在读取分类与云端目录映射…');const request=backupState.request,input={assetIds:[...backupState.assetIds],remoteDirectory:$('backup-directory').value.trim(),layout:$('backup-layout').value};$('backup-preview').disabled=true;
  try{const plan=await api(API+'/backup-plan',input);if(request!==backupState.request||!$('backup-dialog').open)return;if(!plan.planDigest||plan.layout!==input.layout||!Array.isArray(plan.items)||!plan.summary||plan.summary.fileCount!==plan.items.length||plan.items.length!==input.assetIds.length)throw new Error('上传预览不完整，请重新预览。');backupState.plan=plan;backupState.input=input;renderBackupPlan(plan);$('backup-submit').disabled=false;backupStatus('分类与保存位置已列出，确认后开始上传。');}
  catch(error){if(request===backupState.request)backupStatus(error.message||'无法生成上传预览。',true);}
  finally{if(request===backupState.request)$('backup-preview').disabled=false;}
}
async function submitBackup(){
  if(backupState.submitting||!backupState.plan||!backupState.input){backupStatus('请先预览分类与目录，再确认上传。',true);return;}
  const input=backupState.input,plan=backupState.plan;if(input.remoteDirectory!==$('backup-directory').value.trim()||input.layout!==$('backup-layout').value){invalidateBackupPlan();return;}
  backupState.submitting=true;$('backup-submit').disabled=true;$('backup-preview').disabled=true;$('backup-directory').disabled=true;$('backup-layout').disabled=true;backupStatus('正在创建上传任务…');
  try{await api(API+'/backup',{...input,assetIds:[...input.assetIds],planDigest:plan.planDigest,syncCatalog:input.layout==='classified'});$('backup-dialog').close();notice('已按预览创建网盘上传任务，可在任务与传输中查看进度。');tab('history');}
  catch(error){if(error.status===409){invalidateBackupPlan((error.message||'上传计划或网盘状态有变化。')+' 请核对以上提示，重新预览后再确认上传。');$('backup-mapping').replaceChildren();}else backupStatus(error.message||'未能创建上传任务，请重试。',true);}
  finally{backupState.submitting=false;$('backup-directory').disabled=false;$('backup-layout').disabled=false;$('backup-preview').disabled=false;$('backup-submit').disabled=!backupState.plan;}
}
function catalogStatus(text,error=false){$('catalog-status').textContent=text;$('catalog-status').classList.toggle('error',error);}
function catalogCount(value){return typeof value==='number'&&Number.isFinite(value)?value.toLocaleString('zh-CN'):'—';}
function catalogCounts(value){const c=value.counts||{};return catalogCount(value.projectCount??c.projects??c.projectCount)+' 个工程 · '+catalogCount(value.assetCount??c.assets??c.assetCount)+' 个资源 · '+catalogCount(value.embeddingCount??c.embeddings??c.embeddingCount)+' 条向量';}
function catalogCloudCounts(value){const count=value.fetchableAssetCount??value.uploadedAssetCount;if(typeof count!=='number')return '已上传原素材数量未提供';return catalogCount(count)+' 个原素材已上传，可按需拉取';}
function selectedSnapshot(){return catalogState.snapshots.find(item=>String(item.manifestFsId)===catalogState.selected);}
function updateCatalogActions(){
  const currentOption=$('catalog-scope').querySelector('[value=current]');currentOption.disabled=!$('project').value;if(!currentOption.disabled)currentOption.textContent='当前工程：'+projectName();else currentOption.textContent='当前工程（请先选择）';
  const busy=catalogState.busy||catalogState.jobs.some(job=>running(job)&&['publish','restore'].includes(job.operation||job.kind));
  $('catalog-publish').disabled=busy;$('catalog-restore').disabled=busy||!selectedSnapshot();
  const snapshot=selectedSnapshot();$('catalog-restore-preview').hidden=!snapshot;
  if(snapshot){$('catalog-restore-title').textContent='恢复 '+time(snapshot.createdAt);$('catalog-restore-summary').textContent=catalogCounts(snapshot)+'；'+catalogCloudCounts(snapshot)+'。';}
}
function renderCatalogLocal(){const local=catalogState.status?.local||{},root=$('catalog-local');root.replaceChildren();for(const [label,value] of [['工程',local.projectCount],['资源',local.assetCount],['向量',local.embeddingCount]]){const item=node('div');item.append(node('strong',catalogCount(value)),node('span',label));root.append(item);}const note=node('p',catalogCloudCounts(local),'catalog-local-note');root.append(note);}
function renderCatalogSnapshots(){
  const root=$('catalog-snapshots');root.replaceChildren();
  if(!catalogState.snapshots.length){root.append(node('p','此目录还没有云索引。发布本机索引后，其他设备就能从这里恢复。','catalog-empty'));updateCatalogActions();return;}
  for(const snapshot of catalogState.snapshots){
    if(snapshot.manifestFsId===undefined||snapshot.manifestFsId===null)continue;
    const key=String(snapshot.manifestFsId),label=node('label',undefined,'catalog-snapshot'+(key===catalogState.selected?' selected':'')),radio=node('input');radio.type='radio';radio.name='catalog-snapshot';radio.value=key;radio.checked=key===catalogState.selected;
    radio.onchange=()=>{catalogState.selected=key;renderCatalogSnapshots();};
    const text=node('span',undefined,'catalog-snapshot-copy'),head=node('span',undefined,'row spread');head.append(node('strong',time(snapshot.createdAt)),node('span',bytes(snapshot.catalogBytes),'hint'));text.append(head,node('span',catalogCounts(snapshot),'catalog-snapshot-counts'),node('span',catalogCloudCounts(snapshot),'catalog-snapshot-source'));label.append(radio,text);root.append(label);
  }
  updateCatalogActions();
}
function catalogJobTitle(job){return {publish:'发布云索引',restore:'恢复云索引',fetch:'下载原素材','asset-fetch':'下载原素材'}[job.operation||job.kind]||'云索引任务';}
function catalogJobProgress(job){const progress=job.progress;if(typeof progress==='string')return progress;const phase=progress?.phase||job.phase;const phases={queued:'等待处理',preparing:'准备任务',snapshot:'整理本机索引',export:'整理本机索引',exporting:'整理本机索引',upload:'上传索引','uploading-index':'上传索引','uploading-manifest':'上传版本清单',download:'下载索引','reading-manifest':'读取版本清单','downloading-index':'下载索引',validate:'校验索引',restore:'恢复索引',restoring:'恢复索引',fetch:'下载原素材','downloading-original':'下载原素材',verify:'校验文件',completed:'完成'};const parts=[];if(phase)parts.push(phases[phase]||phase);const processed=progress?.processed??progress?.completed??progress?.completedBytes,total=progress?.total??progress?.totalBytes;if(typeof processed==='number'&&typeof total==='number'&&total>0)parts.push((progress?.totalBytes!==undefined?bytes(processed)+' / '+bytes(total):processed+' / '+total));if(!parts.length&&running(job))parts.push('在后台处理，可继续浏览资源');return parts.join(' · ');}
function renderCatalogJobs(){
  for(const id of ['catalog-dialog-jobs','catalog-history-jobs']){
    const root=$(id);root.replaceChildren();const jobs=catalogState.jobs.slice(0,id==='catalog-dialog-jobs'?4:30);
    if(!jobs.length)root.append(node('p','发布、恢复索引与原素材下载的进度会显示在这里。','job-empty'));
    for(const job of jobs){const wrap=node('article',undefined,'job-item'),head=node('div',undefined,'row spread');head.append(node('strong',catalogJobTitle(job),'job-title'),badge(states[stateOf(job)]||stateOf(job),stateOf(job)==='completed'?'ready':''));wrap.append(head,node('p',[catalogJobProgress(job),time(job.createdAt)].filter(Boolean).join(' · '),'job-meta'));if(job.error)wrap.append(node('p',job.error,'job-error'));if(job.result?.assetCount!==undefined||job.result?.counts)wrap.append(node('p',catalogCounts(job.result),'job-meta'));if(job.result?.vectorsSkipped)wrap.append(node('p',job.result.vectorsSkipped+' 条云端向量与本机备注冲突，已跳过以保留本机修改。','job-meta'));const conflicts=job.result?.conflictCount??(Array.isArray(job.result?.conflicts)?job.result.conflicts.length:job.result?.conflicts);if(typeof conflicts==='number'&&conflicts>0)wrap.append(node('p',conflicts+' 条内容存在冲突，已保留本机版本。','job-meta'));root.append(wrap);}
  }
  $('catalog-task-section').hidden=!catalogState.jobs.length;updateCatalogActions();
  if(current)renderAssetStorage(current);
}
async function loadCatalogVersions(){
  const request=++catalogState.request,directory=$('catalog-directory').value.trim();if(!directory)throw new Error('请填写索引文件夹。');$('catalog-refresh').disabled=true;$('catalog-version-note').textContent='正在读取云端版本…';
  try{const result=await api(API+'/catalog/snapshots?'+new URLSearchParams({remoteDirectory:directory}));if(request!==catalogState.request)return;catalogState.snapshots=(result.items||[]).filter(item=>item&&item.manifestFsId!==undefined);if(!catalogState.snapshots.some(item=>String(item.manifestFsId)===catalogState.selected))catalogState.selected=catalogState.snapshots[0]?String(catalogState.snapshots[0].manifestFsId):'';renderCatalogSnapshots();const notes=[catalogState.snapshots.length+' 个可恢复版本。'];if(result.truncated||result.hasMore)notes.push('仅显示部分版本，可按索引目录分别管理。');if(result.errors?.length)notes.push(result.errors.length+' 个版本无法读取，已跳过；请检查对应版本清单。');$('catalog-version-note').textContent=notes.join(' ');}
  catch(error){if(request!==catalogState.request)return;catalogState.snapshots=[];catalogState.selected='';renderCatalogSnapshots();$('catalog-version-note').textContent=error.message||'云端版本读取失败，请检查网盘授权后刷新。';throw error;}
  finally{if(request===catalogState.request)$('catalog-refresh').disabled=false;}
}
async function openCatalog(){
  $('catalog-dialog').showModal();catalogStatus('');updateCatalogActions();
  const results=await Promise.allSettled([api(API+'/catalog/status'),pollCatalogJobs()]);
  if(results[0].status==='fulfilled'){catalogState.status=results[0].value;renderCatalogLocal();if(!$('catalog-directory').value.trim())$('catalog-directory').value=catalogState.status.defaultRemoteDirectory||'catalogs';}else catalogStatus(results[0].reason.message,true);
  try{await loadCatalogVersions();}catch(error){catalogStatus(error.message,true);}
}
async function publishCatalog(){
  if(catalogState.busy||!$('catalog-publish-form').reportValidity())return;
  const payload={remoteDirectory:$('catalog-directory').value.trim()};if($('catalog-scope').value==='current'){if(!$('project').value)throw new Error('请先在资源库选择工程。');payload.projectIds=[$('project').value];}
  catalogState.busy=true;updateCatalogActions();catalogStatus('正在创建云索引发布任务…');
  try{const job=await api(API+'/catalog/publish',payload);rememberCatalogJob(job);catalogStatus('发布任务已创建，原素材不会重复上传。');await pollCatalogJobs();}catch(error){catalogStatus(error.message,true);}finally{catalogState.busy=false;updateCatalogActions();}
}
async function restoreCatalog(){
  const snapshot=selectedSnapshot();if(!snapshot||catalogState.busy)return;catalogState.busy=true;updateCatalogActions();catalogStatus('正在创建恢复任务…');
  try{const job=await api(API+'/catalog/restore',{manifestFsId:String(snapshot.manifestFsId)});rememberCatalogJob(job);catalogStatus('正在恢复分类索引与向量，完成后即可浏览与搜索。');await pollCatalogJobs();}catch(error){catalogStatus(error.message,true);}finally{catalogState.busy=false;updateCatalogActions();}
}
function rememberCatalogJob(job){const id=job.jobId||job.id;if(!id)throw new Error('服务未返回任务编号，请在任务页检查状态。');catalogState.knownActive.add(String(id));catalogState.jobs=[job,...catalogState.jobs.filter(item=>String(item.jobId||item.id)!==String(id))];renderCatalogJobs();}
function renderAssetStorage(asset){
  const remote=remoteOnly(asset),fetchId=catalogState.fetching.get(asset.id),fetching=!!fetchId;$('detail-storage').hidden=!asset.storage;$('detail-storage').classList.toggle('remote',remote);
  $('detail-storage').textContent=fetching?'正在从百度网盘下载并校验原素材…':remote?(asset.storage?.canFetch?'云端未下载。分类与向量已可用，下载后可预览或保存原文件。':'只有索引，尚未关联可下载的网盘原素材。'):asset.storage?.state==='cached'?'已下载到本机缓存，原文件来自百度网盘。':'原素材在本机，可直接预览与保存。';
  $('download').hidden=remote;if(remote)$('download').removeAttribute('href');else $('download').href=safeUrl(asset.contentUrl||API+'/assets/'+encodeURIComponent(asset.id)+'/content');
  $('fetch-asset').hidden=!remote||!asset.storage?.canFetch;$('fetch-asset').disabled=fetching;$('fetch-asset').querySelector('span').textContent=fetching?'下载中…':asset.kind==='image'?'下载预览':'下载原素材';
}
async function fetchCurrentAsset(){
  if(!current||!remoteOnly(current)||!current.storage?.canFetch||catalogState.fetching.has(current.id))return;const id=current.id;catalogState.fetching.set(id,'creating');renderAssetStorage(current);
  try{const job=await api(API+'/catalog/fetch',{assetId:id}),jobId=String(job.jobId||job.id||'');if(!jobId)throw new Error('未能读取下载任务编号，请在任务页检查状态。');catalogState.fetching.set(id,jobId);rememberCatalogJob(job);notice('开始下载所选原素材，完成后自动显示预览。');await pollCatalogJobs();}
  catch(error){catalogState.fetching.delete(id);throw error;}finally{if(current?.id===id)renderAssetStorage(current);}
}
function renderVectorSearch(asset){
  const vectors=(asset.embeddings||[]).filter(vector=>typeof vector.model==='string'&&vector.model);$('vector-search-controls').hidden=!vectors.length;$('detail-vector-model').replaceChildren();
  vectors.forEach((vector,index)=>$('detail-vector-model').add(new Option(vector.model+' · '+vector.dimensions+' 维',String(index))));$('vector-similar').disabled=!vectors.length;updateVectorHint();
}
function selectedVector(){return (current?.embeddings||[]).filter(vector=>typeof vector.model==='string'&&vector.model)[Number($('detail-vector-model').value)||0];}
function updateVectorHint(){const vector=selectedVector();$('vector-similar-note').textContent=vector?'直接比较 '+vector.model+(vector.space?' / '+vector.space:'')+' 的已保存向量，不调用模型 API。':'直接比较已保存向量，不调用模型 API。';}

async function pollCatalogJobs(){
  if(catalogState.polling)return;catalogState.polling=true;
  try{
    const result=await api(API+'/catalog/jobs'),jobs=result.items||[],finished=[];catalogState.jobs=jobs;
    for(const job of jobs){const id=String(job.jobId||job.id||'');if(running(job)){catalogState.knownActive.add(id);const assetId=job.assetId||job.result?.assetId||job.input?.assetId;if(assetId&&['fetch','asset-fetch'].includes(job.operation||job.kind))catalogState.fetching.set(assetId,id);}else if(catalogState.knownActive.delete(id))finished.push(job);}
    for(const [assetId,id] of catalogState.fetching){const job=jobs.find(item=>String(item.jobId||item.id)===id);if(job&&!running(job))catalogState.fetching.delete(assetId);}
    renderCatalogJobs();
    if(explorer.preview?.pendingFetch&&!catalogState.fetching.has(explorer.preview.asset.id)){
      const previewAsset=explorer.preview.asset;explorer.preview.pendingFetch=false;if($('preview-dialog').open)await showPreview(previewAsset);
    }
    if(finished.length){
      const changed=finished.some(job=>stateOf(job)==='completed');if(changed){await loadProjects();if(currentTab==='library')await loadAssets();if(current)await loadDetail(current.id);if($('catalog-dialog').open){catalogState.status=await api(API+'/catalog/status');renderCatalogLocal();if(finished.some(job=>(job.operation||job.kind)==='publish'))try{await loadCatalogVersions();}catch(error){catalogStatus(error.message,true);}}}
      const failed=finished.find(job=>stateOf(job)!=='completed');if(failed)notice(catalogJobTitle(failed)+'未完成：'+(failed.error||states[stateOf(failed)]||stateOf(failed)),true);else notice(finished.some(job=>(job.operation||job.kind)==='restore')?'云索引恢复完成，分类与已有向量已可用。':'云索引或素材下载任务已完成。');
    }
  }finally{catalogState.polling=false;}
}

function downloadJson(value,name){const blob=new Blob([JSON.stringify(value,null,2)],{type:'application/json'}),url=URL.createObjectURL(blob),a=node('a');a.href=url;a.download=name;document.body.append(a);a.click();a.remove();setTimeout(()=>URL.revokeObjectURL(url),1000);}
for(const name of ['library','cloud','history'])$('tab-'+name).onclick=()=>tab(name);
$('notice-dismiss').onclick=()=>notice('');
for(const el of document.querySelectorAll('[data-close]'))el.onclick=()=>$(el.dataset.close).close();
for(const dialog of document.querySelectorAll('dialog'))dialog.addEventListener('click',event=>{if(event.target===dialog){const box=dialog.getBoundingClientRect();if(event.clientX<box.left||event.clientX>box.right||event.clientY<box.top||event.clientY>box.bottom)dialog.close();}});
$('register').onclick=$('empty-add').onclick=()=>$('project-dialog').showModal();
$('project-form').onsubmit=event=>{event.preventDefault();run(disabledWhile($('project-form').querySelector('[type=submit]'),async()=>{const project=await api(API+'/projects',{root:$('project-root').value.trim(),name:$('project-name').value.trim()});$('project-dialog').close();await loadProjects();await navigateLocal({projectId:project.id,path:''});await scan(project.id);}));};
$('project').onchange=()=>run(()=>navigateLocal({projectId:$('project').value,path:'',kind:'',query:'',mode:'text'}));
$('scan').onclick=()=>run(()=>scan($('project').value));
$('manifest').onclick=()=>run(async()=>{if(!$('project').value)throw new Error('请先选择一个工程。');const value=await api(API+'/manifest',{projectId:$('project').value});downloadJson(value,projectName()+'-resource-manifest.json');});
$('search-form').onsubmit=event=>{event.preventDefault();run(()=>navigateLocal({...localLocation(),path:'',query:$('query').value.trim()}));};
$('search-mode').onchange=()=>{$('query').placeholder=$('search-mode').value==='semantic'?'描述你想找的素材，例如：蓝色的战斗界面按钮':'搜索名称、路径、标签…';offset=0;if($('search-mode').value==='text')run(()=>navigateLocal(localLocation()));};
$('previous').onclick=()=>{offset=Math.max(0,offset-PAGE_SIZE);run(loadAssets);};$('next').onclick=()=>{offset+=PAGE_SIZE;run(loadAssets);};
$('view-list').onclick=()=>setView('list');$('view-grid').onclick=()=>setView('grid');
$('toggle-inspector').onclick=()=>{const show=$('inspector').hidden;openInspector(show);if(show){const asset=selectedEntry();if(asset)run(()=>isFolder(asset)?folderProperties(asset):loadDetail(asset.id));}};$('close-inspector').onclick=()=>openInspector(false);
$('select-page').onchange=()=>{const checked=$('select-page').checked;for(const asset of entries){if(!toggleSelection(asset.id,checked))break;}};
$('clear').onclick=clearSelection;$('task-scope').onchange=selectedInfo;
for(const value of ['embed','classify','both'])$(value).onclick=()=>run(disabledWhile($(value),()=>startAi(value)));
$('backup').onclick=openBackup;
$('backup-preview').onclick=previewBackup;
$('backup-directory').oninput=()=>invalidateBackupPlan();$('backup-layout').onchange=()=>invalidateBackupPlan();
$('backup-form').onsubmit=event=>{event.preventDefault();run(submitBackup);};
$('backup-dialog').addEventListener('close',()=>invalidateBackupPlan('先预览，再确认上传。'));
$('action-form').onsubmit=event=>{event.preventDefault();run(disabledWhile($('action-submit'),async()=>{await actionWork($('action-value').value.trim());$('action-dialog').close();}));};
$('deps').onclick=()=>run(()=>loadRelated('dependencies',{direction:'out',depth:1}));$('reverse').onclick=()=>run(()=>loadRelated('dependencies',{direction:'in',depth:1}));$('similar').onclick=()=>run(()=>loadRelated('similar',{mode:'visual',limit:20}));$('exact').onclick=()=>run(()=>loadRelated('similar',{mode:'exact',limit:20}));
$('ocr').onclick=()=>run(disabledWhile($('ocr'),async()=>{if(!current)return;const id=current.id;notice('正在识别图片文字…');await api(API+'/ocr',{assetId:id,language:'eng'});notice('图片文字识别已完成。');await loadDetail(id);}));
$('save-note').onclick=()=>run(disabledWhile($('save-note'),saveNote));
$('open-catalog').onclick=()=>run(openCatalog);
$('catalog-refresh').onclick=()=>run(async()=>{catalogStatus('');try{await loadCatalogVersions();}catch(error){catalogStatus(error.message,true);}});
$('catalog-publish-form').onsubmit=event=>{event.preventDefault();run(publishCatalog);};
$('catalog-restore').onclick=()=>run(restoreCatalog);
$('catalog-scope').onchange=updateCatalogActions;
$('catalog-directory').oninput=()=>{++catalogState.request;catalogState.snapshots=[];catalogState.selected='';$('catalog-refresh').disabled=false;renderCatalogSnapshots();$('catalog-version-note').textContent='目录已更改，点击「刷新版本」读取。';};
$('catalog-account').onclick=()=>run(async()=>{$('catalog-dialog').close();await loadCloudConfig();$('cloud-dialog').showModal();});
$('fetch-asset').onclick=()=>run(fetchCurrentAsset);
$('detail-vector-model').onchange=updateVectorHint;
$('vector-similar').onclick=()=>run(disabledWhile($('vector-similar'),async()=>{const vector=selectedVector();if(!vector)throw new Error('此素材还没有已保存的向量。');await loadRelated('similar',{mode:'semantic',model:vector.model,limit:20});}));
$('open-models').onclick=()=>run(async()=>{await loadModelConfig();$('model-save-status').textContent='';$('model-dialog').showModal();});
$('model-form').onsubmit=event=>{event.preventDefault();run(disabledWhile($('model-form').querySelector('[type=submit]'),()=>saveModels()));};
$('embedding-preset').onchange=()=>applyPreset('embedding');$('classification-preset').onchange=()=>applyPreset('classification');$('classification-mode').onchange=updateModeHint;
$('test-embedding').onclick=disabledWhile($('test-embedding'),()=>testModel('embedding'));$('test-classification').onclick=disabledWhile($('test-classification'),()=>testModel('classification'));
$('open-cloud-settings').onclick=()=>run(async()=>{await loadCloudConfig();$('cloud-dialog').showModal();});
$('config-form').onsubmit=event=>{event.preventDefault();run(disabledWhile($('config-form').querySelector('[type=submit]'),async()=>{await api('/api/netdisk/config',Object.fromEntries(['clientId','clientSecret','appName','redirectUri'].map(key=>[key,$(key).value])));$('clientSecret').value='';await loadCloudConfig();notice('网盘应用配置已保存。');}));};
$('authorize').onclick=()=>run(async()=>{const result=await api('/api/netdisk/authorize',{});oauthState=result.state;$('auth-link').href=result.url;$('exchange-area').hidden=false;window.open(result.url,'_blank','noopener,noreferrer');});
$('exchange').onclick=()=>run(disabledWhile($('exchange'),async()=>{await api('/api/netdisk/exchange',{code:$('auth-code').value.trim(),state:oauthState});$('auth-code').value='';oauthState='';$('exchange-area').hidden=true;await loadCloudConfig();await loadCloud();notice('网盘账号已授权。');$('cloud-dialog').close();}));
$('authorize-phone').onclick=()=>run(disabledWhile($('authorize-phone'),async()=>{await api('/api/netdisk/connect-android',{appId:$('androidAppId').value.trim(),thirdId:$('thirdId').value.trim(),from:$('sdkFrom').value.trim()});notice('网盘授权已保存到手机，可在手机原生网盘页面使用。');}));
$('cloud-path-form').onsubmit=event=>{event.preventDefault();run(()=>navigateCloud($('remote-dir').value));};
$('cloud-up').onclick=()=>run(()=>navigateCloud(parentCloudPath()));
$('cloud-back').onclick=()=>run(()=>cloudHistory(-1));$('cloud-forward').onclick=()=>run(()=>cloudHistory(1));
$('cloud-previous').onclick=()=>{cloudOffset=Math.max(0,cloudOffset-100);run(loadCloud);};$('cloud-next').onclick=()=>{cloudOffset+=100;run(loadCloud);};
$('mkdir').onclick=()=>showAction({title:'新建网盘文件夹',description:'文件夹创建在当前打开的网盘目录。',label:'文件夹名称',submit:'创建文件夹',work:async name=>{await api('/api/netdisk/mkdir',{path:[$('remote-dir').value.replace(/\/$/,''),name].filter(Boolean).join('/')});cloudOffset=0;await loadCloud();notice('文件夹已创建。');}});
$('upload').onclick=()=>$('files').click();$('files').onchange=()=>{const files=[...$('files').files];$('files').value='';if(files.length)run(()=>uploadFiles(files));};
$('refresh-jobs').onclick=()=>run(loadJobs);
$('asset-mcp').textContent=location.origin+'/mcp/assets';$('netdisk-mcp').textContent=location.origin+'/mcp/netdisk';
document.addEventListener('visibilitychange',()=>{if(!document.hidden)run(pollAi);});window.addEventListener('beforeunload',()=>{clearInterval(jobTimer);clearTimeout(scanTimer);for(const active of cloudExplorer.previewJobs.values())clearTimeout(active.timer);for(const active of fileOps.jobs.values())clearTimeout(active.timer);disposeTextViewer();});
for(const group of ['embedding','classification'])$('model-tab-'+group).onclick=()=>{for(const other of ['embedding','classification']){$(other+'-section').hidden=other!==group;$('model-tab-'+other).setAttribute('aria-selected',String(other===group));}};
for(const group of ['embedding','classification'])$(group+'-load-models').onclick=()=>run(disabledWhile($(group+'-load-models'),async()=>{const payload=providerPayload(group);if(payload)await api(API+'/ai/config',{[group]:payload});const result=await api(API+'/ai/models?kind='+group);const models=result.items||result.models||result.data||[];$(group+'-models').replaceChildren();for(const model of models){const id=typeof model==='string'?model:model.id||model.name;if(id)$(group+'-models').append(new Option(id,id));}$(group+'-test-result').textContent=models.length?'已读取 '+models.length+' 个模型，可在模型输入框中选择。':'服务商未返回模型列表，可手动填写。';$(group+'-apiKey').value='';}));

for(const [scope,prefix] of [['local','file'],['netdisk','cloud']]){for(const [action,work] of [['cut',()=>setFileClipboard('move',scope)],['copy',()=>setFileClipboard('copy',scope)],['paste',()=>pasteFiles(scope)],['rename',()=>renameFile(scope)],['delete',()=>deleteFiles(scope)]])$(prefix+'-'+action).onclick=()=>run(work);}
$('file-operation-execute').onclick=()=>run(executeFilePlan);$('file-operation-dismiss').onclick=()=>{$('file-operation-status').hidden=true;};
$('asset-content').addEventListener('dragover',event=>fileDragOver(event,'local',null,$('asset-content')));$('asset-content').addEventListener('dragleave',()=>$('asset-content').classList.remove('file-drop-target'));$('asset-content').addEventListener('drop',event=>fileDrop(event,'local',null,$('asset-content')));
$('cloud-table').addEventListener('dragover',event=>fileDragOver(event,'netdisk',null,$('cloud-table')));$('cloud-table').addEventListener('dragleave',()=>$('cloud-table').classList.remove('file-drop-target'));$('cloud-table').addEventListener('drop',event=>fileDrop(event,'netdisk',null,$('cloud-table')));
$('open-selected').onclick=()=>run(()=>openEntry(selectedEntry()));
$('nav-back').onclick=()=>run(()=>navigateHistory(-1));$('nav-forward').onclick=()=>run(()=>navigateHistory(1));$('nav-up').onclick=()=>run(()=>navigateLocal(localUpLocation()));
$('refresh-directory').onclick=()=>run(loadAssets);$('edit-address').onclick=editAddress;$('address-breadcrumbs').ondblclick=editAddress;
$('address-form').onsubmit=event=>{event.preventDefault();run(submitAddress);};
$('address-input').onkeydown=event=>{if(event.key==='Escape'){event.preventDefault();$('address-input').hidden=true;$('address-breadcrumbs').hidden=false;$('asset-content').focus();}};
$('address-input').onblur=()=>{setTimeout(()=>{if(document.activeElement!==$('address-input')){$('address-input').hidden=true;$('address-breadcrumbs').hidden=false;}},120);};
$('asset-content').addEventListener('click',event=>{if(!event.target.closest('.tabulator-row,.tabulator-header,.asset-card,button,a,input')){clearSelection();focusFiles('');}});
$('cloud-table').addEventListener('click',event=>{if(!event.target.closest('.tabulator-row,.tabulator-header')){cloudExplorer.selection.clear();cloudExplorer.focusId='';syncCloudSelection();}});
$('preview-close').onclick=()=>$('preview-dialog').close();$('preview-previous').onclick=()=>run(()=>movePreview(-1));$('preview-next').onclick=()=>run(()=>movePreview(1));
$('preview-find').onclick=()=>textViewer.editor?.getAction('actions.find')?.run();$('preview-wrap').onclick=toggleTextWrap;
$('preview-zoom-in').onclick=()=>setImageZoom(explorer.zoom*1.25);$('preview-zoom-out').onclick=()=>setImageZoom(explorer.zoom/1.25);$('preview-zoom').onclick=()=>setImageZoom(1);
$('preview-dialog').addEventListener('close',()=>{disposeTextViewer();++explorer.previewRequest;$('preview-content').querySelectorAll('video,audio').forEach(media=>{media.pause();media.removeAttribute('src');media.load();});$('preview-content').replaceChildren();const scope=explorer.preview?.asset.scope;explorer.preview=null;if(scope==='netdisk')$('cloud-table').focus({preventScroll:true});else focusFiles(explorer.focusId);});
document.addEventListener('keydown',keyboardFiles);
document.addEventListener('pointerdown',event=>{if(!event.target.closest('#file-context-menu'))closeContext();});
window.addEventListener('blur',closeContext);window.addEventListener('resize',()=>{closeContext();textViewer.editor?.layout();});

renderCategories();buildTables();selectedInfo();run(async()=>{await loadProjects();pushLocalHistory(localLocation());await loadAssets();});run(loadModelConfig);run(pollAi);run(pollCatalogJobs);jobTimer=setInterval(()=>{if(!document.hidden){run(pollAi);if(currentTab==='history')run(loadJobs);if(catalogState.knownActive.size||$('catalog-dialog').open)run(pollCatalogJobs);}},3500);
})();
