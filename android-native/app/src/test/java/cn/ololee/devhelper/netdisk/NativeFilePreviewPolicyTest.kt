package cn.ololee.devhelper.netdisk

import org.junit.Assert.*
import org.junit.Test

class NativeFilePreviewPolicyTest {
    @Test fun activeAndGameSourceFormatsAlwaysRemainPlainText() {
        for (name in listOf("index.html", "vector.svg", "scene.unity", "hero.prefab", "texture.png.meta", "water.shader", "LICENSE", "spine.atlas")) {
            assertEquals(name, "text", NativeFilePreviewPolicy.kind(name, "application/octet-stream"))
        }
        assertEquals("text", NativeFilePreviewPolicy.kind("vector.svg", "image/svg+xml"))
        assertEquals("text", NativeFilePreviewPolicy.kind("page.html", "application/x-executable"))
        assertEquals("unsupported", NativeFilePreviewPolicy.kind("program.exe", "application/octet-stream"))
    }
    @Test fun supportedMediaIncludesCaseInsensitiveNamesAndProviderMimes() {
        assertEquals("image", NativeFilePreviewPolicy.kind("ART.PNG", ""))
        assertEquals("audio", NativeFilePreviewPolicy.kind("recording.m4a", "application/octet-stream"))
        assertEquals("video", NativeFilePreviewPolicy.kind("movie.mp4", ""))
        assertEquals("video", NativeFilePreviewPolicy.kind("untitled", "video/mp4"))
        assertEquals("text", NativeFilePreviewPolicy.kind("untitled", "text/plain; charset=UTF-8"))
        assertEquals("ambiguous", NativeFilePreviewPolicy.kind("segment.ts", "video/mp2t"))
        assertEquals("ambiguous", NativeFilePreviewPolicy.kind("code.ts", "text/plain"))
    }
    @Test fun tsMetadataNeverMistakesAnExtensionGuessedMimeForContentEvidence() {
        for (mime in listOf("video/mp2t", "video/mp2ts", "video/MP2T", "text/plain", "application/typescript", "application/octet-stream", "")) {
            assertEquals(mime, "ambiguous", NativeFilePreviewPolicy.kind("CODE.TS", mime))
        }
        assertEquals("text", NativeFilePreviewPolicy.kind("component.tsx", "video/mp2t"))
    }
    @Test fun typeScriptSourceWinsOverBothAndroidTransportStreamMimeGuesses() {
        val code = "// 中文😀游戏脚本\nexport const scene = { name: 'castle', color: '#47ffaa' };\n"
        for (mime in listOf("video/mp2t", "video/mp2ts")) {
            assertEquals("ambiguous", NativeFilePreviewPolicy.kind("scene.ts", mime))
            val inspected = NativeFilePreviewPolicy.inspectTs(code.toByteArray(Charsets.UTF_8), false)
            assertEquals("text", inspected.kind); assertEquals(code, inspected.text)
        }
        val utf16 = byteArrayOf(0xff.toByte(), 0xfe.toByte()) + code.toByteArray(Charsets.UTF_16LE)
        assertEquals(code, NativeFilePreviewPolicy.inspectTs(utf16, false).text)
    }
    @Test fun truncatedTypeScriptReusesACompleteTextPrefixWithoutCorruptingTheTail() {
        val prefix = "export const name = '你".toByteArray(Charsets.UTF_8).dropLast(1).toByteArray()
        val truncated = NativeFilePreviewPolicy.inspectTs(prefix, true)
        assertEquals("text", truncated.kind); assertEquals("export const name = '", truncated.text)
        assertEquals("unsupported", NativeFilePreviewPolicy.inspectTs(prefix, false).kind)
    }
    private fun realTransportPrefix(): ByteArray {
        // 1,504 bytes generated locally with existing FFmpeg 7.1, not downloaded media:
        // color=c=black:size=32x32:rate=25:duration=0.24 -> mpeg2video -> mpegts, first 8 packets.
        val hex = """
            474011100042f0250001c10000ff01ff0001fc80144812010646466d70656709536572766963653031777c43caffffff
            ffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffff
            ffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffff
            ffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffff47400010
            0000b00d0001c100000001f0002ab104b2ffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffff
            ffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffff
            ffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffff
            ffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffff475000100002b012
            0001c10000e100f00002e100f0009e8b23d1ffffffffffffffffffffffffffffffffffffffffffffffffffffffffffff
            ffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffff
            ffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffff
            ffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffff47410030595000007b0c7e00
            ffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffff
            ffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffff000001e0000080c00a310007f481
            110007d861000001b302002013ffffe018000001b5148a00010000000001b80008004000000100000ffff8000001b58f
            fff341800000010113f87d29488b94a522200000010213f87d29488b94a52220474100318400ffffffffffffffffffff
            ffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffff
            ffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffff
            ffffffffffffffffffffffffffffffffffffffffffffffffff000001e0000080c00a31000910a1110007f48100000100
            0057fffb80000001b5811ff34180000001011279c0000001021279c04741003284100000891c7e00ffffffffffffffff
            ffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffff
            ffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffff
            ffffffffffffffffffffffffffffffffffffffffff000001e0000080c00a3100092cc111000910a1000001000097fffb
            80000001b5811ff34180000001011279c0000001021279c0474000110000b00d0001c100000001f0002ab104b2ffffff
            ffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffff
            ffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffff
            ffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffff
            ffffffffffffffffffffffffffffffffffffffff475000110002b0120001c10000e100f00002e100f0009e8b23d1ffff
            ffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffff
            ffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffff
            ffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffff
            ffffffffffffffffffffffffffffffff
        """.filterNot { it.isWhitespace() }
        require(hex.length == 188 * 8 * 2)
        return ByteArray(hex.length / 2) { index -> hex.substring(index * 2, index * 2 + 2).toInt(16).toByte() }
    }
    @Test fun generatedRealMpegTransportStreamIsVideoRatherThanMislabelledSourceText() {
        val packets = realTransportPrefix()
        assertEquals(188 * 8, packets.size)
        assertNull(NativeFilePreviewPolicy.text(packets, false))
        for (mime in listOf("video/mp2t", "video/mp2ts", "text/plain")) {
            assertEquals("ambiguous", NativeFilePreviewPolicy.kind("recording.ts", mime))
            val inspected = NativeFilePreviewPolicy.inspectTs(packets, false)
            assertEquals("video", inspected.kind); assertNull(inspected.text)
        }
    }
    @Test fun transportSyncDetectionSupports188BytePacketsAnd192And204ByteWrappers() {
        val packets = realTransportPrefix()
        for (stride in listOf(188, 192, 204)) {
            val wrapped = ByteArray(8 * stride)
            val prefix = if (stride == 192) 4 else 0
            for (packet in 0 until 8) packets.copyInto(wrapped, packet * stride + prefix, packet * 188, (packet + 1) * 188)
            assertEquals("stride=$stride", "video", NativeFilePreviewPolicy.inspectTs(wrapped, false).kind)
        }
    }
    @Test fun randomBinaryAndTooFewOrMalformedTransportPacketsRemainUnsupported() {
        val packets = realTransportPrefix()
        val random = ByteArray(4096) { ((it * 31 + 7) % 256).toByte() }
        assertEquals("unsupported", NativeFilePreviewPolicy.inspectTs(random, false).kind)
        assertEquals("unsupported", NativeFilePreviewPolicy.inspectTs(packets.copyOf(188 * 4), false).kind)
        val corruptSync = packets.copyOf().apply { this[188 * 4] = 0 }
        assertEquals("unsupported", NativeFilePreviewPolicy.inspectTs(corruptSync, false).kind)
        val reservedHeader = packets.copyOf().apply { this[3] = 0 }
        assertEquals("unsupported", NativeFilePreviewPolicy.inspectTs(reservedHeader, false).kind)
    }
    @Test fun coincidentalAsciiSyncCharactersDoNotTurnTypeScriptIntoVideo() {
        val plausibleAscii = ByteArray(188 * 8) { 'A'.code.toByte() }
        for (packet in 0 until 8) "GPAT".toByteArray().copyInto(plausibleAscii, packet * 188)
        val result = NativeFilePreviewPolicy.inspectTs(plausibleAscii, false)
        assertEquals("text", result.kind); assertEquals(plausibleAscii.toString(Charsets.UTF_8), result.text)
        assertEquals("text", NativeFilePreviewPolicy.inspectTs(byteArrayOf(), false).kind)
    }
    @Test fun textDecodingSupportsBomAndRejectsBinaryRatherThanShowingCorruption() {
        assertEquals("你好\nUnity", NativeFilePreviewPolicy.text("你好\nUnity".toByteArray(), false))
        assertEquals("你好", NativeFilePreviewPolicy.text(byteArrayOf(0xff.toByte(), 0xfe.toByte()) + "你好".toByteArray(Charsets.UTF_16LE), false))
        assertEquals("hello", NativeFilePreviewPolicy.text(byteArrayOf(0xef.toByte(), 0xbb.toByte(), 0xbf.toByte()) + "hello".toByteArray(), false))
        assertNull(NativeFilePreviewPolicy.text(byteArrayOf(0, 1, 2, 3), false))
        assertNull(NativeFilePreviewPolicy.text(byteArrayOf(0xc3.toByte(), 0x28), false))
    }
    @Test fun partialUtf8CharacterIsIgnoredOnlyWhenThePreviewWasActuallyTruncated() {
        val cut = "A你".toByteArray().dropLast(1).toByteArray()
        assertEquals("A", NativeFilePreviewPolicy.text(cut, true))
        assertNull(NativeFilePreviewPolicy.text(cut, false))
    }
    @Test fun hugeImageDimensionsAreSampledWithoutOverflowAndOriginalIsUntouched() {
        assertEquals(1, NativeFilePreviewPolicy.sampledImageScale(1920, 1080))
        assertEquals(4, NativeFilePreviewPolicy.sampledImageScale(12000, 8000))
        assertTrue(NativeFilePreviewPolicy.sampledImageScale(1_000_000, 1_000_000) > 1)
    }
}
