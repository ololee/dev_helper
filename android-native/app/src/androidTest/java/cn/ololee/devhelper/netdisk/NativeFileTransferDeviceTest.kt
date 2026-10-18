package cn.ololee.devhelper.netdisk

import android.content.Context
import android.content.ContextWrapper
import android.net.Uri
import androidx.test.ext.junit.runners.AndroidJUnit4
import androidx.test.platform.app.InstrumentationRegistry
import com.google.gson.JsonArray
import com.google.gson.JsonObject
import org.junit.Assert.*
import org.junit.Test
import org.junit.runner.RunWith
import java.io.File
import java.util.UUID
import java.util.concurrent.CancellationException
import java.util.concurrent.CountDownLatch
import java.util.concurrent.TimeUnit
import java.util.concurrent.atomic.AtomicInteger

/** Real Android local storage/queue, isolated verification app, no network or production data. */
@RunWith(AndroidJUnit4::class)
class NativeFileTransferDeviceTest {
    private class Cloud : NativeTransferCloud {
        private data class Entry(val id: String, val directory: Boolean, val bytes: ByteArray, val revision: Long)
        private val entries = linkedMapOf<String, Entry>()
        private var sequence = 100L
        var holdUpload = false
        val uploading = CountDownLatch(1)
        init { put(rootPath(), true) }
        @Synchronized private fun put(path: String, directory: Boolean, bytes: ByteArray = byteArrayOf()) {
            check(!entries.containsKey(path)); sequence++
            entries[path] = Entry(sequence.toString(), directory, bytes, sequence)
        }
        override fun account() = "generated-android-device-test"
        override fun rootPath() = "/apps/GeneratedAndroidTest"
        @Synchronized override fun stat(path: String): JsonObject? = entries[path]?.let { entry -> JsonObject().apply {
            addProperty("path", path); addProperty("name", path.substringAfterLast('/')); addProperty("fsId", entry.id)
            addProperty("isDirectory", entry.directory); addProperty("bytes", entry.bytes.size)
            addProperty("modifiedAt", entry.revision); addProperty("md5", "opaque-provider-fingerprint")
        } }
        @Synchronized override fun list(path: String) = entries.keys.filter { it != rootPath() && it.substringBeforeLast('/') == path }.map { stat(it)!! }
        @Synchronized override fun mkdir(path: String): JsonObject { put(path, true); return stat(path)!! }
        override fun upload(local: File, path: String, progress: (Long) -> Unit, cancelled: () -> Boolean): JsonObject {
            uploading.countDown()
            while (holdUpload) { if (cancelled()) throw CancellationException(); Thread.sleep(10) }
            if (cancelled()) throw CancellationException()
            val data = local.readBytes()
            synchronized(this) { put(path, false, data) }
            progress(data.size.toLong())
            return stat(path)!!
        }
        override fun download(meta: JsonObject, destination: File, progress: (Long) -> Unit, cancelled: () -> Boolean): JsonObject {
            val data = synchronized(this) { entries[meta.get("path").asString]!!.bytes.copyOf() }
            check(!destination.exists())
            destination.outputStream().use { output ->
                var offset = 0
                while (offset < data.size) {
                    if (cancelled()) throw CancellationException()
                    val count = minOf(1024, data.size - offset); output.write(data, offset, count); offset += count; progress(offset.toLong())
                }
            }
            return JsonObject().apply { addProperty("sha256", NativeTransferEngine.sha256(destination)); addProperty("bytes", data.size) }
        }
        @Synchronized override fun delete(meta: JsonObject): JsonObject {
            val path = meta.get("path").asString; check(path != rootPath())
            check(list(path).isEmpty()); check(entries.remove(path) != null)
            return JsonObject().apply { addProperty("deleted", true); addProperty("verified", true) }
        }
    }

