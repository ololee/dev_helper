package cn.ololee.devhelper.netdisk

import android.app.Application
import android.content.ClipData
import android.content.Intent
import android.graphics.Canvas
import android.graphics.Paint
import android.graphics.Point
import android.graphics.RectF
import android.graphics.drawable.GradientDrawable
import android.graphics.drawable.Drawable
import android.net.Uri
import android.os.Bundle
import android.view.DragEvent
import android.view.HapticFeedbackConstants
import android.view.View
import android.widget.CheckBox
import android.widget.LinearLayout
import android.widget.ImageView
import android.widget.TextView
import androidx.activity.ComponentActivity
import androidx.activity.compose.BackHandler
import androidx.activity.compose.setContent
import androidx.activity.result.contract.ActivityResultContracts
import androidx.compose.foundation.background
import androidx.compose.foundation.horizontalScroll
import androidx.compose.foundation.layout.*
import androidx.compose.foundation.lazy.LazyColumn
import androidx.compose.foundation.lazy.items
import androidx.compose.foundation.rememberScrollState
import androidx.compose.material3.*
import androidx.compose.runtime.*
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.text.style.TextOverflow
import androidx.compose.ui.unit.dp
import androidx.compose.ui.viewinterop.AndroidView
import androidx.lifecycle.AndroidViewModel
import androidx.lifecycle.ViewModelProvider
import androidx.lifecycle.viewModelScope
import cn.ololee.devhelper.knowledge.*
import com.google.gson.JsonArray
import com.google.gson.JsonObject
import kotlinx.coroutines.CancellationException
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.delay
import kotlinx.coroutines.launch
import kotlinx.coroutines.withContext
import java.text.DateFormat
import java.util.Date
import java.util.UUID

private fun JsonObject.string(key: String, fallback: String = "") = NativeFileTransferPresentation.text(this, key, fallback)
private fun JsonObject.boolean(key: String) = NativeFileTransferPresentation.flag(this, key)
private fun JsonObject.long(key: String) = NativeFileTransferPresentation.number(this, key)
private fun JsonArray?.rows() = NativeFileTransferPresentation.objects(this)
private fun fileKey(scope: String, value: JsonObject) = value.string(if (scope == "local") "uri" else "path")

internal data class NativeFileClipboard(val id: String, val scope: String, val operation: String, val items: List<JsonObject>)
private data class NativeFileDrag(val nonce: String, val clipboard: NativeFileClipboard)

/** Only this process's opaque localState is accepted. Drag text never contains a path or URI. */
private object NativeFileClipboardState {
    var value by mutableStateOf<NativeFileClipboard?>(null)
    var activeDrag: NativeFileDrag? = null
    val jobClipboards = mutableMapOf<String, String>()
    fun drag(event: DragEvent): NativeFileClipboard? = (event.localState as? NativeFileDrag)
        ?.takeIf { it === activeDrag }?.clipboard
    fun observe(job: JsonObject) {
        val current = value ?: return
        if (current.operation != "move" || jobClipboards[job.string("jobId", job.string("id"))] != current.id) return
        if (!NativeFileTransferPresentation.terminal(job)) return
        if (NativeFileTransferPresentation.moveFinished(job)) { value = null; return }
        val remaining = NativeFileTransferPresentation.remainingCutItems(job, current.scope, current.items)
        if (remaining != current.items) value = current.copy(items = remaining).takeIf { it.items.isNotEmpty() }
    }
}

internal data class NativeFilePane(
    val scope: String, val directory: String = "", val parent: String = "", val name: String = "",
    val items: List<JsonObject> = emptyList(), val selected: Set<String> = emptySet(), val loading: Boolean = false,
    val hasMore: Boolean = false, val error: String = ""
)
internal data class NativeFileRenameDraft(val scope: String, val items: List<JsonObject>, val name: String)

