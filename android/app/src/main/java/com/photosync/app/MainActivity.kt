package com.photosync.app

import android.Manifest
import android.content.Context
import android.content.Intent
import android.content.SharedPreferences
import android.content.res.ColorStateList
import android.content.res.Configuration
import android.content.pm.PackageManager
import android.net.Uri
import android.os.Build
import android.os.Bundle
import android.os.Handler
import android.os.Looper
import android.os.PowerManager
import android.os.SystemClock
import android.provider.Settings
import android.view.View
import android.widget.Toast
import androidx.activity.result.contract.ActivityResultContracts
import androidx.appcompat.app.AppCompatDelegate
import androidx.appcompat.app.AppCompatActivity
import androidx.core.app.ActivityCompat
import androidx.core.content.ContextCompat
import androidx.lifecycle.lifecycleScope
import com.journeyapps.barcodescanner.ScanContract
import com.journeyapps.barcodescanner.ScanIntentResult
import com.journeyapps.barcodescanner.ScanOptions
import com.photosync.app.databinding.ActivityMainBinding
import com.google.gson.Gson
import okhttp3.OkHttpClient
import okhttp3.Request
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.launch
import kotlinx.coroutines.withContext
import java.util.concurrent.TimeUnit

class MainActivity : AppCompatActivity() {

    private lateinit var binding: ActivityMainBinding
    private lateinit var prefs: SharedPreferences
    private val logLines = mutableListOf<String>()
    private var isConnected = false
    private val handler = Handler(Looper.getMainLooper())
    private var pollRunnable: Runnable? = null
    private var syncClientForPoll: SyncClient? = null
    private var activeSyncMode: String? = null
    private var localPendingMode: String? = null
    private var remotePhase: String = ""
    private var remoteRequestedMode: String = SYNC_MODE_INCREMENTAL
    private var remoteSyncMode: String = SYNC_MODE_INCREMENTAL
    private var remoteStoppingSinceMs: Long = 0L
    private var consecutivePollFailures: Int = 0

    companion object {
        private const val PERMISSION_REQUEST_CODE = 1001
        private const val PREFS_NAME = "photosync_prefs"
        private const val KEY_SERVER_IP = "server_ip"
        private const val KEY_SERVER_PORT = "server_port"
        private const val KEY_CONNECTION_MODE = "connection_mode"
        private const val KEY_SYNCED_COUNT = "synced_count"
        private const val KEY_TOTAL_PHOTOS = "total_photos"
        private const val KEY_UI_THEME = "ui_theme" // system | light | dark
        private const val THEME_LIGHT = "light"
        private const val THEME_DARK = "dark"
        private const val DEFAULT_SERVER_IP = "192.168.1.7"
        private const val SYNC_MODE_INCREMENTAL = "incremental"
        private const val SYNC_MODE_FULL = "full"
        private const val STOPPING_UI_TIMEOUT_MS = 4000L
    }

    private val btnPrimaryBlue by lazy {
        ColorStateList.valueOf(ContextCompat.getColor(this, R.color.brand_primary))
    }
    private val btnStopRed by lazy {
        ColorStateList.valueOf(ContextCompat.getColor(this, R.color.danger))
    }

    // 二维码扫描结果回调
    private val barcodeLauncher = registerForActivityResult(ScanContract()) { result: ScanIntentResult ->
        if (result.contents != null) {
            handleQrResult(result.contents)
        } else {
            addLog("扫码已取消")
        }
    }

