package cn.ololee.devhelper.netdisk

import android.content.Context
import android.os.StatFs
import android.system.Os
import android.system.OsConstants
import com.google.gson.JsonArray
import com.google.gson.JsonObject
import java.io.File
import java.io.FileInputStream
import java.io.InputStream
import java.util.UUID
import java.util.concurrent.CancellationException
import java.util.concurrent.ConcurrentHashMap
import java.util.concurrent.Executors
import java.util.concurrent.atomic.AtomicBoolean

data class NativeFilePreviewContent(val id: String, val name: String, val kind: String, val mimeType: String,
    val file: File?, val text: String?, val truncated: Boolean, val size: Long)
data class NativeFilePreviewBinary(val stream: InputStream, val mimeType: String, val bytes: Long)

/** Private, explicitly requested binary previews. Closing a preview never changes its source. */
class NativeFilePreviewRuntime private constructor(context: Context, private val sourceOverride: NativeFileTransferRuntime? = null) {
    private val context = context.applicationContext
    private val home = File(this.context.cacheDir, "native-file-previews").apply { mkdirs() }.canonicalFile
    private val runtime by lazy { sourceOverride ?: NativeFileTransferRuntime.get(this.context) }
    private val contents = ConcurrentHashMap<String, NativeFilePreviewContent>()
    private val jobs = linkedMapOf<String, JsonObject>()
    private val cancellations = ConcurrentHashMap<String, AtomicBoolean>()
    private val executor = Executors.newSingleThreadExecutor { r -> Thread(r, "native-file-preview").apply { isDaemon = true } }
    private val lock = Any()
    companion object {
        @Volatile private var instance: NativeFilePreviewRuntime? = null
        @JvmStatic fun get(context: Context): NativeFilePreviewRuntime = instance ?: synchronized(this) {
            instance ?: NativeFilePreviewRuntime(context).also { instance = it }
        }
        internal fun createForTests(context: Context, source: NativeFileTransferRuntime) = NativeFilePreviewRuntime(context, source)
    }
    init {
        // A new app process has no active player leases. Only this component's UUID directories are removed.
        home.listFiles()?.filter { it.name.matches(Regex("[0-9a-f-]{36}")) && !java.nio.file.Files.isSymbolicLink(it.toPath()) }
            ?.forEach { it.deleteRecursively() }
    }
    fun open(args: JsonObject, onProgress: (Long, Long) -> Unit = { _, _ -> }, cancelled: () -> Boolean = { false }): NativeFilePreviewContent {
        require(args.keySet().all { it in setOf("scope", "uri", "path", "fsId") }) { "预览参数包含未知字段" }
        fun checkCancelled() { if (cancelled() || Thread.currentThread().isInterrupted) throw CancellationException("预览已取消") }
        checkCancelled()
        val meta = runtime.previewMetadata(args, cancelled)
        checkCancelled()
        val name = meta.get("name").asString
        val size = meta.get("bytes").asLong
        val mime = meta.get("mime")?.asString ?: "application/octet-stream"
        val hintedKind = NativeFilePreviewPolicy.kind(name, mime)
        val id = UUID.randomUUID().toString()
        val request = args.deepCopy().apply {
            add("expectedFingerprint", meta.get("fingerprint"))
            add("expectedIdentity", meta.get("identity"))
        }
        var inspectedPrefix: ByteArray? = null
        var inspectedText: String? = null
        val kind = if (hintedKind == "ambiguous") {
            val prefix = runtime.readPreviewPrefix(request, NativeFilePreviewPolicy.TEXT_BYTES, cancelled)
            checkCancelled(); inspectedPrefix = prefix
            NativeFilePreviewPolicy.inspectTs(prefix, size > prefix.size).also { inspectedText = it.text }.kind
        } else hintedKind
        val effectiveMime = if (hintedKind == "ambiguous") when (kind) {
            "text" -> "text/plain"
            "video" -> "video/mp2t"
            else -> "application/octet-stream"
        } else mime
        if (kind == "unsupported") return NativeFilePreviewContent(id, name, kind, effectiveMime, null, null, false, size).also { contents[id] = it }
        if (kind == "text") {
            val prefix = inspectedPrefix ?: runtime.readPreviewPrefix(request, NativeFilePreviewPolicy.TEXT_BYTES, cancelled)
            checkCancelled()
            val truncated = size > prefix.size
            val text = inspectedText ?: NativeFilePreviewPolicy.text(prefix, truncated)
            val content = NativeFilePreviewContent(id, name, if (text == null) "unsupported" else "text", effectiveMime, null, text, truncated, size)
            contents[id] = content
            if (cancelled()) { close(id); throw CancellationException("预览已取消") }
            onProgress(prefix.size.toLong(), minOf(size, NativeFilePreviewPolicy.TEXT_BYTES.toLong()))
            return content
        }
        val directory = File(home, id)
        check(directory.mkdir()) { "无法创建预览缓存" }
        try {
            val available = StatFs(home.path).availableBytes
            require(size >= 0 && size <= available - 8L * 1024 * 1024) { "手机空间不足以预览此文件，请先释放空间" }
            val extension = name.substringAfterLast('.', "").takeIf { it.matches(Regex("[a-zA-Z0-9]{1,12}")) }?.let { ".$it" }.orEmpty()
            val target = File(directory, "content$extension")
            onProgress(0, size)
            runtime.copyPreviewSource(request, target, { done -> checkCancelled(); onProgress(done.coerceAtMost(size), size) }, cancelled)
            checkCancelled()
            val content = NativeFilePreviewContent(id, name, kind, effectiveMime, target, null, false, size)
            contents[id] = content
            if (cancelled()) { close(id); throw CancellationException("预览已取消") }
            onProgress(size, size)
            return content
        } catch (failure: Throwable) { directory.deleteRecursively(); throw failure }
    }
    fun close(id: String) {
        require(id.matches(Regex("[0-9a-f-]{36}"))) { "预览 ID 无效" }
        synchronized(lock) {
            contents.remove(id)
            File(home, id).deleteRecursively()
        }
    }
    fun metadata(args: JsonObject): JsonObject = runtime.previewMetadata(args).apply {
        val hint = NativeFilePreviewPolicy.kind(get("name").asString, get("mime")?.asString.orEmpty())
        addProperty("kind", hint); addProperty("metadataOnly", true)
        addProperty("requiresContentInspection", hint == "ambiguous")
        remove("fingerprint"); remove("identity")
        addProperty("encoding", "binary"); addProperty("native", true)
    }
    fun start(args: JsonObject): JsonObject {
        require(args.keySet().all { it in setOf("scope", "uri", "path", "fsId") }) { "预览参数包含未知字段" }
        val request = args.deepCopy()
        val id = UUID.randomUUID().toString(); val cancel = AtomicBoolean(false)
        val record = JsonObject().apply { addProperty("jobId", id); addProperty("state", "queued"); addProperty("bytes", 0); addProperty("totalBytes", 0); addProperty("cancelRequested", false) }
        synchronized(lock) {
            while (jobs.size >= 50) {
                val previous = jobs.entries.firstOrNull { it.value.get("state").asString !in setOf("queued", "running") } ?: error("预览任务过多，请先取消或关闭预览")
                previous.value.get("previewId")?.asString?.let { close(it) }; jobs.remove(previous.key)
            }
            jobs[id] = record; cancellations[id] = cancel
        }
        executor.submit {
            try {
                synchronized(lock) { record.addProperty("state", "running") }
                val content = open(request, { done, total -> synchronized(lock) { record.addProperty("bytes", done); record.addProperty("totalBytes", total) } }, { cancel.get() })
                synchronized(lock) {
                    if (cancel.get()) { close(content.id); record.addProperty("state", "cancelled") }
                    else {
                        record.addProperty("state", "completed"); record.addProperty("previewId", content.id)
                        record.addProperty("name", content.name); record.addProperty("kind", content.kind); record.addProperty("mimeType", content.mimeType)
                        record.addProperty("size", content.size); record.addProperty("truncated", content.truncated)
                        if (content.file != null || content.text != null) record.addProperty("contentPath", "/api/netdisk/file-ops/previews/${content.id}/content")
                        record.addProperty("encoding", "binary")
                    }
                }
            } catch (failure: Exception) { synchronized(lock) {
                record.addProperty("state", if (cancel.get() || failure is CancellationException) "cancelled" else "failed")
                record.addProperty("error", if (cancel.get()) "预览已取消" else "文件无法预览，请检查目录授权、文件格式和网盘连接")
            } } finally { cancellations.remove(id) }
        }
        return job(id)
    }
    fun job(id: String): JsonObject = synchronized(lock) { jobs[id]?.deepCopy() ?: error("预览任务不存在或已关闭") }
    fun cancel(id: String): JsonObject = synchronized(lock) {
        val job = jobs[id] ?: error("预览任务不存在或已关闭")
        cancellations[id]?.set(true); job.addProperty("cancelRequested", true)
        job.get("previewId")?.asString?.let { close(it); job.remove("contentPath"); job.addProperty("state", "closed") }
        job.deepCopy()
    }
    fun binary(id: String): NativeFilePreviewBinary = synchronized(lock) {
        val content = contents[id] ?: error("预览已关闭，请重新打开文件")
        val file = content.file
        if (file != null) {
            val fd = Os.open(file.path, OsConstants.O_RDONLY or OsConstants.O_NOFOLLOW, 0)
            try {
                val stat = Os.fstat(fd)
                check(OsConstants.S_ISREG(stat.st_mode) && stat.st_size == content.size) { "预览缓存已改变，请重新打开" }
                NativeFilePreviewBinary(FileInputStream(fd),
                    content.mimeType.takeIf { it.matches(Regex("[A-Za-z0-9!#$&^_.+-]+/[A-Za-z0-9!#$&^_.+-]+")) } ?: "application/octet-stream", stat.st_size)
            } catch (failure: Throwable) { runCatching { Os.close(fd) }; throw failure }
        }
        else {
            val bytes = content.text?.toByteArray(Charsets.UTF_8) ?: error("此类型没有可读取的预览内容")
            NativeFilePreviewBinary(bytes.inputStream(), "text/plain; charset=utf-8", bytes.size.toLong())
        }
    }
    internal fun closeForTests() {
        cancellations.values.forEach { it.set(true) }; executor.shutdownNow()
        contents.keys.toList().forEach { close(it) }
    }
}
