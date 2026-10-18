package cn.ololee.devhelper.netdisk

import com.google.gson.JsonArray
import com.google.gson.JsonObject
import com.google.gson.JsonParser
import okhttp3.Request
import okio.Buffer
import org.junit.Assert.*
import org.junit.Test
import java.io.File
import java.io.IOException
import java.io.InputStream
import java.nio.file.Files
import java.security.MessageDigest
import java.util.concurrent.CancellationException

/** Offline protocol fixtures. No account credentials or real cloud requests are used. */
class BaiduBinaryFileClientTest {
    private val root = "/apps/test-fixture"
    private fun config() = JsonObject().apply {
        addProperty("remoteRoot", root); addProperty("accessToken", "private-fixture-token")
        addProperty("appId", "fixture-app"); addProperty("clientId", "fixture-client"); addProperty("thirdId", "fixture-third")
    }
    private class FakeTransport : BinaryTransport {
        val requests = ArrayList<Request>()
        val cancellationChecks = ArrayList<() -> Boolean>()
        fun cancellationRequested(): Boolean {
            // JDK 25 ArrayList.getLast() exposes a callable `last` property to Kotlin;
            // indexing avoids accidentally invoking that property before the assertion.
            val callback: () -> Boolean = cancellationChecks[cancellationChecks.size - 1]
            return callback()
        }
        val steps = ArrayList<(Request) -> BinaryReply>()
        fun json(text: String, inspect: (Request) -> Unit = {}) { steps.add { request -> inspect(request); BinaryReply(200, text.toByteArray().inputStream()) } }
        fun bytes(bytes: ByteArray, inspect: (Request) -> Unit = {}) { steps.add { request -> inspect(request); BinaryReply(200, bytes.inputStream()) } }
        override fun open(request: Request, cancelled: () -> Boolean): BinaryReply {
            if (cancelled()) throw CancellationException("fixture-cancel")
            requests.add(request)
            cancellationChecks.add(cancelled)
            check(steps.isNotEmpty()) { "Unexpected offline request: ${request.url.encodedPath}" }
            return steps.removeAt(0)(request)
        }
    }
    private fun raw(name: String = "sample.bin", size: Long = 3, id: String = "41", isdir: Boolean = false, md5: String = "opaque_provider_digest!", time: Long = 1700000000) = JsonObject().apply {
        addProperty("path", "$root/$name"); addProperty("server_filename", name.substringAfterLast('/'))
        addProperty("fs_id", id); addProperty("size", size); addProperty("isdir", if (isdir) 1 else 0)
        addProperty("server_mtime", time); addProperty("md5", md5)
    }
    private fun normalized(value: JsonObject) = JsonObject().apply {
        addProperty("path", value.get("path").asString); addProperty("name", value.get("server_filename").asString)
        addProperty("fsId", value.get("fs_id").asString); addProperty("bytes", value.get("size").asLong)
        addProperty("isDirectory", value.get("isdir").asInt == 1); addProperty("modifiedAt", value.get("server_mtime").asLong)
        addProperty("md5", value.get("md5").asString)
    }
    private fun meta(value: JsonObject) = JsonObject().apply { addProperty("errno", 0); add("list", JsonArray().apply { add(value) }) }.toString()
    private fun mutationAck(path: String, code: Int = 0, task: String? = null) = JsonObject().apply {
        addProperty("errno", 0); if (task != null) addProperty("taskid", task)
        add("info", JsonArray().apply { add(JsonObject().apply { addProperty("path", path); addProperty("errno", code) }) })
    }.toString()
    private fun listing(vararg values: JsonObject, more: Boolean = false) = JsonObject().apply {
        addProperty("errno", 0); addProperty("has_more", more); add("list", JsonArray().apply { values.forEach { add(it) } })
    }.toString()
    private fun withTemp(block: (File) -> Unit) { val dir = Files.createTempDirectory("devhelper-binary-fixture-").toFile(); try { block(dir) } finally { dir.deleteRecursively() } }
    private fun body(request: Request): String = Buffer().also { request.body!!.writeTo(it) }.readUtf8()
    private fun hash(name: String, bytes: ByteArray) = MessageDigest.getInstance(name).digest(bytes).joinToString("") { "%02x".format(it.toInt() and 255) }
    private inline fun <reified T: Throwable> rejected(block: () -> Unit): T { try { block() } catch (error: Throwable) { if (error is T) return error; throw error }; throw AssertionError("Expected ${T::class.java.simpleName}") }

