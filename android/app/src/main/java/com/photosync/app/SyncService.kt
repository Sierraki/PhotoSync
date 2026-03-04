package com.photosync.app

import android.app.*
import android.content.Context
import android.content.Intent
import android.content.pm.ServiceInfo
import android.os.Build
import android.os.IBinder
import androidx.core.app.NotificationCompat
import kotlinx.coroutines.*

/**
 * 前台服务：在后台执行照片同步任务，防止系统杀死进程
 */
class SyncService : Service() {

    companion object {
        const val CHANNEL_ID = "photosync_channel"
        const val NOTIFICATION_ID = 1001
        const val ACTION_START = "com.photosync.START_SYNC"
        const val ACTION_STOP = "com.photosync.STOP_SYNC"
        const val EXTRA_SERVER_IP = "server_ip"
        const val EXTRA_CONNECTION_MODE = "connection_mode"

        // 同步状态回调
        var onSyncProgress: ((SyncProgress) -> Unit)? = null
        var onSyncLog: ((String) -> Unit)? = null
        var isSyncing: Boolean = false
            private set
    }

    data class SyncProgress(
        val total: Int,           // 手机端总数
        val synced: Int,          // 本次已同步
        val skipped: Int,         // 跳过（已存在）
        val failed: Int,          // 失败
        val currentFile: String,
        val needSync: Int = 0,    // 需要同步的数量
        val pcSynced: Int = 0,    // 电脑端已有数量
        val speed: Double = 0.0,  // 同步速度（个/秒）
        val eta: Long = 0         // 预计剩余时间（秒）
    )

    private val serviceScope = CoroutineScope(Dispatchers.IO + SupervisorJob())
    private var syncJob: Job? = null

    override fun onBind(intent: Intent?): IBinder? = null

    override fun onCreate() {
        super.onCreate()
        createNotificationChannel()
    }

    override fun onStartCommand(intent: Intent?, flags: Int, startId: Int): Int {
        when (intent?.action) {
            ACTION_START -> {
                val serverIp = intent.getStringExtra(EXTRA_SERVER_IP) ?: ""
                val modeStr = intent.getStringExtra(EXTRA_CONNECTION_MODE) ?: "WIFI"
                val mode = ConnectionMode.valueOf(modeStr)
                startForegroundSync()
                startSync(serverIp, mode)
            }
            ACTION_STOP -> {
                stopSync()
            }
        }
        return START_NOT_STICKY
    }

