package cn.ololee.devhelper.netdisk

import com.google.gson.JsonArray
import com.google.gson.JsonObject
import java.util.Locale

/** Display and clipboard decisions are deliberately separate from the transfer engine. */
internal object NativeFileTransferPresentation {
    fun text(value: JsonObject, key: String, fallback: String = ""): String =
        runCatching { value.get(key)?.takeUnless { it.isJsonNull }?.asString }.getOrNull() ?: fallback
    fun flag(value: JsonObject, key: String): Boolean = runCatching { value.get(key)?.asBoolean == true }.getOrDefault(false)
    fun number(value: JsonObject, key: String): Long = runCatching { value.get(key)?.asLong ?: 0L }.getOrDefault(0L)
    private fun explicitlyFalse(value: JsonObject, key: String): Boolean = runCatching {
        value.has(key) && !value.get(key).isJsonNull && value.get(key).asJsonPrimitive.isBoolean && !value.get(key).asBoolean
    }.getOrDefault(false)
    fun objects(value: JsonArray?): List<JsonObject> = value?.mapNotNull { if (it.isJsonObject) it.asJsonObject else null } ?: emptyList()
    fun terminal(job: JsonObject): Boolean = text(job, "state") in setOf("completed", "completed-with-errors", "failed", "cancelled", "interrupted")
    fun moveFinished(job: JsonObject): Boolean {
        val result = job.getAsJsonObject("result") ?: job
        return text(job, "state") == "completed" && text(job, "operation", text(result, "operation")) == "move" &&
            flag(result, "moveCommitted") && text(result, "sourceCleanupState") == "completed" && explicitlyFalse(result, "sourcePreserved")
    }
    /** A partial move only removes positively confirmed source entries from the clipboard. */
    fun cleanedSources(job: JsonObject, sourceScope: String): Set<String> {
        if (!terminal(job) || text(job, "operation") != "move") return emptySet()
        val result = job.getAsJsonObject("result") ?: job
        return objects(result.getAsJsonArray("items")).filter {
            text(it, "sourceCleanupState") == "completed" && text(it, "transferState") == "completed" &&
                flag(it, "targetVerified") && flag(it, "destinationCommitted") && explicitlyFalse(it, "sourcePreserved")
        }.map { text(it, if (sourceScope == "local") "sourceUri" else "sourcePath", text(it, "source")) }.filter { it.isNotBlank() }.toSet()
    }
    /** Keep a failed, engine-proven automatic .meta companion after its primary was moved. */
    fun remainingCutItems(job: JsonObject, sourceScope: String, current: List<JsonObject>): List<JsonObject> {
        if (!terminal(job) || text(job, "operation") != "move") return current
        if (moveFinished(job)) return emptyList()
        val key = if (sourceScope == "local") "uri" else "path"
        val sourceKey = if (sourceScope == "local") "sourceUri" else "sourcePath"
        val removed = cleanedSources(job, sourceScope)
        val result = job.getAsJsonObject("result") ?: job
        val reported = objects(result.getAsJsonArray("items"))
        val bySource = reported.associateBy { text(it, sourceKey, text(it, "source")) }
        val remaining = current.filterNot { text(it, key) in removed }.map { source ->
            val item = bySource[text(source, key)]
            val review = item != null && (text(item, "sourceCleanupState") in setOf("partial", "unknown") ||
                (item.has("sourcePreserved") && item.get("sourcePreserved").isJsonNull) || explicitlyFalse(item, "sourcePreserved"))
            if (review && !flag(source, "needsSourceReview")) source.deepCopy().apply { addProperty("needsSourceReview", true) } else source
        }.toMutableList()
        val primaries = current.filter {
            !flag(it, "isDirectory") && text(it, "name").isNotBlank() && !text(it, "name").endsWith(".meta") && text(it, key) in removed
        }.associateBy { text(it, key) }
        reported.forEach { item ->
            if (!flag(item, "automaticCompanion")) return@forEach
            val primary = primaries[text(item, "companionOf")] ?: return@forEach
            if (text(item, "name") != text(primary, "name") + ".meta" || flag(item, "isDirectory")) return@forEach
            val ref = text(item, sourceKey, text(item, "source"))
            if (ref.isBlank() || ref == text(primary, key) || ref in removed || remaining.any { text(it, key) == ref }) return@forEach
            val valid = if (sourceScope == "netdisk") ref == text(primary, key) + ".meta" else runCatching {
                val parsed = java.net.URI(ref)
                parsed.scheme in setOf("file", "content") && parsed.query == null && parsed.fragment == null &&
                    (parsed.scheme != "file" || ref == text(primary, key) + ".meta")
            }.getOrDefault(false)
            if (!valid) return@forEach
            remaining.add(JsonObject().apply {
                addProperty(key, ref); addProperty("name", text(item, "name")); addProperty("isDirectory", false)
                addProperty("bytes", number(item, "bytes")); addProperty("automaticCompanion", true)
                addProperty("companionOf", text(item, "companionOf"))
                addProperty("needsSourceReview", !flag(item, "sourcePreserved") || text(item, "sourceCleanupState") in setOf("partial", "unknown"))
            })
        }
        return if (remaining == current) current else remaining
    }
    fun bytes(value: Long): String {
        val count = value.coerceAtLeast(0)
        val units = arrayOf("B", "KB", "MB", "GB", "TB")
        var amount = count.toDouble(); var index = 0
        while (amount >= 1024 && index < units.lastIndex) { amount /= 1024; index++ }
        return if (index == 0) "$count B" else String.format(Locale.ROOT, "%.1f %s", amount, units[index])
    }
    fun progress(job: JsonObject): Float {
        if (text(job, "state") == "completed") return 1f
        val value = job.getAsJsonObject("progress") ?: return 0f
        val total = number(value, "totalBytes")
        if (total > 0) return (number(value, "bytesTransferred").toDouble() / total).toFloat().coerceIn(0f, 1f)
        val count = number(value, "fileCount")
        return if (count > 0) (number(value, "completedFiles").toFloat() / count).coerceIn(0f, 1f) else 0f
    }
    fun stage(job: JsonObject): String {
        val state = text(job, "state")
        if (state == "completed") return if (text(job, "operation") == "move") if (moveFinished(job)) "移动完成" else "文件已传输，请检查源文件处理结果" else "复制完成"
        if (state == "cancelled") return "已取消，查看已传输项目"
        if (state in setOf("failed", "completed-with-errors", "interrupted")) return when (state) { "interrupted" -> "传输已中断"; "completed-with-errors" -> "部分项目未完成"; else -> "传输未完成" }
        if (flag(job, "cancelRequested")) return "正在取消，等待当前操作停止"
        return when (text(job.getAsJsonObject("progress") ?: JsonObject(), "phase", state)) {
            "queued" -> "等待传输"
            "planning", "preparing", "staging", "creating-directories" -> "准备文件和目录"
            "reading-source" -> if (text(job, "sourceScope") == "netdisk") "正在读取网盘文件" else "正在读取手机文件"
            "transferring" -> if (text(job, "destinationScope") == "netdisk") "正在传到百度网盘" else "正在保存到手机"
            "uploading", "upload" -> "正在传到百度网盘"
            "downloading", "download" -> "正在保存到手机"
            "verifying", "verifying-upload", "verifying-before-cleanup", "verifying-batch", "verify" -> "正在校验文件"
            "committing", "commit" -> "正在保存目标文件"
            "cleaning-sources", "cleanup", "source-cleanup" -> "正在处理源文件"
            "completed" -> "传输完成"
            else -> "正在传输"
        }
    }
}
