package com.photosync.app

import android.Manifest
import android.content.Context
import android.content.Intent
import android.content.SharedPreferences
import android.content.pm.PackageManager
import android.net.Uri
import android.os.Build
import android.os.Bundle
import android.view.View
import android.widget.Toast
import androidx.activity.result.contract.ActivityResultContracts
import androidx.appcompat.app.AppCompatActivity
import androidx.core.app.ActivityCompat
import androidx.lifecycle.lifecycleScope
import com.journeyapps.barcodescanner.ScanContract
import com.journeyapps.barcodescanner.ScanIntentResult
import com.journeyapps.barcodescanner.ScanOptions
import com.photosync.app.databinding.ActivityMainBinding
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.launch
import kotlinx.coroutines.withContext

class MainActivity : AppCompatActivity() {

    private lateinit var binding: ActivityMainBinding
    private lateinit var prefs: SharedPreferences
    private val logLines = mutableListOf<String>()
    private var isConnected = false

    companion object {
        private const val PERMISSION_REQUEST_CODE = 1001
        private const val PREFS_NAME = "photosync_prefs"
        private const val KEY_SERVER_IP = "server_ip"
        private const val KEY_CONNECTION_MODE = "connection_mode"
        private const val KEY_SYNCED_COUNT = "synced_count"
        private const val KEY_TOTAL_PHOTOS = "total_photos"
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
        binding = ActivityMainBinding.inflate(layoutInflater)
        setContentView(binding.root)

        prefs = getSharedPreferences(PREFS_NAME, Context.MODE_PRIVATE)

        setupUI()
        restoreSettings()
        requestPermissions()
        setupSyncCallbacks()
    }

    private fun setupUI() {
        // 连接模式切换
        binding.rgConnectionMode.setOnCheckedChangeListener { _, checkedId ->
            isConnected = false
            updateConnectionStatus(connected = false, message = "未连接")
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
                    binding.etServerIp.setText("localhost")
                    updateConnectionStatus(connected = false, message = "USB 模式：请确保已通过 USB 连接并开启调试")
                }
            }
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

        // 开始同步
        binding.btnSync.setOnClickListener {
            if (!isConnected) {
                Toast.makeText(this, "请先测试连接", Toast.LENGTH_SHORT).show()
                return@setOnClickListener
            }
            startSync()
        }

