package cn.ololee.devhelper.netdisk

import android.content.Context
import android.content.ContextWrapper
import android.graphics.Bitmap
import android.graphics.BitmapFactory
import android.net.Uri
import androidx.test.ext.junit.runners.AndroidJUnit4
import androidx.test.platform.app.InstrumentationRegistry
import com.google.gson.JsonObject
import org.junit.Assert.*
import org.junit.Test
import org.junit.runner.RunWith
import java.io.File
import java.util.UUID
import java.util.concurrent.CancellationException

@RunWith(AndroidJUnit4::class)
class NativeFilePreviewDeviceTest {
    private class NoCloud : NativeTransferCloud {
        override fun account(): String = error("No cloud calls in isolated previews")
        override fun rootPath(): String = error("No cloud calls in isolated previews")
        override fun stat(path: String): JsonObject? = error("Unexpected cloud read")
        override fun list(path: String): List<JsonObject> = error("Unexpected cloud read")
        override fun mkdir(path: String): JsonObject = error("Unexpected cloud write")
        override fun upload(local: File, path: String, progress: (Long) -> Unit, cancelled: () -> Boolean): JsonObject = error("Unexpected upload")
        override fun download(meta: JsonObject, destination: File, progress: (Long) -> Unit, cancelled: () -> Boolean): JsonObject = error("Unexpected download")
        override fun delete(meta: JsonObject): JsonObject = error("Unexpected deletion")
    }
    private fun fixture(block: (NativeFilePreviewRuntime, File) -> Unit) {
        val base = InstrumentationRegistry.getInstrumentation().targetContext
        assertEquals("cn.ololee.devhelper.verification", base.packageName)
        val directory = File(base.cacheDir, "preview-device-${UUID.randomUUID()}").canonicalFile.apply { mkdirs() }
        val context = object : ContextWrapper(base) {
            override fun getApplicationContext(): Context = this
            override fun getFilesDir() = File(directory, "files").apply { mkdirs() }
            override fun getCacheDir() = File(directory, "cache").apply { mkdirs() }
            override fun getExternalFilesDir(type: String?) = File(directory, "external").apply { mkdirs() }
        }
        val source = NativeFileTransferRuntime.createForTests(context, NoCloud())
        val preview = NativeFilePreviewRuntime.createForTests(context, source)
        try { block(preview, File(Uri.parse(source.status().get("defaultUri").asString).path!!)) }
        finally { preview.closeForTests(); source.closeForTests(); directory.deleteRecursively() }
    }
    private fun args(file: File) = JsonObject().apply { addProperty("scope", "local"); addProperty("uri", Uri.fromFile(file).toString()) }
    @Test fun largeTextPreviewReadsABoundedPrefixAndNeverCreatesAMediaCopy() = fixture { preview, root ->
        val data = "只读 preview\n".repeat(40000).toByteArray()
        val file = File(root, "大日志.txt").apply { writeBytes(data) }
        val content = preview.open(args(file))
        assertEquals("text", content.kind); assertTrue(content.truncated); assertNull(content.file)
        assertTrue(content.text!!.toByteArray().size <= NativeFilePreviewPolicy.TEXT_BYTES)
        assertArrayEquals(data, file.readBytes())
        preview.binary(content.id).let { binary -> binary.stream.use { assertEquals(content.text, it.readBytes().toString(Charsets.UTF_8)) } }
        preview.close(content.id)
        assertThrows(IllegalStateException::class.java) { preview.binary(content.id) }
        assertArrayEquals(data, file.readBytes())
    }
    @Test fun imageIsAnIndependentBinaryCacheAndClosingPreservesOriginal() = fixture { preview, root ->
        val source = File(root, "generated.png")
        Bitmap.createBitmap(64, 32, Bitmap.Config.ARGB_8888).also { bitmap ->
            bitmap.eraseColor(android.graphics.Color.CYAN)
            source.outputStream().use { bitmap.compress(Bitmap.CompressFormat.PNG, 100, it) }; bitmap.recycle()
        }
        val original = source.readBytes(); val content = preview.open(args(source))
        assertEquals("image", content.kind); assertNotEquals(source.canonicalPath, content.file!!.canonicalPath)
        assertArrayEquals(original, content.file.readBytes())
        BitmapFactory.decodeFile(content.file.path).also { assertEquals(64, it.width); assertEquals(32, it.height); it.recycle() }
        preview.close(content.id); assertFalse(content.file.exists()); assertArrayEquals(original, source.readBytes())
    }
    @Test fun asyncPreviewPublishesOnlyBinaryPathAndCanBeClosedOrCancelled() = fixture { preview, root ->
        val source = File(root, "readme.md").apply { writeText("# Generated\n只读测试") }
        val initial = preview.start(args(source)); val id = initial.get("jobId").asString
        val deadline = System.currentTimeMillis() + 10_000
        var job = preview.job(id)
        while (job.get("state").asString in setOf("queued", "running") && System.currentTimeMillis() < deadline) { Thread.sleep(10); job = preview.job(id) }
        assertEquals("completed", job.get("state").asString)
        assertTrue(job.get("contentPath").asString.startsWith("/api/netdisk/file-ops/previews/"))
        assertFalse(job.toString().contains("base64")); assertFalse(job.toString().contains("/data/")); assertFalse(job.has("text"))
        val previewId = job.get("previewId").asString
        assertEquals("closed", preview.cancel(id).get("state").asString)
        assertThrows(IllegalStateException::class.java) { preview.binary(previewId) }
        assertThrows(CancellationException::class.java) { preview.open(args(source), cancelled = { true }) }
        assertEquals("# Generated\n只读测试", source.readText())
    }
}
