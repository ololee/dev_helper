package cn.ololee.devhelper.netdisk

import java.nio.ByteBuffer
import java.nio.CharBuffer
import java.nio.charset.CodingErrorAction
import java.nio.charset.StandardCharsets
import java.util.Locale

/** Classification is local metadata only; file contents are never executed or sent to a model. */
object NativeFilePreviewPolicy {
    const val TEXT_BYTES = 256 * 1024
    data class TsInspection(val kind: String, val text: String? = null)
    private val texts = setOf("txt", "md", "markdown", "json", "jsonl", "xml", "svg", "yaml", "yml", "toml", "ini", "cfg", "conf", "log", "csv", "tsv", "html", "htm", "css", "js", "mjs", "cjs", "ts", "tsx", "jsx", "py", "java", "kt", "kts", "c", "h", "cpp", "hpp", "cc", "cs", "go", "rs", "sh", "bat", "ps1", "sql", "lua", "gd", "shader", "glsl", "hlsl", "cginc", "compute", "mat", "meta", "unity", "prefab", "asset", "atlas", "fnt", "properties", "gradle", "gitignore", "gitattributes")
    private val images = setOf("png", "jpg", "jpeg", "webp", "gif", "bmp", "heic", "heif", "avif")
    private val audio = setOf("mp3", "m4a", "aac", "wav", "ogg", "opus", "flac", "amr", "3ga")
    private val video = setOf("mp4", "m4v", "mkv", "webm", "mov", "3gp", "ts", "mpeg", "mpg")
    fun kind(name: String, mime: String): String {
        val extension = name.substringAfterLast('.', "").lowercase(Locale.ROOT)
        val normalized = mime.substringBefore(';').trim().lowercase(Locale.ROOT)
        // Android MIME maps guess both TypeScript and transport streams from the same extension.
        // Neither video/mp2t nor video/mp2ts is content evidence; open() resolves a pinned prefix.
        if (extension == "ts") return "ambiguous"
        // SVG/HTML/code are always read as plain text, including a provider's active MIME type.
        if (extension in texts || name.lowercase(Locale.ROOT) in setOf("license", "readme", "cmakelists.txt", "dockerfile", "makefile")) return "text"
        if (extension in images || normalized.startsWith("image/") && normalized != "image/svg+xml") return "image"
        if (extension in audio || normalized.startsWith("audio/")) return "audio"
        if (extension in video || normalized.startsWith("video/")) return "video"
        if (normalized.startsWith("text/") || normalized in setOf("image/svg+xml", "application/json", "application/xml", "application/yaml", "application/javascript")) return "text"
        return "unsupported"
    }

    /** Text wins for .ts sources; binary video requires several complete transport packets. */
    fun inspectTs(prefix: ByteArray, truncated: Boolean): TsInspection {
        text(prefix, truncated)?.let { return TsInspection("text", it) }
        for (stride in intArrayOf(188, 192, 204)) {
            for (offset in 0 until minOf(stride, prefix.size)) {
                val completePackets = if (prefix.size - offset >= 188) 1 + (prefix.size - offset - 188) / stride else 0
                if (completePackets < 5) continue
                val checkedPackets = minOf(32, completePackets)
                var valid = true
                for (packet in 0 until checkedPackets) {
                    val start = offset + packet * stride
                    val sync = prefix[start].toInt() and 255
                    val flags = prefix[start + 1].toInt() and 255
                    val control = prefix[start + 3].toInt() and 255
                    // Reject malformed/error-marked headers and the reserved adaptation control.
                    if (sync != 0x47 || (flags and 0x80) != 0 || (control and 0x30) == 0) { valid = false; break }
                }
                if (valid) return TsInspection("video")
            }
        }
        return TsInspection("unsupported")
    }

    /** Strict UTF-8 and BOM-marked UTF-16; an incomplete tail is allowed only for a truncated prefix. */
    fun text(bytes: ByteArray, truncated: Boolean): String? {
        val utf16le = bytes.size >= 2 && bytes[0] == 0xff.toByte() && bytes[1] == 0xfe.toByte()
        val utf16be = bytes.size >= 2 && bytes[0] == 0xfe.toByte() && bytes[1] == 0xff.toByte()
        val utf8bom = bytes.size >= 3 && bytes[0] == 0xef.toByte() && bytes[1] == 0xbb.toByte() && bytes[2] == 0xbf.toByte()
        val charset = when { utf16le -> StandardCharsets.UTF_16LE; utf16be -> StandardCharsets.UTF_16BE; else -> StandardCharsets.UTF_8 }
        val skip = if (utf16le || utf16be) 2 else if (utf8bom) 3 else 0
        return runCatching {
            val decoder = charset.newDecoder().onMalformedInput(CodingErrorAction.REPORT).onUnmappableCharacter(CodingErrorAction.REPORT)
            val buffer = CharBuffer.allocate(bytes.size + 1)
            val result = decoder.decode(ByteBuffer.wrap(bytes, skip, bytes.size - skip), buffer, !truncated)
            if (result.isError) return null
            if (!truncated && decoder.flush(buffer).isError) return null
            buffer.flip()
            val value = buffer.toString()
            if (value.any { it == '\u0000' || it.code < 0x20 && it !in "\t\r\n" }) null else value
        }.getOrNull()
    }

    fun sampledImageScale(width: Int, height: Int, longestSide: Int = 4096, maxPixels: Long = 8_000_000): Int {
        require(width > 0 && height > 0)
        var sample = 1
        while (width.toLong() / sample > longestSide || height.toLong() / sample > longestSide ||
            width.toLong() / sample * (height.toLong() / sample) > maxPixels) sample *= 2
        return sample
    }
}
