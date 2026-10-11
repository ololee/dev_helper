"""Read-only mini-game package formats and evidence based engine detection.

The Douyin v2 key derivation was documented in the user supplied TTPKG
research material. It is a versioned decoder, not a universal platform API.
No package JavaScript or executable content is ever evaluated.
"""
from __future__ import annotations

import hashlib
import json
import re
import shutil
import stat
import struct
from pathlib import Path
import zipfile

MAX_FILES = 100000
MAX_HEADER = 16 * 1024 * 1024
MAX_EXPANDED = 32 * 1024**3  # Per operation, not a media library quota.
TEXT_EXT = ('.js', '.json', '.ttml', '.wxml', '.wxss', '.ttss')
ASSET_EXT = {'.png','.jpg','.jpeg','.webp','.gif','.bmp','.ktx','.ktx2','.pvr','.astc',
             '.mp3','.ogg','.wav','.m4a','.mp4','.webm','.ttf','.otf','.fnt','.atlas',
             '.skel','.bytes','.json','.ccon','.cconb','.bin','.obj','.mtl','.glb','.gltf',
             '.bundle','.unity3d','.asset','.assets','.res','.ress','.resource','.data','.pck'}
_LIZ = bytes(v & 255 for v in [45,-75,42,50,98,-31,-5,72,-62,55,-112,87,0,103,12,19,-31,43,-110,-61,-110,-16,-32,58,-96,119,-38,-30,85,36,-79,29,61,37,95,-5,121,69,-61,31,-54,66,-118,-46,-36,69,104,-91,125,-125,-92,36,-119,-28,111,82,115,108,13,-45,86,31,-3,3])
_LIZIZ = bytes(v & 255 for v in [-66,17,-69,-98,-60,126,100,107,-22,-34,-12,-47,-126,112,-68,-27,119,63,-111,-28,102,5,-72,98,58,10,-32,4,-95,-21,-85,-120,-84,-100,-55,29,-82,106,-76,-112,43,-56,12,-13,-87,-124,1,78,69,91,100,105,-113,62,-49,-26,60,-24,-10,-1,97,98,-42,109])


class FormatError(ValueError):
    pass


def safe_path(value: str) -> str:
    if not isinstance(value,str) or not value or len(value)>2048 or '\\' in value or ':' in value:
        raise FormatError('资源路径无效。')
    if any(ord(c)<32 or ord(c)==127 for c in value) or any(p in ('','.','..') for p in value.split('/')):
        raise FormatError('资源路径越界。')
    if len(value.split('/'))>64:
        raise FormatError('资源目录层级过深。')
    return value


def check_cancel(cancel=None):
    if cancel is not None and cancel.is_set():
        raise InterruptedError('任务已取消。')


def rc4(data: bytes, key: bytes) -> bytes:
    s=list(range(256)); j=0
    for i in range(256):
        j=(j+s[i]+key[i%len(key)])&255; s[i],s[j]=s[j],s[i]
    out=bytearray(len(data)); i=j=0
    for n,v in enumerate(data):
        i=(i+1)&255; j=(j+s[i])&255; s[i],s[j]=s[j],s[i]
        out[n]=v^s[(s[i]+s[j])&255]
    return bytes(out)


def derive_key(seed: bytes) -> bytes:
    return hashlib.sha512(hashlib.sha512(seed).digest()+bytes(a^b for a,b in zip(_LIZ,_LIZIZ))).digest()[:32]


def _exact(stream,n):
    if n<0 or n>MAX_HEADER: raise FormatError('包索引长度无效。')
    value=stream.read(n)
    if len(value)!=n: raise FormatError('包文件不完整。')
    return value