    @Test fun scopesRejectRootSourcesAndOutsidePathsBeforeNetwork() {
        val transport = FakeTransport(); val client = BaiduBinaryFileClient(config(), transport)
        rejected<IllegalArgumentException> { client.list("/apps/other") }
        rejected<IllegalArgumentException> { client.mkdir(root) }
        rejected<IllegalArgumentException> { BaiduBinaryFileClient(config().apply { addProperty("remoteRoot", "/") }, transport) }
        assertTrue(transport.requests.isEmpty())
    }
    @Test fun accountUsesActualAuthorizedUserAndDoesNotReturnToken() {
        val transport = FakeTransport().apply { json("{\"errno\":0,\"uk\":12345678}") }
        val client = BaiduBinaryFileClient(config(), transport)
        val account = client.account(); assertEquals(64, account.length); assertFalse(account.contains("private-fixture-token"))
        assertEquals(account, client.account()); assertEquals(1, transport.requests.size)
        val different = FakeTransport().apply { json("{\"errno\":0,\"uk\":87654321}") }
        assertNotEquals(account, BaiduBinaryFileClient(config(), different).account())
    }
    @Test fun listsEveryPageAndPreservesOpaqueProviderMd5() {
        val transport = FakeTransport().apply {
            json(listing(raw("one.bin"), more = true)) { assertEquals("0", it.url.queryParameter("start")) }
            json(listing(raw("two.bin", id = "42"))) { assertEquals("1000", it.url.queryParameter("start")) }
        }
        val rows = BaiduBinaryFileClient(config(), transport).list(root)
        assertEquals(2, rows.size); assertEquals("opaque_provider_digest!", rows[0].get("md5").asString)
    }
    @Test fun knownSiblingStatsReadFreshMetadataWithoutRepeatingDirectoryPages() {
        val first = raw("one.bin"); val second = raw("two.bin", id = "42")
        val changedFirst = raw("one.bin", time = 1700000001)
        val t = FakeTransport().apply {
            json(listing(first, more = true)); json(listing(second))
            json(meta(first)); json(meta(second)); json(meta(changedFirst))
        }
        val client = BaiduBinaryFileClient(config(), t)
        client.list(root)
        assertEquals("41", client.stat("$root/one.bin")!!.get("fsId").asString)
        assertEquals("42", client.stat("$root/two.bin")!!.get("fsId").asString)
        assertEquals(1700000001L, client.stat("$root/one.bin")!!.get("modifiedAt").asLong)
        assertEquals(2, t.requests.count { it.url.queryParameter("method") == "list" })
        assertEquals(3, t.requests.count { it.url.queryParameter("method") == "filemetas" })
        assertTrue(t.steps.isEmpty())
    }
    @Test fun firstStatAlsoRemembersSiblingIdsButNotTheirMetadata() {
        val first = raw("one.bin"); val second = raw("two.bin", id = "42")
        val t = FakeTransport().apply { json(listing(first, second)); json(meta(first)); json(meta(second)) }
        val client = BaiduBinaryFileClient(config(), t)
        client.stat("$root/one.bin"); client.stat("$root/two.bin")
        assertEquals(1, t.requests.count { it.url.queryParameter("method") == "list" })
        assertEquals(2, t.requests.count { it.url.queryParameter("method") == "filemetas" })
    }
    @Test fun missingCachedIdReListsParentAndUsesReplacementId() {
        val original = raw(); val replacement = raw(id = "42", time = 1700000001)
        val t = FakeTransport().apply {
            json(listing(original)); json("{\"errno\":-9}")
            json(listing(replacement)); json(meta(replacement)); json(meta(replacement))
        }
        val client = BaiduBinaryFileClient(config(), t); client.list(root)
        assertEquals("42", client.stat("$root/sample.bin")!!.get("fsId").asString)
        assertEquals("42", client.stat("$root/sample.bin")!!.get("fsId").asString)
        assertEquals(listOf("[41]", "[42]", "[42]"), t.requests.filter { it.url.queryParameter("method") == "filemetas" }.map { it.url.queryParameter("fsids") })
        assertEquals(2, t.requests.count { it.url.queryParameter("method") == "list" })
    }
    @Test fun emptyMetadataEvictsOldIdAndAbsenceIsNeverNegativelyCached() {
        val replacement = raw(id = "42")
        val t = FakeTransport().apply {
            json(listing(raw())); json("{\"errno\":0,\"list\":[]}"); json(listing())
            json(listing(replacement)); json(meta(replacement))
        }
        val client = BaiduBinaryFileClient(config(), t); client.list(root)
        assertNull(client.stat("$root/sample.bin"))
        assertEquals("42", client.stat("$root/sample.bin")!!.get("fsId").asString)
        assertEquals(3, t.requests.count { it.url.queryParameter("method") == "list" })
    }
    @Test fun movedCachedIdCannotMasqueradeAsTheOriginalPath() {
        val moved = raw("moved.bin"); val replacement = raw(id = "42")
        val t = FakeTransport().apply {
            json(listing(raw())); json(meta(moved)); json(listing(moved, replacement)); json(meta(replacement))
        }
        val client = BaiduBinaryFileClient(config(), t); client.list(root)
        val result = client.stat("$root/sample.bin")!!
        assertEquals("$root/sample.bin", result.get("path").asString); assertEquals("42", result.get("fsId").asString)
    }
    @Test fun cachedMetadataPermissionOrNetworkFailuresAreNotTreatedAsMissing() {
        val t = FakeTransport().apply { json(listing(raw())); json("{\"errno\":-6}") }
        val client = BaiduBinaryFileClient(config(), t); client.list(root)
        rejected<BinaryProviderException> { client.stat("$root/sample.bin") }
        assertEquals(1, t.requests.count { it.url.queryParameter("method") == "list" })
        val network = FakeTransport().apply { json(listing(raw())); steps.add { throw IOException("fixture") } }
        val other = BaiduBinaryFileClient(config(), network); other.list(root)
        rejected<IOException> { other.stat("$root/sample.bin") }
        assertEquals(2, network.requests.size)
    }
    @Test fun cachedStatPassesLiveCancellationToTheMetadataRequestAndDoesNotFallBack() {
        var cancel = false
        val t = FakeTransport().apply {
            json(listing(raw()))
            steps.add { request ->
                assertEquals("filemetas", request.url.queryParameter("method"))
                cancel = true; assertTrue(cancellationRequested())
                throw IOException("cancelled metadata request fixture")
            }
        }
        val client = BaiduBinaryFileClient(config(), t); client.list(root)
        rejected<CancellationException> { client.stat("$root/sample.bin", { cancel }) }
        assertEquals(2, t.requests.size); assertEquals(1, t.requests.count { it.url.queryParameter("method") == "list" })
    }
    @Test fun uncachedStatPassesCancellationThroughParentListAndFreshFileInfo() {
        var cancel = false
        val t = FakeTransport().apply {
            json(listing(raw()))
            steps.add { request ->
                assertEquals("filemetas", request.url.queryParameter("method"))
                cancel = true; assertTrue(cancellationRequested()); throw IOException("cancelled fresh metadata")
            }
        }
        rejected<CancellationException> { BaiduBinaryFileClient(config(), t).stat("$root/sample.bin", { cancel }) }
        assertEquals(2, t.requests.size)
        val listingCancelled = FakeTransport().apply { steps.add { request ->
            assertEquals("list", request.url.queryParameter("method")); cancel = true
            assertTrue(cancellationRequested()); throw IOException("cancelled list")
        } }
        cancel = false
        rejected<CancellationException> { BaiduBinaryFileClient(config(), listingCancelled).stat("$root/sample.bin", { cancel }) }
        assertEquals(1, listingCancelled.requests.size)
    }
    @Test fun directoryPaginationCancellationStopsBeforeTheNextPageAndClosesTheResponse() {
        var cancel = false; var closed = false
        val t = FakeTransport().apply {
            steps.add { BinaryReply(200, listing(raw("first.bin"), more = true).toByteArray().inputStream(), cleanup = { closed = true; cancel = true }) }
            json(listing(raw("second.bin", id = "42")))
        }
        rejected<CancellationException> { BaiduBinaryFileClient(config(), t).list(root, { cancel }) }
        assertEquals(1, t.requests.size); assertEquals(1, t.steps.size); assertTrue(closed)
    }
    @Test fun metadataBodyCancellationClosesTheBodyAndNeverReturnsPartialFacts() {
        var cancel = false; var closed = false
        val bytes = listing(raw()).toByteArray()
        val t = FakeTransport().apply { steps.add {
            BinaryReply(200, object : InputStream() {
                private var sent = false
                override fun read(): Int = throw AssertionError("Use bounded reads")
                override fun read(buffer: ByteArray, offset: Int, length: Int): Int {
                    if (sent) return -1
                    bytes.copyInto(buffer, offset); sent = true; cancel = true; return bytes.size
                }
                override fun close() { closed = true }
            })
        } }
        rejected<CancellationException> { BaiduBinaryFileClient(config(), t).stat("$root/sample.bin", { cancel }) }
        assertEquals(1, t.requests.size); assertTrue(closed)
    }
    @Test fun rootStatAndRemovedIdFallbackPreservePreparationCancellation() {
        var cancel = false
        val rootTransport = FakeTransport().apply { steps.add {
            cancel = true; assertTrue(cancellationRequested()); throw IOException("cancelled root listing")
        } }
        rejected<CancellationException> { BaiduBinaryFileClient(config(), rootTransport).stat(root, { cancel }) }
        cancel = false
        val fallback = FakeTransport().apply {
            json(listing(raw())); json("{\"errno\":-9}")
            steps.add { request ->
                assertEquals("list", request.url.queryParameter("method")); cancel = true
                assertTrue(cancellationRequested()); throw IOException("cancelled replacement search")
            }
        }
        val client = BaiduBinaryFileClient(config(), fallback); client.list(root)
        rejected<CancellationException> { client.stat("$root/sample.bin", { cancel }) }
        assertEquals(3, fallback.requests.size)
    }
    @Test fun cancelledStatAndListNeverBeginANetworkRequest() {
        val t = FakeTransport(); val client = BaiduBinaryFileClient(config(), t)
        rejected<CancellationException> { client.stat("$root/sample.bin", { true }) }
        rejected<CancellationException> { client.list(root, { true }) }
        assertTrue(t.requests.isEmpty())
    }
    @Test fun cachedIdMovedOutsideRootIsEvictedWithoutBrowsingOutsideTheApplication() {
        val moved = raw().apply { addProperty("path", "/apps/another-app/moved.bin") }
        val replacement = raw(id = "42")
        val t = FakeTransport().apply {
            json(listing(raw())); json(meta(moved)); json(listing(replacement)); json(meta(replacement))
        }
        val client = BaiduBinaryFileClient(config(), t); client.list(root)
        assertEquals("42", client.stat("$root/sample.bin")!!.get("fsId").asString)
        assertTrue(t.requests.filter { it.url.queryParameter("method") == "list" }.all { it.url.queryParameter("dir") == root })
    }
    @Test fun rapidUploadAndMkdirReceiptsPrimePositiveIdCache() = withTemp { dir ->
        val file = File(dir, "fixture").apply { writeText("abc") }; val record = raw()
        val t = FakeTransport().apply {
            json(listing()); json(record.deepCopy().apply { addProperty("return_type", 2) }.toString())
            json(meta(record)); json(meta(record))
        }
        val client = BaiduBinaryFileClient(config(), t)
        client.upload(file, "$root/sample.bin", {}, { false }); client.stat("$root/sample.bin")
        assertEquals(1, t.requests.count { it.url.queryParameter("method") == "list" })
        val folder = raw("folder", size = 0, isdir = true)
        val mkdirTransport = FakeTransport().apply { json(listing()); json(folder.toString()); json(meta(folder)); json(meta(folder)) }
        val folderClient = BaiduBinaryFileClient(config(), mkdirTransport)
        folderClient.mkdir("$root/folder"); folderClient.stat("$root/folder")
        assertEquals(1, mkdirTransport.requests.count { it.url.queryParameter("method") == "list" })
    }
    @Test fun postDeleteAlwaysReadsFreshParentAndRecognizesAReplacementAtTheSamePath() {
        val original = raw(); val replacement = raw(id = "42")
        val t = FakeTransport().apply {
            json(listing(original)); json(meta(original))
            json("{\"errno\":0,\"info\":[{\"path\":\"$root/sample.bin\",\"errno\":0}]}")
            json(listing(replacement)); json(meta(replacement)); json(meta(replacement))
        }
        val client = BaiduBinaryFileClient(config(), t); client.list(root)
        val deleted = client.delete(normalized(original))
        assertFalse(deleted.get("verified").asBoolean); assertEquals("unknown", deleted.get("state").asString)
        assertEquals("42", client.stat("$root/sample.bin")!!.get("fsId").asString)
        assertEquals(2, t.requests.count { it.url.queryParameter("method") == "list" })
        assertTrue(t.steps.isEmpty())
    }
    @Test fun ambiguousDeleteInvalidatesOldPositiveIdsWithoutReplayingTheMutation() {
        val original = raw()
        val t = FakeTransport().apply {
            json(listing(original)); json(meta(original)); steps.add { throw IOException("fixture") }; json(listing())
        }
        val client = BaiduBinaryFileClient(config(), t); client.list(root)
        assertFalse(client.delete(normalized(original)).get("verified").asBoolean)
        assertNull(client.stat("$root/sample.bin"))
        assertEquals(1, t.requests.count { it.url.queryParameter("method") == "filemanager" })
        assertEquals(2, t.requests.count { it.url.queryParameter("method") == "list" })
        assertTrue(t.steps.isEmpty())
    }
    @Test fun duplicateAndNonDirectChildrenStopIncompleteListing() {
        for (reply in listOf(listing(raw(), raw()), listing(raw("nested/a.bin")))) {
            val t = FakeTransport().apply { json(reply) }
            rejected<IllegalArgumentException> { BaiduBinaryFileClient(config(), t).list(root) }
        }
    }
    @Test fun statAbsentOnlyWhenCompleteParentListConfirmsNoMatch() {
        val t = FakeTransport().apply { json(listing()) }
        assertNull(BaiduBinaryFileClient(config(), t).stat("$root/missing.bin"))
        val failed = FakeTransport().apply { json("{\"errno\":-6}") }
        rejected<BinaryProviderException> { BaiduBinaryFileClient(config(), failed).stat("$root/missing.bin") }
    }
    @Test fun existingUploadTargetFailsWithoutMutation() = withTemp { dir ->
        val source = File(dir, "x.bin").apply { writeBytes(byteArrayOf(1, 2, 3)) }
        val t = FakeTransport().apply { json(listing(raw())); json(meta(raw())) }
        rejected<IllegalArgumentException> { BaiduBinaryFileClient(config(), t).upload(source, "$root/sample.bin", {}, { false }) }
        assertEquals(2, t.requests.size); assertTrue(t.requests.all { it.method == "GET" })
    }
    @Test fun uploadStreamsExactBinaryChecksPartsAndUsesFailOnConflictForBothMutations() = withTemp { dir ->
        val bytes = ByteArray(150_123) { (it % 253).toByte() }; val source = File(dir, "x.bin").apply { writeBytes(bytes) }
        val original = raw(size = bytes.size.toLong()); val progress = ArrayList<Long>()
        val t = FakeTransport().apply {
            json(listing())
            json("{\"errno\":0,\"uploadid\":\"fixture-upload\",\"return_type\":1,\"block_list\":[0]}") {
                val form = body(it); assertTrue(form.contains("rtype=0")); assertTrue(form.contains("block_list=")); assertFalse(form.contains("base64"))
            }
            json("{\"md5\":\"${hash("MD5", bytes)}\"}") {
                assertEquals("upload", it.url.queryParameter("method"))
                val buffer = Buffer(); it.body!!.writeTo(buffer); val multipart = buffer.readByteArray()
                val first = multipart.asList().windowed(4).indexOfFirst { part -> part == listOf<Byte>(13,10,13,10) } + 4
                assertArrayEquals(bytes, multipart.copyOfRange(first, first + bytes.size))
                assertTrue(it.body!!.contentType().toString().startsWith("multipart/form-data; boundary="))
            }
            json(original.toString()) { val form = body(it); assertTrue(form.contains("rtype=0")); assertFalse(form.contains("autoinit")) }
            json(meta(original))
        }
        val result = BaiduBinaryFileClient(config(), t).upload(source, "$root/sample.bin", { progress.add(it) }, { false })
        assertEquals(hash("SHA-256", bytes), result.get("sha256").asString)
        assertEquals("41", result.get("fsId").asString); assertEquals(1700000000L, result.get("modifiedAt").asLong)
        assertEquals("opaque_provider_digest!", result.get("md5").asString)
        assertEquals(bytes.size.toLong(), progress.last()); assertTrue(progress.zipWithNext().all { it.second >= it.first })
        assertTrue(progress.size >= 4); assertTrue(t.steps.isEmpty())
    }
    @Test fun badPartHashNeverCreatesCloudFile() = withTemp { dir ->
        val source = File(dir, "x.bin").apply { writeText("abc") }
        val t = FakeTransport().apply { json(listing()); json("{\"uploadid\":\"fixture\",\"block_list\":[0]}"); json("{\"md5\":\"incorrect\"}") }
        rejected<IllegalArgumentException> { BaiduBinaryFileClient(config(), t).upload(source, "$root/sample.bin", {}, { false }) }
        assertFalse(t.requests.any { it.url.queryParameter("method") == "create" })
    }
    @Test fun duplicateMissingPartsAndProviderRenameAreRejectedBeforeParts() = withTemp { dir ->
        val source = File(dir, "x.bin").apply { writeText("abc") }
        for (pre in listOf("{\"uploadid\":\"fixture\",\"block_list\":[0,0]}", "{\"uploadid\":\"fixture\",\"path\":\"$root/renamed.bin\"}")) {
            val t = FakeTransport().apply { json(listing()); json(pre) }
            rejected<IllegalArgumentException> { BaiduBinaryFileClient(config(), t).upload(source, "$root/sample.bin", {}, { false }) }
            assertEquals(2, t.requests.size)
        }
    }
    @Test fun rapidUploadRequiresExactTargetMetadataAndChecksActualServerTime() = withTemp { dir ->
        val source = File(dir, "x.bin").apply { writeText("abc") }; val record = raw()
        val t = FakeTransport().apply {
            json(listing()); json("{\"return_type\":2}"); json(listing(record)); json(meta(record))
        }
        val result = BaiduBinaryFileClient(config(), t).upload(source, "$root/sample.bin", {}, { false })
        assertEquals(1700000000L, result.get("modifiedAt").asLong); assertTrue(result.get("verified").asBoolean)
        assertTrue(t.requests.none { it.url.queryParameter("method") == "create" })
    }
    @Test fun ambiguousPrecreateIsNeverRetriedAndErrorDoesNotLeakToken() = withTemp { dir ->
        val source = File(dir, "x.bin").apply { writeText("abc") }
        val t = FakeTransport().apply { json(listing()); steps.add { throw IOException("https://bad.invalid/?access_token=private-fixture-token") } }
        val error = rejected<IOException> { BaiduBinaryFileClient(config(), t).upload(source, "$root/sample.bin", {}, { false }) }
        assertFalse(error.message!!.contains("private-fixture-token")); assertEquals(2, t.requests.size)
    }
    @Test fun downloadUsesBinaryAndOriginalShaNotOpaqueProviderMd5() = withTemp { dir ->
        val bytes = byteArrayOf(0, -1, 42); val raw = raw().apply { addProperty("dlink", "https://fixture.baidupcs.com/download?keep=yes") }
        val meta = normalized(raw).apply { addProperty("sha256", hash("SHA-256", bytes)) }; val progress = ArrayList<Long>()
        val t = FakeTransport().apply {
            json(meta(raw)); bytes(bytes) {
                assertEquals("private-fixture-token", it.url.queryParameter("access_token")); assertEquals("identity", it.header("Accept-Encoding"))
            }; json(meta(raw))
        }
        val output = File(dir, "received.bin")
        val result = BaiduBinaryFileClient(config(), t).download(meta, output, { progress.add(it) }, { false })
        assertArrayEquals(bytes, output.readBytes()); assertEquals(hash("SHA-256", bytes), result.get("sha256").asString)
        assertEquals("opaque_provider_digest!", result.get("remoteMd5").asString); assertTrue(result.get("verifiedAgainstManifest").asBoolean)
        assertEquals(3L, progress.last())
    }
    @Test fun downloadRejectsChangedSourceAndNeverTouchesExistingDestination() = withTemp { dir ->
        val before = raw(); val changed = raw(time = 1700000001).apply { addProperty("dlink", "https://fixture.baidupcs.com/d") }
        val t = FakeTransport().apply { json(meta(changed)) }
        val out = File(dir, "keep.bin").apply { writeText("original") }
        rejected<IllegalArgumentException> { BaiduBinaryFileClient(config(), t).download(normalized(before), out, {}, { false }) }
        assertEquals("original", out.readText()); assertEquals(1, t.requests.size)
    }
    @Test fun truncatedOversizedOrWrongShaDownloadsRemoveOnlyNewIncompleteFile() = withTemp { dir ->
        for (bytes in listOf(byteArrayOf(1), byteArrayOf(1,2,3,4), byteArrayOf(1,2,3))) {
            val raw = raw().apply { addProperty("dlink", "https://fixture.baidupcs.com/d") }
            val selected = normalized(raw).apply { addProperty("sha256", "0".repeat(64)) }
            val t = FakeTransport().apply { json(meta(raw)); bytes(bytes) }
            val out = File(dir, "failed-${bytes.size}.bin")
            rejected<IllegalArgumentException> { BaiduBinaryFileClient(config(), t).download(selected, out, {}, { false }) }
            assertFalse(out.exists())
        }
    }
    @Test fun untrustedDownloadHostIsRejectedBeforeSendingToken() = withTemp { dir ->
        val raw = raw().apply { addProperty("dlink", "https://evil.example/steal") }
        val t = FakeTransport().apply { json(meta(raw)) }
        rejected<IllegalArgumentException> { BaiduBinaryFileClient(config(), t).download(normalized(raw), File(dir,"out"), {}, { false }) }
        assertEquals(1, t.requests.size)
    }
    @Test fun bosLinksNeverReceiveOAuthTokenAndRedirectsAreScoped() = withTemp { dir ->
        val raw = raw().apply { addProperty("dlink", "https://fixture.bcebos.com/signed?access_token=bad&signature=yes") }
        val t = FakeTransport().apply {
            json(meta(raw)); bytes(byteArrayOf(0,1,2)) { assertNull(it.url.queryParameter("access_token")); assertEquals("yes", it.url.queryParameter("signature")) }; json(meta(raw))
        }
        BaiduBinaryFileClient(config(), t).download(normalized(raw), File(dir,"out"), {}, { false })
        val unsafe = FakeTransport().apply { json(meta(raw)); steps.add { BinaryReply(302, byteArrayOf().inputStream(), mapOf("Location" to "https://evil.example/d")) } }
        rejected<IllegalArgumentException> { BaiduBinaryFileClient(config(), unsafe).download(normalized(raw), File(dir,"other"), {}, { false }) }
        assertEquals(2, unsafe.requests.size)
    }
    @Test fun cancellationStopsFileSliceBeforeNextChunkWithoutEncoding() = withTemp { dir ->
        val source = File(dir,"large.bin").apply { writeBytes(ByteArray(250_000) { it.toByte() }) }
        var cancel = false; val progress = ArrayList<Long>(); val body = FileSliceBody(source, 3, 200_000, { progress.add(it); cancel = true }, { cancel })
        val sink = Buffer(); rejected<CancellationException> { body.writeTo(sink) }
        assertEquals(65_536L, sink.size); assertEquals(listOf(65_536L), progress); assertTrue(body.isOneShot())
        assertArrayEquals(source.readBytes().copyOfRange(3, 65_539), sink.readByteArray())
    }
    @Test fun redirectsKeepPcsAuthorizationAndRemoveItWhenEnteringBos() = withTemp { dir ->
        val raw = raw().apply { addProperty("dlink", "https://fixture.baidupcs.com/start") }
        val t = FakeTransport().apply {
            json(meta(raw))
            steps.add { BinaryReply(302, byteArrayOf().inputStream(), mapOf("Location" to "https://other.pcs.baidu.com/next")) }
            steps.add { request ->
                assertEquals("private-fixture-token", request.url.queryParameter("access_token"))
                BinaryReply(302, byteArrayOf().inputStream(), mapOf("Location" to "https://fixture.bcebos.com/final?signature=yes&access_token=must-remove"))
            }
            bytes(byteArrayOf(1, 2, 3)) { assertNull(it.url.queryParameter("access_token")); assertEquals("yes", it.url.queryParameter("signature")) }
            json(meta(raw))
        }
        BaiduBinaryFileClient(config(), t).download(normalized(raw), File(dir, "redirected"), {}, { false })
        assertTrue(t.steps.isEmpty())
    }
    @Test fun cancellationDuringDownloadDeletesPartialAndKeepsCloudSource() = withTemp { dir ->
        val raw = raw(size = 150_000).apply { addProperty("dlink", "https://fixture.baidupcs.com/d") }; var cancel = false
        val t = FakeTransport().apply { json(meta(raw)); bytes(ByteArray(150_000)) }
        val out = File(dir,"cancelled.bin")
        rejected<CancellationException> { BaiduBinaryFileClient(config(), t).download(normalized(raw), out, { cancel = true }, { cancel }) }
        assertFalse(out.exists()); assertFalse(t.requests.any { it.url.queryParameter("opera") == "delete" })
    }
    @Test fun deleteRequiresOriginalSnapshotMatchingAndPositivePerItemReceipt() {
        val raw = raw(); val t = FakeTransport().apply {
            json(meta(raw)); json("{\"errno\":0,\"info\":[{\"path\":\"$root/sample.bin\",\"errno\":0}]}") {
                assertTrue(body(it).contains("async=0")); assertTrue(body(it).contains("ondup=fail"))
            }; json(listing())
        }
        val result = BaiduBinaryFileClient(config(), t).delete(normalized(raw))
        assertTrue(result.get("deleted").asBoolean); assertTrue(result.get("verified").asBoolean)
        val changed = FakeTransport().apply { json(meta(raw(time = 1700000001))) }
        rejected<IllegalArgumentException> { BaiduBinaryFileClient(config(), changed).delete(normalized(raw)) }
        assertEquals(1, changed.requests.size)
    }
    @Test fun renameUsesOneFailOnConflictMutationAndConfirmsExactPathIdAndDigest() {
        val original = raw(); val renamed = raw("renamed.bin", time = 1700000001)
        val t = FakeTransport().apply {
            json(meta(original)); json(listing(original)); json(meta(original))
            json(mutationAck("$root/sample.bin")) { request ->
                assertEquals("rename", request.url.queryParameter("opera")); assertEquals("POST", request.method)
                val form = request.body as okhttp3.FormBody
                val fields = (0 until form.size).associate { form.name(it) to form.value(it) }
                assertEquals("0", fields["async"]); assertEquals("fail", fields["ondup"])
                val items = JsonParser.parseString(fields["filelist"]).asJsonArray
                assertEquals(1, items.size()); assertEquals("$root/sample.bin", items[0].asJsonObject.get("path").asString)
                assertEquals("renamed.bin", items[0].asJsonObject.get("newname").asString)
            }
            json(listing(renamed)); json(meta(renamed)); json(meta(renamed))
        }
        val client = BaiduBinaryFileClient(config(), t)
        val result = client.rename(normalized(original), "renamed.bin")
        assertEquals("$root/renamed.bin", result.get("path").asString); assertEquals("41", result.get("fsId").asString)
        assertTrue(result.get("renamed").asBoolean); assertTrue(result.get("verified").asBoolean)
        assertFalse(result.get("unchanged").asBoolean); assertEquals("$root/sample.bin", result.get("originalPath").asString)
        assertEquals("opaque_provider_digest!", result.get("md5").asString)
        // Positive target cache is updated, but stat still reads live metadata.
        assertEquals("41", client.stat("$root/renamed.bin")!!.get("fsId").asString)
        assertEquals(2, t.requests.count { it.url.queryParameter("method") == "list" })
        assertEquals(1, t.requests.count { it.url.queryParameter("method") == "filemanager" })
        assertTrue(t.steps.isEmpty())
    }
    @Test fun unchangedRenameChecksTheLiveSnapshotAndNeverMutates() {
        val original = raw(); val t = FakeTransport().apply { json(meta(original)) }
        val result = BaiduBinaryFileClient(config(), t).rename(normalized(original), "sample.bin")
        assertTrue(result.get("unchanged").asBoolean); assertFalse(result.get("renamed").asBoolean)
        assertTrue(result.get("verified").asBoolean); assertEquals("$root/sample.bin", result.get("path").asString)
        assertEquals(1, t.requests.size); assertEquals("GET", t.requests[0].method)
        val changed = FakeTransport().apply { json(meta(raw(time = 1700000001))) }
        rejected<IllegalArgumentException> { BaiduBinaryFileClient(config(), changed).rename(normalized(original), "sample.bin") }
    }
    @Test fun renameRejectsRootTraversalAndInvalidNamesBeforeAnyNetworkRequest() {
        val t = FakeTransport(); val client = BaiduBinaryFileClient(config(), t)
        for (name in listOf("", " ", ".", "..", "../elsewhere", "nested/name", "a\\b", "line\nbreak", "\u0000", "x".repeat(256))) {
            rejected<IllegalArgumentException> { client.rename(normalized(raw()), name) }
        }
        rejected<IllegalArgumentException> { client.rename(normalized(raw()).apply { addProperty("path", root) }, "other") }
        rejected<IllegalArgumentException> { client.rename(normalized(raw()).apply { addProperty("path", "/apps/other/file") }, "other") }
        assertTrue(t.requests.isEmpty())
    }
    @Test fun renameCollisionNeverSendsTheMutationEvenIfSourceWasCached() {
        val original = raw(); val occupied = raw("taken.bin", id = "42")
        val t = FakeTransport().apply { json(listing(original)); json(meta(original)); json(listing(original, occupied)); json(meta(occupied)) }
        val client = BaiduBinaryFileClient(config(), t); client.list(root)
        rejected<IllegalArgumentException> { client.rename(normalized(original), "taken.bin") }
        assertTrue(t.requests.all { it.method == "GET" }); assertEquals(2, t.requests.count { it.url.queryParameter("method") == "list" })
    }
    @Test fun renameSourceMustStillMatchAfterDestinationInspectionIncludingDirectoryMtime() {
        for (isdir in listOf(false, true)) {
            val original = raw(isdir = isdir)
            val t = FakeTransport().apply { json(meta(original)); json(listing(original)); json(meta(raw(isdir = isdir, time = 1700000001))) }
            rejected<IllegalArgumentException> { BaiduBinaryFileClient(config(), t).rename(normalized(original), "renamed") }
            assertTrue(t.requests.all { it.method == "GET" })
        }
    }
    @Test fun ambiguousRenameIsNotReplayedAndInvalidatesSourceAndTargetCaches() {
        val original = raw(); val renamed = raw("renamed.bin")
        val t = FakeTransport().apply {
            json(listing(original)); json(meta(original)); json(listing(original)); json(meta(original))
            steps.add { throw IOException("secret=private-fixture-token") }
            json(listing(renamed)); json(meta(renamed))
        }
        val client = BaiduBinaryFileClient(config(), t); client.list(root)
        val error = rejected<IllegalStateException> { client.rename(normalized(original), "renamed.bin") }
        assertFalse(error.message!!.contains("private-fixture-token"))
        assertEquals("41", client.stat("$root/renamed.bin")!!.get("fsId").asString)
        assertEquals(1, t.requests.count { it.url.queryParameter("method") == "filemanager" })
        assertEquals(3, t.requests.count { it.url.queryParameter("method") == "list" }); assertTrue(t.steps.isEmpty())
    }
    @Test fun missingDuplicateOrNegativeRenameAcknowledgementNeverReportsSuccess() {
        val original = raw()
        val badResponses = listOf("{\"errno\":0}", mutationAck("$root/sample.bin", -8),
            "{\"errno\":0,\"info\":[{\"path\":\"$root/sample.bin\",\"errno\":0},{\"path\":\"$root/sample.bin\",\"errno\":0}]}",
            mutationAck("$root/another.bin"))
        for (response in badResponses) {
            val t = FakeTransport().apply { json(meta(original)); json(listing(original)); json(meta(original)); json(response) }
            rejected<IllegalStateException> { BaiduBinaryFileClient(config(), t).rename(normalized(original), "renamed.bin") }
            assertEquals(1, t.requests.count { it.url.queryParameter("method") == "filemanager" })
        }
    }
    @Test fun renameRequiresOldPathAbsentAndTargetIdentityUnchanged() {
        val original = raw(); val renamed = raw("renamed.bin")
        for (after in listOf(listing(original, renamed), listing(raw("renamed.bin", id = "99")), listing())) {
            val t = FakeTransport().apply {
                json(meta(original)); json(listing(original)); json(meta(original)); json(mutationAck("$root/sample.bin")); json(after)
            }
            rejected<IllegalStateException> { BaiduBinaryFileClient(config(), t).rename(normalized(original), "renamed.bin") }
            assertEquals(1, t.requests.count { it.url.queryParameter("method") == "filemanager" })
        }
    }
    @Test fun changedRenameDigestSizeOrFreshMetadataPathRemainsUnconfirmed() {
        val original = raw(); val renamed = raw("renamed.bin")
        val altered = listOf(raw("renamed.bin", size = 4), raw("renamed.bin", md5 = "changed-opaque-digest"), raw("moved-again.bin"), raw("renamed.bin", isdir = true))
        for (after in altered) {
            val t = FakeTransport().apply {
                json(meta(original)); json(listing(original)); json(meta(original)); json(mutationAck("$root/sample.bin"))
                json(listing(renamed)); json(meta(after))
            }
            rejected<IllegalStateException> { BaiduBinaryFileClient(config(), t).rename(normalized(original), "renamed.bin") }
            assertEquals(1, t.requests.count { it.url.queryParameter("method") == "filemanager" })
        }
    }
    @Test fun synchronousDirectoryRenameRequiresStableSnapshotAndReturnsNewMetadata() {
        val original = raw("folder", size = 0, isdir = true); val renamed = raw("new-folder", size = 0, isdir = true)
        val t = FakeTransport().apply {
            json(meta(original)); json(listing(original)); json(meta(original)); json(mutationAck("$root/folder"))
            json(listing(renamed)); json(meta(renamed))
        }
        val result = BaiduBinaryFileClient(config(), t).rename(normalized(original), "new-folder")
        assertTrue(result.get("renamed").asBoolean); assertTrue(result.get("isDirectory").asBoolean)
        assertEquals("$root/new-folder", result.get("path").asString)
    }
    @Test fun changedDirectoryRenameMtimeAndAsyncDirectoryTaskRemainUnconfirmed() {
        val original = raw("folder", size = 0, isdir = true); val renamed = raw("new-folder", size = 0, isdir = true, time = 1700000001)
        val changed = FakeTransport().apply {
            json(meta(original)); json(listing(original)); json(meta(original)); json(mutationAck("$root/folder"))
            json(listing(renamed)); json(meta(renamed))
        }
        rejected<IllegalStateException> { BaiduBinaryFileClient(config(), changed).rename(normalized(original), "new-folder") }
        val asynchronous = FakeTransport().apply {
            json(meta(original)); json(listing(original)); json(meta(original)); json(mutationAck("$root/folder", task = "123"))
        }
        rejected<IllegalStateException> { BaiduBinaryFileClient(config(), asynchronous).rename(normalized(original), "new-folder") }
        assertEquals(1, asynchronous.requests.count { it.url.queryParameter("method") == "filemanager" })
    }
    @Test fun fileRenameWithoutAProviderDigestCannotSilentlyIgnoreMtimeChanges() {
        val original = raw(md5 = ""); val renamed = raw("renamed.bin", md5 = "", time = 1700000001)
        val t = FakeTransport().apply {
            json(meta(original)); json(listing(original)); json(meta(original)); json(mutationAck("$root/sample.bin"))
            json(listing(renamed)); json(meta(renamed))
        }
        rejected<IllegalStateException> { BaiduBinaryFileClient(config(), t).rename(normalized(original), "renamed.bin") }
    }
    @Test fun previewPrefixWith200ReadsOnlyBoundedBytesAndClosesUnreadTail() {
        val original = raw("large.log", size = 100L * 1024 * 1024).apply { addProperty("dlink", "https://fixture.baidupcs.com/log") }
        var consumed = 0; var closed = false; var largestRead = 0
        val t = FakeTransport().apply {
            json(meta(original))
            steps.add { request ->
                assertEquals("bytes=0-262143", request.header("Range")); assertEquals("identity", request.header("Accept-Encoding"))
                BinaryReply(200, object : InputStream() {
                    override fun read(): Int { throw AssertionError("Use bounded binary reads") }
                    override fun read(buffer: ByteArray, offset: Int, length: Int): Int {
                        largestRead = maxOf(largestRead, length)
                        repeat(length) { index -> buffer[offset + index] = ((consumed + index) % 251).toByte() }
                        consumed += length; return length
                    }
                    override fun close() { closed = true }
                }, mapOf("Content-Length" to original.get("size").asString))
            }
            json(meta(original))
        }
        val prefix = BaiduBinaryFileClient(config(), t).previewPrefix(normalized(original), 256 * 1024, { false })
        assertEquals(256 * 1024, prefix.size); assertEquals(256 * 1024, consumed); assertTrue(closed)
        assertTrue(largestRead <= 64 * 1024); assertArrayEquals(ByteArray(prefix.size) { (it % 251).toByte() }, prefix)
        assertTrue(t.steps.isEmpty())
    }
    @Test fun previewPrefixAcceptsExact206AndUsesSmallerOriginalFileSize() {
        val original = raw("short.txt", size = 3).apply { addProperty("dlink", "https://fixture.baidupcs.com/text") }
        val t = FakeTransport().apply {
            json(meta(original)); steps.add { request ->
                assertEquals("bytes=0-2", request.header("Range"))
                BinaryReply(206, byteArrayOf(0, -1, 42).inputStream(), mapOf("Content-Range" to "bytes 0-2/3", "Content-Length" to "3"))
            }; json(meta(original))
        }
        assertArrayEquals(byteArrayOf(0, -1, 42), BaiduBinaryFileClient(config(), t).previewPrefix(normalized(original), 50, { false }))
    }
    @Test fun previewPrefixRejectsWrong206RangesTotalsAndBodyLengthsWithoutReadingAnything() {
        val original = raw("log.txt", size = 100).apply { addProperty("dlink", "https://fixture.baidupcs.com/text") }
        for (header in listOf(null, "bytes 1-4/100", "bytes 0-3/99", "bytes 0-2/100", "bytes 0-4/100", "bytes 0-3/*", "bytes 0-3/100, 5-6/100")) {
            var closed = false; var read = false
            val t = FakeTransport().apply { json(meta(original)); steps.add {
                BinaryReply(206, object : InputStream() { override fun read(): Int { read = true; return -1 }; override fun close() { closed = true } },
                    if (header == null) emptyMap() else mapOf("Content-Range" to header))
            } }
            rejected<IllegalArgumentException> { BaiduBinaryFileClient(config(), t).previewPrefix(normalized(original), 4, { false }) }
            assertFalse(read); assertTrue(closed)
        }
        val wrongLength = FakeTransport().apply { json(meta(original)); steps.add {
            BinaryReply(206, byteArrayOf(1,2,3,4).inputStream(), mapOf("Content-Range" to "bytes 0-3/100", "Content-Length" to "5"))
        } }
        rejected<IllegalArgumentException> { BaiduBinaryFileClient(config(), wrongLength).previewPrefix(normalized(original), 4, { false }) }
    }
    @Test fun previewPrefixRejectsShortOrEncodedBodiesAndUnexpectedStatus() {
        val original = raw("log.txt", size = 100).apply { addProperty("dlink", "https://fixture.baidupcs.com/text") }
        for (reply in listOf(BinaryReply(200, byteArrayOf(1,2,3).inputStream()),
            BinaryReply(206, byteArrayOf(1,2,3).inputStream(), mapOf("Content-Range" to "bytes 0-3/100")),
            BinaryReply(200, byteArrayOf(1,2,3,4).inputStream(), mapOf("Content-Encoding" to "gzip")),
            BinaryReply(404, byteArrayOf().inputStream()))) {
            val t = FakeTransport().apply { json(meta(original)); steps.add { reply } }
            rejected<IllegalArgumentException> { BaiduBinaryFileClient(config(), t).previewPrefix(normalized(original), 4, { false }) }
        }
    }
    @Test fun previewPrefixChecksSourceBeforeAndAfterTheBoundedRead() {
        val original = raw("log.txt", size = 100).apply { addProperty("dlink", "https://fixture.baidupcs.com/text") }
        val changed = original.deepCopy().apply { addProperty("server_mtime", 1700000001) }
        val before = FakeTransport().apply { json(meta(changed)) }
        rejected<IllegalArgumentException> { BaiduBinaryFileClient(config(), before).previewPrefix(normalized(original), 4, { false }) }
        assertEquals(1, before.requests.size)
        val after = FakeTransport().apply { json(meta(original)); bytes(byteArrayOf(1,2,3,4)); json(meta(changed)) }
        rejected<IllegalArgumentException> { BaiduBinaryFileClient(config(), after).previewPrefix(normalized(original), 4, { false }) }
        assertEquals(3, after.requests.size)
    }
    @Test fun previewPrefixCancelClosesResponseWithoutReadingTheTailOrWritingFiles() {
        val original = raw("log.txt", size = 200_000).apply { addProperty("dlink", "https://fixture.baidupcs.com/text") }
        var cancel = false; var closed = false; var consumed = 0
        val t = FakeTransport().apply { json(meta(original)); steps.add {
            BinaryReply(200, object : InputStream() {
                override fun read(): Int = throw AssertionError("Use bounded reads")
                override fun read(buffer: ByteArray, offset: Int, length: Int): Int { consumed += length; cancel = true; return length }
                override fun close() { closed = true }
            })
        } }
        rejected<CancellationException> { BaiduBinaryFileClient(config(), t).previewPrefix(normalized(original), 150_000, { cancel }) }
        assertEquals(65_536, consumed); assertTrue(closed); assertEquals(2, t.requests.size)
        val alreadyCancelled = FakeTransport()
        rejected<CancellationException> { BaiduBinaryFileClient(config(), alreadyCancelled).previewPrefix(normalized(original), 4, { true }) }
        assertTrue(alreadyCancelled.requests.isEmpty())
    }
    @Test fun previewPrefixRetainsRangeAcrossRedirectsAndNeverLeaksTokenToBosOrOtherHosts() {
        val original = raw("log.txt", size = 100).apply { addProperty("dlink", "https://fixture.baidupcs.com/text") }
        val t = FakeTransport().apply {
            json(meta(original)); steps.add { request ->
                assertEquals("private-fixture-token", request.url.queryParameter("access_token"))
                BinaryReply(302, byteArrayOf().inputStream(), mapOf("Location" to "https://fixture.bcebos.com/signed?signature=yes&access_token=remove"))
            }; steps.add { request ->
                assertEquals("bytes=0-3", request.header("Range")); assertNull(request.url.queryParameter("access_token"))
                assertEquals("yes", request.url.queryParameter("signature"))
                BinaryReply(206, byteArrayOf(1,2,3,4).inputStream(), mapOf("Content-Range" to "bytes 0-3/100"))
            }; json(meta(original))
        }
        assertArrayEquals(byteArrayOf(1,2,3,4), BaiduBinaryFileClient(config(), t).previewPrefix(normalized(original), 4, { false }))
        val unsafe = FakeTransport().apply { json(meta(original)); steps.add {
            BinaryReply(302, byteArrayOf().inputStream(), mapOf("Location" to "https://evil.example/text"))
        } }
        rejected<IllegalArgumentException> { BaiduBinaryFileClient(config(), unsafe).previewPrefix(normalized(original), 4, { false }) }
        assertEquals(2, unsafe.requests.size)
    }
    @Test fun previewPrefixEmptyFileNeedsOnlySnapshotChecksAndEnforcesMaximumBeforeNetwork() {
        val original = raw("empty.txt", size = 0)
        val t = FakeTransport().apply { json(meta(original)); json(meta(original)) }
        assertArrayEquals(byteArrayOf(), BaiduBinaryFileClient(config(), t).previewPrefix(normalized(original), 256 * 1024, { false }))
        assertTrue(t.requests.all { it.url.queryParameter("method") == "filemetas" && it.url.queryParameter("dlink") == "0" })
        val limits = FakeTransport(); val client = BaiduBinaryFileClient(config(), limits)
        for (max in listOf(-1, 0, 256 * 1024 + 1)) rejected<IllegalArgumentException> { client.previewPrefix(normalized(original), max, { false }) }
        assertTrue(limits.requests.isEmpty())
    }
    @Test fun previewPrefixNetworkErrorsRemainSanitizedAndNoRetryOccurs() {
        val original = raw("log.txt", size = 100).apply { addProperty("dlink", "https://fixture.baidupcs.com/text") }
        val t = FakeTransport().apply { json(meta(original)); steps.add { throw IOException("https://fixture?access_token=private-fixture-token") } }
        val failure = rejected<IOException> { BaiduBinaryFileClient(config(), t).previewPrefix(normalized(original), 4, { false }) }
        assertFalse(failure.message!!.contains("private-fixture-token")); assertEquals(2, t.requests.size)
    }
    @Test fun recursiveDirectoryDeleteUsesOneScopedRequestAndEvictsDescendantIds() {
        val folder = raw("folder", 0, isdir = true); val child = raw("folder/file.bin", id = "42")
        val t = FakeTransport().apply {
            json(listing(child)); json(meta(folder)); json(mutationAck("$root/folder")) { request ->
                val form = request.body as okhttp3.FormBody
                val fields = (0 until form.size).associate { form.name(it) to form.value(it) }
                assertEquals("delete", request.url.queryParameter("opera")); assertEquals("0", fields["async"])
                assertEquals("fail", fields["ondup"]); assertEquals("[\"$root/folder\"]", fields["filelist"])
            }
            json(listing()); json("{\"errno\":-9}") { assertEquals("list", it.url.queryParameter("method")); assertEquals("$root/folder", it.url.queryParameter("dir")) }
        }
        val client = BaiduBinaryFileClient(config(), t); client.list("$root/folder")
        assertTrue(client.delete(normalized(folder)).get("verified").asBoolean)
        rejected<BinaryProviderException> { client.stat("$root/folder/file.bin") }
        assertEquals(1, t.requests.count { it.url.queryParameter("method") == "filemanager" }); assertTrue(t.steps.isEmpty())
    }
    @Test fun directoryDeleteChecksMtimeAndRejectsApplicationRoot() {
        val original = raw("folder", 0, isdir = true)
        val t = FakeTransport().apply { json(meta(raw("folder", 0, isdir = true, time = 1700000001))) }
        val client = BaiduBinaryFileClient(config(), t)
        rejected<IllegalArgumentException> { client.delete(normalized(original)) }
        rejected<IllegalArgumentException> { client.delete(normalized(original).apply { addProperty("path", root) }) }
        assertTrue(t.requests.all { it.method == "GET" }); assertEquals(1, t.requests.size)
    }
    @Test fun fractionalDeleteAcknowledgementCannotBeCoercedToSuccess() {
        val original = raw(); val t = FakeTransport().apply {
            json(meta(original)); json("{\"errno\":0,\"info\":[{\"path\":\"$root/sample.bin\",\"errno\":0.5}]}"); json(listing())
        }
        assertFalse(BaiduBinaryFileClient(config(), t).delete(normalized(original)).get("verified").asBoolean)
    }
    @Test fun unconfirmedOrAmbiguousDeleteCannotReportSuccessAndNeverReplays() {
        val raw = raw()
        val t = FakeTransport().apply { json(meta(raw)); json("{\"errno\":0}"); json(listing()) }
        val result = BaiduBinaryFileClient(config(), t).delete(normalized(raw))
        assertFalse(result.get("deleted").asBoolean); assertEquals("unknown", result.get("state").asString)
        val ambiguous = FakeTransport().apply { json(meta(raw)); steps.add { throw IOException("fixture interrupted") } }
        val uncertain = BaiduBinaryFileClient(config(), ambiguous).delete(normalized(raw))
        assertFalse(uncertain.get("verified").asBoolean); assertEquals(2, ambiguous.requests.size)
    }
    @Test fun asyncDirectoryDeleteRemainsUnknownEvenWhenParentListingIsEmpty() {
        val raw = raw("folder", 0, isdir = true)
        val t = FakeTransport().apply { json(meta(raw)); json("{\"errno\":0,\"taskid\":\"123\",\"info\":[{\"path\":\"$root/folder\",\"errno\":0}]}"); json(listing()) }
        val result = BaiduBinaryFileClient(config(), t).delete(normalized(raw))
        assertFalse(result.get("deleted").asBoolean)
    }
    @Test fun emptyUploadUsesEmptyBlockDigestAndConfirmsMetadata() = withTemp { dir ->
        val source = File(dir,"empty").apply { writeBytes(byteArrayOf()) }; val raw = raw(size = 0)
        val t = FakeTransport().apply {
            json(listing()); json("{\"uploadid\":\"fixture\",\"block_list\":[0]}") { assertTrue(body(it).contains(hash("MD5", byteArrayOf()))) }
            json("{\"md5\":\"${hash("MD5", byteArrayOf())}\"}") { val sink = Buffer(); it.body!!.writeTo(sink) }
            json(raw.toString()); json(meta(raw))
        }
        val result = BaiduBinaryFileClient(config(), t).upload(source, "$root/sample.bin", {}, { false })
        assertEquals(0L, result.get("bytes").asLong); assertEquals(hash("SHA-256", byteArrayOf()), result.get("sha256").asString)
    }
}
