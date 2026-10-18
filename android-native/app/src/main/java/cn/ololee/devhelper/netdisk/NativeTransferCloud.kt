package cn.ololee.devhelper.netdisk

import com.google.gson.JsonObject
import java.io.File

/** Frozen account, scoped binary data plane. Progress is cumulative bytes for the current file. */
interface NativeTransferCloud {
    fun account(): String
    fun rootPath(): String
    fun stat(path: String): JsonObject?
    fun list(path: String): List<JsonObject>
    /** Preparation can cancel network metadata reads while older/fake stores remain compatible. */
    fun stat(path: String, cancelled: () -> Boolean): JsonObject? = stat(path)
    fun list(path: String, cancelled: () -> Boolean): List<JsonObject> = list(path)
    fun mkdir(path: String): JsonObject
    fun upload(local: File, path: String, progress: (Long) -> Unit, cancelled: () -> Boolean): JsonObject
    fun download(meta: JsonObject, destination: File, progress: (Long) -> Unit, cancelled: () -> Boolean): JsonObject
    fun delete(meta: JsonObject): JsonObject
    /** Success returns the new live metadata; ambiguous outcomes throw and must never be replayed. */
    fun rename(meta: JsonObject, newName: String): JsonObject =
        throw UnsupportedOperationException("当前网盘连接不支持重命名")
    /** Bounded binary text preview; this must never download or persist the unread tail. */
    fun previewPrefix(meta: JsonObject, maxBytes: Int, cancelled: () -> Boolean): ByteArray =
        throw UnsupportedOperationException("当前网盘连接不支持文本前缀预览")
}
