package cn.ololee.devhelper.netdisk

import android.content.Context
import android.content.Intent
import android.net.Uri
import android.provider.DocumentsContract
import android.system.Os
import android.system.OsConstants
import com.google.gson.JsonArray
import com.google.gson.JsonElement
import com.google.gson.JsonObject
import com.google.gson.JsonParser
import java.io.File
import java.io.FileInputStream
import java.io.FileOutputStream
import java.nio.file.Files
import java.util.UUID
import java.util.concurrent.CancellationException
import java.util.concurrent.ConcurrentHashMap
import java.util.concurrent.Executors
import java.util.concurrent.atomic.AtomicInteger
import java.util.concurrent.atomic.AtomicBoolean

/** Android-native local/SAF ↔ Baidu batch operations, independent of the computer service. */
class NativeFileTransferRuntime private constructor(context: Context, private val cloudOverride:NativeTransferCloud?=null, private val foregroundStarter:(()->Unit)?=null) {
    private val context=context.applicationContext
    private val lock=Any()
    private val home=File(this.context.filesDir,"native-file-transfers").apply {mkdirs()}
    private val defaultRoot=File(this.context.getExternalFilesDir(null)?:this.context.filesDir,"resource-files").apply {mkdirs()}.canonicalFile
    private val rootsFile=File(home,"roots.json")
    private val jobsFile=File(home,"jobs.json")
    private val roots=mutableListOf<String>()
    private val records=linkedMapOf<String,JsonObject>()
    private val plans=mutableMapOf<String,NativeTransferEngine.Plan>()
    private val cancellations=mutableMapOf<String,AtomicBoolean>()
    private val activeCount=AtomicInteger(0)
    private val parentRefs=ConcurrentHashMap<String,String>()
    private val executor=Executors.newSingleThreadExecutor { r->Thread(r,"native-file-transfer").apply {isDaemon=true} }
    private val engine=NativeTransferEngine(File(home,"staging"))
    private var lastPersist=0L
    init {
        runCatching {JsonParser.parseString(rootsFile.readText()).asJsonArray.forEach { roots.add(it.asString) }}
        runCatching {JsonParser.parseString(jobsFile.readText()).asJsonArray.forEach { element ->
            val job=element.asJsonObject
            if(job.get("state")?.asString in listOf("queued","running")) {
                job.addProperty("state","interrupted");job.addProperty("error","应用已重启，任务已中断。请查看已有目标后重新选择；不会恢复删除源文件。")
                job.addProperty("cancelSupported",false);job.addProperty("interruptedAt",System.currentTimeMillis())
            }
            records[job.get("jobId").asString]=job
        }}
        persist()
        if(cloudOverride==null)BaiduNetdiskRuntime.get(this.context).registerTransferBusyProvider { activeCount.get()>0 }
    }
    companion object {
        @Volatile private var instance: NativeFileTransferRuntime?=null
        @JvmStatic fun get(context: Context): NativeFileTransferRuntime = instance?:synchronized(this) {instance?:NativeFileTransferRuntime(context).also {instance=it}}
        internal fun createForTests(context:Context,cloud:NativeTransferCloud,foregroundStarter:()->Unit={}):NativeFileTransferRuntime = NativeFileTransferRuntime(context,cloud,foregroundStarter)
    }
    fun status(): JsonObject = synchronized(lock) { JsonObject().apply {
        addProperty("defaultUri",Uri.fromFile(defaultRoot).toString());addProperty("mcpPath","/mcp/netdisk")
        addProperty("jobsPath","/api/netdisk/file-ops/jobs");addProperty("encoding","binary");addProperty("native",true)
        add("roots",JsonArray().apply {
            add(JsonObject().apply {addProperty("uri",Uri.fromFile(defaultRoot).toString());addProperty("name","手机资源文件");addProperty("default",true);addProperty("readable",true);addProperty("writable",true)})
            roots.forEach { saved -> runCatching {val store=LocalStore();val node=store.stat(saved)
                add(JsonObject().apply {addProperty("uri",saved);addProperty("name",node?.name?:"已授权手机目录");addProperty("default",false);addProperty("readable",node!=null);addProperty("writable",hasGrant(Uri.parse(saved),true))})
            }.onFailure {add(JsonObject().apply {addProperty("uri",saved);addProperty("name","目录授权已失效，请重新选择");addProperty("default",false);addProperty("readable",false);addProperty("writable",false)})} }
        })
    } }
    fun registerTree(uri: Uri): JsonObject {
        require(uri.scheme=="content"&&DocumentsContract.isTreeUri(uri)&&uri.query==null&&uri.fragment==null) {"请选择手机文件夹"}
        require(hasGrant(uri,false)&&hasGrant(uri,true)) {"需要系统文件夹选择器授予可持久的读写权限"}
        val root=DocumentsContract.buildDocumentUriUsingTree(uri,DocumentsContract.getTreeDocumentId(uri))
        synchronized(lock) {if(uri.toString() !in roots)roots.add(uri.toString());save(rootsFile,JsonArray().apply {roots.forEach {add(it)}})}
        try {LocalStore().stat(root.toString())?:error("无法读取该文件夹")}
        catch(t:Throwable) {synchronized(lock) {roots.remove(uri.toString());save(rootsFile,JsonArray().apply {roots.forEach {add(it)}})};throw t}
        return status()
    }
    fun localFiles(args: JsonObject): JsonObject {
        only(args,setOf("pathUri","offset","limit"))
        val store=LocalStore();val uri=text(args,"pathUri",Uri.fromFile(defaultRoot).toString())
        val node=store.stat(uri)?:error("手机目录不存在");require(node.directory) {"请选择文件夹"}
        val offset=integer(args,"offset",0,0,Int.MAX_VALUE);val limit=integer(args,"limit",100,1,1000)
        val rows=store.children(node).sortedWith(compareBy<TransferNode> {!it.directory}.thenBy {it.name.lowercase()}.thenBy {it.name})
        return JsonObject().apply {
            addProperty("pathUri",node.ref);node.parent?.let {addProperty("parentUri",it)};addProperty("name",node.name)
            addProperty("offset",offset);addProperty("limit",limit);addProperty("hasMore",offset.toLong()+limit<rows.size)
            add("items",JsonArray().apply {rows.drop(offset).take(limit).forEach {n -> add(nodeJson(n))}})
        }
    }
    fun plan(args: JsonObject): JsonObject {
        only(args,setOf("scope","destinationScope","operation","items","destination"))
        val source=text(args,"scope");val target=text(args,"destinationScope")
        require(source in listOf("local","netdisk") && target in listOf("local","netdisk") && source!=target) {"请选择本地和百度网盘之间的目标"}
        val op=text(args,"operation");val array=args.get("items")?.takeIf {it.isJsonArray}?.asJsonArray?:error("items 必须为数组")
        require(array.size() in 1..100) {"每批选择 1 至 100 个项目"}
        val destination=args.get("destination")?.takeIf {it.isJsonObject}?.asJsonObject?:error("请选择目标文件夹")
        only(destination,if(target=="local")setOf("uri") else setOf("path"))
        val refs=array.map {element ->require(element.isJsonObject) {"项目必须为对象"};val item=element.asJsonObject
            only(item,if(source=="local")setOf("uri") else setOf("path","fsId"));text(item,if(source=="local")"uri" else "path") }
        val cloud=CloudStore(cloudClient())
        val local=LocalStore();val sourceStore=if(source=="local")local else cloud;val targetStore=if(target=="local")local else cloud
        array.forEachIndexed { index,element ->if(source=="netdisk"&&element.asJsonObject.has("fsId")) {
            val node=cloud.stat(refs[index])?:error("网盘文件不存在")
            require(node.extra.get("fsId")?.asString==text(element.asJsonObject,"fsId")) {"网盘文件 ID 已改变，请刷新列表"}
        } }
        val plan=engine.plan(sourceStore,targetStore,op,refs,text(destination,if(target=="local")"uri" else "path"),cloud.client.account())
        synchronized(lock) {plans.entries.removeAll {it.value.expires<System.currentTimeMillis()};require(plans.size<100) {"操作计划太多，请稍后重试"};plans[plan.id]=plan}
        return plan.publicJson()
    }
    fun execute(args: JsonObject): JsonObject {
        only(args,setOf("planId","planDigest"));val planId=text(args,"planId");val digest=text(args,"planDigest")
        val plan=synchronized(lock) {plans[planId]?:error("计划已过期或应用已重启，请重新粘贴")}
        require(plan.digest==digest&&plan.expires>=System.currentTimeMillis()) {"操作计划已改变或过期，请重新粘贴"}
        synchronized(lock) {records.values.firstOrNull {it.get("planId")?.asString==planId}?.let {return it.deepCopy()}}
        val id=UUID.randomUUID().toString();val cancellation=AtomicBoolean(false)
        val record=JsonObject().apply {
            addProperty("id",id);addProperty("jobId",id);addProperty("planId",planId);addProperty("scope","transfer")
            addProperty("sourceScope",plan.source.scope);addProperty("destinationScope",plan.target.scope);addProperty("operation",plan.operation)
            addProperty("state","queued");addProperty("createdAt",System.currentTimeMillis());addProperty("updatedAt",System.currentTimeMillis())
            addProperty("cancelSupported",true);addProperty("cancelRequested",false);addProperty("encoding","binary")
            add("items",plan.publicJson().getAsJsonArray("items"));add("progress",JsonObject().apply {
                addProperty("phase","queued");addProperty("bytesTransferred",0);addProperty("totalBytes",plan.bytes*4)
                addProperty("fileCount",plan.fileCount);addProperty("completedFiles",0);addProperty("currentFile","")
            })
        }
        val accepted=configurationGuard {
            check(cloudClient().account()==plan.account) {"百度账号或应用配置已改变，请重新粘贴"}
            if(foregroundStarter!=null)foregroundStarter.invoke() else NativeFileTransferService.ensureStarted(context)
            synchronized(lock) {
                val existing=records.values.firstOrNull {it.get("planId")?.asString==planId}
                if(existing!=null)existing.deepCopy() else {
                    records[id]=record;cancellations[id]=cancellation;activeCount.incrementAndGet()
                    try {persist()} catch(t:Throwable) {records.remove(id);cancellations.remove(id);activeCount.decrementAndGet();throw t}
                    record.deepCopy()
                }
            }
        }
        if(accepted.get("jobId").asString!=id)return accepted
        executor.submit {
            try {
                synchronized(lock) {record.addProperty("state","running");persist()}
                val result=engine.execute(plan,{fields -> synchronized(lock) {
                    record.add("progress",fields.deepCopy());fields.get("items")?.let {record.add("items",it.deepCopy())}
                    record.addProperty("updatedAt",System.currentTimeMillis());if(System.currentTimeMillis()-lastPersist>500)persist()
                }},{cancellation.get()})
                synchronized(lock) {
                    record.add("result",result);record.add("items",result.getAsJsonArray("items").deepCopy())
                    record.addProperty("state",result.get("state").asString);record.addProperty("cancelSupported",false)
                    record.addProperty("updatedAt",System.currentTimeMillis());record.addProperty("completedAt",System.currentTimeMillis());persist()
                }
            } catch(t:Throwable) {synchronized(lock) {
                record.addProperty("state",if(cancellation.get())"cancelled" else "failed");record.addProperty("cancelSupported",false)
                record.addProperty("error",NativeTransferEngine.safeError(t));record.addProperty("updatedAt",System.currentTimeMillis());persist()
            }} finally {activeCount.decrementAndGet();synchronized(lock) {cancellations.remove(id)}}
        }
        return synchronized(lock) {record.deepCopy()}
    }
    fun jobs(): JsonObject = synchronized(lock) {JsonObject().apply {add("jobs",JsonArray().apply {records.values.toList().asReversed().forEach {add(it.deepCopy())}})}}
    fun job(id: String): JsonObject = synchronized(lock) {records[id]?.deepCopy()?:error("传输任务不存在")}
    fun cancel(id: String): JsonObject = synchronized(lock) {
        val record=records[id]?:error("传输任务不存在")
        if(active(record)) {cancellations[id]?.set(true);record.addProperty("cancelRequested",true);record.addProperty("updatedAt",System.currentTimeMillis());persist()}
        record.deepCopy()
    }
    private fun cloudClient():NativeTransferCloud=cloudOverride?:BaiduNetdiskRuntime.get(context).binaryClient()
    private fun <T> configurationGuard(block:()->T):T=if(cloudOverride!=null)block() else BaiduNetdiskRuntime.get(context).withTransferConfigurationGuard(block)
    internal fun closeForTests() {synchronized(lock) {cancellations.values.forEach {it.set(true)}};executor.shutdownNow()}
    private fun active(record:JsonObject)=record.get("state")?.asString in listOf("queued","running")
    private fun persist() {
        synchronized(lock) {
            while(records.size>100) {val key=records.entries.firstOrNull {!active(it.value)}?.key?:break;records.remove(key)}
            save(jobsFile,JsonArray().apply {records.values.forEach {add(it)}});lastPersist=System.currentTimeMillis()
        }
    }
    private fun save(file:File,contents:JsonElement) {
        val temp=File(file.parentFile,file.name+".pending")
        FileOutputStream(temp).use {out->out.write(contents.toString().toByteArray(Charsets.UTF_8));out.fd.sync()}
        check(temp.renameTo(file)) {"任务记录保存失败"}
    }
    private fun hasGrant(uri:Uri,write:Boolean):Boolean = context.contentResolver.persistedUriPermissions.any {
        it.uri.authority==uri.authority && runCatching {DocumentsContract.getTreeDocumentId(it.uri)==DocumentsContract.getTreeDocumentId(uri)}.getOrDefault(false) &&
            it.isReadPermission && (!write||it.isWritePermission)
    }
    private fun nodeJson(n:TransferNode)=JsonObject().apply {addProperty("uri",n.ref);addProperty("name",n.name);addProperty("isDirectory",n.directory);addProperty("bytes",n.bytes);addProperty("modifiedAt",n.modified)}
    private inner class LocalStore: NativeTransferStore {
        override val scope="local"
        private val resolver get()=context.contentResolver
        private fun file(ref:String):File {
            val uri=Uri.parse(ref);require(uri.scheme=="file"&&uri.authority.isNullOrEmpty()&&uri.query==null&&uri.fragment==null) {"手机本地路径无效"}
            val raw=File(uri.path?:error("手机本地路径无效")).absoluteFile
            require(raw.path==defaultRoot.path||raw.path.startsWith(defaultRoot.path+File.separator)) {"只允许手机资源目录或系统授权的文件夹"}
            var part:File?=raw
            while(part!=null&&part.path.startsWith(defaultRoot.path)) {require(!Files.isSymbolicLink(part.toPath())) {"不支持符号链接"};if(part==defaultRoot)break;part=part.parentFile}
            require(raw.canonicalPath==raw.path) {"手机路径包含跳转"};return raw
        }
        private fun document(ref:String):Uri {
            val uri=Uri.parse(ref);require(uri.scheme=="content"&&uri.query==null&&uri.fragment==null&&DocumentsContract.isTreeUri(uri)) {"只接受系统授权的文件夹 URI"}
            val stored=synchronized(lock) {roots.firstOrNull {saved ->runCatching {
                val root=Uri.parse(saved);root.authority==uri.authority&&DocumentsContract.getTreeDocumentId(root)==DocumentsContract.getTreeDocumentId(uri)
            }.getOrDefault(false)}}?:error("该目录未获得持久授权，请重新选择手机文件夹")
            val tree=Uri.parse(stored);check(hasGrant(tree,false)) {"手机目录授权已失效"}
            val treeId=DocumentsContract.getTreeDocumentId(tree)
            val docId=runCatching {DocumentsContract.getDocumentId(uri)}.getOrDefault(treeId)
            val rootUri=DocumentsContract.buildDocumentUriUsingTree(tree,treeId)
            val canonical=DocumentsContract.buildDocumentUriUsingTree(tree,docId)
            require(docId==treeId||DocumentsContract.isChildDocument(resolver,rootUri,canonical)) {"文件不在已授权目录内"}
            return canonical
        }
        private fun fromFile(file:File):TransferNode? {
            val checked=file(Uri.fromFile(file).toString());if(!checked.exists())return null
            require(checked.isFile||checked.isDirectory) {"不支持特殊文件"}
            val stat=Os.lstat(checked.path);require(OsConstants.S_ISREG(stat.st_mode)||OsConstants.S_ISDIR(stat.st_mode)) {"不支持特殊文件或链接"}
            return TransferNode(Uri.fromFile(checked).toString(),if(checked==defaultRoot)null else Uri.fromFile(checked.parentFile).toString(),
                if(checked==defaultRoot)"手机资源文件" else checked.name,checked.isDirectory,if(checked.isDirectory)0 else stat.st_size,
                checked.lastModified(),"${stat.st_dev}:${stat.st_ino}:${stat.st_size}:${stat.st_mtime}:${stat.st_mtim.tv_nsec}:${stat.st_ctime}:${stat.st_ctim.tv_nsec}:${stat.st_mode}",identity="${stat.st_dev}:${stat.st_ino}")
        }
        private fun fromDocument(uri:Uri,parent:String?=null):TransferNode? {
            val checked=document(uri.toString())
            val queried=try {resolver.query(checked,arrayOf(DocumentsContract.Document.COLUMN_DOCUMENT_ID,DocumentsContract.Document.COLUMN_DISPLAY_NAME,
                DocumentsContract.Document.COLUMN_MIME_TYPE,DocumentsContract.Document.COLUMN_SIZE,DocumentsContract.Document.COLUMN_LAST_MODIFIED),null,null,null)}
                catch(_:java.io.FileNotFoundException) {return null}
            check(queried!=null) {"手机目录状态不可确认，请检查授权或存储设备"}
            queried.use { cursor ->
                if(!cursor.moveToFirst())return null
                val id=cursor.getString(0);val name=cursor.getString(1)?:error("目录名称不可读");NativeTransferEngine.safeName(name)
                val dir=cursor.getString(2)==DocumentsContract.Document.MIME_TYPE_DIR;val size=if(dir)0L else if(cursor.isNull(3))error("文件大小未知，无法校验传输") else cursor.getLong(3)
                val modified=if(cursor.isNull(4))0L else cursor.getLong(4)
                val actual=DocumentsContract.buildDocumentUriUsingTree(checked,id);document(actual.toString())
                return TransferNode(actual.toString(),parent?:knownParent(actual),name,dir,size,modified,"$id:$name:$dir:$size:$modified",identity="$id")
            }
            return null
        }
        private val parentCache get()=parentRefs
        private fun knownParent(uri:Uri):String? {
            if(DocumentsContract.getDocumentId(uri)==DocumentsContract.getTreeDocumentId(uri))return null
            parentCache[uri.toString()]?.let{return it}
            // Standard Android ExternalStorageProvider document IDs preserve folder structure.
            if(uri.authority=="com.android.externalstorage.documents") {
                val id=DocumentsContract.getDocumentId(uri);val root=DocumentsContract.getTreeDocumentId(uri)
                val candidate=id.substringBeforeLast('/',"")
                if(candidate==root||candidate.startsWith(root+"/"))return DocumentsContract.buildDocumentUriUsingTree(uri,candidate).toString()
            }
            return null
        }
        override fun stat(ref:String):TransferNode?=if(Uri.parse(ref).scheme=="file")fromFile(file(ref)) else fromDocument(document(ref))
        override fun children(node:TransferNode):List<TransferNode> {
            require(node.directory) {"请选择文件夹"}
            if(Uri.parse(node.ref).scheme=="file")return file(node.ref).listFiles()?.map {fromFile(it)?:error("目录内容已改变")}?:error("无法读取手机目录")
            val uri=document(node.ref);val rows=mutableListOf<TransferNode>();val childUri=DocumentsContract.buildChildDocumentsUriUsingTree(uri,DocumentsContract.getDocumentId(uri))
            resolver.query(childUri,arrayOf(DocumentsContract.Document.COLUMN_DOCUMENT_ID,DocumentsContract.Document.COLUMN_DISPLAY_NAME,
                DocumentsContract.Document.COLUMN_MIME_TYPE,DocumentsContract.Document.COLUMN_SIZE,DocumentsContract.Document.COLUMN_LAST_MODIFIED),null,null,null)?.use { cursor ->
                while(cursor.moveToNext()) {
                    check(rows.size<100000) {"目录项目过多"};val id=cursor.getString(0)
                    val child=DocumentsContract.buildDocumentUriUsingTree(uri,id);document(child.toString())
                    val name=NativeTransferEngine.safeName(cursor.getString(1)?:error("文件名称不可读"))
                    val dir=cursor.getString(2)==DocumentsContract.Document.MIME_TYPE_DIR
                    val size=if(dir)0L else if(cursor.isNull(3))error("文件大小未知，无法校验传输") else cursor.getLong(3)
                    val modified=if(cursor.isNull(4))0L else cursor.getLong(4)
                    parentCache[child.toString()]=node.ref
                    rows.add(TransferNode(child.toString(),node.ref,name,dir,size,modified,"$id:$name:$dir:$size:$modified",identity=id))
                }
            }?:error("无法读取手机目录")
            return rows
        }
        override fun child(parent:TransferNode,name:String):TransferNode? {
            NativeTransferEngine.safeName(name)
            if(Uri.parse(parent.ref).scheme=="file")return fromFile(File(file(parent.ref),name))
            val uri=document(parent.ref);val query=DocumentsContract.buildChildDocumentsUriUsingTree(uri,DocumentsContract.getDocumentId(uri))
            resolver.query(query,arrayOf(DocumentsContract.Document.COLUMN_DOCUMENT_ID,DocumentsContract.Document.COLUMN_DISPLAY_NAME),null,null,null)?.use {cursor->
                while(cursor.moveToNext())if(cursor.getString(1)==name) {
                    val child=DocumentsContract.buildDocumentUriUsingTree(uri,cursor.getString(0));parentCache[child.toString()]=parent.ref
                    return fromDocument(child,parent.ref)?:error("目标目录内容已改变")
                }
            }?:error("无法检查目标目录")
            return null
        }
        override fun isRoot(node:TransferNode):Boolean = node.ref==Uri.fromFile(defaultRoot).toString() || (Uri.parse(node.ref).scheme=="content" && DocumentsContract.getDocumentId(Uri.parse(node.ref))==DocumentsContract.getTreeDocumentId(Uri.parse(node.ref)))
        override fun read(node:TransferNode,destination:File,progress:(Long)->Unit,cancelled:()->Boolean) {
            val current=stat(node.ref)?:error("手机源文件不存在");check(current.fingerprint==node.fingerprint&&!current.directory) {"手机文件已改变"}
            val input=if(Uri.parse(node.ref).scheme=="file") {
                val fd=Os.open(file(node.ref).path,OsConstants.O_RDONLY or OsConstants.O_NOFOLLOW,0)
                val stream=FileInputStream(fd)
                try {val held=Os.fstat(fd);check("${held.st_dev}:${held.st_ino}"==node.identity&&OsConstants.S_ISREG(held.st_mode)) {"手机文件已被替换"}}
                catch(t:Throwable) {stream.close();throw t}
                stream
            } else resolver.openInputStream(document(node.ref))?:error("无法读取手机文件")
            input.use {src ->FileOutputStream(destination).use {out->copy(src,out,progress,cancelled);out.fd.sync()}}
        }
        private fun writable(ref:String) {if(Uri.parse(ref).scheme=="content")check(hasGrant(document(ref),true)) {"目录没有写入权限"}}
        override fun createDirectory(parent:TransferNode,name:String):TransferNode {
            NativeTransferEngine.safeName(name);writable(parent.ref);check(child(parent,name)==null) {"目标已存在同名项目"}
            return if(Uri.parse(parent.ref).scheme=="file") {
                val target=File(file(parent.ref),name);check(target.mkdir()) {"手机目录创建失败"};fromFile(target)?:error("手机目录创建失败")
            } else {
                val created=DocumentsContract.createDocument(resolver,document(parent.ref),DocumentsContract.Document.MIME_TYPE_DIR,name)?:error("手机目录创建失败")
                val result=fromDocument(created,parent.ref)?:error("无法确认手机目录")
                check(result.name==name&&result.directory) {"手机目录名称不符，未覆盖已有文件"};result
            }
        }
        override fun write(parent:TransferNode,name:String,source:File,progress:(Long)->Unit,cancelled:()->Boolean):TransferNode {
            NativeTransferEngine.safeName(name);writable(parent.ref);check(child(parent,name)==null) {"目标已存在同名文件"}
            if(Uri.parse(parent.ref).scheme=="file") {
                val target=File(file(parent.ref),name);val fd=Os.open(target.path,OsConstants.O_WRONLY or OsConstants.O_CREAT or OsConstants.O_EXCL or OsConstants.O_NOFOLLOW,384)
                try {source.inputStream().use {input->FileOutputStream(fd).use {out->copy(input,out,progress,cancelled);out.fd.sync()}}}
                catch(t:Throwable) {runCatching {target.delete()};throw t}
                return fromFile(target)?:error("手机文件写入后不可见")
            }
            val created=DocumentsContract.createDocument(resolver,document(parent.ref),"application/octet-stream",name)?:error("无法创建手机文件")
            val node=fromDocument(created,parent.ref)?:error("无法确认手机文件")
            check(node.name==name&&!node.directory) {"目标名称冲突，已保留源文件"}
            try {source.inputStream().use {input->resolver.openOutputStream(document(created.toString()),"wt")?.use {out->copy(input,out,progress,cancelled)}?:error("手机文件不可写")}}
            catch(t:Throwable) {runCatching {DocumentsContract.deleteDocument(resolver,created)};throw t}
            return fromDocument(created,parent.ref)?:error("手机文件写入后不可见")
        }
        override fun remove(node:TransferNode):Boolean {
            require(!isRoot(node)) {"不能删除授权目录"};writable(node.ref)
            val now=stat(node.ref)?:return false
            check(now.fingerprint==node.fingerprint) {"手机文件已改变，已保留源"}
            if(now.directory)check(children(now).isEmpty()) {"目录非空，已保留源"}
            val done=if(Uri.parse(node.ref).scheme=="file")file(node.ref).delete() else DocumentsContract.deleteDocument(resolver,document(node.ref))
            return done&&stat(node.ref)==null
        }
    }
    private class CloudStore(val client:NativeTransferCloud):NativeTransferStore {
        override val scope="netdisk"
        private fun normalize(meta:JsonObject):TransferNode {
            val path=meta.get("path")?.asString?:error("网盘文件路径缺失")
            val name=meta.get("name")?.asString?:path.substringAfterLast('/')
            val dir=meta.get("isDirectory")?.asBoolean?:false
            val bytes=meta.get("bytes")?.asLong?:0L;val modified=meta.get("modifiedAt")?.asLong?:0L
            val finger=listOf(path,meta.get("fsId")?.asString,name,dir,bytes,modified,meta.get("md5")?.asString).joinToString(":")
            return TransferNode(path,if(path==client.rootPath())null else path.substringBeforeLast('/'),name,dir,bytes,modified,finger,meta.deepCopy(),identity=meta.get("fsId")?.asString?:error("网盘文件 ID 缺失"))
        }
        override fun stat(ref:String)=client.stat(ref)?.let(::normalize)
        override fun children(node:TransferNode)=client.list(node.ref).map(::normalize)
        override fun child(parent:TransferNode,name:String):TransferNode? {NativeTransferEngine.safeName(name);return client.stat(parent.ref.trimEnd('/')+"/"+name)?.let(::normalize)}
        override fun isRoot(node:TransferNode)=node.ref==client.rootPath()
        override fun read(node:TransferNode,destination:File,progress:(Long)->Unit,cancelled:()->Boolean) {client.download(node.extra,destination,progress,cancelled)}
        override fun createDirectory(parent:TransferNode,name:String)=normalize(client.mkdir(parent.ref.trimEnd('/')+"/"+NativeTransferEngine.safeName(name)))
        override fun write(parent:TransferNode,name:String,source:File,progress:(Long)->Unit,cancelled:()->Boolean)=normalize(client.upload(source,parent.ref.trimEnd('/')+"/"+NativeTransferEngine.safeName(name),progress,cancelled))
        override fun remove(node:TransferNode):Boolean {val response=client.delete(node.extra);return response.get("deleted")?.asBoolean==true&&response.get("verified")?.asBoolean==true}
    }
    private fun copy(input:java.io.InputStream,out:java.io.OutputStream,progress:(Long)->Unit,cancelled:()->Boolean) {
        val buffer=ByteArray(256*1024);var done=0L
        while(true) {if(cancelled())throw CancellationException("传输已取消");val n=input.read(buffer);if(n<0)break;out.write(buffer,0,n);done+=n;progress(done)}
        out.flush()
    }
    private fun only(args:JsonObject,keys:Set<String>) {require(args.keySet().all {it in keys}) {"含不支持的参数"}}
    private fun text(args:JsonObject,key:String,fallback:String?=null):String {
        val value=args.get(key)?:return fallback?:error("缺少参数：$key")
        require(value.isJsonPrimitive&&value.asJsonPrimitive.isString) {"$key 必须是文本"}
        return value.asString.also {require(it.isNotBlank()&&it.length<=8192&&it.none {c->c.code<32||c.code==127}) {"$key 无效"}}
    }
    private fun integer(args:JsonObject,key:String,fallback:Int,min:Int,max:Int):Int {if(!args.has(key))return fallback
        return runCatching {args.get(key).asBigDecimal.intValueExact()}.getOrElse {error("$key 必须为整数")}.also {require(it in min..max) {"$key 超出范围"}}}
}
