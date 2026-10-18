package cn.ololee.devhelper.knowledge

import androidx.activity.compose.BackHandler
import androidx.compose.foundation.horizontalScroll
import androidx.compose.foundation.interaction.MutableInteractionSource
import androidx.compose.foundation.layout.*
import androidx.compose.foundation.rememberScrollState
import androidx.compose.foundation.verticalScroll
import androidx.compose.material3.*
import androidx.compose.runtime.*
import androidx.compose.runtime.saveable.rememberSaveable
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.platform.LocalContext
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.text.font.FontFamily
import androidx.compose.ui.unit.dp
import com.google.gson.JsonObject
import java.time.Instant
import java.time.ZoneId
import java.time.format.DateTimeFormatter

@Composable
internal fun NativeManagementApp(vm: NativeManagementViewModel, finish: () -> Unit,
    pick: (audio: Boolean, insert: Boolean) -> Unit, photo: () -> Unit, record: (String) -> Unit,
    readClipboard: () -> Unit, writeClipboard: () -> Unit, export: (JsonObject) -> Unit,
    exportNote: (NativeDocumentDraft) -> Unit, openLink: (String) -> Unit,
    allowNotifications: () -> Unit = {}, notificationSettings: () -> Unit = {}, openSpine: () -> Unit = {}, openMiniGames: () -> Unit = {},
    pickFiles: () -> Unit = {}, saveResourceBatch: (String) -> Unit = {}, shareResourceBatch: (String) -> Unit = {}) {
    val state by vm.state.collectAsState()
    var deleting by remember { mutableStateOf<Pair<String, JsonObject>?>(null) }
    var renaming by remember { mutableStateOf<JsonObject?>(null) }
    var renameText by remember { mutableStateOf("") }
    var lastNotes by rememberSaveable { mutableStateOf("notes") }
    var lastMedia by rememberSaveable { mutableStateOf("resources") }
    var lastTasks by rememberSaveable { mutableStateOf("ai") }
    var lastDevices by rememberSaveable { mutableStateOf("connection") }
    var lastNotice by remember {mutableStateOf("" to false)}
    if(state.message.isNotBlank())SideEffect {lastNotice=state.message to state.error}
    val visibleNotice=if(state.message.isNotBlank())state.message to state.error else lastNotice
    val group = nativeWorkspaceGroup(state.section)
    val editingDocument = state.draft != null && state.section in listOf("notes", "memory", "skills")
    val orderedSections=nativeWorkspaceSections.flatten()
    val sectionRank=if(state.section=="ai-settings")orderedSections.indexOfFirst {it.first=="ai"}*2+1
        else orderedSections.indexOfFirst {it.first==state.section}.coerceAtLeast(0)*2
    val pageIdentity=if(editingDocument)"editor:${state.draft!!.session}" else state.section
    LaunchedEffect(state.section) {
        when (group) { 0 -> lastNotes = state.section; 1 -> lastMedia = state.section; 2 -> lastTasks = if (state.section == "ai-settings") "ai" else state.section; 3 -> lastDevices = state.section }
    }
    NativeDesignTheme {
        Surface(Modifier.fillMaxSize().then(if (state.mediaEditor == null) Modifier.windowInsetsPadding(WindowInsets.safeDrawing) else Modifier), color = if (state.mediaEditor != null) NativeDesignTokens.Canvas else NativeDesignTokens.Background) {
            Box(Modifier.fillMaxSize()) {
                if (state.mediaEditor != null) {
                    NativeMediaEditor(state.mediaEditor!!, vm::updateMedia, vm::exportMedia, vm::cancelMediaExport, vm::dismissMedia, vm::preview, vm::previewMediaFrame, vm::loadAudioWaveform)
                } else Column(Modifier.fillMaxSize()) {
                    Surface(color = NativeDesignTokens.Surface) {
                        Row(Modifier.fillMaxWidth().heightIn(min = 56.dp).padding(horizontal = 8.dp), verticalAlignment = Alignment.CenterVertically) {
                            NativeTextButton(onClick = { if (state.section == "ai-settings") vm.select("ai") else if (state.section == "ai") vm.select(state.draft?.kind?.let { if (it == "skill") "skills" else if (it == "memory") "memory" else "notes" } ?: "tasks") else if (editingDocument) vm.closeEditor() else finish() }) { Text("返回") }
                            Text(if (state.section == "ai-settings") "DeepSeek 设置" else if (state.section == "ai") "AI 助手" else if (state.section == "notifications") "任务提醒" else if (editingDocument) { if (state.draft!!.id.isBlank()) "新建资料" else "编辑资料" } else "随身工作台", Modifier.weight(1f), style = MaterialTheme.typography.titleLarge)
                            if (editingDocument) NativeTextButton(onClick = { vm.saveDocument() }, enabled = "save-document" !in state.busy) { Text("保存") }
                            else NativeTextButton(onClick = { vm.select("connection") }) { Text(if (state.relayStatus.flag("registered")) "已连接" else "本地可用", style = MaterialTheme.typography.labelMedium) }
                        }
                    }
                    if (!editingDocument && state.section != "ai-settings") NativeSectionTabs(nativeWorkspaceSections[group], state.section) { section -> if (section == "spine") openSpine() else vm.select(section) }
                    NativeMotionReveal(state.message.isNotBlank()) { Surface(color = if (visibleNotice.second) Color(0xFFFFEEF0) else NativeDesignTokens.SoftAccent, shape = MaterialTheme.shapes.small, modifier = Modifier.fillMaxWidth().padding(horizontal = 16.dp, vertical = 8.dp).nativeMotionEnter(visibleNotice.first,offsetDp=0f)) {
                        Row(Modifier.padding(start = 12.dp), verticalAlignment = Alignment.CenterVertically) {
                            Text(visibleNotice.first, Modifier.weight(1f), style = MaterialTheme.typography.bodySmall, color = if (visibleNotice.second) MaterialTheme.colorScheme.error else NativeDesignTokens.Ink)
                            NativeTextButton(onClick = vm::clearNotice, enabled = state.message.isNotBlank()) { Text("关闭") }
                        }
                    } }
                    NativeMotionPage(pageIdentity,sectionRank+if(editingDocument)1 else 0,Modifier.weight(1f).fillMaxWidth()) {
                        when {
                            state.section == "ai" -> NativeAiAssistant(state, vm, openLink)
                            state.section == "ai-settings" -> NativeAiSettings(state, vm)
                            state.section == "notifications" -> NativeTaskNotificationSettings(state, vm, allowNotifications, notificationSettings)
                            state.section == "ima-collector" -> NativeImaCollectorScreen(state, vm)
                            editingDocument -> NativeDocumentEditor(state, vm, pick, photo, exportNote, openLink)
                            state.section in listOf("notes", "memory", "skills") -> NativeDocuments(state, vm) { deleting = "document" to it }
                            state.section == "recordings" -> NativeRecordings(state, vm, record, { pick(true, false) }, export, rename = { renaming = it; renameText = it.text("name") }, delete = { deleting = "resource" to it })
                            state.section == "resources" -> NativeResources(state, vm, { pick(false, false) }, pickFiles, photo, export, saveResourceBatch, shareResourceBatch, rename = { renaming = it; renameText = it.text("name") }, delete = { deleting = "resource" to it })
                            state.section == "spine" -> NativePageColumn { NativePageHeading("Spine 播放器"); NativeButton(onClick = openSpine) { Text("打开 Spine 播放器") } }
                            state.section == "minigames" -> NativePageColumn { NativePageHeading("小游戏"); Text("扫描抖音和微信本机缓存，手机解包抖音素材与播放Spine，Unity工程传电脑处理。", color = NativeDesignTokens.Muted); NativeButton(onClick = openMiniGames) { Text("打开小游戏管理") } }
                            state.section == "directory" -> NativeDirectory(state, vm)
                            state.section == "schedules" -> NativeSchedules(state, vm) { deleting = "schedule" to it }
                            state.section == "tasks" -> NativeTasks(state, vm, openLink)
                            state.section == "devices" -> NativeDevices(state, vm, openLink)
                            state.section == "clipboard" -> NativeClipboard(state, vm, readClipboard, writeClipboard)
                            else -> NativeConnection(state, vm)
                        }
                    }
                    if (!editingDocument && state.section != "ai-settings") NavigationBar(containerColor = NativeDesignTokens.Surface, tonalElevation = 0.dp) {
                        nativeWorkspaceTitles.forEachIndexed { index, title ->
                            val interaction=remember {MutableInteractionSource()}
                            NavigationBarItem(selected = group == index,modifier=Modifier.nativePressFeedback(interaction),interactionSource=interaction,
                            onClick = { vm.select(listOf(lastNotes, lastMedia, lastTasks, lastDevices)[index]) },
                            icon = { NativeNavIcon(index, if (group == index) NativeDesignTokens.Accent else NativeDesignTokens.Muted) },
                            label = { Text(title) }, colors = NavigationBarItemDefaults.colors(indicatorColor = NativeDesignTokens.SoftAccent,
                                selectedTextColor = NativeDesignTokens.Accent, unselectedTextColor = NativeDesignTokens.Muted)) }
                    }
                }
                if (state.mediaEditor != null && state.taskCompletion != null) NativeTaskCompletionEditorBanner(state.taskCompletion!!, Modifier.align(Alignment.TopCenter))
                state.preview?.let { NativeMediaPreviewDialog(it, dismiss = vm::dismissPreview, export = { export(it.item) }, edit = { vm.editMedia(it.item) }) }
            }
            if (state.confirmDiscard) NativeAlertDialog(onDismissRequest = vm::cancelDiscard, title = { Text("保存当前修改？") }, text = { Text("草稿已保留在手机。") },
                confirmButton = { NativeTextButton(onClick = { vm.saveDocument(true) }) { Text("保存并返回") } },
                dismissButton = { Row { NativeTextButton(onClick = vm::cancelDiscard) { Text("继续编辑") }; NativeTextButton(onClick = { vm.closeEditor(true) }) { Text("放弃修改") } } })
            deleting?.let { (kind, item) -> NativeAlertDialog(onDismissRequest = { deleting = null }, title = { Text("删除${if (kind == "schedule") "日程" else if (kind == "resource") "素材" else "资料"}？") },
                text = { Text(item.text("title", item.text("name")) + "\n删除后无法撤销。") },
                confirmButton = { NativeTextButton(onClick = {
                    when (kind) { "document" -> vm.deleteDocument(item); "resource" -> vm.deleteResource(item); else -> vm.deleteSchedule(item.text("id")) }; deleting = null
                }) { Text("删除") } }, dismissButton = { NativeTextButton(onClick = { deleting = null }) { Text("取消") } }) }
            renaming?.let { item -> NativeAlertDialog(onDismissRequest = { renaming = null }, title = { Text("素材名称") }, text = { Column { NativeField("名称", renameText, { renameText = it }); if (state.error) Text(state.message, color = MaterialTheme.colorScheme.error) } },
                confirmButton = { NativeTextButton(onClick = { if (renameText.isNotBlank()) vm.renameResource(item, renameText) }, enabled = "rename-resource" !in state.busy) { Text("保存") } },
                dismissButton = { NativeTextButton(onClick = { renaming = null }) { Text("关闭") } }) }
            if (state.resourceBatchResults.isNotEmpty()) NativeAlertDialog(onDismissRequest = vm::dismissResourceResults,
                title = { Text("逐项处理结果") }, text = { Column(Modifier.heightIn(max = 400.dp).verticalScroll(rememberScrollState()), verticalArrangement = Arrangement.spacedBy(12.dp)) {
                    state.resourceBatchResults.forEach { result -> Text("${result.name}\n${if (result.success) "成功" else "失败"}：${result.message}", color = if (result.success) NativeDesignTokens.Ink else MaterialTheme.colorScheme.error) }
                } }, confirmButton = { NativeTextButton(onClick = vm::dismissResourceResults) { Text("关闭") } })
        }
    }
    BackHandler(enabled = state.section in listOf("ai", "ai-settings") || editingDocument || state.mediaEditor != null || state.preview != null) {
        when { state.preview != null -> vm.dismissPreview(); state.mediaEditor != null -> vm.dismissMedia(); state.section == "ai-settings" -> vm.select("ai"); state.section == "ai" -> vm.select(state.draft?.kind?.let { if (it == "skill") "skills" else if (it == "memory") "memory" else "notes" } ?: "tasks"); else -> vm.closeEditor() }
    }
}

