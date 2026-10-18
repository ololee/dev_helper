package cn.ololee.devhelper.netdisk

import android.graphics.Bitmap
import android.graphics.BitmapFactory
import android.graphics.Matrix
import android.media.ExifInterface
import android.media.MediaPlayer
import android.widget.VideoView
import androidx.compose.foundation.Image
import androidx.compose.foundation.background
import androidx.compose.foundation.gestures.detectTapGestures
import androidx.compose.foundation.gestures.rememberTransformableState
import androidx.compose.foundation.gestures.transformable
import androidx.compose.foundation.horizontalScroll
import androidx.compose.foundation.layout.*
import androidx.compose.foundation.lazy.LazyColumn
import androidx.compose.foundation.lazy.itemsIndexed
import androidx.compose.foundation.rememberScrollState
import androidx.compose.foundation.text.selection.SelectionContainer
import androidx.compose.material3.*
import androidx.compose.runtime.*
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.draw.clipToBounds
import androidx.compose.ui.geometry.Offset
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.graphics.asImageBitmap
import androidx.compose.ui.graphics.graphicsLayer
import androidx.compose.ui.input.pointer.pointerInput
import androidx.compose.ui.layout.ContentScale
import androidx.compose.ui.platform.LocalContext
import androidx.compose.ui.platform.LocalView
import androidx.compose.ui.text.font.FontFamily
import androidx.compose.ui.text.style.TextOverflow
import androidx.compose.ui.unit.dp
import androidx.compose.ui.unit.sp
import androidx.compose.ui.viewinterop.AndroidView
import androidx.lifecycle.Lifecycle
import androidx.lifecycle.LifecycleEventObserver
import androidx.lifecycle.findViewTreeLifecycleOwner
import cn.ololee.devhelper.knowledge.*
import com.google.gson.JsonObject
import kotlinx.coroutines.CancellationException
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.delay
import kotlinx.coroutines.withContext
import java.io.Closeable
import java.util.concurrent.atomic.AtomicLong
import kotlin.math.min

/** The cache is removed only after every player/decoder has released its file lease. */
private class NativePreviewLease(private val runtime: NativeFilePreviewRuntime) {
    private val lifecycle = NativeFilePreviewLease { runtime.close(it) }
    val cancelled get() = lifecycle.cancelled
    val done = AtomicLong(0)
    val total = AtomicLong(0)
    fun accept(value: NativeFilePreviewContent) = lifecycle.accept(value.id)
    fun acquire(): Closeable = lifecycle.acquire()
    fun close() = lifecycle.close()
}

