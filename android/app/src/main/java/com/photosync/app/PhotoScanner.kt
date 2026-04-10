package com.photosync.app

import android.content.ContentUris
import android.content.Context
import android.net.Uri
import android.provider.MediaStore
import java.io.InputStream
import java.security.MessageDigest

/**
 * 扫描手机相册中的照片和视频
 */
data class PhotoInfo(
    val id: Long,
    val uri: Uri,
    val displayName: String,
    val dateTaken: Long,      // 拍摄时间戳(毫秒)
    val size: Long,
    val mimeType: String,
    val bucketName: String,   // 相册名称
    var md5Hash: String = ""  // 文件 MD5
)

class PhotoScanner(private val context: Context) {

    private data class ProgressState(
        val total: Int,
        var scanned: Int = 0,
        var lastReported: Int = 0,
    )

    private fun maybeReportProgress(state: ProgressState, onProgress: ((Int, Int) -> Unit)?) {
        if (onProgress == null) return
        val scanned = state.scanned
        val total = state.total
        if (total <= 0) return
        if (scanned <= 1 || scanned == total || scanned - state.lastReported >= 500) {
            state.lastReported = scanned
            onProgress(scanned, total)
        }
    }

    /**
     * 扫描所有照片和视频
     */
    fun scanAll(onProgress: ((scanned: Int, total: Int) -> Unit)? = null): List<PhotoInfo> {
        val photos = mutableListOf<PhotoInfo>()

        val imageCount = queryCount(MediaStore.Images.Media.EXTERNAL_CONTENT_URI)
        val videoCount = queryCount(MediaStore.Video.Media.EXTERNAL_CONTENT_URI)
        val total = imageCount + videoCount
        val state = ProgressState(total = total)

        photos.addAll(
            scanMedia(
                MediaStore.Images.Media.EXTERNAL_CONTENT_URI,
                isImage = true,
                progressState = state,
                onProgress = onProgress,
            )
        )
        photos.addAll(
            scanMedia(
                MediaStore.Video.Media.EXTERNAL_CONTENT_URI,
                isImage = false,
                progressState = state,
                onProgress = onProgress,
            )
        )
        return photos.sortedByDescending { it.dateTaken }
    }

    /**
     * 增量扫描：仅返回给定时间戳之后新增/更新的媒体
     */
    fun scanSince(
        sinceMillis: Long,
        onProgress: ((scanned: Int, total: Int) -> Unit)? = null,
    ): List<PhotoInfo> {
        if (sinceMillis <= 0L) {
            return scanAll(onProgress)
        }

        // 兼容不同设备字段行为：同时使用 DATE_TAKEN(毫秒) 和 DATE_ADDED(秒)
        val sinceSec = sinceMillis / 1000L
        val selection = "(${MediaStore.MediaColumns.DATE_TAKEN} >= ?) OR (${MediaStore.MediaColumns.DATE_ADDED} >= ?)"
        val selectionArgs = arrayOf(sinceMillis.toString(), sinceSec.toString())

        val imageCount = queryCount(MediaStore.Images.Media.EXTERNAL_CONTENT_URI, selection, selectionArgs)
        val videoCount = queryCount(MediaStore.Video.Media.EXTERNAL_CONTENT_URI, selection, selectionArgs)
        val total = imageCount + videoCount
        val state = ProgressState(total = total)

        val photos = mutableListOf<PhotoInfo>()
        photos.addAll(
            scanMedia(
                MediaStore.Images.Media.EXTERNAL_CONTENT_URI,
                isImage = true,
                sinceMillis = sinceMillis,
                progressState = state,
                onProgress = onProgress,
            )
        )
        photos.addAll(
            scanMedia(
                MediaStore.Video.Media.EXTERNAL_CONTENT_URI,
                isImage = false,
                sinceMillis = sinceMillis,
                progressState = state,
                onProgress = onProgress,
            )
        )
        return photos.sortedByDescending { it.dateTaken }
    }

