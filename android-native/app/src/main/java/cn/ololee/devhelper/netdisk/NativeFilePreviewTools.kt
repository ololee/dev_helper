package cn.ololee.devhelper.netdisk

import com.google.gson.JsonArray
import com.google.gson.JsonObject
import com.google.gson.JsonParser

object NativeFilePreviewTools {
    private val names = setOf("netdisk_file_preview_info", "netdisk_file_preview_open", "netdisk_file_preview_job", "netdisk_file_preview_close")
    fun handles(name: String) = name in names
    fun list(): JsonArray = JsonArray().apply {
        val source = """{"scope":{"enum":["local","netdisk"]},"uri":{"type":"string","maxLength":8192},"path":{"type":"string","maxLength":8192},"fsId":{"type":"string","maxLength":24}}"""
        for ((name, description) in linkedMapOf(
            "netdisk_file_preview_info" to "Read scoped file metadata and its native preview kind without downloading bytes. Use uri for phone-local authorized directories, path and optional fsId for Baidu. Does not expose download tokens.",
            "netdisk_file_preview_open" to "Start an on-demand native preview of one scoped text, image, audio or video file. Poll netdisk_file_preview_job, then GET the returned relative contentPath as binary HTTP. Text reads at most 256 KiB and is returned as plain UTF-8; truncated marks a prefix. Media stays binary. No model or script runs. Close after reading; caches last only for this app process.",
            "netdisk_file_preview_job" to "Read preview loading progress, name, kind, truncation and completed binary contentPath. Failed, cancelled or unknown states are not completion; no source file is changed.",
            "netdisk_file_preview_close" to "Cancel loading or close a completed preview and discard its temporary cache. Does not delete, rename or modify the source file. Pass the preview jobId, not the source ID."
        )) {
            val reference = name in setOf("netdisk_file_preview_info", "netdisk_file_preview_open")
            add(JsonObject().apply {
                addProperty("name", name); addProperty("description", description)
                add("inputSchema", JsonObject().apply {
                    addProperty("type", "object"); addProperty("additionalProperties", false)
                    add("properties", JsonParser.parseString(if (reference) source else """{"jobId":{"type":"string","maxLength":36}}"""))
                    add("required", JsonArray().apply { add(if (reference) "scope" else "jobId") })
                })
                add("annotations", JsonObject().apply {
                    addProperty("readOnlyHint", name != "netdisk_file_preview_close")
                    addProperty("destructiveHint", false); addProperty("idempotentHint", name != "netdisk_file_preview_open")
                    addProperty("openWorldHint", true)
                })
            })
        }
    }
    fun call(runtime: NativeFilePreviewRuntime, name: String, args: JsonObject): JsonObject {
        require(handles(name)) { "未知文件预览工具" }
        val reference = name in setOf("netdisk_file_preview_info", "netdisk_file_preview_open")
        require(args.keySet().all { it in if (reference) setOf("scope", "uri", "path", "fsId") else setOf("jobId") }) { "预览参数包含未知字段" }
        return when (name) {
            "netdisk_file_preview_info" -> runtime.metadata(args)
            "netdisk_file_preview_open" -> runtime.start(args)
            "netdisk_file_preview_job" -> runtime.job(BaiduNetdiskTools.required(args, "jobId"))
            else -> runtime.cancel(BaiduNetdiskTools.required(args, "jobId"))
        }
    }
}
