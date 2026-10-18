package cn.ololee.devhelper.netdisk

import com.google.gson.JsonArray
import com.google.gson.JsonObject
import com.google.gson.JsonParser
import okhttp3.FormBody
import okhttp3.HttpUrl.Companion.toHttpUrl
import okhttp3.MediaType.Companion.toMediaType
import okhttp3.MultipartBody
import okhttp3.OkHttpClient
import okhttp3.Request
import okhttp3.RequestBody
import okio.BufferedSink
import java.io.Closeable
import java.io.File
import java.io.FileInputStream
import java.io.IOException
import java.io.InputStream
import java.io.RandomAccessFile
import java.nio.file.Files
import java.nio.file.LinkOption
import java.nio.file.StandardOpenOption
import java.security.MessageDigest
import java.util.concurrent.CancellationException
import java.util.concurrent.ConcurrentHashMap
import java.util.concurrent.TimeUnit
import java.util.concurrent.atomic.AtomicBoolean

/** Open Platform binary data plane. Mutations are never retried or silently renamed. */
internal class BaiduBinaryFileClient(
    config: JsonObject,
    private val transport: BinaryTransport = OkHttpBinaryTransport()
) : NativeTransferCloud {
    private val config = config.deepCopy()
    private val root = BaiduNetdiskPolicy.path(BaiduNetdiskPolicy.get(config, "remoteRoot"))
    private val token = BaiduNetdiskPolicy.get(config, "accessToken")
    // Only positive IDs are cached. Every stat still reads live filemetas, never cached metadata.
    private val knownFileIds = ConcurrentHashMap<String, String>()
    @Volatile private var accountId: String? = null

    init {
        require(root != "/" && root.startsWith("/apps/")) { "二进制资源传输需要限定到本应用 /apps/ 目录" }
        require(token.isNotBlank()) { "请先授权百度网盘" }
    }
    override fun rootPath(): String = root
    override fun account(): String {
        accountId?.let { return it }
        synchronized(this) {
            accountId?.let { return it }
            val user = json("GET", NAS_API, mapOf("method" to "uinfo"))
            val id = string(user, "uk")
            require(Regex("[0-9]{1,24}").matches(id) && id.any { it != '0' }) { "百度未返回有效账号 ID" }
            val fields = listOf("appId", "clientId", "thirdId").map { BaiduNetdiskPolicy.get(config, it) } + listOf(root, id)
            return digest("SHA-256", fields.joinToString("\u0000").toByteArray()).also { accountId = it }
        }
    }
    private fun path(input: String, source: Boolean = false): String = BaiduNetdiskPolicy.insideRoot(config, input).also {
        require(!source || it != root) { "不能将网盘应用根目录作为文件操作源" }
    }
    override fun list(path: String): List<JsonObject> = listDirectory(this.path(path), { false })
    private fun listDirectory(path: String, cancelled: () -> Boolean): List<JsonObject> {
        val rows = ArrayList<JsonObject>()
        val seen = HashSet<String>()
        for (start in 0 until 100_000 step 1000) {
            checkCancelled(cancelled)
            val value = try { json("GET", FILE_API, mapOf("method" to "list", "dir" to path,
                "start" to start.toString(), "limit" to "1000", "order" to "name", "desc" to "0"), cancelled = cancelled) }
            catch (error: BinaryProviderException) {
                if (error.providerCode == -9L && path == root) return rememberListing(path, emptyList())
                throw error
            }
            val entries = value.get("list")?.takeIf { it.isJsonArray }?.asJsonArray ?: error("百度文件列表格式无效")
            for (entry in entries) {
                require(entry.isJsonObject) { "百度文件列表项目格式无效" }
                val item = metadata(entry.asJsonObject)
                val itemPath = string(item, "path")
                require(parent(itemPath) == path && seen.add(itemPath)) { "网盘目录列表发生变化或返回重复项目，请刷新" }
                rows.add(item)
            }
            val hasMore = value.get("has_more")?.let {
                require(it.isJsonPrimitive) { "百度分页标记无效" }
                if (it.asJsonPrimitive.isBoolean) it.asBoolean else integer(value, "has_more") != 0L
            } ?: (entries.size() >= 1000)
            if (!hasMore) return rememberListing(path, rows)
            require(entries.size() > 0) { "百度分页未返回项目，未确认目录完整性" }
        }
        error("网盘目录超过单次确认范围，请分目录操作")
    }
    override fun stat(path: String): JsonObject? {
        val target = this.path(path)
        if (target == root) {
            listDirectory(target, { false })
            return JsonObject().apply { addProperty("path", root); addProperty("name", root.substringAfterLast('/'))
                addProperty("fsId", "0"); addProperty("isDirectory", true); addProperty("bytes", 0L); addProperty("modifiedAt", 0L); addProperty("md5", "") }
        }
        knownFileIds[target]?.let { id ->
            val cached = try { fileInfo(id, false) }
            catch (_: BinaryFileMissingException) { null }
            catch (_: BinaryFileOutsideRootException) { null }
            catch (error: BinaryProviderException) { if (error.providerCode == -9L) null else throw error }
            if (cached != null && string(cached, "path") == target) return cached
            // An old ID may have been removed or moved. Re-list the parent to detect a replacement.
            knownFileIds.remove(target, id)
        }
        return statFromParent(target)
    }
    private fun statFromParent(target: String): JsonObject? {
        val match = listDirectory(parent(target), { false }).singleOrNull { string(it, "path") == target } ?: return null
        return fileInfo(string(match, "fsId"), false).also {
            require(string(it, "path") == target) { "网盘文件 ID 与路径不一致" }
        }
    }
    private fun rememberListing(directory: String, entries: List<JsonObject>): List<JsonObject> {
        val found = entries.associate { string(it, "path") to string(it, "fsId") }
        // A complete new listing also invalidates former direct children; absence is not cached.
        knownFileIds.entries.forEach { (path, id) ->
            if (parent(path) == directory && path !in found) knownFileIds.remove(path, id)
        }
        knownFileIds.putAll(found)
        return entries
    }
    private fun invalidate(path: String) {
        knownFileIds.entries.forEach { (known, id) ->
            if (known == path || known.startsWith("$path/")) knownFileIds.remove(known, id)
        }
    }
    override fun mkdir(path: String): JsonObject {
        val target = this.path(path, true)
        require(stat(target) == null) { "目标存在同名文件或目录，不会覆盖" }
        val result = json("POST", FILE_API, mapOf("method" to "create"), mapOf(
            "path" to target, "isdir" to "1", "size" to "0", "block_list" to "[]", "rtype" to "0"))
        require(string(result, "path") == target && integer(result, "isdir") == 1L) { "百度未确认创建准确目录，请核对云端列表" }
        val id = fsId(result)
        return fileInfo(id, false).also {
            require(string(it, "path") == target && bool(it, "isDirectory")) { "百度未确认新建目录" }
        }
    }
    override fun upload(local: File, path: String, progress: (Long) -> Unit, cancelled: () -> Boolean): JsonObject {
        val target = this.path(path, true)
        checkCancelled(cancelled)
        require(Files.isRegularFile(local.toPath(), LinkOption.NOFOLLOW_LINKS)) { "上传源必须是普通文件" }
        require(stat(target) == null) { "目标存在同名文件或目录，不会覆盖" }
        val before = fingerprint(local)
        val total = local.length()
        require(total <= BLOCK_SIZE.toLong() * MAX_BLOCKS) { "文件超过百度分片协议限制" }
        val blocks = ArrayList<String>()
        val sha = MessageDigest.getInstance("SHA-256")
        FileInputStream(local).use { input ->
            val buffer = ByteArray(BLOCK_SIZE)
            do {
                var count = 0
                while (count < buffer.size) {
                    checkCancelled(cancelled)
                    val read = input.read(buffer, count, minOf(IO_SIZE, buffer.size - count))
                    if (read < 0) break
                    count += read
                }
                if (count == 0) break
                blocks.add(digest("MD5", buffer, count)); sha.update(buffer, 0, count)
            } while (true)
        }
        if (blocks.isEmpty()) blocks.add(digest("MD5", byteArrayOf()))
        require(fingerprint(local) == before) { "源文件在读取期间改变，已停止" }
        val blockList = JsonArray().apply { blocks.forEach { add(it) } }.toString()
        val form = linkedMapOf("path" to target, "size" to total.toString(), "isdir" to "0", "autoinit" to "1",
            "block_list" to blockList, "rtype" to "0", "local_mtime" to (local.lastModified() / 1000).toString())
        val pre = json("POST", FILE_API, mapOf("method" to "precreate"), form, cancelled = cancelled)
        if (pre.has("path")) require(string(pre, "path") == target) { "百度预上传路径改变，未接受自动改名结果" }
        checkCancelled(cancelled)
        val result: JsonObject
        if (pre.get("return_type")?.asString == "2") {
            result = strictReceipt(pre, target, total, rapid = true)
        } else {
            val uploadId = string(pre, "uploadid").also { require(it.isNotEmpty() && it.length <= 4096) { "百度未返回上传 ID" } }
            val missing = pre.get("block_list")?.let {
                require(it.isJsonArray) { "百度缺失分片列表无效" }
                it.asJsonArray.map { item -> runCatching { item.asBigDecimal.intValueExact() }.getOrElse { error("百度分片编号无效") } }
            } ?: blocks.indices.toList()
            require(missing.all { it in blocks.indices } && missing.toSet().size == missing.size) { "百度分片列表重复或超出范围" }
            var transferred = total - missing.sumOf { partSize(total, it).toLong() }
            progress(transferred)
            for (number in missing) {
                checkCancelled(cancelled)
                require(fingerprint(local) == before) { "上传源在传输期间改变，已停止" }
                val base = transferred
                val body = MultipartBody.Builder().setType(MultipartBody.FORM).addFormDataPart("file", "part.bin",
                    FileSliceBody(local, number.toLong() * BLOCK_SIZE, partSize(total, number).toLong(),
                        { n -> progress(minOf(total, base + n)) }, cancelled)).build()
                val reply = json("POST", UPLOAD_API, mapOf("method" to "upload", "type" to "tmpfile", "path" to target,
                    "uploadid" to uploadId, "partseq" to number.toString()), body = body, cancelled = cancelled)
                require(string(reply, "md5").lowercase() == blocks[number]) { "上传分片 MD5 校验失败，未提交文件" }
                transferred += partSize(total, number)
                progress(transferred)
            }
            checkCancelled(cancelled)
            require(fingerprint(local) == before) { "上传源在提交前改变，已停止" }
            form.remove("autoinit"); form["uploadid"] = uploadId
            result = strictReceipt(json("POST", FILE_API, mapOf("method" to "create"), form, cancelled = cancelled), target, total, false)
        }
        // Persisted SHA describes the exact immutable staging source. Provider md5 stays opaque.
        result.addProperty("sha256", hex(sha.digest())); result.addProperty("verified", true)
        result.addProperty("verification", "upload-receipt"); progress(total)
        return result
    }
    private fun strictReceipt(value: JsonObject, target: String, total: Long, rapid: Boolean): JsonObject {
        if (value.has("path")) require(string(value, "path") == target) { "百度上传路径与目标不一致，未接受改名结果" }
        if (value.has("size")) require(integer(value, "size") == total) { "百度上传文件大小与源不一致" }
        if (value.has("isdir")) require(integer(value, "isdir") == 0L) { "百度上传结果不是文件" }
        val hasId = value.has("fs_id")
        if (hasId) fsId(value)
        if (hasId && value.has("size") && value.has("path")) return fileInfo(fsId(value), false).also {
            require(string(it, "path") == target && integer(it, "bytes") == total && !bool(it, "isDirectory")) {
                "百度上传后文件元数据与目标不一致"
            }
        }
        require(rapid) { "百度未确认文件路径、大小和 ID，请检查云端列表后重试" }
        val candidate = if (hasId) fileInfo(fsId(value), false) else stat(target) ?: error("百度秒传未找到准确目标，请检查云端列表")
        require(string(candidate, "path") == target && integer(candidate, "bytes") == total && !bool(candidate, "isDirectory")) { "百度秒传结果与目标不一致" }
        return candidate
    }
    override fun download(meta: JsonObject, destination: File, progress: (Long) -> Unit, cancelled: () -> Boolean): JsonObject {
        checkCancelled(cancelled)
        val target = path(string(meta, "path"), true)
        val current = fileInfo(string(meta, "fsId"), true, cancelled)
        require(sameSnapshot(meta, current) && !bool(current, "isDirectory")) { "网盘源文件已变化，请刷新后重试" }
        val total = integer(current, "bytes")
        val expectedSha = string(meta, "sha256").takeIf { it.isNotEmpty() }
        expectedSha?.let { require(Regex("[0-9a-fA-F]{64}").matches(it)) { "SHA256 格式无效" } }
        require(!destination.exists()) { "本地目标已存在，不会覆盖" }
        destination.parentFile?.let { require(it.isDirectory) { "本地下载目标目录不存在" } }
        var link = downloadUrl(string(current, "_dlink"))
        val sha = MessageDigest.getInstance("SHA-256")
        var copied = 0L
        var created = false
        try {
            var redirects = 0
            var reply: BinaryReply
            while (true) {
                checkCancelled(cancelled)
                reply = transport.open(Request.Builder().url(link).get().header("User-Agent", "pan.baidu.com")
                    .header("Accept-Encoding", "identity").build(), cancelled)
                if (reply.code !in setOf(301, 302, 303, 307, 308)) break
                val location = reply.header("Location")
                reply.close()
                require(++redirects <= 5 && !location.isNullOrEmpty()) { "百度下载重定向无效" }
                link = downloadUrl(link.toHttpUrl().resolve(location)!!.toString())
            }
            reply.use {
                require(it.code in 200..299) { "百度下载失败，请检查网络或授权" }
                Files.newOutputStream(destination.toPath(), StandardOpenOption.CREATE_NEW, StandardOpenOption.WRITE).use { output ->
                    created = true
                    val buffer = ByteArray(IO_SIZE)
                    while (true) {
                        checkCancelled(cancelled)
                        val read = it.input.read(buffer)
                        if (read < 0) break
                        copied += read
                        require(copied <= total) { "下载内容超出声明大小，未保留不完整文件" }
                        output.write(buffer, 0, read); sha.update(buffer, 0, read); progress(copied)
                    }
                    output.flush()
                }
            }
            checkCancelled(cancelled)
            val digest = hex(sha.digest())
            require(copied == total) { "下载文件大小校验失败，未保留不完整文件" }
            require(expectedSha == null || digest == expectedSha.lowercase()) { "下载 SHA256 与源记录不一致" }
            require(sameSnapshot(meta, fileInfo(string(meta, "fsId"), false, cancelled))) { "下载期间网盘源已变化，未提交文件" }
            progress(total)
            return JsonObject().apply { addProperty("path", target); addProperty("fsId", string(meta, "fsId")); addProperty("bytes", copied)
                addProperty("sha256", digest); addProperty("verified", true); addProperty("verifiedAgainstManifest", expectedSha != null)
                addProperty("remoteMd5", string(current, "md5")) }
        } catch (failure: Throwable) {
            if (created) destination.delete()
            if (cancelled()) throw CancellationException("传输已取消，未清理源文件")
            if (failure is IOException) throw IOException("二进制下载中断，请查看任务状态，源文件保留")
            throw failure
        }
    }
    override fun delete(meta: JsonObject): JsonObject {
        val target = path(string(meta, "path"), true)
        require(sameSnapshot(meta, fileInfo(string(meta, "fsId"), false))) { "网盘源在清理前变化，未删除" }
        val response = try { json("POST", FILE_API, mapOf("method" to "filemanager", "opera" to "delete"),
            mapOf("async" to "0", "ondup" to "fail", "filelist" to JsonArray().apply { add(target) }.toString())) }
        catch (error: Exception) { return JsonObject().apply { addProperty("path", target); addProperty("deleted", false)
            addProperty("verified", false); addProperty("state", "unknown"); addProperty("error", "百度未确认清理结果，未重放删除，请核对云端") } }
        finally { invalidate(target) }
        val info = response.get("info")?.takeIf { it.isJsonArray }?.asJsonArray
        val receipt = info?.filter { it.isJsonObject && string(it.asJsonObject, "path") == target }?.singleOrNull()?.asJsonObject
        val accepted = receipt != null && (receipt.get("errno") ?: receipt.get("error_code"))?.let { runCatching { it.asLong == 0L }.getOrDefault(false) } == true
        val asynchronous = string(response, "taskid").let { it.isNotBlank() && it != "0" }
        // Never let a former positive ID stand in for post-delete path verification.
        val absent = try { statFromParent(target) == null } catch (error: Exception) { false }
        val verified = accepted && absent && !(asynchronous && bool(meta, "isDirectory"))
        return JsonObject().apply { addProperty("path", target); addProperty("fsId", string(meta, "fsId")); addProperty("deleted", verified)
            addProperty("verified", verified); addProperty("state", if (verified) "completed" else "unknown")
            if (!verified) addProperty("error", "百度尚未确认源文件清理完成，请核对云端，未自动重试") }
    }
    private fun fileInfo(id: String, dlink: Boolean, cancelled: () -> Boolean = { false }): JsonObject {
        require(Regex("[0-9]{1,24}").matches(id) && id.any { it != '0' }) { "百度文件 ID 无效" }
        val response = json("GET", META_API, mapOf("method" to "filemetas", "fsids" to "[$id]", "dlink" to if (dlink) "1" else "0"), cancelled = cancelled)
        val rows = response.get("list")?.takeIf { it.isJsonArray }?.asJsonArray ?: error("百度未返回文件元数据")
        if (rows.size() == 0) throw BinaryFileMissingException()
        require(rows.size() == 1 && rows[0].isJsonObject && fsId(rows[0].asJsonObject) == id) { "百度返回文件 ID 与请求不一致" }
        val reportedPath = BaiduNetdiskPolicy.path(string(rows[0].asJsonObject, "path"))
        if (reportedPath != root && !reportedPath.startsWith("$root/")) throw BinaryFileOutsideRootException()
        return metadata(rows[0].asJsonObject).apply {
            knownFileIds[string(this, "path")] = string(this, "fsId")
            if (dlink) addProperty("_dlink", string(rows[0].asJsonObject, "dlink"))
        }
    }
    private fun metadata(value: JsonObject): JsonObject {
        val target = path(string(value, "path"), true)
        val isdir = integer(value, "isdir"); require(isdir in 0L..1L) { "百度文件类型无效" }
        val size = integer(value, "size"); require(size >= 0) { "百度文件大小无效" }
        val mtime = if (value.has("server_mtime")) integer(value, "server_mtime") else 0L
        require(mtime >= 0) { "百度文件时间无效" }
        val md5 = string(value, "md5"); require(md5.length <= 256 && md5.none { it.code < 32 || it.code == 127 }) { "百度文件摘要无效" }
        return JsonObject().apply { addProperty("path", target); addProperty("name", target.substringAfterLast('/'))
            addProperty("fsId", fsId(value)); addProperty("isDirectory", isdir == 1L); addProperty("bytes", size)
            addProperty("modifiedAt", mtime); addProperty("md5", md5) }
    }
    private fun json(method: String, endpoint: String, query: Map<String, String>, form: Map<String, String>? = null,
        body: RequestBody? = null, cancelled: () -> Boolean = { false }): JsonObject {
        checkCancelled(cancelled)
        val url = endpoint.toHttpUrl().newBuilder().addQueryParameter("access_token", token).apply {
            query.forEach { (key, value) -> addQueryParameter(key, value) }
        }.build()
        val builder = Request.Builder().url(url).header("Accept", "application/json").header("User-Agent", "DevHelper/Android")
        if (method == "POST") builder.post(body ?: FormBody.Builder().apply { form.orEmpty().forEach { (key, value) -> add(key, value) } }.build()) else builder.get()
        try {
            transport.open(builder.build(), cancelled).use { response ->
                require(response.code in 200..299) { "百度网盘请求失败（HTTP ${response.code}），未自动重试" }
                // A 1,000-row page can contain 2,048-byte paths; bound metadata independently of media.
                val bytes = response.input.readBounded(8 * 1024 * 1024)
                val value = try { JsonParser.parseString(bytes.toString(Charsets.UTF_8)).asJsonObject } catch (_: Exception) { error("百度响应格式无效，未自动重试") }
                val code = (value.get("errno") ?: value.get("error_code"))?.let { runCatching { it.asBigDecimal.longValueExact() }.getOrElse { error("百度错误码无效") } } ?: 0L
                if (code != 0L) throw BinaryProviderException(code)
                return value
            }
        } catch (failure: IOException) {
            checkCancelled(cancelled)
            throw IOException("百度网络请求中断，未自动重试；请核对任务及云端状态")
        }
    }
    private fun downloadUrl(raw: String): String {
        val url = runCatching { raw.toHttpUrl() }.getOrElse { error("百度下载地址无效") }
        val host = url.host.lowercase()
        require(url.isHttps && url.port == 443 && url.username.isEmpty() && url.password.isEmpty() &&
            listOf("pcs.baidu.com", "baidupcs.com", "bcebos.com").any { host == it || host.endsWith(".$it") }) { "百度返回了不受支持的下载域名" }
        return url.newBuilder().removeAllQueryParameters("access_token").apply {
            // BOS links are signed; app OAuth tokens must never reach arbitrary buckets.
            if (host != "bcebos.com" && !host.endsWith(".bcebos.com")) addQueryParameter("access_token", token)
        }.fragment(null).build().toString()
    }
    companion object {
        internal const val BLOCK_SIZE = 4 * 1024 * 1024
        private const val MAX_BLOCKS = 10_000
        internal const val IO_SIZE = 64 * 1024
        private const val FILE_API = "https://pan.baidu.com/rest/2.0/xpan/file"
        private const val META_API = "https://pan.baidu.com/rest/2.0/xpan/multimedia"
        private const val NAS_API = "https://pan.baidu.com/rest/2.0/xpan/nas"
        private const val UPLOAD_API = "https://d.pcs.baidu.com/rest/2.0/pcs/superfile2"
        private fun partSize(total: Long, number: Int) = minOf(BLOCK_SIZE.toLong(), maxOf(0L, total - number.toLong() * BLOCK_SIZE)).toInt()
        internal fun string(obj: JsonObject, key: String): String = obj.get(key)?.takeUnless { it.isJsonNull }?.asString ?: ""
        internal fun integer(obj: JsonObject, key: String): Long = runCatching { obj.get(key).asBigDecimal.longValueExact() }.getOrElse { error("百度字段 $key 不是有效整数") }
        internal fun bool(obj: JsonObject, key: String): Boolean = obj.get(key)?.asBoolean ?: false
        private fun fsId(obj: JsonObject): String = string(obj, "fs_id").also {
            require(Regex("[0-9]{1,24}").matches(it) && it.any { c -> c != '0' }) { "百度文件 ID 无效" }
        }
        private fun parent(path: String): String = path.substringBeforeLast('/').ifEmpty { "/" }
        private fun fingerprint(file: File): String {
            val a = Files.readAttributes(file.toPath(), java.nio.file.attribute.BasicFileAttributes::class.java, LinkOption.NOFOLLOW_LINKS)
            require(a.isRegularFile) { "上传源不再是普通文件" }
            return "${a.fileKey()}:${a.size()}:${a.lastModifiedTime()}"
        }
        internal fun sameSnapshot(a: JsonObject, b: JsonObject): Boolean = listOf("path", "fsId", "isDirectory", "bytes", "modifiedAt", "md5").all { a.get(it) == b.get(it) }
        private fun digest(algorithm: String, bytes: ByteArray, count: Int = bytes.size) = hex(MessageDigest.getInstance(algorithm).apply { update(bytes, 0, count) }.digest())
        private fun hex(bytes: ByteArray) = bytes.joinToString("") { "%02x".format(it.toInt() and 255) }
        private fun checkCancelled(cancelled: () -> Boolean) { if (cancelled() || Thread.currentThread().isInterrupted) throw CancellationException("传输已取消，未清理源文件") }
    }
}

