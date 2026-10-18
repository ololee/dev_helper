package cn.ololee.devhelper.netdisk

import com.google.gson.JsonObject
import java.io.File

/** Frozen account, scoped binary data plane. Progress is cumulative bytes for the current file. */
interface NativeTransferCloud {
    fun account(): String
    fun rootPath(): String
    fun stat(path: String): JsonObject?
    fun list(path: String): List<JsonObject>
    fun mkdir(path: String): JsonObject
    fun upload(local: File, path: String, progress: (Long) -> Unit, cancelled: () -> Boolean): JsonObject
    fun download(meta: JsonObject, destination: File, progress: (Long) -> Unit, cancelled: () -> Boolean): JsonObject
    fun delete(meta: JsonObject): JsonObject
}