/** Runtime owns transfer work; rotation and leaving this screen do not cancel a job. */
internal class NativeFileManagerViewModel(application: Application) : AndroidViewModel(application) {
    private val transfer by lazy { NativeFileTransferRuntime.get(application) }
    private val netdisk by lazy { BaiduNetdiskRuntime.get(application) }
    private val preferences = application.getSharedPreferences("native-file-manager", 0)
    var local by mutableStateOf(NativeFilePane("local")); private set
    var remote by mutableStateOf(NativeFilePane("netdisk")); private set
    var activeScope by mutableStateOf(preferences.getString("scope", "local") ?: "local"); private set
    var dual by mutableStateOf(preferences.getBoolean("dual", true)); private set
    var dragOperation by mutableStateOf("copy"); private set
    var jobs by mutableStateOf<List<JsonObject>>(emptyList()); private set
    var roots by mutableStateOf<List<JsonObject>>(emptyList()); private set
    var pendingPlan by mutableStateOf<JsonObject?>(null); private set
    var planning by mutableStateOf(false); private set
    var notice by mutableStateOf(""); private set
    var configured by mutableStateOf(false); private set
    var renameDraft by mutableStateOf<NativeFileRenameDraft?>(null); private set
    var previewItems by mutableStateOf<List<JsonObject>>(emptyList()); private set
    var previewScope by mutableStateOf("local"); private set
    var previewIndex by mutableStateOf(0); private set
    private var cloudRoot = ""
    private var planClipboard: NativeFileClipboard? = null
    private val generations = mutableMapOf("local" to 0, "netdisk" to 0)
    private var pollErrorShown = false
    init {
        viewModelScope.launch {
            try {
                val status = withContext(Dispatchers.IO) { transfer.status() }
                roots = status.getAsJsonArray("roots").rows()
                val cloud = withContext(Dispatchers.IO) { netdisk.status() }
                configured = cloud.boolean("configured"); cloudRoot = cloud.string("remoteRoot")
                load("local", preferences.getString("local-directory", null) ?: status.string("defaultUri"))
                if (configured) load("netdisk", preferences.getString("cloud-directory", null)?.takeIf { it == cloudRoot || it.startsWith("$cloudRoot/") } ?: cloudRoot)
                else remote = remote.copy(error = "先连接百度网盘账号，再与手机目录互传文件。")
            } catch (failure: Exception) { notice = failure.message ?: "无法读取文件目录"; load("local", "") }
        }
        viewModelScope.launch {
            while (true) {
                try {
                    val result = withContext(Dispatchers.IO) { transfer.jobs() }
                    val previous = jobs.associateBy { it.string("jobId", it.string("id")) }
                    val nextJobs = result.getAsJsonArray("jobs").rows().sortedByDescending { it.long("createdAt") }
                    if (jobs.toString() != nextJobs.toString()) jobs = nextJobs
                    jobs.forEach(NativeFileClipboardState::observe)
                    if (jobs.any { NativeFileTransferPresentation.terminal(it) && previous[it.string("jobId", it.string("id"))]?.let { old -> !NativeFileTransferPresentation.terminal(old) } == true }) refresh()
                    pollErrorShown = false
                } catch (stopped: CancellationException) { throw stopped }
                catch (failure: Exception) { if (!pollErrorShown) { notice = failure.message ?: "传输状态暂时无法读取"; pollErrorShown = true } }
                delay(800)
            }
        }
    }
    fun pane(scope: String) = if (scope == "local") local else remote
    private fun replace(value: NativeFilePane) { if (value.scope == "local") local = value else remote = value }
    fun activate(scope: String) { activeScope = scope; preferences.edit().putString("scope", scope).apply() }
    fun toggleDual() { dual = !dual; preferences.edit().putBoolean("dual", dual).apply() }
    fun dragMode(operation: String) { dragOperation = operation }
    fun dismissNotice() { notice = "" }
    fun refresh() {
        load("local", local.directory)
        viewModelScope.launch {
            try {
                val cloud = withContext(Dispatchers.IO) { netdisk.status() }
                configured = cloud.boolean("configured"); cloudRoot = cloud.string("remoteRoot")
                if (configured) load("netdisk", remote.directory.takeIf { it == cloudRoot || it.startsWith("$cloudRoot/") } ?: cloudRoot)
                else remote = remote.copy(items = emptyList(), selected = emptySet(), error = "先连接百度网盘账号，再与手机目录互传文件。")
            } catch (failure: Exception) { remote = remote.copy(error = failure.message ?: "无法读取百度网盘状态") }
        }
    }
    fun load(scope: String, directory: String, more: Boolean = false) {
        val before = pane(scope)
        if (more && before.loading) return
        val generation = (generations[scope] ?: 0) + 1; generations[scope] = generation
        replace(before.copy(loading = true, error = ""))
        viewModelScope.launch {
            try {
                val offset = if (more) before.items.size else 0
                val result = withContext(Dispatchers.IO) {
                    if (scope == "local") transfer.localFiles(JsonObject().apply { if (directory.isNotBlank()) addProperty("pathUri", directory); addProperty("offset", offset); addProperty("limit", 100) })
                    else transfer.cloudFiles(JsonObject().apply { addProperty("path", directory); addProperty("offset", offset); addProperty("limit", 100) })
                }
                if (generations[scope] != generation) return@launch
                val current = if (scope == "local") result.string("pathUri", directory) else directory
                val parent = if (scope == "local") result.string("parentUri") else if (current != cloudRoot) current.substringBeforeLast('/').takeIf { it == cloudRoot || it.startsWith("$cloudRoot/") }.orEmpty() else ""
                val entries = result.getAsJsonArray(if (scope == "local") "items" else "files").rows()
                val values = (if (more) before.items + entries else entries).distinctBy { fileKey(scope, it) }
                val same = before.directory == current
                replace(NativeFilePane(scope, current, parent, if (scope == "local") result.string("name", "手机文件") else if (current == cloudRoot) "百度网盘" else current.substringAfterLast('/'), values,
                    if (same) before.selected.intersect(values.map { fileKey(scope, it) }.toSet()) else emptySet(), false, result.boolean("hasMore")))
                preferences.edit().putString(if (scope == "local") "local-directory" else "cloud-directory", current).apply()
            } catch (stopped: CancellationException) { throw stopped }
            catch (failure: Exception) {
                if (generations[scope] == generation) replace(pane(scope).copy(loading = false, error = failure.message ?: "目录加载失败"))
            }
        }
    }
    fun registerTree(uri: Uri) {
        viewModelScope.launch {
            try { withContext(Dispatchers.IO) { transfer.registerTree(uri) }; roots = withContext(Dispatchers.IO) { transfer.status() }.getAsJsonArray("roots").rows(); load("local", uri.toString()); activate("local") }
            catch (failure: Exception) { notice = failure.message ?: "无法打开所选文件夹" }
        }
    }
    fun toggle(scope: String, file: JsonObject) {
        activate(scope); val value = pane(scope); val key = fileKey(scope, file)
        if (key !in value.selected && value.selected.size >= 100) { notice = "每批最多选择 100 项，文件夹内的文件会一起传输"; return }
        replace(value.copy(selected = if (key in value.selected) value.selected - key else value.selected + key))
    }
    fun selectAll() { val value = pane(activeScope); replace(value.copy(selected = value.items.take(100).map { fileKey(activeScope, it) }.toSet())); if (value.items.size > 100) notice = "已选择前 100 项" }
    fun clearSelection() { replace(pane(activeScope).copy(selected = emptySet())) }
    fun selectedItems(scope: String = activeScope) = pane(scope).let { value -> value.items.filter { fileKey(scope, it) in value.selected } }
    fun showRename(scope: String = activeScope, files: List<JsonObject> = selectedItems(scope)) {
        if (files.isEmpty()) return
        notice = ""; renameDraft = NativeFileRenameDraft(scope, files.map { it.deepCopy() }, if (files.size == 1) files.single().string("name") else "{name}_{index}{ext}")
    }
    fun renameText(value: String) { renameDraft = renameDraft?.copy(name = value) }
    fun closeRename() { if (!planning) renameDraft = null }
    fun planRename() { renameDraft?.let { mutation(it.scope, "rename", it.items, it.name) } }
    fun mutation(scope: String, operation: String, files: List<JsonObject>, name: String? = null) {
        if (planning || pendingPlan != null || files.isEmpty()) return
        planning = true; notice = ""; activate(scope)
        viewModelScope.launch {
            try {
                val request = JsonObject().apply {
                    addProperty("scope", scope); addProperty("operation", operation)
                    if (name != null) addProperty("name", name)
                    add("items", JsonArray().apply { files.forEach { original -> add(JsonObject().apply {
                        if (scope == "local") addProperty("uri", original.string("uri")) else { addProperty("path", original.string("path")); if (original.has("fsId")) add("fsId", original.get("fsId")) }
                    }) } })
                }
                pendingPlan = withContext(Dispatchers.IO) { transfer.plan(request) }; planClipboard = null; renameDraft = null
            } catch (failure: Exception) { notice = failure.message ?: "无法建立文件操作计划" }
            finally { planning = false }
        }
    }
    fun preview(scope: String = activeScope, files: List<JsonObject> = selectedItems(scope)) {
        val selected = files.filterNot { it.boolean("isDirectory") }.map { it.deepCopy() }
        if (selected.isEmpty()) { notice = "请先选择要预览的文件"; return }
        activate(scope); previewScope = scope; previewIndex = 0; previewItems = selected
    }
    fun previewAt(index: Int) { previewIndex = index.coerceIn(0, (previewItems.size - 1).coerceAtLeast(0)) }
    fun closePreview() { previewItems = emptyList(); previewIndex = 0 }
    fun clipboard(operation: String) {
        val value = pane(activeScope); val files = value.items.filter { fileKey(activeScope, it) in value.selected }
        if (files.isNotEmpty()) { NativeFileClipboardState.value = NativeFileClipboard(UUID.randomUUID().toString(), activeScope, operation, files.map { it.deepCopy() }); notice = "已${if (operation == "move") "剪切" else "复制"} ${files.size} 项，请在另一端目录粘贴" }
    }
    fun drag(scope: String, file: JsonObject): NativeFileClipboard {
        activate(scope)
        val value = pane(scope); val key = fileKey(scope, file)
        val files = if (key in value.selected) value.items.filter { fileKey(scope, it) in value.selected } else listOf(file)
        replace(value.copy(selected = files.map { fileKey(scope, it) }.toSet()))
        return NativeFileClipboard(UUID.randomUUID().toString(), scope, dragOperation, files.map { it.deepCopy() })
    }
    fun paste(scope: String, directory: String = pane(scope).directory, source: NativeFileClipboard? = NativeFileClipboardState.value) {
        if (planning || pendingPlan != null || source == null) return
        if (source.scope == scope) { notice = "请选择另一端的目标目录：手机与百度网盘之间互传"; return }
        if (directory.isBlank()) { notice = "请先打开目标目录"; return }
        planning = true; activate(scope)
        viewModelScope.launch {
            try {
                val request = JsonObject().apply {
                    addProperty("scope", source.scope); addProperty("destinationScope", scope); addProperty("operation", source.operation)
                    add("items", JsonArray().apply { source.items.forEach { original -> add(JsonObject().apply {
                        if (source.scope == "local") addProperty("uri", original.string("uri")) else { addProperty("path", original.string("path")); if (original.has("fsId")) add("fsId", original.get("fsId")) }
                    }) } })
                    add("destination", JsonObject().apply { addProperty(if (scope == "local") "uri" else "path", directory) })
                }
                pendingPlan = withContext(Dispatchers.IO) { transfer.plan(request) }; planClipboard = source
            } catch (failure: Exception) { notice = failure.message ?: "无法建立传输计划" }
            finally { planning = false }
        }
    }
    fun closePlan() { if (!planning) { pendingPlan = null; planClipboard = null } }
    fun executePlan() {
        val plan = pendingPlan ?: return; val clipboard = planClipboard
        if (planning) return
        planning = true
        viewModelScope.launch {
            try {
                val job = withContext(Dispatchers.IO) { transfer.execute(JsonObject().apply { addProperty("planId", plan.string("planId")); addProperty("planDigest", plan.string("planDigest")) }) }
                if (clipboard != null) NativeFileClipboardState.jobClipboards[job.string("jobId", job.string("id"))] = clipboard.id
                jobs = listOf(job) + jobs.filterNot { it.string("jobId", it.string("id")) == job.string("jobId", job.string("id")) }
                pendingPlan = null; planClipboard = null; notice = "已加入后台任务，可离开此页"
            } catch (failure: Exception) { notice = failure.message ?: "文件操作未启动" }
            finally { planning = false }
        }
    }
    fun cancel(job: JsonObject) {
        viewModelScope.launch {
            try { val updated = withContext(Dispatchers.IO) { transfer.cancel(job.string("jobId", job.string("id"))) }; jobs = jobs.map { if (it.string("jobId", it.string("id")) == updated.string("jobId", updated.string("id"))) updated else it }; notice = if (job.string("operation") in setOf("rename", "trash")) "已请求取消；当前单项操作需等待回执，请查看逐项结果" else "已请求取消；已写入的目标文件会在结果中列出" }
            catch (failure: Exception) { notice = failure.message ?: "取消请求未完成" }
        }
    }
}

