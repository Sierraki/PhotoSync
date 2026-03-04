package com.photosync.app

import android.content.Context
import android.net.ConnectivityManager
import android.net.Network
import android.net.NetworkCapabilities
import android.net.NetworkRequest
import com.google.gson.Gson
import com.google.gson.reflect.TypeToken
import okhttp3.*
import okhttp3.MediaType.Companion.toMediaType
import okhttp3.RequestBody.Companion.toRequestBody
import okio.BufferedSink
import okio.source
import java.io.IOException
import java.io.InputStream
import java.text.SimpleDateFormat
import java.util.*
import java.util.concurrent.TimeUnit

/**
 * 与 PC 端 PhotoSync 服务器通信的客户端
 * 支持局域网 (WiFi) 和 USB (ADB端口转发) 两种模式
 */
enum class ConnectionMode {
    WIFI,  // 局域网模式：连接到指定 IP
    USB    // USB 模式：通过 ADB reverse 连接到 localhost
}

class SyncClient(private val context: Context) {

    private val gson = Gson()

    // 是否尝试绑定 WiFi 网络（VPN 场景下使用）
    var bindToWifi: Boolean = true

    private val client: OkHttpClient by lazy {
        val builder = OkHttpClient.Builder()
            .connectTimeout(10, TimeUnit.SECONDS)
            .writeTimeout(120, TimeUnit.SECONDS)
            .readTimeout(30, TimeUnit.SECONDS)

        // 尝试绑定到 WiFi 网络，绕过 VPN
        if (bindToWifi && connectionMode == ConnectionMode.WIFI) {
            getWifiNetwork()?.let { wifiNetwork ->
                builder.socketFactory(wifiNetwork.socketFactory)
            }
        }

        builder.build()
    }

    var connectionMode: ConnectionMode = ConnectionMode.WIFI
    var serverIp: String = ""
    var serverPort: Int = 8920

    private val baseUrl: String
        get() {
            val ip = serverIp.trim()
            return when (connectionMode) {
                ConnectionMode.WIFI -> {
                    if (ip.contains(":")) "http://$ip"
                    else "http://$ip:$serverPort"
                }
                ConnectionMode.USB -> "http://localhost:$serverPort"
            }
        }

    /**
     * 获取 WiFi 网络对象（用于绕过 VPN）
     */
    private fun getWifiNetwork(): Network? {
        return try {
            val cm = context.getSystemService(Context.CONNECTIVITY_SERVICE) as ConnectivityManager
            cm.allNetworks.firstOrNull { network ->
                val caps = cm.getNetworkCapabilities(network)
                caps != null && caps.hasTransport(NetworkCapabilities.TRANSPORT_WIFI)
            }
        } catch (e: Exception) {
            null
        }
    }

    /**
     * 测试与服务器的连接
     */
    fun testConnection(): Result<Map<String, Any>> {
        return try {
            val request = Request.Builder()
                .url("$baseUrl/api/status")
                .get()
                .build()
            val response = client.newCall(request).execute()
            if (response.isSuccessful) {
                val body = response.body?.string() ?: "{}"
                val type = object : TypeToken<Map<String, Any>>() {}.type
                val data: Map<String, Any> = gson.fromJson(body, type)
                Result.success(data)
            } else {
                Result.failure(IOException("服务器返回 ${response.code}"))
            }
        } catch (e: Exception) {
            Result.failure(e)
        }
    }

    /**
     * 批量检查相册内是否已存在（相册内去重）
     */
    fun checkAlbum(items: List<Map<String, String>>): Result<Map<String, Boolean>> {
        return try {
            val jsonBody = gson.toJson(items)
            val requestBody = jsonBody.toRequestBody("application/json".toMediaType())
            val request = Request.Builder()
                .url("$baseUrl/api/check_album")
                .post(requestBody)
                .build()
            val response = client.newCall(request).execute()
            if (response.isSuccessful) {
                val body = response.body?.string() ?: "{}"
                val type = object : TypeToken<Map<String, Boolean>>() {}.type
                val data: Map<String, Boolean> = gson.fromJson(body, type)
                Result.success(data)
            } else {
                Result.failure(IOException("检查失败: ${response.code}"))
            }
        } catch (e: Exception) {
            Result.failure(e)
        }
    }

    /**
     * 批量检查文件是否已同步（旧接口，按路径）
     */
    fun checkPaths(paths: List<String>): Result<Map<String, Boolean>> {
        return try {
            val jsonBody = gson.toJson(paths)
            val requestBody = jsonBody.toRequestBody("application/json".toMediaType())
            val request = Request.Builder()
                .url("$baseUrl/api/check_paths")
                .post(requestBody)
                .build()
            val response = client.newCall(request).execute()
            if (response.isSuccessful) {
                val body = response.body?.string() ?: "{}"
                val type = object : TypeToken<Map<String, Boolean>>() {}.type
                val data: Map<String, Boolean> = gson.fromJson(body, type)
                Result.success(data)
            } else {
                Result.failure(IOException("检查失败: ${response.code}"))
            }
        } catch (e: Exception) {
            Result.failure(e)
        }
    }

