package cn.ololee.devhelper.netdisk

import com.google.gson.JsonObject
import org.junit.Assert.*
import org.junit.Test
import java.io.File
import java.nio.file.Files
import java.util.concurrent.CancellationException

/** A FUSE-compatible rename must copy exclusively, verify, and only then clean the old name. */
class NativeRenameCopyTest {
    private class Store(val engine:NativeTransferEngine):NativeTransferStore {
        data class Item(val directory:Boolean,var content:ByteArray=byteArrayOf(),var revision:Long=1)
        override val scope="local"
        val data=linkedMapOf("/" to Item(true));var removed=0;var corrupt=false;var cancel=false;var rejectCleanup=false;var afterWrite:(()->Unit)?=null;var afterProof:(()->Unit)?=null
        fun parent(ref:String)=ref.substringBeforeLast('/').ifBlank {"/"}
        fun touch(ref:String) {data[ref]?.let {it.revision++}}
        fun put(ref:String,text:String) {data[ref]=Item(false,text.toByteArray());touch(parent(ref))}
        fun dir(ref:String) {data[ref]=Item(true);touch(parent(ref))}
        override fun stat(ref:String)=data[ref]?.let {TransferNode(ref,if(ref=="/")null else parent(ref),if(ref=="/")"root" else ref.substringAfterLast('/'),it.directory,if(it.directory)0L else it.content.size.toLong(),it.revision,"$ref:${it.revision}:${it.content.contentHashCode()}",identity=System.identityHashCode(it).toString())}
        override fun children(node:TransferNode)=data.keys.filter {it!="/"&&parent(it)==node.ref}.map {stat(it)!!}
        override fun child(parent:TransferNode,name:String)=stat(parent.ref.trimEnd('/')+"/"+name)
        override fun isRoot(node:TransferNode)=node.ref=="/"
        override fun read(node:TransferNode,destination:File,progress:(Long)->Unit,cancelled:()->Boolean) {if(cancelled())throw CancellationException();destination.writeBytes(data[node.ref]!!.content);progress(destination.length())}
        override fun createDirectory(parent:TransferNode,name:String):TransferNode {val ref=parent.ref.trimEnd('/')+"/"+name;check(ref !in data);dir(ref);return stat(ref)!!}
        override fun write(parent:TransferNode,name:String,source:File,progress:(Long)->Unit,cancelled:()->Boolean):TransferNode {
            if(cancelled())throw CancellationException();val ref=parent.ref.trimEnd('/')+"/"+name;check(ref !in data)
            data[ref]=Item(false,source.readBytes().let {if(corrupt)ByteArray(it.size){42} else it});touch(parent.ref);afterWrite?.invoke();progress(source.length());return stat(ref)!!
        }
        override fun remove(node:TransferNode):Boolean {if(rejectCleanup)return false;check(!node.directory||children(node).isEmpty());data.remove(node.ref);touch(parent(node.ref));removed++;return true}
        override fun rename(node:TransferNode,newName:String,inventory:List<NativeTransferEngine.Entry>,progress:(JsonObject)->Unit,cancelled:()->Boolean):TransferNode {
            val receipt=engine.renameByCopy(this,node,newName,inventory,progress,cancelled)
            if(receipt.get("moveCommitted").asBoolean!=true||receipt.get("state").asString!="completed")throw NativeRenameCopyException(receipt)
            val result=child(stat(node.parent!!)!!,newName)!!
            result.extra.addProperty("_renameVerified",true);result.extra.addProperty("_renameOriginalIdentity",node.identity);result.extra.add("_renameCopyResult",receipt)
            afterProof?.invoke();return result
        }
        fun inventory(ref:String):List<NativeTransferEngine.Entry> {
            val rows=mutableListOf<NativeTransferEngine.Entry>()
            fun visit(node:TransferNode,rel:List<String>) {rows.add(NativeTransferEngine.Entry(node,rel));if(node.directory)children(node).forEach {visit(it,rel+it.name)}}
            visit(stat(ref)!!,emptyList());return rows
        }
    }
    private fun fixture(block:(NativeTransferEngine,Store)->Unit) {
        val tmp=Files.createTempDirectory("native-rename-copy-").toFile()
        try {val engine=NativeTransferEngine(tmp);block(engine,Store(engine))}finally {tmp.deleteRecursively()}
    }
    @Test fun folderAndEmptyDirectoryGetNewIdentityOnlyAfterAllFilesVerified()=fixture {engine,store ->
        store.dir("/old");store.dir("/old/empty");store.put("/old/payload","hello 世界");store.put("/old.meta","unselected sidecar")
        val inventory=store.inventory("/old");val result=engine.renameByCopy(store,inventory.first().node,"新目录😀",inventory,{}, {false})
        assertEquals("completed",result.get("state").asString);assertTrue(result.get("moveCommitted").asBoolean)
        assertEquals("completed",result.get("sourceCleanupState").asString);assertNull(store.stat("/old"));assertTrue(store.stat("/新目录😀/empty")!!.directory)
        assertEquals("hello 世界",String(store.data["/新目录😀/payload"]!!.content));assertNotEquals(inventory.first().node.identity,store.stat("/新目录😀")!!.identity)
        assertNotNull(store.stat("/old.meta"));assertEquals(3,result.getAsJsonArray("verifiedTargets").size())
        assertTrue(result.getAsJsonArray("items")[0].asJsonObject.getAsJsonArray("files").all {it.asJsonObject.get("verified").asBoolean})
    }
    @Test fun existingTargetIsNeverOverwrittenByCompatibilityRename()=fixture {engine,store ->
        store.put("/old","original");store.put("/new","keep");val tree=store.inventory("/old")
        assertThrows(IllegalStateException::class.java) {engine.renameByCopy(store,tree.first().node,"new",tree,{}, {false})}
        assertEquals("keep",String(store.data["/new"]!!.content));assertNotNull(store.stat("/old"));assertEquals(0,store.removed)
    }
    @Test fun corruptEqualLengthCopyLeavesOriginalAndReportsVerificationFailure()=fixture {engine,store ->
        store.put("/old","original");store.corrupt=true;val tree=store.inventory("/old")
        val result=engine.renameByCopy(store,tree.first().node,"new",tree,{}, {false})
        assertEquals("failed",result.get("state").asString);assertTrue(result.get("sourcePreserved").asBoolean);assertFalse(result.get("moveCommitted").asBoolean);assertEquals(0,store.removed)
    }
    @Test fun cancelAfterTargetCreationNeverStartsSourceCleanup()=fixture {engine,store ->
        store.put("/old","original");store.afterWrite={store.cancel=true};val tree=store.inventory("/old")
        val result=engine.renameByCopy(store,tree.first().node,"new",tree,{}, {store.cancel})
        assertEquals("cancelled",result.get("state").asString);assertTrue(result.get("sourcePreserved").asBoolean);assertEquals("not-started",result.get("sourceCleanupState").asString);assertNotNull(store.stat("/old"));assertEquals(0,store.removed)
    }
    @Test fun staleDescendantFromReviewedInventoryCannotBeSilentlyRenamed()=fixture {engine,store ->
        store.dir("/old");store.put("/old/asset","old");val tree=store.inventory("/old");store.put("/old/asset","new")
        val result=engine.renameByCopy(store,tree.first().node,"new",tree,{}, {false})
        assertEquals("failed",result.get("state").asString);assertNull(store.stat("/new"));assertEquals(0,store.removed)
    }
    @Test fun operationEngineAcceptsCausalCopyProofWithNewInodesAndPairsMeta()=fixture {_,store ->
        store.put("/old.txt","payload");store.put("/old.txt.meta","guid:test")
        val operations=NativeFileOperationEngine();val plan=operations.plan(store,"rename",listOf("/old.txt"),"新名字😀.txt")
        val progress=mutableListOf<JsonObject>();val result=operations.execute(plan,{progress.add(it)}, {false})
        assertEquals("completed",result.get("state").asString);assertEquals(2,result.get("completedCount").asInt)
        assertEquals("payload",String(store.data["/新名字😀.txt"]!!.content));assertNotNull(store.stat("/新名字😀.txt.meta"))
        assertTrue(result.getAsJsonArray("items").all {it.asJsonObject.getAsJsonObject("compatibilityTransfer").get("moveCommitted").asBoolean})
        assertTrue(progress.any {it.get("phase").asString=="verifying-batch"});assertTrue(progress.all {it.get("bytesTransferred").asLong<=it.get("totalBytes").asLong})
    }
    @Test fun failedSourceCleanupReportsPartialCopyAndSkipsMetaWithoutReplaying()=fixture {_,store ->
        store.put("/old.txt","payload");store.put("/old.txt.meta","guid:test");store.rejectCleanup=true
        val operations=NativeFileOperationEngine();val result=operations.execute(operations.plan(store,"rename",listOf("/old.txt"),"new.txt"),{}, {false})
        val rows=result.getAsJsonArray("items");assertEquals("partial",rows[0].asJsonObject.get("state").asString)
        assertTrue(rows[0].asJsonObject.get("sourcePreserved").asBoolean);assertFalse(rows[0].asJsonObject.get("renamed").asBoolean)
        assertEquals("skipped",rows[1].asJsonObject.get("state").asString);assertNotNull(store.stat("/old.txt.meta"));assertNull(store.stat("/new.txt.meta"));assertEquals(0,store.removed)
    }
    @Test fun cancellationOfOnlyCompatibilityRenameIsStillReportedCancelled()=fixture {_,store ->
        store.put("/old.txt","payload");store.afterWrite={store.cancel=true}
        val operations=NativeFileOperationEngine();val result=operations.execute(operations.plan(store,"rename",listOf("/old.txt"),"new.txt"),{}, {store.cancel})
        assertEquals("cancelled",result.get("state").asString);assertEquals("cancelled",result.getAsJsonArray("items")[0].asJsonObject.get("state").asString)
        assertNotNull(store.stat("/old.txt"));assertEquals(0,store.removed)
    }
    @Test fun destinationChangedAfterCausalProofCannotBecomeSuccessfulRename()=fixture {_,store ->
        store.put("/old.txt","payload");store.afterProof={store.data["/new.txt"]!!.content="changed".toByteArray();store.touch("/new.txt")}
        val operations=NativeFileOperationEngine();val result=operations.execute(operations.plan(store,"rename",listOf("/old.txt"),"new.txt"),{}, {false})
        val row=result.getAsJsonArray("items")[0].asJsonObject;assertEquals("unknown",row.get("state").asString);assertTrue(row.get("renamed").isJsonNull);assertTrue(row.get("sourcePreserved").isJsonNull)
    }
}