@Composable
internal fun NativePageColumn(content: @Composable ColumnScope.() -> Unit) {
    Column(Modifier.fillMaxSize().verticalScroll(rememberScrollState()).imePadding().padding(horizontal = 20.dp, vertical = 24.dp), verticalArrangement = Arrangement.spacedBy(16.dp), content = content)
}
@Composable
internal fun NativeCard(motionId: String?=null,content: @Composable ColumnScope.() -> Unit) {
    Surface(shape = MaterialTheme.shapes.large, color = NativeDesignTokens.Surface, modifier = Modifier.fillMaxWidth()
        .then(if(motionId!=null)Modifier.nativeListItemEnter("card:$motionId") else Modifier)) {
        Column(Modifier.padding(20.dp), verticalArrangement = Arrangement.spacedBy(16.dp), content = content)
    }
}
@OptIn(ExperimentalLayoutApi::class)
@Composable
internal fun NativeActions(content: @Composable FlowRowScope.() -> Unit) { FlowRow(horizontalArrangement = Arrangement.spacedBy(8.dp), verticalArrangement = Arrangement.spacedBy(8.dp), content = content) }
@Composable
internal fun NativeField(label: String, value: String, change: (String) -> Unit, multiline: Boolean = false) {
    OutlinedTextField(value = value, onValueChange = change, label = { Text(label) }, modifier = Modifier.fillMaxWidth(), singleLine = !multiline, minLines = if (multiline) 5 else 1, maxLines = if (multiline) 18 else 1,
        shape = MaterialTheme.shapes.small, colors = OutlinedTextFieldDefaults.colors(unfocusedContainerColor = NativeDesignTokens.Surface, focusedContainerColor = NativeDesignTokens.Surface))
}
@Composable
internal fun NativeEmpty(text: String) { Text(text, Modifier.fillMaxWidth().padding(vertical = 32.dp), style = MaterialTheme.typography.bodyMedium, color = NativeDesignTokens.Muted) }
@Composable
internal fun NativePagination(page: NativePage, step: Int, change: (Int) -> Unit) {
    Row(Modifier.fillMaxWidth(), horizontalArrangement = Arrangement.SpaceBetween, verticalAlignment = Alignment.CenterVertically) {
        NativeTextButton(onClick = { change((page.offset - step).coerceAtLeast(0)) }, enabled = page.offset > 0) { Text("上一页") }
        Text("共 ${page.total} 项", style = MaterialTheme.typography.bodySmall, color = NativeDesignTokens.Muted)
        NativeTextButton(onClick = { change(page.offset + step) }, enabled = page.hasMore) { Text("下一页") }
    }
}
internal fun nativeSize(bytes: Long) = when { bytes >= 1024 * 1024 -> "%.1f MB".format(bytes / 1048576.0); bytes >= 1024 -> "%.1f KB".format(bytes / 1024.0); else -> "$bytes B" }
internal fun nativeDate(value: String): String = runCatching { Instant.parse(value).atZone(ZoneId.systemDefault()).format(DateTimeFormatter.ofPattern("MM-dd HH:mm")) }.getOrDefault(value)

