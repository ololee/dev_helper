"""Immutable cloud catalog versions and verified, on-demand original downloads."""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import copy
import hashlib
import json
import math
import os
from pathlib import Path, PurePosixPath
import re
import threading
import time
import uuid

from baidu_drive import BaiduDriveError, _atomic_private
from catalog_snapshot import CatalogSnapshot

PROVIDER='baidu-netdisk'
MANIFEST_FORMAT='devhelper-catalog-manifest'
MAX_MANIFEST_BYTES=1024*1024


def _fsid(value):
    if isinstance(value,bool) or not isinstance(value,(str,int)) or not re.fullmatch(r'[0-9]{1,24}',str(value)) or int(value)<=0:
        raise ValueError('请选择有效的网盘文件 ID')
    return str(value)


def _digest_file(path):
    sha=hashlib.sha256()
    with Path(path).open('rb') as f:
        for chunk in iter(lambda:f.read(1024*1024),b''):sha.update(chunk)
    return sha.hexdigest()


class CloudCatalogService:
    def __init__(self,workspace):
        self.workspace=workspace;self.library=workspace.library
        self.root=workspace.root/'cloud-catalog';self.root.mkdir(mode=0o700,exist_ok=True)
        if self.root.is_symlink():raise ValueError('云索引目录不可为符号链接')
        self.root.chmod(0o700)
        self.transient=self.root/'transfers';self.transient.mkdir(mode=0o700,exist_ok=True)
        if self.transient.is_symlink():raise ValueError('云索引暂存目录不可为符号链接')
        self.state_path=self.root/'jobs.json'
        if self.state_path.is_symlink():raise ValueError('云索引状态不可为符号链接')
        self.lock=threading.RLock();self.closed=False;self.pool=ThreadPoolExecutor(max_workers=1,thread_name_prefix='cloud-catalog')
        self._jobs=json.loads(self.state_path.read_text()) if self.state_path.exists() else {}
        self.codec=CatalogSnapshot(self.library)
        for job in self._jobs.values():
            if job['state'] in ('queued','running'):
                job.update(state='interrupted',error='服务重启，原索引保留；请核对云端版本后重新操作',finishedAt=time.time())
        if self._jobs:_atomic_private(self.state_path,self._jobs)

    @property
    def drive(self):return self.workspace.drive

    def jobs(self):
        with self.lock:return copy.deepcopy(sorted(self._jobs.values(),key=lambda x:x['createdAt'],reverse=True))

    def job(self,identifier):
        with self.lock:
            if identifier not in self._jobs:raise BaiduDriveError('云索引任务不存在',404)
            return copy.deepcopy(self._jobs[identifier])

    def _update(self,identifier,**fields):
        with self.lock:
            self._jobs[identifier].update(fields,updatedAt=time.time());_atomic_private(self.state_path,self._jobs)

    def _start(self,kind,work,**fields):
        with self.lock:
            if self.closed:raise BaiduDriveError('云索引服务正在停止',409)
            if sum(j['state'] in ('queued','running') for j in self._jobs.values())>=8:raise BaiduDriveError('请等待当前云索引任务完成',409)
            identifier=str(uuid.uuid4());self._jobs[identifier]={'jobId':identifier,'kind':kind,'state':'queued','createdAt':time.time(),**fields}
            _atomic_private(self.state_path,self._jobs);self.pool.submit(self._run,identifier,work,fields)
        return self.job(identifier)

    def _run(self,identifier,work,fields):
        try:
            if self.closed:raise ValueError('服务正在停止')
            self._update(identifier,state='running',phase='preparing')
            result=work(identifier,**fields)
            self._update(identifier,state='completed',phase='completed',result=result,finishedAt=time.time())
        except Exception as error:
            self._update(identifier,state='interrupted' if self.closed else 'failed',error=self.workspace.public_error(error),finishedAt=time.time())

    def _wait(self,identifier,transfer):
        self._update(identifier,transferJobId=transfer['jobId'])
        while transfer['state'] in ('queued','running'):
            if self.closed or self.workspace._closed:self.drive.cancel(transfer['jobId'])
            time.sleep(.1);transfer=self.drive.job(transfer['jobId'])
        if transfer['state']!='completed':raise BaiduDriveError(transfer.get('error') or '云端传输尚未完成，请核对任务',409)
        return transfer['result']

    def locations(self,confirm_legacy=False):
        """Only completed, confirmed original uploads become restorable pointers."""
        locations=[]
        with self.workspace.lock:backups=copy.deepcopy(list(self.workspace.backups.values()))
        for backup in backups:
            for entry in backup.get('items',[]):
                receipt=entry.get('result') or {}
                if entry.get('state')!='completed' or receipt.get('pathConfirmed') is False:continue
                try:
                    item=self.library.asset(entry['assetId']);fs_id=_fsid(receipt.get('fsId'))
                    path=self.drive.remote_path(receipt.get('remotePath'),file=True)
                except (ValueError,KeyError,BaiduDriveError):continue
                if item['sha256']!=receipt.get('sha256') or item['bytes']!=receipt.get('bytes'):continue
                if receipt.get('pathConfirmed') is not True:
                    if not confirm_legacy:continue
                    # Older receipts had no confirmation flag. During an explicit
                    # publication only, verify them against current provider file
                    # metadata before using them as recoverable cloud pointers.
                    try:info=self.drive.file_info(fs_id)
                    except (ValueError,BaiduDriveError):continue
                    if info.get('isdir') or info.get('path')!=path or info.get('size')!=item['bytes']:continue
                    with self.workspace.lock:
                        stored=self.workspace.backups.get(backup['jobId'],{})
                        for saved in stored.get('items',[]):
                            if saved.get('transferJobId')==entry.get('transferJobId') and saved.get('assetId')==entry['assetId']:
                                saved['result']={**saved['result'],'pathConfirmed':True}
                        self.workspace._save_backups()
                locations.append(dict(assetId=item['id'],provider=PROVIDER,fsId=fs_id,path=path,sha256=item['sha256'],bytes=item['bytes']))
        return locations

    def _profiles(self):
        states=[self.workspace.ai.status()]
        states.extend(j.get('models',{}) for j in self.workspace.ai.jobs(limit=200))
        profiles={}
        for state in states:
            value=state.get('embedding',{})
            if value.get('space') and value.get('model') and value.get('baseUrl'):
                profiles[value['space']]={k:value.get(k) for k in ('space','baseUrl','model','dimensions')}
                profiles[value['space']].update(inputVersion='asset-metadata-text-v1',mode='chat')
        return list(profiles.values())

    def status(self):
        with self.library._db() as db:
            projects=db.execute('SELECT COUNT(*) FROM projects').fetchone()[0]
            assets=db.execute('SELECT COUNT(*) FROM assets').fetchone()[0]
            vectors=db.execute('SELECT COUNT(*) FROM embeddings e JOIN assets a ON a.id=e.asset_id WHERE e.sha256=a.sha256').fetchone()[0]
            restored={row[0] for row in db.execute('SELECT DISTINCT r.asset_id FROM remote_locations r JOIN assets a ON a.id=r.asset_id WHERE r.sha256=a.sha256 AND r.bytes=a.size')}
        restored.update(x['assetId'] for x in self.locations())
        return {'local':{'projectCount':projects,'assetCount':assets,'embeddingCount':vectors,'uploadedAssetCount':len(restored),'fetchableAssetCount':len(restored)},
                'activeJobs':sum(j['state'] in ('queued','running') for j in self.jobs()),'defaultRemoteDirectory':'catalogs','runsModelOnPhone':False,
                'format':'SQLite metadata and float32 vectors; original files are fetched separately','includesSecrets':False}

    def publish(self,project_ids=None,remote_directory='catalogs'):
        if project_ids is not None and (not isinstance(project_ids,list) or not 1<=len(project_ids)<=1000 or len(set(project_ids))!=len(project_ids)):
            raise ValueError('请选择有效工程列表')
        for project_id in project_ids or []:self.library.project(project_id)
        directory=self.drive.remote_path(remote_directory).rstrip('/')
        self.drive._token()
        return self._start('publish',self._publish,projectIds=project_ids,remoteDirectory=directory)

    @staticmethod
    def _counts(summary):
        counts=summary.get('counts',{})
        return {'projectCount':counts.get('projects',counts.get('projectCount',0)),
                'assetCount':counts.get('assets',counts.get('assetCount',0)),
                'embeddingCount':counts.get('embeddings',counts.get('embeddingCount',0)),
                'uploadedAssetCount':summary.get('uploadedAssetCount',counts.get('locatedAssets',counts.get('uploadedAssetCount',counts.get('locations',0))))}

    def _confirmed(self,receipt,target,sha,size):
        if receipt.get('sha256')!=sha or receipt.get('bytes')!=size or receipt.get('pathConfirmed') is not True:
            raise BaiduDriveError('索引传输未确认摘要、大小或实际路径，请核对网盘后再操作',409)
        path=receipt.get('remotePath')
        if not isinstance(path,str) or path!=target:raise ValueError('索引回执路径与计划不一致')
        if receipt.get('fsId'):return {**receipt,'fsId':_fsid(receipt['fsId'])}
        result=self.drive.list_files(path.rsplit('/',1)[0],limit=1000)
        found=[x for x in result['items'] if x.get('path')==path and not x.get('isdir') and x.get('size')==size]
        if len(found)!=1:raise ValueError('网盘尚未确认索引文件 ID，请先核对云端版本')
        return {**receipt,'fsId':_fsid(found[0]['fs_id'])}

    def _publish(self,identifier,projectIds,remoteDirectory):
        path=self.transient/(identifier+'.sqlite3');manifest_path=self.transient/(identifier+'.json')
        try:
            self._update(identifier,phase='exporting')
            summary=self.codec.export(path,project_ids=projectIds,locations=self.locations(confirm_legacy=True),query_profiles=self._profiles())
            snapshot_id=summary['snapshotId'];directory=remoteDirectory+'/'+snapshot_id
            self.workspace._ensure_archive_directory(directory,set())
            sha=summary.get('sha256') or _digest_file(path);size=path.stat().st_size
            self._update(identifier,phase='uploading-index',snapshotId=snapshot_id,catalogBytes=size)
            receipt=self._wait(identifier,self.drive.start_upload(path,directory+'/catalog.sqlite3',name='catalog.sqlite3'))
            receipt=self._confirmed(receipt,directory+'/catalog.sqlite3',sha,size)
            counts=self._counts(summary)
            manifest={'format':MANIFEST_FORMAT,'schemaVersion':1,'snapshotId':snapshot_id,'createdAt':time.time(),
                      'catalogFsId':receipt['fsId'],'catalogFile':'catalog.sqlite3','catalogSha256':sha,'catalogBytes':size,
                      'remoteDirectory':directory,**counts,'queryProfiles':summary.get('queryProfiles',[]),
                      'originalFilesIncluded':False,'credentialsIncluded':False}
            _atomic_private(manifest_path,manifest)
            self._update(identifier,phase='uploading-manifest')
            manifest_receipt=self._wait(identifier,self.drive.start_upload(manifest_path,directory+'/manifest.json',name='manifest.json'))
            manifest_receipt=self._confirmed(manifest_receipt,directory+'/manifest.json',_digest_file(manifest_path),manifest_path.stat().st_size)
            return {**manifest,'manifestFsId':manifest_receipt['fsId'],'manifestRemotePath':manifest_receipt['remotePath']}
        finally:
            path.unlink(missing_ok=True);manifest_path.unlink(missing_ok=True)

    def _download(self,identifier,fs_id,path,expected_sha=None):
        return self._wait(identifier,self.drive.start_download(_fsid(fs_id),path,expected_sha256=expected_sha))

    def _load_manifest(self,fs_id):
        fs_id=_fsid(fs_id);info=self.drive.file_info(fs_id)
        if info.get('isdir') or not 0<int(info.get('size',0))<=MAX_MANIFEST_BYTES or PurePosixPath(info['path']).name!='manifest.json':
            raise ValueError('请选择云索引的 manifest.json')
        directory=info['path'].rsplit('/',1)[0];snapshot_id=directory.rsplit('/',1)[-1]
        try:
            if str(uuid.UUID(snapshot_id))!=snapshot_id:raise ValueError()
        except (ValueError,TypeError):raise ValueError('云索引版本目录无效') from None
        path=self.transient/(str(uuid.uuid4())+'.manifest.json')
        try:
            transfer=self.drive.start_download(fs_id,path)
            while transfer['state'] in ('queued','running'):
                if self.closed:self.drive.cancel(transfer['jobId'])
                time.sleep(.1);transfer=self.drive.job(transfer['jobId'])
            if transfer['state']!='completed':raise BaiduDriveError(transfer.get('error') or '云索引清单下载失败',409)
            if path.stat().st_size>MAX_MANIFEST_BYTES:raise ValueError('索引清单超过解析范围')
            value=json.loads(path.read_text('utf-8'))
            if (not isinstance(value,dict) or value.get('format')!=MANIFEST_FORMAT or isinstance(value.get('schemaVersion'),bool) or value.get('schemaVersion')!=1 or value.get('snapshotId')!=snapshot_id
                or value.get('catalogFile')!='catalog.sqlite3' or not isinstance(value.get('catalogSha256'),str) or not re.fullmatch('[0-9a-f]{64}',value['catalogSha256'])):
                raise ValueError('云索引清单格式或版本不匹配')
            timestamp=value.get('createdAt')
            if isinstance(timestamp,bool) or not isinstance(timestamp,(int,float)) or not math.isfinite(timestamp) or not 0<=timestamp<=10**12:
                raise ValueError('云索引时间格式错误')
            for key in ('catalogBytes','projectCount','assetCount','embeddingCount','uploadedAssetCount'):
                if isinstance(value.get(key),bool) or not isinstance(value.get(key),int) or value[key]<0:raise ValueError('云索引计数格式错误')
            if not 0<value['catalogBytes']<=2*1024**3:raise ValueError('该索引超过当前单版本 2 GiB 解析范围')
            fs=_fsid(value.get('catalogFsId'));catalog_info=self.drive.file_info(fs)
            if catalog_info.get('isdir') or catalog_info.get('path')!=directory+'/catalog.sqlite3' or catalog_info.get('size')!=value['catalogBytes']:
                raise ValueError('索引文件与版本清单不匹配，请核对网盘')
            # Never forward arbitrary extra manifest fields or provider URLs.
            allowed=('format','schemaVersion','snapshotId','createdAt','catalogFsId','catalogFile','catalogSha256','catalogBytes',
                     'projectCount','assetCount','embeddingCount','uploadedAssetCount')
            return {k:value[k] for k in allowed if k in value}|{'directory':directory,'manifestFsId':fs_id}
        finally:path.unlink(missing_ok=True)

    def snapshots(self,remote_directory='catalogs'):
        directory=self.drive.remote_path(remote_directory).rstrip('/')
        try:listing=self.drive.list_files(directory,limit=100)
        except BaiduDriveError as error:
            if error.provider_code=='-9':return {'items':[],'directory':directory,'hasMore':False,'errors':[]}
            raise
        items=[];errors=[]
        dirs=[x for x in listing['items'] if x.get('isdir') and re.fullmatch('[0-9a-f-]{36}',PurePosixPath(x.get('path','')).name)]
        for entry in dirs[:20]:
            try:
                children=self.drive.list_files(entry['path'],limit=100)
                manifests=[x for x in children['items'] if not x.get('isdir') and x.get('server_filename',PurePosixPath(x.get('path','')).name)=='manifest.json']
                if len(manifests)==1:items.append(self._load_manifest(manifests[0]['fs_id']))
            except (ValueError,BaiduDriveError) as error:errors.append({'snapshotId':PurePosixPath(entry['path']).name,'error':self.workspace.public_error(error)})
        items.sort(key=lambda x:x.get('createdAt',0),reverse=True)
        return {'items':items,'directory':directory,'hasMore':len(dirs)>20 or listing.get('hasMore',False),'errors':errors}

    def restore(self,manifest_fs_id):
        return self._start('restore',self._restore,manifestFsId=_fsid(manifest_fs_id))

    def _restore(self,identifier,manifestFsId):
        self._update(identifier,phase='reading-manifest');manifest=self._load_manifest(manifestFsId)
        path=self.transient/(identifier+'.sqlite3')
        try:
            self._update(identifier,phase='downloading-index',snapshotId=manifest['snapshotId'],catalogBytes=manifest['catalogBytes'])
            self._download(identifier,manifest['catalogFsId'],path,manifest['catalogSha256'])
            if path.stat().st_size!=manifest['catalogBytes']:raise ValueError('索引文件大小与清单不一致')
            inspected=self.codec.inspect(path,expected_sha256=manifest['catalogSha256'])
            if inspected['snapshotId']!=manifest['snapshotId'] or self._counts(inspected)!=self._counts({'counts':manifest}):
                raise ValueError('索引内容与版本清单不一致')
            self._update(identifier,phase='restoring')
            result=self.codec.restore(path,expected_sha256=manifest['catalogSha256'])
            return {**result,'manifestFsId':manifestFsId,'noRescan':True,'noModelCall':True,'originalFilesDownloaded':0}
        finally:path.unlink(missing_ok=True)

    def fetch(self,asset_id):
        asset=self.library.asset(asset_id)
        if asset.get('availability') in ('local','cached'):
            # Opening checks the saved descriptor signature; a stale local cache
            # must not be reported as already available.
            try:
                with self.library.open_file(asset_id):pass
                return self._start('fetch',lambda identifier,assetId:{'assetId':assetId,'alreadyAvailable':True},assetId=asset_id)
            except ValueError:pass
        locations=self.library.remote_locations(asset_id)
        if not locations:locations=[x for x in self.locations() if x['assetId']==asset_id]
        if not locations:raise BaiduDriveError('该素材尚未上传云端，请先在原电脑上传原文件',409)
        return self._start('fetch',self._fetch,assetId=asset_id)

    def _fetch(self,identifier,assetId):
        item=self.library.asset(assetId);locations=self.library.remote_locations(assetId) or [x for x in self.locations() if x['assetId']==assetId]
        candidates=[x for x in locations if x['provider']==PROVIDER and x['sha256']==item['sha256'] and x['bytes']==item['bytes']]
        if not candidates:raise ValueError('没有与当前资源摘要匹配的云端原文件')
        location=candidates[-1];path=self.transient/(identifier+'.original')
        try:
            info=self.drive.file_info(location['fsId'])
            if info.get('isdir') or info.get('size')!=item['bytes']:raise ValueError('云端原文件大小已变化')
            self._update(identifier,phase='downloading-original',bytes=item['bytes'])
            self._download(identifier,location['fsId'],path,item['sha256'])
            self.library.cache_file(assetId,path)
            return {'assetId':assetId,'sha256':item['sha256'],'bytes':item['bytes'],'availability':'cached','verified':True}
        finally:path.unlink(missing_ok=True)

    def close(self):
        self.closed=True;self.pool.shutdown(wait=True,cancel_futures=False)
