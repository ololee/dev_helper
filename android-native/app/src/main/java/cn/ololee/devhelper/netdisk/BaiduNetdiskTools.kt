package cn.ololee.devhelper.netdisk

import com.google.gson.*

/** Credentials are configured through the native settings screen, never returned to an AI. */
object BaiduNetdiskTools {
    @JvmStatic fun list(): JsonArray {
        val array=JsonArray()
        fun add(name:String,description:String,properties:String="{}",required:List<String> = emptyList(),read:Boolean=true) {
            array.add(JsonObject().apply {
                addProperty("name",name);addProperty("description",description)
                add("inputSchema",JsonObject().apply {addProperty("type","object");add("properties",JsonParser.parseString(properties));addProperty("additionalProperties",false);add("required",JsonArray().apply {required.forEach {add(it)}})})
                add("annotations",JsonObject().apply {addProperty("readOnlyHint",read);addProperty("destructiveHint",false);addProperty("openWorldHint",true)})
            })
        }
        add("netdisk_status","Show Baidu Netdisk configuration status and missing fields. Never returns OAuth tokens.")
        add("netdisk_list","List remote resource files inside the configured root directory. File data is transferred separately as binary HTTP, never base64.","""{"path":{"type":"string"},"offset":{"type":"integer","minimum":0},"limit":{"type":"integer","minimum":1,"maximum":1000}}""")
        add("netdisk_mkdir","Create a remote directory inside the configured resource root.","""{"path":{"type":"string"}}""",listOf("path"),false)
        add("netdisk_upload","Start background binary upload of a readable file path on this Android device (or an already granted content URI). Does not read files on the AI client computer. Duplicate names get a new name; originals are retained. Poll netdisk_jobs.","""{"localPath":{"type":"string"},"remoteDir":{"type":"string"}}""",listOf("localPath"),false)
        add("netdisk_download","Start a binary download of a remote fsId into this phone's private download storage. Poll netdisk_job, then GET its contentPath for a binary stream.","""{"fsId":{"type":"string"}}""",listOf("fsId"),false)
        add("netdisk_jobs","List persistent SDK upload/download tasks and byte progress.")
        add("netdisk_job","Read one upload:<id> or download:<id> background task.","""{"jobId":{"type":"string"}}""",listOf("jobId"))
        add("netdisk_job_control","Pause, resume, or cancel a transfer. Cancel retains local files and remote originals.","""{"jobId":{"type":"string"},"action":{"type":"string","enum":["pause","resume","cancel"]}}""",listOf("jobId","action"),false)
        array.addAll(NativeFileTransferTools.list())
        return array
    }
    @JvmStatic fun call(runtime:BaiduNetdiskRuntime,name:String,args:JsonObject):JsonObject {
        if(NativeFileTransferTools.handles(name))return NativeFileTransferTools.call(runtime.nativeTransfers(),name,args)
        val allowed=when(name){"netdisk_status","netdisk_jobs"->setOf();"netdisk_list"->setOf("path","offset","limit");"netdisk_mkdir"->setOf("path");"netdisk_upload"->setOf("localPath","remoteDir");"netdisk_download"->setOf("fsId");"netdisk_job"->setOf("jobId");"netdisk_job_control"->setOf("jobId","action");else->throw IllegalArgumentException("未知网盘工具")}
        require(args.keySet().all {it in allowed}){"网盘参数包含未知字段"}
        return when(name){"netdisk_status"->runtime.status();"netdisk_jobs"->runtime.jobs();"netdisk_list"->runtime.files(args);"netdisk_mkdir"->runtime.mkdir(args);"netdisk_upload"->runtime.upload(args);"netdisk_download"->runtime.download(args);"netdisk_job"->runtime.job(required(args,"jobId"));else->runtime.action(required(args,"jobId"),required(args,"action"))}
    }
    internal fun required(args:JsonObject,key:String):String {require(args.has(key)&&args.get(key).isJsonPrimitive&&args.get(key).asJsonPrimitive.isString){"缺少文本参数: $key"};return args.get(key).asString}
}