@Composable
private fun NativeDocuments(state: NativeUiState, vm: NativeManagementViewModel, delete: (JsonObject) -> Unit) = NativePageColumn {
    NativePageHeading(when (state.section) { "memory" -> "我的记忆"; "skills" -> "我的 Skills"; else -> "我的笔记" }) { NativeButton(onClick = vm::newDocument) { Text("新建") } }
    NativeField("搜索标题或内容", state.query, vm::search)
    NativeActions { NativeOutlinedButton(onClick = { vm.refreshDocuments(0) }) { Text("搜索") }; NativeTextButton(onClick = { vm.refreshDocuments() }) { Text("刷新") } }
    if (state.section == "notes") Text("随时记下来，资料留在手机。", style = MaterialTheme.typography.bodySmall, color = NativeDesignTokens.Muted)
    if (state.documents.items.isEmpty()) NativeEmpty("还没有资料，点“新建”开始记录。")
    Column { state.documents.items.forEach { item ->
        NativeListRow(item.text("title"), nativeDate(item.text("updatedAt")) + if (item.flag("autoLoad")) "  连接时加载" else "",
            open = { vm.openDocument(item.text("id")) }, actions = listOf(NativeMenuAction("打开") { vm.openDocument(item.text("id")) }, NativeMenuAction("AI 整理") { vm.aiDocument(item.text("id")) }, NativeMenuAction("发送到设备") { vm.sendDocument(item.text("id")) }, NativeMenuAction("删除") { delete(item) }), motionId="NativeManagementScreens.kt:${item.text("id", item.text("path", item.text("name", item.text("title"))))}")
    } }
    NativePagination(state.documents, 30, vm::refreshDocuments)
}