    // 相机权限请求（用于扫码）
    private val cameraPermissionLauncher = registerForActivityResult(
        ActivityResultContracts.RequestPermission()
    ) { granted ->
        if (granted) {
            launchQrScanner()
        } else {
            Toast.makeText(this, "需要相机权限才能扫描二维码", Toast.LENGTH_SHORT).show()
        }
    }

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)

        // 必须在 setContentView 之前应用，否则会闪一下
        prefs = getSharedPreferences(PREFS_NAME, Context.MODE_PRIVATE)
        applyThemeFromPrefs()

        binding = ActivityMainBinding.inflate(layoutInflater)
        setContentView(binding.root)

        setupUI()
        // 防止在主题切换后的重建过程中按钮文案残留
        updateThemeToggleButtonText()
        activeSyncMode = SyncService.currentSyncMode
        restoreSettings()
        initPollingFromCurrentSettings()
        requestPermissions()
        setupSyncCallbacks()
        requestIgnoreBatteryOptimizations()
    }

    private fun applyThemeFromPrefs() {
        val theme = prefs.getString(KEY_UI_THEME, null)
        val targetNightMode = when (theme) {
            THEME_LIGHT -> AppCompatDelegate.MODE_NIGHT_NO
            THEME_DARK -> AppCompatDelegate.MODE_NIGHT_YES
            else -> AppCompatDelegate.MODE_NIGHT_FOLLOW_SYSTEM
        }

        val currentNightMode = AppCompatDelegate.getDefaultNightMode()
        if (currentNightMode == AppCompatDelegate.MODE_NIGHT_UNSPECIFIED && targetNightMode == AppCompatDelegate.MODE_NIGHT_FOLLOW_SYSTEM) {
            return
        }

        if (currentNightMode != targetNightMode) {
            AppCompatDelegate.setDefaultNightMode(targetNightMode)
        }
    }

    // 请求忽略电池优化
    private fun requestIgnoreBatteryOptimizations() {
        try {
            val intent = Intent(Settings.ACTION_REQUEST_IGNORE_BATTERY_OPTIMIZATIONS)
            intent.data = Uri.parse("package:$packageName")
            batteryOptimLauncher.launch(intent)
        } catch (e: Exception) {
            // 某些设备可能不支持
        }
    }

    private val batteryOptimLauncher = registerForActivityResult(
        ActivityResultContracts.StartActivityForResult()
    ) { /* 用户选择后不做特别处理 */ }

    private fun setupUI() {
        binding.tvSubtitle.text = getAppVersionLabel()

        // 左上角主题切换：一键深/浅色
        updateThemeToggleButtonText()
        binding.btnThemeToggle.setOnClickListener {
            val isDark = isNightActive()
            val newTheme = if (isDark) THEME_LIGHT else THEME_DARK
            prefs.edit().putString(KEY_UI_THEME, newTheme).apply()
            applyThemeFromPrefs()
            recreate()
        }

        // GitHub 跳转按钮
        binding.btnGithub.setOnClickListener {
            try {
                val intent = Intent(Intent.ACTION_VIEW, Uri.parse("https://github.com/Sierraki"))
                startActivity(intent)
            } catch (e: Exception) {
                Toast.makeText(this, "无法打开链接", Toast.LENGTH_SHORT).show()
            }
        }

        // 连接模式切换
        binding.rgConnectionMode.setOnCheckedChangeListener { _, checkedId ->
            isConnected = false
            updateConnectionStatus(connected = false, message = "未连接")
            updateSyncButtons()
            when (checkedId) {
                R.id.rbWifi -> {
                    binding.layoutWifiConfig.visibility = View.VISIBLE
                    binding.layoutUsbConfig.visibility = View.GONE
                    binding.tvUsbHint.visibility = View.GONE
                }
                R.id.rbUsb -> {
                    binding.layoutWifiConfig.visibility = View.GONE
                    binding.layoutUsbConfig.visibility = View.VISIBLE
                    binding.tvUsbHint.visibility = View.GONE
                    updateConnectionStatus(connected = false, message = "未连接")
                }
            }
            initPollingFromCurrentSettings()
        }

        // 扫码连接按钮
        binding.btnScanQr.setOnClickListener {
            startQrScan()
        }

        // 测试连接按钮 (WiFi)
        binding.btnTestConnection.setOnClickListener {
            testConnection()
        }

        // 测试连接按钮 (USB)
        binding.btnTestUsbConnection.setOnClickListener {
            testConnection()
        }

        binding.btnSyncIncremental.setOnClickListener {
            onSyncModeButtonClicked(SYNC_MODE_INCREMENTAL)
        }

        binding.btnSyncFull.setOnClickListener {
            // 同步中：右侧按钮作为“停止同步”；空闲时：作为“全量同步”启动
            if (isUiBusy()) {
                onStopButtonClicked()
            } else {
                onSyncModeButtonClicked(SYNC_MODE_FULL)
            }
        }

        updateSyncButtons()
        
        // 获取电脑 IP 地址
        fetchAndDisplayPcIp()
    }

    private fun isUiBusy(): Boolean {
        val localSyncing = SyncService.isSyncing
        val localPending = !localSyncing && (localPendingMode == SYNC_MODE_FULL || localPendingMode == SYNC_MODE_INCREMENTAL)
        val remoteBusy = isRemoteBusyWithoutLocalSync()
        return localSyncing || remoteBusy || localPending
    }

    private fun onStopButtonClicked() {
        if (remotePhase == "stopping") {
            Toast.makeText(this, "正在停止，请稍候...", Toast.LENGTH_SHORT).show()
            return
        }

        // 本地同步或本地 pending：直接停止
        if (SyncService.isSyncing || (!SyncService.isSyncing && (localPendingMode == SYNC_MODE_FULL || localPendingMode == SYNC_MODE_INCREMENTAL))) {
            stopSync()
            return
        }

        // 仅远端在跑：请求远端停止
        if (isRemoteBusyWithoutLocalSync()) {
            requestRemoteStopOrCancel()
            return
        }
    }

    private fun isNightActive(): Boolean {
        val nightMode = resources.configuration.uiMode and Configuration.UI_MODE_NIGHT_MASK
        return nightMode == Configuration.UI_MODE_NIGHT_YES
    }

    private fun updateThemeToggleButtonText() {
        // 文案表示“点击后切换到哪种模式”
        binding.btnThemeToggle.text = if (isNightActive()) "浅色" else "深色"
    }

    private fun getAppVersionLabel(): String {
        val versionName = try {
            if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.TIRAMISU) {
                packageManager.getPackageInfo(
                    packageName,
                    PackageManager.PackageInfoFlags.of(0)
                ).versionName
            } else {
                @Suppress("DEPRECATION")
                packageManager.getPackageInfo(packageName, 0).versionName
            }
        } catch (e: Exception) {
            null
        }

        return if (versionName.isNullOrBlank()) "Version -" else "Version $versionName"
    }

    private fun onSyncModeButtonClicked(mode: String) {
        if (remotePhase == "stopping") {
            Toast.makeText(this, "正在停止，请稍候...", Toast.LENGTH_SHORT).show()
            return
        }

        if (!SyncService.isSyncing && localPendingMode == mode) {
            stopSync()
            return
        }

        if (isRemoteBusyWithoutLocalSync()) {
            val remoteMode = getRemoteEffectiveMode()
            if (remoteMode == mode) {
                requestRemoteStopOrCancel()
            } else {
                Toast.makeText(this, "当前：${if (remoteMode == SYNC_MODE_FULL) "全量" else "增量"}", Toast.LENGTH_SHORT).show()
            }
            return
        }

        val serviceMode = SyncService.currentSyncMode
        if (SyncService.isSyncing && (activeSyncMode == mode || serviceMode == mode || serviceMode == null)) {
            stopSync()
            return
        }

        if (!isConnected) {
            Toast.makeText(this, "请先测试连接", Toast.LENGTH_SHORT).show()
            return
        }

        if (SyncService.isSyncing) {
            Toast.makeText(this, "正在同步中，请先停止当前任务", Toast.LENGTH_SHORT).show()
            return
        }

        startSync(mode)
    }

    private fun updateSyncButtons() {
        maybeRecoverFromStaleStopping()

        val localSyncing = SyncService.isSyncing
        val localPending = !localSyncing && (localPendingMode == SYNC_MODE_FULL || localPendingMode == SYNC_MODE_INCREMENTAL)
        val remoteBusy = isRemoteBusyWithoutLocalSync()
        val syncing = localSyncing || remoteBusy || localPending
        val canStart = !syncing
        val effectiveMode = if (localSyncing) {
            activeSyncMode ?: SyncService.currentSyncMode
        } else if (localPending) {
            localPendingMode
        } else if (remoteBusy) {
            getRemoteEffectiveMode()
        } else {
            activeSyncMode
        }

        val inferredMode = when {
            effectiveMode == SYNC_MODE_FULL || effectiveMode == SYNC_MODE_INCREMENTAL -> effectiveMode
            syncing && binding.tvSyncMode.text.contains("全量") -> SYNC_MODE_FULL
            syncing && binding.tvSyncMode.text.contains("增量") -> SYNC_MODE_INCREMENTAL
            syncing -> SYNC_MODE_INCREMENTAL
            else -> null
        }

        val stopping = remotePhase == "stopping"

        // 与截图一致：左侧固定显示“当前：增量/全量”（同步中不可点）；右侧固定为“停止同步”（同步中可点）
        if (syncing) {
            val modeLabel = if (inferredMode == SYNC_MODE_FULL) "全量" else "增量"
            binding.btnSyncIncremental.text = "当前：$modeLabel"
            binding.btnSyncFull.text = if (stopping) "停止中..." else "停止同步"

            binding.btnSyncIncremental.isEnabled = false
            binding.btnSyncFull.isEnabled = !stopping

            binding.btnSyncIncremental.backgroundTintList = btnPrimaryBlue
            binding.btnSyncFull.backgroundTintList = btnStopRed

            val primaryText = ContextCompat.getColor(this, R.color.brand_on_primary)
            val stopText = ContextCompat.getColor(this, R.color.danger_on)
            binding.btnSyncIncremental.setTextColor(primaryText)
            binding.btnSyncFull.setTextColor(stopText)
        } else {
            binding.btnSyncIncremental.text = "增量同步"
            binding.btnSyncFull.text = "全量同步"

            binding.btnSyncIncremental.isEnabled = canStart
            binding.btnSyncFull.isEnabled = canStart

            binding.btnSyncIncremental.backgroundTintList = btnPrimaryBlue
            binding.btnSyncFull.backgroundTintList = btnPrimaryBlue

            val primaryText = ContextCompat.getColor(this, R.color.brand_on_primary)
            binding.btnSyncIncremental.setTextColor(primaryText)
            binding.btnSyncFull.setTextColor(primaryText)
        }
    }

    private fun formatSpeedMb(speedMb: Double): String {
        val v = speedMb.coerceAtLeast(0.0)
        return String.format("%.1f MB/s", v)
    }

    private fun normalizeMode(raw: String?): String {
        return if (raw?.lowercase() == SYNC_MODE_FULL) SYNC_MODE_FULL else SYNC_MODE_INCREMENTAL
    }

    private fun isRemoteBusyWithoutLocalSync(): Boolean {
        if (SyncService.isSyncing) return false
        return remotePhase in setOf("requested", "preparing_full", "scanning", "syncing", "stopping")
    }

    private fun getRemoteEffectiveMode(): String {
        val fromSync = normalizeMode(remoteSyncMode)
        if (fromSync == SYNC_MODE_FULL) return SYNC_MODE_FULL
        return normalizeMode(remoteRequestedMode)
    }

    private fun requestRemoteStopOrCancel() {
        val client = syncClientForPoll
        if (client == null) {
            Toast.makeText(this, "未连接到服务器", Toast.LENGTH_SHORT).show()
            return
        }

        lifecycleScope.launch {
            val result = withContext(Dispatchers.IO) {
                client.requestStopSync()
            }

            if (result.isSuccess) {
                val msg = result.getOrNull() ?: "已发送停止请求"
                addLog(msg)
                remotePhase = "stopping"
                remoteStoppingSinceMs = SystemClock.elapsedRealtime()
                binding.tvSyncSpeed.text = "当前: 已请求停止，等待手机端确认..."
                updateSyncButtons()
            } else {
                val err = result.exceptionOrNull()?.message ?: "停止失败"
                Toast.makeText(this@MainActivity, err, Toast.LENGTH_SHORT).show()
                addLog("停止请求失败: $err")
            }
        }
    }

    private fun applyRemoteStatusSnapshot(status: Map<String, Any>) {
        remotePhase = (status["phase"] as? String)?.trim().orEmpty()
        remoteRequestedMode = normalizeMode(status["requested_sync_mode"] as? String)
        remoteSyncMode = normalizeMode(status["sync_mode"] as? String)
        if (remotePhase == "stopping") {
            if (remoteStoppingSinceMs == 0L) {
                remoteStoppingSinceMs = SystemClock.elapsedRealtime()
            }
        } else {
            remoteStoppingSinceMs = 0L
        }

        if (SyncService.isSyncing) {
            localPendingMode = null
            return
        }

        val remoteMode = getRemoteEffectiveMode()
        val modeText = if (remoteMode == SYNC_MODE_FULL) "全量" else "增量"
        val currentText = (status["current"] as? String)?.trim().orEmpty()
        val needSync = (status["need_sync"] as? Number)?.toInt() ?: 0
        val synced = (status["synced"] as? Number)?.toInt() ?: 0
        val skipped = (status["skipped"] as? Number)?.toInt() ?: 0
        val failed = (status["failed"] as? Number)?.toInt() ?: 0
        val done = synced + skipped + failed
        val speed = (status["speed"] as? Number)?.toDouble() ?: 0.0

        binding.tvSpeedValue.text = formatSpeedMb(speed)

        if (isRemoteBusyWithoutLocalSync()) {
            localPendingMode = null
            binding.progressSync.visibility = View.VISIBLE
            binding.tvSyncMode.text = "同步状态：$modeText"
            val pct = if (needSync > 0) (done * 100) / needSync else 0
            binding.tvSyncProgress.text = if (needSync > 0) {
                "上传进度: $done/$needSync ($pct%)"
            } else {
                "上传进度: 0/0 (0%)"
            }
            binding.tvSyncSpeed.text = "当前: ${if (currentText.isNotEmpty()) currentText else "正在准备..."}"
        } else if (remotePhase == "done") {
            binding.progressSync.visibility = View.GONE
            binding.tvSyncMode.text = "同步状态：-"
            binding.tvSyncProgress.text = "上传已完成"
            binding.tvSyncSpeed.text = if (currentText.isNotEmpty()) "当前: $currentText" else ""
            binding.tvSpeedValue.text = formatSpeedMb(0.0)
        }

        updateSyncButtons()
    }

    private fun maybeRecoverFromStaleStopping() {
        if (remotePhase != "stopping") return
        if (remoteStoppingSinceMs == 0L) return

        val elapsed = SystemClock.elapsedRealtime() - remoteStoppingSinceMs
        if (elapsed < STOPPING_UI_TIMEOUT_MS) return

        remotePhase = ""
        remoteRequestedMode = SYNC_MODE_INCREMENTAL
        remoteSyncMode = SYNC_MODE_INCREMENTAL
        remoteStoppingSinceMs = 0L
        localPendingMode = null
        if (!SyncService.isSyncing) {
            binding.progressSync.visibility = View.GONE
            binding.tvSyncMode.text = "同步状态：-"
            binding.tvSyncProgress.text = "上传已停止"
            binding.tvSyncSpeed.text = "当前: -"
            binding.tvSpeedValue.text = formatSpeedMb(0.0)
        }
    }

    private fun initPollingFromCurrentSettings() {
        val mode = if (binding.rbUsb.isChecked) ConnectionMode.USB else ConnectionMode.WIFI
        val serverIp = binding.etServerIp.text.toString().trim()
        val serverPort = binding.etServerPort.text.toString().trim().toIntOrNull() ?: 9001

        if (mode == ConnectionMode.WIFI && serverIp.isEmpty()) {
            stopPolling()
            syncClientForPoll?.shutdown()
            syncClientForPoll = null
            return
        }

        syncClientForPoll?.shutdown()
        syncClientForPoll = SyncClient(applicationContext).apply {
            connectionMode = mode
            this.serverIp = serverIp
            this.serverPort = serverPort
        }
        startPolling()
    }

    /**
     * 从 SharedPreferences 恢复上次的设置
     */
    private fun restoreSettings() {
        // 恢复 IP 地址（格式可能是 "ip:port" 兼容旧版）
        val savedAddress = prefs.getString(KEY_SERVER_IP, "") ?: ""
        val savedPort = prefs.getString(KEY_SERVER_PORT, "9001") ?: "9001"
        
        if (savedAddress.isNotEmpty()) {
            if (savedAddress.contains(":")) {
                // 旧版格式 "ip:port"，拆分
                val parts = savedAddress.split(":")
                binding.etServerIp.setText(parts[0])
                if (parts.size > 1) {
                    binding.etServerPort.setText(parts[1])
                } else {
                    binding.etServerPort.setText(savedPort)
                }
            } else {
                // 新版格式，分开存储
                binding.etServerIp.setText(savedAddress)
                binding.etServerPort.setText(savedPort)
            }
        } else {
            binding.etServerIp.setText(DEFAULT_SERVER_IP)
            binding.etServerPort.setText("9001")
        }

        val savedMode = prefs.getString(KEY_CONNECTION_MODE, "WIFI") ?: "WIFI"
        if (savedMode == "USB") {
            binding.rbUsb.isChecked = true
        }

        val savedSynced = prefs.getInt(KEY_SYNCED_COUNT, 0)
        if (savedSynced > 0) {
            binding.tvSyncedPhotos.text = savedSynced.toString()
        }
    }

    /**
     * 保存当前设置到 SharedPreferences
     */
    private fun saveSettings() {
        val ip = binding.etServerIp.text.toString().trim()
        val port = binding.etServerPort.text.toString().trim().ifEmpty { "9001" }
        
        prefs.edit()
            .putString(KEY_SERVER_IP, ip)
            .putString(KEY_SERVER_PORT, port)
            .putString(KEY_CONNECTION_MODE, if (binding.rbUsb.isChecked) "USB" else "WIFI")
            .apply()
    }

    private fun saveSyncStats(synced: Int, total: Int) {
        prefs.edit()
            .putInt(KEY_SYNCED_COUNT, synced)
            .putInt(KEY_TOTAL_PHOTOS, total)
            .apply()
    }

    /**
     * 获取并显示电脑 IP 地址
     */
    private fun fetchAndDisplayPcIp() {
        val preferredPort = binding.etServerPort.text
            ?.toString()
            ?.trim()
            ?.toIntOrNull()
            ?: (prefs.getString(KEY_SERVER_PORT, "9001")?.toIntOrNull() ?: 9001)

        lifecycleScope.launch {
            try {
                withContext(Dispatchers.IO) {
                    val urls = buildList {
                        add("http://127.0.0.1:$preferredPort/api/server-info")
                        add("http://127.0.0.1:9002/api/server-info")
                        add("http://127.0.0.1:9001/api/server-info")
                        add("http://127.0.0.1:8920/api/server-info")
                    }.distinct()
                    
                    for (url in urls) {
                        try {
                            val request = Request.Builder().url(url).get().build()
                            val client = OkHttpClient.Builder()
                                .connectTimeout(3, TimeUnit.SECONDS)
                                .build()
                            val response = client.newCall(request).execute()
                            
                            if (response.isSuccessful) {
                                val jsonStr = response.body?.string() ?: continue
                                val json = Gson().fromJson(jsonStr, Map::class.java)
                                
                                val serverIp = json["server_ip"] as? String
                                if (serverIp != null) {
                                    runOnUiThread {
                                        binding.tvPcIp.text = serverIp
                                        // 如果输入框为空，自动填充
                                        if (binding.etServerIp.text.toString().isEmpty()) {
                                            binding.etServerIp.setText(serverIp)
                                        }
                                    }
                                    return@withContext
                                }
                            }
                        } catch (e: Exception) {
                            // 继续尝试下一个 URL
                        }
                    }
                    
                    // 所有尝试都失败
                    runOnUiThread {
                        binding.tvPcIp.text = "未检测到"
                    }
                }
            } catch (e: Exception) {
                runOnUiThread {
                    binding.tvPcIp.text = "获取失败"
                }
            }
        }
    }

    /**
     * 启动二维码扫描
     */
    private fun startQrScan() {
        if (checkSelfPermission(Manifest.permission.CAMERA) != PackageManager.PERMISSION_GRANTED) {
            cameraPermissionLauncher.launch(Manifest.permission.CAMERA)
        } else {
            launchQrScanner()
        }
    }

    private fun launchQrScanner() {
        val options = ScanOptions().apply {
            setDesiredBarcodeFormats(ScanOptions.QR_CODE)
            setPrompt("将二维码置于取景框内扫描")
            setCameraId(0)
            setBeepEnabled(false)
            setOrientationLocked(true)
        }
        barcodeLauncher.launch(options)
    }

    /**
     * 处理二维码扫描结果：解析 URL，填入地址，直接连接
     */
    private fun handleQrResult(contents: String) {
        addLog("扫码结果: $contents")
        try {
            val uri = Uri.parse(contents)
            val host = uri.host ?: ""
            val port = uri.port

            if (host.isEmpty()) {
                Toast.makeText(this, "无效的二维码内容", Toast.LENGTH_SHORT).show()
                addLog("无效二维码: 无法解析主机地址")
                return
            }

            // 分别设置 IP 和端口
            binding.etServerIp.setText(host)
            if (port > 0) {
                binding.etServerPort.setText(port.toString())
            } else {
                binding.etServerPort.setText("9001")  // 默认端口
            }

            // 切换到 WiFi 模式
            binding.rbWifi.isChecked = true
            binding.layoutWifiConfig.visibility = View.VISIBLE
            binding.tvUsbHint.visibility = View.GONE

            val address = if (port > 0) "$host:$port" else "$host:9001"
            addLog("扫码识别地址: $address，正在连接...")
            Toast.makeText(this, "正在连接 $address ...", Toast.LENGTH_SHORT).show()

            // 直接连接（不只是填入地址）
            testConnection()
        } catch (e: Exception) {
            Toast.makeText(this, "二维码解析失败: ${e.message}", Toast.LENGTH_SHORT).show()
            addLog("二维码解析失败: ${e.message}")
        }
    }

    private fun requestPermissions() {
        val perms = mutableListOf<String>()

        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.TIRAMISU) {
            if (checkSelfPermission(Manifest.permission.READ_MEDIA_IMAGES) != PackageManager.PERMISSION_GRANTED) {
                perms.add(Manifest.permission.READ_MEDIA_IMAGES)
            }
            if (checkSelfPermission(Manifest.permission.READ_MEDIA_VIDEO) != PackageManager.PERMISSION_GRANTED) {
                perms.add(Manifest.permission.READ_MEDIA_VIDEO)
            }
            if (checkSelfPermission(Manifest.permission.POST_NOTIFICATIONS) != PackageManager.PERMISSION_GRANTED) {
                perms.add(Manifest.permission.POST_NOTIFICATIONS)
            }
        } else {
            if (checkSelfPermission(Manifest.permission.READ_EXTERNAL_STORAGE) != PackageManager.PERMISSION_GRANTED) {
                perms.add(Manifest.permission.READ_EXTERNAL_STORAGE)
            }
        }

        if (perms.isNotEmpty()) {
            ActivityCompat.requestPermissions(this, perms.toTypedArray(), PERMISSION_REQUEST_CODE)
        } else {
            scanPhotos()
        }
    }

    override fun onRequestPermissionsResult(requestCode: Int, permissions: Array<out String>, grantResults: IntArray) {
        super.onRequestPermissionsResult(requestCode, permissions, grantResults)
        if (requestCode == PERMISSION_REQUEST_CODE) {
            if (grantResults.all { it == PackageManager.PERMISSION_GRANTED }) {
                scanPhotos()
            } else {
                Toast.makeText(this, "需要相册权限才能同步照片", Toast.LENGTH_LONG).show()
            }
        }
    }

    private fun scanPhotos() {
        lifecycleScope.launch {
            val count = withContext(Dispatchers.IO) {
                PhotoScanner(applicationContext).scanAll().size
            }
            binding.tvTotalPhotos.text = count.toString()
            addLog("扫描到 $count 个照片/视频")
        }
    }

    private fun setupSyncCallbacks() {
        SyncService.onSyncProgress = { progress ->
            runOnUiThread {
                localPendingMode = null
                binding.tvTotalPhotos.text = progress.total.toString()
                binding.tvSyncedPhotos.text = progress.synced.toString()
                val uploadedDone = progress.synced + progress.skipped + progress.failed
                binding.tvPcPhotos.text = progress.pcSynced.toString()

                val percent = if (progress.needSync > 0) {
                    (uploadedDone * 100) / progress.needSync
                } else 0

                binding.progressSync.progress = percent

                val modeText = if (progress.syncMode == SYNC_MODE_FULL) "全量" else "增量"
                binding.tvSyncMode.text = "同步状态：$modeText"

                // 第一行：上传进度
                binding.tvSyncProgress.text = "上传进度: $uploadedDone/${progress.needSync} ($percent%)"

                // 第二行：当前文件
                binding.tvSyncSpeed.text = "当前: ${progress.currentFile}"

                // 速度卡
                binding.tvSpeedValue.text = formatSpeedMb(progress.speed)

                // 实时保存同步进度
                saveSyncStats(progress.synced, progress.total)
            }
        }

        SyncService.onSyncLog = { message ->
            runOnUiThread {
                addLog(message)
                if (!SyncService.isSyncing) {
                    localPendingMode = null
                    activeSyncMode = null
                    updateSyncButtons()
                    binding.progressSync.visibility = View.GONE
                    binding.tvSyncMode.text = "同步状态：-"
                    binding.tvSyncProgress.text = "上传已完成"
                    binding.tvSyncSpeed.text = ""
                    binding.tvSpeedValue.text = formatSpeedMb(0.0)
                }
            }
        }
    }

    /**
     * 测试与服务器的连接（不开始同步）
     */
    private fun testConnection() {
        val mode = if (binding.rbUsb.isChecked) ConnectionMode.USB else ConnectionMode.WIFI
        val serverIp = binding.etServerIp.text.toString().trim()
        val serverPort = binding.etServerPort.text.toString().trim().toIntOrNull() ?: 9001

        if (mode == ConnectionMode.WIFI && serverIp.isEmpty()) {
            Toast.makeText(this, "请输入服务器 IP 地址", Toast.LENGTH_SHORT).show()
            return
        }

        val testButton = if (mode == ConnectionMode.USB) binding.btnTestUsbConnection else binding.btnTestConnection
        testButton.isEnabled = false
        testButton.text = "连接中..."
        updateConnectionStatus(connected = false, message = "正在连接...")
        addLog("正在测试连接到 ${if (mode == ConnectionMode.USB) "USB(localhost:$serverPort)" else "$serverIp:$serverPort"} ...")

        lifecycleScope.launch {
            try {
                val result = withContext(Dispatchers.IO) {
                    val client = SyncClient(applicationContext).apply {
                        connectionMode = mode
                        this.serverIp = serverIp
                        this.serverPort = serverPort
                    }
                    client.testConnection()
                }

                if (result.isSuccess) {
                    val status = result.getOrNull().orEmpty()
                    val pcTotal = (status["total_synced"] as? Number)?.toInt() ?: 0
                    isConnected = true
                    // 保存 SyncClient 用于轮询
                    syncClientForPoll = SyncClient(applicationContext).apply {
                        connectionMode = mode
                        this.serverIp = serverIp
                        this.serverPort = serverPort
                    }
                    binding.tvPcPhotos.text = pcTotal.toString()
                    // 开始轮询检查 PC 端同步请求
                    startPolling()
                    updateConnectionStatus(connected = true, message = "已连接到服务器")
                    updateSyncButtons()
                    addLog("连接成功！可以开始同步")
                    addLog("电脑端照片: $pcTotal")
                    Toast.makeText(this@MainActivity, "连接成功", Toast.LENGTH_SHORT).show()
                    // 连接成功后保存设置
                    saveSettings()
                } else {
                    isConnected = false
                    val msg = result.exceptionOrNull()?.message ?: "未知错误"
                    updateConnectionStatus(connected = false, message = "连接失败: $msg")
                    updateSyncButtons()
                    addLog("连接失败: $msg，尝试获取最新服务器地址...")
                    
                    // 连接失败时，尝试从服务器获取最新地址
                    if (mode == ConnectionMode.WIFI) {
                        tryFetchAndUpdateServerInfo(serverIp)
                    }
                    
                    Toast.makeText(this@MainActivity, "连接失败: $msg", Toast.LENGTH_LONG).show()
                }
            } catch (e: Exception) {
                isConnected = false
                updateConnectionStatus(connected = false, message = "连接异常: ${e.message}")
                updateSyncButtons()
                addLog("连接异常: ${e.message}")
                Toast.makeText(this@MainActivity, "连接异常: ${e.message}", Toast.LENGTH_LONG).show()
            }
            testButton.isEnabled = true
            // Button text and color will be updated by updateConnectionStatus()
        }
    }

    /**
     * 尝试从服务器获取最新地址信息
     */
    private fun tryFetchAndUpdateServerInfo(hint: String) {
        lifecycleScope.launch {
            try {
                val updated = withContext(Dispatchers.IO) {
                    fetchServerInfoFromUrl(hint)
                }
                if (updated) {
                    addLog("已获取最新服务器地址，请重新尝试连接")
                    Toast.makeText(this@MainActivity, "已获取最新地址，请重新连接", Toast.LENGTH_SHORT).show()
                }
            } catch (e: Exception) {
                // 获取失败，不做处理
            }
        }
    }

    /**
     * 调用后端 /api/server-info 获取最新的服务器配置
     */
    private fun fetchServerInfoFromUrl(hintUrl: String): Boolean {
        return try {
            val preferredPort = binding.etServerPort.text
                ?.toString()
                ?.trim()
                ?.toIntOrNull()
                ?: (prefs.getString(KEY_SERVER_PORT, "9001")?.toIntOrNull() ?: 9001)

            // 尝试从旧地址获取新地址
            val urls = buildList {
                add("http://$hintUrl/api/server-info")
                add("http://127.0.0.1:$preferredPort/api/server-info")
                add("http://127.0.0.1:9001/api/server-info")
                add("http://127.0.0.1:8920/api/server-info")
            }.distinct()
            
            var success = false
            for (url in urls) {
                try {
                    val request = Request.Builder().url(url).get().build()
                    val client = OkHttpClient.Builder()
                        .connectTimeout(5, TimeUnit.SECONDS)
                        .build()
                    val response = client.newCall(request).execute()
                    
                    if (response.isSuccessful) {
                        val jsonStr = response.body?.string() ?: continue
                        val json = Gson().fromJson(jsonStr, Map::class.java)
                        
                        if (json["status"] == "ok") {
                            val serverIp = json["server_ip"] as? String
                            val serverPort = (json["server_port"] as? Number)?.toInt()
                            
                            if (serverIp != null && serverPort != null) {
                                // 分别更新 IP 和端口输入框
                                runOnUiThread {
                                    binding.etServerIp.setText(serverIp)
                                    binding.etServerPort.setText(serverPort.toString())
                                }
                                addLog("已更新服务器地址: $serverIp:$serverPort")
                                saveSettings()
                                success = true
                                break
                            }
                        }
                    }
                } catch (e: Exception) {
                    // 继续尝试下一个 URL
                }
            }
            success
        } catch (e: Exception) {
            false
        }
    }

    /**
     * 开始轮询检查 PC 端同步请求
     */
    private fun startPolling() {
        stopPolling()
        pollRunnable = object : Runnable {
            override fun run() {
                checkPcSyncRequest()
                handler.postDelayed(this, 2000) // 每 2 秒检查一次
            }
        }
        handler.post(pollRunnable!!)
    }

    /**
     * 停止轮询
     */
    private fun stopPolling() {
        pollRunnable?.let { handler.removeCallbacks(it) }
        pollRunnable = null
    }

    /**
     * 检查 PC 端是否有同步请求
     */
    private fun checkPcSyncRequest() {
        val client = syncClientForPoll ?: return

        lifecycleScope.launch {
            try {
                val statusResult = withContext(Dispatchers.IO) {
                    client.getWifiSyncStatus()
                }

                // 只要请求成功，认为网络链路可用，清空失败计数
                consecutivePollFailures = 0

                val connectedNow = statusResult.isSuccess

                if (connectedNow != isConnected) {
                    isConnected = connectedNow
                    runOnUiThread {
                        if (connectedNow) {
                            updateConnectionStatus(connected = true, message = "已连接到服务器")
                        } else {
                            updateConnectionStatus(connected = false, message = "未连接")
                        }
                        updateSyncButtons()
                    }
                }

                if (!connectedNow) {
                    if (!SyncService.isSyncing) {
                        remotePhase = ""
                        remoteRequestedMode = SYNC_MODE_INCREMENTAL
                        remoteSyncMode = SYNC_MODE_INCREMENTAL
                        localPendingMode = null
                        runOnUiThread {
                            updateSyncButtons()
                            binding.progressSync.visibility = View.GONE
                            binding.tvSyncMode.text = "同步状态：-"
                            binding.tvSyncProgress.text = "上传已停止"
                            binding.tvSyncSpeed.text = "当前: -"
                        }
                    }
                    return@launch
                }

                statusResult.getOrNull()?.let { status ->
                    applyRemoteStatusSnapshot(status)
                }

                if (SyncService.isSyncing) {
                    return@launch
                }

                val hasRequest = withContext(Dispatchers.IO) {
                    client.checkSyncRequest()
                }
                if (hasRequest.requestSync) {
                    val requestMode = if (hasRequest.syncMode == SYNC_MODE_FULL) {
                        SYNC_MODE_FULL
                    } else {
                        SYNC_MODE_INCREMENTAL
                    }
                    addLog("收到 PC 端${if (requestMode == SYNC_MODE_FULL) "全量" else "增量"}同步请求，开始同步...")
                    startSync(requestMode)
                }
            } catch (e: Exception) {
                consecutivePollFailures += 1

                // 断网/切网时，OkHttp 可能持续抛异常；这里做最小状态回落，避免 UI 长期卡在“已连接”。
                if (consecutivePollFailures >= 2 && isConnected) {
                    isConnected = false
                    remotePhase = ""
                    remoteRequestedMode = SYNC_MODE_INCREMENTAL
                    remoteSyncMode = SYNC_MODE_INCREMENTAL
                    localPendingMode = null
                    runOnUiThread {
                        updateConnectionStatus(connected = false, message = "未连接")
                        updateSyncButtons()
                        binding.progressSync.visibility = View.GONE
                        binding.tvSyncMode.text = "同步状态：-"
                        binding.tvSyncProgress.text = "上传已停止"
                        binding.tvSyncSpeed.text = "当前: -"
                    }
                }
            }
        }
    }

    private fun startSync(syncMode: String) {
        val mode = if (binding.rbUsb.isChecked) ConnectionMode.USB else ConnectionMode.WIFI
        val serverIp = binding.etServerIp.text.toString().trim()
        val wifiPort = binding.etServerPort.text.toString().trim().toIntOrNull() ?: 9001
        val serverPort = wifiPort

        val normalizedMode = if (syncMode == SYNC_MODE_FULL) SYNC_MODE_FULL else SYNC_MODE_INCREMENTAL
        addLog("开始${if (normalizedMode == SYNC_MODE_FULL) "全量" else "增量"}同步...")
        saveSettings()
        localPendingMode = normalizedMode
        remotePhase = "requested"
        remoteRequestedMode = normalizedMode
        remoteSyncMode = normalizedMode

        val intent = Intent(this@MainActivity, SyncService::class.java).apply {
            action = SyncService.ACTION_START
            putExtra(SyncService.EXTRA_SERVER_IP, serverIp)
            putExtra(SyncService.EXTRA_SERVER_PORT, serverPort)
            putExtra(SyncService.EXTRA_CONNECTION_MODE, mode.name)
            putExtra(SyncService.EXTRA_SYNC_MODE, normalizedMode)
        }

        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O) {
            startForegroundService(intent)
        } else {
            startService(intent)
        }

        activeSyncMode = normalizedMode
        updateSyncButtons()
        binding.progressSync.visibility = View.VISIBLE
        binding.progressSync.progress = 0
        binding.tvSyncMode.text = "同步状态：${if (normalizedMode == SYNC_MODE_FULL) "全量" else "增量"}"
        binding.tvSyncProgress.text = "上传进度: 0/0 (0%)"
        binding.tvSyncSpeed.text = "当前: 正在准备..."
    }

    private fun stopSync() {
        localPendingMode = null
        remotePhase = ""
        remoteRequestedMode = SYNC_MODE_INCREMENTAL
        remoteSyncMode = SYNC_MODE_INCREMENTAL
        val intent = Intent(this, SyncService::class.java).apply {
            action = SyncService.ACTION_STOP
        }
        startService(intent)

        val client = syncClientForPoll
        if (client != null) {
            lifecycleScope.launch {
                withContext(Dispatchers.IO) {
                    client.requestStopSync()
                }
            }
        }

        activeSyncMode = null
        updateSyncButtons()
        binding.progressSync.visibility = View.GONE
        binding.tvSyncMode.text = "同步状态：-"
        binding.tvSyncProgress.text = "上传已停止"
        binding.tvSyncSpeed.text = "当前: -"
        addLog("用户停止同步")
    }

    private fun updateConnectionStatus(connected: Boolean, message: String) {
        binding.tvConnectionStatus.text = message
        val dotDrawable = if (connected) R.drawable.dot_green else R.drawable.dot_gray
        binding.viewStatusDot.setBackgroundResource(dotDrawable)
        
        // Update test connection buttons appearance
        val (btnText, btnColor) = if (connected) {
            "已成功连接" to 0xFF22c55e.toInt() // green
        } else {
            "未连接" to 0xFFef4444.toInt() // red
        }
        
        binding.btnTestConnection.text = btnText
        binding.btnTestConnection.setBackgroundColor(btnColor)
        binding.btnTestUsbConnection.text = btnText
        binding.btnTestUsbConnection.setBackgroundColor(btnColor)
    }

    private fun addLog(message: String) {
        val timestamp = java.text.SimpleDateFormat("HH:mm:ss", java.util.Locale.getDefault())
            .format(java.util.Date())
        logLines.add("[$timestamp] $message")
        if (logLines.size > 200) {
            logLines.removeAt(0)
        }
        binding.tvLog.text = logLines.joinToString("\n")
        binding.tvLog.parent?.let {
            if (it is android.widget.ScrollView) {
                it.post { it.fullScroll(View.FOCUS_DOWN) }
            }
        }
    }

    override fun onDestroy() {
        super.onDestroy()
        stopPolling()
        syncClientForPoll?.shutdown()
        syncClientForPoll = null
        SyncService.onSyncProgress = null
        SyncService.onSyncLog = null
    }

    override fun onResume() {
        super.onResume()
        initPollingFromCurrentSettings()
        activeSyncMode = SyncService.currentSyncMode
        if (SyncService.isSyncing) {
            binding.tvSyncMode.text = "同步状态：${if (activeSyncMode == SYNC_MODE_FULL) "全量" else "增量"}"
        }
        updateSyncButtons()
    }

    override fun onPause() {
        super.onPause()
        // 增强“记忆性”：用户编辑了 IP/端口不一定点测试连接，也要能保存
        saveSettings()
    }
}