    private fun fixture(block: (Context, NativeFileTransferRuntime, Cloud, File) -> Unit) {
        val base = InstrumentationRegistry.getInstrumentation().targetContext
        assertEquals("cn.ololee.devhelper.verification", base.packageName)
        val temporary = File(base.cacheDir, "native-transfer-device-${UUID.randomUUID()}").apply { mkdirs() }
        val context = object : ContextWrapper(base) {
            override fun getApplicationContext(): Context = this
            override fun getFilesDir() = File(temporary, "files").apply { mkdirs() }
            override fun getExternalFilesDir(type: String?) = File(temporary, "external").apply { mkdirs() }
        }
        val cloud = Cloud(); val runtime = NativeFileTransferRuntime.createForTests(context, cloud)
        try { block(context, runtime, cloud, File(Uri.parse(runtime.status().get("defaultUri").asString).path!!)) }
        finally { runtime.closeForTests(); temporary.deleteRecursively() }
    }
    private fun request(scope: String, operation: String, refs: List<String>, destination: String) = JsonObject().apply {
        addProperty("scope", scope); addProperty("destinationScope", if (scope == "local") "netdisk" else "local"); addProperty("operation", operation)
        add("items", JsonArray().apply { refs.forEach { ref -> add(JsonObject().apply { addProperty(if (scope == "local") "uri" else "path", ref) }) } })
        add("destination", JsonObject().apply { addProperty(if (scope == "local") "path" else "uri", destination) })
    }
    private fun run(runtime: NativeFileTransferRuntime, args: JsonObject): JsonObject {
        val plan = runtime.plan(args)
        val execution = JsonObject().apply { add("planId", plan.get("planId")); add("planDigest", plan.get("planDigest")) }
        val initial = runtime.execute(execution)
        assertEquals(initial.get("jobId"), runtime.execute(execution).get("jobId"))
        return await(runtime, initial.get("jobId").asString)
    }
    private fun await(runtime: NativeFileTransferRuntime, id: String): JsonObject {
        val deadline = System.currentTimeMillis() + 20_000
        while (System.currentTimeMillis() < deadline) {
            val job = runtime.job(id)
            if (job.get("state").asString !in setOf("queued", "running")) return job
            Thread.sleep(10)
        }
        throw AssertionError("Generated device transfer did not settle")
    }
    @Test fun actualAndroidFilesRoundTripWithMetaEmptyFolderAndPersistedProgress() = fixture { context, runtime, cloud, root ->
        val folder = File(root, "generated").apply { mkdirs() }; File(folder, "empty").mkdir()
        val content = ByteArray(65_537) { (it % 251).toByte() }; File(folder, "texture.bin").writeBytes(content)
        File(folder, "texture.bin.meta").writeText("fileFormatVersion: 2\nguid: 0123456789abcdef0123456789abcdef\n")
        val uploaded = run(runtime, request("local", "copy", listOf(Uri.fromFile(folder).toString()), cloud.rootPath()))
        assertEquals("completed", uploaded.get("state").asString)
        val progress = uploaded.getAsJsonObject("progress")
        assertEquals(progress.get("totalBytes"), progress.get("bytesTransferred"))
        assertTrue(File(folder, "texture.bin").exists())
        val destination = File(root, "returned").apply { mkdir() }
        val moved = run(runtime, request("netdisk", "move", listOf(cloud.rootPath() + "/generated"), Uri.fromFile(destination).toString()))
        assertEquals("completed", moved.get("state").asString)
        assertTrue(moved.getAsJsonObject("result").get("moveCommitted").asBoolean)
        assertArrayEquals(content, File(destination, "generated/texture.bin").readBytes())
        assertEquals(File(folder, "texture.bin.meta").readText(), File(destination, "generated/texture.bin.meta").readText())
        assertTrue(File(destination, "generated/empty").isDirectory)
        assertNull(cloud.stat(cloud.rootPath() + "/generated"))
        runtime.closeForTests()
        val restored = NativeFileTransferRuntime.createForTests(context, cloud)
        try { assertEquals("completed", restored.job(moved.get("jobId").asString).get("state").asString) }
        finally { restored.closeForTests() }
    }
    @Test fun cancellationUsesPersistentQueueAndKeepsActualPhoneOriginal() = fixture { _, runtime, cloud, root ->
        val file = File(root, "cancel.txt").apply { writeText("Generated cancellation source") }
        cloud.holdUpload = true
        val plan = runtime.plan(request("local", "move", listOf(Uri.fromFile(file).toString()), cloud.rootPath()))
        val initial = runtime.execute(JsonObject().apply { add("planId", plan.get("planId")); add("planDigest", plan.get("planDigest")) })
        assertTrue(cloud.uploading.await(5, TimeUnit.SECONDS))
        runtime.cancel(initial.get("jobId").asString)
        val result = await(runtime, initial.get("jobId").asString)
        assertEquals("cancelled", result.get("state").asString)
        assertEquals("Generated cancellation source", file.readText())
        assertTrue(result.getAsJsonObject("result").get("sourcePreserved").asBoolean)
        assertNull(cloud.stat(cloud.rootPath() + "/cancel.txt"))
    }
    @Test fun realLocalAdapterRejectsOutsidePathAndSymlinkBeforeAnyTransfer() = fixture { context, runtime, cloud, root ->
        val outside = File(context.cacheDir, "generated-outside-${UUID.randomUUID()}").apply { writeText("outside") }
        val link = File(root, "linked.txt")
        try {
            java.nio.file.Files.createSymbolicLink(link.toPath(), outside.toPath())
            for (ref in listOf(Uri.fromFile(outside).toString(), Uri.fromFile(link).toString())) {
                assertThrows(IllegalArgumentException::class.java) { runtime.plan(request("local", "move", listOf(ref), cloud.rootPath())) }
            }
            assertEquals("outside", outside.readText())
            assertTrue(cloud.list(cloud.rootPath()).isEmpty())
        } finally { link.delete(); outside.delete() }
    }
}
