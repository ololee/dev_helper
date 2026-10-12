"""Private mini-game library: immutable inputs, asynchronous exports and HTTP/MCP facade."""
from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor
import copy
import hashlib
import io
import json
import mimetypes
import os
from pathlib import Path
import re
import shutil
import struct
import threading
import time
from urllib.parse import quote, unquote, urlsplit
import uuid
import zipfile

from minigame_formats import ASSET_EXT, MAX_EXPANDED, WEB_DATA_SIGNATURES, FormatError, check_cancel, inspect_tree, safe_path, unpack_package, unpack_web_data, unpack_zip, web_data_entries

BASE='/api/knowledge/minigames'
ICON_LIMIT=2*1024*1024
ICON_SUFFIXES=('.png','.jpg','.webp','.gif')
PACKAGE_EXT={'.pkg','.wxapkg','.ttpkg'}
CACHE_ASSOCIATIONS={'none','scoped-appid','scoped-directory','unassociated','version-unconfirmed','manual'}


def valid_app_id(platform,value):
    return isinstance(value,str) and bool(re.fullmatch('wx[0-9a-fA-F]{16}' if platform=='wechat' else 'tt[0-9a-fA-F]{8,64}' if platform=='douyin' else r'(?!)',value))


def now(): return int(time.time()*1000)


def cache_modified_at(metadata):
    """Preserve source cache time, never substitute archive/import/scan time."""
    value=metadata.get('cacheModifiedAt')
    if isinstance(value,(int,float)) and not isinstance(value,bool) and 0<value<=8640000000000000:
        return int(value)
    source=metadata.get('source')
    sources=source if isinstance(source,list) else [source] if isinstance(source,dict) else []
    stamps=[]
    for entry in sources:
        value=entry.get('modifiedSeconds') if isinstance(entry,dict) else None
        if isinstance(value,(int,float)) and not isinstance(value,bool) and 0<value<=8640000000000:
            stamps.append(int(value*1000))
    return max(stamps,default=0)


def source_manifest(root):
    selected=None
    for name in ('devhelper-export.json','manifest.json'):
        path=root/name
        if not path.is_file() or path.stat().st_size>16*1024**2: continue
        try:
            value=json.loads(path.read_text('utf-8'))
            game=value.get('game') if isinstance(value,dict) else None
            if isinstance(game,dict):
                if selected is None: selected={**value,'game':dict(game)}
                elif not isinstance(selected.get('files'),list) and isinstance(value.get('files'),list):
                    # Desktop raw exports keep the phone file-role manifest.
                    # Prefer the latest game metadata, retaining original roles.
                    selected['files']=value['files']
        except (ValueError,UnicodeError,OSError): pass
    if selected is None: return {}
    if not cache_modified_at(selected['game']):
        # Old phone raw archives stored source timestamps on files while
        # game.source remained "phone-cache"; export/import dates are unrelated.
        stamp=cache_modified_at({'source':selected.get('files')})
        if stamp: selected['game']['cacheModifiedAt']=stamp
    return selected


def source_metadata(root): return source_manifest(root).get('game',{})


def metadata_text(value,limit):
    return value.strip() if isinstance(value,str) and len(value.strip())<=limit else ''


def field_source(metadata,field,value):
    if not value: return ''
    supplied=metadata_text(metadata.get(field),160 if field=='name' else 100)
    return (metadata_text(metadata.get(field+'Source'),100) or 'imported-metadata') if supplied else 'package-metadata'


def icon_suffix(path):
    if path.is_symlink() or not path.is_file() or not 0<path.stat().st_size<=ICON_LIMIT: return None
    with path.open('rb') as source: header=source.read(16)
    if header.startswith(b'\x89PNG\r\n\x1a\n'): return '.png'
    if header.startswith(b'\xff\xd8\xff'): return '.jpg'
    if header.startswith((b'GIF87a',b'GIF89a')): return '.gif'
    if header.startswith(b'RIFF') and header[8:12]==b'WEBP': return '.webp'
    return None


def normalized_icon(data):
    if not isinstance(data,bytes) or not 0<len(data)<=ICON_LIMIT: raise ValueError('图标须为不超过 2 MiB 的 PNG、JPEG 或 WebP 图片。')
    try:
        from PIL import Image, ImageOps
    except ImportError: raise ValueError('请安装电脑版依赖中的 Pillow 后再设置图标。') from None
    try:
        with Image.open(io.BytesIO(data)) as source:
            if source.format not in ('PNG','JPEG','WEBP'): raise ValueError('请选择 PNG、JPEG 或 WebP 图片。')
            if not 0<source.width<=2048 or not 0<source.height<=2048: raise ValueError('图标原图的宽高均须不超过 2048 像素。')
            source.load()
            image=ImageOps.exif_transpose(source).convert('RGBA')
            image.thumbnail((256,256),Image.Resampling.LANCZOS)
            result=io.BytesIO(); image.save(result,format='PNG',optimize=True)
            return result.getvalue()
    except (OSError,SyntaxError,Image.DecompressionBombError): raise ValueError('图标图片无法完整解码，请重新选择 PNG、JPEG 或 WebP 图片。') from None


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