/** Compact native two-pane explorer; transfers run directly on the phone. */
class NativeFileManagerActivity : ComponentActivity() {
    private lateinit var model: NativeFileManagerViewModel
    private val folderPicker = registerForActivityResult(ActivityResultContracts.OpenDocumentTree()) { uri ->
        if (uri != null) runCatching {
            contentResolver.takePersistableUriPermission(uri, Intent.FLAG_GRANT_READ_URI_PERMISSION or Intent.FLAG_GRANT_WRITE_URI_PERMISSION)
            model.registerTree(uri)
        }.onFailure { android.widget.Toast.makeText(this, it.message ?: "文件夹授权未完成", android.widget.Toast.LENGTH_LONG).show() }
    }
    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        model = ViewModelProvider(this)[NativeFileManagerViewModel::class.java]
        setContent { NativeDesignTheme { FileScreen() } }
    }
    override fun onResume() { super.onResume(); if (::model.isInitialized) model.refresh() }
    @Composable private fun FileScreen() {
        var showJobs by remember { mutableStateOf(false) }
        var showRoots by remember { mutableStateOf(false) }
        val clipboard = NativeFileClipboardState.value
        val active = model.pane(model.activeScope)
        BackHandler(enabled = active.selected.isNotEmpty()) { model.clearSelection() }
        Surface(Modifier.fillMaxSize().windowInsetsPadding(WindowInsets.safeDrawing), color = NativeDesignTokens.Background) {
            Column(Modifier.fillMaxSize()) {
                Row(Modifier.fillMaxWidth().padding(horizontal = 8.dp), verticalAlignment = Alignment.CenterVertically) {
                    NativeTextButton(onClick = { finish() }) { Text("返回") }
                    Text("本地与网盘", Modifier.weight(1f), style = MaterialTheme.typography.titleLarge)
                    NativeTextButton(onClick = { showJobs = true }) { Text("任务${model.jobs.count { !NativeFileTransferPresentation.terminal(it) }.let { if (it > 0) " $it" else "" }}") }
                    NativeOverflow("文件管理", listOf(
                        NativeMenuAction(if (model.dual) "切换单栏" else "切换双栏") { model.toggleDual() },
                        NativeMenuAction("选择手机文件夹") { folderPicker.launch(null) },
                        NativeMenuAction("已授权文件夹") { showRoots = true },
                        NativeMenuAction("百度账号设置") { startActivity(Intent(this@NativeFileManagerActivity, BaiduNetdiskActivity::class.java)) },
                        NativeMenuAction("刷新两个目录") { model.refresh() }
                    ))
                }
                NativeSectionTabs(listOf("local" to "手机文件", "netdisk" to "百度网盘"), model.activeScope, model::activate)
                Row(Modifier.fillMaxWidth().horizontalScroll(rememberScrollState()).padding(horizontal = 8.dp), verticalAlignment = Alignment.CenterVertically) {
                    Text("已选 ${active.selected.size}", Modifier.padding(horizontal = 8.dp), style = MaterialTheme.typography.labelMedium, color = NativeDesignTokens.Muted)
                    NativeTextButton(onClick = { model.clipboard("copy") }, enabled = active.selected.isNotEmpty()) { Text("复制") }
                    NativeTextButton(onClick = { model.clipboard("move") }, enabled = active.selected.isNotEmpty()) { Text("剪切") }
                    NativeTextButton(onClick = { model.paste(model.activeScope) }, enabled = clipboard != null && clipboard.scope != model.activeScope && active.directory.isNotBlank() && !model.planning) { Text("粘贴") }
                    NativeTextButton(onClick = { model.preview() }, enabled = model.selectedItems().any { !it.boolean("isDirectory") }) { Text("预览所选") }
                    NativeTextButton(onClick = { model.showRename() }, enabled = active.selected.isNotEmpty() && !model.planning) { Text("重命名") }
                    NativeTextButton(onClick = { model.mutation(model.activeScope, "trash", model.selectedItems()) }, enabled = active.selected.isNotEmpty() && !model.planning) { Text("删除", color = MaterialTheme.colorScheme.error) }
                    NativeTextButton(onClick = model::selectAll, enabled = active.items.isNotEmpty()) { Text("全选") }
                    if (active.selected.isNotEmpty()) NativeTextButton(onClick = model::clearSelection) { Text("取消选择") }
                    NativeOverflow("拖拽方式", listOf(NativeMenuAction("拖拽时复制") { model.dragMode("copy") }, NativeMenuAction("拖拽时移动") { model.dragMode("move") }), buttonLabel = if (model.dragOperation == "copy") "拖拽：复制" else "拖拽：移动")
                }
                if (clipboard != null) Surface(color = NativeDesignTokens.SoftAccent) {
                    Row(Modifier.fillMaxWidth().padding(start = 16.dp), verticalAlignment = Alignment.CenterVertically) {
                        val reviewCount = clipboard.items.count { it.boolean("needsSourceReview") }
                        Text("${if (clipboard.scope == "local") "手机" else "网盘"} ${clipboard.items.size} 项待${if (clipboard.operation == "move") "移动" else "复制"}" + if (reviewCount > 0) " · $reviewCount 项源状态待核对" else "", Modifier.weight(1f), style = MaterialTheme.typography.bodySmall, color = NativeDesignTokens.Accent)
                        NativeTextButton(onClick = { NativeFileClipboardState.value = null }) { Text("清除") }
                    }
                }
                model.jobs.firstOrNull { !NativeFileTransferPresentation.terminal(it) }?.let { job -> TransferSummary(job, { showJobs = true }) }
                if (model.notice.isNotBlank()) Row(Modifier.fillMaxWidth().padding(start = 16.dp), verticalAlignment = Alignment.CenterVertically) {
                    Text(model.notice, Modifier.weight(1f), style = MaterialTheme.typography.bodySmall, color = NativeDesignTokens.Muted, maxLines = 3)
                    NativeTextButton(onClick = model::dismissNotice) { Text("知道了") }
                }
                if (model.planning) LinearProgressIndicator(Modifier.fillMaxWidth())
                BoxWithConstraints(Modifier.weight(1f)) {
                    if (!model.dual) FilePane(model.pane(model.activeScope), Modifier.fillMaxSize())
                    else if (maxWidth >= 600.dp) Row(Modifier.fillMaxSize()) { FilePane(model.local, Modifier.weight(1f)); VerticalDivider(); FilePane(model.remote, Modifier.weight(1f)) }
                    else Column(Modifier.fillMaxSize()) { FilePane(model.local, Modifier.weight(1f)); HorizontalDivider(thickness = 4.dp, color = NativeDesignTokens.Line); FilePane(model.remote, Modifier.weight(1f)) }
                }
            }
        }
        model.renameDraft?.let { draft -> AlertDialog(onDismissRequest = model::closeRename, title = { Text(if (draft.items.size == 1) "重命名" else "批量重命名 ${draft.items.size} 项") }, text = {
            Column(verticalArrangement = Arrangement.spacedBy(8.dp)) {
                OutlinedTextField(draft.name, model::renameText, label = { Text(if (draft.items.size == 1) "新名称" else "名称模板") }, modifier = Modifier.fillMaxWidth(), singleLine = true, enabled = !model.planning)
                if (draft.items.size > 1) Text("{name} 和 {stem} 为去扩展名的名称，{ext} 为含点的扩展名，{index} 从 1 起。例如 {name}_{index}{ext}。下一步检查实际名称后再执行。", style = MaterialTheme.typography.bodySmall, color = NativeDesignTokens.Muted)
                else Text("扩展名也是名称的一部分，请保留需要的文件格式。", style = MaterialTheme.typography.bodySmall, color = NativeDesignTokens.Muted)
                if (model.notice.isNotBlank()) Text(model.notice, style = MaterialTheme.typography.bodySmall, color = MaterialTheme.colorScheme.error)
                if (model.planning) LinearProgressIndicator(Modifier.fillMaxWidth())
            }
        }, confirmButton = { NativeButton(onClick = model::planRename, enabled = !model.planning && draft.name.isNotBlank()) { Text("预览名称") } }, dismissButton = { NativeTextButton(onClick = model::closeRename, enabled = !model.planning) { Text("取消") } }) }
        model.pendingPlan?.let { plan -> AlertDialog(onDismissRequest = model::closePlan, title = { Text("${NativeFileTransferPresentation.operationName(plan.string("operation"))}这些项目？") }, text = {
            Column(verticalArrangement = Arrangement.spacedBy(8.dp)) {
                val operation = plan.string("operation")
                val mutation = operation in setOf("rename", "trash")
                val scope = plan.string("sourceScope", plan.string("scope"))
                Text(if (mutation) if (scope == "local") "手机文件" else "百度网盘" else if (scope == "local") "手机 → 百度网盘" else "百度网盘 → 手机", style = MaterialTheme.typography.titleMedium)
                val targetName = plan.string("destinationName").ifBlank { "所选文件夹" }
                if (!mutation) Text("目标：$targetName", style = MaterialTheme.typography.bodyMedium, fontWeight = FontWeight.Medium)
                Text(if (mutation) "共 ${plan.getAsJsonArray("items")?.size() ?: 0} 项" else "共 ${plan.long("fileCount")} 个文件 · ${NativeFileTransferPresentation.bytes(if (plan.has("bytes")) plan.long("bytes") else plan.long("totalBytes"))}")
                LazyColumn(Modifier.heightIn(max = 180.dp)) { items(plan.getAsJsonArray("items").rows()) { item ->
                    val name = item.string("sourceName", item.string("name", "文件"))
                    Column(Modifier.padding(vertical = 3.dp)) {
                        Text(name, maxLines = 1, overflow = TextOverflow.Ellipsis)
                        if (operation == "rename") Text("改为 ${item.string("targetName", item.string("destinationName", "请检查目标名称"))}", style = MaterialTheme.typography.bodySmall, color = NativeDesignTokens.Accent, maxLines = 2, overflow = TextOverflow.Ellipsis)
                        else if (!mutation) Text("存入 $targetName/$name", style = MaterialTheme.typography.bodySmall, color = NativeDesignTokens.Muted, maxLines = 1, overflow = TextOverflow.Ellipsis)
                    }
                } }
                Text(when (operation) {
                    "trash" -> if (scope == "local") "手机文件将从原目录永久删除，文件夹内的内容也会删除。此操作无法在 DevHelper 中撤销，请确认已保留需要的备份。" else "网盘文件及文件夹内容将删除，回收与恢复按百度网盘的平台规则处理。请确认已保留需要的备份。"
                    "rename" -> "按以上实际名称执行；同名目标不会覆盖。可确认的同级 .meta 会一起处理，部分完成会逐项显示。"
                    "move" -> "整批传输并校验成功后才处理源文件。失败或取消的未完成项目保留源文件，同名目标不会覆盖。"
                    else -> "保留目录结构，同名目标不会覆盖。"
                }, style = MaterialTheme.typography.bodySmall, color = if (operation == "trash") MaterialTheme.colorScheme.error else NativeDesignTokens.Muted)
            }
        }, confirmButton = { NativeButton(onClick = model::executePlan, enabled = !model.planning) { Text("确认${NativeFileTransferPresentation.operationName(plan.string("operation"))}") } }, dismissButton = { NativeTextButton(onClick = model::closePlan, enabled = !model.planning) { Text("取消") } }) }
        if (showRoots) AlertDialog(onDismissRequest = { showRoots = false }, title = { Text("手机文件夹") }, text = { Column {
            model.roots.forEach { root -> NativeTextButton(onClick = { showRoots = false; model.activate("local"); model.load("local", root.string("uri")) }, enabled = root.boolean("readable")) { Text(root.string("name", "手机文件")) } }
            NativeTextButton(onClick = { showRoots = false; folderPicker.launch(null) }) { Text("选择其他文件夹…") }
        } }, confirmButton = { NativeTextButton(onClick = { showRoots = false }) { Text("关闭") } })
        if (showJobs) TransferJobs { showJobs = false }
        if (model.previewItems.isNotEmpty()) NativeFilePreview(model.previewScope, model.previewItems, model.previewIndex, model::previewAt, model::closePreview)
    }
    @Composable private fun FilePane(pane: NativeFilePane, modifier: Modifier) {
        Column(modifier.background(NativeDesignTokens.Surface)) {
            Row(Modifier.fillMaxWidth().heightIn(min = 48.dp).padding(horizontal = 8.dp), verticalAlignment = Alignment.CenterVertically) {
                NativeTextButton(onClick = { model.activate(pane.scope); model.load(pane.scope, pane.parent) }, enabled = pane.parent.isNotBlank() && !pane.loading) { Text("上一级") }
                Column(Modifier.weight(1f)) {
                    Text(pane.name.ifBlank { if (pane.scope == "local") "手机文件" else "百度网盘" }, style = MaterialTheme.typography.titleSmall, maxLines = 1, overflow = TextOverflow.Ellipsis)
                    Text(if (pane.scope == "local") "手机文件夹" else "网盘应用目录", style = MaterialTheme.typography.bodySmall, color = NativeDesignTokens.Muted)
                }
                NativeTextButton(onClick = { model.activate(pane.scope); model.load(pane.scope, pane.directory) }, enabled = !pane.loading && pane.directory.isNotBlank()) { Text("刷新") }
            }
            NativeDropTarget(pane.scope, pane.directory, model.activeScope == pane.scope, enabled = pane.directory.isNotBlank(), activate = { model.activate(pane.scope) }, drop = { model.paste(pane.scope, pane.directory, it) })
            if (pane.loading) LinearProgressIndicator(Modifier.fillMaxWidth())
            if (pane.error.isNotBlank()) Column(Modifier.padding(horizontal = 16.dp, vertical = 8.dp)) {
                Text(pane.error, color = MaterialTheme.colorScheme.error, style = MaterialTheme.typography.bodySmall)
                if (pane.scope == "netdisk") NativeTextButton(onClick = { startActivity(Intent(this@NativeFileManagerActivity, BaiduNetdiskActivity::class.java)) }) { Text("网盘账号设置") }
                else NativeTextButton(onClick = { folderPicker.launch(null) }) { Text("重新选择文件夹") }
            }
            LazyColumn(Modifier.fillMaxWidth().weight(1f), contentPadding = PaddingValues(bottom = 8.dp)) {
                if (pane.items.isEmpty() && !pane.loading && pane.error.isBlank()) item { Text("此文件夹为空，可将另一端的文件粘贴或拖到上方目录栏。", Modifier.padding(16.dp), color = NativeDesignTokens.Muted, style = MaterialTheme.typography.bodySmall) }
                items(pane.items, key = { fileKey(pane.scope, it) }) { file -> NativeFileRow(file, pane.selected.contains(fileKey(pane.scope, file)),
                    toggle = { model.toggle(pane.scope, file) },
                    open = { if (pane.selected.isNotEmpty()) model.toggle(pane.scope, file) else if (file.boolean("isDirectory")) { model.activate(pane.scope); model.load(pane.scope, fileKey(pane.scope, file)) } else model.preview(pane.scope, listOf(file)) },
                    menu = { action -> when (action) {
                        "rename" -> model.showRename(pane.scope, listOf(file))
                        "trash" -> model.mutation(pane.scope, "trash", listOf(file))
                        "preview" -> model.preview(pane.scope, listOf(file))
                        "select" -> model.toggle(pane.scope, file)
                    } },
                    drag = { model.drag(pane.scope, file) },
                    canDrop = { it.scope != pane.scope && file.boolean("isDirectory") },
                    drop = { model.paste(pane.scope, fileKey(pane.scope, file), it) }) }
                if (pane.hasMore) item { NativeTextButton(onClick = { model.load(pane.scope, pane.directory, more = true) }, enabled = !pane.loading, modifier = Modifier.fillMaxWidth()) { Text("加载更多") } }
            }
        }
    }
    @Composable private fun TransferSummary(job: JsonObject, open: () -> Unit) {
        Surface(color = NativeDesignTokens.SoftAccent, onClick = open) {
            Column(Modifier.fillMaxWidth().padding(horizontal = 16.dp, vertical = 8.dp)) {
                Row(verticalAlignment = Alignment.CenterVertically) {
                    Text(NativeFileTransferPresentation.stage(job), Modifier.weight(1f), style = MaterialTheme.typography.labelLarge, color = NativeDesignTokens.Accent)
                    if (!job.boolean("cancelRequested") && job.boolean("cancelSupported")) NativeTextButton(onClick = { model.cancel(job) }) { Text("取消") }
                }
                LinearProgressIndicator(progress = { NativeFileTransferPresentation.progress(job) }, modifier = Modifier.fillMaxWidth())
                val progress = job.getAsJsonObject("progress") ?: JsonObject()
                Text(NativeFileTransferPresentation.progressSummary(job), style = MaterialTheme.typography.bodySmall, color = NativeDesignTokens.Muted)
                if (progress.string("currentFile").isNotBlank()) Text(progress.string("currentFile").substringAfterLast('/'), style = MaterialTheme.typography.bodySmall, maxLines = 1, overflow = TextOverflow.Ellipsis)
            }
        }
    }
    @OptIn(ExperimentalMaterial3Api::class)
    @Composable private fun TransferJobs(close: () -> Unit) {
        ModalBottomSheet(onDismissRequest = close, containerColor = NativeDesignTokens.Surface) {
            Row(Modifier.fillMaxWidth().padding(horizontal = 20.dp), verticalAlignment = Alignment.CenterVertically) { Text("文件任务", Modifier.weight(1f), style = MaterialTheme.typography.titleLarge); NativeTextButton(onClick = close) { Text("收起") } }
            Text("收起或离开此页不会停止后台任务。", Modifier.padding(horizontal = 20.dp, vertical = 4.dp), style = MaterialTheme.typography.bodySmall, color = NativeDesignTokens.Muted)
            LazyColumn(Modifier.fillMaxWidth().heightIn(max = 560.dp), contentPadding = PaddingValues(20.dp)) {
                if (model.jobs.isEmpty()) item { Text("还没有传输任务。在手机与网盘之间复制、粘贴或长按拖动文件即可开始。", style = MaterialTheme.typography.bodyMedium) }
                items(model.jobs.take(50), key = { it.string("jobId", it.string("id")) }) { job ->
                    var expanded by remember(job.string("jobId", job.string("id"))) { mutableStateOf(false) }
                    Column(Modifier.fillMaxWidth().padding(vertical = 10.dp)) {
                        Row(verticalAlignment = Alignment.CenterVertically) {
                            Column(Modifier.weight(1f)) {
                                Text(NativeFileTransferPresentation.jobTitle(job), style = MaterialTheme.typography.titleSmall)
                                Text(NativeFileTransferPresentation.stage(job), style = MaterialTheme.typography.bodySmall, color = NativeDesignTokens.Muted)
                            }
                            if (!NativeFileTransferPresentation.terminal(job) && job.boolean("cancelSupported")) NativeTextButton(onClick = { model.cancel(job) }, enabled = !job.boolean("cancelRequested")) { Text(if (job.boolean("cancelRequested")) "取消中" else "取消") }
                        }
                        LinearProgressIndicator(progress = { NativeFileTransferPresentation.progress(job) }, modifier = Modifier.fillMaxWidth().padding(vertical = 6.dp))
                        val progress = job.getAsJsonObject("progress") ?: JsonObject()
                        Text(NativeFileTransferPresentation.progressSummary(job), style = MaterialTheme.typography.bodySmall)
                        progress.string("currentFile").takeIf { it.isNotBlank() }?.let { Text(it.substringAfterLast('/'), maxLines = 1, overflow = TextOverflow.Ellipsis, style = MaterialTheme.typography.bodySmall, color = NativeDesignTokens.Muted) }
                        job.string("error").takeIf { it.isNotBlank() }?.let { Text(it, color = MaterialTheme.colorScheme.error, style = MaterialTheme.typography.bodySmall) }
                        val result = job.getAsJsonObject("result") ?: job
                        val results = result.getAsJsonArray("items").rows().ifEmpty { job.getAsJsonArray("items").rows() }
                        if (results.isNotEmpty()) NativeTextButton(onClick = { expanded = !expanded }) { Text(if (expanded) "收起项目结果" else "查看 ${results.size} 项结果") }
                        if (expanded) results.forEach { item ->
                            Text(item.string("name", item.string("source").substringAfterLast('/')), style = MaterialTheme.typography.bodySmall, fontWeight = FontWeight.Medium)
                            val status = NativeFileTransferPresentation.itemStatus(job, item)
                            Text(status + item.string("error").takeIf { it.isNotBlank() }?.let { "：$it" }.orEmpty(), Modifier.padding(bottom = 6.dp), style = MaterialTheme.typography.bodySmall, color = NativeDesignTokens.Muted)
                        }
                    }
                    HorizontalDivider()
                }
            }
        }
    }
}