@Composable
private fun NativeDocumentEditor(state: NativeUiState, vm: NativeManagementViewModel, pick: (Boolean, Boolean) -> Unit,
    photo: () -> Unit, export: (NativeDocumentDraft) -> Unit, openLink: (String) -> Unit) = NativePageColumn {
    val draft = state.draft ?: return@NativePageColumn
    var choosingRecording by rememberSaveable(draft.session) { mutableStateOf(false) }
    var showingOptions by rememberSaveable(draft.session) { mutableStateOf(false) }
    NativeField("标题", draft.title, { value -> vm.edit { it.copy(title = value) } })
    Row(Modifier.fillMaxWidth(), verticalAlignment = Alignment.CenterVertically) {
        Text(if (draft.dirty) "草稿已保留，尚未保存" else if (draft.id.isBlank()) "新建资料" else "已保存", Modifier.weight(1f), style = MaterialTheme.typography.bodySmall, color = NativeDesignTokens.Muted)
        NativeOverflow("资料", listOf(NativeMenuAction("导出 Markdown") { export(draft) }, NativeMenuAction("资料设置") { showingOptions = !showingOptions }, NativeMenuAction("返回列表") { vm.closeEditor() }))
    }
    NativeSectionTabs(listOf("edit" to "编辑", "preview" to "预览"), if (state.previewMarkdown) "preview" else "edit") { vm.markdownPreview(it == "preview") }
    NativeMotionPage("markdown:${draft.session}:${state.previewMarkdown}",if(state.previewMarkdown)1 else 0,Modifier.fillMaxWidth()) {
        if (state.previewMarkdown) Surface(Modifier.fillMaxWidth(), color = NativeDesignTokens.Surface) { Column(Modifier.padding(16.dp)) { NativeMarkdown(draft.content, openLink, { vm.previewAttachment(it) }, vm.repository) } }
        else NativeField("正文", draft.content, { value -> vm.edit { it.copy(content = value) } }, true)
    }
    NativeActions {
        NativeOverflow("插入素材", listOf(NativeMenuAction("选择图片或视频") { pick(false, true) }, NativeMenuAction("拍照") { photo() }, NativeMenuAction("选择音频") { pick(true, true) }, NativeMenuAction("选择已有录音") { choosingRecording = true; vm.refreshAudio(0) }), buttonLabel = "插入素材")
        NativeOverflow("AI 整理", nativeAiActions.filter { it.first != "chat" && it.first != "media" }.map { (id, label) -> NativeMenuAction(label) { vm.openAi(id) } }, buttonLabel = "AI 整理")
    }
    NativeDraftAttachments(draft, vm)
    if (choosingRecording) NativeAlertDialog(onDismissRequest = { choosingRecording = false }, title = { Text("选择录音") },
        text = { Column(Modifier.heightIn(max = 360.dp).verticalScroll(rememberScrollState())) {
            if (state.audio.items.isEmpty()) Text("可先在录音页录制或导入音频。")
            state.audio.items.forEach { item -> NativeTextButton(onClick = { vm.insertAttachments(listOf(item)); choosingRecording = false }, modifier = Modifier.fillMaxWidth().heightIn(min = 48.dp)) { Text(item.text("name")) } }
            NativePagination(state.audio, 24, vm::refreshAudio)
        } }, confirmButton = { NativeTextButton(onClick = { choosingRecording = false }) { Text("关闭") } })
    NativeMotionReveal(showingOptions) {
        NativeField("标签（逗号分隔）", draft.tags, { value -> vm.edit { it.copy(tags = value) } })
        Row(verticalAlignment = Alignment.CenterVertically) { Checkbox(draft.enabled, { value -> vm.edit { it.copy(enabled = value) } }); Text("启用这份资料") }
        if (draft.kind != "note") Row(verticalAlignment = Alignment.CenterVertically) { Checkbox(draft.autoLoad, { value -> vm.edit { it.copy(autoLoad = value) } }); Text("连接时加载") }
    }
}

