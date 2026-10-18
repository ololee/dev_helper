package cn.ololee.devhelper.knowledge

import android.Manifest
import android.content.ClipData
import android.content.ClipboardManager
import android.content.Intent
import android.content.pm.PackageManager
import android.net.Uri
import android.os.Bundle
import android.os.Build
import android.provider.Settings
import androidx.activity.ComponentActivity
import androidx.activity.compose.setContent
import androidx.activity.result.contract.ActivityResultContracts
import androidx.core.content.ContextCompat
import androidx.core.content.FileProvider
import androidx.lifecycle.ViewModelProvider
import androidx.lifecycle.lifecycleScope
import cn.ololee.devhelper.relay.RelayClient
import io.github.libsumcp.tools.ClipboardAccess
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.launch
import kotlinx.coroutines.withContext
import java.io.File

/** Native Compose management. No browser, HTML asset or localhost listener is required. */
class NativeManagementActivity : ComponentActivity() {
    override fun onResume() {
        super.onResume()
        RelayClient.get(this).resumeEnabled()
        if (::model.isInitialized) { model.refreshTaskNotifications(); model.refreshRecording() }
    }
    private lateinit var model: NativeManagementViewModel
    private var photoPath: String? = null
    private var exportResource: Pair<String, String>? = null
    private var exportDocument: String? = null
    private var exportBatchJob: String? = null
    private var sharingResources = false
    private var microphoneRequest: Pair<Long, String>? = null
    private val mediaPicker = registerForActivityResult(ActivityResultContracts.OpenMultipleDocuments()) { uris ->
        if (uris.isNotEmpty()) model.importUris(uris)
    }
    private val filePicker = registerForActivityResult(ActivityResultContracts.OpenMultipleDocuments()) { uris ->
        if (uris.isNotEmpty()) model.importUris(uris)
    }
    private val batchExportPicker = registerForActivityResult(ActivityResultContracts.CreateDocument("application/zip")) { uri ->
        val id = exportBatchJob; exportBatchJob = null
        if (uri != null && id != null) lifecycleScope.launch {
            runCatching { withContext(Dispatchers.IO) {
                val file = model.repository.runtime.resourceBatchExportFile(id)
                contentResolver.openOutputStream(uri, "w")?.use { output -> file.inputStream().use { it.copyTo(output, 65536) } } ?: error("无法写入所选位置")
            } }.onSuccess { model.notice("资源 ZIP 已保存") }.onFailure { model.notice(it.message ?: "ZIP 保存未完成", true) }
        }
    }
    private val microphone = registerForActivityResult(ActivityResultContracts.RequestPermission()) { granted ->
        val request = microphoneRequest
        microphoneRequest = null
        if (request != null && request.first == model.recordingRequest && model.state.value.section == "recordings") {
            if (granted) model.startRecording(request.second) else model.notice("麦克风权限未允许，仍可导入已有音频", true)
        }
    }
    private val notificationPermission = registerForActivityResult(ActivityResultContracts.RequestPermission()) { granted ->
        model.refreshTaskNotifications()
        model.notice(if (granted) "已允许发送任务通知" else "通知权限未允许，可在系统通知设置中开启", !granted)
    }
    private val takePicture = registerForActivityResult(ActivityResultContracts.TakePicture()) { saved ->
        val file = photoPath?.let(::File); photoPath = null
        if (saved && file?.isFile == true) {
            val uri = FileProvider.getUriForFile(this, "$packageName.knowledge.files", file)
            model.importUris(listOf(uri))
        } else { file?.delete(); model.notice("拍照已取消") }
    }
    private val exportPicker = registerForActivityResult(ActivityResultContracts.CreateDocument("application/octet-stream")) { uri ->
        val resource = exportResource; val text = exportDocument; exportResource = null; exportDocument = null
        if (uri != null) lifecycleScope.launch {
            runCatching { withContext(Dispatchers.IO) {
                require(resource != null || text != null) { "导出已中断，请重新选择文件" }
                contentResolver.openOutputStream(uri, "w")?.use { output ->
                    if (resource != null) model.repository.runtime.openManagedResource(resource.first, resource.second).file.inputStream().use { it.copyTo(output, 65536) }
                    else if (text != null) output.write(text.toByteArray(Charsets.UTF_8))
                } ?: error("无法写入所选位置")
            } }.onSuccess { model.notice("文件已导出") }.onFailure { model.notice(it.message ?: "导出未完成", true) }
        }
    }
    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        model = ViewModelProvider(this)[NativeManagementViewModel::class.java]
        photoPath = savedInstanceState?.getString("photoPath")
        exportResource = savedInstanceState?.getString("exportId")?.let { savedInstanceState.getString("exportSource", "attachment") to it }
        exportDocument = savedInstanceState?.getString("exportDocument")
        exportBatchJob = savedInstanceState?.getString("exportBatchJob")
        if (savedInstanceState == null) handleNavigationIntent(intent)
        else if (model.state.value.taskCompletion == null && savedInstanceState.containsKey("notificationTaskId"))
            model.acceptTaskNotification(savedInstanceState.getString("notificationTaskId").orEmpty(), savedInstanceState.getString("notificationNextStep").orEmpty())
        setContent {
            NativeManagementApp(model, finish = { finish() },
                pick = { audio, insert -> model.startImport(insert); mediaPicker.launch(if (audio) arrayOf("audio/*") else arrayOf("image/*", "video/*", "audio/*")) },
                pickFiles = { model.startImport(false); runCatching { filePicker.launch(arrayOf("*/*")) }.onFailure { model.notice("文件选择器暂不可用", true) } },
                saveResourceBatch = { id -> if (exportBatchJob == null) { exportBatchJob = id; batchExportPicker.launch("resources-$id.zip") } },
                shareResourceBatch = ::shareResourceBatch,
                photo = {
                    model.startImport(model.state.value.draft != null)
                    runCatching {
                        val directory = File(cacheDir, "knowledge-capture").apply { mkdirs() }
                        val file = File.createTempFile("photo-", ".jpg", directory); photoPath = file.absolutePath
                        takePicture.launch(FileProvider.getUriForFile(this, "$packageName.knowledge.files", file))
                    }.onFailure { model.notice("相机未能打开，草稿已保留", true) }
                },
                record = { name ->
                    model.pendingRecordingName = name
                    if (ContextCompat.checkSelfPermission(this, Manifest.permission.RECORD_AUDIO) == PackageManager.PERMISSION_GRANTED) model.startRecording(name)
                    else {
                        microphoneRequest = model.recordingRequest to name
                        microphone.launch(Manifest.permission.RECORD_AUDIO)
                    }
                },
                readClipboard = {
                    val clipboard = getSystemService(ClipboardManager::class.java)
                    val clip = clipboard.primaryClip
                    if (clip == null || clip.itemCount == 0) model.notice("手机剪贴板当前没有文本")
                    else clip.getItemAt(0).text?.let { model.clipboardText(it.toString()); model.notice("已读取手机剪贴板") }
                        ?: model.notice("当前剪贴板内容不是文本", true)
                },
                writeClipboard = {
                    getSystemService(ClipboardManager::class.java).setPrimaryClip(ClipData.newPlainText("DevHelper", model.state.value.clipboardText))
                    model.notice("已写入手机剪贴板")
                },
                export = { item -> exportResource = item.text("source", "attachment") to item.text("id"); exportDocument = null; exportPicker.launch(item.text("name", "素材")) },
                exportNote = { draft -> exportResource = null; exportDocument = draft.content; exportPicker.launch(draft.title.ifBlank { "笔记" }.replace(Regex("[/\\\\\\r\\n]"), "-") + ".md") },
                openSpine = { startActivity(Intent(this, SpineActivity::class.java)) },
                openMiniGames = { startActivity(Intent(this, MiniGameActivity::class.java)) },
                allowNotifications = {
                    if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.TIRAMISU &&
                        ContextCompat.checkSelfPermission(this, Manifest.permission.POST_NOTIFICATIONS) != PackageManager.PERMISSION_GRANTED)
                        notificationPermission.launch(Manifest.permission.POST_NOTIFICATIONS)
                    else model.refreshTaskNotifications()
                },
                notificationSettings = {
                    val settings = Intent(Settings.ACTION_APP_NOTIFICATION_SETTINGS).putExtra(Settings.EXTRA_APP_PACKAGE, packageName)
                    runCatching { startActivity(settings) }.onFailure { model.notice("系统通知设置暂时无法打开", true) }
                },
                openLink = { url ->
                    val uri = Uri.parse(url)
                    if (uri.scheme in listOf("https", "http", "mailto")) runCatching { startActivity(Intent(Intent.ACTION_VIEW, uri)) }.onFailure { model.notice("未找到可打开此链接的应用", true) }
                })
        }
    }
    override fun onNewIntent(intent: Intent) {
        super.onNewIntent(intent)
        setIntent(intent)
        handleNavigationIntent(intent)
    }
    private fun handleNavigationIntent(source: Intent) {
        if (source.getStringExtra("section") == "file-manager") {
            model.select("resources")
            source.removeExtra("section")
            startActivity(Intent(this, cn.ololee.devhelper.netdisk.NativeFileManagerActivity::class.java))
            return
        }
        if (source.data?.lastPathSegment?.startsWith("minigame-") == true) {
            model.select("minigames")
            source.removeExtra("notificationTaskId"); source.removeExtra("notificationNextStep")
            startActivity(Intent(this, MiniGameActivity::class.java))
            return
        }
        if (source.hasExtra("notificationTaskId") || source.hasExtra("notificationNextStep")) {
            model.acceptTaskNotification(source.getStringExtra("notificationTaskId").orEmpty(), source.getStringExtra("notificationNextStep").orEmpty())
            source.removeExtra("notificationTaskId"); source.removeExtra("notificationNextStep")
        } else model.select(source.getStringExtra("section") ?: "notes")
    }
    override fun onWindowFocusChanged(hasFocus: Boolean) {
        super.onWindowFocusChanged(hasFocus); ClipboardAccess.windowFocusChanged(this, hasFocus)
    }
    override fun onSaveInstanceState(outState: Bundle) {
        outState.putString("photoPath", photoPath)
        exportResource?.let { outState.putString("exportSource", it.first); outState.putString("exportId", it.second) }
        if ((exportDocument?.toByteArray()?.size ?: 0) <= 65536) outState.putString("exportDocument", exportDocument)
        exportBatchJob?.let { outState.putString("exportBatchJob", it) }
        model.state.value.taskCompletion?.let {
            outState.putString("notificationTaskId", it.taskId)
            outState.putString("notificationNextStep", it.nextStep)
        }
        super.onSaveInstanceState(outState)
    }
    override fun onStop() { microphoneRequest = null; model.cancelPendingRecording(); model.persistDraft(); super.onStop() }
    override fun onDestroy() { ClipboardAccess.windowFocusChanged(this, false); super.onDestroy() }

    private fun shareResourceBatch(id: String) {
        if (sharingResources) return
        sharingResources = true
        lifecycleScope.launch {
            try {
                val file = withContext(Dispatchers.IO) {
                    val source = model.repository.runtime.resourceBatchExportFile(id)
                    val directory = File(cacheDir, "knowledge-resource-share").apply { mkdirs() }
                    directory.listFiles()?.filter { it.isFile && it.lastModified() < System.currentTimeMillis() - 24 * 60 * 60 * 1000L }?.forEach { it.delete() }
                    val temporary = File.createTempFile("resources-", ".partial", directory)
                    val ready = File(directory, temporary.name.removeSuffix(".partial") + ".zip")
                    try { source.inputStream().use { input -> temporary.outputStream().use { input.copyTo(it, 65536) } }; check(temporary.renameTo(ready)) { "无法准备分享文件" }; ready }
                    finally { temporary.delete() }
                }
                val uri = FileProvider.getUriForFile(this@NativeManagementActivity, "$packageName.knowledge.files", file)
                startActivity(Intent.createChooser(Intent(Intent.ACTION_SEND).apply {
                    type = "application/zip"; putExtra(Intent.EXTRA_STREAM, uri)
                    clipData = ClipData.newRawUri("资源 ZIP", uri); addFlags(Intent.FLAG_GRANT_READ_URI_PERMISSION)
                }, "分享所选资源"))
                model.notice("资源 ZIP 已交给分享面板")
            } catch (error: Exception) { model.notice(error.message ?: "分享未完成，导出仍保留", true) }
            finally { sharingResources = false }
        }
    }
}
