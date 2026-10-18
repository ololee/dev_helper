package cn.ololee.devhelper.netdisk

import com.google.gson.JsonObject
import org.junit.Assert.*
import org.junit.Test

class NativeFileTransferPresentationTest {
    private fun job(state: String = "running", operation: String = "copy", result: JsonObject? = null) = JsonObject().apply {
        addProperty("state", state); addProperty("operation", operation); if (result != null) add("result", result)
    }

    @Test fun moveNeverReportsCompleteUntilBothExplicitSourceCleanupAndCommitSucceed() {
        val result = JsonObject().apply { addProperty("moveCommitted", true); addProperty("sourceCleanupState", "pending"); addProperty("sourcePreserved", false) }
        val value = job("completed", "move", result)
        assertFalse(NativeFileTransferPresentation.moveFinished(value))
        assertFalse(NativeFileTransferPresentation.stage(value) == "移动完成")
        result.addProperty("sourceCleanupState", "completed")
        assertTrue(NativeFileTransferPresentation.moveFinished(value))
        assertEquals("移动完成", NativeFileTransferPresentation.stage(value))
        result.remove("moveCommitted")
        assertFalse(NativeFileTransferPresentation.moveFinished(value))
    }

    @Test fun cancelledAndPartialJobsAreTerminalWithoutBeingMovedSuccessfully() {
        listOf("cancelled", "completed-with-errors", "failed", "interrupted").forEach { state ->
            val value = job(state, "move")
            assertTrue(NativeFileTransferPresentation.terminal(value))
            assertFalse(NativeFileTransferPresentation.moveFinished(value))
        }
        assertFalse(NativeFileTransferPresentation.terminal(job("running")))
    }

    @Test fun byteProgressIsClampedAndCompletedEmptyDirectoryShowsComplete() {
        val value = job().apply { add("progress", JsonObject().apply { addProperty("bytesTransferred", 150); addProperty("totalBytes", 100) }) }
        assertEquals(1f, NativeFileTransferPresentation.progress(value))
        value.getAsJsonObject("progress").addProperty("bytesTransferred", -1)
        assertEquals(0f, NativeFileTransferPresentation.progress(value))
        assertEquals(1f, NativeFileTransferPresentation.progress(job("completed")))
        assertEquals(0f, NativeFileTransferPresentation.progress(job("queued")))
    }

    @Test fun verificationAndCleanupAreDistinctVisibleStages() {
        val value = job("running", "move").apply { add("progress", JsonObject().apply { addProperty("phase", "verifying-upload") }) }
        assertEquals("正在校验文件", NativeFileTransferPresentation.stage(value))
        value.getAsJsonObject("progress").addProperty("phase", "cleaning-sources")
        assertEquals("正在处理源文件", NativeFileTransferPresentation.stage(value))
        value.addProperty("cancelRequested", true)
        assertTrue(NativeFileTransferPresentation.stage(value).contains("取消"))
    }

    @Test fun formattedByteCountsAreStableAndNeverNegative() {
        assertEquals("0 B", NativeFileTransferPresentation.bytes(-5))
        assertEquals("1023 B", NativeFileTransferPresentation.bytes(1023))
        assertEquals("1.0 KB", NativeFileTransferPresentation.bytes(1024))
        assertEquals("2.5 MB", NativeFileTransferPresentation.bytes(2621440))
    }

    @Test fun unknownSourceRetentionNeverClearsClipboardEvenWithOptimisticFlags() {
        val result = JsonObject().apply {
            addProperty("moveCommitted", true); addProperty("sourceCleanupState", "completed")
            add("sourcePreserved", com.google.gson.JsonNull.INSTANCE)
        }
        val value = job("completed", "move", result)
        assertFalse(NativeFileTransferPresentation.moveFinished(value))
        result.remove("sourcePreserved")
        assertFalse(NativeFileTransferPresentation.moveFinished(value))
        result.addProperty("sourcePreserved", true)
        assertFalse(NativeFileTransferPresentation.moveFinished(value))
        result.addProperty("sourcePreserved", false)
        assertTrue(NativeFileTransferPresentation.moveFinished(value))
        value.addProperty("state", "failed")
        assertFalse(NativeFileTransferPresentation.moveFinished(value))
    }

    @Test fun partialCloudCleanupOnlyRemovesExplicitlyConfirmedVerifiedOriginals() {
        fun item(path: String, cleanup: String, retained: Boolean?) = JsonObject().apply {
            addProperty("sourcePath", path); addProperty("sourceCleanupState", cleanup)
            addProperty("targetVerified", true); addProperty("destinationCommitted", true); addProperty("transferState", "completed")
            if (retained == null) add("sourcePreserved", com.google.gson.JsonNull.INSTANCE) else addProperty("sourcePreserved", retained)
        }
        val confirmed = item("/apps/test/good", "completed", false)
        val result = JsonObject().apply { add("items", com.google.gson.JsonArray().apply {
            add(confirmed); add(item("/apps/test/unknown", "completed", null)); add(item("/apps/test/partial", "partial", false)); add(item("/apps/test/kept", "completed", true))
        }) }
        val value = job("completed-with-errors", "move", result)
        assertEquals(setOf("/apps/test/good"), NativeFileTransferPresentation.cleanedSources(value, "netdisk"))
        confirmed.addProperty("targetVerified", false)
        assertTrue(NativeFileTransferPresentation.cleanedSources(value, "netdisk").isEmpty())
        confirmed.addProperty("targetVerified", true); value.addProperty("state", "running")
        assertTrue(NativeFileTransferPresentation.cleanedSources(value, "netdisk").isEmpty())
    }