@Composable
private fun NativeDraftAttachments(draft: NativeDocumentDraft, vm: NativeManagementViewModel) {
    val ids = remember(draft.content) { Regex("/api/knowledge/attachments/([0-9a-f-]{36})/content").findAll(draft.content).map { it.groupValues[1] }.distinct().toList() }
    var items by remember(draft.session) { mutableStateOf<List<JsonObject>>(emptyList()) }
    LaunchedEffect(ids) { items = kotlinx.coroutines.withContext(kotlinx.coroutines.Dispatchers.IO) { ids.mapNotNull { runCatching { vm.repository.runtime.attachments().store().read(it) }.getOrNull() } } }
    Column { items.forEach { item ->
        val actions = mutableListOf(NativeMenuAction("查看附件") { vm.preview(item) })
        if (item.text("kind") in listOf("image", "video")) actions += NativeMenuAction("AI 理解素材") { vm.openAi("media", resource = item) }
        if (item.text("kind") in listOf("image", "video")) actions += NativeMenuAction(if (item.text("kind") == "image") "裁剪与标注" else "剪辑视频") { vm.editMedia(item) }
        if (item.text("kind") == "audio") actions += NativeMenuAction("剪辑音频") { vm.editMedia(item) }
        if (item.text("kind") == "video") actions += NativeMenuAction("生成帧拼图") { vm.editMedia(item, true) }
        actions += NativeMenuAction("移除引用") { vm.edit { it.copy(content = it.content.replace(Regex("!?\\[[^]]*\\]\\(/api/knowledge/attachments/${item.text("id")}/content\\)"), "")) } }
        NativeListRow(item.text("name"), nativeSize(item.number("bytes")), kind = 1, open = { vm.preview(item) }, actions = actions, motionId="NativeManagementScreens.kt:${item.text("id", item.text("path", item.text("name", item.text("title"))))}")
    } }
}

