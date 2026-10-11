"""Private mini-game library: immutable inputs, asynchronous exports and HTTP/MCP facade."""
from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor
import copy
import hashlib
import json
import mimetypes
import os
from pathlib import Path
import shutil
import threading
import time
from urllib.parse import quote, unquote, urlsplit
import uuid
import zipfile

from minigame_formats import ASSET_EXT, MAX_EXPANDED, FormatError, check_cancel, inspect_tree, safe_path, unpack_package, unpack_zip

BASE='/api/knowledge/minigames'


def now(): return int(time.time()*1000)


def identifier(value):
    try:
        parsed=str(uuid.UUID(value))
        if value!=parsed: raise ValueError()
        return parsed
    except (ValueError,TypeError,AttributeError): raise ValueError('小游戏或任务 ID 无效。') from None


def write_json(path, value):
    temp=path.with_suffix('.pending')
    temp.write_text(json.dumps(value,ensure_ascii=False,indent=2),encoding='utf-8')
    os.chmod(temp,0o600); os.replace(temp,path)


def files(root):
    return sorted(p for p in root.rglob('*') if p.is_file() and not p.is_symlink())


def file_kind(path):
    suffix=path.suffix.lower()
    if suffix in {'.png','.jpg','.jpeg','.webp','.gif','.ktx','.pvr','.astc'}: return 'image'
    if suffix in {'.wav','.mp3','.ogg','.m4a'}: return 'audio'
    if suffix in {'.mp4','.webm'}: return 'video'
    if suffix in {'.atlas','.skel'} or path.name.lower().endswith(('.skel.bytes','.atlas.txt')): return 'spine'
    return 'data'