@Composable
private fun NativeDropTarget(scope: String, directory: String, active: Boolean, enabled: Boolean, activate: () -> Unit, drop: (NativeFileClipboard) -> Unit) {
    AndroidView(modifier = Modifier.fillMaxWidth().height(38.dp), factory = { context -> TextView(context).apply { gravity = android.view.Gravity.CENTER_VERTICAL; textSize = 12f; setPadding(16.dpPixels(context), 0, 16.dpPixels(context), 0); isClickable = true } }, update = { view ->
        fun normal() { view.text = if (view.isActivated) "松开以传输到此文件夹" else if (active) "当前目标目录 · 长按文件可拖动" else "点此选为目标目录 · 可接收另一端文件"; view.setTextColor(if (active || view.isActivated) 0xFF365EEA.toInt() else 0xFF637087.toInt()); view.setBackgroundColor(if (view.isActivated) 0xFFD9E5FF.toInt() else if (active) 0xFFEAF0FF.toInt() else 0xFFF2F5F9.toInt()) }
        normal(); view.contentDescription = "${if (scope == "local") "手机" else "百度网盘"}当前目录拖放目标"
        view.setOnClickListener { activate() }
        view.setOnDragListener { _, event ->
            val payload = NativeFileClipboardState.drag(event)
            val accept = enabled && directory.isNotBlank() && payload != null && payload.scope != scope
            when (event.action) {
                DragEvent.ACTION_DRAG_STARTED -> accept
                DragEvent.ACTION_DRAG_ENTERED -> { if (accept) { view.isActivated = true; view.setBackgroundColor(0xFFD9E5FF.toInt()); view.text = "松开以${if (payload!!.operation == "move") "移动" else "复制"} ${payload.items.size} 项到此文件夹"; view.performHapticFeedback(HapticFeedbackConstants.CLOCK_TICK) }; accept }
                DragEvent.ACTION_DRAG_LOCATION -> accept
                DragEvent.ACTION_DRAG_EXITED -> { view.isActivated = false; normal(); accept }
                DragEvent.ACTION_DROP -> { if (accept) drop(payload!!); view.isActivated = false; normal(); accept }
                DragEvent.ACTION_DRAG_ENDED -> { view.isActivated = false; normal(); NativeFileClipboardState.activeDrag = null; true }
                else -> false
            }
        }
    })
}