    /**
     * 批量检查文件是否已同步（旧接口，按MD5）
     */
    fun checkFiles(hashes: List<String>): Result<Map<String, Boolean>> {
        return try {
            val jsonBody = gson.toJson(hashes)
            val requestBody = jsonBody.toRequestBody("application/json".toMediaType())
            val request = Request.Builder()
                .url("$baseUrl/api/check")
                .post(requestBody)
                .build()
            val response = client.newCall(request).execute()
            if (response.isSuccessful) {
                val body = response.body?.string() ?: "{}"
                val type = object : TypeToken<Map<String, Boolean>>() {}.type
                val data: Map<String, Boolean> = gson.fromJson(body, type)
                Result.success(data)
            } else {
                Result.failure(IOException("检查失败: ${response.code}"))
            }
        } catch (e: Exception) {
            Result.failure(e)
        }
    }

    /**
     * 上传单张照片到服务器（流式上传，不会 OOM）
     */
    fun uploadPhoto(photo: PhotoInfo, scanner: PhotoScanner): Result<UploadResult> {
        return try {
            val dateFormat = SimpleDateFormat("yyyy-MM-dd'T'HH:mm:ss", Locale.getDefault())
            val takenDate = if (photo.dateTaken > 0) {
                dateFormat.format(Date(photo.dateTaken))
            } else ""

            // 使用流式 RequestBody，避免把整个文件加载到内存
            val streamBody = object : RequestBody() {
                override fun contentType() = photo.mimeType.toMediaType()

                override fun contentLength() = photo.size

                override fun writeTo(sink: BufferedSink) {
                    val inputStream: InputStream = scanner.openInputStream(photo.uri)
                        ?: throw IOException("无法读取文件: ${photo.displayName}")
                    inputStream.use { stream ->
                        sink.writeAll(stream.source())
                    }
                }
            }

            val requestBody = MultipartBody.Builder()
                .setType(MultipartBody.FORM)
                .addFormDataPart("file_hash", photo.md5Hash)
                .addFormDataPart("original_name", photo.displayName)
                .addFormDataPart("taken_date", takenDate)
                .addFormDataPart("album", photo.bucketName)
                .addFormDataPart("file", photo.displayName, streamBody)
                .build()

            val request = Request.Builder()
                .url("$baseUrl/api/upload")
                .post(requestBody)
                .build()

            val response = client.newCall(request).execute()
            if (response.isSuccessful) {
                val body = response.body?.string() ?: "{}"
                val result = gson.fromJson(body, UploadResult::class.java)
                Result.success(result)
            } else {
                Result.failure(IOException("上传失败: ${response.code}"))
            }
        } catch (e: Exception) {
            Result.failure(e)
        }
    }

    /**
     * 通知服务器扫描进度（扫描阶段）
     */
    fun notifyScanProgress(deviceName: String, scanned: Int, total: Int) {
        try {
            val requestBody = MultipartBody.Builder()
                .setType(MultipartBody.FORM)
                .addFormDataPart("device", deviceName)
                .addFormDataPart("phase", "scanning")
                .addFormDataPart("scanned", scanned.toString())
                .addFormDataPart("total", total.toString())
                .build()

            val request = Request.Builder()
                .url("$baseUrl/api/wifi/scan")
                .post(requestBody)
                .build()
            client.newCall(request).execute().close()
        } catch (e: Exception) {
            // 忽略错误
        }
    }

    /**
     * 通知服务器开始 WiFi 同步
     */
    fun notifySyncStart(deviceName: String, phoneTotal: Int, needSync: Int) {
        try {
            val requestBody = MultipartBody.Builder()
                .setType(MultipartBody.FORM)
                .addFormDataPart("device", deviceName)
                .addFormDataPart("phone_total", phoneTotal.toString())
                .addFormDataPart("need_sync", needSync.toString())
                .build()

            val request = Request.Builder()
                .url("$baseUrl/api/wifi/start")
                .post(requestBody)
                .build()
            client.newCall(request).execute().close()
        } catch (e: Exception) {
            // 忽略错误，不影响同步
        }
    }

    /**
     * 更新服务器端的同步进度
     */
    fun notifySyncProgress(current: String, synced: Int, skipped: Int, failed: Int) {
        try {
            val requestBody = MultipartBody.Builder()
                .setType(MultipartBody.FORM)
                .addFormDataPart("current", current)
                .addFormDataPart("synced", synced.toString())
                .addFormDataPart("skipped", skipped.toString())
                .addFormDataPart("failed", failed.toString())
                .build()

            val request = Request.Builder()
                .url("$baseUrl/api/wifi/progress")
                .post(requestBody)
                .build()
            client.newCall(request).execute().close()
        } catch (e: Exception) {
            // 忽略错误
        }
    }

    /**
     * 通知服务器同步结束
     */
    fun notifySyncStop(message: String) {
        try {
            val requestBody = MultipartBody.Builder()
                .setType(MultipartBody.FORM)
                .addFormDataPart("message", message)
                .build()

            val request = Request.Builder()
                .url("$baseUrl/api/wifi/stop")
                .post(requestBody)
                .build()
            client.newCall(request).execute().close()
        } catch (e: Exception) {
            // 忽略错误
        }
    }

    fun shutdown() {
        client.dispatcher.executorService.shutdown()
    }
}

data class UploadResult(
    val status: String,   // "ok" | "skipped" | "error"
    val message: String,
    val path: String = ""
)