def _table_tpkg(stream,size):
    if _exact(stream,4)!=b'TPKG': raise FormatError('不是 TPKG 包。')
    version,length=struct.unpack('<ii',_exact(stream,8))
    if version!=2: raise FormatError(f'当前支持 TPKG v2，此包版本为 {version}；原包已保留。')
    block=_exact(stream,length)
    if len(block)<8: raise FormatError('TPKG 元信息缺失。')
    jlen=struct.unpack_from('<i',block)[0]
    if jlen<4 or jlen+4>len(block): raise FormatError('TPKG 元信息越界。')
    try:
        meta=json.loads(block[8:8+jlen-4]); seed=meta['__ttks']
        if not isinstance(seed,str) or not 1<=len(seed)<=256: raise ValueError()
        key=derive_key(seed.encode())
    except (ValueError,KeyError,TypeError,UnicodeError): raise FormatError('TPKG 密钥元信息不受支持。') from None
    count=struct.unpack('<i',_exact(stream,4))[0]
    if not 0<count<=MAX_FILES: raise FormatError('TPKG 条目数量无效。')
    table=[]
    for _ in range(count):
        n=struct.unpack('<i',_exact(stream,4))[0]
        if not 1<=n<=2048: raise FormatError('TPKG 文件名解码失败，宿主密钥版本可能不兼容。')
        entry=rc4(_exact(stream,n+8),key)
        try: name=safe_path(entry[:n].decode('utf-8',errors='strict'))
        except UnicodeError: raise FormatError('TPKG 文件名解码失败，宿主密钥版本可能不兼容。') from None
        offset,length=struct.unpack_from('<ii',entry,n)
        table.append((name,offset,length))
    return table,key,stream.tell(),'ttpkg-v2-rc4'


def _table_wx(stream,size):
    header=_exact(stream,14)
    if header[0]!=0xbe or header[13]!=0xed:
        raise FormatError('此微信包不是明文 WXAPKG，可能受平台加密；支持导出原包，不尝试绕过账号密钥。')
    index_size,body_size=struct.unpack_from('>II',header,5)
    if index_size>MAX_HEADER or 14+index_size+body_size!=size: raise FormatError('WXAPKG 索引或数据大小无效。')
    count=struct.unpack('>I',_exact(stream,4))[0]
    if not 0<count<=MAX_FILES: raise FormatError('WXAPKG 条目数量无效。')
    table=[]
    for _ in range(count):
        n=struct.unpack('>I',_exact(stream,4))[0]
        try: name=safe_path(_exact(stream,n).decode().lstrip('/'))
        except UnicodeError: raise FormatError('WXAPKG 文件名无效。') from None
        offset,length=struct.unpack('>II',_exact(stream,8)); table.append((name,offset,length))
    if stream.tell()!=14+index_size: raise FormatError('WXAPKG 索引长度不匹配。')
    return table,None,stream.tell(),'wxapkg-plaintext'


def unpack_package(source: Path, output: Path, cancel=None, progress=None) -> dict:
    """Validate the entire index before creating any output; stream file contents."""
    size=source.stat().st_size
    with source.open('rb') as stream:
        magic=stream.read(4); stream.seek(0)
        if magic==b'TPKG': table,key,end,decoder=_table_tpkg(stream,size)
        elif magic[:1]==b'\xbe': table,key,end,decoder=_table_wx(stream,size)
        else: raise FormatError('不支持的小游戏包格式；请保留原包。')
        names=set(); total=0; spans=[]
        for name,offset,length in table:
            folded=name.casefold()
            if folded in names: raise FormatError('包含重复或大小写冲突的路径。')
            names.add(folded)
            if offset<end or length<0 or offset+length>size: raise FormatError('包文件范围越界。')
            spans.append((offset,offset+length)); total+=length
        if total>MAX_EXPANDED: raise FormatError('单次解包超过 32 GiB，请分包处理。')
        nonempty=sorted((a,b) for a,b in spans if a!=b)
        if any(a<previous[1] for previous,(a,b) in zip(nonempty,nonempty[1:])): raise FormatError('包数据范围重叠。')
        # Prefix conflicts would otherwise turn a file into a directory mid-extract.
        if any('/'.join(name.split('/')[:n]).casefold() in names for name,_,_ in table for n in range(1,len(name.split('/')))):
            raise FormatError('包文件与目录冲突。')
        output.mkdir(parents=True,exist_ok=False)
        try:
            for index,(name,offset,length) in enumerate(table):
                check_cancel(cancel); target=output/name; target.parent.mkdir(parents=True,exist_ok=True)
                stream.seek(offset)
                with target.open('xb') as dst:
                    left=length; position=0
                    while left:
                        check_cancel(cancel); chunk=stream.read(min(8192,left))
                        if not chunk: raise FormatError('包读取中断。')
                        if key:
                            n=min(1024,len(chunk)) if name.lower().endswith(TEXT_EXT) else min(64,len(chunk)) if position==0 else 0
                            chunk=rc4(chunk[:n],key)+chunk[n:] if n else chunk
                        dst.write(chunk); left-=len(chunk); position+=len(chunk)
                if progress: progress(index+1,len(table),name)
        except BaseException:
            shutil.rmtree(output,ignore_errors=True); raise
    return {'decoder':decoder,'files':len(table),'bytes':total}


