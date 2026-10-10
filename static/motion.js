/* Presentation only: state, focus and media close immediately. */
(function(global){
 'use strict';
 const doc=global.document,preference=global.matchMedia?.('(prefers-reduced-motion: reduce)');
 const running=new Set(),active=new WeakMap(),exits=new WeakMap(),maskExits=new WeakMap(),visibility=new WeakMap(),groups=new WeakMap(),states=new WeakMap(),armedLists=new WeakMap(),listKeys=new WeakMap(),accordions=new WeakMap(),framesReady=new WeakSet(),navGroups=new Set();
 const ease='cubic-bezier(.2,.7,.2,1)',listSelector='.item-list,.note-list,.audio-list,.task-list,.resource-grid,.attachment-grid,.attachment-gallery,.desktop-files,.resource-files-list,.device-grid,.sync-conflict-list,#schedule-list,#relay-catalog-list,#relay-transfer-list';
 const modalSelector='.modal-backdrop,[role=menu],.popover',mediaSelector='video,audio,canvas,iframe,.media-workspace,.media-editor-modal';
 let trigger=null,layoutObserver=null;
 const reduced=()=>preference?.matches===true;
 const visible=element=>element?.isConnected!==false&&!element?.hidden&&!element?.closest?.('[hidden]');
 const validRect=rect=>rect&&rect.width>0&&rect.height>0;
 function stop(element){const entry=active.get(element);if(entry){entry.animation.cancel();entry.complete();}}
 function animate(element,frames,duration=180,delay=0,onfinish,options={}){
  stop(element);
  if(reduced()||!visible(element)||typeof element.animate!=='function'){onfinish?.();return null;}
  let animation;
  try{animation=element.animate(frames,{duration,delay,easing:ease,fill:'none',...options});}catch(_){onfinish?.();return null;}
  if(options.pseudoElement&&animation.effect&&animation.effect.pseudoElement!==options.pseudoElement){animation.cancel();onfinish?.();return null;}
  let completed=false;
  const entry={animation,complete(){if(completed)return;completed=true;running.delete(entry);if(active.get(element)===entry)active.delete(element);onfinish?.();}};
  active.set(element,entry);running.add(entry);animation.onfinish=entry.complete;animation.oncancel=entry.complete;return animation;
 }
 function forgetExit(element){for(const store of [exits,maskExits]){const proxy=store.get(element);if(proxy){stop(proxy);proxy.remove();store.delete(element);}}}
 function enter(element,kind='panel',direction=1,axis='X'){
  forgetExit(element);
  const base=kind==='toast'?(global.getComputedStyle?.(element).transform||'none'):'none';
  const transform=base==='none'?'':base+' ';
  const from=kind==='route'?'translate'+axis+'('+direction*10+'px)':kind==='toast'?'translateY(6px)':'translateY(0px)';
  animate(element,[{opacity:0,transform:transform+from},{opacity:1,transform:base}],kind==='route'?180:140);
 }
 function armLists(container){container?.querySelectorAll?.(listSelector).forEach(list=>armedLists.set(list,Date.now()+1500));}
 function listEntrance(list){
  let known=listKeys.get(list);if(!known){known=new Set();listKeys.set(list,known);}
  const items=Array.from(list.children).filter(item=>!item.matches('.empty,.hint'));
  const fresh=[];
  for(const item of items){const key=item.dataset?.motionId||item.getAttribute('data-motion-id');if(key&&!known.has(key)){known.add(key);if(visible(list)&&visible(item))fresh.push(item);}}
  // Bound presentation bookkeeping, never the stored media or document library.
  while(known.size>4096)known.delete(known.values().next().value);
  const until=armedLists.get(list),armed=until&&until>=Date.now();
  if(!visible(list)||!items.length)return;
  if(armed)armedLists.delete(list);
  const entrance=fresh.length?fresh:armed?items.filter(item=>!item.dataset?.motionId&&!item.getAttribute('data-motion-id')):[];
  entrance.slice(0,6).forEach((item,index)=>animate(item,item.querySelector(mediaSelector)?[{opacity:0},{opacity:1}]:[{opacity:0,transform:'translateY(4px)'},{opacity:1,transform:'none'}],140,index*16));
 }
 function pageFor(button){
  const explicit=button.getAttribute('aria-controls');if(explicit)return doc.getElementById(explicit);
  const tab=button.dataset.tab;if(tab){const id={memory:'documents-page',skill:'documents-page',vectors:'vectors-page',schedules:'schedules-page',resources:'resources-page',ai:'ai-page'}[tab];return doc.getElementById(id);}
  return null;
 }
 function navAxis(group){return /^column/.test(global.getComputedStyle?.(group).flexDirection||'')?'Y':'X';}
 function positionIndicator(group,button,moving=false){
  if(!button||!visible(group))return;
  const rect=button.getBoundingClientRect(),container=group.getBoundingClientRect();if(!validRect(rect)||!validRect(container))return;
  const state=groups.get(group);if(!state)return;
  group.setAttribute('aria-orientation',navAxis(group)==='Y'?'vertical':'horizontal');
  let pill=state.pill;
  if(!pill){
   pill=doc.createElement('span');pill.classList.add('motion-nav-indicator');pill.setAttribute('aria-hidden','true');pill.style.pointerEvents='none';
   if((global.getComputedStyle?.(group).position||'static')==='static')group.style.position='relative';
   group.appendChild(pill);group.classList.add('motion-nav-ready');state.pill=pill;
  }
  const old=state.geometry?pill.getBoundingClientRect():null;
  stop(pill);
  const x=rect.left-container.left+(group.scrollLeft||0)-(group.clientLeft||0),y=rect.top-container.top+(group.scrollTop||0)-(group.clientTop||0);
  Object.assign(pill.style,{width:rect.width+'px',height:rect.height+'px',transform:'translate('+x+'px,'+y+'px)',opacity:'1'});
  if(moving&&validRect(old)){
   const fromX=old.left-container.left+(group.scrollLeft||0)-(group.clientLeft||0),fromY=old.top-container.top+(group.scrollTop||0)-(group.clientTop||0);
   animate(pill,[{transform:'translate('+fromX+'px,'+fromY+'px) scale('+old.width/rect.width+','+old.height/rect.height+')',opacity:1},{transform:pill.style.transform,opacity:1}],200);
  }
  state.geometry=rect;
 }
 function route(button){
  const group=button.closest('.tabs,[role=tablist]');if(!group||button.getAttribute('aria-selected')!=='true')return;
  const buttons=Array.from(group.querySelectorAll('[aria-selected]')),index=buttons.indexOf(button),state=groups.get(group)||{index:-1};
  const previous=state.index,old=buttons[previous],axis=navAxis(group);state.index=index;groups.set(group,state);positionIndicator(group,button,previous!==index);
  if(previous===index)return;
  const page=pageFor(button);if(!page||!visible(page))return;
  const oldRect=old?.getBoundingClientRect(),newRect=button.getBoundingClientRect(),delta=oldRect?(axis==='Y'?newRect.top-oldRect.top:newRect.left-oldRect.left):0;
  const direction=delta?Math.sign(delta):previous<0||index>=previous?1:-1;
  enter(page,'route',direction,axis);armLists(page);page.querySelectorAll(listSelector).forEach(listEntrance);
 }
 function rememberModal(element){
  if(!visible(element)||element.hidden)return;
  const panel=element.querySelector('.modal,[role=dialog]')||element,rect=panel.getBoundingClientRect();
  if(validRect(rect))visibility.set(element,{...visibility.get(element),hidden:false,rect,full:element.classList.contains('media-workspace')});
 }
 function rememberLayout(){doc.querySelectorAll(modalSelector).forEach(rememberModal);}
 function modalOpen(element){
  forgetExit(element);const panel=element.querySelector('.modal,[role=dialog]')||element;
  rememberModal(element);layoutObserver?.observe(panel);
  const rect=visibility.get(element)?.rect||panel.getBoundingClientRect();
  if(element.matches('.modal-backdrop')){
   const color=visibility.get(element)?.maskColor||global.getComputedStyle?.(element).backgroundColor||'rgba(18,24,35,.22)';
   visibility.set(element,{...visibility.get(element),maskColor:color});
   element.style.setProperty?.('--motion-backdrop-color',color);element.classList.add('motion-backdrop-ready');
   // Animate only the mask pseudo-element, never the parent of a media canvas.
   animate(element,[{opacity:0},{opacity:1}],140,0,undefined,{pseudoElement:'::before'});
  }
  // Fullscreen editors only fade. Canvas/ROI/seek coordinates never move.
  if(element.classList.contains('media-workspace')){
   for(let ancestor=element.parentElement;ancestor;ancestor=ancestor.parentElement)stop(ancestor);
   animate(panel,[{opacity:0},{opacity:1}],140);
  }else{
   const anchor=trigger&&Date.now()-trigger.at<1800?trigger.rect:null;
   const x=anchor?Math.max(0,Math.min(100,(anchor.left+anchor.width/2-rect.left)/Math.max(1,rect.width)*100)):50;
   const y=anchor?Math.max(0,Math.min(100,(anchor.top+anchor.height/2-rect.top)/Math.max(1,rect.height)*100)):35;
   panel.style.transformOrigin=x+'% '+y+'%';
   animate(panel,[{opacity:0,transform:'translateY('+((y<50?-1:1)*6)+'px) scale(.975)'},{opacity:1,transform:'none'}],180);
  }
  armLists(element);element.querySelectorAll(listSelector).forEach(listEntrance);
 }
 function passiveCopy(node){
  if(node.nodeType===1&&node.matches('script,iframe,video,audio,canvas,object,embed'))return null;
  const copy=node.cloneNode(false);
  for(const child of node.childNodes||node.children||[]){const next=passiveCopy(child);if(next)copy.appendChild(next);}
  return copy;
 }
 function modalClosed(element,previous){
  const panel=element.querySelector('.modal,[role=dialog]')||element;stop(panel);stop(element);forgetExit(element);
  if(reduced()||!previous?.rect||!doc.body)return;
  if(element.matches('.modal-backdrop')){
   const mask=doc.createElement('div');mask.classList.add('motion-mask-exit');mask.inert=true;mask.setAttribute('aria-hidden','true');mask.style.background=previous.maskColor||'rgba(18,24,35,.22)';
   doc.body.appendChild(mask);maskExits.set(element,mask);
   animate(mask,[{opacity:1},{opacity:0}],100,0,()=>{mask.remove();if(maskExits.get(element)===mask)maskExits.delete(element);});
  }
  const rect=previous.rect;let proxy;
  if(previous.full){proxy=doc.createElement('div');proxy.style.background='rgba(18,24,35,.16)';}
  else{
   proxy=passiveCopy(panel);
   [proxy,...proxy.querySelectorAll('*')].forEach(node=>{for(const key of ['id','name','autofocus','href','src','role','aria-modal'])node.removeAttribute(key);node.setAttribute('tabindex','-1');});
  }
  proxy.hidden=false;proxy.inert=true;proxy.setAttribute('aria-hidden','true');proxy.classList.add('motion-exit-proxy');
  Object.assign(proxy.style,{left:rect.left+'px',top:rect.top+'px',width:rect.width+'px',height:rect.height+'px',pointerEvents:'none'});
  doc.body.appendChild(proxy);exits.set(element,proxy);
  animate(proxy,previous.full?[{opacity:1},{opacity:0}]:[{opacity:1,transform:'none'},{opacity:0,transform:'translateY(3px) scale(.99)'}],100,0,()=>{proxy.remove();if(exits.get(element)===proxy)exits.delete(element);});
 }
 function frameReady(element){
  if(framesReady.has(element))return true;
  try{const document=element.contentDocument;if(document?.readyState==='complete'&&document.URL&&document.URL!=='about:blank'){framesReady.add(element);return true;}}catch(_){/* Cross-origin frames are ready only after their load event. */}
  return false;
 }
 function hiddenChanged(element,oldValue){
  const previous=visibility.get(element),wasHidden=typeof previous==='object'?previous.hidden:previous??oldValue!==null;
  if(wasHidden===element.hidden)return false;
  visibility.set(element,{...previous,hidden:element.hidden});
  if(element.matches(modalSelector)){if(element.hidden)modalClosed(element,previous);else if(visible(element))modalOpen(element);return !element.hidden;}
  if(element.hidden){stop(element);return false;}
  if(!visible(element))return false;
  if(element.matches('.app-frame,#knowledge-ai-frame')){if(frameReady(element))animate(element,[{opacity:0},{opacity:1}],140);}
  else if(element.matches('.toast'))enter(element,'toast');
  else if(element.matches('.message,[role=status],#document-form,#note-form,#schedule-form,#document-preview,#note-preview'))enter(element);
  return true;
 }
 function stateText(element){return element.textContent.trim().slice(0,512).replace(/\d+(?:[.,:]\d+)*\s*%?/g,'#');}
 function changedState(element,skip=false){
  if(!element)return;
  const value=stateText(element),previous=states.get(element);states.set(element,value);
  if(skip||!visible(element)||previous===undefined||previous===value||!value)return;
  animate(element,[{opacity:.5},{opacity:1}],140);
 }
 function passiveDetails(details){return !details.closest('.media-workspace')&&!details.querySelector(mediaSelector);}
 function accordionState(details){
  let state=accordions.get(details);if(!state){state={desired:!!details.open,expected:!!details.open,generation:0,busy:false};accordions.set(details,state);layoutObserver?.observe(details);}return state;
 }
 function accordion(details,opening,fromHeight){
  const state=accordionState(details),summary=details.querySelector('summary');if(!summary)return;
  const current=fromHeight??details.getBoundingClientRect().height;
  state.generation++;const generation=state.generation;stop(details);
  if(!state.busy){state.height=details.style.height||'';state.overflow=details.style.overflow||'';state.aria=summary.getAttribute('aria-expanded');state.inert=Array.from(details.children).filter(child=>child!==summary).map(child=>[child,child.inert]);}
  state.busy=true;state.desired=opening;
  details.style.height=state.height;details.style.overflow=state.overflow;
  details.open=false;const closed=details.getBoundingClientRect().height;
  details.open=true;const expanded=details.getBoundingClientRect().height;state.expected=true;
  summary.setAttribute('aria-expanded',String(opening));
  for(const [child,inert] of state.inert)child.inert=opening?inert:true;
  if(!opening&&state.inert.some(([child])=>child.contains?.(doc.activeElement)))summary.focus?.();
  const target=opening?expanded:closed;
  const box=global.getComputedStyle?.(details),offset=box?.boxSizing==='content-box'?['paddingTop','paddingBottom','borderTopWidth','borderBottomWidth'].reduce((total,key)=>total+(parseFloat(box[key])||0),0):0;
  details.style.overflow='hidden';details.style.height=Math.max(0,target-offset)+'px';
  const finish=()=>{
   if(state.generation!==generation)return;
   details.open=opening;state.expected=opening;state.busy=false;
   details.style.height=state.height;details.style.overflow=state.overflow;
   for(const [child,inert] of state.inert)child.inert=inert;
   if(state.aria===null)summary.removeAttribute('aria-expanded');else summary.setAttribute('aria-expanded',state.aria);
   state.lastHeight=details.getBoundingClientRect().height;
  };
  animate(details,[{height:Math.max(0,current-offset)+'px'},{height:Math.max(0,target-offset)+'px'}],200,0,finish);
 }
 function detailsChanged(details){
  const state=accordionState(details);
  if(state.expected===!!details.open)return;
  if(!passiveDetails(details)){state.expected=state.desired=!!details.open;if(details.open)for(const child of details.children)if(child.tagName!=='SUMMARY')animate(child,[{opacity:0},{opacity:1}],120);return;}
  const previous=state.lastHeight;accordion(details,!!details.open,previous);
 }
 function initialize(){
  if(typeof global.ResizeObserver==='function')layoutObserver=new global.ResizeObserver(entries=>{
   for(const entry of entries){const element=entry.target;if(navGroups.has(element)){const state=groups.get(element),buttons=Array.from(element.querySelectorAll('[aria-selected]'));positionIndicator(element,buttons[state.index]);}else if(element.tagName==='DETAILS'){const state=accordionState(element);if(!state.busy)state.lastHeight=element.getBoundingClientRect().height;}else{const modal=element.closest(modalSelector);if(modal)rememberModal(modal);}}
  });
  doc.querySelectorAll('.tabs,[role=tablist]').forEach(group=>{const buttons=Array.from(group.querySelectorAll('[aria-selected]')),index=buttons.findIndex(button=>button.getAttribute('aria-selected')==='true');groups.set(group,{index});navGroups.add(group);positionIndicator(group,buttons[index]);layoutObserver?.observe(group);});
  doc.querySelectorAll(modalSelector+',.toast,.message,[role=status],.app-frame,#knowledge-ai-frame').forEach(element=>visibility.set(element,{hidden:element.hidden}));
  doc.querySelectorAll('.badge,[role=status],.message,.device-error').forEach(element=>states.set(element,stateText(element)));
  doc.querySelectorAll('details').forEach(details=>{const state=accordionState(details);state.lastHeight=details.getBoundingClientRect().height;});
  doc.querySelectorAll(listSelector).forEach(list=>listKeys.set(list,new Set(Array.from(list.children).map(item=>item.dataset?.motionId||item.getAttribute('data-motion-id')).filter(Boolean))));
  const observer=new MutationObserver(records=>{
   const hidden=new Map(),selected=new Set(),lists=new Set(),status=new Set(),details=new Set(),revealed=new Set();
   for(const record of records){
    const target=record.target.nodeType===1?record.target:record.target.parentElement;if(!target||target.closest?.('.motion-exit-proxy,.motion-nav-indicator'))continue;
    if(record.type==='attributes'){
     if(record.attributeName==='hidden'&&!hidden.has(target))hidden.set(target,record.oldValue);
     if(record.attributeName==='aria-selected'&&record.oldValue!==target.getAttribute('aria-selected')&&target.getAttribute('aria-selected')==='true')selected.add(target);
     if(record.attributeName==='open')details.add(target);
     if(record.attributeName==='class'&&target.classList.contains('editor-expanded'))for(let ancestor=target;ancestor;ancestor=ancestor.parentElement)stop(ancestor);
    }else{
     const list=target.matches?.(listSelector)?target:target.closest?.(listSelector);if(list)lists.add(list);
     const item=target.closest?.('.badge,[role=status],.message,.device-error');if(item)status.add(item);
    }
   }
   hidden.forEach((oldValue,element)=>{if(hiddenChanged(element,oldValue))revealed.add(element);});
   selected.forEach(route);details.forEach(detailsChanged);lists.forEach(listEntrance);status.forEach(element=>changedState(element,revealed.has(element)));
  });
  observer.observe(doc.body,{subtree:true,attributes:true,attributeOldValue:true,attributeFilter:['hidden','aria-selected','open','class'],childList:true,characterData:true});
  doc.addEventListener('pointerdown',event=>{rememberLayout();const button=event.target.closest?.('button,a,[role=button],summary');if(button)trigger={rect:button.getBoundingClientRect(),at:Date.now()};},{capture:true,passive:true});
  doc.addEventListener('keydown',event=>{rememberLayout();if(event.key==='Enter'||event.key===' '){const button=event.target.closest?.('button,a,[role=button],summary');if(button)trigger={rect:button.getBoundingClientRect(),at:Date.now()};}},{capture:true});
  doc.addEventListener('click',event=>{
   rememberLayout();const summary=event.target.closest?.('summary'),details=summary?.parentElement;
   if(!details||details.tagName!=='DETAILS'||event.defaultPrevented||event.button>0||!passiveDetails(details)||event.target.closest?.('a,button,input,select,textarea'))return;
   event.preventDefault();const state=accordionState(details);accordion(details,!state.desired);
  },{capture:true});
  doc.addEventListener('load',event=>{const frame=event.target;if(!frame.matches?.('.app-frame,#knowledge-ai-frame')||!frame.getAttribute('src')||frame.getAttribute('src')==='about:blank')return;framesReady.add(frame);if(visible(frame))animate(frame,[{opacity:0},{opacity:1}],140);},{capture:true});
  global.addEventListener?.('resize',()=>{rememberLayout();for(const group of navGroups){const state=groups.get(group),buttons=Array.from(group.querySelectorAll('[aria-selected]'));positionIndicator(group,buttons[state.index]);}},{passive:true});
  doc.addEventListener('scroll',event=>{const group=event.target;if(!navGroups.has(group))return;const state=groups.get(group),buttons=Array.from(group.querySelectorAll('[aria-selected]'));positionIndicator(group,buttons[state.index]);},{capture:true,passive:true});
  const surface=doc.querySelector('main>section:not([hidden]),.shell>section:not([hidden]),.shell>.workspace');if(surface){enter(surface);armLists(surface);surface.querySelectorAll(listSelector).forEach(listEntrance);}
 }
 const preferenceChanged=()=>{if(reduced())for(const entry of [...running]){entry.animation.cancel();entry.complete();}};
 preference?.addEventListener?.('change',preferenceChanged);
 global.DevHelperMotion=Object.freeze({reduced,enter,stop,scrollBehavior:()=>reduced()?'instant':'smooth'});
 if(doc.readyState==='loading')doc.addEventListener('DOMContentLoaded',initialize,{once:true});else initialize();
})(window);
