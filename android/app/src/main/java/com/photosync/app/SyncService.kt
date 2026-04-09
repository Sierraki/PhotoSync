package com.photosync.app

import android.app.*
import android.content.Context
import android.content.Intent
import android.content.pm.ServiceInfo
import android.os.Build
import android.os.IBinder
import android.os.PowerManager
import androidx.core.app.NotificationCompat
import kotlinx.coroutines.*
import java.util.concurrent.atomic.AtomicInteger
import java.util.concurrent.atomic.AtomicLong

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
        const val EXTRA_SERVER_PORT = "server_port"
        const val EXTRA_CONNECTION_MODE = "connection_mode"
        const val EXTRA_SYNC_MODE = "sync_mode"
        private const val PREFS_NAME = "photosync_prefs"
        private const val KEY_LAST_SCAN_TS = "last_scan_ts"
        private const val KEY_LAST_SERVER_ID = "last_server_id"
        private const val KEY_LAST_SERVER_STORAGE_PATH = "last_server_storage_path"
        private const val KEY_LAST_PC_TOTAL = "last_pc_total"

        // 同步状态回调
        var onSyncProgress: ((SyncProgress) -> Unit)? = null
        var onSyncLog: ((String) -> Unit)? = null
        var isSyncing: Boolean = false
            private set
        var currentSyncMode: String? = null
            private set
    }

    data class SyncProgress(
        val total: Int,           // 手机端总数
        val synced: Int,          // 本次已同步
        val skipped: Int,         // 跳过（已存在）
        val failed: Int,          // 失败
        val syncMode: String,     // incremental | full
        val currentFile: String,
        val needSync: Int = 0,    // 需要同步的数量
        val pcSynced: Int = 0,    // 电脑端已有数量
        val speed: Double = 0.0,  // 同步速度（MB/s）
        val eta: Long = 0,        // 预计剩余时间（秒）
        val bytesSent: Long = 0   // 已传输字节数
    )

    private val serviceScope = CoroutineScope(Dispatchers.IO + SupervisorJob())
    private var syncJob: Job? = null
    private var wakeLock: PowerManager.WakeLock? = null
    @Volatile
    private var activeClient: SyncClient? = null

    override fun onBind(intent: Intent?): IBinder? = null

    override fun onCreate() {
        super.onCreate()
        // 获取 WakeLock 防止息屏后变慢
        val powerManager = getSystemService(Context.POWER_SERVICE) as PowerManager
        wakeLock = powerManager.newWakeLock(PowerManager.PARTIAL_WAKE_LOCK, "PhotoSync:SyncWakeLock")
        wakeLock?.acquire(30 * 60 * 1000L) // 最多30分钟
        createNotificationChannel()
    }

    override fun onStartCommand(intent: Intent?, flags: Int, startId: Int): Int {
        when (intent?.action) {
            ACTION_START -> {
                val serverIp = intent.getStringExtra(EXTRA_SERVER_IP) ?: ""
                val serverPort = intent.getIntExtra(EXTRA_SERVER_PORT, 8920)
                val modeStr = intent.getStringExtra(EXTRA_CONNECTION_MODE) ?: "WIFI"
                val mode = ConnectionMode.valueOf(modeStr)
                val syncMode = intent.getStringExtra(EXTRA_SYNC_MODE) ?: "incremental"
                startForegroundSync()
                startSync(serverIp, serverPort, mode, syncMode)
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

    private fun detectOptimalConcurrency(mode: ConnectionMode, totalFiles: Int): Int {
        val cores = Runtime.getRuntime().availableProcessors().coerceAtLeast(2)
        val cpuLimit = when {
            cores <= 2 -> 2
            cores <= 4 -> 3
            cores <= 8 -> 4
            else -> 5
        }

        val byFileCount = when {
            totalFiles <= 10 -> 1
            totalFiles <= 50 -> 2
            totalFiles <= 200 -> 3
            else -> 4
        }

        val byMode = when (mode) {
            ConnectionMode.USB -> 4
            ConnectionMode.WIFI -> 3
        }

        return minOf(cpuLimit, byFileCount, byMode).coerceIn(1, 5)
    }

    private fun startSync(serverIp: String, serverPort: Int, mode: ConnectionMode, syncModeRaw: String) {
        if (isSyncing) return
        isSyncing = true

        val syncMode = if (syncModeRaw.lowercase() == "full") "full" else "incremental"
        currentSyncMode = syncMode

        syncJob = serviceScope.launch {
            val scanner = PhotoScanner(applicationContext)
            val client = SyncClient(applicationContext).apply {
                connectionMode = mode
                this.serverIp = serverIp
                this.serverPort = serverPort
            }
            activeClient = client

            try {
                val scanStartTs = System.currentTimeMillis()

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
                val serverStoragePath = (serverStatus["storage_path"] as? String)?.trim().orEmpty()
                log("服务器连接成功 (${if (mode == ConnectionMode.USB) "USB" else "WiFi"})")
                log("电脑端已同步: $pcSyncedCount 个文件")

                // 2) 扫描相册（优先增量，仅在明确条件下全量）
                val prefs = getSharedPreferences(PREFS_NAME, Context.MODE_PRIVATE)
                val lastScanTs = prefs.getLong(KEY_LAST_SCAN_TS, 0L)
                val currentServerId = "${mode.name}|${serverIp.trim()}|${client.serverPort}"
                val previousServerId = prefs.getString(KEY_LAST_SERVER_ID, "") ?: ""
                val previousStoragePath = prefs.getString(KEY_LAST_SERVER_STORAGE_PATH, "") ?: ""
                val previousPcTotal = prefs.getInt(KEY_LAST_PC_TOTAL, -1)
                val hasLegacyCursorOnly = lastScanTs > 0L &&
                    (previousServerId.isEmpty() || previousStoragePath.isEmpty())
                val pcTotalDropped = previousPcTotal >= 0 && pcSyncedCount < previousPcTotal

                val serverChanged = previousServerId.isNotEmpty() && previousServerId != currentServerId
                val storagePathChanged = serverStoragePath.isNotEmpty() &&
                    previousStoragePath.isNotEmpty() &&
                    previousStoragePath != serverStoragePath
                val shouldBootstrapIncremental =
                    syncMode != "full" && lastScanTs <= 0L && pcSyncedCount > 0
                val forceFullScan =
                    syncMode == "full" ||
                    serverChanged || storagePathChanged || (lastScanTs <= 0L && !shouldBootstrapIncremental) ||
                    pcTotalDropped

                if (syncMode == "full") {
                    log("已选择全量同步模式，本次将全量扫描")
                } else {
                    log("已选择增量同步模式")
                }

                if (serverChanged) {
                    log("检测到服务器目标变化，切换为全量扫描")
                }
                if (storagePathChanged) {
                    log("检测到电脑端存储路径已变更，切换为全量扫描")
                }
                if (hasLegacyCursorOnly) {
                    log("检测到旧版本扫描游标，自动补齐服务器上下文并继续增量")
                    prefs.edit()
                        .putString(KEY_LAST_SERVER_ID, currentServerId)
                        .putString(KEY_LAST_SERVER_STORAGE_PATH, serverStoragePath)
                        .putInt(KEY_LAST_PC_TOTAL, pcSyncedCount)
                        .apply()
                }
                if (shouldBootstrapIncremental) {
                    log("检测到电脑端已有历史同步数据且本机无增量游标，已初始化增量基线")
                    prefs.edit()
                        .putLong(KEY_LAST_SCAN_TS, scanStartTs)
                        .putString(KEY_LAST_SERVER_ID, currentServerId)
                        .putString(KEY_LAST_SERVER_STORAGE_PATH, serverStoragePath)
                        .putInt(KEY_LAST_PC_TOTAL, pcSyncedCount)
                        .apply()
                }
                if (pcTotalDropped) {
                    log("检测到电脑端索引数量回退（可能已刷新数据库），切换为全量扫描")
                }

                var phoneLibraryTotal = 0
                val allPhotos = if (!forceFullScan) {
                    log("正在增量扫描手机相册...")
                    val incremental = scanner.scanSince(lastScanTs)
                    if (incremental.isEmpty()) {
                        val fullSnapshot = scanner.scanAll()
                        phoneLibraryTotal = fullSnapshot.size
                        if (fullSnapshot.isEmpty()) {
                            log("增量扫描结果为空，本次无需同步")
                            incremental
                        } else {
                            log("增量为空，开始与电脑数据库比对手机清单...")
                            val batchSize = 500
                            val needSync = mutableListOf<PhotoInfo>()
                            for (batchStart in fullSnapshot.indices step batchSize) {
                                val batch = fullSnapshot.subList(
                                    batchStart,
                                    minOf(batchStart + batchSize, fullSnapshot.size)
                                )
                                val manifest = batch.map { p ->
                                    mapOf(
                                        "album" to (p.bucketName.ifBlank { "unsorted" }),
                                        "filename" to p.displayName,
                                        "size" to p.size,
                                    )
                                }

                                val compareResult = client.checkManifest(manifest)
                                if (compareResult.isFailure) {
                                    log("清单比对失败，回退全量补齐: ${compareResult.exceptionOrNull()?.message}")
                                    needSync.clear()
                                    needSync.addAll(fullSnapshot)
                                    break
                                }

                                val existsMap = compareResult.getOrNull().orEmpty()
                                for (photo in batch) {
                                    val key = "${photo.bucketName.ifBlank { "unsorted" }}|${photo.displayName}|${photo.size}"
                                    if (existsMap[key] != true) {
                                        needSync.add(photo)
                                    }
                                }
                            }
                            log("数据库比对完成：手机总数=$phoneLibraryTotal，需补齐=${needSync.size}")
                            needSync
                        }
                    } else {
                        // 增量模式下为电脑端统计补充总量展示
                        phoneLibraryTotal = maxOf(incremental.size, prefs.getInt("last_phone_total", 0))
                        log("增量扫描发现 ${incremental.size} 个新增/更新文件")
                        incremental
                    }
                } else {
                    log("正在全量扫描手机相册（首次同步）...")
                    scanner.scanAll().also {
                        phoneLibraryTotal = it.size
                        log("手机端发现 ${it.size} 个文件")
                    }
                }

                if (phoneLibraryTotal <= 0) {
                    phoneLibraryTotal = allPhotos.size
                }
                prefs.edit().putInt("last_phone_total", phoneLibraryTotal).apply()

                if (allPhotos.isEmpty()) {
                    log("增量无变化，无需同步")
                    // 没有新增也更新扫描时间，避免下次重复全量扫描
                    prefs.edit()
                        .putLong(KEY_LAST_SCAN_TS, scanStartTs)
                        .putString(KEY_LAST_SERVER_ID, currentServerId)
                        .putString(KEY_LAST_SERVER_STORAGE_PATH, serverStoragePath)
                        .putInt(KEY_LAST_PC_TOTAL, pcSyncedCount)
                        .apply()
                    isSyncing = false
                    stopSelf()
                    return@launch
                }

                // 3) 使用电脑端校验：手机端不再预先计算 MD5，直接上传由服务端最终判重
                log("已启用电脑端校验：跳过手机端 MD5 预计算")
                val batchSize = 100
                val deviceName = android.os.Build.MODEL

                for (batchStart in allPhotos.indices step batchSize) {
                    if (!isSyncing) break
                    if (client.checkStopRequest()) {
                        log("收到电脑端停止请求，正在停止同步...")
                        isSyncing = false
                        break
                    }
                    val scanned = minOf(batchStart + batchSize, allPhotos.size)
                    client.notifyScanProgress(deviceName, scanned, phoneLibraryTotal, syncMode)
                    log("已扫描 $scanned / ${allPhotos.size}...")
                }

                if (!isSyncing) {
                    client.notifySyncStop("手机端已停止同步")
                    return@launch
                }

                val needSyncCount = allPhotos.size
                val alreadySyncedCount = 0
                val photosNeedSync = allPhotos
                val uploadConcurrency = detectOptimalConcurrency(mode, needSyncCount)

                log("========== 同步统计 ==========")
                log("手机端照片总数: $phoneLibraryTotal")
                log("电脑端已同步: $pcSyncedCount")
                log("本次已跳过（相册内重复）: $alreadySyncedCount")
                log("本次需要同步: $needSyncCount")
                log("==============================")

                // 通知服务器同步统计（网页端显示进度）
                client.notifySyncStart(deviceName, phoneLibraryTotal, needSyncCount, syncMode)

                // 5) 开始上传需要同步的文件
                log("开始同步 $needSyncCount 个文件...（自动并发 $uploadConcurrency 路）")
                val syncedCounter = AtomicInteger(0)
                val skippedCounter = AtomicInteger(alreadySyncedCount)
                val failedCounter = AtomicInteger(0)
                val bytesSentCounter = AtomicLong(0L)
                val firstByteTs = AtomicLong(0L)

                val queue = ArrayDeque(photosNeedSync)
                val queueLock = Any()

                val workers = (0 until uploadConcurrency).map {
                    launch(Dispatchers.IO) {
                        while (isSyncing) {
                            if (client.checkStopRequest()) {
                                log("收到电脑端停止请求，正在停止同步...")
                                isSyncing = false
                                break
                            }

                            val photo = synchronized(queueLock) {
                                if (queue.isNotEmpty()) queue.removeFirst() else null
                            } ?: break

                            try {
                                val uploadResult = client.uploadPhoto(photo, scanner)
                                if (uploadResult.isSuccess) {
                                    val result = uploadResult.getOrNull()!!
                                    if (result.status == "ok") {
                                        syncedCounter.incrementAndGet()
                                        // 优先使用服务端返回的真实写入字节数，避免部分设备 photo.size=0 导致速度始终为 0
                                        val measuredSize = if (result.size > 0L) result.size else photo.size
                                        val uploadedBytes = bytesSentCounter.addAndGet(measuredSize.coerceAtLeast(0L))
                                        if (uploadedBytes > 0L && firstByteTs.get() == 0L) {
                                            firstByteTs.compareAndSet(0L, System.currentTimeMillis())
                                        }
                                        log("已同步: ${photo.displayName}")
                                    } else {
                                        skippedCounter.incrementAndGet()
                                    }
                                } else {
                                    failedCounter.incrementAndGet()
                                    log("失败: ${photo.displayName} - ${uploadResult.exceptionOrNull()?.message}")
                                }
                            } catch (e: OutOfMemoryError) {
                                failedCounter.incrementAndGet()
                                log("内存不足跳过: ${photo.displayName}")
                                System.gc()
                            } catch (e: Exception) {
                                failedCounter.incrementAndGet()
                                log("失败: ${photo.displayName} - ${e.message}")
                            }

                            val synced = syncedCounter.get()
                            val skipped = skippedCounter.get()
                            val failed = failedCounter.get()
                            val bytesSent = bytesSentCounter.get()
                            val done = synced + skipped + failed

                            // 计算速度和剩余时间 (MB/s)
                            val beginTs = firstByteTs.get()
                            val elapsedMs = if (beginTs > 0L) (System.currentTimeMillis() - beginTs) else 0L
                            val speed = if (elapsedMs > 0 && bytesSent > 0L) {
                                (bytesSent / 1024.0 / 1024.0) / (elapsedMs / 1000.0)
                            } else 0.0
                            val remaining = (needSyncCount - done).coerceAtLeast(0)
                            val eta = if (speed > 0 && synced > 0) {
                                val avgBytes = bytesSent.toDouble() / synced.toDouble()
                                val remainingBytes = avgBytes * remaining.toDouble()
                                (remainingBytes / 1024.0 / 1024.0 / speed).toLong()
                            } else 0L

                            val progress = SyncProgress(
                                total = allPhotos.size,
                                synced = synced,
                                skipped = skipped,
                                failed = failed,
                                syncMode = syncMode,
                                currentFile = photo.displayName,
                                needSync = needSyncCount,
                                pcSynced = pcSyncedCount + synced,
                                speed = speed,
                                eta = eta,
                                bytesSent = bytesSent
                            )
                            updateProgress(progress)
                            updateNotification("已同步 $synced / $needSyncCount")

                            // 每上传一个文件，通知服务器更新进度
                            client.notifySyncProgress(
                                photo.displayName,
                                synced,
                                skipped,
                                failed,
                                bytesSent,
                                speed,
                                eta,
                            )
                        }
                    }
                }

                workers.joinAll()

                val synced = syncedCounter.get()
                val skipped = skippedCounter.get()
                val failed = failedCounter.get()
                val done = synced + skipped + failed
                val completedAll = done >= needSyncCount
                val summaryMsg = if (completedAll && isSyncing) {
                    "同步完成: $synced 个, 跳过: $skipped, 失败: $failed"
                } else {
                    "同步已停止: 已完成 $done/$needSyncCount, 成功: $synced, 跳过: $skipped, 失败: $failed"
                }
                client.notifySyncStop(summaryMsg)

                // 仅在完整结束后推进增量游标，避免中途中断造成漏传
                if (completedAll && isSyncing) {
                    prefs.edit()
                        .putLong(KEY_LAST_SCAN_TS, scanStartTs)
                        .putString(KEY_LAST_SERVER_ID, currentServerId)
                        .putString(KEY_LAST_SERVER_STORAGE_PATH, serverStoragePath)
                        .putInt(KEY_LAST_PC_TOTAL, pcSyncedCount + synced)
                        .apply()
                } else {
                    log("检测到同步未完整结束，本次不更新增量扫描游标")
                }

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
                activeClient = null
                client.shutdown()
                isSyncing = false
                currentSyncMode = null
                stopSelf()
            }
        }
    }

    private fun stopSync() {
        // 本地主动停止时，立即通知服务器结束同步，避免 PC 端卡在“等待手机端确认”。
        try {
            activeClient?.notifySyncStop("手机端已停止同步")
        } catch (_: Exception) {
            // 忽略通知失败
        }

        isSyncing = false
        currentSyncMode = null
        syncJob?.cancel()
        stopForeground(STOP_FOREGROUND_REMOVE)
        stopSelf()
    }

    private fun updateProgress(progress: SyncProgress) {
        onSyncProgress?.invoke(progress)
    }

    private fun log(message: String) {
        onSyncLog?.invoke(message)
        try {
            activeClient?.notifySyncLog(message)
        } catch (_: Exception) {
            // 忽略上报错误
        }
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
        currentSyncMode = null
        syncJob?.cancel()
        serviceScope.cancel()
        // 释放 WakeLock
        try {
            if (wakeLock != null && wakeLock!!.isHeld) {
                wakeLock!!.release()
            }
        } catch (e: Exception) {
            // 忽略
        }
    }
}
