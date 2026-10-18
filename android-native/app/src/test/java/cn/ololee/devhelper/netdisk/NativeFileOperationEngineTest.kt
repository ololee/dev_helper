package cn.ololee.devhelper.netdisk

import com.google.gson.JsonObject
import org.junit.Assert.*
import org.junit.Test
import java.io.File
import java.util.concurrent.CancellationException

class NativeFileOperationEngineTest {
    private class Store(override val scope:String="local"):NativeTransferStore {
        data class Item(val directory:Boolean,val content:String="",var revision:Int=1)
        val entries=linkedMapOf("/" to Item(true));var removed=0;var renamed=0;var failDelete:String?=null
        var deleteThenThrow=false;var renameThenThrow=false;var readFailure=false
        fun parent(path:String)=path.substringBeforeLast('/').ifBlank {"/"}
        fun put(path:String,value:String="data") {entries[path]=Item(false,value);entries[parent(path)]?.revision=entries[parent(path)]!!.revision+1}
        fun dir(path:String) {entries[path]=Item(true);entries[parent(path)]?.revision=entries[parent(path)]!!.revision+1}
        override fun stat(ref:String):TransferNode? {if(readFailure)error("查询失败");return entries[ref]?.let {e->TransferNode(ref,if(ref=="/")null else parent(ref),if(ref=="/")"root" else ref.substringAfterLast('/'),e.directory,if(e.directory)0 else e.content.toByteArray().size.toLong(),e.revision.toLong(),"$ref:${e.revision}:${e.content}",identity=System.identityHashCode(e).toString())}}
        override fun children(node:TransferNode)=entries.keys.filter {it!="/"&&parent(it)==node.ref}.map {stat(it)!!}
        override fun child(parent:TransferNode,name:String)=stat(parent.ref.trimEnd('/')+"/"+name)
        override fun isRoot(node:TransferNode)=node.ref=="/"
        override fun read(node:TransferNode,destination:File,progress:(Long)->Unit,cancelled:()->Boolean) {error("Mutations must not download file data")}
        override fun createDirectory(parent:TransferNode,name:String):TransferNode {error("Mutations must not create dirs")}
        override fun write(parent:TransferNode,name:String,source:File,progress:(Long)->Unit,cancelled:()->Boolean):TransferNode {error("Mutations must not upload file data")}
        override fun rename(node:TransferNode,newName:String):TransferNode {
            val path=parent(node.ref).trimEnd('/')+"/"+newName;check(!entries.containsKey(path));renamed++
            val selected=entries.filterKeys {it==node.ref||it.startsWith(node.ref+"/")}.toMap()
            selected.forEach {(old,value)->entries.remove(old);entries[path+old.removePrefix(node.ref)]=value}
            if(renameThenThrow)error("回执丢失")
            return stat(path)!!
        }
        override fun remove(node:TransferNode):Boolean {
            if(node.ref==failDelete)return false
            check(!node.directory||children(node).isEmpty());entries.remove(node.ref);removed++
            entries[parent(node.ref)]?.let {it.revision++}
            if(deleteThenThrow)error("回执丢失")
            return true
        }
    }
    private val engine=NativeFileOperationEngine()
    private fun plan(s:Store,op:String="rename",refs:List<String> = listOf("/a.png"),name:String?=if(op=="rename")"new.png" else null)=engine.plan(s,op,refs,name)
    @Test fun literalNameAndSafeTokensRespectExtensionsAndUnicode() {
        assertEquals("图像_2.png",NativeFileOperationEngine.name("{stem}_{index}{ext}","图像.png",false,2,2))
        assertEquals("archive.tar_1.gz",NativeFileOperationEngine.name("{name}_{index}{ext}","archive.tar.gz",false,1,2))
        assertEquals(".gitignore_1",NativeFileOperationEngine.name("{name}_{index}{ext}",".gitignore",false,1,1))
        assertEquals("folder.ext_1",NativeFileOperationEngine.name("{stem}_{index}{ext}","folder.ext",true,1,1))
        for(value in listOf("../bad","x/y","{unknown}","x\u0000y","", "a".repeat(256)))assertThrows(IllegalArgumentException::class.java) {NativeFileOperationEngine.name(value,"a.png",false,1,1)}
        assertThrows(IllegalArgumentException::class.java) {NativeFileOperationEngine.name("same.png","a.png",false,1,2)}
    }
    @Test fun oneFileRenameHasVerifiedActualReceiptAndPreservesContent() {
        val s=Store();s.put("/a.png","texture");val result=engine.execute(plan(s,name="图像😀.png"),{}, {false})
        val item=result.getAsJsonArray("items")[0].asJsonObject
        assertEquals("completed",result.get("state").asString);assertTrue(item.get("renamed").asBoolean);assertTrue(item.get("targetVerified").asBoolean)
        assertEquals("/图像😀.png",item.get("destination").asString);assertNull(s.stat("/a.png"));assertEquals("texture",s.entries["/图像😀.png"]!!.content)
    }
    @Test fun batchRenameKeepsMetaPairingAndIndexesPrimarySelectionsOnly() {
        val s=Store();s.put("/a.png");s.put("/a.png.meta","guid:a");s.put("/b.png");s.put("/b.png.meta","guid:b")
        val p=plan(s,refs=listOf("/a.png","/a.png.meta","/b.png"),name="asset_{index}{ext}")
        assertEquals(listOf("asset_1.png","asset_1.png.meta","asset_2.png","asset_2.png.meta"),p.sources.map {it.targetName})
        assertFalse(p.sources[1].automatic);assertTrue(p.sources[3].automatic)
        val r=engine.execute(p,{}, {false});assertEquals("completed",r.get("state").asString);assertEquals(4,s.renamed)
        assertEquals("guid:a",s.entries["/asset_1.png.meta"]!!.content)
    }
    @Test fun explicitlySelectedDirectoryWithMetaSuffixIsNeverSilentlyDropped() {
        val s=Store();s.put("/a.png");s.dir("/a.png.meta");s.put("/a.png.meta/child","directory content")
        val p=plan(s,"trash",listOf("/a.png","/a.png.meta"))
        assertEquals(setOf("/a.png","/a.png.meta"),p.sources.map {it.node.ref}.toSet())
        assertTrue(p.sources.all {it.companionOf==null&&!it.automatic})
        val result=engine.execute(p,{}, {false});assertEquals("completed",result.get("state").asString)
        assertEquals(setOf("/"),s.entries.keys)
    }
    @Test fun explicitlySelectedSecondLevelMetaRemainsIndependentAndIsRenamed() {
        val s=Store();s.put("/a","base");s.put("/a.meta","first");s.put("/a.meta.meta","second")
        val p=plan(s,refs=listOf("/a","/a.meta","/a.meta.meta"),name="item_{index}{ext}")
        assertEquals(setOf("/a","/a.meta","/a.meta.meta"),p.sources.map {it.node.ref}.toSet())
        assertEquals(listOf("item_1","item_1.meta","item_2.meta"),p.sources.map {it.targetName})
        assertNull(p.sources.last().companionOf)
        val result=engine.execute(p,{}, {false});assertEquals("completed",result.get("state").asString);assertEquals(3,s.renamed)
        assertEquals("second",s.entries["/item_2.meta"]!!.content)
    }
    @Test fun folderRenameChecksFullDescendantsAndMeta() {
        val s=Store("netdisk");s.dir("/game");s.dir("/game/empty");s.put("/game/a.png");s.put("/game.meta","guid:folder")
        val p=plan(s,refs=listOf("/game"),name="renamed");assertEquals(2,p.directories)
        val r=engine.execute(p,{}, {false});assertEquals("completed",r.get("state").asString)
        assertNotNull(s.stat("/renamed/a.png"));assertNotNull(s.stat("/renamed/empty"));assertNotNull(s.stat("/renamed.meta"))
    }
    @Test fun sameNameIsConfirmedNoOpWithoutWrite() {
        val s=Store();s.put("/a.png");val r=engine.execute(plan(s,name="a.png"),{}, {false})
        assertEquals("completed",r.get("state").asString);assertEquals(0,s.renamed)
        assertFalse(r.getAsJsonArray("items")[0].asJsonObject.get("renamed").asBoolean)
    }
    @Test fun collisionAndSwapAreRejectedBeforeAnyMutation() {
        val s=Store();s.put("/a.png");s.put("/new.png","keep")
        assertThrows(IllegalStateException::class.java) {plan(s)};assertEquals("keep",s.entries["/new.png"]!!.content);assertEquals(0,s.renamed)
    }
    @Test fun collisionCreatedAfterPreviewCannotOverwriteAndSkipsMeta() {
        val s=Store();s.put("/a.png");s.put("/a.png.meta");val p=plan(s);s.put("/new.png","keep")
        val r=engine.execute(p,{}, {false});assertEquals("failed",r.get("state").asString);assertEquals(0,s.renamed)
        assertNotNull(s.stat("/a.png.meta"));assertNull(s.stat("/new.png.meta"));assertEquals("skipped",r.getAsJsonArray("items")[1].asJsonObject.get("state").asString)
    }
    @Test fun rootOverlapAndDuplicateSelectionsAreRejected() {
        val s=Store();s.dir("/game");s.put("/game/a.png")
        for(refs in listOf(listOf("/"),listOf("/game","/game/a.png"),listOf("/game/a.png","/game/a.png")))assertThrows(IllegalArgumentException::class.java) {plan(s,"trash",refs)}
    }
    @Test fun trashRemovesVerifiedSnapshotLeavesThenEmptyDirsAndMeta() {
        val s=Store();s.dir("/game");s.dir("/game/empty");s.put("/game/a.png");s.put("/game.meta")
        val p=plan(s,"trash",listOf("/game"));assertTrue(p.json().get("warning").asString.contains("永久"))
        val r=engine.execute(p,{}, {false});assertEquals("completed",r.get("state").asString);assertEquals(setOf("/"),s.entries.keys)
        assertTrue(r.getAsJsonArray("items").all {it.asJsonObject.get("deleted").asBoolean})
    }
    @Test fun staleSecondSourcePreventsDeletingFirstSource() {
        val s=Store();s.put("/a.png");s.put("/b.png");val p=plan(s,"trash",listOf("/a.png","/b.png"));s.put("/b.png","changed")
        val r=engine.execute(p,{}, {false});assertEquals("failed",r.get("state").asString);assertEquals(0,s.removed);assertNotNull(s.stat("/a.png"))
    }
    @Test fun deleteFailureNeverClaimsDeletedOrPreservedWithoutEvidence() {
        val s=Store();s.put("/a.png");s.failDelete="/a.png"
        val r=engine.execute(plan(s,"trash"),{}, {false});val row=r.getAsJsonArray("items")[0].asJsonObject
        assertEquals("failed",r.get("state").asString);assertEquals("unknown",row.get("state").asString);assertTrue(row.get("deleted").isJsonNull);assertTrue(row.get("sourcePreserved").isJsonNull)
    }
    @Test fun deletionReceiptLossIsProbedAndNeverRetried() {
        val s=Store();s.put("/a.png");s.deleteThenThrow=true
        val r=engine.execute(plan(s,"trash"),{}, {false});assertEquals("completed",r.get("state").asString);assertEquals(1,s.removed)
        assertTrue(r.getAsJsonArray("items")[0].asJsonObject.get("deleted").asBoolean)
    }
    @Test fun renameReceiptLossCanBeConfirmedByFreshIdentityAndFullTree() {
        val s=Store();s.dir("/game");s.put("/game/asset");s.renameThenThrow=true
        val r=engine.execute(plan(s,refs=listOf("/game"),name="next"),{}, {false})
        assertEquals("completed",r.get("state").asString);assertEquals(1,s.renamed);assertNotNull(s.stat("/next/asset"))
    }
    @Test fun cancellationBeforeFirstWritePreservesEverything() {
        val s=Store();s.put("/a.png");val r=engine.execute(plan(s,"trash"),{}, {true})
        assertEquals("cancelled",r.get("state").asString);assertEquals(0,s.removed);assertTrue(r.getAsJsonArray("items")[0].asJsonObject.get("sourcePreserved").asBoolean)
    }
    @Test fun cancellationDuringFolderDeletionHasHonestPartialOutcome() {
        val s=Store();s.dir("/game");s.put("/game/a");s.put("/game/b");s.put("/game.meta")
        val r=engine.execute(plan(s,"trash",listOf("/game")),{}, {s.removed>0})
        assertEquals("cancelled",r.get("state").asString);assertFalse(r.getAsJsonArray("items")[0].asJsonObject.get("sourcePreserved").asBoolean)
        assertNotNull(s.stat("/game/b"));assertNotNull(s.stat("/game.meta"));assertEquals(1,s.removed)
    }
    @Test fun changedDirectoryContentsInvalidateDeletePreview() {
        val s=Store();s.dir("/game");s.put("/game/a");val p=plan(s,"trash",listOf("/game"));s.put("/game/new")
        assertEquals("failed",engine.execute(p,{}, {false}).get("state").asString);assertEquals(0,s.removed)
    }
    @Test fun safStyleStableDocumentUriCanBeRenamedWithProviderCausalReceipt() {
        val store=object:NativeTransferStore {
            override val scope="local";var currentName="a.txt"
            fun root()=TransferNode("doc:root",null,"root",true,0,1,"root",identity="root-id")
            fun item()=TransferNode("doc:file","doc:root",currentName,false,4,1,"file:$currentName",identity="file-id")
            override fun stat(ref:String)=when(ref) {"doc:root"->root();"doc:file"->item();else->null}
            override fun children(node:TransferNode)=if(node.directory)listOf(item()) else emptyList()
            override fun child(parent:TransferNode,name:String)=item().takeIf {it.name==name}
            override fun isRoot(node:TransferNode)=node.ref=="doc:root"
            override fun rename(node:TransferNode,newName:String):TransferNode {currentName=newName;return item().also {it.extra.addProperty("_renameVerified",true);it.extra.addProperty("_renameOriginalIdentity",node.identity)}}
            override fun read(node:TransferNode,destination:File,progress:(Long)->Unit,cancelled:()->Boolean) {error("No data read")}
            override fun createDirectory(parent:TransferNode,name:String):TransferNode=error("No dirs")
            override fun write(parent:TransferNode,name:String,source:File,progress:(Long)->Unit,cancelled:()->Boolean):TransferNode=error("No upload")
            override fun remove(node:TransferNode):Boolean=error("No delete")
        }
        val p=engine.plan(store,"rename",listOf("doc:file"),"new.txt")
        val result=engine.execute(p,{}, {false});assertEquals("completed",result.get("state").asString)
        val row=result.getAsJsonArray("items")[0].asJsonObject
        assertEquals("doc:file",row.get("destination").asString);assertTrue(row.get("renamed").asBoolean)
    }
    @Test fun unsupportedOptionsAndExpiredPlansNeverMutate() {
        val s=Store();s.put("/a.png")
        assertThrows(IllegalArgumentException::class.java) {plan(s,"trash",name="bad")}
        assertThrows(IllegalArgumentException::class.java) {engine.plan(s,"rename",listOf("/a.png"),null)}
        assertThrows(IllegalArgumentException::class.java) {engine.execute(plan(s).copy(expires=0),{}, {false})}
        assertEquals(0,s.renamed+s.removed)
    }
}
