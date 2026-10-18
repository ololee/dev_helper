package cn.ololee.devhelper.netdisk

import com.google.gson.JsonObject
import org.junit.Assert.*
import org.junit.Test
import java.io.File
import java.nio.file.Files
import java.util.concurrent.CancellationException

class NativeTransferEngineTest {
    private class MemoryStore(override val scope:String):NativeTransferStore {
        data class Item(val directory:Boolean,var bytes:ByteArray=byteArrayOf(),var revision:Long=1)
        val data=linkedMapOf("/" to Item(true));var failWrite:String?=null;var corruptWrite:String?=null
        var failRemove:String?=null;var deleteThenThrow:String?=null;var statFailure:String?=null;var removed=0;var onRead:((String)->Unit)?=null;var onWrite:((String)->Unit)?=null
        fun dir(path:String) {data[path]=Item(true);touch(parent(path))}
        fun put(path:String,body:String) {data[path]=Item(false,body.toByteArray());touch(parent(path))}
        private fun parent(ref:String)=ref.substringBeforeLast('/').ifEmpty {"/"}
        private fun touch(ref:String) {data[ref]?.let {it.revision++}}
        override fun stat(ref:String):TransferNode? {check(ref!=statFailure) {"状态不可确认"};return data[ref]?.let {
            TransferNode(ref,if(ref=="/")null else parent(ref),if(ref=="/")"root" else ref.substringAfterLast('/'),it.directory,
                if(it.directory)0L else it.bytes.size.toLong(),it.revision,"$ref:${it.directory}:${it.bytes.size}:${it.revision}",identity="$ref:${System.identityHashCode(it)}")
        }}
        override fun children(node:TransferNode)=data.keys.filter {it!="/"&&parent(it)==node.ref}.map {stat(it)!!}
        override fun child(parent:TransferNode,name:String)=stat(parent.ref.trimEnd('/')+"/"+name)
        override fun isRoot(node:TransferNode)=node.ref=="/"
        override fun read(node:TransferNode,destination:File,progress:(Long)->Unit,cancelled:()->Boolean) {
            if(cancelled())throw CancellationException();onRead?.invoke(node.ref)
            destination.writeBytes(data[node.ref]!!.bytes);progress(destination.length())
        }
        override fun createDirectory(parent:TransferNode,name:String):TransferNode {val path=parent.ref.trimEnd('/')+"/"+name
            check(!data.containsKey(path));dir(path);return stat(path)!!}
        override fun write(parent:TransferNode,name:String,source:File,progress:(Long)->Unit,cancelled:()->Boolean):TransferNode {
            if(cancelled())throw CancellationException();val path=parent.ref.trimEnd('/')+"/"+name
            check(!data.containsKey(path));check(path!=failWrite) {"写入失败"};val body=source.readBytes()
            data[path]=Item(false,if(path==corruptWrite)ByteArray(body.size){42} else body);touch(parent.ref)
            onWrite?.invoke(path);progress(body.size.toLong());return stat(path)!!
        }
        override fun remove(node:TransferNode):Boolean {if(node.ref==failRemove)return false
            if(node.ref==deleteThenThrow) {data.remove(node.ref);throw IllegalStateException("回执丢失")}
            check(data[node.ref]!=null);check(!node.directory||children(node).isEmpty());data.remove(node.ref);touch(parent(node.ref));removed++;return true}
    }
    private fun withFixture(block:(NativeTransferEngine,MemoryStore,MemoryStore)->Unit) {
        val temp=Files.createTempDirectory("native-transfer-test-").toFile()
        try {block(NativeTransferEngine(temp),MemoryStore("local"),MemoryStore("netdisk"))}finally{temp.deleteRecursively()}
    }
    private fun plan(e:NativeTransferEngine,a:MemoryStore,b:MemoryStore,op:String="copy",refs:List<String> = listOf("/file.txt"))=e.plan(a,b,op,refs,"/","account")
    @Test fun binaryCopyVerifiesShaAndReportsThreePassProgress()=withFixture {e,a,b ->
        a.put("/file.txt","hello 世界");val updates=mutableListOf<JsonObject>()
        val result=e.execute(plan(e,a,b),{updates.add(it)},{false})
        assertEquals("completed",result.get("state").asString);assertArrayEquals(a.data["/file.txt"]!!.bytes,b.data["/file.txt"]!!.bytes)
        assertTrue(result.get("sourcePreserved").asBoolean);assertEquals(0,a.removed)
        assertEquals(4L*a.data["/file.txt"]!!.bytes.size,updates.last().get("bytesTransferred").asLong)
        assertEquals(setOf("reading-source","transferring","verifying"),updates.map {it.get("phase").asString}.toSet().intersect(setOf("reading-source","transferring","verifying")))
        assertTrue(result.getAsJsonArray("items")[0].asJsonObject.get("targetVerified").asBoolean)
    }
    @Test fun moveCleansOnlyAfterWholeBatchVerified()=withFixture {e,a,b ->
        a.put("/a.txt","a");a.put("/b.txt","bb");b.onWrite={assertEquals(0,a.removed)}
        val result=e.execute(plan(e,a,b,"move",listOf("/a.txt","/b.txt")),{}, {false})
        assertEquals("completed",result.get("state").asString);assertTrue(result.get("moveCommitted").asBoolean)
        assertEquals("completed",result.get("sourceCleanupState").asString);assertEquals(2,a.removed)
    }
    @Test fun failureOnSecondFilePreservesEntireSourceBatch()=withFixture {e,a,b ->
        a.put("/a.txt","a");a.put("/b.txt","bb");b.failWrite="/b.txt"
        val result=e.execute(plan(e,a,b,"move",listOf("/a.txt","/b.txt")),{}, {false})
        assertEquals("failed",result.get("state").asString);assertFalse(result.get("moveCommitted").asBoolean)
        assertEquals(0,a.removed);assertTrue(a.data.keys.containsAll(listOf("/a.txt","/b.txt")))
        assertNotNull(b.stat("/a.txt"));assertTrue(result.get("sourcePreserved").asBoolean)
    }
    @Test fun equalLengthCorruptionFailsFullShaAndNeverCleansSource()=withFixture {e,a,b ->
        a.put("/file.txt","not stars");b.corruptWrite="/file.txt"
        val result=e.execute(plan(e,a,b,"move"),{}, {false})
        assertEquals("failed",result.get("state").asString);assertEquals(0,a.removed)
        assertTrue(result.get("error").asString.contains("SHA-256"))
    }
    @Test fun cancellationBeforeCleanupKeepsAllSourcesAndShowsCancelled()=withFixture {e,a,b ->
        a.put("/file.txt","a");var cancelled=false;b.onWrite={cancelled=true}
        val result=e.execute(plan(e,a,b,"move"),{}, {cancelled})
        assertEquals("cancelled",result.get("state").asString);assertTrue(result.get("sourcePreserved").asBoolean)
        assertEquals(0,a.removed);assertFalse(result.get("moveCommitted").asBoolean)
    }
    @Test fun cancellationDuringCleanupHasExplicitPartialResult()=withFixture {e,a,b ->
        a.put("/a.txt","a");a.put("/b.txt","b")
        val result=e.execute(plan(e,a,b,"move",listOf("/a.txt","/b.txt")),{}, {a.removed>0})
        assertEquals("cancelled",result.get("state").asString);assertEquals("partial",result.get("sourceCleanupState").asString)
        assertFalse(result.get("sourcePreserved").asBoolean);assertFalse(result.get("moveCommitted").asBoolean)
        assertNotNull(a.stat("/b.txt"));assertNotNull(b.stat("/a.txt"));assertNotNull(b.stat("/b.txt"))
    }
    @Test fun cleanupFailureIsNeverReportedAsSuccessfulMove()=withFixture {e,a,b ->
        a.put("/file.txt","a");a.failRemove="/file.txt"
        val result=e.execute(plan(e,a,b,"move"),{}, {false})
        assertEquals("completed-with-errors",result.get("state").asString);assertFalse(result.get("moveCommitted").asBoolean)
        assertTrue(result.get("sourcePreserved").asBoolean);assertEquals("failed",result.getAsJsonArray("items")[0].asJsonObject.get("sourceCleanupState").asString)
    }
    @Test fun directoryAndEmptyDirectoryPreserveStructureInBothDirections()=withFixture {e,a,b ->
        a.dir("/game");a.dir("/game/empty");a.put("/game/info.json","{}");a.dir("/game/nested");a.put("/game/nested/asset","data")
        val first=e.execute(plan(e,a,b,refs=listOf("/game")),{}, {false});assertEquals("completed",first.get("state").asString)
        assertTrue(b.stat("/game/empty")!!.directory);a.dir("/restored")
        val reverse=e.plan(b,a,"move",listOf("/game"),"/restored","account")
        val second=e.execute(reverse,{}, {false});assertEquals("completed",second.get("state").asString)
        assertNull(b.stat("/game"));assertNotNull(a.stat("/restored/game/nested/asset"));assertTrue(a.stat("/restored/game/empty")!!.directory)
    }
    @Test fun sidecarMetaIsAutomaticallyIncludedWithoutDuplicate()=withFixture {e,a,b ->
        a.put("/file.txt","a");a.put("/file.txt.meta","guid:42")
        val p=plan(e,a,b);assertEquals(2,p.sources.size);assertTrue(p.sources[1].companion)
        assertEquals(2,plan(e,a,b,refs=listOf("/file.txt","/file.txt.meta")).sources.size)
        val result=e.execute(p,{}, {false});assertEquals("completed",result.get("state").asString);assertNotNull(b.stat("/file.txt.meta"))
    }
    @Test fun unityFolderMetaMovesAlongsideDirectory()=withFixture {e,a,b ->
        a.dir("/game");a.put("/game.meta","guid:folder");a.put("/game/texture.png","data");a.put("/game/texture.png.meta","guid:image")
        val p=plan(e,a,b,"move",listOf("/game"));assertEquals(2,p.sources.size)
        val result=e.execute(p,{}, {false});assertEquals("completed",result.get("state").asString)
        assertNotNull(b.stat("/game.meta"));assertNotNull(b.stat("/game/texture.png.meta"));assertNull(a.stat("/game.meta"))
    }
    @Test fun noOverwriteAtPlanOrAfterPlan()=withFixture {e,a,b ->
        a.put("/file.txt","a");val p=plan(e,a,b,"move");b.put("/file.txt","other")
        assertThrows(IllegalStateException::class.java) {plan(e,a,b)}
        val result=e.execute(p,{}, {false});assertEquals("failed",result.get("state").asString)
        assertEquals("other",String(b.data["/file.txt"]!!.bytes));assertNotNull(a.stat("/file.txt"))
    }
    @Test fun sourceModificationAfterPlanCannotDeleteOrCopyChangedData()=withFixture {e,a,b ->
        a.put("/file.txt","a");val p=plan(e,a,b,"move");a.put("/file.txt","changed")
        val result=e.execute(p,{}, {false});assertEquals("failed",result.get("state").asString)
        assertEquals(0,a.removed);assertNull(b.stat("/file.txt"))
    }
    @Test fun fileAddedToSourceDirectoryAfterTransferPreventsAllCleanup()=withFixture {e,a,b ->
        a.dir("/game");a.put("/game/asset","data");b.onWrite={a.put("/game/new","new")}
        val result=e.execute(plan(e,a,b,"move",listOf("/game")),{}, {false})
        assertEquals("failed",result.get("state").asString);assertEquals(0,a.removed);assertNotNull(a.stat("/game/new"))
    }
    @Test fun rootDuplicateAndParentChildSelectionsAreRejected()=withFixture {e,a,b ->
        a.dir("/game");a.put("/game/a","a")
        assertThrows(IllegalArgumentException::class.java) {plan(e,a,b,refs=listOf("/"))}
        assertThrows(IllegalArgumentException::class.java) {plan(e,a,b,refs=listOf("/game","/game/a"))}
        assertThrows(IllegalArgumentException::class.java) {plan(e,a,b,refs=listOf("/game/a","/game/a"))}
        assertThrows(IllegalArgumentException::class.java) {e.plan(a,a,"copy",listOf("/game/a"),"/","account")}
    }
    @Test fun unsafeNamesRejectTraversalAndControlCharacters() {
        for(name in listOf("..",".","../bad","a/b","a\\b","a\u0000b","", "a".repeat(256)))
            assertThrows(IllegalArgumentException::class.java) {NativeTransferEngine.safeName(name)}
        assertEquals("资源图片.png",NativeTransferEngine.safeName("资源图片.png"))
    }
    @Test fun persistedPublicPlanNeverContainsAccountOrPrivateStaging()=withFixture {e,a,b ->
        a.put("/file.txt","a");val p=plan(e,a,b).publicJson().toString()
        assertFalse(p.contains("account"));assertFalse(p.contains("native-transfer-test-"));assertTrue(p.contains("planDigest"))
    }
    @Test fun zeroByteAndEmptyDirectoryMovesComplete()=withFixture {e,a,b ->
        a.put("/file.txt","");a.dir("/empty")
        val result=e.execute(plan(e,a,b,"move",listOf("/file.txt","/empty")),{}, {false})
        assertEquals("completed",result.get("state").asString);assertTrue(result.get("moveCommitted").asBoolean)
        assertEquals(0L,b.stat("/file.txt")!!.bytes);assertTrue(b.stat("/empty")!!.directory)
    }
    @Test fun targetEditedDuringLaterFileTransferPreventsEntireBatchCleanup()=withFixture {e,a,b ->
        a.put("/a.txt","a");a.put("/b.txt","b");b.onWrite={path->if(path=="/b.txt")b.put("/a.txt","edited")}
        val result=e.execute(plan(e,a,b,"move",listOf("/a.txt","/b.txt")),{}, {false})
        assertEquals("failed",result.get("state").asString);assertEquals(0,a.removed)
        assertNotNull(a.stat("/a.txt"));assertNotNull(a.stat("/b.txt"))
    }
    @Test fun targetEditedAfterBatchVerificationBeforeCleanupIsCaught()=withFixture {e,a,b ->
        a.put("/file.txt","a");var changed=false
        val result=e.execute(plan(e,a,b,"move"),{p->if(!changed&&p.get("phase").asString=="cleaning-sources") {changed=true;b.put("/file.txt","replacement")}}, {false})
        assertEquals("completed-with-errors",result.get("state").asString);assertEquals(0,a.removed)
        assertFalse(result.get("moveCommitted").asBoolean);assertTrue(result.get("sourcePreserved").asBoolean)
    }
    @Test fun deletionReceiptLossCannotReportOriginalPreserved()=withFixture {e,a,b ->
        a.put("/file.txt","a");a.deleteThenThrow="/file.txt"
        val result=e.execute(plan(e,a,b,"move"),{}, {false})
        assertEquals("completed-with-errors",result.get("state").asString);assertFalse(result.get("sourcePreserved").asBoolean)
        assertFalse(result.get("moveCommitted").asBoolean);assertNull(a.stat("/file.txt"))
    }
    @Test fun unqueryableSourceAfterDeleteFailureIsExplicitlyUnknown()=withFixture {e,a,b ->
        a.put("/file.txt","a");a.failRemove="/file.txt"
        var failureEnabled=false
        val result=e.execute(plan(e,a,b,"move"),{p->
            if(p.get("phase").asString=="cleaning-sources"&&!failureEnabled) {failureEnabled=true;a.statFailure="/file.txt"}
        }, {false})
        assertEquals("completed-with-errors",result.get("state").asString)
        assertTrue(result.get("sourcePreserved").isJsonNull)
        assertEquals("unknown",result.getAsJsonArray("items")[0].asJsonObject.get("sourceCleanupState").asString)
    }
    @Test fun sourceDirectoryReplacementIsNeverDeletedByFreshMetadata()=withFixture {e,a,b ->
        a.dir("/empty");var changed=false
        val result=e.execute(plan(e,a,b,"move",listOf("/empty")),{p->if(!changed&&p.get("phase").asString=="cleaning-sources") {changed=true;a.dir("/empty")}}, {false})
        assertEquals("completed-with-errors",result.get("state").asString);assertEquals(0,a.removed)
        assertFalse(result.get("moveCommitted").asBoolean);assertTrue(result.get("sourcePreserved").isJsonNull)
    }
    @Test fun expiryNeverExecutesFileOperations()=withFixture {e,a,b ->
        a.put("/file.txt","a");val p=plan(e,a,b).copy(expires=1)
        assertThrows(IllegalArgumentException::class.java) {e.execute(p,{}, {false})};assertNull(b.stat("/file.txt"))
    }
}
