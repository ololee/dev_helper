package cn.ololee.devhelper.netdisk

import android.content.Context
import com.google.gson.JsonObject
import com.google.gson.JsonParseException
import com.google.gson.JsonParser
import io.github.libsumcp.protocol.HttpEndpoint
import java.io.FileNotFoundException

/** The native screen and HTTP MCP share the same persisted transfer queue. */
object NativeFileTransferHttpEndpoint {
    @JvmStatic fun handle(context: Context, method: String, target: String, body: ByteArray?): HttpEndpoint.Response? {
        val normalized = target.replaceFirst("/api/knowledge/netdisk/", "/api/netdisk/")
        val base = "/api/netdisk/file-ops"
        val path = normalized.substringBefore('?')
        if (path != base && !path.startsWith("$base/")) return null
        return try {
            require(!normalized.contains('?')) { "此接口不接受查询参数" }
            val route = path.removePrefix(base)
            val runtime = NativeFileTransferRuntime.get(context)
            fun fields(vararg allowed: String): JsonObject {
                require((body?.size ?: 0) <= 65536) { "传输请求超过 64KB" }
                val value = if (body == null || body.isEmpty()) JsonObject() else JsonParser.parseString(body.toString(Charsets.UTF_8)).also {
                    require(it.isJsonObject) { "参数必须为 JSON 对象" }
                }.asJsonObject
                require(value.keySet().all { it in allowed }) { "传输参数包含未知字段" }
                return value
            }
            fun expected(value: String) { require(method == value) { "此入口需要 $value 请求" } }
            when {
                route == "/status" -> { expected("GET"); fields(); json(200, runtime.status()) }
                route == "/local" -> { expected("POST"); json(200, runtime.localFiles(fields("pathUri", "offset", "limit"))) }
                route == "/plan" -> { expected("POST"); json(200, runtime.plan(fields("scope", "destinationScope", "operation", "items", "destination", "name"))) }
                route == "/execute" -> { expected("POST"); json(202, runtime.execute(fields("planId", "planDigest"))) }
                route == "/jobs" -> { expected("GET"); fields(); json(200, runtime.jobs()) }
                route == "/preview/info" -> { expected("POST"); json(200, NativeFilePreviewRuntime.get(context).metadata(fields("scope", "uri", "path", "fsId"))) }
                route == "/previews" -> { expected("POST"); json(202, NativeFilePreviewRuntime.get(context).start(fields("scope", "uri", "path", "fsId"))) }
                route.startsWith("/previews/") -> {
                    expected("GET"); fields()
                    val parts = route.removePrefix("/previews/").split('/')
                    require(parts.size == 2 && parts[0].matches(Regex("[0-9a-f-]{36}")) && parts[1] == "content") { "预览内容路径无效" }
                    val content = NativeFilePreviewRuntime.get(context).binary(parts[0])
                    HttpEndpoint.Response(200, content.mimeType, content.stream, content.bytes,
                        "Cache-Control: no-store\r\nX-Content-Type-Options: nosniff\r\nContent-Disposition: attachment; filename=\"preview\"\r\nContent-Security-Policy: default-src 'none'; sandbox\r\n")
                }
                route.startsWith("/preview-jobs/") -> {
                    val parts = route.removePrefix("/preview-jobs/").split('/')
                    require(parts[0].matches(Regex("[0-9a-f-]{36}"))) { "预览任务 ID 无效" }
                    if (parts.size == 1) { expected("GET"); fields(); json(200, NativeFilePreviewRuntime.get(context).job(parts[0])) }
                    else if (parts.size == 2 && parts[1] == "cancel") { expected("POST"); fields(); json(200, NativeFilePreviewRuntime.get(context).cancel(parts[0])) }
                    else json(404, JsonObject().apply { addProperty("error", "预览路由不存在") })
                }
                route.startsWith("/jobs/") -> {
                    val parts = route.removePrefix("/jobs/").split('/')
                    require(parts[0].matches(Regex("[0-9a-f-]{36}"))) { "传输任务 ID 无效" }
                    if (parts.size == 1) { expected("GET"); fields(); json(200, runtime.job(parts[0])) }
                    else if (parts.size == 2 && parts[1] == "cancel") { expected("POST"); fields(); json(200, runtime.cancel(parts[0])) }
                    else json(404, JsonObject().apply { addProperty("error", "传输路由不存在") })
                }
                else -> json(404, JsonObject().apply { addProperty("error", "传输路由不存在") })
            }
        } catch (failure: Exception) {
            val code = when (failure) {
                is FileNotFoundException -> 404
                is SecurityException -> 403
                is IllegalArgumentException, is JsonParseException -> 400
                else -> 409
            }
            val message = when (failure) {
                is IllegalArgumentException, is IllegalStateException, is SecurityException -> failure.message?.take(300) ?: "文件传输失败"
                else -> "文件传输失败，请检查目录授权、空间和网盘连接"
            }
            json(code, JsonObject().apply { addProperty("error", message) })
        }
    }

    private fun json(code: Int, value: JsonObject) = HttpEndpoint.Response(code, "application/json; charset=utf-8", value.toString().toByteArray(),
        "Cache-Control: no-store\r\nX-Content-Type-Options: nosniff\r\n")
}