@Composable
internal fun NativeFilePreview(scope: String, files: List<JsonObject>, index: Int, select: (Int) -> Unit, dismiss: () -> Unit) {
    val current = files.getOrNull(index) ?: return
    val context = LocalContext.current.applicationContext
    val runtime = remember(context) { NativeFilePreviewRuntime.get(context) }
    val ref = NativeFileTransferPresentation.text(current, if (scope == "local") "uri" else "path")
    var attempt by remember(scope, ref) { mutableIntStateOf(0) }
    key(scope, ref, attempt) {
        val lease = remember { NativePreviewLease(runtime) }
        var content by remember { mutableStateOf<NativeFilePreviewContent?>(null) }
        var failure by remember { mutableStateOf("") }
        var done by remember { mutableLongStateOf(0) }
        var total by remember { mutableLongStateOf(0) }
        DisposableEffect(lease) { onDispose { lease.close() } }
        LaunchedEffect(lease) {
            try {
                val arguments = JsonObject().apply {
                    addProperty("scope", scope); addProperty(if (scope == "local") "uri" else "path", ref)
                    if (scope == "netdisk" && current.has("fsId")) add("fsId", current.get("fsId"))
                }
                // Accept inside IO: cancellation at dispatcher hand-off must not orphan a completed cache.
                val value = withContext(Dispatchers.IO) {
                    runtime.open(arguments, { bytes, length -> lease.done.set(bytes); lease.total.set(length) }, { lease.cancelled.get() })
                        .also { lease.accept(it) }
                }
                if (!lease.cancelled.get()) content = value
            } catch (stopped: CancellationException) { lease.close(); throw stopped }
            catch (error: Exception) { failure = error.message?.take(400) ?: "无法加载文件预览"; lease.close() }
        }
        LaunchedEffect(lease, content, failure) {
            while (content == null && failure.isBlank()) { done = lease.done.get(); total = lease.total.get(); delay(120) }
        }
        NativeFullPage(NativeFileTransferPresentation.text(current, "name", "文件预览"), dismiss) {
            Column(Modifier.fillMaxSize()) {
                if (files.size > 1) Row(Modifier.fillMaxWidth().padding(horizontal = 12.dp), verticalAlignment = Alignment.CenterVertically, horizontalArrangement = Arrangement.SpaceBetween) {
                    NativeTextButton(onClick = { select(index - 1) }, enabled = index > 0) { Text("上一项") }
                    Text("${index + 1} / ${files.size}", style = MaterialTheme.typography.labelMedium, color = NativeDesignTokens.Muted)
                    NativeTextButton(onClick = { select(index + 1) }, enabled = index < files.lastIndex) { Text("下一项") }
                }
                val value = content
                when {
                    failure.isNotBlank() -> Box(Modifier.weight(1f).fillMaxWidth(), contentAlignment = Alignment.Center) {
                        Column(Modifier.padding(24.dp), horizontalAlignment = Alignment.CenterHorizontally) { Text(failure, color = MaterialTheme.colorScheme.error); NativeTextButton(onClick = { attempt++ }) { Text("重新加载") } }
                    }
                    value == null -> Box(Modifier.weight(1f).fillMaxWidth(), contentAlignment = Alignment.Center) {
                        Column(Modifier.padding(24.dp), horizontalAlignment = Alignment.CenterHorizontally, verticalArrangement = Arrangement.spacedBy(12.dp)) {
                            Text(if (scope == "netdisk") "正在读取网盘预览" else "正在读取文件")
                            if (total > 0) LinearProgressIndicator(progress = { (done.toDouble() / total).toFloat().coerceIn(0f, 1f) }, modifier = Modifier.fillMaxWidth()) else LinearProgressIndicator(Modifier.fillMaxWidth())
                            Text(if (total > 0) "${NativeFileTransferPresentation.bytes(done)} / ${NativeFileTransferPresentation.bytes(total)}" else NativeFileTransferPresentation.bytes(done), style = MaterialTheme.typography.bodySmall, color = NativeDesignTokens.Muted)
                            NativeOutlinedButton(onClick = { lease.close(); dismiss() }) { Text("取消加载") }
                        }
                    }
                    value.kind == "text" -> NativePreviewText(value, Modifier.weight(1f))
                    value.kind == "image" -> NativePreviewImage(value, lease, Modifier.weight(1f))
                    value.kind == "video" -> NativePreviewVideo(value, lease, Modifier.weight(1f))
                    value.kind == "audio" -> NativePreviewAudio(value, lease, Modifier.weight(1f))
                    else -> Box(Modifier.weight(1f).fillMaxWidth(), contentAlignment = Alignment.Center) {
                        Column(Modifier.padding(24.dp), verticalArrangement = Arrangement.spacedBy(8.dp)) { Text("此格式暂不支持内置预览"); Text("${value.mimeType.ifBlank { "未知文件类型" }} · ${NativeFileTransferPresentation.bytes(value.size)}", color = NativeDesignTokens.Muted, style = MaterialTheme.typography.bodySmall) }
                    }
                }
                HorizontalDivider()
                Text("只读预览，原文件保留。每次只读取当前一项。", Modifier.fillMaxWidth().padding(horizontal = 16.dp, vertical = 8.dp), color = NativeDesignTokens.Muted, style = MaterialTheme.typography.bodySmall)
            }
        }
    }
}