    private fun selectedSource(ref: String, name: String, scope: String = "netdisk") = JsonObject().apply {
        addProperty(if (scope == "local") "uri" else "path", ref); addProperty("name", name); addProperty("isDirectory", false)
    }
    private fun movedItem(ref: String, name: String, scope: String = "netdisk", cleanup: String = "completed", retained: Boolean? = false) = JsonObject().apply {
        addProperty(if (scope == "local") "sourceUri" else "sourcePath", ref); addProperty("source", ref); addProperty("name", name)
        addProperty("sourceCleanupState", cleanup); addProperty("targetVerified", true); addProperty("destinationCommitted", true); addProperty("transferState", "completed")
        if (retained == null) add("sourcePreserved", com.google.gson.JsonNull.INSTANCE) else addProperty("sourcePreserved", retained)
    }
    private fun partialMove(vararg items: JsonObject) = job("completed-with-errors", "move", JsonObject().apply {
        addProperty("sourceCleanupState", "partial"); addProperty("moveCommitted", false)
        add("items", com.google.gson.JsonArray().apply { items.forEach { add(it) } })
    })

    @Test fun failedAutomaticMetaRemainsInCutClipboardAfterPrimaryCleanupSucceeds() {
        val path = "/apps/test/hero.png"
        val original = selectedSource(path, "hero.png")
        val primary = movedItem(path, "hero.png")
        val meta = movedItem("$path.meta", "hero.png.meta", cleanup = "failed", retained = true).apply {
            addProperty("automaticCompanion", true); addProperty("companionOf", path)
        }
        val remaining = NativeFileTransferPresentation.remainingCutItems(partialMove(primary, meta), "netdisk", listOf(original))
        assertEquals(1, remaining.size)
        assertEquals("$path.meta", remaining.single().get("path").asString)
        assertEquals("hero.png.meta", remaining.single().get("name").asString)
        assertFalse(remaining.single().get("needsSourceReview").asBoolean)
        // Seeing this terminal job again cannot duplicate or discard the remaining companion.
        assertEquals(remaining, NativeFileTransferPresentation.remainingCutItems(partialMove(primary, meta), "netdisk", remaining))
    }

    @Test fun opaqueSafAutomaticMetaNeedsExplicitParentProofAndUnknownStateIsMarked() {
        val path = "content://documents/tree/root/document/opaque-primary-id"
        val metaPath = "content://documents/tree/root/document/opaque-meta-id"
        val original = selectedSource(path, "hero.png", "local")
        val primary = movedItem(path, "hero.png", "local")
        val meta = movedItem(metaPath, "hero.png.meta", "local", "unknown", null).apply {
            addProperty("automaticCompanion", true); addProperty("companionOf", path)
        }
        val remaining = NativeFileTransferPresentation.remainingCutItems(partialMove(primary, meta), "local", listOf(original))
        assertEquals(metaPath, remaining.single().get("uri").asString)
        assertTrue(remaining.single().get("needsSourceReview").asBoolean)
        meta.remove("companionOf")
        assertTrue(NativeFileTransferPresentation.remainingCutItems(partialMove(primary, meta), "local", listOf(original)).isEmpty())
    }

    @Test fun unrelatedAndUnprovenMetaCannotBeIntroducedIntoClipboard() {
        val path = "/apps/test/hero.png"
        val original = selectedSource(path, "hero.png")
        val primary = movedItem(path, "hero.png")
        fun companion(ref: String, name: String, parent: String, automatic: Boolean = true) = movedItem(ref, name, cleanup = "unknown", retained = null).apply {
            addProperty("automaticCompanion", automatic); addProperty("companionOf", parent)
        }
        listOf(
            companion("$path.meta", "hero.png.meta", path, automatic = false),
            companion("$path.meta", "hero.png.meta", "/apps/test/unselected.png"),
            companion("$path.meta", "other.png.meta", path),
            companion("/apps/other/hero.png.meta", "hero.png.meta", path)
        ).forEach { meta -> assertTrue(NativeFileTransferPresentation.remainingCutItems(partialMove(primary, meta), "netdisk", listOf(original)).isEmpty()) }
    }

    @Test fun successfulCompanionDoesNotCreateExtraClipboardEntryAndPendingPrimaryIsNotDuplicated() {
        val path = "/apps/test/hero.png"
        val original = selectedSource(path, "hero.png")
        val primary = movedItem(path, "hero.png")
        val meta = movedItem("$path.meta", "hero.png.meta").apply { addProperty("automaticCompanion", true); addProperty("companionOf", path) }
        assertTrue(NativeFileTransferPresentation.remainingCutItems(partialMove(primary, meta), "netdisk", listOf(original)).isEmpty())
        primary.addProperty("sourceCleanupState", "failed"); primary.addProperty("sourcePreserved", true)
        meta.addProperty("sourceCleanupState", "unknown"); meta.add("sourcePreserved", com.google.gson.JsonNull.INSTANCE)
        assertEquals(listOf(original), NativeFileTransferPresentation.remainingCutItems(partialMove(primary, meta), "netdisk", listOf(original)))
    }
}