class MiniGameService:
    """Standalone Python API; AI/notifier are optional caller supplied callbacks."""
    def __init__(self, root: Path, knowledge=None, assetripper=None):
        self.root=Path(root).resolve(); self.root.mkdir(parents=True,exist_ok=True,mode=0o700)
        self.game_root=self.root/'games'; self.export_root=self.root/'exports'; self.job_root=self.root/'jobs'
        for folder in (self.game_root,self.export_root,self.job_root): folder.mkdir(exist_ok=True,mode=0o700)
        self.lock=threading.RLock(); self.pool=ThreadPoolExecutor(max_workers=2,thread_name_prefix='minigames')
        self.knowledge=knowledge; self.assetripper=assetripper; self.ai=None; self.notifier=None
        self.events={}; self.spine_locks={}; self.closed=False
        for path in self.job_root.glob('*.json'):
            try:
                value=json.loads(path.read_text())
                if value.get('state') in ('queued','running'):
                    value.update(state='failed',message='服务重启中断了任务，原包保留；请手动重新提交。',error='interrupted',updatedAt=now())
                    write_json(path,value)
            except (OSError,ValueError): pass
        for path in self.export_root.glob('.pending-*'): shutil.rmtree(path,ignore_errors=True)
        for path in self.game_root.glob('.pending-*'): shutil.rmtree(path,ignore_errors=True)

    def close(self):
        self.closed=True
        for event in self.events.values(): event.set()
        self.pool.shutdown(wait=True,cancel_futures=False)

    def _read(self, folder, id):
        path=folder/identifier(id)/'manifest.json' if folder!=self.job_root else folder/(identifier(id)+'.json')
        if not path.is_file(): raise FileNotFoundError('未找到该小游戏、导出或任务。')
        return json.loads(path.read_text('utf-8'))

    def list_games(self, platform='all',offset=0,limit=40):
        if platform not in ('all','douyin','wechat','unknown'): raise ValueError('平台无效。')
        if not isinstance(offset,int) or not isinstance(limit,int) or offset<0 or not 1<=limit<=100: raise ValueError('分页参数无效。')
        with self.lock:
            rows=[json.loads(p.read_text('utf-8')) for p in self.game_root.glob('*/manifest.json') if not p.parent.name.startswith('.')]
        rows=[v for v in rows if platform=='all' or v['platform']==platform]
        rows.sort(key=lambda v:v['updatedAt'],reverse=True)
        return {'games':rows[offset:offset+limit],'total':len(rows),'offset':offset,'limit':limit,'hasMore':offset+limit<len(rows)}

    def get_game(self,id):
        with self.lock:
            game=self._read(self.game_root,id)
            exports=[json.loads(p.read_text('utf-8')) for p in self.export_root.glob('*/manifest.json') if not p.parent.name.startswith('.')]
        return {'game':game,'exports':sorted((e for e in exports if e['gameId']==id),key=lambda e:e['createdAt'],reverse=True)}

    def metadata(self,id,fields):
        if set(fields)-{'name'} or not isinstance(fields.get('name'),str) or not 1<=len(fields['name'].strip())<=150: raise ValueError('名称须为 1 至 150 个字符。')
        with self.lock:
            game=self._read(self.game_root,id); game.update(name=fields['name'].strip(),nameSource='manual',updatedAt=now())
            write_json(self.game_root/id/'manifest.json',game)
        return game

    def jobs(self):
        with self.lock: rows=[json.loads(p.read_text('utf-8')) for p in self.job_root.glob('*.json')]
        return {'jobs':sorted(rows,key=lambda v:v['createdAt'],reverse=True)[:100]}

    def job(self,id):
        with self.lock: return self._read(self.job_root,id)

    def cancel(self,id):
        with self.lock:
            job=self.job(id)
            if job['state'] in ('queued','running'):
                self.events[id].set(); self._update(id,message='正在取消，等待处理程序退出。')
            return self.job(id)

    def _update(self,id,**fields):
        with self.lock:
            job=self._read(self.job_root,id); job.update(fields,updatedAt=now()); write_json(self.job_root/(id+'.json'),job)
            return copy.deepcopy(job)

    def _submit(self,type,operation,notify=False,*,not_started_cleanup=None):
        if self.closed: raise ValueError('服务正在停止。')
        id=str(uuid.uuid4()); event=threading.Event(); stamp=now()
        job={'id':id,'type':type,'state':'queued','progress':None,'message':'已排队','createdAt':stamp,'updatedAt':stamp}
        with self.lock:
            self.events[id]=event; write_json(self.job_root/(id+'.json'),job)
        def run():
            started=False
            try:
                check_cancel(event); self._update(id,state='running',message='正在处理')
                started=True
                result=operation(event,lambda **fields:self._update(id,**fields))
                # A completed immutable export is retained even if cancellation raced its publish.
                message=result.get('message','已完成') if isinstance(result,dict) else '已完成'
                self._update(id,state='completed',progress=100,message=message,result=result)
            except InterruptedError: self._update(id,state='cancelled',message='已取消，原文件保留。')
            except Exception as error:
                if getattr(error,'code',None)=='cancelled':
                    self._update(id,state='cancelled',message='已取消，原文件保留。')
                else:
                    safe=str(error) if isinstance(error,(ValueError,FormatError,FileNotFoundError)) or error.__class__.__name__=='AssetRipperError' else '处理失败，请检查资源包完整性、磁盘空间和工具配置。'
                    self._update(id,state='failed',message=safe[:600],error=safe[:600])
            finally:
                if not started and not_started_cleanup is not None:
                    not_started_cleanup()
                receipt=self.job(id)
                if notify and self.notifier and receipt['state'] in ('completed','failed'):
                    try:
                        result=self.notifier({'eventId':'minigame-'+id,'taskId':id,'title':'小游戏资源处理'+('完成' if receipt['state']=='completed' else '失败'),
                                              'message':receipt['message'],'nextStep':'可以打开小游戏管理页查看结果。'})
                        self._update(id,notification=result)
                    except Exception: self._update(id,notification={'delivered':False,'status':'unavailable'})
        self.pool.submit(run); return job

    def scan(self,fields=None):
        platform=(fields or {}).get('platform','all')
        def run(event,report):
            result=self.list_games(platform,0,100)
            for game in result['games']:
                check_cancel(event); report(message='检查本机已导入资源')
                self._refresh(game['id'])
            return {**self.list_games(platform,0,100),'source':'imported','message':'电脑列出已导入的资源；宿主缓存扫描请切换到手机。'}
        return self._submit('scan',run)

    def import_archive(self,source: Path,platform='unknown',name=''):
        """Copy input before returning job, so a caller cannot race attachment deletion."""
        if platform not in ('unknown','douyin','wechat'): raise ValueError('平台无效。')
        if not isinstance(name,str) or len(name)>150: raise ValueError('名称过长。')
        if source.stat().st_size>MAX_EXPANDED: raise FormatError('单次资源包超过 32 GiB，请分包处理。')
        id=str(uuid.uuid4()); pending=self.game_root/('.pending-'+id); pending.mkdir()
        try: shutil.copyfile(source,pending/'original.zip')
        except BaseException: shutil.rmtree(pending,ignore_errors=True); raise
        def run(event,report):
            published=False
            try:
                report(message='读取导入包索引'); unpack_zip(pending/'original.zip',pending/'input',event)
                metadata={}
                manifest=pending/'input/manifest.json'
                if manifest.is_file() and manifest.stat().st_size<=16*1024**2:
                    try: metadata=json.loads(manifest.read_text()).get('game',{})
                    except (ValueError,AttributeError): pass
                analysis=inspect_tree(pending/'input')
                game={'id':id,'platform':platform if platform!='unknown' else metadata.get('platform','unknown'),
                      'appId':analysis['appId'] or metadata.get('appId',''),'name':name or analysis['name'] or metadata.get('name',''),
                      'version':str(metadata.get('version',''))[:100],'source':'imported','createdAt':now(),'updatedAt':now(),
                      'issues':[],'packageCount':len([p for p in files(pending/'input') if p.suffix.lower() in ('.pkg','.wxapkg','.ttpkg')]),
                      'exportModes':['raw','unpacked','assets','unity-project'],**{k:v for k,v in analysis.items() if k not in ('appId','name')}}
                if game['platform'] not in ('douyin','wechat','unknown'): game['platform']='unknown'
                if not isinstance(game['appId'],str) or len(game['appId'])>100: game['appId']=''
                if not isinstance(game['name'],str) or len(game['name'])>150: game['name']=''
                if not game['appId']: game['issues'].append('包内未提供 AppID；缓存文件名不能作为 AppID。')
                if not game['name']: game['issues'].append('名称未知，可手动补充。')
                if game['engine']=='unknown': game['issues'].append('需解包后再检查引擎。')
                write_json(pending/'manifest.json',game); check_cancel(event); os.replace(pending,self.game_root/id); published=True
                return {'game':game,'gameId':id}
            finally:
                if not published: shutil.rmtree(pending,ignore_errors=True)
        try:
            return self._submit('import',run,not_started_cleanup=lambda:shutil.rmtree(pending,ignore_errors=True))
        except BaseException:
            shutil.rmtree(pending,ignore_errors=True); raise

    def import_attachment(self,fields):
        if not self.knowledge: raise ValueError('独立模式请使用 import_archive(Path)。')
        with self.knowledge.lock:
            metadata,path=self.knowledge._attachment(fields.get('attachmentId'))
            if metadata['mimeType']!='application/zip': raise ValueError('请选择 ZIP 资源包。')
            return self.import_archive(path,fields.get('platform','unknown'),fields.get('name',''))

    def _refresh(self,id,root=None):
        analysis=inspect_tree(root or self.game_root/id/'input')
        with self.lock:
            game=self._read(self.game_root,id)
            for key in ('engine','engineVersion','engineVariant','evidence','confidence'): game[key]=analysis[key]
            if not game['appId']: game['appId']=analysis['appId']
            if not game['name']: game['name']=analysis['name']
            game['updatedAt']=now(); write_json(self.game_root/id/'manifest.json',game)
        return game

    def _unpack(self,source,output,event,report):
        output.mkdir()
        source_files=files(source); decoded=0; issues=[]
        for index,file in enumerate(source_files):
            check_cancel(event); rel=file.relative_to(source)
            if file.suffix.lower() in ('.pkg','.ttpkg','.wxapkg'):
                destination=output/'packages'/rel.parent/file.stem
                try:
                    report(message='解包 '+file.name,progress=round(index/max(1,len(source_files))*65))
                    unpack_package(file,destination,event); decoded+=1
                except FormatError as error: issues.append({'path':rel.as_posix(),'message':str(error)})
            else:
                target=output/'cached'/rel; target.parent.mkdir(parents=True,exist_ok=True); shutil.copyfile(file,target)
        if issues and not decoded and not any(p.suffix.lower() in ASSET_EXT or p.suffix.lower()=='.js' for p in files(output) if p.name!='manifest.json'):
            raise FormatError(issues[0]['message'])
        # Resolve cacheList URL metadata into an additional readable mirror. Keep
        # hashed originals; duplicate URL collisions are never overwritten.
        mirrored_bytes=sum(p.stat().st_size for p in files(output))
        for index in list(output.rglob('cacheList.json')):
            if index.stat().st_size>16*1024**2: continue
            try: obj=json.loads(index.read_text()); mapping=obj.get('files',obj) if isinstance(obj,dict) else {}
            except (ValueError,UnicodeError): continue
            if not isinstance(mapping,dict): continue
            for url,entry in list(mapping.items())[:100000]:
                check_cancel(event)
                if not isinstance(url,str) or not isinstance(entry,(str,dict)): continue
                local=entry if isinstance(entry,str) else entry.get('url') or entry.get('path') or entry.get('fileName')
                if not isinstance(local,str): continue
                # Cocos cache managers record sandbox URIs, not OS paths. Resolve
                # only the documented gamecaches sandbox into this imported copy.
                if local.startswith('ttfile://user/gamecaches/'):
                    local=local[len('ttfile://user/gamecaches/'):]
                elif local.startswith('wxfile://usr/gamecaches/'):
                    local=local[len('wxfile://usr/gamecaches/'):]
                elif local.startswith('gamecaches/') and index.parent.name=='gamecaches':
                    local=local[len('gamecaches/'):]
                try: relative=safe_path(local.removeprefix('./')); remote=safe_path(unquote(urlsplit(url).path).lstrip('/'))
                except (ValueError,FormatError): continue
                candidate=index.parent/relative
                if candidate.is_file() and candidate.resolve().is_relative_to(output.resolve()):
                    target=output/'cdn-assets'/remote
                    if not target.exists():
                        size=candidate.stat().st_size
                        if mirrored_bytes+size>MAX_EXPANDED: raise FormatError('解包及缓存路径镜像超过单次 32 GiB，请分包处理。')
                        mirrored_bytes+=size; target.parent.mkdir(parents=True,exist_ok=True); shutil.copyfile(candidate,target)
        return {'packagesDecoded':decoded,'issues':issues}

    def export(self,fields):
        id=identifier(fields.get('gameId')); game=self.get_game(id)['game']; mode=fields.get('mode','assets')
        if mode not in ('raw','unpacked','assets','unity-project'): raise ValueError('导出方式无效。')
        for key in ('summarize','notify'):
            if key in fields and not isinstance(fields[key],bool): raise ValueError(key+' 必须是布尔值。')
        def run(event,report):
            export_id=str(uuid.uuid4()); pending=self.export_root/('.pending-'+export_id); pending.mkdir()
            published=False
            try:
                content=pending/'files'; content.mkdir(); detail={}; job_message=None
                source=self.game_root/id/'input'
                if mode=='raw': shutil.copytree(source,content,dirs_exist_ok=True)
                else:
                    detail=self._unpack(source,pending/'unpacked',event,report)
                    updated=self._refresh(id,pending/'unpacked')
                    if mode=='unity-project':
                        if updated['engine'] not in ('unity','tuanjie'): raise ValueError('未识别到 Unity 或团结资源。请先解包并查看识别依据。')
                        if not self.assetripper: raise ValueError('请先配置电脑 AssetRipper。')
                        report(message='AssetRipper 正在导出，进度按实际阶段更新。',progress=None)
                        result=self.assetripper.export([pending/'unpacked'],content,updated['engineVariant'] or 'auto',
                            cancel_event=event,progress_callback=lambda value:report(message=str(value.get('message',value.get('phase','AssetRipper 正在处理')))[:300],progress=None))
                        detail['assetripper']={k:v for k,v in result.items() if k not in ('output_dir','manifest_path')}
                        if result.get('status')=='partial':
                            count=result['failed_collections']
                            job_message=f'Unity/团结项目已导出，但有 {count} 个集合未能导出，请查看素材说明。'
                            detail.setdefault('issues',[]).append({'code':'unity_partial_export','message':job_message,'failedCollections':count})
                    else:
                        for file in files(pending/'unpacked'):
                            check_cancel(event)
                            if mode=='assets' and file.suffix.lower() not in ASSET_EXT and not file.name.lower().endswith(('.atlas.txt','.json.txt','.skel.bytes')): continue
                            dest=content/file.relative_to(pending/'unpacked'); dest.parent.mkdir(parents=True,exist_ok=True); shutil.copyfile(file,dest)
                    shutil.rmtree(pending/'unpacked',ignore_errors=True)
                check_cancel(event); report(message='建立素材索引并打包',progress=85)
                inventory=[{'path':p.relative_to(content).as_posix(),'bytes':p.stat().st_size,'kind':file_kind(p)} for p in files(content)]
                if not inventory: raise ValueError('没有可导出的素材。原包保留，请选择原包或解包文件。')
                from spine import _json_version, binary_version, _atlas_pages, _choose_atlas
                skeletons=[]
                atlases={}
                for item in inventory:
                    if item['path'].lower().endswith(('.atlas','.atlas.txt')):
                        if (content/item['path']).stat().st_size<=4*1024**2:
                            pages=_atlas_pages(content/item['path'],item['path'])
                            if pages: atlases[item['path']]=pages
                known_paths={item['path'] for item in inventory}
                for item in inventory:
                    file=content/item['path']; version=None
                    if file.name.lower().endswith(('.json','.json.txt')):
                        if file.stat().st_size<=16*1024**2: version=_json_version(file)
                    elif file.name.lower().endswith(('.skel','.skel.bytes')):
                        with file.open('rb') as src: version=binary_version(src.read(512)) or None
                    if version is not None:
                        atlas=_choose_atlas(item['path'],list(atlases))
                        missing=[page for page in atlases.get(atlas,[]) if page not in known_paths]
                        skeletons.append({'skeleton':item['path'],'version':version,'atlas':atlas,'textures':atlases.get(atlas,[]),
                                          'ready':bool(atlas) and not missing,'issues':['未找到匹配图集'] if not atlas else ['缺少纹理：'+p for p in missing]})
                manifest={'id':export_id,'gameId':id,'mode':mode,'name':game['name'] or game['appId'] or '小游戏资源',
                          'createdAt':now(),'fileCount':len(inventory),'files':inventory,'spine':skeletons,'detail':detail,
                          'archivePath':BASE+'/exports/'+export_id+'/archive'}
                with zipfile.ZipFile(pending/'archive.zip','w',compression=zipfile.ZIP_DEFLATED,compresslevel=1,allowZip64=True) as archive:
                    for item in inventory:
                        check_cancel(event); archive.write(content/item['path'],item['path'])
                    archive.writestr('devhelper-export.json',json.dumps({k:v for k,v in manifest.items() if k!='files'},ensure_ascii=False))
                archive=pending/'archive.zip'; digest=hashlib.sha256()
                with archive.open('rb') as src:
                    while chunk:=src.read(1024*1024): check_cancel(event); digest.update(chunk)
                manifest.update(bytes=archive.stat().st_size,sha256=digest.hexdigest())
                if fields.get('summarize'):
                    report(message='DeepSeek 正在整理素材清单',progress=None)
                    try:
                        manifest['summary']=self._summarize(manifest,event)
                    except InterruptedError: raise
                    except Exception:
                        manifest['summaryError']='素材已导出；DeepSeek 整理未完成，请检查 AI 配置后单独重试。'
                    else:
                        (content/'资源说明.md').write_text(manifest['summary'],'utf-8')
                        with zipfile.ZipFile(archive,'a',compression=zipfile.ZIP_DEFLATED,compresslevel=1) as z: z.write(content/'资源说明.md','资源说明.md')
                        manifest['files'].append({'path':'资源说明.md','bytes':(content/'资源说明.md').stat().st_size,'kind':'data'})
                        manifest['fileCount']=len(manifest['files']); manifest['bytes']=archive.stat().st_size
                        with archive.open('rb') as src: manifest['sha256']=hashlib.file_digest(src,'sha256').hexdigest()
                write_json(pending/'manifest.json',manifest); check_cancel(event); os.replace(pending,self.export_root/export_id); published=True
                result={'exportId':export_id,'export':manifest,'game':self.get_game(id)['game']}
                if job_message: result['message']=job_message
                return result
            finally:
                if not published: shutil.rmtree(pending,ignore_errors=True)
        return self._submit('export',run,fields.get('notify',True))

    def get_export(self,id): return self._read(self.export_root,id)

    def export_file(self,id,relative=None):
        manifest=self.get_export(id)
        if relative is None: return self.export_root/id/'archive.zip'
        path=safe_path(relative)
        if not any(item['path']==path for item in manifest['files']): raise FileNotFoundError('此文件不在导出清单内。')
        return self.export_root/id/'files'/path

    def open_spine(self,id,fields=None):
        identifier(id)
        with self.lock: guard=self.spine_locks.setdefault(id,threading.RLock())
        with guard: return self._open_spine(id,fields)

    def _open_spine(self,id,fields=None):
        fields=fields or {}; manifest=self.get_export(id)
        if not manifest['spine']: raise ValueError('没有有效 Spine 骨架。Cocos 序列化文件需要先还原骨架数据。')
        ready=[item for item in manifest['spine'] if item.get('ready')]
        selected=fields.get('skeleton',ready[0]['skeleton'] if ready else manifest['spine'][0]['skeleton'])
        if not any(item['skeleton']==selected for item in manifest['spine']): raise ValueError('请选择已识别的 Spine 骨架。')
        if not any(item['skeleton']==selected for item in ready): raise ValueError('所选 Spine 骨架缺少有效图集或纹理，请先补齐资源。')
        if not self.knowledge: raise ValueError('独立模式请将导出 ZIP 导入 Spine 播放器。')
        if manifest.get('spineBundleId'):
            try: bundle=self.knowledge.spine.bundle(manifest['spineBundleId'])
            except FileNotFoundError: bundle=None
            except ValueError: bundle=None
        else: bundle=None
        if bundle is None:
            # Only playable resources go into this pack; large Unity exports need
            # not fit the player's per-import bound.
            path=self.export_root/id/'spine.zip'
            resources=set()
            for item in ready:
                resources.update([item['skeleton'],item['atlas'],*item['textures']])
            with zipfile.ZipFile(path,'w',compression=zipfile.ZIP_DEFLATED,compresslevel=1) as archive:
                for relative in sorted(resources):
                    archive.write(self.export_file(id,relative),relative)
            with path.open('rb') as src: attachment=self.knowledge.import_attachment(src,manifest['name']+' Spine.zip','application/zip',path.stat().st_size)
            bundle=self.knowledge.spine.import_bundle({'attachmentId':attachment['id'],'name':manifest['name']})
            with self.lock:
                manifest=self.get_export(id)
                manifest['spineBundleId']=bundle['id']; write_json(self.export_root/id/'manifest.json',manifest)
        return {'bundle':bundle,'openUrl':'/spine?entry='+bundle['id']+'&skeleton='+quote(selected,safe='')}

    def _summarize(self,manifest,event):
        if not self.ai: raise ValueError('请先在 AI 设置配置 DeepSeek。')
        check_cancel(event)
        content=json.dumps({'name':manifest['name'],'mode':manifest['mode'],'fileCount':manifest['fileCount'],
                            'files':manifest['files'][:1000],'detail':manifest['detail'],'spine':manifest['spine']},ensure_ascii=False)[:48000]
        result=self.ai(content); check_cancel(event)
        if not isinstance(result,str) or not result.strip(): raise ValueError('DeepSeek 未返回素材说明。')
        return result

    def summarize(self,id):
        def run(event,report):
            manifest=self.get_export(id); report(message='仅将素材清单发送给 DeepSeek，资源文件保留在本机。')
            summary=self._summarize(manifest,event)
            with self.lock:
                manifest=self.get_export(id); manifest['summary']=summary; write_json(self.export_root/id/'manifest.json',manifest)
            return {'exportId':id,'summary':summary}
        return self._submit('summarize',run,True)

    def setup_assetripper(self,fields):
        if not self.assetripper: raise ValueError('此设备不支持 AssetRipper。')
        if set(fields)-{'install_dotnet','install_tuanjie'}: raise ValueError('安装参数无效。')
        if any(not isinstance(v,bool) for v in fields.values()): raise ValueError('安装选项须为布尔值。')
        return self._submit('assetripper-setup',lambda event,report:self.assetripper.setup(**fields,cancel_event=event,
            progress_callback=lambda value:report(message=str(value.get('message',value.get('phase','正在安装 AssetRipper')))[:300],progress=None)))

    @staticmethod
    def tool_specs():
        def spec(name,description,properties=None,required=(),read_only=True):
            schema={'type':'object','properties':properties or {},'additionalProperties':False}
            if required: schema['required']=list(required)
            return {'name':'devhelper_minigame_'+name,'description':description,'inputSchema':schema,
                    'annotations':{'readOnlyHint':read_only,'destructiveHint':False,'idempotentHint':read_only,'openWorldHint':not read_only}}
        ident={'type':'string','format':'uuid'}
        return [
            spec('list','List imported mini-games with AppID/name mapping and engine evidence. Phone cache scans are separate.',{'platform':{'enum':['all','douyin','wechat','unknown']},'offset':{'type':'integer','minimum':0},'limit':{'type':'integer','minimum':1,'maximum':100}}),
            spec('get','Read one game and its completed immutable exports.',{'id':ident},('id',)),
            spec('scan','Refresh imported desktop games. To scan host caches use the Android minigame tool through devhelper_call_tool.',{'platform':{'enum':['all','douyin','wechat','unknown']}},read_only=False),
            spec('import','Import a selected ZIP attachment, including a phone raw-cache export. Copies original before queuing; poll returned job.',{'attachmentId':ident,'platform':{'enum':['douyin','wechat','unknown']},'name':{'type':'string','maxLength':150}},('attachmentId',),False),
            spec('export','Queue raw, unpacked, asset or reconstructed Unity/Tuanjie project export. Originals retained. Poll job; queued is not success. summarize explicitly sends inventory to configured DeepSeek, not binary files.',{'gameId':ident,'mode':{'enum':['raw','unpacked','assets','unity-project']},'summarize':{'type':'boolean'},'notify':{'type':'boolean'}},('gameId','mode'),False),
            spec('job','Read persistent job state, progress and completed output. No task is replayed.',{'id':ident},('id',)),
            spec('cancel','Cancel a queued/running job; poll until stopped. Completed outputs and original caches remain.',{'id':ident},('id',),False),
            spec('rename','Edit a game display name; does not guess or change AppID.',{'id':ident,'name':{'type':'string','minLength':1,'maxLength':150}},('id','name'),False),
            spec('spine','Import identified Spine skeletons/atlases/textures from one export into the existing player and return openUrl.',{'exportId':ident,'skeleton':{'type':'string','maxLength':2048}},('exportId',),False),
            spec('summarize','Explicitly send a bounded resource inventory to DeepSeek and store Markdown. No binary/code uploads or file moves.',{'exportId':ident},('exportId',),False),
            spec('assetripper_status','Read computer AssetRipper/UnityPy availability; Unity export never runs on Android.'),
            spec('assetripper_setup','Explicitly download/build pinned AssetRipper-CLI into private runtime. Optional .NET SDK and UnityPy installation. Requires network; poll returned job.',{'install_dotnet':{'type':'boolean'},'install_tuanjie':{'type':'boolean'}},read_only=False),
            spec('assetripper_configure','Configure existing computer CLI and optional .NET/Python executables; GUI is not a CLI. Reads return redacted status.',{'executable':{'type':'string'},'dotnet':{'type':'string'},'python':{'type':'string'}},read_only=False),
        ]

    def call_tool(self,name,arguments=None):
        args=arguments or {}; action=name.removeprefix('devhelper_minigame_')
        if action=='list': return self.list_games(**args)
        if action=='get': return self.get_game(args['id'])
        if action=='scan': return self.scan(args)
        if action=='import': return self.import_attachment(args)
        if action=='export': return self.export(args)
        if action=='job': return self.job(args['id'])
        if action=='cancel': return self.cancel(args['id'])
        if action=='rename': return self.metadata(args['id'],{'name':args['name']})
        if action=='spine': return self.open_spine(args['exportId'],{k:v for k,v in args.items() if k=='skeleton'})
        if action=='summarize': return self.summarize(args['exportId'])
        if action=='assetripper_status': return self.assetripper.status() if self.assetripper else {'available':False}
        if action=='assetripper_setup': return self.setup_assetripper(args)
        if action=='assetripper_configure': return self.assetripper.configure(args)
        raise ValueError('未找到小游戏工具。')