@Composable
private fun NativePreviewText(content: NativeFilePreviewContent, modifier: Modifier) {
    val lines = remember(content.id) { content.text.orEmpty().split('\n') }
    var wrap by remember(content.id) { mutableStateOf(false) }
    Column(modifier.fillMaxWidth()) {
        Row(Modifier.fillMaxWidth().padding(horizontal = 16.dp), verticalAlignment = Alignment.CenterVertically) {
            Text("${content.mimeType} · ${NativeFileTransferPresentation.bytes(content.size)}", Modifier.weight(1f), style = MaterialTheme.typography.bodySmall, color = NativeDesignTokens.Muted)
            NativeTextButton(onClick = { wrap = !wrap }) { Text(if (wrap) "横向滚动" else "自动换行") }
        }
        if (content.truncated) Text("文件较大，仅预览前 256 KiB；原文件未截断。", Modifier.padding(horizontal = 16.dp, vertical = 4.dp), style = MaterialTheme.typography.bodySmall, color = MaterialTheme.colorScheme.error)
        SelectionContainer(Modifier.weight(1f).fillMaxWidth()) {
            LazyColumn(Modifier.fillMaxSize().background(NativeDesignTokens.Background).then(if (!wrap) Modifier.horizontalScroll(rememberScrollState()) else Modifier), contentPadding = PaddingValues(vertical = 12.dp, horizontal = 8.dp)) {
                itemsIndexed(lines) { number, line -> Row(Modifier.fillMaxWidth().padding(vertical = 1.dp)) {
                    Text("${number + 1}", Modifier.width(48.dp).padding(end = 12.dp), color = NativeDesignTokens.Muted, fontFamily = FontFamily.Monospace, fontSize = 12.sp)
                    Text(line.ifEmpty { " " }, fontFamily = FontFamily.Monospace, fontSize = 13.sp, lineHeight = 20.sp, softWrap = wrap)
                } }
            }
        }
    }
}

private fun decodedPreview(content: NativeFilePreviewContent): Bitmap {
    val path = content.file?.absolutePath ?: error("图片预览文件不存在")
    val bounds = BitmapFactory.Options().apply { inJustDecodeBounds = true }
    BitmapFactory.decodeFile(path, bounds)
    require(bounds.outWidth > 0 && bounds.outHeight > 0) { "此图片格式无法解码" }
    var sampling = 1
    while (bounds.outWidth.toLong() / sampling * (bounds.outHeight / sampling) > 8_000_000L || maxOf(bounds.outWidth, bounds.outHeight) / sampling > 4096) sampling *= 2
    val bitmap = BitmapFactory.decodeFile(path, BitmapFactory.Options().apply { inSampleSize = sampling; inPreferredConfig = Bitmap.Config.ARGB_8888 }) ?: error("图片预览无法解码")
    val rotation = runCatching { ExifInterface(path).getAttributeInt(ExifInterface.TAG_ORIENTATION, ExifInterface.ORIENTATION_NORMAL) }.getOrDefault(ExifInterface.ORIENTATION_NORMAL)
    val matrix = Matrix()
    when (rotation) {
        ExifInterface.ORIENTATION_FLIP_HORIZONTAL -> matrix.setScale(-1f, 1f)
        ExifInterface.ORIENTATION_ROTATE_180 -> matrix.setRotate(180f)
        ExifInterface.ORIENTATION_FLIP_VERTICAL -> matrix.setScale(1f, -1f)
        ExifInterface.ORIENTATION_TRANSPOSE -> { matrix.setRotate(90f); matrix.postScale(-1f, 1f) }
        ExifInterface.ORIENTATION_ROTATE_90 -> matrix.setRotate(90f)
        ExifInterface.ORIENTATION_TRANSVERSE -> { matrix.setRotate(270f); matrix.postScale(-1f, 1f) }
        ExifInterface.ORIENTATION_ROTATE_270 -> matrix.setRotate(270f)
    }
    if (matrix.isIdentity) return bitmap
    return Bitmap.createBitmap(bitmap, 0, 0, bitmap.width, bitmap.height, matrix, true).also { if (it !== bitmap) bitmap.recycle() }
}