private class NativeFileRowView(context: android.content.Context) : LinearLayout(context) {
    val symbol = ImageView(context)
    val title = TextView(context)
    val metadata = TextView(context)
    val selected = CheckBox(context)
    val more = TextView(context)
    init {
        orientation = HORIZONTAL; gravity = android.view.Gravity.CENTER_VERTICAL
        setPadding(12.dpPixels(context), 2.dpPixels(context), 6.dpPixels(context), 2.dpPixels(context))
        minimumHeight = 58.dpPixels(context); isClickable = true; isLongClickable = true
        symbol.setPadding(5.dpPixels(context), 9.dpPixels(context), 5.dpPixels(context), 9.dpPixels(context))
        addView(symbol, LayoutParams(36.dpPixels(context), 44.dpPixels(context)))
        val labels = LinearLayout(context).apply { orientation = VERTICAL; gravity = android.view.Gravity.CENTER_VERTICAL; setPadding(8.dpPixels(context), 0, 0, 0) }
        title.textSize = 14f; title.setTextColor(0xFF1B2430.toInt()); title.maxLines = 1; title.ellipsize = android.text.TextUtils.TruncateAt.END
        metadata.textSize = 11f; metadata.setTextColor(0xFF637087.toInt()); metadata.maxLines = 1; metadata.ellipsize = android.text.TextUtils.TruncateAt.END
        labels.addView(title); labels.addView(metadata); addView(labels, LayoutParams(0, LayoutParams.MATCH_PARENT, 1f))
        selected.buttonTintList = android.content.res.ColorStateList.valueOf(0xFF365EEA.toInt()); addView(selected, LayoutParams(48.dpPixels(context), 48.dpPixels(context)))
        more.text = "⋮"; more.textSize = 22f; more.gravity = android.view.Gravity.CENTER; more.setTextColor(0xFF637087.toInt()); more.isClickable = true
        addView(more, LayoutParams(40.dpPixels(context), 48.dpPixels(context)))
    }
}