def main():
    parser=argparse.ArgumentParser(description='DevHelper mini-game library CLI; HTTP MCP is served by server.py')
    parser.add_argument('--data',type=Path,default=Path.home()/'.devhelper/minigames')
    sub=parser.add_subparsers(dest='command',required=True)
    sub.add_parser('list'); imp=sub.add_parser('import'); imp.add_argument('archive',type=Path); imp.add_argument('--platform',default='unknown',choices=['douyin','wechat','unknown'])
    exp=sub.add_parser('export'); exp.add_argument('game_id'); exp.add_argument('--mode',default='assets',choices=['raw','unpacked','assets','unity-project'])
    sub.add_parser('scan')
    args=parser.parse_args()
    from assetripper import AssetRipperAdapter
    service=MiniGameService(args.data,assetripper=AssetRipperAdapter(args.data.parent/'assetripper'))
    try:
        result=service.list_games() if args.command=='list' else service.import_archive(args.archive,args.platform) if args.command=='import' else service.scan() if args.command=='scan' else service.export({'gameId':args.game_id,'mode':args.mode,'notify':False})
        if 'state' in result:
            while result['state'] in ('queued','running'): time.sleep(.2); result=service.job(result['id'])
        print(json.dumps(result,ensure_ascii=False,indent=2))
        if result.get('state') in ('failed','cancelled'): raise SystemExit(1)
    finally: service.close()


if __name__=='__main__': main()