def unpack_zip(source: Path, output: Path, cancel=None):
    """Reject links, traversal, duplicates and bombs; never extract executable code."""
    with zipfile.ZipFile(source) as archive:
        entries=archive.infolist()
        if len(entries)>MAX_FILES: raise FormatError('ZIP 条目过多。')
        total=0; names=set(); files=[]
        for item in entries:
            name=safe_path(item.filename.rstrip('/'))
            if item.flag_bits&1 or stat.S_ISLNK(item.external_attr>>16): raise FormatError('ZIP 不支持加密文件或符号链接。')
            folded=name.casefold()
            if folded in names: raise FormatError('ZIP 含重复路径。')
            names.add(folded); total+=item.file_size
            if total>MAX_EXPANDED: raise FormatError('单次导入超过 32 GiB，请分包处理。')
            if item.file_size>16*1024**2 and item.file_size>max(item.compress_size,1)*2000: raise FormatError('ZIP 压缩倍率异常。')
            if not item.is_dir(): files.append((item,name))
        output.mkdir(parents=True,exist_ok=False)
        try:
            for item,name in files:
                check_cancel(cancel); target=output/name; target.parent.mkdir(parents=True,exist_ok=True)
                with archive.open(item) as src,target.open('xb') as dst:
                    while chunk:=src.read(1024*1024): check_cancel(cancel); dst.write(chunk)
        except BaseException: shutil.rmtree(output,ignore_errors=True); raise
    return {'decoder':'zip','files':len(files),'bytes':total}