@Composable
private fun NativeFileRow(file: JsonObject, selected: Boolean, toggle: () -> Unit, open: () -> Unit, menu: (String) -> Unit, drag: () -> NativeFileClipboard,
    canDrop: (NativeFileClipboard) -> Boolean, drop: (NativeFileClipboard) -> Unit) {
    AndroidView(modifier = Modifier.fillMaxWidth().height(58.dp), factory = { NativeFileRowView(it) }, update = { view ->
        val name = file.string("name", "文件")
        view.title.text = name; view.symbol.setImageDrawable(NativeFileGlyph(file.boolean("isDirectory")))
        val modified = file.long("modifiedAt").let { if (it in 1..99_999_999_999L) it * 1000 else it }
        view.metadata.text = (if (file.boolean("isDirectory")) "文件夹" else NativeFileTransferPresentation.bytes(file.long("bytes"))) + if (modified > 0) " · ${DateFormat.getDateTimeInstance(DateFormat.SHORT, DateFormat.SHORT).format(Date(modified))}" else ""
        fun normal() { view.background = GradientDrawable().apply { setColor(if (view.isActivated) 0xFFD9E5FF.toInt() else if (selected) 0xFFEAF0FF.toInt() else android.graphics.Color.WHITE); setStroke(1, 0xFFF0F2F6.toInt()) } }
        normal(); view.contentDescription = "$name，${if (file.boolean("isDirectory")) "文件夹" else "文件"}，${if (selected) "已选择" else "未选择"}，长按拖动"
        view.selected.setOnCheckedChangeListener(null); view.selected.isChecked = selected; view.selected.contentDescription = "选择 $name"
        view.selected.setOnCheckedChangeListener { _, checked -> if (checked != selected) toggle() }
        view.setOnClickListener { open() }
        view.more.contentDescription = "$name 的更多操作"
        view.more.setOnClickListener { anchor -> android.widget.PopupMenu(anchor.context, anchor).apply {
            if (!file.boolean("isDirectory")) this.menu.add(0, 1, 0, "预览")
            this.menu.add(0, 2, 1, "重命名"); this.menu.add(0, 3, 2, "删除"); this.menu.add(0, 4, 3, if (selected) "取消选择" else "选择")
            setOnMenuItemClickListener { item -> menu(when (item.itemId) { 1 -> "preview"; 2 -> "rename"; 3 -> "trash"; else -> "select" }); true }
            show()
        } }
        view.setOnLongClickListener {
            view.performHapticFeedback(HapticFeedbackConstants.LONG_PRESS)
            val clipboard = drag(); val envelope = NativeFileDrag(UUID.randomUUID().toString(), clipboard)
            NativeFileClipboardState.activeDrag = envelope
            val started = view.startDragAndDrop(ClipData.newPlainText("DevHelper 文件", "仅限 DevHelper 内部文件拖动"), NativeFileDragShadow(view, "${if (clipboard.operation == "move") "移动" else "复制"} ${clipboard.items.size} 项"), envelope, 0)
            if (!started) NativeFileClipboardState.activeDrag = null
            true
        }
        view.setOnDragListener { _, event ->
            val clipboard = NativeFileClipboardState.drag(event)
            val accept = clipboard != null && canDrop(clipboard)
            when (event.action) {
                DragEvent.ACTION_DRAG_STARTED -> accept
                DragEvent.ACTION_DRAG_ENTERED -> { if (accept) { view.isActivated = true; view.setBackgroundColor(0xFFD9E5FF.toInt()); view.performHapticFeedback(HapticFeedbackConstants.CLOCK_TICK) }; accept }
                DragEvent.ACTION_DRAG_LOCATION -> accept
                DragEvent.ACTION_DRAG_EXITED -> { view.isActivated = false; normal(); accept }
                DragEvent.ACTION_DROP -> { if (accept) drop(clipboard!!); view.isActivated = false; normal(); accept }
                DragEvent.ACTION_DRAG_ENDED -> { view.isActivated = false; normal(); NativeFileClipboardState.activeDrag = null; true }
                else -> false
            }
        }
    })
}