        // 停止同步
        binding.btnStop.setOnClickListener {
            stopSync()
        }
    }

    /**
     * 从 SharedPreferences 恢复上次的设置
     */
    private fun restoreSettings() {
        val savedIp = prefs.getString(KEY_SERVER_IP, "") ?: ""
        if (savedIp.isNotEmpty()) {
            binding.etServerIp.setText(savedIp)
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
        prefs.edit()
            .putString(KEY_SERVER_IP, binding.etServerIp.text.toString().trim())
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

            val address = if (port > 0) "$host:$port" else host
            binding.etServerIp.setText(address)

            // 切换到 WiFi 模式
            binding.rbWifi.isChecked = true
            binding.layoutWifiConfig.visibility = View.VISIBLE
            binding.tvUsbHint.visibility = View.GONE

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
            binding.tvPendingPhotos.text = count.toString()
            addLog("扫描到 $count 个照片/视频")
        }
    }

    private fun setupSyncCallbacks() {
        SyncService.onSyncProgress = { progress ->
            runOnUiThread {
                binding.tvTotalPhotos.text = progress.total.toString()
                binding.tvSyncedPhotos.text = progress.synced.toString()
                binding.tvPendingPhotos.text = (progress.needSync - progress.synced - progress.failed).toString()

                val percent = if (progress.needSync > 0) {
                    ((progress.synced + progress.failed) * 100) / progress.needSync
                } else 0

                binding.progressSync.progress = percent

                // 显示速度和剩余时间
                val speedStr = if (progress.speed > 0) String.format("%.1f", progress.speed) else "0"
                val etaStr = formatEta(progress.eta)
                binding.tvSyncProgress.text = "正在同步: ${progress.currentFile} ($percent%) | 速度: ${speedStr}个/秒 | 剩余: $etaStr"

                // 实时保存同步进度
                saveSyncStats(progress.synced, progress.total)
            }
        }

        SyncService.onSyncLog = { message ->
            runOnUiThread {
                addLog(message)
                if (!SyncService.isSyncing) {
                    binding.btnSync.visibility = View.VISIBLE
                    binding.btnStop.visibility = View.GONE
                    binding.progressSync.visibility = View.GONE
                    binding.tvSyncProgress.text = "同步已完成"
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

        if (mode == ConnectionMode.WIFI && serverIp.isEmpty()) {
            Toast.makeText(this, "请输入服务器 IP 地址", Toast.LENGTH_SHORT).show()
            return
        }

        val testButton = if (mode == ConnectionMode.USB) binding.btnTestUsbConnection else binding.btnTestConnection
        testButton.isEnabled = false
        testButton.text = "连接中..."
        updateConnectionStatus(connected = false, message = "正在连接...")
        addLog("正在测试连接到 ${if (mode == ConnectionMode.USB) "USB(localhost)" else serverIp}:8920 ...")

        lifecycleScope.launch {
            try {
                val result = withContext(Dispatchers.IO) {
                    val client = SyncClient(applicationContext).apply {
                        connectionMode = mode
                        this.serverIp = serverIp
                    }
                    client.testConnection()
                }

                if (result.isSuccess) {
                    isConnected = true
                    updateConnectionStatus(connected = true, message = "已连接到服务器")
                    addLog("连接成功！可以开始同步")
                    Toast.makeText(this@MainActivity, "连接成功", Toast.LENGTH_SHORT).show()
                    // 连接成功后保存设置
                    saveSettings()
                } else {
                    isConnected = false
                    val msg = result.exceptionOrNull()?.message ?: "未知错误"
                    updateConnectionStatus(connected = false, message = "连接失败: $msg")
                    addLog("连接失败: $msg")
                    Toast.makeText(this@MainActivity, "连接失败: $msg", Toast.LENGTH_LONG).show()
                }
            } catch (e: Exception) {
                isConnected = false
                updateConnectionStatus(connected = false, message = "连接异常: ${e.message}")
                addLog("连接异常: ${e.message}")
                Toast.makeText(this@MainActivity, "连接异常: ${e.message}", Toast.LENGTH_LONG).show()
            }
            testButton.isEnabled = true
            testButton.text = "测试连接"
        }
    }

    private fun startSync() {
        val mode = if (binding.rbUsb.isChecked) ConnectionMode.USB else ConnectionMode.WIFI
        val serverIp = binding.etServerIp.text.toString().trim()

        addLog("开始同步...")
        saveSettings()

        val intent = Intent(this@MainActivity, SyncService::class.java).apply {
            action = SyncService.ACTION_START
            putExtra(SyncService.EXTRA_SERVER_IP, serverIp)
            putExtra(SyncService.EXTRA_CONNECTION_MODE, mode.name)
        }

        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O) {
            startForegroundService(intent)
        } else {
            startService(intent)
        }

        binding.btnSync.visibility = View.GONE
        binding.btnStop.visibility = View.VISIBLE
        binding.progressSync.visibility = View.VISIBLE
        binding.progressSync.progress = 0
        binding.tvSyncProgress.text = "正在准备..."
    }

    private fun stopSync() {
        val intent = Intent(this, SyncService::class.java).apply {
            action = SyncService.ACTION_STOP
        }
        startService(intent)
        binding.btnSync.visibility = View.VISIBLE
        binding.btnStop.visibility = View.GONE
        binding.progressSync.visibility = View.GONE
        binding.tvSyncProgress.text = "同步已停止"
        addLog("用户停止同步")
    }

    private fun updateConnectionStatus(connected: Boolean, message: String) {
        binding.tvConnectionStatus.text = message
        val dotDrawable = if (connected) R.drawable.dot_green else R.drawable.dot_gray
        binding.viewStatusDot.setBackgroundResource(dotDrawable)
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

    private fun formatEta(seconds: Long): String {
        return when {
            seconds <= 0 -> "计算中..."
            seconds < 60 -> "${seconds}秒"
            seconds < 3600 -> "${seconds / 60}分${seconds % 60}秒"
            else -> "${seconds / 3600}时${(seconds % 3600) / 60}分"
        }
    }

    override fun onDestroy() {
        super.onDestroy()
        SyncService.onSyncProgress = null
        SyncService.onSyncLog = null
    }
}