    private fun queryCount(
        contentUri: Uri,
        selection: String? = null,
        selectionArgs: Array<String>? = null,
    ): Int {
        return try {
            context.contentResolver.query(
                contentUri,
                arrayOf(MediaStore.MediaColumns._ID),
                selection,
                selectionArgs,
                null,
            )?.use { cursor ->
                cursor.count
            } ?: 0
        } catch (_: Exception) {
            0
        }
    }

    private fun scanMedia(
        contentUri: Uri,
        isImage: Boolean,
        sinceMillis: Long? = null,
        progressState: ProgressState? = null,
        onProgress: ((scanned: Int, total: Int) -> Unit)? = null,
    ): List<PhotoInfo> {
        val photos = mutableListOf<PhotoInfo>()
        val projection = arrayOf(
            MediaStore.MediaColumns._ID,
            MediaStore.MediaColumns.DISPLAY_NAME,
            MediaStore.MediaColumns.DATE_TAKEN,
            MediaStore.MediaColumns.DATE_ADDED,
            MediaStore.MediaColumns.SIZE,
            MediaStore.MediaColumns.MIME_TYPE,
            MediaStore.MediaColumns.BUCKET_DISPLAY_NAME,
        )

        val selection: String?
        val selectionArgs: Array<String>?
        if (sinceMillis != null && sinceMillis > 0L) {
            // 兼容不同设备字段行为：同时使用 DATE_TAKEN(毫秒) 和 DATE_ADDED(秒)
            val sinceSec = sinceMillis / 1000L
            selection = "(${MediaStore.MediaColumns.DATE_TAKEN} >= ?) OR (${MediaStore.MediaColumns.DATE_ADDED} >= ?)"
            selectionArgs = arrayOf(sinceMillis.toString(), sinceSec.toString())
        } else {
            selection = null
            selectionArgs = null
        }

        context.contentResolver.query(
            contentUri,
            projection,
            selection,
            selectionArgs,
            "${MediaStore.MediaColumns.DATE_TAKEN} DESC"
        )?.use { cursor ->
            val idCol = cursor.getColumnIndexOrThrow(MediaStore.MediaColumns._ID)
            val nameCol = cursor.getColumnIndexOrThrow(MediaStore.MediaColumns.DISPLAY_NAME)
            val dateCol = cursor.getColumnIndexOrThrow(MediaStore.MediaColumns.DATE_TAKEN)
            val sizeCol = cursor.getColumnIndexOrThrow(MediaStore.MediaColumns.SIZE)
            val mimeCol = cursor.getColumnIndexOrThrow(MediaStore.MediaColumns.MIME_TYPE)
            val bucketCol = cursor.getColumnIndexOrThrow(MediaStore.MediaColumns.BUCKET_DISPLAY_NAME)

            while (cursor.moveToNext()) {
                val id = cursor.getLong(idCol)
                val uri = ContentUris.withAppendedId(contentUri, id)
                photos.add(
                    PhotoInfo(
                        id = id,
                        uri = uri,
                        displayName = cursor.getString(nameCol) ?: "unknown",
                        dateTaken = cursor.getLong(dateCol),
                        size = cursor.getLong(sizeCol),
                        mimeType = cursor.getString(mimeCol) ?: "image/jpeg",
                        bucketName = cursor.getString(bucketCol) ?: ""
                    )
                )

                if (progressState != null) {
                    progressState.scanned += 1
                    maybeReportProgress(progressState, onProgress)
                }
            }
        }
        return photos
    }

    /**
     * 计算文件的 MD5 哈希值
     */
    fun computeMd5(uri: Uri): String {
        return try {
            val md = MessageDigest.getInstance("MD5")
            context.contentResolver.openInputStream(uri)?.use { input ->
                val buffer = ByteArray(8192)
                var bytesRead: Int
                while (input.read(buffer).also { bytesRead = it } != -1) {
                    md.update(buffer, 0, bytesRead)
                }
            }
            md.digest().joinToString("") { "%02x".format(it) }
        } catch (e: Exception) {
            ""
        }
    }

    /**
     * 获取文件输入流
     */
    fun openInputStream(uri: Uri): InputStream? {
        return context.contentResolver.openInputStream(uri)
    }
}
