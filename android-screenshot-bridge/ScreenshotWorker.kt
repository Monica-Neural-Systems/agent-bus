package com.crumly.screenshotbridge

import android.content.Context
import android.content.Intent
import android.net.Uri
import androidx.documentfile.provider.DocumentFile
import androidx.work.*
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.withContext
import okhttp3.*
import okhttp3.MediaType.Companion.toMediaType
import okhttp3.RequestBody.Companion.asRequestBody
import java.io.File
import java.util.concurrent.TimeUnit

/**
 * Folder-scope enforced by the OS, not by code filters.
 *
 * The user grants access via Storage Access Framework (ACTION_OPEN_DOCUMENT_TREE)
 * by navigating to and selecting their Screenshots folder. The system issues a
 * persisted URI permission scoped to that tree ONLY (takePersistableUriPermission).
 * The app cannot see, list, or open anything outside the granted tree — this is
 * enforced by Android's permission system, not by a bucket-name filter.
 *
 * Works from background WorkManager: persisted URI grants survive reboots and
 * are usable from background workers on the target Pixel (Android 14).
 */
class ScreenshotWorker(appContext: Context, params: WorkerParameters) : CoroutineWorker(appContext, params) {

    companion object {
        const val PREFS = "bridge_prefs"
        const val KEY_TREE_URI = "tree_uri"          // persisted SAF tree grant
        const val KEY_LAST_SEEN = "last_seen_name"  // dedup watermark (filename)
        const val KEY_WORKER_URL = "worker_url"
        const val KEY_SECRET = "shared_secret"

        /** Called once from MainActivity after the user picks the folder. */
        fun persistGrant(context: Context, treeUri: Uri) {
            context.contentResolver.takePersistableUriPermission(
                treeUri,
                Intent.FLAG_GRANT_READ_URI_PERMISSION
            )
            context.getSharedPreferences(PREFS, Context.MODE_PRIVATE)
                .edit().putString(KEY_TREE_URI, treeUri.toString()).apply()
        }

        fun schedule(context: Context) {
            val req = PeriodicWorkRequestBuilder<ScreenshotWorker>(15, TimeUnit.MINUTES)
                .setBackoffCriteria(BackoffPolicy.EXPONENTIAL, 5, TimeUnit.MINUTES)
                .build()
            WorkManager.getInstance(context)
                .enqueueUniquePeriodicWork("screenshot-bridge", ExistingPeriodicWorkPolicy.KEEP, req)
        }
    }

    override suspend fun doWork(): Result = withContext(Dispatchers.IO) {
        val prefs = applicationContext.getSharedPreferences(PREFS, Context.MODE_PRIVATE)
        val treeUriStr = prefs.getString(KEY_TREE_URI, null)
        val workerUrl = prefs.getString(KEY_WORKER_URL, null)
        val secret = prefs.getString(KEY_SECRET, null)
        if (treeUriStr.isNullOrBlank() || workerUrl.isNullOrBlank() || secret.isNullOrBlank()) {
            return@withContext Result.failure() // user has not completed setup
        }

        val treeUri = Uri.parse(treeUriStr)
        // If the user revoked the grant in system settings, this resolves null/empty:
        // observable failure, no silent broad access.
        val root = DocumentFile.fromTreeUri(applicationContext, treeUri)
            ?: return@withContext Result.failure()

        val lastSeen = prefs.getString(KEY_LAST_SEEN, "") ?: ""
        var maxSeen = lastSeen

        // List ONLY within the granted tree. OS-enforced boundary.
        val files = root.listFiles()
            .filter { it.isFile && !it.name.isNullOrBlank() }
            .sortedBy { it.name }
        for (doc in files) {
            val name = doc.name!!
            if (name <= lastSeen) continue // dedup watermark
            if (uploadOne(doc, workerUrl, secret)) {
                if (name > maxSeen) maxSeen = name
            }
        }
        if (maxSeen > lastSeen) prefs.edit().putString(KEY_LAST_SEEN, maxSeen).apply()
        Result.success()
    }

    /** Read-only stream of the original; temp copy uploaded then deleted. */
    private fun uploadOne(doc: DocumentFile, workerUrl: String, secret: String): Boolean {
        return try {
            val tmp = File.createTempFile("shot-", ".jpg", applicationContext.cacheDir)
            applicationContext.contentResolver.openInputStream(doc.uri)?.use { input ->
                tmp.outputStream().use { output -> input.copyTo(output) }
            } ?: return false
            val body = MultipartBody.Builder().setType(MultipartBody.FORM)
                .addFormDataPart("file", doc.name ?: "shot.jpg", tmp.asRequestBody("image/jpeg".toMediaType()))
                .build()
            val req = Request.Builder()
                .url(workerUrl.trimEnd('/') + "/ingest")
                .addHeader("Authorization", "Bearer $secret")
                .post(body)
                .build()
            val ok = OkHttpClient().newCall(req).execute().use { it.isSuccessful }
            tmp.delete()
            ok
        } catch (_: Exception) {
            false
        }
    }
}