def asset_signature(path):
    """Bounded file signatures also recognize hashed, extensionless downloads."""
    with path.open('rb') as source: header=source.read(128)
    if header.startswith((b'UnityFS\0',b'UnityRaw\0',b'UnityWeb\0',*WEB_DATA_SIGNATURES)): return 'unity'
    if len(header)>=20:
        metadata_size,total_size,version,offset=struct.unpack_from('>4I',header)
        version_offset=20
        if version>=22 and len(header)>=48:
            metadata_size,total_size,offset=struct.unpack_from('>IQQ',header,20); version_offset=48
        if (9<=version<=30 and total_size==path.stat().st_size and 0<metadata_size<total_size
                and version_offset<offset<=total_size):
            import re
            if re.match(rb'\d{1,4}\.\d+\.\d+[ft]\d+\x00',header[version_offset:version_offset+64]): return 'unity'
    if header.startswith((b'\x89PNG\r\n\x1a\n',b'\xff\xd8\xff',b'GIF87a',b'GIF89a',b'\xabKTX 11\xbb\r\n\x1a\n',b'\xabKTX 20\xbb\r\n\x1a\n',b'\x13\xab\xa1\x5c',b'PVR\x03',b'DDS ')): return 'image'
    if header.startswith(b'RIFF') and header[8:12]==b'WEBP': return 'image'
    if header.startswith(b'RIFF') and header[8:12]==b'WAVE': return 'audio'
    if header.startswith((b'OggS',b'fLaC',b'ID3')): return 'audio'
    if header.startswith(b'\x1a\x45\xdf\xa3'): return 'video'
    if len(header)>=16 and header[4:8]==b'ftyp': return 'audio' if header[8:12] in (b'M4A ',b'M4B ',b'M4P ') else 'video'
    return ''


def original_package(path,kind=''):
    if asset_signature(path) or kind=='cache': return False
    if path.suffix.lower() in PACKAGE_EXT: return True
    with path.open('rb') as source: header=source.read(14)
    return header.startswith((b'TPKG',b'V1MMWX')) or len(header)==14 and header[0]==0xbe and header[13]==0xed


def payload_files(root,metadata=None):
    metadata=metadata or source_metadata(root)
    excluded={'manifest.json','devhelper-export.json','metadata/app-icon.png','metadata/app-icon.jpg','metadata/app-icon.webp','metadata/app-icon.gif'}
    icon=metadata.get('iconArchivePath')
    if isinstance(icon,str):
        try: excluded.add(safe_path(icon))
        except (ValueError,FormatError): pass
    return [path for path in files(root) if path.relative_to(root).as_posix() not in excluded]


def source_roles(root):
    manifest=source_manifest(root)
    kinds={}
    for item in manifest.get('files',[]) if isinstance(manifest.get('files'),list) else []:
        if isinstance(item,dict) and isinstance(item.get('path'),str):
            kind=item.get('kind',item.get('type'))
            if kind in ('package','cache'): kinds[item['path']]=kind
    return kinds


def inventory(root,metadata=None,app_id=''):
    manifest=source_manifest(root); metadata=metadata if metadata is not None else manifest.get('game',{})
    kinds=source_roles(root)
    rows=payload_files(root,metadata); package_count=package_bytes=cache_count=cache_bytes=0
    for path in rows:
        if original_package(path,kinds.get(path.relative_to(root).as_posix(),'')):
            package_count+=1; package_bytes+=path.stat().st_size
        else: cache_count+=1; cache_bytes+=path.stat().st_size
    association=metadata.get('cacheAssociation')
    if not cache_count: association='none'
    elif not isinstance(association,str) or association not in CACHE_ASSOCIATIONS or association=='none' or association=='scoped-appid' and not app_id: association='unassociated'
    result={'packageCount':package_count,'packageBytes':package_bytes,'cacheFileCount':cache_count,'cacheBytes':cache_bytes,
            'inventoryKind':'mixed' if package_count and cache_count else 'package' if package_count else 'cache',
            'cacheAssociation':association,'bytes':package_bytes+cache_bytes}
    mismatches=[key for key in ('packageCount','packageBytes','cacheFileCount','cacheBytes') if key in metadata and metadata[key]!=result[key]]
    result['inventoryIssues']=[{'code':'inventory_count_mismatch','message':'导入包实际载荷与来源清单的数量或容量不同，显示实际文件统计。'}] if mismatches else []
    return result


def payload_index(root,source_id):
    """Private immutable content index; explicit source identity never comes from a sandbox hash."""
    manifest=source_manifest(root); roles=source_roles(root)
    metadata={item.get('path'):item for item in manifest.get('files',[]) if isinstance(item,dict)} if isinstance(manifest.get('files'),list) else {}
    rows=[]
    for path in payload_files(root,manifest.get('game',{})):
        relative=path.relative_to(root).as_posix(); supplied=metadata.get(relative,{})
        origin=supplied.get('sourceId')
        if not isinstance(origin,str) or not re.fullmatch('[0-9a-f]{64}',origin): origin=hashlib.sha256((source_id+'\0'+relative).encode()).hexdigest()
        original=supplied.get('originalPath',relative)
        try: original=safe_path(original)
        except (ValueError,FormatError): original=relative
        with path.open('rb') as stream: digest=hashlib.file_digest(stream,'sha256').hexdigest()
        rows.append({'path':relative,'kind':'package' if original_package(path,roles.get(relative,'')) else 'cache',
                     'bytes':path.stat().st_size,'sourceId':origin,'originalPath':original,'sha256':digest})
    return rows