def inspect_tree(root: Path) -> dict:
    """Use paired file/header/content evidence. Never infer engine from Wasm alone."""
    paths=sorted(p for p in root.rglob('*') if p.is_file() and not p.is_symlink())
    evidence=[]; versions=[]; cocos=[]; unity=[]; godot=[]; laya=[]; tuanjie=[]; name=''; app_id=''
    for file in paths[:MAX_FILES]:
        relative=file.relative_to(root).as_posix(); lower=relative.lower()
        if file.is_file():
            with file.open('rb') as stream: blob=stream.read(128*1024 if file.suffix.lower() in ('.js','.json','.txt') else 4096)
            text=blob.decode('utf-8',errors='ignore')
            is_unity_header=blob.startswith((b'UnityFS\0',b'UnityRaw\0',b'UnityWeb\0'))
            serialized=False
            if not is_unity_header and len(blob)>=20:
                metadata_size,total_size,format_version,data_offset=struct.unpack_from('>4I',blob)
                version_offset=20
                if format_version>=22 and len(blob)>=48:
                    metadata_size,total_size,data_offset=struct.unpack_from('>IQQ',blob,20); version_offset=48
                serialized=(9<=format_version<=30 and total_size==file.stat().st_size and
                            0<metadata_size<total_size and version_offset<data_offset<=total_size and
                            bool(re.match(rb'\d{1,4}\.\d+\.\d+[ft]\d+\x00',blob[version_offset:version_offset+64])))
            engine_version=re.search(r'\b(\d{1,4}\.\d+\.\d+[ft]\d+)\b',text[:1024]) if is_unity_header or serialized else None
            if engine_version: versions.append(engine_version.group(1))
            if engine_version and re.search(r't\d+$',engine_version.group(1)): tuanjie.append((relative,engine_version.group(1)))
            if is_unity_header and re.search(r'\b(?:Tuanjie|团结引擎)\b',text): tuanjie.append((relative,'Tuanjie 标识'))
            if is_unity_header or serialized: unity.append((relative,'Unity 序列化文件头' if serialized else 'Unity 资源包头'))
            if re.search(r'createUnityInstance|UnityLoader\.instantiate|unityInstance',text): unity.append((relative,'Unity loader 调用'))
            if re.search(r'(?:cc\._RF|cc\.Class|cc\.game|System\.register\([^\n]{0,100}(?:cc|cocos))',text) or re.search(r'cocos[- ](?:creator|engine)|cocos-js/cc',text,re.I): cocos.append((relative,'Cocos 引擎或模块调用'))
            if 'cc.SpriteFrame' in text or 'cc.Asset' in text: cocos.append((relative,'Cocos 序列化类型'))
            if re.search(r'Godot Engine|Godot\.|new Engine\(.*GODOT|GODOT_CONFIG',text): godot.append((relative,'Godot 运行时配置'))
            if blob.startswith(b'GDPC'): godot.append((relative,'Godot PCK 包头'))
            if re.search(r'Laya\.init|Laya3D\.init|laya\.core|layaair',text,re.I): laya.append((relative,'Laya 运行时调用'))
            if file.suffix.lower()=='.json' and file.stat().st_size<=MAX_HEADER and any(token in lower for token in ('manifest','config','metadata','settings','appinfo')):
                try:
                    obj=json.loads(file.read_text('utf-8'))
                    if isinstance(obj,dict):
                        for k in ('appId','appid','app_id'):
                            candidate=obj.get(k)
                            if isinstance(candidate,str) and re.fullmatch(r'(?:tt|wx)[A-Za-z0-9]{8,64}',candidate): app_id=app_id or candidate
                        for k in ('appName','gameName','displayName'):
                            candidate=obj.get(k)
                            if isinstance(candidate,str) and 0<len(candidate)<=150: name=name or candidate
                except (ValueError,UnicodeError,OSError): pass
    engine='unknown'; chosen=[]
    if tuanjie and (unity or versions): engine='tuanjie'; chosen=tuanjie+unity
    elif unity: engine='unity'; chosen=unity
    elif cocos: engine='cocos'; chosen=cocos
    elif godot: engine='godot'; chosen=godot
    elif laya: engine='laya'; chosen=laya
    evidence=[{'path':p,'match':m} for p,m in chosen[:24]]
    version=next((v for v in versions if re.search(r't\d+$',v)),versions[0] if versions else '') if engine=='tuanjie' else (versions[0] if versions else '')
    if engine=='cocos':
        for file in paths[:MAX_FILES]:
            if file.suffix.lower() in ('.json','.js'):
                with file.open('rb') as stream: text=stream.read(128*1024).decode('utf-8',errors='ignore')
                match=re.search(r'(?:Cocos\s*Creator|engineVersion|cc\.ENGINE_VERSION|CocosEngine)["\s:=]+(?:v)?(\d+\.\d+\.\d+)',text,re.I)
                if match: version=match.group(1); break
    return {'engine':engine,'engineVariant':'tuanjie' if engine=='tuanjie' else 'unity' if engine=='unity' and re.search(r'f\d+$',version) else '',
            'engineVersion':version,'confidence':'high' if len(chosen)>1 else 'medium' if chosen else 'unknown',
            'evidence':evidence,'appId':app_id,'name':name,'fileCount':len(paths),'bytes':sum(p.stat().st_size for p in paths)}
