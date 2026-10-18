package cn.ololee.devhelper.netdisk

import android.app.Notification
import android.app.NotificationChannel
import android.app.NotificationManager
import android.app.PendingIntent
import android.app.Service
import android.content.Context
import android.content.Intent
import android.content.pm.ServiceInfo
import android.os.Handler
import android.os.IBinder
import android.os.Looper
import android.os.SystemClock
import com.google.gson.JsonObject

/** Keeps explicitly queued binary transfers alive when the file screen is left. */
class NativeFileTransferService : Service() {
    private val handler = Handler(Looper.getMainLooper())
    private var idleDeadline = 0L
    private var hadActive = false
    private var stopping = false
    private val runtime by lazy { NativeFileTransferRuntime.get(this) }

    companion object {
        private const val CHANNEL = "native-file-transfers"
        private const val NOTIFICATION = 8768
        private const val CANCEL = "cn.ololee.devhelper.netdisk.CANCEL_TRANSFERS"

        /** Called before queueing: platform rejection must never start a hidden move. */
        fun ensureStarted(context: Context) {
            context.startForegroundService(Intent(context, NativeFileTransferService::class.java))
        }
    }

    override fun onCreate() {
        super.onCreate()
        getSystemService(NotificationManager::class.java).createNotificationChannel(
            NotificationChannel(CHANNEL, "文件传输", NotificationManager.IMPORTANCE_LOW).apply {
                description = "文件传输、重命名和删除的进度，可取消尚未处理的项目"
                setShowBadge(false)
            }
        )
        startForeground(NOTIFICATION, notification(null, 0), ServiceInfo.FOREGROUND_SERVICE_TYPE_DATA_SYNC)
        idleDeadline = SystemClock.elapsedRealtime() + 15_000L
        handler.post(tick)
    }

    override fun onStartCommand(intent: Intent?, flags: Int, startId: Int): Int {
        if (intent?.action == CANCEL) cancelActive()
        else idleDeadline = SystemClock.elapsedRealtime() + 15_000L
        return START_NOT_STICKY
    }

    private fun active(): List<JsonObject> = runtime.jobs().getAsJsonArray("jobs")
        .map { it.asJsonObject }.filter { it.string("state") in setOf("queued", "running") }

    private fun cancelActive() {
        active().forEach { job -> runCatching { runtime.cancel(job.string("jobId")) } }
    }

    private val tick = object : Runnable {
        override fun run() {
            if (stopping) return
            val jobs = runCatching { active() }.getOrDefault(emptyList())
            if (jobs.isNotEmpty()) {
                hadActive = true
                getSystemService(NotificationManager::class.java).notify(NOTIFICATION, notification(jobs.first(), jobs.size))
            } else if (hadActive || SystemClock.elapsedRealtime() >= idleDeadline) {
                stopping = true
                stopForeground(STOP_FOREGROUND_REMOVE)
                stopSelf()
                return
            }
            handler.postDelayed(this, 750L)
        }
    }

    private fun notification(job: JsonObject?, count: Int): Notification {
        val open = PendingIntent.getActivity(this, 0,
            Intent(this, NativeFileManagerActivity::class.java).addFlags(Intent.FLAG_ACTIVITY_CLEAR_TOP or Intent.FLAG_ACTIVITY_SINGLE_TOP),
            PendingIntent.FLAG_UPDATE_CURRENT or PendingIntent.FLAG_IMMUTABLE)
        val cancel = PendingIntent.getService(this, 0,
            Intent(this, NativeFileTransferService::class.java).setAction(CANCEL),
            PendingIntent.FLAG_UPDATE_CURRENT or PendingIntent.FLAG_IMMUTABLE)
        val progress = job?.getAsJsonObject("progress")
        val transferred = progress?.number("bytesTransferred") ?: 0L
        val total = progress?.number("totalBytes") ?: 0L
        val stage = when (progress?.string("phase")) {
            "uploading" -> "传到网盘"
            "downloading" -> "传到手机"
            "verifying", "verifying-upload", "verifying-batch", "verifying-before-cleanup" -> "校验文件"
            "cleaning-sources" -> "处理源文件"
            "committing" -> "保存文件"
            "staging", "preparing", "reading-source" -> "读取文件"
            "creating-directories" -> "创建目录"
            "renaming" -> "重命名文件"
            "deleting" -> "删除文件"
            else -> "正在传输"
        }
        val mutation = job?.string("operation") in setOf("rename", "trash")
        val detail = if (job == null) "准备文件任务" else if (mutation) "$stage · 已确认 ${progress?.number("completedItems") ?: 0} 项" else "$stage · ${size(transferred)} / ${size(total)}" + if (count > 1) " · $count 个任务" else ""
        return Notification.Builder(this, CHANNEL)
            .setSmallIcon(android.R.drawable.stat_sys_upload)
            .setContentTitle("本地与网盘")
            .setContentText(detail)
            .setContentIntent(open)
            .setCategory(Notification.CATEGORY_PROGRESS)
            .setOnlyAlertOnce(true)
            .setOngoing(true)
            .setProgress(100, if (total > 0L) (transferred.coerceAtMost(total).toDouble() * 100 / total).toInt().coerceAtMost(99) else 0, total <= 0)
            .addAction(Notification.Action.Builder(android.graphics.drawable.Icon.createWithResource(this, android.R.drawable.ic_menu_close_clear_cancel), "取消任务", cancel).build())
            .build()
    }

    // Android's data-sync time budget is enforced; never silently restart it.
    override fun onTimeout(startId: Int, fgsType: Int) {
        cancelActive()
        stopping = true
        handler.removeCallbacks(tick)
        stopForeground(STOP_FOREGROUND_REMOVE)
        stopSelf(startId)
    }

    override fun onDestroy() {
        handler.removeCallbacks(tick)
        if (!stopping) cancelActive()
        super.onDestroy()
    }

    override fun onBind(intent: Intent?): IBinder? = null
    private fun JsonObject.string(key: String) = get(key)?.takeIf { it.isJsonPrimitive }?.asString.orEmpty()
    private fun JsonObject.number(key: String) = runCatching { get(key)?.asLong ?: 0L }.getOrDefault(0L)
    private fun size(value: Long) = when {
        value >= 1024L * 1024 * 1024 -> "%.1f GB".format(value / (1024.0 * 1024 * 1024))
        value >= 1024L * 1024 -> "%.1f MB".format(value / (1024.0 * 1024))
        value >= 1024 -> "%.0f KB".format(value / 1024.0)
        else -> "$value B"
    }
}