@Composable
private fun NativeRecordings(state: NativeUiState, vm: NativeManagementViewModel, record: (String) -> Unit, pick: () -> Unit, export: (JsonObject) -> Unit, rename: (JsonObject) -> Unit, delete: (JsonObject) -> Unit) = NativePageColumn {
    var name by rememberSaveable { mutableStateOf("") }
    val recording = state.recording.text("state", "idle")
    val millis = state.recording.number("elapsedMillis")
    val countdown = recording == "countdown"
    val pending = recording in listOf("starting", "countdown")
    NativePageHeading("录音") { NativeTextButton(onClick = pick) { Text("导入音频") } }
    NativeCard {
        Text(if (countdown) state.recording.number("countdownRemaining").coerceIn(1, 3).toString() else "%02d:%02d".format(millis / 60000, millis / 1000 % 60),
            modifier = Modifier.nativeMotionEnter(if (countdown) "countdown:${state.recording.number("countdownRemaining")}" else recording, offsetDp=0f),
            style = MaterialTheme.typography.headlineLarge.copy(fontFamily=FontFamily.Monospace), color = NativeDesignTokens.Accent)
        Text(when (recording) { "recording" -> "正在录音"; "countdown" -> "倒计时结束后开始录音"; "starting" -> "准备倒计时"; "cancelled" -> "已取消，未开始录音"; "stopping" -> "正在保存"; "saved" -> "已保存到手机"; "failed" -> "录音未完成"; else -> "准备录制" },Modifier.nativeMotionEnter(recording,offsetDp=0f), style = MaterialTheme.typography.bodySmall)
        NativeField("录音名称（可选）", name, { name = it })
        if (state.recording.text("error").isNotBlank()) Text(state.recording.text("error"), color = MaterialTheme.colorScheme.error)
        Row(horizontalArrangement = Arrangement.spacedBy(8.dp)) {
            NativeButton(onClick = { record(name) }, enabled = recording !in listOf("starting", "countdown", "recording", "stopping") && "recording-start" !in state.busy && "recording-stop" !in state.busy, modifier = Modifier.weight(1f).heightIn(min = 48.dp)) { Text("开始录音") }
            NativeOutlinedButton(onClick = vm::stopRecording, enabled = (pending || recording == "recording" || recording == "failed") && "recording-stop" !in state.busy, modifier = Modifier.weight(1f).heightIn(min = 48.dp)) { Text(if (pending) "取消倒计时" else "停止并保存") }
        }
        Text("先保存，手动发送或转写时才传输。", style = MaterialTheme.typography.bodySmall, color = NativeDesignTokens.Muted)
    }
    NativeDevicePicker(state, vm)
    NativePageHeading("录音库") { NativeTextButton(onClick = { vm.refreshAudio() }) { Text("刷新") } }
    if (state.audio.items.isEmpty()) NativeEmpty("录下第一段声音，或导入已有音频。")
    Column { state.audio.items.forEach { item ->
        NativeListRow(item.text("name"), nativeSize(item.number("bytes")) + "  " + nativeDate(item.text("createdAt")), 1, open = { vm.preview(item) }, actions = listOf(
            NativeMenuAction("播放") { vm.preview(item) }, NativeMenuAction("剪辑音频") { vm.editMedia(item) }, NativeMenuAction("写入新笔记") { vm.noteForRecording(item) }, NativeMenuAction("导出") { export(item) },
            NativeMenuAction("发送到设备", "send-attachment" !in state.busy) { vm.sendAttachment(item) },
            NativeMenuAction("发送并转写文字", "send-attachment" !in state.busy) { vm.sendAttachment(item, true, false) },
            NativeMenuAction("发送、转写并提炼", "send-attachment" !in state.busy) { vm.sendAttachment(item, true, true) },
            NativeMenuAction("改名") { rename(item) }, NativeMenuAction("删除") { delete(item) }), motionId="NativeManagementScreens.kt:${item.text("id", item.text("path", item.text("name", item.text("title"))))}")
    } }
    NativePagination(state.audio, 24, vm::refreshAudio)
}

