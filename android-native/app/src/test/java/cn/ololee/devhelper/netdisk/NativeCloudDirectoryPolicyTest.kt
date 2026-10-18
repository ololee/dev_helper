package cn.ololee.devhelper.netdisk

import com.google.gson.JsonNull
import com.google.gson.JsonObject
import org.junit.Assert.*
import org.junit.Test
import java.io.File

/** Pure offline UI listing checks; no Context, SDK initialization, account, or network. */
class NativeCloudDirectoryPolicyTest {
    private class Cloud : NativeTransferCloud {
        val root = "/apps/generated-native-browser"
        val requests = mutableListOf<String>()
        var rows = emptyList<JsonObject>()
        override fun rootPath() = root
        override fun account(): String = error("Directory browsing must not fetch the account")
        override fun list(path: String): List<JsonObject> { requests.add(path); return rows }
        override fun stat(path: String): JsonObject? = error("Listing must not stat or download each item")
        override fun mkdir(path: String): JsonObject = error("Read-only")
        override fun upload(local: File, path: String, progress: (Long) -> Unit, cancelled: () -> Boolean): JsonObject = error("Read-only")
        override fun download(meta: JsonObject, destination: File, progress: (Long) -> Unit, cancelled: () -> Boolean): JsonObject = error("Read-only")
        override fun delete(meta: JsonObject): JsonObject = error("Read-only")
        fun entry(name: String, folder: Boolean = false) = JsonObject().apply {
            addProperty("path", "$root/$name"); addProperty("name", name); addProperty("fsId", name.hashCode().toString())
            addProperty("isDirectory", folder); addProperty("bytes", if (folder) 0 else 12); addProperty("modifiedAt", 1700000000)
            addProperty("md5", "opaque-provider-value")
        }
    }
    @Test fun browserUsesHttpCloudInterfaceAndPaginatesStableFolderFirstRows() {
        val cloud = Cloud().apply { rows = listOf(entry("z.png"), entry("folder", true), entry("a.txt")) }
        val first = NativeCloudDirectoryPolicy.list(cloud, JsonObject().apply { addProperty("limit", 2) })
        assertEquals(listOf(cloud.root), cloud.requests); assertTrue(first.get("hasMore").asBoolean)
        assertEquals(listOf("folder", "a.txt"), first.getAsJsonArray("files").map { it.asJsonObject.get("name").asString })
        val second = NativeCloudDirectoryPolicy.list(cloud, JsonObject().apply { addProperty("path", cloud.root); addProperty("offset", 2); addProperty("limit", 2) })
        assertEquals("z.png", second.getAsJsonArray("files").single().asJsonObject.get("name").asString)
        assertFalse(second.get("hasMore").asBoolean); assertEquals(3, second.get("total").asInt)
        val end = NativeCloudDirectoryPolicy.list(cloud, JsonObject().apply { addProperty("offset", Int.MAX_VALUE); addProperty("limit", 1000) })
        assertTrue(end.getAsJsonArray("files").isEmpty); assertFalse(end.get("hasMore").asBoolean)
    }
    @Test fun invalidScopesAndPaginationAreRejectedBeforeListing() {
        val cloud = Cloud()
        val bad = listOf(
            JsonObject().apply { addProperty("path", "/apps/other") },
            JsonObject().apply { addProperty("path", cloud.root + "-other") },
            JsonObject().apply { addProperty("path", cloud.root + "/../other") },
            JsonObject().apply { add("path", JsonNull.INSTANCE) },
            JsonObject().apply { addProperty("offset", -1) },
            JsonObject().apply { addProperty("offset", 1.5) },
            JsonObject().apply { addProperty("limit", 0) },
            JsonObject().apply { addProperty("limit", 1001) },
            JsonObject().apply { addProperty("limit", "10") },
            JsonObject().apply { addProperty("accessToken", "forbidden") }
        )
        bad.forEach { args -> assertThrows(IllegalArgumentException::class.java) { NativeCloudDirectoryPolicy.list(cloud, args) } }
        assertTrue(cloud.requests.isEmpty())
    }
    @Test fun unexpectedDirectoryRowsAreRejectedAndPrivateFieldsNeverReachTheUi() {
        val cloud = Cloud()
        for (path in listOf("/apps/other/a", cloud.root + "/nested/child", cloud.root)) {
            cloud.rows = listOf(cloud.entry("item").apply { addProperty("path", path) })
            assertThrows(IllegalArgumentException::class.java) { NativeCloudDirectoryPolicy.list(cloud, JsonObject()) }
        }
        val row = cloud.entry("item").apply { addProperty("_dlink", "https://private-download.example/"); addProperty("accessToken", "not-for-ui") }
        cloud.rows = listOf(row, row.deepCopy())
        assertThrows(IllegalArgumentException::class.java) { NativeCloudDirectoryPolicy.list(cloud, JsonObject()) }
        cloud.rows = listOf(row)
        val listed = NativeCloudDirectoryPolicy.list(cloud, JsonObject()).getAsJsonArray("files").single().asJsonObject
        assertFalse(listed.has("_dlink")); assertFalse(listed.has("accessToken")); assertEquals("opaque-provider-value", listed.get("md5").asString)
        listed.addProperty("name", "changed-ui-copy"); assertEquals("item", row.get("name").asString)
    }
    @Test fun emptyAndFailingHttpListingsDoNotFallBackToSdkOrInventFiles() {
        val cloud = Cloud()
        val result = NativeCloudDirectoryPolicy.list(cloud, JsonObject())
        assertTrue(result.getAsJsonArray("files").isEmpty); assertFalse(result.get("hasMore").asBoolean)
        val failed = object : NativeTransferCloud by cloud { override fun list(path: String): List<JsonObject> = throw IllegalStateException("generated-network-failure") }
        val error = assertThrows(IllegalStateException::class.java) { NativeCloudDirectoryPolicy.list(failed, JsonObject()) }
        assertEquals("generated-network-failure", error.message)
    }
}
