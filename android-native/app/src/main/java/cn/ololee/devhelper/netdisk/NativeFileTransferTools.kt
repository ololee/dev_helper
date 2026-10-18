package cn.ololee.devhelper.netdisk

import com.google.gson.JsonArray
import com.google.gson.JsonObject
import com.google.gson.JsonParser

/** Source references are device-owned URIs/IDs, never binary data or client paths. */
object NativeFileTransferTools {
    private val names = setOf("netdisk_local_roots", "netdisk_local_list", "netdisk_transfer_plan", "netdisk_transfer_execute",
        "netdisk_transfer_jobs", "netdisk_transfer_job", "netdisk_transfer_cancel", "netdisk_file_operation_plan")
    fun handles(name: String) = name in names

    fun list(): JsonArray {
        val tools = JsonArray()
        fun add(name: String, description: String, properties: String = "{}", required: List<String> = emptyList(), readOnly: Boolean = true, destructive: Boolean = false) {
            tools.add(JsonObject().apply {
                addProperty("name", name); addProperty("description", description)
                add("inputSchema", JsonObject().apply {
                    addProperty("type", "object"); addProperty("additionalProperties", false)
                    add("properties", JsonParser.parseString(properties))
                    add("required", JsonArray().apply { required.forEach { add(it) } })
                })
                add("annotations", JsonObject().apply {
                    addProperty("readOnlyHint", readOnly); addProperty("destructiveHint", destructive)
                    addProperty("idempotentHint", readOnly); addProperty("openWorldHint", true)
                })
            })
        }
        add("netdisk_local_roots", "Read phone file-manager roots already authorized in the native system folder picker, the default device file root, and supported transfer operations. Does not grant access or list unrelated private app files.")
        add("netdisk_local_list", "Browse one authorized phone directory. Use returned item.uri and pathUri in transfer plans; URIs belong to this phone. Select other roots in the native UI, not by inventing arbitrary paths.", """{"pathUri":{"type":"string","maxLength":8192},"offset":{"type":"integer","minimum":0},"limit":{"type":"integer","minimum":1,"maximum":500}}""")
        add("netdisk_transfer_plan", "Preview a batch phone-local to Baidu Netdisk transfer, or the reverse, including folders. Source scope and destinationScope must differ. Copy preserves sources; move verifies the entire batch before cleaning sources. No overwrite or automatic rename. Read this exact plan before execute; all file bytes remain binary streams on the phone.", """{"scope":{"enum":["local","netdisk"]},"destinationScope":{"enum":["local","netdisk"]},"operation":{"enum":["copy","move"]},"items":{"type":"array","minItems":1,"maxItems":100,"items":{"type":"object","additionalProperties":false,"properties":{"uri":{"type":"string","maxLength":8192},"path":{"type":"string","maxLength":8192},"fsId":{"type":"string","maxLength":24}}}},"destination":{"type":"object","additionalProperties":false,"properties":{"uri":{"type":"string","maxLength":8192},"path":{"type":"string","maxLength":8192}}}}""", listOf("scope", "destinationScope", "operation", "items", "destination"))
        add("netdisk_file_operation_plan", "Preview rename or deletion of selected phone-local or Baidu files and folders. Local deletion is permanent; Baidu recycling follows platform policy. Read every planned source and target, including companion .meta files, before netdisk_transfer_execute. A single name is exact; batch templates support {name}/{stem} without extension, {ext} with dot, and {index} starting at 1. No overwrite, no shell execution.", """{"scope":{"enum":["local","netdisk"]},"operation":{"enum":["rename","trash"]},"items":{"type":"array","minItems":1,"maxItems":100,"items":{"type":"object","additionalProperties":false,"properties":{"uri":{"type":"string","maxLength":8192},"path":{"type":"string","maxLength":8192},"fsId":{"type":"string","maxLength":24}}}},"name":{"type":"string","maxLength":240,"description":"Required for rename; exact single name or a batch template such as {name}_{index}{ext}."}}""", listOf("scope", "operation", "items"))
        add("netdisk_transfer_execute", "Queue a reviewed transfer exactly once using planId and planDigest. Poll netdisk_transfer_job. A move is finished only when moveCommitted is true and sourceCleanupState is completed; partial, cancelled, unknown or interrupted are not success. Do not blindly replay writes after a network timeout.", """{"planId":{"type":"string","maxLength":64},"planDigest":{"type":"string","maxLength":128}}""", listOf("planId", "planDigest"), false, true)
        add("netdisk_transfer_jobs", "List this phone's persisted native transfer, rename and deletion jobs, byte/file progress and per-item results. Process restart marks unfinished operations interrupted without replaying mutations.")
        add("netdisk_transfer_job", "Read a native transfer job, byte/file progress, verified destination results and per-source cleanup state.", """{"jobId":{"type":"string","maxLength":64}}""", listOf("jobId"))
        add("netdisk_transfer_cancel", "Request cancellation of one native transfer. Already committed destinations stay; source deletion already submitted may have completed. Read final per-item receipts to determine preserved sources and never assume rollback.", """{"jobId":{"type":"string","maxLength":64}}""", listOf("jobId"), false)
        return tools
    }

    fun call(runtime: NativeFileTransferRuntime, name: String, args: JsonObject): JsonObject {
        val allowed = when (name) {
            "netdisk_local_roots", "netdisk_transfer_jobs" -> emptySet()
            "netdisk_local_list" -> setOf("pathUri", "offset", "limit")
            "netdisk_transfer_plan" -> setOf("scope", "destinationScope", "operation", "items", "destination")
            "netdisk_file_operation_plan" -> setOf("scope", "operation", "items", "name")
            "netdisk_transfer_execute" -> setOf("planId", "planDigest")
            "netdisk_transfer_job", "netdisk_transfer_cancel" -> setOf("jobId")
            else -> throw IllegalArgumentException("未知原生文件传输工具")
        }
        require(args.keySet().all { it in allowed }) { "传输参数包含未知字段" }
        return when (name) {
            "netdisk_local_roots" -> runtime.status()
            "netdisk_local_list" -> runtime.localFiles(args)
            "netdisk_transfer_plan" -> { require(BaiduNetdiskTools.required(args, "operation") in setOf("copy", "move")) { "请使用文件操作计划进行重命名或删除" }; runtime.plan(args) }
            "netdisk_file_operation_plan" -> { require(BaiduNetdiskTools.required(args, "operation") in setOf("rename", "trash")) { "文件操作计划只接受重命名或删除" }; runtime.plan(args) }
            "netdisk_transfer_execute" -> runtime.execute(args)
            "netdisk_transfer_jobs" -> runtime.jobs()
            "netdisk_transfer_job" -> runtime.job(BaiduNetdiskTools.required(args, "jobId"))
            else -> runtime.cancel(BaiduNetdiskTools.required(args, "jobId"))
        }
    }
}