@Composable
private fun NativeResources(state: NativeUiState, vm: NativeManagementViewModel, pick: () -> Unit, pickFiles: () -> Unit, photo: () -> Unit, export: (JsonObject) -> Unit,
    saveBatch: (String) -> Unit, shareBatch: (String) -> Unit, rename: (JsonObject) -> Unit, delete: (JsonObject) -> Unit) = NativePageColumn {
    var deletingBatch by remember { mutableStateOf<List<JsonObject>?>(null) }
    val fileManagerContext = LocalContext.current
    val working = "resource-batch" in state.busy || "import" in state.busy
    NativePageHeading("资料资源") { NativeOverflow("资料资源", listOf(NativeMenuAction("选择任意文件（可多选）", !working) { pickFiles() }, NativeMenuAction("选择图片、视频或音频", !working) { pick() }, NativeMenuAction("拍照", !working) { photo() }, NativeMenuAction("刷新") { vm.refreshResources() }), buttonLabel = "添加") }
    NativeOutlinedButton(onClick = { fileManagerContext.startActivity(android.content.Intent(fileManagerContext, cn.ololee.devhelper.netdisk.NativeFileManagerActivity::class.java)) }, modifier = Modifier.fillMaxWidth()) { Text("本地与百度网盘 · 文件管理") }
    Row(Modifier.horizontalScroll(rememberScrollState()), horizontalArrangement = Arrangement.spacedBy(8.dp)) {
        listOf("all" to "全部", "image" to "图片", "video" to "视频", "audio" to "音频", "file" to "文件", "archive" to "压缩包").forEach { (id, label) -> NativeFilterChip(state.resourceKind == id, { vm.resourceKind(id) }, label = { Text(label) }) }
    }
    NativeField("搜索素材", state.query, vm::search)
    NativeTextButton(onClick = { vm.refreshResources(0) }) { Text("搜索") }
    NativeActions {
        NativeTextButton(onClick = { vm.resourceSelection(!state.resourceSelectionMode) }, enabled = !working) { Text(if (state.resourceSelectionMode) "退出多选" else "多选管理") }
        if (state.resourceSelectionMode) NativeTextButton(onClick = vm::selectResourcePage, enabled = !working) { Text("选择本页") }
    }
    if (state.resourceSelectionMode) NativeCard {
        Text("已选择 ${state.selectedResources.size} 项（可跨页，最多 100 项）")
        NativeActions {
            NativeButton(onClick = { vm.exportResources(state.selectedResources) }, enabled = state.selectedResources.isNotEmpty() && !working) { Text("导出所选资源 ZIP") }
            NativeOutlinedButton(onClick = { deletingBatch = nativeResourceSelection(state.selectedResources) }, enabled = state.selectedResources.isNotEmpty() && !working) { Text("删除所选资源") }
        }
    }
    state.resourceBatchJob?.let { job -> NativeCard {
        Text(if (job.text("state") == "completed") "资源 ZIP 已准备好" else if (job.text("state") == "failed") "导出未完成" else "正在打包所选资源")
        Text("共 ${job.number("fileCount")} 个文件", style = MaterialTheme.typography.bodySmall, color = NativeDesignTokens.Muted)
        if (job.text("state") in listOf("queued", "running")) LinearProgressIndicator(progress = { job.number("progress").coerceIn(0, 100).toFloat() / 100f }, modifier = Modifier.fillMaxWidth())
        job.text("error").takeIf(String::isNotBlank)?.let { Text(it, color = MaterialTheme.colorScheme.error) }
        if (job.text("state") == "completed") NativeActions {
            val id = job.text("jobId", job.text("id"))
            NativeButton(onClick = { saveBatch(id) }, enabled = !working) { Text("保存 ZIP") }
            NativeOutlinedButton(onClick = { shareBatch(id) }, enabled = !working) { Text("分享 ZIP") }
        }
    } }
    NativeManagementListPane("resources:${state.resourceKind}", listOf("all", "image", "video", "audio", "file", "archive").indexOf(state.resourceKind),
        state.resources.offset, loading = "resources" in state.busy) {
        if (state.resources.items.isEmpty()) NativeEmpty("还没有素材。可从右上角导入，截屏和录像也会在这里显示。")
        state.resources.items.forEach { item ->
        val media = item.text("kind") in listOf("image", "video", "audio")
        val actions = mutableListOf(NativeMenuAction("导出") { export(item) })
        if (media) actions.add(0, NativeMenuAction("预览") { vm.preview(item) })
        if (item.text("source", "attachment") == "attachment") actions += NativeMenuAction("改名") { rename(item) }
        if (item.text("kind") in listOf("image", "video")) actions += NativeMenuAction("AI 理解素材") { vm.openAi("media", resource = item) }
        if (item.text("kind") in listOf("image", "video")) actions += NativeMenuAction(if (item.text("kind") == "image") "裁剪与标注" else "剪辑视频") { vm.editMedia(item) }
        if (item.text("kind") == "audio") actions += NativeMenuAction("剪辑音频") { vm.editMedia(item) }
        if (item.text("kind") == "video") actions += NativeMenuAction("生成帧拼图") { vm.editMedia(item, true) }
        actions += NativeMenuAction("发送到设备") { vm.sendAttachment(item) }
        actions += NativeMenuAction("删除", item.number("referenceCount") == 0L) { delete(item) }
        val rowOpen: () -> Unit = if (state.resourceSelectionMode) ({ vm.toggleResource(item) }) else if (media) ({ vm.preview(item) }) else ({ export(item) })
        Row(verticalAlignment = Alignment.CenterVertically) {
            if (state.resourceSelectionMode) Checkbox(state.selectedResources.any { nativeResourceKey(it) == nativeResourceKey(item) }, { vm.toggleResource(item) }, enabled = !working)
            Box(Modifier.weight(1f)) { NativeListRow(item.text("name"), (if (item.text("source") == "artifact") "截屏 / 录像" else "资料附件") + "  " + nativeSize(item.number("bytes")) + if (item.number("referenceCount") > 0) "  ${item.number("referenceCount")}处引用" else "", 1, open = rowOpen, actions = if (state.resourceSelectionMode) emptyList() else actions, motionId="resources:${nativeResourceKey(item)}") }
        }
        }
    }
    NativePagination(state.resources, 24, vm::refreshResources)
    deletingBatch?.let { captured -> NativeAlertDialog(onDismissRequest = { deletingBatch = null }, title = { Text("删除所选 ${captured.size} 个资源？") },
        text = { Column(Modifier.heightIn(max = 360.dp).verticalScroll(rememberScrollState())) {
            Text("删除后无法撤销。被资料引用或正在使用的资源会保留，并逐项说明失败原因。")
            captured.forEach { Text(it.text("name", "资源")) }
        } }, confirmButton = { NativeTextButton(onClick = { vm.deleteResources(captured); deletingBatch = null }, enabled = !working) { Text("确认删除") } },
        dismissButton = { NativeTextButton(onClick = { deletingBatch = null }) { Text("取消") } }) }
}

