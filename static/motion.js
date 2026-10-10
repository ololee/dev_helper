/* DevHelper motion is presentation only: state, focus and media close immediately. */
(function(global){
 'use strict';
 const doc=global.document,preference=global.matchMedia?.('(prefers-reduced-motion: reduce)');
 const running=new Set(),active=new WeakMap(),exits=new WeakMap(),visibility=new WeakMap(),groups=new WeakMap(),states=new WeakMap(),armedLists=new WeakMap();
 const ease='cubic-bezier(.2,.7,.2,1)',listSelector='.item-list,.note-list,.audio-list,.task-list,.resource-grid,.attachment-grid,.desktop-files,.resource-files-list,.device-grid,.sync-conflict-list,#relay-catalog-list,#relay-transfer-list';
 let trigger=null;
 const reduced=()=>preference?.matches===true;
 const visible=element=>element?.isConnected!==false&&!element?.hidden&&!element?.closest?.('[hidden]');
 function stop(element){const entry=active.get(element);if(entry){entry.animation.cancel();entry.cleanup();}}
 function animate(element,frames,duration=180,delay=0,onfinish){
  stop(element);
  if(reduced()||!visible(element)||typeof element.animate!=='function'){onfinish?.();return null;}
  const animation=element.animate(frames,{duration,delay,easing:ease,fill:'none'});
  const entry={animation,cleanup(){running.delete(entry);if(active.get(element)===entry)active.delete(element);}};
  active.set(element,entry);running.add(entry);
  animation.onfinish=()=>{entry.cleanup();onfinish?.();};animation.oncancel=()=>{entry.cleanup();onfinish?.();};return animation;
 }
 function forgetExit(element){const proxy=exits.get(element);if(proxy){stop(proxy);proxy.remove();exits.delete(element);}}
 function enter(element,kind='panel',direction=1){
  forgetExit(element);
  const base=kind==='toast'?(global.getComputedStyle?.(element).transform||'none'):'none';
  const distance=kind==='route'?10:kind==='toast'?6:0;
  const transform=base==='none'?'':base+' ';
  animate(element,[{opacity:0,transform:transform+(kind==='route'?'translateX('+direction*distance+'px)':'translateY('+distance+'px)')},{opacity:1,transform:base}],kind==='route'?180:140);
 }
 function armLists(container){container?.querySelectorAll?.(listSelector).forEach(list=>armedLists.set(list,Date.now()+1500));}
 function listEntrance(list){
  const until=armedLists.get(list);if(!until||until<Date.now()||!visible(list))return;
  const items=Array.from(list.children).filter(item=>!item.matches('.empty,.hint')&&visible(item));if(!items.length)return;
  armedLists.delete(list);items.slice(0,6).forEach((item,index)=>animate(item,[{opacity:0,transform:'translateY(4px)'},{opacity:1,transform:'none'}],140,index*16));
 }
 function pageFor(button){
  const explicit=button.getAttribute('aria-controls');if(explicit)return doc.getElementById(explicit);
  const tab=button.dataset.tab;if(tab){const id={memory:'documents-page',skill:'documents-page',vectors:'vectors-page',schedules:'schedules-page',resources:'resources-page',ai:'ai-page'}[tab];return doc.getElementById(id);}
  return null;
 }
 function route(button){
  const group=button.closest('.tabs,[role=tablist]');if(!group||button.getAttribute('aria-selected')!=='true')return;
  const buttons=Array.from(group.querySelectorAll('[aria-selected]')),index=buttons.indexOf(button),previous=groups.get(group);groups.set(group,index);
  if(previous===index)return;const page=pageFor(button);if(!page||!visible(page))return;
  enter(page,'route',previous==null||index>=previous?1:-1);armLists(page);page.querySelectorAll(listSelector).forEach(listEntrance);
 }
 function modalOpen(element){
  forgetExit(element);const panel=element.querySelector('.modal,[role=dialog]')||element;
  const rect=panel.getBoundingClientRect();visibility.set(element,{hidden:false,rect,full:element.classList.contains('media-workspace')});
  // Fullscreen editors only fade. No scaling or translation of canvas/ROI/seek.
  if(element.classList.contains('media-workspace')){
   for(let ancestor=element.parentElement;ancestor;ancestor=ancestor.parentElement)stop(ancestor);
   animate(panel,[{opacity:0},{opacity:1}],140);
  }
  else{
   const anchor=trigger&&Date.now()-trigger.at<1800?trigger.rect:null;
   const x=anchor?Math.max(0,Math.min(100,(anchor.left+anchor.width/2-rect.left)/Math.max(1,rect.width)*100)):50;
   const y=anchor?Math.max(0,Math.min(100,(anchor.top+anchor.height/2-rect.top)/Math.max(1,rect.height)*100)):35;
   panel.style.transformOrigin=x+'% '+y+'%';
   animate(panel,[{opacity:0,transform:'translateY('+((y<50?-1:1)*6)+'px) scale(.975)'},{opacity:1,transform:'none'}],180);
  }
  armLists(element);element.querySelectorAll(listSelector).forEach(listEntrance);
 }
 function modalClosed(element,previous){
  const panel=element.querySelector('.modal,[role=dialog]')||element;stop(panel);forgetExit(element);
  if(reduced()||!previous?.rect||!doc.body)return;
  const rect=previous.rect;let proxy;
  if(previous.full){proxy=doc.createElement('div');proxy.style.background='rgba(18,24,35,.16)';}
  else{
   proxy=panel.cloneNode(true);
   proxy.querySelectorAll('script,iframe,video,audio,canvas,object,embed').forEach(node=>node.remove());
   [proxy,...proxy.querySelectorAll('*')].forEach(node=>{node.removeAttribute('id');node.removeAttribute('name');node.removeAttribute('autofocus');node.removeAttribute('href');node.removeAttribute('src');node.removeAttribute('role');node.removeAttribute('aria-modal');node.setAttribute('tabindex','-1');});
  }
  proxy.hidden=false;proxy.inert=true;proxy.setAttribute('aria-hidden','true');proxy.classList.add('motion-exit-proxy');
  Object.assign(proxy.style,{left:rect.left+'px',top:rect.top+'px',width:rect.width+'px',height:rect.height+'px',pointerEvents:'none'});
  doc.body.appendChild(proxy);exits.set(element,proxy);
  animate(proxy,previous.full?[{opacity:1},{opacity:0}]:[{opacity:1,transform:'none'},{opacity:0,transform:'translateY(3px) scale(.99)'}],100,0,()=>{proxy.remove();if(exits.get(element)===proxy)exits.delete(element);});
 }
 function hiddenChanged(element,oldValue){
  const previous=visibility.get(element),wasHidden=typeof previous==='object'?previous.hidden:previous??oldValue!==null;
  if(wasHidden===element.hidden)return;
  visibility.set(element,{hidden:element.hidden,rect:previous?.rect,full:previous?.full});
  if(element.matches('.modal-backdrop,[role=menu],.popover')){if(element.hidden)modalClosed(element,previous);else if(visible(element))modalOpen(element);return;}
  if(element.hidden){stop(element);return;}
  if(!visible(element))return;
  if(element.matches('.toast'))enter(element,'toast');
  else if(element.matches('.message,[role=status],#document-form,#note-form,#schedule-form,#document-preview,#note-preview'))enter(element);
 }
 function changedState(element){
  if(!element||!visible(element))return;
  // Progress/clock ticks must not restart an animation on every poll.
  const value=element.textContent.trim().slice(0,512).replace(/\d+(?:[.,:]\d+)*\s*%?/g,'#'),previous=states.get(element);states.set(element,value);
  if(previous===undefined||previous===value||!value)return;
  animate(element,[{opacity:.5},{opacity:1}],140);
 }
 function initialize(){
  doc.querySelectorAll('.tabs,[role=tablist]').forEach(group=>{const buttons=Array.from(group.querySelectorAll('[aria-selected]'));groups.set(group,buttons.findIndex(button=>button.getAttribute('aria-selected')==='true'));});
  doc.querySelectorAll('.modal-backdrop,[role=menu],.popover,.toast,.message,[role=status]').forEach(element=>visibility.set(element,{hidden:element.hidden}));
  doc.querySelectorAll('.badge,[role=status],.message,.device-error').forEach(element=>states.set(element,element.textContent.trim().slice(0,512).replace(/\d+(?:[.,:]\d+)*\s*%?/g,'#')));
  const observer=new MutationObserver(records=>{
   const hidden=new Map(),selected=new Set(),lists=new Set(),status=new Set();
   for(const record of records){
    const target=record.target.nodeType===1?record.target:record.target.parentElement;if(!target||target.closest?.('.motion-exit-proxy'))continue;
    if(record.type==='attributes'){
     if(record.attributeName==='hidden'&&!hidden.has(target))hidden.set(target,record.oldValue);
     if(record.attributeName==='aria-selected'&&record.oldValue!==target.getAttribute('aria-selected')&&target.getAttribute('aria-selected')==='true')selected.add(target);
     if(record.attributeName==='open'&&target.open&&record.oldValue===null){for(const child of target.children)if(child.tagName!=='SUMMARY')enter(child);}
     if(record.attributeName==='open'&&!target.open&&record.oldValue!==null){const summary=target.querySelector('summary');if(summary)animate(summary,[{opacity:.7},{opacity:1}],120);}
     if(record.attributeName==='class'&&target.classList.contains('editor-expanded')){for(let ancestor=target;ancestor;ancestor=ancestor.parentElement)stop(ancestor);}
    }else{
     const list=target.matches?.(listSelector)?target:target.closest?.(listSelector);if(list)lists.add(list);
     const item=target.closest?.('.badge,[role=status],.message,.device-error');if(item)status.add(item);
    }
   }
   hidden.forEach((oldValue,element)=>hiddenChanged(element,oldValue));selected.forEach(route);lists.forEach(listEntrance);status.forEach(changedState);
  });
  observer.observe(doc.body,{subtree:true,attributes:true,attributeOldValue:true,attributeFilter:['hidden','aria-selected','open','class'],childList:true,characterData:true});
  doc.addEventListener('pointerdown',event=>{const button=event.target.closest?.('button,a,[role=button],summary');if(button)trigger={rect:button.getBoundingClientRect(),at:Date.now()};},{capture:true,passive:true});
  doc.addEventListener('keydown',event=>{if(event.key==='Enter'||event.key===' '){const button=event.target.closest?.('button,a,[role=button],summary');if(button)trigger={rect:button.getBoundingClientRect(),at:Date.now()};}},{capture:true});
  const surface=doc.querySelector('main>section:not([hidden]),.shell>section:not([hidden]),.shell>.workspace');if(surface){enter(surface);armLists(surface);surface.querySelectorAll(listSelector).forEach(listEntrance);}
 }
 const preferenceChanged=()=>{if(reduced())for(const entry of [...running]){entry.animation.cancel();entry.cleanup();}};
 preference?.addEventListener?.('change',preferenceChanged);
 global.DevHelperMotion=Object.freeze({reduced,enter,stop,scrollBehavior:()=>reduced()?'instant':'smooth'});
 if(doc.readyState==='loading')doc.addEventListener('DOMContentLoaded',initialize,{once:true});else initialize();
})(window);