    private fun startForegroundSync() {
        val notification = buildNotification("正在准备同步...")
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.Q) {
            startForeground(NOTIFICATION_ID, notification, ServiceInfo.FOREGROUND_SERVICE_TYPE_DATA_SYNC)
        } else {
            startForeground(NOTIFICATION_ID, notification)
        }
    }

    private fun startSync(serverIp: String, mode: ConnectionMode) {
        if (isSyncing) return
        isSyncing = true

        syncJob = serviceScope.launch {
            val scanner = PhotoScanner(applicationContext)
            val client = SyncClient(applicationContext).apply {
                connectionMode = mode
                this.serverIp = serverIp
            }

            try {
                // 1) 测试连接
                log("正在连接服务器...")
                val connResult = client.testConnection()
                if (connResult.isFailure) {
                    log("连接失败: ${connResult.exceptionOrNull()?.message}")
                    isSyncing = false
                    stopSelf()
                    return@launch
                }
                val serverStatus = connResult.getOrNull() ?: emptyMap()
                val pcSyncedCount = (serverStatus["total_synced"] as? Number)?.toInt() ?: 0
                log("服务器连接成功 (${if (mode == ConnectionMode.USB) "USB" else "WiFi"})")
                log("电脑端已同步: $pcSyncedCount 个文件")

                // 2) 扫描相册
                log("正在扫描手机相册...")
                val allPhotos = scanner.scanAll()
                log("手机端发现 ${allPhotos.size} 个文件")

                if (allPhotos.isEmpty()) {
                    log("相册为空，无需同步")
                    isSyncing = false
                    stopSelf()
                    return@launch
                }

                // 3) 预先计算所有 MD5 并统计需要同步的数量
                log("正在计算文件指纹并检查同步状态...")
                val batchSize = 100
                var needSyncCount = 0
                var alreadySyncedCount = 0
                val photosNeedSync = mutableListOf<PhotoInfo>()
                val deviceName = android.os.Build.MODEL

                for (batchStart in allPhotos.indices step batchSize) {
                    if (!isSyncing) break

                    val batch = allPhotos.subList(
                        batchStart,
                        minOf(batchStart + batchSize, allPhotos.size)
                    )

                    // 计算这一批的 MD5
                    var failedMd5Count = 0
                    for (photo in batch) {
                        if (!isSyncing) break
                        try {
                            photo.md5Hash = scanner.computeMd5(photo.uri)
                        } catch (e: Exception) {
                            photo.md5Hash = ""
                            failedMd5Count++
                        }
                    }
                    if (failedMd5Count > 0) {
                        log("警告: $failedMd5Count 个文件无法计算MD5，将直接上传")
                    }

                    // 构建检查列表（相册 + MD5）
                    val checkItems = batch.map { photo ->
                        val album = photo.bucketName.ifEmpty { "unsorted" }
                        mapOf("album" to album, "md5" to photo.md5Hash)
                    }

                    // 批量检查已同步状态（相册内去重）
                    val checkResult = try {
                        client.checkAlbum(checkItems)
                    } catch (e: Exception) {
                        log("批量检查失败: ${e.message}")
                        // 如果检查失败，把所有照片都加入待上传
                        for (photo in batch) {
                            needSyncCount++
                            photosNeedSync.add(photo)
                        }
                        continue
                    }
                    val syncedMap = checkResult.getOrDefault(emptyMap())

                    for ((index, photo) in batch.withIndex()) {
                        val album = photo.bucketName.ifEmpty { "unsorted" }
                        val key = "$album|${photo.md5Hash}"
                        val isSynced = syncedMap[key] ?: false
                        if (isSynced) {
                            alreadySyncedCount++
                        } else {
                            needSyncCount++
                            photosNeedSync.add(photo)
                        }
                    }

                    // 通知服务器扫描进度
                    val scanned = minOf(batchStart + batchSize, allPhotos.size)
                    client.notifyScanProgress(deviceName, scanned, allPhotos.size)
                    log("已检查 $scanned / ${allPhotos.size}...")
                }

                log("========== 同步统计 ==========")
                log("手机端照片总数: ${allPhotos.size}")
                log("电脑端已同步: $pcSyncedCount")
                log("本次已跳过（相册内重复）: $alreadySyncedCount")
                log("本次需要同步: $needSyncCount")
                log("==============================")

                // 通知服务器同步统计（网页端显示进度）
                client.notifySyncStart(deviceName, allPhotos.size, needSyncCount)

                if (photosNeedSync.isEmpty()) {
                    log("所有照片已同步完成，无需操作")
                    client.notifySyncStop("所有照片已同步完成")
                    isSyncing = false
                    stopSelf()
                    return@launch
                }

                // 5) 开始上传需要同步的文件
                log("开始同步 $needSyncCount 个文件...")
                var synced = 0
                var skipped = alreadySyncedCount
                var failed = 0
                val startTime = System.currentTimeMillis()

                for ((index, photo) in photosNeedSync.withIndex()) {
                    if (!isSyncing) break

                    // 计算速度和剩余时间
                    val elapsedMs = System.currentTimeMillis() - startTime
                    val speed = if (elapsedMs > 0 && synced > 0) {
                        synced.toDouble() / (elapsedMs / 1000.0)
                    } else 0.0
                    val remaining = needSyncCount - synced - failed
                    val eta = if (speed > 0) (remaining / speed).toLong() else 0L

                    // 上传
                    try {
                        val uploadResult = client.uploadPhoto(photo, scanner)
                        if (uploadResult.isSuccess) {
                            val result = uploadResult.getOrNull()!!
                            if (result.status == "ok") {
                                synced++
                                log("已同步: ${photo.displayName}")
                            } else {
                                skipped++
                            }
                        } else {
                            failed++
                            log("失败: ${photo.displayName} - ${uploadResult.exceptionOrNull()?.message}")
                        }
                    } catch (e: OutOfMemoryError) {
                        failed++
                        log("内存不足跳过: ${photo.displayName}")
                        System.gc()
                    } catch (e: Exception) {
                        failed++
                        log("失败: ${photo.displayName} - ${e.message}")
                    }

                    val progress = SyncProgress(
                        total = allPhotos.size,
                        synced = synced,
                        skipped = skipped,
                        failed = failed,
                        currentFile = photo.displayName,
                        needSync = needSyncCount,
                        pcSynced = pcSyncedCount,
                        speed = speed,
                        eta = eta
                    )
                    updateProgress(progress)
                    updateNotification("已同步 $synced / $needSyncCount")

                    // 每上传一个文件，通知服务器更新进度
                    client.notifySyncProgress(photo.displayName, synced, skipped, failed)
                }

                val summaryMsg = "同步完成: $synced 个, 跳过: $skipped, 失败: $failed"
                client.notifySyncStop(summaryMsg)

                log("========== 同步完成 ==========")
                log("本次同步: $synced 个")
                log("跳过（已存在）: $skipped 个")
                log("失败: $failed 个")
                log("电脑端现有: ${pcSyncedCount + synced} 个")
                log("==============================")
            } catch (e: CancellationException) {
                log("同步已取消")
            } catch (e: Exception) {
                log("同步异常: ${e.message}")
            } finally {
                client.shutdown()
                isSyncing = false
                stopSelf()
            }
        }
    }

    private fun stopSync() {
        isSyncing = false
        syncJob?.cancel()
        stopForeground(STOP_FOREGROUND_REMOVE)
        stopSelf()
    }

    private fun updateProgress(progress: SyncProgress) {
        onSyncProgress?.invoke(progress)
    }

    private fun log(message: String) {
        onSyncLog?.invoke(message)
    }

    private fun createNotificationChannel() {
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O) {
            val channel = NotificationChannel(
                CHANNEL_ID,
                "照片同步",
                NotificationManager.IMPORTANCE_LOW
            ).apply {
                description = "照片同步进度通知"
            }
            val manager = getSystemService(Context.NOTIFICATION_SERVICE) as NotificationManager
            manager.createNotificationChannel(channel)
        }
    }

    private fun buildNotification(text: String): Notification {
        val intent = Intent(this, MainActivity::class.java)
        val pendingIntent = PendingIntent.getActivity(
            this, 0, intent,
            PendingIntent.FLAG_UPDATE_CURRENT or PendingIntent.FLAG_IMMUTABLE
        )

        return NotificationCompat.Builder(this, CHANNEL_ID)
            .setContentTitle("PhotoSync")
            .setContentText(text)
            .setSmallIcon(android.R.drawable.ic_menu_upload)
            .setContentIntent(pendingIntent)
            .setOngoing(true)
            .build()
    }

    private fun updateNotification(text: String) {
        val notification = buildNotification(text)
        val manager = getSystemService(Context.NOTIFICATION_SERVICE) as NotificationManager
        manager.notify(NOTIFICATION_ID, notification)
    }

    override fun onDestroy() {
        super.onDestroy()
        isSyncing = false
        syncJob?.cancel()
        serviceScope.cancel()
    }
}
