package cn.ololee.devhelper.netdisk

import com.google.gson.JsonArray
import com.google.gson.JsonObject
import java.io.File
import java.security.MessageDigest
import java.util.UUID
import java.util.concurrent.CancellationException

/** No Android or network dependencies: every source and destination is an explicitly scoped store. */
data class TransferNode(val ref: String, val parent: String?, val name: String, val directory: Boolean,
                        val bytes: Long, val modified: Long, val fingerprint: String, val extra: JsonObject = JsonObject(), val identity: String = ref)
interface NativeTransferStore {
    val scope: String
    fun stat(ref: String): TransferNode?
    fun children(node: TransferNode): List<TransferNode>
    fun child(parent: TransferNode, name: String): TransferNode?
    fun isRoot(node: TransferNode): Boolean
    fun read(node: TransferNode, destination: File, progress: (Long) -> Unit, cancelled: () -> Boolean)
    fun createDirectory(parent: TransferNode, name: String): TransferNode
    fun write(parent: TransferNode, name: String, source: File, progress: (Long) -> Unit, cancelled: () -> Boolean): TransferNode
    fun remove(node: TransferNode): Boolean
}

class NativeTransferEngine(private val staging: File) {
    data class Entry(val node: TransferNode, val relative: List<String>)
    data class Source(val node: TransferNode, val tree: List<Entry>, val companion: Boolean = false, val companionOf: String? = null) {
        val childrenByParent=tree.groupBy {it.node.parent}.mapValues {entry->entry.value.map {it.node.ref}.toSet()}
    }
    data class Plan(val id: String, val digest: String, val operation: String, val source: NativeTransferStore,
                    val target: NativeTransferStore, val destination: TransferNode, val sources: List<Source>,
                    val expires: Long, val account: String) {
        val fileCount: Int = sources.sumOf { it.tree.count { e -> !e.node.directory } }
        val bytes: Long = sources.sumOf { it.tree.sumOf { e -> if (e.node.directory) 0L else e.node.bytes } }
        fun publicJson(): JsonObject = JsonObject().apply {
            addProperty("planId",id); addProperty("planDigest",digest); addProperty("scope","transfer")
            addProperty("sourceScope",source.scope); addProperty("destinationScope",target.scope); addProperty("operation",operation)
            addProperty("expiresAt",expires); addProperty("fileCount",fileCount); addProperty("totalBytes",bytes * 4)
            addProperty("bytes",bytes); addProperty("encoding","binary"); addProperty("conflictPolicy","fail")
            addProperty("destinationName",destination.name); addProperty("destinationRef",destination.ref)
            add("items",JsonArray().apply { sources.forEach { s -> add(JsonObject().apply {
                addProperty("source",s.node.ref);addProperty("name",s.node.name);addProperty("isDirectory",s.node.directory)
                addProperty("destination",destination.ref);addProperty("automaticCompanion",s.companion)
                s.companionOf?.let {addProperty("companionOf",it)}
                addProperty("fileCount",s.tree.count { !it.node.directory });addProperty("bytes",s.tree.sumOf { if(it.node.directory)0L else it.node.bytes })
            }) } })
        }
    }
    fun plan(source: NativeTransferStore, target: NativeTransferStore, operation: String,
             refs: List<String>, destinationRef: String, account: String): Plan {
        require(source.scope in setOf("local","netdisk") && target.scope in setOf("local","netdisk") && source.scope != target.scope) { "请选择本地和百度网盘之间的目标" }
        require(operation in setOf("copy","move")) { "只支持复制或剪切" }
        require(refs.isNotEmpty() && refs.size <= 100 && refs.toSet().size == refs.size) { "每批选择 1 至 100 个不重复项目" }
        val destination = target.stat(destinationRef) ?: error("目标文件夹不存在")
        require(destination.directory) { "目标必须是文件夹" }
        val originals = refs.map { source.stat(it) ?: error("源文件不存在") }
        require(originals.none { source.isRoot(it) }) { "不能复制或移动授权目录本身，请选择其中的文件或文件夹" }
        val expanded = originals.toMutableList()
        val companions = mutableMapOf<String,String>()
        originals.filter { !it.name.endsWith(".meta") }.forEach { node ->
            node.parent?.let { p -> source.stat(p)?.let { source.child(it,node.name+".meta") } }?.let { meta ->
                if(!meta.directory && expanded.none { it.ref==meta.ref }) { expanded.add(meta); companions[meta.ref]=node.ref }
            }
        }
        require(expanded.size <= 200) { "项目及关联 .meta 数量过多" }
        val names = mutableSetOf<String>()
        val seen = mutableSetOf<String>()
        val sources = expanded.map { node ->
            safeName(node.name)
            require(names.add(node.name)) { "所选项目中有同名项目，请分批传输" }
            check(target.child(destination,node.name)==null) { "目标已存在同名项目：${node.name}" }
            val tree = ArrayList<Entry>()
            fun visit(n: TransferNode, rel: List<String>) {
                require(rel.size<=128 && seen.size<100000 && seen.add(n.ref)) { "目录过深、文件过多，或源项目相互包含" }
                safeName(n.name);require(n.bytes>=0) {"文件大小无效"};tree.add(Entry(n,rel))
                if(n.directory) {
                    val children=source.children(n)
                    require(children.map { it.name }.distinct().size==children.size) { "源目录含重复名称" }
                    children.forEach { visit(it,rel+it.name) }
                }
            }
            visit(node,emptyList());Source(node,tree,node.ref in companions,companions[node.ref])
        }
        val bytes=sources.sumOf { it.tree.sumOf { e -> if(e.node.directory)0L else e.node.bytes } }
        require(bytes in 0..Long.MAX_VALUE/4) { "传输大小超出范围" }
        return Plan(UUID.randomUUID().toString(),UUID.randomUUID().toString()+UUID.randomUUID().toString(),operation,
            source,target,destination,sources,System.currentTimeMillis()+20*60*1000,account)
    }
    fun execute(plan: Plan, progress: (JsonObject)->Unit, cancelled: ()->Boolean): JsonObject {
        require(System.currentTimeMillis()<=plan.expires) { "操作计划已过期，请重新粘贴" }
        staging.mkdirs();check(staging.isDirectory) {"无法创建传输暂存目录"}
        val folder=File(staging,UUID.randomUUID().toString()).apply { check(mkdir()) {"无法创建传输暂存目录"} }
        val items=plan.sources.map { s -> JsonObject().apply {
            addProperty("source",s.node.ref);addProperty(if(plan.source.scope=="local")"sourceUri" else "sourcePath",s.node.ref);addProperty("name",s.node.name);addProperty("state","queued")
            addProperty("transferState","queued");addProperty("sourcePreserved",true);addProperty("sourceCleanupState","not-started");addProperty("isDirectory",s.node.directory)
            addProperty("destinationCommitted",false);add("files",JsonArray())
            addProperty("automaticCompanion",s.companion);s.companionOf?.let {addProperty("companionOf",it)}
        } }.toMutableList()
        var doneBytes=0L;var completed=0;var verified=0;var phase="preparing";var current="";var cleanupStarted=false
        val targetTrees=plan.sources.map { mutableListOf<Entry>() }
        val sourceHashes=mutableMapOf<String,String>()
        val targetHashes=mutableMapOf<String,String>()
        fun emit(extra: Long=0) { progress(JsonObject().apply {
            addProperty("phase",phase);addProperty("bytesTransferred",(doneBytes+extra).coerceIn(0,plan.bytes*4))
            addProperty("totalBytes",plan.bytes*4);addProperty("fileCount",plan.fileCount)
            addProperty("completedFiles",completed);addProperty("verifiedFiles",verified);addProperty("currentFile",current)
            add("items",JsonArray().apply {items.forEach {item -> add(JsonObject().apply {
                item.entrySet().filter {it.key!="files"}.forEach {entry ->add(entry.key,entry.value.deepCopy())}
            })}})
        }) }
        fun checkCancel() { if(cancelled())throw CancellationException("传输已取消") }
        fun same(store: NativeTransferStore, node: TransferNode) {
            val now=store.stat(node.ref) ?: error("文件已被移动或删除：${node.name}")
            check(now.fingerprint==node.fingerprint&&now.identity==node.identity) { "文件已改变，请重新选择：${node.name}" }
        }
        fun unchanged(source: Source) {
            source.tree.forEach { same(plan.source,it.node) }
            source.tree.filter { it.node.directory }.forEach { entry ->
                val expected=source.childrenByParent[entry.node.ref]?:emptySet()
                check(plan.source.children(entry.node).map { it.ref }.toSet()==expected) { "源目录内容已改变：${entry.node.name}" }
            }
        }
        fun verifyTargets(index:Int, countBytes:Boolean) {
            val item=items[index];val tree=targetTrees[index];item.addProperty("targetVerified",false)
            val childrenByParent=tree.groupBy {it.node.parent}.mapValues {entry->entry.value.map {it.node.ref}.toSet()}
            tree.forEach { entry ->
                checkCancel();val node=entry.node;current=node.name
                val now=plan.target.stat(node.ref)?:error("目标文件已被删除；已保留源")
                check(now.identity==node.identity) {"目标文件已被替换；已保留源"}
                if(node.directory) {
                    require(now.directory) {"目标目录已改变；已保留源"}
                    val expected=childrenByParent[node.ref]?:emptySet()
                    check(plan.target.children(now).map {it.ref}.toSet()==expected) {"目标目录内容已改变；已保留源"}
                } else {
                    check(now.fingerprint==node.fingerprint) {"目标文件已改变；已保留源"}
                    val expectedHash=targetHashes[node.ref]?:error("目标校验记录不存在")
                    val verify=File(folder,UUID.randomUUID().toString()+".batch-verify")
                    try {
                        plan.target.read(now,verify,{n->if(countBytes)emit(n.coerceIn(0,node.bytes)) else emit()},cancelled)
                        checkCancel();same(plan.target,now)
                        check(verify.length()==node.bytes&&sha256(verify,cancelled)==expectedHash) {"目标 SHA-256 再校验失败；已保留源"}
                        if(countBytes)doneBytes+=node.bytes
                    } finally {verify.delete()}
                }
                emit()
            }
            item.addProperty("targetVerified",true)
        }
        fun preserved(source:Source):Boolean? {
            // A missing delete receipt is not evidence that deletion failed. Never retry deletion blindly.
            if(cancelled())return null
            return try {
                for(entry in source.tree) {
                    val node=plan.source.stat(entry.node.ref)?:return false
                    if(node.identity!=entry.node.identity)return null
                    if(!node.directory) {
                        if(node.fingerprint!=entry.node.fingerprint)return null
                        val expected=sourceHashes[entry.node.ref]?:return null
                        val probe=File(folder,UUID.randomUUID().toString()+".source-probe")
                        try {
                            plan.source.read(node,probe,{},cancelled)
                            if(probe.length()!=node.bytes||sha256(probe,cancelled)!=expected)return null
                        } finally {probe.delete()}
                    }
                }
                true
            } catch(_:Throwable) {null}
        }
        val results=JsonObject().apply {
            addProperty("scope","transfer");addProperty("sourceScope",plan.source.scope);addProperty("destinationScope",plan.target.scope)
            addProperty("operation",plan.operation);addProperty("encoding","binary");addProperty("moveCommitted",false)
            addProperty("sourceCleanupState","not-started");addProperty("sourcePreserved",true)
        }
        try {
            emit();checkCancel()
            val liveDestination=plan.target.stat(plan.destination.ref)?:error("目标目录不存在")
            check(liveDestination.directory&&liveDestination.identity==plan.destination.identity) {"目标目录已被替换，请重新选择"}
            plan.sources.forEach { s -> unchanged(s);check(plan.target.child(plan.destination,s.node.name)==null) {"目标已存在同名项目：${s.node.name}"} }
            plan.sources.forEachIndexed { index,s ->
                checkCancel();val item=items[index];item.addProperty("state","running");item.addProperty("transferState","running")
                val dirs=mutableMapOf<List<String>,TransferNode>();dirs[emptyList()]=plan.destination
                for(entry in s.tree) {
                    checkCancel();current=(listOf(s.node.name)+entry.relative).joinToString("/")
                    val parent=if(entry.relative.isEmpty())plan.destination else dirs[entry.relative.dropLast(1)]?:error("传输目录结构无效")
                    val name=if(entry.relative.isEmpty())s.node.name else entry.relative.last()
                    check(plan.target.child(parent,name)==null) { "目标已存在同名项目：$name" }
                    if(entry.node.directory) {
                        phase="creating-directories";emit();val created=plan.target.createDirectory(parent,name)
                        require(created.directory && created.name==name) {"目标目录创建结果不符"}
                        targetTrees[index].add(Entry(created,entry.relative))
                        dirs[entry.relative]=created
                        if(entry.relative.isEmpty()) {item.addProperty("destination",created.ref);item.addProperty("destinationCommitted",true)}
                    } else {
                        val input=File(folder,UUID.randomUUID().toString()+".source")
                        val output=File(folder,UUID.randomUUID().toString()+".verify")
                        try {
                            phase="reading-source";emit();same(plan.source,entry.node)
                            plan.source.read(entry.node,input,{n->emit(n.coerceIn(0,entry.node.bytes))},cancelled)
                            checkCancel();check(input.isFile&&input.length()==entry.node.bytes) {"源文件长度校验失败"}
                            val hash=sha256(input,cancelled);sourceHashes[entry.node.ref]=hash;same(plan.source,entry.node);doneBytes+=entry.node.bytes
                            phase="transferring";emit()
                            val target=plan.target.write(parent,name,input,{n->emit(n.coerceIn(0,entry.node.bytes))},cancelled)
                            require(!target.directory&&target.name==name&&target.bytes==entry.node.bytes) {"目标文件大小或名称不符"}
                            targetTrees[index].add(Entry(target,entry.relative));targetHashes[target.ref]=hash
                            doneBytes+=entry.node.bytes
                            item.getAsJsonArray("files").add(JsonObject().apply {
                                addProperty("source",entry.node.ref);addProperty("destination",target.ref);addProperty("name",name)
                                addProperty("bytes",target.bytes);addProperty("sha256",hash);addProperty("verified",false)
                                target.extra.get("fsId")?.let { add("fsId",it.deepCopy()) }
                            })
                            if(entry.relative.isEmpty()) {item.addProperty("destination",target.ref);item.addProperty("destinationCommitted",true)}
                            phase="verifying";emit();plan.target.read(target,output,{n->emit(n.coerceIn(0,entry.node.bytes))},cancelled)
                            checkCancel();same(plan.target,target)
                            check(output.length()==entry.node.bytes && sha256(output,cancelled)==hash) { "目标 SHA-256 校验失败，已保留源文件" }
                            doneBytes+=entry.node.bytes;verified++;completed++
                            item.getAsJsonArray("files").last().asJsonObject.addProperty("verified",true)
                        } finally {input.delete();output.delete()}
                    }
                    emit()
                }
                item.addProperty("state","completed");item.addProperty("transferState","completed");item.addProperty("targetVerified",true)
                item.addProperty("destinationCommitted",true);emit()
            }
            checkCancel()
            phase="verifying-batch";emit();plan.sources.indices.forEach {verifyTargets(it,true)}
            // Verify the entire source batch before the first destructive action.
            plan.sources.forEach { unchanged(it) };checkCancel()
            if(plan.operation=="move") {
                phase="cleaning-sources";cleanupStarted=true;emit();results.addProperty("sourceCleanupState","running")
                plan.sources.forEachIndexed { index,s ->
                    checkCancel();current=s.node.name;val item=items[index];var removed=0
                    try {
                        phase="verifying-before-cleanup";emit();verifyTargets(index,false)
                        phase="cleaning-sources";emit();unchanged(s)
                        // Files first, then now-empty directories. Check each live fingerprint immediately before deletion.
                        val ordered=s.tree.filter { !it.node.directory }+s.tree.filter { it.node.directory }.sortedByDescending { it.relative.size }
                        ordered.forEach { entry ->
                            checkCancel()
                            if(!entry.node.directory)same(plan.source,entry.node)
                            else check(plan.source.children(entry.node).isEmpty()) { "源目录新增内容，已保留" }
                            val deleting=if(entry.node.directory)plan.source.stat(entry.node.ref)?:error("源目录已改变") else entry.node
                            check(deleting.identity==entry.node.identity) {"源目录已被替换，已停止清理"}
                            check(plan.source.remove(deleting)) { "源文件处理未确认完成：${entry.node.name}" }
                            removed++
                        }
                        item.addProperty("sourcePreserved",false);item.addProperty("sourceCleanupState","completed")
                    } catch(t:Throwable) {
                        item.addProperty("state","completed-with-errors")
                        val retained=if(removed>0)false else preserved(s)
                        if(retained==null)item.add("sourcePreserved",com.google.gson.JsonNull.INSTANCE) else item.addProperty("sourcePreserved",retained)
                        item.addProperty("sourceCleanupState",if(retained==null)"unknown" else if(retained)"failed" else "partial")
                        item.addProperty("error",safeError(t))
                        if(t is CancellationException)throw t
                    }
                    emit()
                }
                val all=items.all {it.get("sourceCleanupState").asString=="completed"}
                results.addProperty("moveCommitted",all);results.addProperty("sourceCleanupState",if(all)"completed" else "partial")
                summarizePreserved(results,items)
                results.addProperty("state",if(all)"completed" else "completed-with-errors")
            } else {results.addProperty("state","completed");results.addProperty("sourceCleanupState","not-required")}
            phase=results.get("state").asString;emit()
        } catch(t:Throwable) {
            val cancelledState=t is CancellationException || cancelled()
            results.addProperty("state",if(cancelledState)"cancelled" else "failed");results.addProperty("error",safeError(t))
            results.addProperty("sourceCleanupState",if(cleanupStarted)"partial" else "not-started")
            summarizePreserved(results,items)
            items.filter { it.get("transferState").asString!="completed" }.forEach {it.addProperty("state",if(cancelledState)"cancelled" else "failed")}
            phase=if(cancelledState)"cancelled" else "failed";emit()
        } finally {folder.deleteRecursively()}
        results.add("items",JsonArray().apply {items.forEach {add(it.deepCopy())}})
        return results
    }
    private fun summarizePreserved(result:JsonObject,items:List<JsonObject>) {
        val values=items.map {it.get("sourcePreserved")}
        if(values.any {it!=null&&!it.isJsonNull&&!it.asBoolean})result.addProperty("sourcePreserved",false)
        else if(values.any {it==null||it.isJsonNull})result.add("sourcePreserved",com.google.gson.JsonNull.INSTANCE)
        else result.addProperty("sourcePreserved",true)
    }
    companion object {
        fun safeName(name: String): String {require(name.isNotBlank()&&name !in listOf(".","..")&&name.length<=255&&name.none {it=='/'||it=='\\'||it.code<32||it.code==127}) {"文件名无效"};return name}
        fun sha256(file: File,cancelled: ()->Boolean={false}): String {
            val digest=MessageDigest.getInstance("SHA-256");file.inputStream().use { input -> val buffer=ByteArray(256*1024)
                while(true) {if(cancelled())throw CancellationException("传输已取消");val n=input.read(buffer);if(n<0)break;digest.update(buffer,0,n)} }
            val hex="0123456789abcdef";val bytes=digest.digest();val chars=CharArray(bytes.size*2)
            bytes.forEachIndexed {index,value->val n=value.toInt()and 255;chars[index*2]=hex[n ushr 4];chars[index*2+1]=hex[n and 15]}
            return String(chars)
        }
        fun safeError(t:Throwable):String = when(t) {
            is CancellationException -> "传输已取消；请查看逐项源文件处理状态"
            is IllegalArgumentException,is IllegalStateException -> (t.message?:"传输检查未通过").replace(Regex("https?://\\S+"),"[远程地址]").take(400)
            else -> "文件或网络操作失败；请检查逐项结果后重试"
        }
    }
}
