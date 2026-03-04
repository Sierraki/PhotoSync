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

    /**
     * 扫描所有照片和视频
     */
    fun scanAll(): List<PhotoInfo> {
        val photos = mutableListOf<PhotoInfo>()
        photos.addAll(scanMedia(MediaStore.Images.Media.EXTERNAL_CONTENT_URI, isImage = true))
        photos.addAll(scanMedia(MediaStore.Video.Media.EXTERNAL_CONTENT_URI, isImage = false))
        return photos.sortedByDescending { it.dateTaken }
    }

    private fun scanMedia(contentUri: Uri, isImage: Boolean): List<PhotoInfo> {
        val photos = mutableListOf<PhotoInfo>()
        val projection = arrayOf(
            MediaStore.MediaColumns._ID,
            MediaStore.MediaColumns.DISPLAY_NAME,
            MediaStore.MediaColumns.DATE_TAKEN,
            MediaStore.MediaColumns.SIZE,
            MediaStore.MediaColumns.MIME_TYPE,
            MediaStore.MediaColumns.BUCKET_DISPLAY_NAME,
        )

        context.contentResolver.query(
            contentUri,
            projection,
            null,
            null,
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