@Composable
private fun NativePreviewImage(content: NativeFilePreviewContent, lease: NativePreviewLease, modifier: Modifier) {
    var bitmap by remember(content.id) { mutableStateOf<Bitmap?>(null) }
    var failure by remember(content.id) { mutableStateOf("") }
    var scale by remember(content.id) { mutableFloatStateOf(1f) }
    var offset by remember(content.id) { mutableStateOf(Offset.Zero) }
    LaunchedEffect(content.id) {
        try { bitmap = withContext(Dispatchers.IO) { lease.acquire().use { decodedPreview(content) } } }
        catch (stopped: CancellationException) { throw stopped }
        catch (error: Exception) { failure = error.message ?: "图片预览失败" }
    }
    val gestures = rememberTransformableState { zoom, pan, _ -> scale = (scale * zoom).coerceIn(1f, 8f); offset = if (scale <= 1f) Offset.Zero else offset + pan }
    Column(modifier.fillMaxWidth()) {
        Box(Modifier.weight(1f).fillMaxWidth().background(NativeDesignTokens.Canvas).clipToBounds().transformable(gestures).pointerInput(content.id) { detectTapGestures(onDoubleTap = { scale = if (scale > 1f) 1f else 2.5f; offset = Offset.Zero }) }, contentAlignment = Alignment.Center) {
            val image = bitmap
            if (image != null) Image(image.asImageBitmap(), contentDescription = content.name, modifier = Modifier.fillMaxSize().graphicsLayer { scaleX = scale; scaleY = scale; translationX = offset.x; translationY = offset.y }, contentScale = ContentScale.Fit)
            else if (failure.isNotBlank()) Text(failure, Modifier.padding(24.dp), color = Color.White) else CircularProgressIndicator(color = Color.White)
        }
        Row(Modifier.fillMaxWidth().padding(horizontal = 16.dp), verticalAlignment = Alignment.CenterVertically) { Text("双指缩放与移动，双击放大", Modifier.weight(1f), style = MaterialTheme.typography.bodySmall, color = NativeDesignTokens.Muted); NativeTextButton(onClick = { scale = 1f; offset = Offset.Zero }) { Text("适合屏幕") } }
    }
}

private class NativePreviewPlayback {
    var ready by mutableStateOf(false)
    var playing by mutableStateOf(false)
    var position by mutableFloatStateOf(0f)
    var duration by mutableFloatStateOf(0f)
    var error by mutableStateOf("")
}

@Composable
private fun PausePreviewInBackground(pause: () -> Unit) {
    val owner = LocalView.current.findViewTreeLifecycleOwner()
    val latest by rememberUpdatedState(pause)
    DisposableEffect(owner) {
        val observer = LifecycleEventObserver { _, event -> if (event == Lifecycle.Event.ON_STOP) latest() }
        owner?.lifecycle?.addObserver(observer)
        onDispose { owner?.lifecycle?.removeObserver(observer) }
    }
}

@Composable
private fun NativePreviewAudio(content: NativeFilePreviewContent, lease: NativePreviewLease, modifier: Modifier) {
    val state = remember(content.id) { NativePreviewPlayback() }
    var player by remember(content.id) { mutableStateOf<MediaPlayer?>(null) }
    DisposableEffect(content.id) {
        var hold: Closeable? = null
        var created: MediaPlayer? = null
        try {
            hold = lease.acquire(); val audio = MediaPlayer(); created = audio; player = audio
            audio.setDataSource(content.file?.absolutePath ?: error("音频预览文件不存在"))
            audio.setOnPreparedListener { state.duration = it.duration.toFloat(); state.ready = true }
            audio.setOnCompletionListener { state.playing = false; state.position = state.duration }
            audio.setOnErrorListener { _, _, _ -> state.error = "设备无法播放这段音频"; state.playing = false; true }
            audio.prepareAsync()
        } catch (error: Exception) { state.error = error.message ?: "音频播放失败" }
        onDispose { player = null; runCatching { created?.release() }; hold?.close() }
    }
    PausePreviewInBackground { if (state.ready) runCatching { player?.pause() }; state.playing = false }
    LaunchedEffect(state.playing, state.ready) { while (state.playing && state.ready) { state.position = runCatching { player?.currentPosition?.toFloat() ?: state.position }.getOrDefault(state.position); delay(200) } }
    Column(modifier.fillMaxWidth()) {
        Box(Modifier.fillMaxWidth().weight(1f).background(NativeDesignTokens.Canvas), contentAlignment = Alignment.Center) { Text(if (state.error.isNotBlank()) state.error else if (!state.ready) "正在准备音频…" else "${content.name}\n${previewTime(state.duration)}", Modifier.padding(24.dp), color = Color.White) }
        NativePreviewPlaybackControls(state, seek = { state.position = it; runCatching { player?.seekTo(it.toInt()) } }, toggle = {
            runCatching { if (state.playing) player?.pause() else { if (state.position >= state.duration) { player?.seekTo(0); state.position = 0f }; player?.start() }; state.playing = !state.playing }
                .onFailure { state.error = "播放失败，请重新打开"; state.playing = false }
        })
    }
}