/** Byte ranges use a 64 KiB buffer rather than loading media or Base64 in memory. */
internal class FileSliceBody(private val file: File, private val offset: Long, private val length: Long,
    private val progress: (Long) -> Unit, private val cancelled: () -> Boolean) : RequestBody() {
    override fun contentType() = "application/octet-stream".toMediaType()
    override fun contentLength(): Long = length
    override fun isOneShot() = true
    override fun writeTo(sink: BufferedSink) {
        RandomAccessFile(file, "r").use { input ->
            input.seek(offset); var copied = 0L; val buffer = ByteArray(BaiduBinaryFileClient.IO_SIZE)
            while (copied < length) {
                if (cancelled() || Thread.currentThread().isInterrupted) throw CancellationException("传输已取消")
                val read = input.read(buffer, 0, minOf(buffer.size.toLong(), length - copied).toInt())
                require(read > 0) { "上传分片源已改变" }
                sink.write(buffer, 0, read); copied += read; progress(copied)
            }
        }
    }
}
internal class BinaryProviderException(val providerCode: Long) : IllegalStateException("百度网盘错误码 $providerCode；未自动重试")
private class BinaryFileMissingException : IllegalStateException("百度未找到所选文件 ID，请刷新列表")
private class BinaryFileOutsideRootException : IllegalStateException("所选文件 ID 已离开本应用目录，请刷新列表")
internal interface BinaryTransport { fun open(request: Request, cancelled: () -> Boolean): BinaryReply }
internal class BinaryReply(val code: Int, val input: InputStream, private val headers: Map<String, String> = emptyMap(), private val cleanup: () -> Unit = {}) : Closeable {
    fun header(key: String): String? = headers.entries.firstOrNull { it.key.equals(key, true) }?.value
    override fun close() { try { input.close() } finally { cleanup() } }
}
internal class OkHttpBinaryTransport : BinaryTransport {
    private val client = OkHttpClient.Builder().retryOnConnectionFailure(false).followRedirects(false).followSslRedirects(false)
        .connectTimeout(30, TimeUnit.SECONDS).readTimeout(90, TimeUnit.SECONDS).writeTimeout(90, TimeUnit.SECONDS).build()
    override fun open(request: Request, cancelled: () -> Boolean): BinaryReply {
        val call = client.newCall(request); val finished = AtomicBoolean(false)
        val watcher = Thread({
            try { while (!finished.get()) { if (cancelled()) { call.cancel(); break }; Thread.sleep(100) } }
            catch (_: InterruptedException) { /* response closed */ }
        }, "netdisk-cancel").apply { isDaemon = true; start() }
        try {
            val response = call.execute()
            return BinaryReply(response.code, response.body?.byteStream() ?: byteArrayOf().inputStream(),
                response.headers.toMap()) { finished.set(true); watcher.interrupt(); response.close() }
        } catch (failure: Throwable) { finished.set(true); watcher.interrupt(); throw failure }
    }
}
private fun InputStream.readBounded(limit: Int): ByteArray {
    val output = java.io.ByteArrayOutputStream(); val buffer = ByteArray(8192)
    while (true) { val n = read(buffer); if (n < 0) break; require(output.size() + n <= limit) { "百度响应超过合理大小" }; output.write(buffer, 0, n) }
    return output.toByteArray()
}