private class NativeFileDragShadow(view: View, private val label: String) : View.DragShadowBuilder(view) {
    private val density = view.resources.displayMetrics.density
    override fun onProvideShadowMetrics(outShadowSize: Point, outShadowTouchPoint: Point) { outShadowSize.set((160 * density).toInt(), (48 * density).toInt()); outShadowTouchPoint.set((80 * density).toInt(), (24 * density).toInt()) }
    override fun onDrawShadow(canvas: Canvas) {
        val paint = Paint(Paint.ANTI_ALIAS_FLAG).apply { color = 0xFF365EEA.toInt() }
        canvas.drawRoundRect(RectF(0f, 0f, 160 * density, 48 * density), 10 * density, 10 * density, paint)
        paint.color = android.graphics.Color.WHITE; paint.textSize = 15 * density; paint.textAlign = Paint.Align.CENTER
        canvas.drawText(label, 80 * density, 29 * density, paint)
    }
}

private class NativeFileGlyph(private val folder: Boolean) : Drawable() {
    private val paint = Paint(Paint.ANTI_ALIAS_FLAG)
    override fun draw(canvas: Canvas) {
        val saved = canvas.save(); canvas.translate(bounds.left.toFloat(), bounds.top.toFloat()); canvas.scale(bounds.width() / 24f, bounds.height() / 24f)
        paint.color = 0xFF365EEA.toInt(); paint.strokeWidth = 1.5f; paint.style = Paint.Style.STROKE; paint.strokeJoin = Paint.Join.ROUND
        if (folder) {
            val path = android.graphics.Path().apply { moveTo(2f, 7f); lineTo(2f, 5f); quadTo(2f, 3f, 4f, 3f); lineTo(9f, 3f); lineTo(12f, 6f); lineTo(20f, 6f); quadTo(22f, 6f, 22f, 8f); lineTo(22f, 19f); quadTo(22f, 21f, 20f, 21f); lineTo(4f, 21f); quadTo(2f, 21f, 2f, 19f); close() }
            paint.style = Paint.Style.FILL; paint.color = 0xFFEAF0FF.toInt(); canvas.drawPath(path, paint)
            paint.style = Paint.Style.STROKE; paint.color = 0xFF365EEA.toInt(); canvas.drawPath(path, paint); canvas.drawLine(3f, 9f, 21f, 9f, paint)
        } else {
            val path = android.graphics.Path().apply { moveTo(5f, 2f); lineTo(14f, 2f); lineTo(20f, 8f); lineTo(20f, 22f); lineTo(5f, 22f); close(); moveTo(14f, 2f); lineTo(14f, 8f); lineTo(20f, 8f) }
            canvas.drawPath(path, paint); canvas.drawLine(8f, 13f, 17f, 13f, paint); canvas.drawLine(8f, 17f, 15f, 17f, paint)
        }
        canvas.restoreToCount(saved)
    }
    override fun setAlpha(alpha: Int) { paint.alpha = alpha }
    override fun setColorFilter(colorFilter: android.graphics.ColorFilter?) { paint.colorFilter = colorFilter }
    @Deprecated("Deprecated in Android") override fun getOpacity(): Int = android.graphics.PixelFormat.TRANSLUCENT
}

private fun Int.dpPixels(context: android.content.Context): Int = (this * context.resources.displayMetrics.density).toInt()