@Composable
private fun NativePreviewVideo(content: NativeFilePreviewContent, lease: NativePreviewLease, modifier: Modifier) {
    val state = remember(content.id) { NativePreviewPlayback() }
    var player by remember(content.id) { mutableStateOf<VideoView?>(null) }
    var aspect by remember(content.id) { mutableFloatStateOf(16f / 9f) }
    var hold by remember(content.id) { mutableStateOf<Closeable?>(null) }
    PausePreviewInBackground { if (state.ready) runCatching { player?.pause() }; state.playing = false }
    LaunchedEffect(state.playing, state.ready) { while (state.playing && state.ready) { state.position = runCatching { player?.currentPosition?.toFloat() ?: state.position }.getOrDefault(state.position); delay(200) } }
    Column(modifier.fillMaxWidth()) {
        BoxWithConstraints(Modifier.fillMaxWidth().weight(1f).background(NativeDesignTokens.Canvas), contentAlignment = Alignment.Center) {
            val width = min(maxWidth.value, maxHeight.value * aspect).dp
            val height = (width.value / aspect).dp
            AndroidView(modifier = Modifier.size(width, height), factory = { context -> VideoView(context).apply {
                try {
                    hold = lease.acquire(); player = this
                    setOnPreparedListener { media -> state.duration = media.duration.toFloat(); state.ready = true; if (media.videoWidth > 0 && media.videoHeight > 0) aspect = media.videoWidth.toFloat() / media.videoHeight; seekTo(1) }
                    setOnCompletionListener { state.playing = false; state.position = state.duration }
                    setOnErrorListener { _, _, _ -> state.error = "设备无法播放这段视频"; state.playing = false; true }
                    setVideoPath(content.file?.absolutePath ?: error("视频预览文件不存在"))
                } catch (error: Exception) { state.error = error.message ?: "视频预览失败"; hold?.close(); hold = null }
            } }, onRelease = { view ->
                // AndroidView release runs even if the surrounding dialog has already requested cache close.
                runCatching { view.stopPlayback() }; player = null; hold?.close(); hold = null
            })
            if (state.error.isNotBlank()) Text(state.error, Modifier.align(Alignment.BottomCenter).padding(20.dp), color = Color.White)
            else if (!state.ready) CircularProgressIndicator(color = Color.White, modifier = Modifier.size(32.dp))
        }
        NativePreviewPlaybackControls(state, seek = { state.position = it; runCatching { player?.seekTo(it.toInt()) } }, toggle = {
            runCatching { if (state.playing) player?.pause() else { if (state.position >= state.duration) { player?.seekTo(0); state.position = 0f }; player?.start() }; state.playing = !state.playing }
                .onFailure { state.error = "播放失败，请重新打开"; state.playing = false }
        })
    }
}

@Composable
private fun NativePreviewPlaybackControls(state: NativePreviewPlayback, seek: (Float) -> Unit, toggle: () -> Unit) {
    var dragging by remember { mutableStateOf(false) }
    var target by remember { mutableFloatStateOf(0f) }
    val duration = state.duration.coerceAtLeast(1f)
    val position = if (dragging) target else state.position
    Column(Modifier.fillMaxWidth().padding(horizontal = 20.dp, vertical = 8.dp)) {
        Slider(value = position.coerceIn(0f, duration), onValueChange = { dragging = true; target = it }, valueRange = 0f..duration, enabled = state.ready && state.error.isBlank(), onValueChangeFinished = { seek(target); dragging = false })
        Row(Modifier.fillMaxWidth(), verticalAlignment = Alignment.CenterVertically, horizontalArrangement = Arrangement.SpaceBetween) {
            Text(previewTime(position), style = MaterialTheme.typography.labelMedium, color = NativeDesignTokens.Muted)
            NativeButton(onClick = toggle, enabled = state.ready && state.error.isBlank()) { Text(if (state.playing) "暂停" else "播放") }
            Text(previewTime(state.duration), style = MaterialTheme.typography.labelMedium, color = NativeDesignTokens.Muted)
        }
    }
}

private fun previewTime(millis: Float): String {
    val seconds = (millis.coerceAtLeast(0f) / 1000).toLong()
    return if (seconds >= 3600) "%d:%02d:%02d".format(seconds / 3600, seconds / 60 % 60, seconds % 60) else "%d:%02d".format(seconds / 60, seconds % 60)
}
