package io.github.zimin3d.filament

import android.Manifest
import android.annotation.SuppressLint
import android.content.ActivityNotFoundException
import android.content.ContentValues
import android.content.Intent
import android.content.pm.PackageManager
import android.net.Uri
import android.os.Bundle
import android.provider.MediaStore
import android.webkit.CookieManager
import android.webkit.PermissionRequest
import android.webkit.URLUtil
import android.webkit.ValueCallback
import android.webkit.WebChromeClient
import android.webkit.WebResourceRequest
import android.webkit.WebView
import android.webkit.WebViewClient
import android.widget.Toast
import androidx.activity.ComponentActivity
import androidx.activity.OnBackPressedCallback
import androidx.activity.result.contract.ActivityResultContracts
import androidx.core.content.FileProvider
import com.chaquo.python.Python
import com.chaquo.python.android.AndroidPlatform
import java.io.File
import java.net.HttpURLConnection
import java.net.URL
import kotlin.concurrent.thread

/** Окно приложения: внутри работает тот же Flask, что и на сервере (launcher.py), а здесь — WebView к нему. */
class MainActivity : ComponentActivity() {
    private lateinit var web: WebView
    private var fileCallback: ValueCallback<Array<Uri>>? = null
    private var cameraUri: Uri? = null
    private var pendingPermission: PermissionRequest? = null

    private val pickFiles = registerForActivityResult(ActivityResultContracts.StartActivityForResult()) { res ->
        var uris = WebChromeClient.FileChooserParams.parseResult(res.resultCode, res.data)
        // снимок с камеры приходит без data — файл уже записан по cameraUri
        if (uris == null && res.resultCode == RESULT_OK && cameraUri != null) uris = arrayOf(cameraUri!!)
        fileCallback?.onReceiveValue(uris)
        fileCallback = null
        cameraUri = null
    }

    private val askCamera = registerForActivityResult(ActivityResultContracts.RequestPermission()) { ok ->
        pendingPermission?.let { if (ok) it.grant(arrayOf(PermissionRequest.RESOURCE_VIDEO_CAPTURE)) else it.deny() }
        pendingPermission = null
    }

    @SuppressLint("SetJavaScriptEnabled")
    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        web = WebView(this)
        setContentView(web)
        web.settings.javaScriptEnabled = true
        web.settings.domStorageEnabled = true
        web.settings.allowFileAccess = false
        web.webViewClient = object : WebViewClient() {
            override fun shouldOverrideUrlLoading(view: WebView, request: WebResourceRequest): Boolean {
                if (request.url.host == "127.0.0.1") {
                    if (request.url.path?.endsWith("/app-update/install") == true) {
                        request.url.getQueryParameter("url")?.let { openUpdate(it) }
                        return true
                    }
                    return false
                }
                // ссылки на магазины и т.п. — во внешнем браузере
                try { startActivity(Intent(Intent.ACTION_VIEW, request.url)) } catch (_: ActivityNotFoundException) {}
                return true
            }
        }
        web.webChromeClient = object : WebChromeClient() {
            override fun onShowFileChooser(view: WebView, callback: ValueCallback<Array<Uri>>,
                                           params: FileChooserParams): Boolean {
                fileCallback?.onReceiveValue(null)
                fileCallback = callback
                return try {
                    pickFiles.launch(chooser(params))
                    true
                } catch (_: ActivityNotFoundException) {
                    fileCallback = null
                    false
                }
            }

            override fun onPermissionRequest(request: PermissionRequest) {
                // камера — для сканера QR-этикеток
                if (PermissionRequest.RESOURCE_VIDEO_CAPTURE !in request.resources) {
                    request.deny()
                } else if (checkSelfPermission(Manifest.permission.CAMERA) == PackageManager.PERMISSION_GRANTED) {
                    request.grant(arrayOf(PermissionRequest.RESOURCE_VIDEO_CAPTURE))
                } else {
                    pendingPermission = request
                    askCamera.launch(Manifest.permission.CAMERA)
                }
            }
        }
        web.setDownloadListener { url, _, disposition, mime, _ -> download(url, disposition, mime) }
        onBackPressedDispatcher.addCallback(this, object : OnBackPressedCallback(true) {
            override fun handleOnBackPressed() {
                if (web.canGoBack()) web.goBack() else finish()
            }
        })

