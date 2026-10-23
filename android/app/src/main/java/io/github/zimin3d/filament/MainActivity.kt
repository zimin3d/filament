package io.github.zimin3d.filament

import android.Manifest
import android.app.AlertDialog
import android.app.PendingIntent
import android.annotation.SuppressLint
import android.content.ActivityNotFoundException
import android.content.ContentValues
import android.content.Intent
import android.content.pm.PackageInstaller
import android.content.pm.PackageManager
import android.net.Uri
import android.os.Bundle
import android.provider.MediaStore
import android.provider.Settings
import android.webkit.CookieManager
import android.webkit.PermissionRequest
import android.webkit.URLUtil
import android.webkit.ValueCallback
import android.webkit.WebChromeClient
import android.webkit.WebResourceRequest
import android.webkit.WebView
import android.webkit.WebViewClient
import android.widget.LinearLayout
import android.widget.ProgressBar
import android.widget.TextView
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
import java.util.concurrent.atomic.AtomicBoolean
import kotlin.concurrent.thread

/** Окно приложения: внутри работает тот же Flask, что и на сервере (launcher.py), а здесь — WebView к нему. */
class MainActivity : ComponentActivity() {
    private lateinit var web: WebView
    private var fileCallback: ValueCallback<Array<Uri>>? = null
    private var cameraUri: Uri? = null
    private var pendingPermission: PermissionRequest? = null
    @Volatile private var updating = false

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
                        request.url.getQueryParameter("url")?.let { installUpdate(it) }
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

    /** Обновление: APK из выпуска на GitHub → системный установщик (тот же ключ подписи — данные сохраняются). */
    private fun installUpdate(url: String) {
        if (!url.startsWith("https://github.com/zimin3d/filament/releases/download/") || !url.endsWith(".apk")) return
        if (!packageManager.canRequestPackageInstalls()) {
            Toast.makeText(this, "Разрешите установку из этого приложения и нажмите кнопку ещё раз", Toast.LENGTH_LONG).show()
            startActivity(Intent(Settings.ACTION_MANAGE_UNKNOWN_APP_SOURCES, Uri.parse("package:$packageName")))
            return
        }
        if (updating) return
        updating = true
        // окно с полосой: сколько скачано из скольких, кнопка «Отмена»
        val bar = ProgressBar(this, null, android.R.attr.progressBarStyleHorizontal).apply { max = 100; isIndeterminate = true }
        val label = TextView(this).apply { text = "Подключение…"; setPadding(0, 16, 0, 0) }
        val box = LinearLayout(this).apply {
            orientation = LinearLayout.VERTICAL
            setPadding(56, 32, 56, 8)
            addView(bar)
            addView(label)
        }
        val cancelled = AtomicBoolean(false)
        val dialog = AlertDialog.Builder(this)
            .setTitle("Скачивание обновления")
            .setView(box)
            .setCancelable(false)
            .setNegativeButton("Отмена") { _, _ -> cancelled.set(true) }
            .show()
        thread {
            val apk = File(File(cacheDir, "updates").apply { mkdirs() }, "filament.apk")
            val error: String? = try {
                val conn = URL(url).openConnection() as HttpURLConnection
                conn.instanceFollowRedirects = true
                conn.connectTimeout = 20000
                conn.readTimeout = 60000
                if (conn.responseCode != 200) throw IllegalStateException("HTTP ${conn.responseCode}")
                val total = conn.contentLengthLong
                var done = 0L
                var shown = -1
                conn.inputStream.use { input ->
                    apk.outputStream().use { out ->
                        val buf = ByteArray(64 * 1024)
                        while (true) {
                            if (cancelled.get()) throw InterruptedException()
                            val n = input.read(buf)
                            if (n < 0) break
                            out.write(buf, 0, n)
                            done += n
                            val pct = if (total > 0) (done * 100 / total).toInt() else -1
                            if (pct != shown) {
                                shown = pct
                                val mb = "%.1f".format(done / 1048576.0) +
                                    (if (total > 0) " из " + "%.1f".format(total / 1048576.0) else "") + " МБ"
                                runOnUiThread {
                                    if (pct >= 0) { bar.isIndeterminate = false; bar.progress = pct }
                                    label.text = if (pct >= 0) "$pct% · $mb" else mb
                                }
                            }
                        }
                    }
                }
                if (total > 0 && done != total) throw IllegalStateException("файл скачан не полностью")
                null
            } catch (e: InterruptedException) {
                "отменено"
            } catch (e: Exception) {
                e.message ?: e.javaClass.simpleName
            }
            runOnUiThread {
                updating = false
                if (dialog.isShowing) dialog.dismiss()
                if (error != null) {
                    apk.delete()
                    if (!cancelled.get()) Toast.makeText(this, "Не удалось скачать обновление: $error", Toast.LENGTH_LONG).show()
                } else {
                    startInstall(apk)
                }
            }
        }
    }

    /** Установка через PackageInstaller: система сама спросит подтверждение, а результат
     *  (в т.ч. текст ошибки — другая подпись, нет места) придёт в InstallResultReceiver. */
    private fun startInstall(apk: File) {
        try {
            val installer = packageManager.packageInstaller
            val params = PackageInstaller.SessionParams(PackageInstaller.SessionParams.MODE_FULL_INSTALL)
            params.setAppPackageName(packageName)
            val id = installer.createSession(params)
            installer.openSession(id).use { session ->
                apk.inputStream().use { input ->
                    session.openWrite("filament.apk", 0, apk.length()).use { out ->
                        input.copyTo(out)
                        session.fsync(out)
                    }
                }
                val result = Intent(this, InstallResultReceiver::class.java)
                val pi = PendingIntent.getBroadcast(this, id, result,
                    PendingIntent.FLAG_UPDATE_CURRENT or PendingIntent.FLAG_MUTABLE)
                session.commit(pi.intentSender)
            }
            Toast.makeText(this, "Устанавливаю обновление…", Toast.LENGTH_SHORT).show()
        } catch (e: Exception) {
            Toast.makeText(this, "Не удалось начать установку: ${e.message}", Toast.LENGTH_LONG).show()
        }
    }

    /** CSV, этикетки и т.п.: скачиваем со своего сервера (с cookie входа) в «Загрузки». */
    private fun download(url: String, disposition: String?, mime: String?) {
        if (!url.startsWith("http://127.0.0.1")) return
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
