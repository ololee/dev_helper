package cn.ololee.devhelper.netdisk

import android.content.Context
import android.content.ContextWrapper
import android.net.Uri
import android.system.Os
import androidx.test.ext.junit.runners.AndroidJUnit4
import androidx.test.platform.app.InstrumentationRegistry
import com.google.gson.JsonArray
import com.google.gson.JsonObject
import org.junit.Assert.*
import org.junit.Test
import org.junit.runner.RunWith
import java.io.File
import java.util.UUID

/** All files belong to the isolated verification APK. Never targets the user's installed app. */
@RunWith(AndroidJUnit4::class)
class NativeFileOperationDeviceTest {
    private class NoCloud:NativeTransferCloud {
        override fun account():String=error("Local operations must not contact Baidu")
        override fun rootPath():String=error("Local operations must not contact Baidu")
        override fun stat(path:String):JsonObject?=error("Unexpected cloud operation")
        override fun list(path:String):List<JsonObject> = error("Unexpected cloud operation")
        override fun mkdir(path:String):JsonObject=error("Unexpected cloud operation")
        override fun upload(local:File,path:String,progress:(Long)->Unit,cancelled:()->Boolean):JsonObject=error("Unexpected cloud operation")
        override fun download(meta:JsonObject,destination:File,progress:(Long)->Unit,cancelled:()->Boolean):JsonObject=error("Unexpected cloud operation")
        override fun delete(meta:JsonObject):JsonObject=error("Unexpected cloud operation")
    }
    private fun fixture(externalStorage:Boolean=false,block:(Context,NativeFileTransferRuntime,File)->Unit) {
        val base=InstrumentationRegistry.getInstrumentation().targetContext
        assertEquals("cn.ololee.devhelper.verification",base.packageName)
        val home=File(base.cacheDir,"native-mutation-test-${UUID.randomUUID()}").canonicalFile.apply {mkdirs()}
        val externalHome=if(externalStorage)File(base.getExternalFilesDir(null),"native-mutation-test-${UUID.randomUUID()}").canonicalFile.apply {mkdirs()} else null
        val context=object:ContextWrapper(base) {
            override fun getApplicationContext():Context=this
            override fun getFilesDir()=File(home,"files").apply {mkdirs()}
            override fun getCacheDir()=File(home,"cache").apply {mkdirs()}
            override fun getExternalFilesDir(type:String?)=externalHome?:File(home,"external").apply {mkdirs()}
        }
        val runtime=NativeFileTransferRuntime.createForTests(context,NoCloud())
        try {block(context,runtime,File(Uri.parse(runtime.status().get("defaultUri").asString).path!!))}
        finally {runtime.closeForTests();home.deleteRecursively();externalHome?.deleteRecursively()}
    }
    private fun request(op:String,files:List<File>,name:String?=null)=JsonObject().apply {
        addProperty("scope","local");addProperty("operation",op);name?.let {addProperty("name",it)}
        add("items",JsonArray().apply {files.forEach {add(JsonObject().apply {addProperty("uri",Uri.fromFile(it).toString())})}})
    }
    private fun run(runtime:NativeFileTransferRuntime,args:JsonObject):JsonObject {
        val plan=runtime.plan(args);val exec=JsonObject().apply {add("planId",plan.get("planId"));add("planDigest",plan.get("planDigest"))}
        val initial=runtime.execute(exec);assertEquals(initial.get("jobId"),runtime.execute(exec).get("jobId"))
        val deadline=System.currentTimeMillis()+10000
        while(System.currentTimeMillis()<deadline) {val j=runtime.job(initial.get("jobId").asString);if(j.get("state").asString !in setOf("queued","running"))return j;Thread.sleep(10)}
        error("Synthetic mutation did not settle")
    }
    @Test fun nativeFileAndDirectoryRenamePreserveUnicodeAndMetaWithoutCloud()=fixture {_,runtime,root ->
        val first=File(root,"image.png").apply {writeText("generated pixels")};File(root,"image.png.meta").writeText("guid:generated")
        val fileJob=run(runtime,request("rename",listOf(first),"图片😀.png"));assertEquals("completed",fileJob.get("state").asString)
        assertEquals("generated pixels",File(root,"图片😀.png").readText());assertEquals("guid:generated",File(root,"图片😀.png.meta").readText());assertFalse(first.exists())
        val folder=File(root,"folder").apply {mkdir()};File(folder,"empty").mkdir();File(folder,"shader.glsl").writeText("void main() {}")
        val folderJob=run(runtime,request("rename",listOf(folder),"场景🚀"));assertEquals("completed",folderJob.get("state").asString)
        assertTrue(File(root,"场景🚀/empty").isDirectory);assertEquals("void main() {}",File(root,"场景🚀/shader.glsl").readText())
    }
    @Test fun actualExternalScopedStorageSupportsFileFolderAndMetaRenameWithoutOverwrite()=fixture(externalStorage=true) {_,runtime,root ->
        assertTrue(root.path.contains("/storage/emulated/")||root.path.contains("/mnt/"))
        val file=File(root,"a.txt").apply {writeText("external synthetic file")};File(root,"a.txt.meta").writeText("guid:external-file")
        val folder=File(root,"folder").apply {mkdir()};File(folder,"empty").mkdir();File(folder,"payload.bin").writeBytes(ByteArray(256*1024) {(it%251).toByte()})
        File(root,"folder.meta").writeText("guid:external-folder");val sourceHash=NativeTransferEngine.sha256(File(folder,"payload.bin"))
        val result=run(runtime,request("rename",listOf(file,folder),"item_{index}{ext}"))
        assertEquals("completed",result.get("state").asString);assertEquals(4,result.getAsJsonObject("result").get("completedCount").asInt)
        assertFalse(file.exists());assertFalse(folder.exists());assertEquals("external synthetic file",File(root,"item_1.txt").readText())
        assertEquals("guid:external-file",File(root,"item_1.txt.meta").readText());assertEquals("guid:external-folder",File(root,"item_2.meta").readText())
        assertTrue(File(root,"item_2/empty").isDirectory);assertEquals(sourceHash,NativeTransferEngine.sha256(File(root,"item_2/payload.bin")))
        val kept=File(root,"keep").apply {writeText("do not overwrite")}
        assertThrows(IllegalStateException::class.java) {runtime.plan(request("rename",listOf(File(root,"item_1.txt")),kept.name))}
        assertEquals("do not overwrite",kept.readText());assertTrue(File(root,"item_1.txt").exists())
    }
    @Test fun kernelRenameNoReplaceRejectsExistingFileAndFolder()=fixture {_,_,root ->
        val a=File(root,"a").apply {writeText("original")};val b=File(root,"b").apply {writeText("keep")};val stat=Os.lstat(a.path)
        assertThrows(IllegalStateException::class.java) {NativeFileAtomicMove.rename(root.path,a.name,b.name,stat.st_dev,stat.st_ino)}
        assertEquals("original",a.readText());assertEquals("keep",b.readText())
        val d1=File(root,"d1").apply {mkdir()};val d2=File(root,"d2").apply {mkdir()};val ds=Os.lstat(d1.path)
        assertThrows(IllegalStateException::class.java) {NativeFileAtomicMove.rename(root.path,d1.name,d2.name,ds.st_dev,ds.st_ino)}
        assertTrue(d1.isDirectory&&d2.isDirectory)
    }
    @Test fun privatePreviewCommitMovesVerifiedInodeAcrossParentsWithoutHardLink()=fixture {context,_,_ ->
        val source=File(context.filesDir,"commit-${UUID.randomUUID()}.source").apply {writeText("verified preview bytes")}
        val targetDirectory=File(context.cacheDir,"target-${UUID.randomUUID()}").apply {mkdir()}
        val target=File(targetDirectory,"图像😀.png");val stat=Os.lstat(source.path)
        NativeFileAtomicMove.commitPreview(source.parentFile!!.canonicalPath,targetDirectory.canonicalPath,source.name,target.name,stat.st_dev,stat.st_ino)
        assertFalse(source.exists());assertEquals("verified preview bytes",target.readText())
        val another=File(context.filesDir,"another-${UUID.randomUUID()}.source").apply {writeText("retain on conflict")};val other=Os.lstat(another.path)
        assertThrows(IllegalStateException::class.java) {NativeFileAtomicMove.commitPreview(another.parentFile!!.canonicalPath,targetDirectory.canonicalPath,another.name,target.name,other.st_dev,other.st_ino)}
        assertEquals("retain on conflict",another.readText());assertEquals("verified preview bytes",target.readText())
        val absent=File(targetDirectory,"absent.png")
        assertThrows(IllegalStateException::class.java) {NativeFileAtomicMove.commitPreview(another.parentFile!!.canonicalPath,targetDirectory.canonicalPath,another.name,absent.name,other.st_dev,other.st_ino+1)}
        assertTrue(another.exists());assertFalse(absent.exists())
    }
    @Test fun batchRenameAndTrashDeleteOnlyReviewedItems()=fixture {_,runtime,root ->
        val a=File(root,"a.txt").apply {writeText("a")};val b=File(root,"b.txt").apply {writeText("b")};val keep=File(root,"keep.txt").apply {writeText("keep")}
        val renamed=run(runtime,request("rename",listOf(a,b),"note_{index}{ext}"));assertEquals("completed",renamed.get("state").asString)
        val deleted=run(runtime,request("trash",listOf(File(root,"note_1.txt"),File(root,"note_2.txt"))))
        assertEquals("completed",deleted.get("state").asString);assertEquals("keep",keep.readText())
        assertFalse(File(root,"note_1.txt").exists());assertTrue(deleted.getAsJsonObject("result").getAsJsonArray("items").all {it.asJsonObject.get("deleted").asBoolean})
    }
    @Test fun directoryTrashIncludesMetaButPreservesUnselectedSibling()=fixture {_,runtime,root ->
        val folder=File(root,"selected").apply {mkdir()};File(folder,"empty").mkdir();File(folder,"asset").writeText("asset")
        File(root,"selected.meta").writeText("guid:folder");File(root,"unselected").writeText("keep")
        val plan=runtime.plan(request("trash",listOf(folder)));assertTrue(plan.get("warning").asString.contains("永久"));assertEquals(2,plan.getAsJsonArray("items").size())
        val result=run(runtime,request("trash",listOf(folder)));assertEquals("completed",result.get("state").asString)
        assertFalse(folder.exists());assertFalse(File(root,"selected.meta").exists());assertEquals("keep",File(root,"unselected").readText())
    }
    @Test fun controlledPreviewReadsOnlyPrefixAndCreatesExclusivePrivateBinary()=fixture {context,runtime,root ->
        val data=ByteArray(600000) {(it%127).toByte()};val source=File(root,"generated.txt").apply {writeBytes(data)}
        val args=JsonObject().apply {addProperty("scope","local");addProperty("uri",Uri.fromFile(source).toString())}
        val meta=runtime.previewMetadata(args);assertEquals(600000L,meta.get("bytes").asLong)
        val pinned=args.deepCopy().apply {add("expectedFingerprint",meta.get("fingerprint"));add("expectedIdentity",meta.get("identity"))}
        assertArrayEquals(data.copyOf(256*1024),runtime.readPreviewPrefix(pinned,256*1024,{false}))
        val destination=File(context.cacheDir,"generated-preview.bin")
        val copied=runtime.copyPreviewSource(pinned,destination,{}, {false});assertArrayEquals(data,destination.readBytes());assertTrue(copied.get("verified").asBoolean)
        assertThrows(IllegalArgumentException::class.java) {runtime.copyPreviewSource(pinned,destination,{}, {false})}
        val outside=File(root,"not-private-output.bin")
        assertThrows(IllegalArgumentException::class.java) {runtime.copyPreviewSource(pinned,outside,{}, {false})};assertFalse(outside.exists())
        source.appendText("changed")
        assertThrows(IllegalStateException::class.java) {runtime.readPreviewPrefix(pinned,100,{false})}
    }
    @Test fun restartClearsOnlyOwnedRegularPreviewStagesAndPreservesUnknownFiles()=fixture {context,runtime,_ ->
        val stage=File(context.filesDir,"native-file-transfers/previews").apply {mkdirs()}
        val abandoned=File(stage,"${UUID.randomUUID()}.source").apply {writeText("incomplete preview")}
        val unknown=File(stage,"unknown.bin").apply {writeText("keep unknown")}
        val invalid=File(stage,"not-a-uuid.source").apply {writeText("keep invalid")}
        val directory=File(stage,"${UUID.randomUUID()}.source").apply {mkdir()};File(directory,"keep.txt").writeText("keep directory")
        val original=File(context.cacheDir,"original-${UUID.randomUUID()}").apply {writeText("keep linked target")}
        val symlink=File(stage,"${UUID.randomUUID()}.source");java.nio.file.Files.createSymbolicLink(symlink.toPath(),original.toPath())
        runtime.closeForTests()
        val restarted=NativeFileTransferRuntime.createForTests(context,NoCloud())
        try {
            assertFalse(abandoned.exists());assertEquals("keep unknown",unknown.readText());assertEquals("keep invalid",invalid.readText())
            assertEquals("keep directory",File(directory,"keep.txt").readText());assertTrue(java.nio.file.Files.isSymbolicLink(symlink.toPath()))
            assertEquals("keep linked target",original.readText())
        } finally {restarted.closeForTests()}
    }
    @Test fun restartDoesNotClaimUnfinishedSourceCleanupPreservedOriginals()=fixture {context,runtime,_ ->
        fun row(state:String,cleanup:String,preserved:Boolean)=JsonObject().apply {
            addProperty("state",state);addProperty("sourceCleanupState",cleanup);addProperty("sourcePreserved",preserved)
            addProperty("deleted",false);addProperty("renamed",false)
        }
        val rows=JsonArray().apply {add(row("completed","running",true));add(row("completed","completed",false));add(row("skipped","not-started",true))}
        val id=UUID.randomUUID().toString()
        val record=JsonObject().apply {
            addProperty("jobId",id);addProperty("state","running");addProperty("operation","move");addProperty("sourcePreserved",true)
            add("items",rows.deepCopy());add("progress",JsonObject().apply {addProperty("phase","cleaning-sources");add("items",rows.deepCopy())})
            add("result",JsonObject().apply {addProperty("sourcePreserved",true);addProperty("moveCommitted",false);add("items",rows.deepCopy())})
        }
        runtime.closeForTests();File(context.filesDir,"native-file-transfers/jobs.json").writeText(JsonArray().apply {add(record)}.toString())
        val restarted=NativeFileTransferRuntime.createForTests(context,NoCloud())
        try {
            val job=restarted.job(id);assertEquals("interrupted",job.get("state").asString);assertEquals("interrupted",job.getAsJsonObject("progress").get("phase").asString)
            assertTrue(job.get("sourcePreserved").isJsonNull);assertTrue(job.getAsJsonObject("result").get("sourcePreserved").isJsonNull)
            for(array in listOf(job.getAsJsonArray("items"),job.getAsJsonObject("progress").getAsJsonArray("items"),job.getAsJsonObject("result").getAsJsonArray("items"))) {
                assertEquals("unknown",array[0].asJsonObject.get("state").asString);assertTrue(array[0].asJsonObject.get("sourcePreserved").isJsonNull)
                assertTrue(array[0].asJsonObject.get("deleted").isJsonNull);assertTrue(array[0].asJsonObject.get("renamed").isJsonNull)
                assertFalse(array[1].asJsonObject.get("sourcePreserved").asBoolean);assertTrue(array[2].asJsonObject.get("sourcePreserved").asBoolean)
            }
        }finally {restarted.closeForTests()}
    }
    @Test fun mutationsAndPreviewRejectRootOutsideAndSymlink()=fixture {context,runtime,root ->
        assertThrows(IllegalArgumentException::class.java) {runtime.plan(request("trash",listOf(root)))}
        val outside=File(context.cacheDir,"outside.txt").apply {writeText("private-other")}
        val link=File(root,"link.txt");java.nio.file.Files.createSymbolicLink(link.toPath(),outside.toPath())
        try {
            for(file in listOf(outside,link)) {
                assertThrows(IllegalArgumentException::class.java) {runtime.plan(request("trash",listOf(file)))}
                assertThrows(IllegalArgumentException::class.java) {runtime.previewMetadata(JsonObject().apply {addProperty("scope","local");addProperty("uri",Uri.fromFile(file).toString())})}
            }
            assertEquals("private-other",outside.readText())
        } finally {link.delete()}
    }
}