        web.loadData("<p style='font:18px sans-serif;padding:2em;color:#666'>Запуск…</p>", "text/html; charset=utf-8", null)
        thread {
            if (!Python.isStarted()) Python.start(AndroidPlatform(this))
            val version = packageManager.getPackageInfo(packageName, 0).versionName ?: ""
            val url = Python.getInstance().getModule("launcher")
                .callAttr("start", File(filesDir, "data").path, version).toString()
            runOnUiThread { web.loadUrl(url) }
        }
    }

    /** Выбор файла; для фото — ещё и камера. */
    private fun chooser(params: WebChromeClient.FileChooserParams): Intent {
        val pick = params.createIntent()
        val wantsImage = params.acceptTypes.any { it.startsWith("image") }
        if (!wantsImage || packageManager.hasSystemFeature(PackageManager.FEATURE_CAMERA_ANY).not()) return pick
        val dir = File(cacheDir, "camera").apply { mkdirs() }
        val photo = File.createTempFile("photo", ".jpg", dir)
        cameraUri = FileProvider.getUriForFile(this, "$packageName.files", photo)
        val camera = Intent(MediaStore.ACTION_IMAGE_CAPTURE)
            .putExtra(MediaStore.EXTRA_OUTPUT, cameraUri)
            .addFlags(Intent.FLAG_GRANT_WRITE_URI_PERMISSION)
        return Intent.createChooser(pick, "Фото").putExtra(Intent.EXTRA_INITIAL_INTENTS, arrayOf(camera))
    }

    /** Обновление: APK из выпуска на GitHub скачивает и ставит браузер. Сами APK не ставим:
     *  право устанавливать приложения + скачивание APK Play Защита считает признаком вредоноса. */
    private fun openUpdate(url: String) {
        if (!url.startsWith("https://github.com/zimin3d/filament/releases/download/") || !url.endsWith(".apk")) return
        try {
            startActivity(Intent(Intent.ACTION_VIEW, Uri.parse(url)))
            Toast.makeText(this, "Скачивание в браузере — затем откройте файл", Toast.LENGTH_LONG).show()
        } catch (_: ActivityNotFoundException) {
            Toast.makeText(this, "Нет браузера для скачивания", Toast.LENGTH_LONG).show()
        }
    }

    /** CSV, этикетки и т.п.: скачиваем со своего сервера (с cookie входа) в «Загрузки». */
    private fun download(url: String, disposition: String?, mime: String?) {
        // по хосту, не по началу строки: «http://127.0.0.1.evil.com» начинается так же
        if (Uri.parse(url).let { it.scheme != "http" || it.host != "127.0.0.1" }) return
        val name = URLUtil.guessFileName(url, disposition, mime)
        val cookie = CookieManager.getInstance().getCookie(url)
        thread {
            val ok = try {
                val conn = URL(url).openConnection() as HttpURLConnection
                if (cookie != null) conn.setRequestProperty("Cookie", cookie)
                val values = ContentValues().apply {
                    put(MediaStore.Downloads.DISPLAY_NAME, name)
                    put(MediaStore.Downloads.MIME_TYPE, mime ?: conn.contentType ?: "application/octet-stream")
                }
                val target = contentResolver.insert(MediaStore.Downloads.EXTERNAL_CONTENT_URI, values)!!
                conn.inputStream.use { input -> contentResolver.openOutputStream(target)!!.use { input.copyTo(it) } }
                true
            } catch (e: Exception) {
                false
            }
            runOnUiThread {
                Toast.makeText(this, if (ok) "Сохранено в «Загрузки»: $name" else "Не удалось скачать", Toast.LENGTH_LONG).show()
            }
        }
    }
}
