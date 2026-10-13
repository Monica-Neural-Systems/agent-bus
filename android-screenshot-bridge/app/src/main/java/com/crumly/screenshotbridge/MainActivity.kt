package com.crumly.screenshotbridge

import android.app.Activity
import android.content.Context
import android.content.Intent
import android.net.Uri
import android.os.Bundle
import android.widget.Button
import android.widget.EditText
import android.widget.LinearLayout
import android.widget.TextView
import android.widget.Toast
import androidx.activity.result.contract.ActivityResultContracts
import androidx.appcompat.app.AppCompatActivity

/**
 * One-time setup, run by Michael on the Pixel:
 * 1. Tap "Choose Screenshots folder" -> navigate to DCIM/Screenshots (or Pictures/Screenshots)
 *    -> "Use this folder" -> Allow. This is the OS-enforced folder grant.
 * 2. Paste the Cloudflare Worker URL and the shared ingest secret.
 * 3. Tap "Start bridge". Done. WorkManager runs every 15 min from then on.
 */
class MainActivity : AppCompatActivity() {

    private lateinit var statusView: TextView

    private val folderPicker = registerForActivityResult(
        ActivityResultContracts.StartActivityForResult()
    ) { result ->
        if (result.resultCode == Activity.RESULT_OK) {
            val treeUri: Uri? = result.data?.data
            if (treeUri != null) {
                ScreenshotWorker.persistGrant(this, treeUri)
                toast("Folder granted: ${treeUri.lastPathSegment}")
                refreshStatus()
            }
        }
    }

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        val prefs = getSharedPreferences(ScreenshotWorker.PREFS, Context.MODE_PRIVATE)

        val layout = LinearLayout(this).apply {
            orientation = LinearLayout.VERTICAL
            setPadding(48, 48, 48, 48)
        }

        statusView = TextView(this)
        val urlInput = EditText(this).apply {
            hint = "Worker URL (https://....workers.dev)"
            setText(prefs.getString(ScreenshotWorker.KEY_WORKER_URL, ""))
        }
        val secretInput = EditText(this).apply {
            hint = "Ingest shared secret"
            setText(prefs.getString(ScreenshotWorker.KEY_SECRET, ""))
        }
        val pickBtn = Button(this).apply {
            text = "1. Choose Screenshots folder"
            setOnClickListener { openFolderPicker() }
        }
        val startBtn = Button(this).apply {
            text = "2. Save + Start bridge"
            setOnClickListener {
                val url = urlInput.text.toString().trim()
                val secret = secretInput.text.toString().trim()
                if (url.isBlank() || secret.isBlank()) {
                    toast("Paste the worker URL and secret first")
                    return@setOnClickListener
                }
                if (prefs.getString(ScreenshotWorker.KEY_TREE_URI, null).isNullOrBlank()) {
                    toast("Choose the Screenshots folder first")
                    return@setOnClickListener
                }
                prefs.edit()
                    .putString(ScreenshotWorker.KEY_WORKER_URL, url)
                    .putString(ScreenshotWorker.KEY_SECRET, secret)
                    .apply()
                ScreenshotWorker.schedule(this@MainActivity)
                toast("Bridge scheduled — checks every 15 min")
                refreshStatus()
            }
        }

        layout.addView(statusView)
        layout.addView(pickBtn)
        layout.addView(urlInput)
        layout.addView(secretInput)
        layout.addView(startBtn)
        setContentView(layout)
        refreshStatus()
    }

    private fun openFolderPicker() {
        val intent = Intent(Intent.ACTION_OPEN_DOCUMENT_TREE).apply {
            // Read-only is enough; we never write into the folder.
            addFlags(Intent.FLAG_GRANT_READ_URI_PERMISSION or
                     Intent.FLAG_GRANT_PERSISTABLE_URI_PERMISSION)
        }
        folderPicker.launch(intent)
    }

    private fun refreshStatus() {
        val prefs = getSharedPreferences(ScreenshotWorker.PREFS, Context.MODE_PRIVATE)
        val granted = !prefs.getString(ScreenshotWorker.KEY_TREE_URI, null).isNullOrBlank()
        val configured = !prefs.getString(ScreenshotWorker.KEY_WORKER_URL, null).isNullOrBlank()
        statusView.text = "Folder granted: $granted\nWorker configured: $configured"
    }

    private fun toast(msg: String) =
        Toast.makeText(this, msg, Toast.LENGTH_LONG).show()
}