def file_kind(path):
    suffix=path.suffix.lower()
    if suffix in {'.png','.jpg','.jpeg','.webp','.gif','.ktx','.pvr','.astc'}: return 'image'
    if suffix in {'.wav','.mp3','.ogg','.m4a'}: return 'audio'
    if suffix in {'.mp4','.webm'}: return 'video'
    if suffix in {'.atlas','.skel'} or path.name.lower().endswith(('.skel.bytes','.atlas.txt')): return 'spine'
    signature=asset_signature(path)
    return signature if signature and signature!='unity' else 'data'


def asset_file(path):
    return path.suffix.lower() in ASSET_EXT or path.name.lower().endswith(('.atlas.txt','.json.txt','.skel.bytes')) or bool(asset_signature(path))


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
        # Upgrade statistics for already-imported archives without changing
        # identities, manual names, source time or the immutable input files.
        for path in self.game_root.glob('*/manifest.json'):
            try:
                game=json.loads(path.read_text('utf-8'))
                if all(key in game for key in ('packageBytes','cacheFileCount','cacheBytes','inventoryKind','cacheAssociation')): continue
                source=path.parent/'input'
                if source.is_dir():
                    game.update(inventory(source,source_metadata(source),game.get('appId','')))
                    write_json(path,game)
            except (OSError,ValueError,TypeError): pass

    def close(self):
        self.closed=True
        for event in self.events.values(): event.set()
        self.pool.shutdown(wait=True,cancel_futures=False)

    def _read(self, folder, id):
        path=folder/identifier(id)/'manifest.json' if folder!=self.job_root else folder/(identifier(id)+'.json')
        if not path.is_file(): raise FileNotFoundError('未找到该小游戏、导出或任务。')
        return json.loads(path.read_text('utf-8'))

    def list_games(self, platform='all',offset=0,limit=40,sort='latest'):
        if platform not in ('all','douyin','wechat','unknown'): raise ValueError('平台无效。')
        if sort not in ('latest','earliest'): raise ValueError('排序方式无效。')
        if not isinstance(offset,int) or not isinstance(limit,int) or offset<0 or not 1<=limit<=100: raise ValueError('分页参数无效。')
        with self.lock:
            rows=[self._public_group(members) for members in self._groups()]
        rows=[v for v in rows if platform=='all' or v['platform']==platform]
        rows.sort(key=lambda v:(not cache_modified_at(v),(-1 if sort=='latest' else 1)*cache_modified_at(v),v['id']))
        return {'games':rows[offset:offset+limit],'total':len(rows),'offset':offset,'limit':limit,'sort':sort,'hasMore':offset+limit<len(rows)}

    def get_game(self,id):
        with self.lock:
            game=self._public_group(self._members(id))
            exports=[json.loads(p.read_text('utf-8')) for p in self.export_root.glob('*/manifest.json') if not p.parent.name.startswith('.')]
        return {'game':game,'exports':sorted((e for e in exports if e['gameId'] in game['sourceGameIds']),key=lambda e:e['createdAt'],reverse=True)}

    def _base_games(self):
        rows=[]
        for path in self.game_root.glob('*/manifest.json'):
            if path.parent.name.startswith('.'): continue
            value=json.loads(path.read_text('utf-8'))
            if '_payloadInventory' not in value:
                value['_payloadInventory']=payload_index(path.parent/'input',value['id']); write_json(path,value)
            rows.append(value)
        return rows

    def _groups(self):
        rows=self._base_games(); by_id={row['id']:row for row in rows}; parents={key:key for key in by_id}
        def root(key):
            while parents[key]!=key: parents[key]=parents[parents[key]]; key=parents[key]
            return key
        def join(first,second):
            first,second=root(first),root(second)
            if first!=second: parents[max(first,second)]=min(first,second)
        automatic={}
        for row in rows:
            if row.get('_associationExcluded') or row.get('_targetGameId'): continue
            if valid_app_id(row.get('platform'),row.get('appId')):
                key=(row['platform'],row['appId'])
                if key in automatic: join(row['id'],automatic[key])
                else: automatic[key]=row['id']
        for row in rows:
            target=by_id.get(row.get('_targetGameId'))
            if not target or row.get('packageCount',0) or not target.get('packageCount',0) or row['platform']!=target['platform']: continue
            selected=[member for member in rows if root(member['id']) in (root(row['id']),root(target['id']))]
            if any(member.get('appId') and not valid_app_id(member['platform'],member['appId']) for member in selected): continue
            identifiers={member.get('appId') for member in selected if member.get('appId')}
            if len(identifiers)<=1: join(row['id'],target['id'])
        result={}
        for row in rows: result.setdefault(root(row['id']),[]).append(row)
        return list(result.values())

    def _members(self,id):
        id=identifier(id)
        for members in self._groups():
            if any(row['id']==id for row in members): return members
        raise FileNotFoundError('未找到该小游戏、导出或任务。')

    @staticmethod
    def _unique_payload(members):
        seen=set(); entries=[]
        for row in sorted(members,key=lambda row:row['id']):
            for item in row['_payloadInventory']:
                key=(item['sourceId'],item['sha256'])
                if key not in seen: entries.append((row,item)); seen.add(key)
        return entries

    def _public_group(self,members):
        packages=sorted((row for row in members if row.get('packageCount',0)),key=lambda row:row['id'])
        targets={row.get('_targetGameId') for row in members}
        representative=next((row for row in packages if row['id'] in targets),None) or (packages or sorted(members,key=lambda row:row['id']))[0]
        game={key:copy.deepcopy(value) for key,value in representative.items() if not key.startswith('_')}
        candidates=packages or members
        names=sorted((row for row in candidates if row.get('name')),key=lambda row:(row.get('nameSource')=='manual',row.get('updatedAt',0)),reverse=True)
        if names:
            for key in ('name','nameSource'): game[key]=names[0].get(key,'')
        icons=sorted((row for row in candidates if row.get('iconAvailable')),key=lambda row:(row.get('iconSource')=='manual',row.get('updatedAt',0)),reverse=True)
        game['iconAvailable']=bool(icons)
        if icons:
            game.update(iconUrl=BASE+'/games/'+game['id']+'/icon',iconSource=icons[0].get('iconSource',''),iconUpdatedAt=icons[0].get('updatedAt',0))
        detected=sorted((row for row in members if row.get('engine') in ('unity','tuanjie')),key=lambda row:row.get('engine')=='tuanjie',reverse=True)
        if detected:
            for key in ('engine','engineVariant','engineVersion','confidence'): game[key]=detected[0].get(key,'')
        evidence=[]
        for row in members:
            for entry in row.get('evidence',[]):
                if entry not in evidence: evidence.append(entry)
        game['evidence']=evidence[:100]
        entries=self._unique_payload(members); package_entries=[item for _,item in entries if item['kind']=='package']; caches=[item for _,item in entries if item['kind']=='cache']
        game.update(packageCount=len(package_entries),packageBytes=sum(item['bytes'] for item in package_entries),cacheFileCount=len(caches),cacheBytes=sum(item['bytes'] for item in caches))
        game['bytes']=game['packageBytes']+game['cacheBytes'];game['inventoryKind']='mixed' if package_entries and caches else 'package' if package_entries else 'cache'
        game['cacheModifiedAt']=max((cache_modified_at(row) for row in members),default=0)
        game['updatedAt']=max((row.get('updatedAt',0) for row in members),default=0)
        game['sourceGameIds']=sorted(row['id'] for row in members)
        if representative.get('_associationExcluded'): game['associationSource']='unlinked'
        versions=[version for row in packages for version in (row.get('_packageVersionMetadata') or [{'id':row['id'],'version':row.get('version',''),'cacheLabel':row.get('cacheLabel',''),'packageCount':row.get('packageCount',0),'packageBytes':row.get('packageBytes',0)}])]
        game['packageVersions']=[]
        for version in versions:
            if version not in game['packageVersions']: game['packageVersions'].append(version)
        game['linkedCaches']=[{'id':row['id'],'platform':row['platform'],'appId':row.get('appId',''),'inventoryKind':'cache','cacheLabel':row.get('cacheLabel',''),'cacheFileCount':row.get('cacheFileCount',0),'cacheBytes':row.get('cacheBytes',0),'associationSource':'manual' if row.get('_targetGameId') else 'app-id','targetGameId':game['id']} for row in members if not row.get('packageCount',0) and packages]
        if game['linkedCaches']:
            game['cacheAssociation']='manual' if any(row['associationSource']=='manual' for row in game['linkedCaches']) else 'scoped-appid'
        issues=[]
        for row in members:
            for issue in row.get('issues',[]):
                if issue not in issues: issues.append(issue)
        game['issues']=issues
        if len({row.get('version') for row in packages if row.get('version')})>1 and caches:
            game['issues'].append('原包含多个版本，缓存未指定版本；导出会保留全部版本，不视作某一版本的完整工程。')
        return game

    def associate_cache(self,cache_id,target_game_id):
        with self.lock:
            cache=self._read(self.game_root,cache_id); members=self._members(target_game_id); target=self._public_group(members)
            if cache.get('packageCount',0) or not cache.get('cacheFileCount',0): raise ValueError('请选择独立的下载缓存记录。')
            current=self._members(cache_id); caches=current if all(not row.get('packageCount',0) for row in current) else [cache]
            if not target.get('packageCount',0): raise ValueError('请选择包含原包的游戏。')
            if any(row.get('gameType')=='miniprogram' for row in members): raise ValueError('目标已确认为普通小程序，不能关联小游戏资源缓存。')
            if any(row['platform']!=target['platform'] or row['platform'] not in ('wechat','douyin') for row in caches): raise ValueError('缓存与游戏平台不同，不能关联。')
            if any(row.get('appId') and not valid_app_id(row['platform'],row['appId']) for row in [*caches,*members]): raise ValueError('来源 AppID 与平台不匹配，不能关联。')
            identifiers={row.get('appId') for row in [*caches,*members] if row.get('appId')}
            if len(identifiers)>1: raise ValueError('缓存与原包的 AppID 不一致，不能关联。')
            for cache in caches:
                cache.update(_targetGameId=target['id'],_associationExcluded=False,updatedAt=now());write_json(self.game_root/cache['id']/'manifest.json',cache)
            return self._public_group(self._members(target['id']))

    def unlink_cache(self,cache_id):
        with self.lock:
            cache=self._read(self.game_root,cache_id)
            if cache.get('packageCount',0): raise ValueError('原包记录不能作为独立缓存解除。')
            cache.pop('_targetGameId',None);cache.update(_associationExcluded=True,updatedAt=now());write_json(self.game_root/cache['id']/'manifest.json',cache)
            return self._public_group(self._members(cache['id']))

    def metadata_identity(self,id,app_id,name=None):
        with self.lock:
            members=self._members(id);game=self._public_group(members);target=self._read(self.game_root,game['id'])
            if not valid_app_id(target['platform'],app_id): raise ValueError('请输入所属平台的完整合法 AppID。')
            if any(row.get('appId') and row['appId']!=app_id for row in members): raise ValueError('已有来源的 AppID 与输入不一致，不能覆盖。')
            if name is not None and (not isinstance(name,str) or not 1<=len(name.strip())<=160): raise ValueError('名称须为 1 至 160 个字符。')
            target.update(appId=app_id,appIdSource='manual',updatedAt=now())
            if name is not None: target.update(name=name.strip(),nameSource='manual')
            write_json(self.game_root/target['id']/'manifest.json',target)
            return self._public_group(self._members(target['id']))

    def metadata(self,id,fields):
        if set(fields)-{'name'} or not isinstance(fields.get('name'),str) or not 1<=len(fields['name'].strip())<=160: raise ValueError('名称须为 1 至 160 个字符。')
        with self.lock:
            game=self._read(self.game_root,self._public_group(self._members(id))['id']); game.update(name=fields['name'].strip(),nameSource='manual',updatedAt=now())
            write_json(self.game_root/game['id']/'manifest.json',game)
            return self._public_group(self._members(game['id']))

    def icon_file(self,id):
        with self.lock:
            members=self._members(id);preferred=self._public_group(members)['id']
            candidates=sorted(members,key=lambda row:(row['id']==preferred,row.get('iconSource')=='manual',row.get('updatedAt',0)),reverse=True)
            for game in candidates:
                if game.get('iconAvailable') is not True: continue
                for suffix in ICON_SUFFIXES:
                    path=self.game_root/game['id']/('icon'+suffix)
                    if icon_suffix(path)==suffix: return path
        raise FileNotFoundError('未找到已关联的小游戏图标。')

    def metadata_icon(self,id,data):
        data=normalized_icon(data)
        with self.lock:
            id=self._public_group(self._members(id))['id']
            game=self._read(self.game_root,id)
            path=self.game_root/id/'icon.png'; pending=path.with_suffix('.pending')
            pending.write_bytes(data); os.chmod(pending,0o600); os.replace(pending,path)
            for suffix in ICON_SUFFIXES:
                if suffix!='.png': (self.game_root/id/('icon'+suffix)).unlink(missing_ok=True)
            game.update(iconAvailable=True,iconUrl=BASE+'/games/'+id+'/icon',iconSource='manual',updatedAt=now())
            write_json(self.game_root/id/'manifest.json',game)
            return self._public_group(self._members(id))

    def _import_icon(self,root,folder,metadata,game):
        """Only an explicit archived association can supply a local raster icon."""
        relative=metadata.get('iconArchivePath')
        if metadata.get('iconAvailable') is not True or not isinstance(relative,str): return
        try:
            if game.get('iconSource')=='manual' and icon_suffix(folder/'icon.png')=='.png': return
            path=root/safe_path(relative)
            suffix=icon_suffix(path)
            if suffix is None: return
            # Extracted archive paths have already passed traversal/symlink checks.
            target=folder/'icon.png'; pending=target.with_suffix('.pending')
            pending.write_bytes(normalized_icon(path.read_bytes())); os.chmod(pending,0o600); os.replace(pending,target)
            for other in ICON_SUFFIXES:
                if other!='.png': (folder/('icon'+other)).unlink(missing_ok=True)
            game.update(iconAvailable=True,iconUrl=BASE+'/games/'+game['id']+'/icon',
                        iconSource=metadata_text(metadata.get('iconSource'),100) or 'imported-metadata')
        except (ValueError,OSError,FormatError): return

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
            with self.lock: rows=[row for row in self._base_games() if platform=='all' or row['platform']==platform]
            for game in rows:
                check_cancel(event); report(message='检查本机已导入资源')
                self._refresh(game['id'])
            return {**self.list_games(platform,0,100),'source':'imported','message':'电脑列出已导入的资源；宿主缓存扫描请切换到手机。'}
        return self._submit('scan',run)

    def import_archive(self,source: Path,platform='unknown',name=''):
        """Copy input before returning job, so a caller cannot race attachment deletion."""
        if platform not in ('unknown','douyin','wechat'): raise ValueError('平台无效。')
        if not isinstance(name,str) or len(name)>160: raise ValueError('名称过长。')
        if source.stat().st_size>MAX_EXPANDED: raise FormatError('单次资源包超过 32 GiB，请分包处理。')
        id=str(uuid.uuid4()); pending=self.game_root/('.pending-'+id); pending.mkdir()
        try: shutil.copyfile(source,pending/'original.zip')
        except BaseException: shutil.rmtree(pending,ignore_errors=True); raise
        def run(event,report):
            published=False
            try:
                report(message='读取导入包索引'); unpack_zip(pending/'original.zip',pending/'input',event)
                metadata=source_metadata(pending/'input')
                analysis=inspect_tree(pending/'input')
                payload=payload_files(pending/'input',metadata)
                packages=[p for p in payload if original_package(p)]
                game={'id':id,'platform':platform if platform!='unknown' else metadata.get('platform','unknown'),
                      'appId':metadata_text(metadata.get('appId'),100) or analysis['appId'],
                      'name':name.strip() or metadata_text(metadata.get('name'),160) or analysis['name'],
                      'cacheLabel':packages[0].name if len(packages)==1 else metadata_text(metadata.get('cacheLabel'),200) or (payload[0].name if len(payload)==1 else ''),
                      'version':str(metadata.get('version',''))[:100],'source':'imported','createdAt':now(),'updatedAt':now(),
                      'cacheModifiedAt':cache_modified_at(metadata),'iconAvailable':False,
                      'issues':[],
                      'exportModes':['raw','unpacked','assets','unity-project'],**{k:v for k,v in analysis.items() if k not in ('appId','name')}}
                game.update(inventory(pending/'input',metadata,game['appId']))
                if game['platform'] not in ('douyin','wechat','unknown'): game['platform']='unknown'
                if not isinstance(game['appId'],str) or len(game['appId'])>100: game['appId']=''
                if not isinstance(game['name'],str) or len(game['name'])>160: game['name']=''
                game['nameSource']='manual' if name.strip() else field_source(metadata,'name',game['name'])
                game['appIdSource']=field_source(metadata,'appId',game['appId'])
                game['_payloadInventory']=payload_index(pending/'input',id)
                versions=metadata.get('packageVersions')
                if isinstance(versions,list): game['_packageVersionMetadata']=[{key:item[key] for key in ('id','version','cacheLabel','packageCount','packageBytes') if key in item} for item in versions[:1000] if isinstance(item,dict)]
                if metadata_text(metadata.get('metadataSource'),150): game['metadataSource']=metadata_text(metadata['metadataSource'],150)
                self._import_icon(pending/'input',pending,metadata,game)
                if not game['appId']: game['issues'].append('包内未提供 AppID；缓存文件名不能作为 AppID。')
                if not game['name']: game['issues'].append('名称未知，可手动补充。')
                if game['engine']=='unknown': game['issues'].append('需解包后再检查引擎。')
                write_json(pending/'manifest.json',game); check_cancel(event); os.replace(pending,self.game_root/id); published=True
                return {'game':self.get_game(id)['game'],'gameId':id}
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
        metadata=source_metadata(self.game_root/id/'input')
        with self.lock:
            game=self._read(self.game_root,id)
            for key in ('engine','engineVersion','engineVariant','evidence','confidence','gameType','gameTypeEvidence','gameTypeIssues','metadataIssues'): game[key]=analysis[key]
            if not game['appId']:
                game['appId']=metadata_text(metadata.get('appId'),100) or analysis['appId']
                game['appIdSource']=field_source(metadata,'appId',game['appId'])
            if not game['name'] or game.get('nameSource') not in ('manual',None) and metadata_text(metadata.get('name'),160):
                game['name']=metadata_text(metadata.get('name'),160) or analysis['name']
                game['nameSource']=field_source(metadata,'name',game['name'])
            if not cache_modified_at(game): game['cacheModifiedAt']=cache_modified_at(metadata)
            if metadata_text(metadata.get('metadataSource'),150): game['metadataSource']=metadata_text(metadata['metadataSource'],150)
            game.update(inventory(self.game_root/id/'input',metadata,game['appId']))
            self._import_icon(self.game_root/id/'input',self.game_root/id,metadata,game)
            game['updatedAt']=now(); write_json(self.game_root/id/'manifest.json',game)
        return game

    def _unpack(self,source,output,event,report):
        output.mkdir()
        source_files=payload_files(source); roles=source_roles(source); decoded=0; issues=[]; web_data=[]; output_bytes=0
        for index,file in enumerate(source_files):
            check_cancel(event); rel=file.relative_to(source)
            if original_package(file,roles.get(rel.as_posix(),'')):
                destination=output/'packages'/rel.parent/file.stem
                try:
                    report(message='解包 '+file.name,progress=round(index/max(1,len(source_files))*65))
                    detail=unpack_package(file,destination,event); decoded+=1
                    output_bytes+=detail['bytes'] if 'bytes' in detail else sum(path.stat().st_size for path in files(destination))
                except FormatError as error: issues.append({'path':rel.as_posix(),'message':str(error)})
            else:
                with file.open('rb') as stream: header=stream.read(22)
                is_web_data=any(header.startswith(value) for value in WEB_DATA_SIGNATURES)
                expanded=web_data_entries(file,event)[2] if is_web_data else 0
                if output_bytes+file.stat().st_size+expanded>MAX_EXPANDED: raise FormatError('缓存与展开资源超过单次 32 GiB，请分组处理。')
                target=output/'cached'/rel; target.parent.mkdir(parents=True,exist_ok=True); shutil.copyfile(file,target)
                output_bytes+=file.stat().st_size
                if is_web_data:
                    report(message='展开 WebData 缓存 '+file.name,progress=round(index/max(1,len(source_files))*65))
                    detail=unpack_web_data(file,output/'unity-web-data'/rel,event); output_bytes+=detail['bytes']
                    web_data.append({'path':rel.as_posix(),**detail})
            if output_bytes>MAX_EXPANDED: raise FormatError('展开资源超过单次 32 GiB，请分组处理。')
        if issues and not decoded and not any(asset_file(p) or p.suffix.lower()=='.js' for p in files(output)):
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
                # Resolve explicit sandbox URIs within this cache group's
                # namespace, including resources beside the gamecaches index.
                base=index.parent
                sandbox_prefix=next((prefix for prefix in ('ttfile://user/','wxfile://usr/') if local.startswith(prefix)),None)
                if sandbox_prefix:
                    if index.parent.name!='gamecaches': continue
                    base=index.parent.parent
                    local=unquote(local[len(sandbox_prefix):])
                elif local.startswith('gamecaches/') and index.parent.name=='gamecaches':
                    local=local[len('gamecaches/'):]
                try: relative=safe_path(local.removeprefix('./')); remote=safe_path(unquote(urlsplit(url).path).lstrip('/'))
                except (ValueError,FormatError): continue
                candidate=base/relative
                if candidate.is_file() and candidate.resolve().is_relative_to(base.resolve()) and base.resolve().is_relative_to(output.resolve()):
                    target=output/'cdn-assets'/remote
                    if not target.exists():
                        size=candidate.stat().st_size
                        if mirrored_bytes+size>MAX_EXPANDED: raise FormatError('解包及缓存路径镜像超过单次 32 GiB，请分包处理。')
                        mirrored_bytes+=size; target.parent.mkdir(parents=True,exist_ok=True); shutil.copyfile(candidate,target)
        return {'packagesDecoded':decoded,'webDataContainers':web_data,'issues':issues}

    def _unity_input(self,source,output,event):
        """Expose recovered files to AssetRipper once; retain raw WebData elsewhere."""
        output.mkdir()
        for file in files(source):
            check_cancel(event)
            with file.open('rb') as stream: header=stream.read(22)
            if any(header.startswith(value) for value in WEB_DATA_SIGNATURES): continue
            target=output/file.relative_to(source); target.parent.mkdir(parents=True,exist_ok=True)
            try: os.link(file,target)
            except OSError: shutil.copyfile(file,target)
        if not files(output): raise ValueError('WebData 缓存未恢复出可处理资源；原始文件保留，请检查容器完整性。')
        return output

    def _materialize_sources(self,members,output,event,game):
        """Combine only confirmed members, retaining separate source namespaces."""
        if len(members)==1:
            row=members[0]
            return self.game_root/row['id']/'input',[dict(item) for _,item in self._unique_payload(members)]
        output.mkdir(); entries=[]
        for row,item in self._unique_payload(members):
            check_cancel(event)
            source=self.game_root/row['id']/'input'/item['path']
            relative='payload/'+row['id']+'/'+item['path']; target=output/relative;target.parent.mkdir(parents=True,exist_ok=True)
            shutil.copyfile(source,target);entries.append({**item,'path':relative})
        write_json(output/'manifest.json',{'formatVersion':1,'game':game,'files':entries})
        return output,entries

    def export(self,fields):
        id=identifier(fields.get('gameId'))
        with self.lock:
            members=copy.deepcopy(self._members(id));game=self._public_group(members);id=game['id']
        mode=fields.get('mode','assets')
        if mode not in ('raw','unpacked','assets','unity-project'): raise ValueError('导出方式无效。')
        for key in ('summarize','notify'):
            if key in fields and not isinstance(fields[key],bool): raise ValueError(key+' 必须是布尔值。')
        def run(event,report):
            export_id=str(uuid.uuid4()); pending=self.export_root/('.pending-'+export_id); pending.mkdir()
            published=False
            try:
                content=pending/'files'; content.mkdir(); detail={}; job_message=None
                source,source_entries=self._materialize_sources(members,pending/'source-bundle',event,game)
                if mode=='raw': shutil.copytree(source,content,dirs_exist_ok=True)
                else:
                    detail=self._unpack(source,pending/'unpacked',event,report)
                    self._refresh(id,pending/'unpacked'); updated=self.get_game(id)['game']
                    if mode=='unity-project':
                        if updated['engine'] not in ('unity','tuanjie'): raise ValueError('未识别到 Unity 或团结资源。请先解包并查看识别依据。')
                        if not self.assetripper: raise ValueError('请先配置电脑 AssetRipper。')
                        report(message='AssetRipper 正在导出，进度按实际阶段更新。',progress=None)
                        unity_input=self._unity_input(pending/'unpacked',pending/'unity-input',event)
                        result=self.assetripper.export([unity_input],content,updated['engineVariant'] or 'auto',
                            cancel_event=event,progress_callback=lambda value:report(message=str(value.get('message',value.get('phase','AssetRipper 正在处理')))[:300],progress=None))
                        detail['assetripper']={k:v for k,v in result.items() if k not in ('output_dir','manifest_path')}
                        if result.get('status')=='partial':
                            count=result['failed_collections']
                            job_message=f'Unity/团结项目已导出，但有 {count} 个集合未能导出，请查看素材说明。'
                            detail.setdefault('issues',[]).append({'code':'unity_partial_export','message':job_message,'failedCollections':count})
                    else:
                        for file in files(pending/'unpacked'):
                            check_cancel(event)
                            if mode=='assets' and not asset_file(file): continue
                            dest=content/file.relative_to(pending/'unpacked'); dest.parent.mkdir(parents=True,exist_ok=True); shutil.copyfile(file,dest)
                    shutil.rmtree(pending/'unpacked',ignore_errors=True)
                    shutil.rmtree(pending/'unity-input',ignore_errors=True)
                exported_game={key:game[key] for key in ('platform','appId','name','nameSource','appIdSource','metadataSource','cacheLabel','version','cacheModifiedAt','packageCount','packageBytes','cacheFileCount','cacheBytes','inventoryKind','cacheAssociation','packageVersions','linkedCaches','sourceGameIds') if key in game}
                exported_game['iconAvailable']=False
                try:
                    icon=self.icon_file(id)
                    icon_path='metadata/app-icon'+icon.suffix
                    target=content/icon_path; target.parent.mkdir(parents=True,exist_ok=True); shutil.copyfile(icon,target)
                    exported_game.update(iconAvailable=True,iconArchivePath=icon_path,iconSource=game.get('iconSource','imported-metadata'))
                except FileNotFoundError: pass
                if mode=='raw': write_json(content/'manifest.json',{'formatVersion':1,'game':exported_game,'files':source_entries})
                shutil.rmtree(pending/'source-bundle',ignore_errors=True)
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
                          'createdAt':now(),'fileCount':len(inventory),'files':inventory,'spine':skeletons,'detail':detail,'game':exported_game,
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
            spec('list','List imported mini-game packages and downloaded resource caches with separate counts/bytes, explicit cache association, engine evidence and modification time. Unassociated caches remain separate. latest/earliest sort applies before pagination; unknown times are last. Phone cache scans are separate.',{'platform':{'enum':['all','douyin','wechat','unknown']},'offset':{'type':'integer','minimum':0},'limit':{'type':'integer','minimum':1,'maximum':100},'sort':{'enum':['latest','earliest']}}),
            spec('get','Read one game and its completed immutable exports.',{'id':ident},('id',)),
            spec('scan','Refresh imported desktop games. To scan host caches use the Android minigame tool through devhelper_call_tool.',{'platform':{'enum':['all','douyin','wechat','unknown']}},read_only=False),
            spec('import','Import a selected ZIP attachment, including a phone raw-cache export. Copies original before queuing; poll returned job.',{'attachmentId':ident,'platform':{'enum':['douyin','wechat','unknown']},'name':{'type':'string','maxLength':160}},('attachmentId',),False),
            spec('export','Queue raw, unpacked, asset or reconstructed Unity/Tuanjie project export. Originals retained. Poll job; queued is not success. summarize explicitly sends inventory to configured DeepSeek, not binary files.',{'gameId':ident,'mode':{'enum':['raw','unpacked','assets','unity-project']},'summarize':{'type':'boolean'},'notify':{'type':'boolean'}},('gameId','mode'),False),
            spec('job','Read persistent job state, progress and completed output. No task is replayed.',{'id':ident},('id',)),
            spec('cancel','Cancel a queued/running job; poll until stopped. Completed outputs and original caches remain.',{'id':ident},('id',),False),
            spec('rename','Edit a game display name; does not guess or change AppID.',{'id':ident,'name':{'type':'string','minLength':1,'maxLength':160}},('id','name'),False),
            spec('associate','Explicitly associate one independent downloaded cache with a package game on the same platform. Conflicting known AppIDs are rejected. Unknown AppIDs stay unknown; source UUID choices are recorded as manual. Raw export includes all linked sources.',{'cacheId':ident,'targetGameId':ident},('cacheId','targetGameId'),False),
            spec('unlink','Detach a cache by its original source UUID. The choice persists and prevents automatic relinking; files are retained.',{'cacheId':ident},('cacheId',),False),
            spec('identity','Explicitly set a platform-valid AppID on a selected game whose existing source identifiers do not conflict. Optional name sets its display label. Never infers identity from a cache hash.',{'id':ident,'appId':{'type':'string','minLength':10,'maxLength':66},'name':{'type':'string','minLength':1,'maxLength':160}},('id','appId'),False),
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
        if action=='associate': return self.associate_cache(args['cacheId'],args['targetGameId'])
        if action=='unlink': return self.unlink_cache(args['cacheId'])
        if action=='identity': return self.metadata_identity(args['id'],args['appId'],args.get('name'))
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