@Composable
private fun NativeDirectory(state: NativeUiState, vm: NativeManagementViewModel) = NativePageColumn {
    var path by rememberSaveable(state.directoryPath) { mutableStateOf(state.directoryPath) }
    NativePageHeading("手机目录")
    NativeField("目录路径", path, { path = it })
    NativeActions { NativeButton(onClick = { vm.browseDirectory(path) }) { Text("打开目录") }; NativeTextButton(onClick = { vm.browseDirectory(state.directoryParent) }, enabled = state.directoryParent.isNotBlank()) { Text("上一级") } }
    Text("目录访问需开启已授权的手机服务。", style = MaterialTheme.typography.bodySmall, color = NativeDesignTokens.Muted)
    NativeManagementListPane("directory:${state.directoryPath}", state.directoryPath.split('/').count { it.isNotBlank() },
        state.directory.offset, loading = "directory" in state.busy) {
        state.directory.items.forEach { item ->
        val folder = item.text("kind") == "directory"
        val actions = if (folder) listOf(NativeMenuAction("打开文件夹") { vm.browseDirectory(item.text("path")) }) else if (item.text("mediaType") in listOf("image", "video", "audio")) listOf(NativeMenuAction("导入素材库") { vm.importPhoneFile(item.text("path")) }) else emptyList()
        NativeListRow(item.text("name"), if (folder) "文件夹" else nativeSize(item.number("size")), 1, open = if (folder) ({ vm.browseDirectory(item.text("path")) }) else null, actions = actions, motionId="NativeManagementScreens.kt:${item.text("id", item.text("path", item.text("name", item.text("title"))))}")
        }
    }
    NativePagination(state.directory, 40) { vm.browseDirectory(state.directoryPath, it) }
}

/** Only list pixels transition; controls, rows and page state keep their existing behavior. */
@Composable
private fun NativeManagementListPane(route: String, rank: Int, offset: Int, loading: Boolean, content: @Composable () -> Unit) {
    val requested = Triple(route, rank, offset)
    var settled by remember { mutableStateOf(requested) }
    val displayed = if (loading) settled else requested
    var previous by remember { mutableStateOf(displayed) }
    val direction = remember(displayed) {
        if (displayed.second < previous.second || displayed.first == previous.first && displayed.third < previous.third) -1f else 1f
    }
    SideEffect {
        if (!loading) { settled = requested; previous = displayed }
    }
    Column(Modifier.fillMaxWidth().nativeMotionEnter("${displayed.first}:${displayed.third}", offsetDp = direction * 12f, horizontal = true)) {
        content()
    }
}
