package cn.ololee.devhelper.netdisk

import com.google.gson.JsonArray
import com.google.gson.JsonNull
import com.google.gson.JsonObject
import java.util.UUID
import java.util.concurrent.CancellationException

/** Bounded, explicit same-storage mutations. Plans are inventory snapshots, never executable scripts. */
class NativeFileOperationEngine {
    data class Source(val node:TransferNode,val parent:TransferNode,val tree:List<NativeTransferEngine.Entry>,val targetName:String?,val companionOf:String?=null,val automatic:Boolean=false) {
        val children:Map<String?,Set<String>> =tree.groupBy {it.node.parent}.mapValues {it.value.map {e->e.node.ref}.toSet()}
        val childNames:Map<String?,Set<String>> =tree.groupBy {it.node.parent}.mapValues {it.value.map {e->e.node.name}.toSet()}
    }
    data class Plan(val id:String,val digest:String,val operation:String,val store:NativeTransferStore,val sources:List<Source>,val expires:Long,val account:String?) {
        val files=sources.sumOf {it.tree.count {e->!e.node.directory}}
        val directories=sources.sumOf {it.tree.count {e->e.node.directory}}
        val bytes=sources.sumOf {it.tree.sumOf {e->if(e.node.directory)0L else e.node.bytes}}
        fun json()=JsonObject().apply {
            addProperty("planId",id);addProperty("planDigest",digest);addProperty("scope",store.scope);addProperty("sourceScope",store.scope);addProperty("destinationScope",store.scope)
            addProperty("operation",operation);addProperty("expiresAt",expires);addProperty("fileCount",files);addProperty("directoryCount",directories);addProperty("bytes",bytes);addProperty("totalBytes",bytes)
            addProperty("overwrite",false);addProperty("conflictPolicy","fail");addProperty("encoding","binary")
            if(operation=="trash")addProperty("warning",if(store.scope=="local")"将永久删除计划中的手机文件和文件夹，无法通过本应用恢复；自动关联的 .meta 也会删除。" else "将删除计划中的网盘文件和文件夹；回收与保留期限遵守百度网盘规则，本应用不清空回收站。")
            add("items",JsonArray().apply {sources.forEach {add(item(it,store.scope))}})
        }
    }
    fun plan(store:NativeTransferStore,operation:String,refs:List<String>,template:String?=null,account:String?=null):Plan {
        require(operation in setOf("rename","trash")) {"不支持的文件操作"}
        require(refs.size in 1..100&&refs.distinct().size==refs.size) {"每批选择 1 至 100 个不重复项目"}
        require((operation=="rename")== (template!=null)) {"重命名需要名称，删除不接受名称"}
        val originals=refs.map {store.stat(it)?:error("源文件不存在，请刷新")}
        require(originals.none {store.isRoot(it)}) {"不能操作授权根目录本身"}
        val explicitCompanions=originals.mapNotNull {meta->if(meta.directory)null else originals.firstOrNull {!it.name.endsWith(".meta")&&it.parent==meta.parent&&it.name+".meta"==meta.name}?.let {meta.ref to it.ref}}.toMap()
        val primary=originals.filter {it.ref !in explicitCompanions};val expanded=mutableListOf<Triple<TransferNode,String?,Boolean>>()
        val targetNames=mutableMapOf<String,String>()
        primary.forEachIndexed {index,node->if(operation=="rename")targetNames[node.ref]=name(template!!,node.name,node.directory,index+1,primary.size)}
        primary.forEach {node ->
            expanded.add(Triple(node,null,false))
            if(!node.name.endsWith(".meta")) {
                val parent=store.stat(node.parent?:error("无法确认源文件父目录，请从目录列表选择"))?:error("源目录不存在")
                store.child(parent,node.name+".meta")?.takeIf {!it.directory}?.let {meta ->expanded.add(Triple(meta,node.ref,meta.ref !in refs))}
            }
        }
        require(expanded.size<=200) {"所选项目及 .meta 过多"}
        check(originals.all {selected->expanded.any {it.first.ref==selected.ref}}) {"无法完整处理显式选择，请重新选择"}
        val seen=mutableSetOf<String>();val destinations=mutableSetOf<Pair<String,String>>()
        val sources=expanded.map { (node,base,automatic)->
            val parent=store.stat(node.parent?:error("无法确认源文件父目录，请从目录列表选择"))?:error("源目录不存在")
            require(parent.directory&&!store.isRoot(node)) {"不能操作授权根目录"}
            val target=if(operation=="rename")if(base!=null)NativeTransferEngine.safeName(targetNames[base]+".meta") else targetNames[node.ref]!! else null
            if(target!=null) {
                require(destinations.add(parent.ref to target)) {"批量重命名产生同名冲突"}
                val occupied=store.child(parent,target)
                check(occupied==null||occupied.ref==node.ref) {"目标已存在同名项目：$target"}
            }
            val tree=mutableListOf<NativeTransferEngine.Entry>()
            fun visit(n:TransferNode,rel:List<String>) {
                require(rel.size<=128&&seen.size<100000&&seen.add(n.ref)) {"源项目重叠、目录过深或文件过多"}
                NativeTransferEngine.safeName(n.name);require(n.bytes>=0) {"文件大小无效"}
                tree.add(NativeTransferEngine.Entry(n,rel))
                if(n.directory) {val children=store.children(n);require(children.map {it.name}.distinct().size==children.size) {"目录存在重复名称"};children.forEach {visit(it,rel+it.name)}}
            }
            visit(node,emptyList());Source(node,parent,tree,target,base,automatic)
        }
        return Plan(UUID.randomUUID().toString(),UUID.randomUUID().toString()+UUID.randomUUID().toString(),operation,store,sources,System.currentTimeMillis()+20*60*1000,account)
    }
    fun execute(plan:Plan,progress:(JsonObject)->Unit,cancelled:()->Boolean):JsonObject {
        require(System.currentTimeMillis()<=plan.expires) {"操作预览已过期，请重新选择"}
        val rows=plan.sources.map {item(it,plan.store.scope).apply {
            addProperty("state","queued");addProperty("sourcePreserved",true);addProperty("deleted",false);addProperty("renamed",false)
        }}
        var completedFiles=0;var completedItems=0;var bytes=0L;var phase="preparing";var current=""
        fun emit(extraBytes:Long=0)=progress(JsonObject().apply {
            addProperty("phase",phase);addProperty("currentFile",current);addProperty("bytesTransferred",(bytes+extraBytes).coerceIn(0,plan.bytes));addProperty("totalBytes",plan.bytes)
            addProperty("fileCount",plan.files);addProperty("directoryCount",plan.directories);addProperty("completedFiles",completedFiles);addProperty("completedItems",completedItems)
            add("items",JsonArray().apply {rows.forEach {add(it.deepCopy())}})
        })
        fun stop() {if(cancelled())throw CancellationException("操作已取消")}
        fun fresh(n:TransferNode) {val now=plan.store.stat(n.ref)?:error("源项目已不存在")
            check(now.identity==n.identity&&now.fingerprint==n.fingerprint) {"源项目已改变，请重新预览"}}
        fun unchanged(s:Source) {
            s.tree.forEach {fresh(it.node)}
            s.tree.filter {it.node.directory}.forEach {e->check(plan.store.children(e.node).map {it.ref}.toSet()==(s.children[e.node.ref]?:emptySet<String>())) {"目录内容已改变，请重新预览"}}
            val parent=plan.store.stat(s.parent.ref)?:error("源父目录不存在")
            check(parent.identity==s.parent.identity&&parent.directory) {"源父目录已被替换"}
        }
        fun targetTree(s:Source,root:TransferNode,causal:Boolean,copyReceipt:JsonObject?=null):Boolean {
            return try {
                val copyPins=copyReceipt?.getAsJsonArray("verifiedTargets")?.associate {val pin=it.asJsonObject;pin.get("ref").asString to pin}
                if(copyReceipt!=null&&(copyReceipt.get("moveCommitted")?.asBoolean!=true||copyReceipt.get("state")?.asString!="completed"||copyPins?.size!=s.tree.size))return false
                for(e in s.tree) {
                    var n=root
                    for(part in e.relative)n=plan.store.child(n,part)?:return false
                    val old=e.node
                    if(n.directory!=old.directory||n.bytes!=old.bytes||n.name!=(if(e.relative.isEmpty())s.targetName else old.name))return false
                    val sameIdentity=n.identity==old.identity
                    if(!sameIdentity&&!causal)return false
                    if(copyPins!=null) {
                        val pin=copyPins[n.ref]?:return false
                        if(pin.get("identity").asString!=n.identity||pin.get("fingerprint").asString!=n.fingerprint||pin.get("isDirectory").asBoolean!=n.directory)return false
                    }
                    if(old.extra.has("md5")&&n.extra.has("md5")&&old.extra.get("md5")!=n.extra.get("md5"))return false
                    if(old.directory) {
                        val expected=s.childNames[old.ref]?:emptySet<String>()
                        if(plan.store.children(n).map {it.name}.toSet()!=expected)return false
                    }
                }
                true
            }catch(_:Throwable){false}
        }
        fun preserved(s:Source):Boolean?=try {
            var value:Boolean?=true
            for(e in s.tree) {
                val n=plan.store.stat(e.node.ref)
                if(n==null) {value=false;break}
                if(n.identity!=e.node.identity||n.fingerprint!=e.node.fingerprint)value=null
            }
            value
        } catch(_:Throwable) {null}
        fun setPreserved(row:JsonObject,value:Boolean?) {if(value==null)row.add("sourcePreserved",JsonNull.INSTANCE) else row.addProperty("sourcePreserved",value)}
        fun probeRename(s:Source,row:JsonObject,returned:TransferNode?=null) {
            val old=runCatching {plan.store.stat(s.node.ref)}
            val target=runCatching {plan.store.child(s.parent,s.targetName!!)}
            val n=target.getOrNull();val sameName=s.targetName==s.node.name
            val copyReceipt=returned?.extra?.getAsJsonObject("_renameCopyResult")
            copyReceipt?.let {row.add("compatibilityTransfer",it.deepCopy())}
            val causal=returned?.extra?.get("_renameVerified")?.asBoolean==true&&returned.extra.get("_renameOriginalIdentity")?.asString==s.node.identity&&returned.ref==n?.ref
            val stableUriRenamed=causal&&returned?.ref==s.node.ref&&old.getOrNull()?.name==s.targetName&&old.getOrNull()?.identity==n?.identity
            if(old.isSuccess&&target.isSuccess&&n!=null&&(sameName||old.getOrNull()==null||stableUriRenamed)&&targetTree(s,n,causal,copyReceipt)) {
                row.addProperty("state","completed");row.addProperty("renamed",!sameName);row.addProperty("unchanged",sameName)
                row.addProperty("sourcePreserved",sameName);row.addProperty("destination",n.ref);row.addProperty("targetVerified",true)
            } else if(old.isSuccess&&old.getOrNull()?.fingerprint==s.node.fingerprint&&old.getOrNull()?.identity==s.node.identity&&target.isSuccess&&n==null) {
                row.addProperty("state","failed");row.addProperty("renamed",false);row.addProperty("sourcePreserved",true)
            } else {
                row.addProperty("state","unknown");row.add("renamed",JsonNull.INSTANCE);row.add("sourcePreserved",JsonNull.INSTANCE)
            }
        }
        var cancelledResult=false
        try {
            emit();stop();plan.sources.forEach {unchanged(it)}
            plan.sources.forEachIndexed {index,s ->
                stop();val row=rows[index];current=s.node.name
                if(s.companionOf!=null) {
                    val baseIndex=plan.sources.indexOfFirst {it.node.ref==s.companionOf}
                    if(baseIndex<0||rows[baseIndex].get("state").asString!="completed") {
                        row.addProperty("state","skipped");row.addProperty("error","关联主文件操作未确认完成，已保留 .meta");setPreserved(row,preserved(s));emit();return@forEachIndexed
                    }
                }
                row.addProperty("state","running")
                var attempted=false;var removed=0
                try {
                    unchanged(s);phase=if(plan.operation=="rename")"renaming" else "deleting";emit();stop()
                    if(plan.operation=="rename") {
                        val occupied=plan.store.child(s.parent,s.targetName!!)
                        check(occupied==null||occupied.ref==s.node.ref) {"目标已存在同名项目，未覆盖"}
                        val result=if(s.targetName==s.node.name)s.node else {attempted=true;plan.store.rename(s.node,s.targetName,s.tree,{copyProgress->
                            phase=copyProgress.get("phase")?.asString?:"renaming";current=copyProgress.get("currentFile")?.asString?:s.node.name
                            emit((copyProgress.get("bytesTransferred")?.asLong?:0L)/4)
                        },cancelled)}
                        probeRename(s,row,result)
                    } else {
                        val order=s.tree.filter {!it.node.directory}+s.tree.filter {it.node.directory}.sortedByDescending {it.relative.size}
                        order.forEach {e ->
                            stop();val now=plan.store.stat(e.node.ref)?:error("源文件已改变")
                            check(now.identity==e.node.identity) {"源文件已被替换"}
                            if(now.directory)check(plan.store.children(now).isEmpty()) {"目录新增内容，已停止删除"} else fresh(e.node)
                            attempted=true;check(plan.store.remove(now)) {"删除回执不确定，请核对实际文件状态"};removed++
                            if(!e.node.directory) {completedFiles++;bytes+=e.node.bytes};emit()
                        }
                        check(plan.store.stat(s.node.ref)==null) {"删除结果未确认"}
                        row.addProperty("state","completed");row.addProperty("deleted",true);row.addProperty("sourcePreserved",false)
                    }
                    if(row.get("state").asString=="completed") {
                        completedItems++;if(plan.operation=="rename") {completedFiles+=s.tree.count {!it.node.directory};bytes+=s.tree.sumOf {if(it.node.directory)0L else it.node.bytes}}
                    }
                } catch(t:Throwable) {
                    row.addProperty("error",NativeTransferEngine.safeError(t))
                    if(t is NativeRenameCopyException) {
                        val receipt=t.receipt;row.add("compatibilityTransfer",receipt.deepCopy())
                        cancelledResult=cancelledResult||receipt.get("state")?.asString=="cancelled"||cancelled()
                        val copied=receipt.getAsJsonArray("items")?.firstOrNull()?.asJsonObject
                        copied?.get("destination")?.let {row.add("destination",it.deepCopy())}
                        receipt.get("destinationRef")?.let {row.add("destination",it.deepCopy())}
                        val retained=receipt.get("sourcePreserved")
                        if(retained==null||retained.isJsonNull)row.add("sourcePreserved",JsonNull.INSTANCE) else row.add("sourcePreserved",retained.deepCopy())
                        row.addProperty("state",if(receipt.get("state")?.asString=="cancelled")"cancelled" else if(receipt.get("state")?.asString=="completed"||receipt.get("destinationState")?.asString=="unknown")"unknown" else if(copied?.get("destinationCommitted")?.asBoolean==true||receipt.get("destinationState")?.asString=="present")"partial" else "failed")
                        if(retained?.isJsonNull==false&&retained.asBoolean)row.addProperty("renamed",false) else row.add("renamed",JsonNull.INSTANCE)
                        row.addProperty("targetVerified",copied?.get("targetVerified")?.asBoolean==true)
                    }
                    else if(plan.operation=="rename"&&attempted)probeRename(s,row)
                    else if(!attempted) {row.addProperty("state",if(t is CancellationException)"cancelled" else "failed");setPreserved(row,preserved(s))}
                    else {
                        val root=runCatching {plan.store.stat(s.node.ref)}
                        if(root.isSuccess&&root.getOrNull()==null) {row.addProperty("state","completed");row.addProperty("deleted",true);row.addProperty("sourcePreserved",false);completedItems++}
                        else {row.addProperty("state",if(removed>0)"partial" else "unknown");row.add("deleted",JsonNull.INSTANCE)
                            if(removed>0)row.addProperty("sourcePreserved",false) else row.add("sourcePreserved",JsonNull.INSTANCE)}
                    }
                    if(t is CancellationException)throw t
                }
                emit()
            }
        } catch(t:Throwable) {
            cancelledResult=t is CancellationException||cancelled()
            rows.forEachIndexed {index,row->if(row.get("state").asString in setOf("queued","running")) {row.addProperty("state",if(cancelledResult)"cancelled" else "failed");row.addProperty("error",NativeTransferEngine.safeError(t));setPreserved(row,preserved(plan.sources[index]))}}
        }
        val all=rows.all {it.get("state").asString=="completed"};val any=rows.any {it.get("state").asString=="completed"||it.get("state").asString=="partial"}
        val state=if(all)"completed" else if(cancelledResult)"cancelled" else if(any)"completed-with-errors" else "failed"
        phase=state;emit()
        return JsonObject().apply {
            addProperty("scope",plan.store.scope);addProperty("operation",plan.operation);addProperty("state",state)
            addProperty("completedCount",rows.count {it.get("state").asString=="completed"});addProperty("partial",!all&&any)
            add("items",JsonArray().apply {rows.forEach {add(it)}})
        }
    }
    companion object {
        private fun item(s:Source,scope:String)=JsonObject().apply {
            addProperty("source",s.node.ref);addProperty(if(scope=="local")"sourceUri" else "sourcePath",s.node.ref)
            addProperty("name",s.node.name);addProperty("isDirectory",s.node.directory);addProperty("automaticCompanion",s.automatic)
            s.companionOf?.let {addProperty("companionOf",it)}
            addProperty("fileCount",s.tree.count {!it.node.directory});addProperty("directoryCount",s.tree.count {it.node.directory});addProperty("bytes",s.tree.sumOf {if(it.node.directory)0L else it.node.bytes})
            s.targetName?.let {addProperty("newName",it);addProperty("targetName",it);addProperty("destination",s.parent.ref);addProperty("destinationParent",s.parent.ref)}
        }
        fun name(template:String,original:String,directory:Boolean,index:Int,total:Int):String {
            require(template.isNotBlank()&&template.length<=255) {"请输入有效名称或模板"}
            require(total==1||listOf("{index}","{name}","{stem}").any {it in template}) {"批量重命名请使用 {index}、{name} 或 {stem} 区分项目"}
            val dot=if(directory)-1 else original.lastIndexOf('.').takeIf {it>0}?:-1
            val ext=if(dot<0)"" else original.substring(dot);val stem=if(dot<0)original else original.substring(0,dot)
            val result=template.replace("{name}",stem).replace("{stem}",stem).replace("{ext}",ext).replace("{index}",index.toString())
            require('{' !in result&&'}' !in result) {"模板仅支持 {name}、{stem}、{ext}、{index}"}
            return NativeTransferEngine.safeName(result)
        }
    }
}
