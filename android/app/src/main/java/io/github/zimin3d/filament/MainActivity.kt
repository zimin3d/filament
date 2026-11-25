package io.github.zimin3d.filament

import android.Manifest
import android.app.AlertDialog
import android.content.ClipData
import android.content.ClipboardManager
import android.nfc.Tag
import android.nfc.NfcAdapter
import android.provider.Settings
import android.annotation.SuppressLint
import android.content.ActivityNotFoundException
import android.content.ContentValues
import android.content.Intent
import android.content.pm.PackageManager
import android.content.res.Configuration
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
import androidx.core.view.WindowCompat
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
    @Volatile private var serverPort: Int = -1
    private var waitingDialog: AlertDialog? = null

    private fun isLocalAppUrl(u: Uri?): Boolean =
        u != null && u.scheme == "http" && u.host == "127.0.0.1" && serverPort > 0 && u.port == serverPort

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
                if (isLocalAppUrl(request.url)) {
                    if (request.url.path?.endsWith("/app-update/install") == true) {
                        request.url.getQueryParameter("url")?.let { openUpdate(it) }
                        return true
                    }
                    // только /spool/<id>/nfc — иначе сюда попадал и сканер /scan/nfc и молча ничего не делал
                    if (Regex("/spool/\\d+/nfc").matches(request.url.path ?: "")) {  // ?tag=… — запись метки Creality
                        request.url.getQueryParameter("tag")?.let { payload ->
                            nfcSession("Запись NFC-метки Creality") { tag ->
                                StringBuilder().also { log -> CfsTag.write(tag, payload, log); log.appendLine("Итог: записано") }.toString()
                            }
                        }
                        return true
                    }
                    if (request.url.path?.endsWith("/scan/nfc") == true) {  // сканер: метка -> страница катушки
                        val origin = "${request.url.scheme}://${request.url.authority}"
                        waitForTag("RFID-метка катушки") { tag ->
                            val (text, error) = try { CfsTag.payload(tag) to null } catch (e: Exception) { "" to (e.message ?: "ошибка чтения") }
                            runOnUiThread {
                                if (error != null) Toast.makeText(this@MainActivity, "Метку не прочитать: $error. Подробности — «Считать NFC-метку»", Toast.LENGTH_LONG).show()
                                else web.loadUrl("$origin/scan/rfid?tag=" + Uri.encode(text))
                            }
                        }
                        return true
                    }
                    if (request.url.path?.endsWith("/nfc-read") == true) {
                        nfcSession("Чтение NFC-метки") { tag -> CfsTag.read(tag) }
                        return true
                    }
                    return false
                }
                // ссылки на магазины и т.п. — во внешнем браузере
                try { startActivity(Intent(Intent.ACTION_VIEW, request.url)) } catch (_: ActivityNotFoundException) {}
                return true
            }

            override fun onPageFinished(view: WebView, url: String?) {
                if (url != null && url.startsWith("http://127.0.0.1")) matchSystemBars()
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
                } catch (_: Exception) {
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
                    pendingPermission?.deny()
                    pendingPermission = request
                    askCamera.launch(Manifest.permission.CAMERA)
                }
            }
        }
        web.setDownloadListener { url, _, disposition, mime, _ -> download(url, disposition, mime) }
        onBackPressedDispatcher.addCallback(this, object : OnBackPressedCallback(true) {
            override fun handleOnBackPressed() {
                // сначала закрыть открытое на странице: окно подтверждения или лист «Ещё»
                web.evaluateJavascript(CLOSE_OVERLAY) { closed ->
                    if (closed != "true") { if (web.canGoBack()) web.goBack() else finish() }
                }
            }
        })

        setSystemBars(0x16, 0x19, 0x1E)  // до первой страницы — как экран «Запуск…» и тёмная тема по умолчанию
        web.loadData("<body style='margin:0;background:#16191E'><p style='font:18px sans-serif;padding:2em;color:#A3AAB5'>Запуск…</p></body>", "text/html; charset=utf-8", null)
        thread {
            try {
                if (!Python.isStarted()) Python.start(AndroidPlatform(this))
                val version = packageManager.getPackageInfo(packageName, 0).versionName ?: ""
                val url = Python.getInstance().getModule("launcher")
                    .callAttr("start", File(filesDir, "data").path, version).toString()
                serverPort = Uri.parse(url).port
                runOnUiThread { if (!isFinishing && !isDestroyed) web.loadUrl(url) }
            } catch (e: Exception) {
                val msg = android.text.TextUtils.htmlEncode(e.message ?: e.javaClass.simpleName)
                runOnUiThread {
                    if (!isFinishing && !isDestroyed) {
                        web.loadData("<body style='margin:0;background:#16191E;color:#FF6B6B;font:16px sans-serif;padding:2em'>Ошибка запуска: $msg</body>",
                            "text/html; charset=utf-8", null)
                    }
                }
            }
        }
    }

    override fun onPause() {
        super.onPause()
        try { NfcAdapter.getDefaultAdapter(this)?.disableReaderMode(this) } catch (_: Exception) {}
        waitingDialog?.dismiss()
        waitingDialog = null
    }

    override fun onConfigurationChanged(newConfig: Configuration) {
        super.onConfigurationChanged(newConfig)
        // тема «как в системе»: стили страницы пересчитаются сами, панели — по ним, чуть позже
        if (web.url?.startsWith("http://127.0.0.1") == true) {
            web.postDelayed({ matchSystemBars() }, 300)
            web.postDelayed({ matchSystemBars() }, 1500)  // медленный телефон не успел пересчитать стили
        }
    }

    companion object {
        /** JS: закрыть окно подтверждения или лист «Ещё»; true — было что закрывать. */
        private const val CLOSE_OVERLAY = "(function(){var d=document.getElementById('confirm-dialog');" +
            "if(d&&d.open){d.close('cancel');return true}" +
            "if(document.body.classList.contains('nav-open')){var t=document.querySelector('.nav-toggle');" +
            "if(t)t.click();return true}return false})()"
    }

    /** Статус-бар и панель навигации — цветом шапки страницы (тема решается в CSS, здесь не дублируется). */
    private fun matchSystemBars() {
        web.evaluateJavascript(
            "getComputedStyle(document.querySelector('.top') || document.body).backgroundColor"
        ) { result ->
            // "rgb(22, 25, 30)" или "rgba(0, 0, 0, 0)" — у прозрачного фона (страница ошибки) цвет не берём
            val m = Regex("rgba?\\((\\d+),\\s*(\\d+),\\s*(\\d+)(?:,\\s*([\\d.]+))?").find(result ?: "")
                ?: return@evaluateJavascript
            if ((m.groupValues[4].toDoubleOrNull() ?: 1.0) < 0.5) return@evaluateJavascript
            setSystemBars(m.groupValues[1].toInt(), m.groupValues[2].toInt(), m.groupValues[3].toInt())
        }
    }

    private fun setSystemBars(r: Int, g: Int, b: Int) {
        val color = (0xFF shl 24) or (r shl 16) or (g shl 8) or b
        val light = 0.299 * r + 0.587 * g + 0.114 * b > 150  // светлый фон — тёмные значки
        @Suppress("DEPRECATION")
        window.statusBarColor = color
        @Suppress("DEPRECATION")
        window.navigationBarColor = color
        WindowCompat.getInsetsController(window, web).apply {
            isAppearanceLightStatusBars = light
            isAppearanceLightNavigationBars = light
        }
    }

    /** NFC: проверить, что есть и включён, показать «поднесите метку», отдать метку в onTag (фоновый поток). */
    private fun waitForTag(title: String, onTag: (Tag) -> Unit) {
        val nfc = NfcAdapter.getDefaultAdapter(this)
        if (nfc == null) {
            Toast.makeText(this, "В телефоне нет NFC", Toast.LENGTH_LONG).show(); return
        }
        if (!nfc.isEnabled) {
            Toast.makeText(this, "Включите NFC", Toast.LENGTH_LONG).show()
            try { startActivity(Intent(Settings.ACTION_NFC_SETTINGS)) } catch (_: ActivityNotFoundException) {}
            return
        }
        waitingDialog?.dismiss()
        val waiting = AlertDialog.Builder(this)
            .setTitle(title)
            .setMessage("Поднесите метку к задней крышке телефона и держите, пока не появится результат (до нескольких секунд).")
            .setNegativeButton("Отмена") { _, _ -> nfc.disableReaderMode(this); waitingDialog = null }
            .setOnCancelListener { nfc.disableReaderMode(this); waitingDialog = null }
            .show()
        waitingDialog = waiting
        nfc.enableReaderMode(this, { tag ->
            try { onTag(tag) } finally {
                runOnUiThread {
                    try { nfc.disableReaderMode(this) } catch (_: Exception) {}
                    if (!isFinishing && !isDestroyed && waiting.isShowing) waiting.dismiss()
                    if (waitingDialog === waiting) waitingDialog = null
                }
            }
        }, NfcAdapter.FLAG_READER_NFC_A or NfcAdapter.FLAG_READER_SKIP_NDEF_CHECK, null)
    }

    /** NFC: ждём метку, выполняем action, протокол — в окно (скопировать / поделиться) и в files/nfc.log. */
    private fun nfcSession(title: String, action: (Tag) -> String) = waitForTag(title) { tag ->
        val report = try { action(tag) } catch (e: Exception) { "Ошибка: ${e.message ?: e.javaClass.simpleName}" }
        val stamped = "$title — ${java.text.SimpleDateFormat("yyyy-MM-dd HH:mm:ss", java.util.Locale.US).format(java.util.Date())}\n" +
            "Приложение ${packageManager.getPackageInfo(packageName, 0).versionName}, Android ${android.os.Build.VERSION.RELEASE}, " +
            "${android.os.Build.MANUFACTURER} ${android.os.Build.MODEL}\n$report"
        try {
            val logFile = File(filesDir, "nfc.log")
            logFile.appendText(stamped + "\n\n")
            if (logFile.length() > 65_536) {
                val tail = logFile.readText().takeLast(32_768)
                logFile.writeText(tail)
            }
        } catch (_: Exception) {}
        runOnUiThread { if (!isFinishing && !isDestroyed) showReport(title, stamped) }
    }

    /** Протокол на экран: выделяемый текст, «Скопировать», «Поделиться» (Telegram и т.п.). */
    private fun showReport(title: String, text: String) {
        val view = android.widget.TextView(this).apply {
            this.text = text; setTextIsSelectable(true); setPadding(48, 24, 48, 24); textSize = 12f
            typeface = android.graphics.Typeface.MONOSPACE
        }
        AlertDialog.Builder(this)
            .setTitle(title)
            .setView(android.widget.ScrollView(this).apply { addView(view) })
            .setPositiveButton("Поделиться") { _, _ ->
                startActivity(Intent.createChooser(Intent(Intent.ACTION_SEND).setType("text/plain")
                    .putExtra(Intent.EXTRA_TEXT, text), title))
            }
            .setNeutralButton("Скопировать") { _, _ ->
                (getSystemService(CLIPBOARD_SERVICE) as ClipboardManager).setPrimaryClip(ClipData.newPlainText(title, text))
                Toast.makeText(this, "Скопировано", Toast.LENGTH_SHORT).show()
            }
            .setNegativeButton("Закрыть", null)
            .show()
    }

    /** Выбор файла; для фото — ещё и камера. */
    private fun chooser(params: WebChromeClient.FileChooserParams): Intent {
        val pick = params.createIntent()
        val wantsImage = params.acceptTypes.any { it.startsWith("image") }
        if (!wantsImage || packageManager.hasSystemFeature(PackageManager.FEATURE_CAMERA_ANY).not()) return pick
        val dir = File(cacheDir, "camera").apply { mkdirs() }
        val cutoff = System.currentTimeMillis() - 3_600_000L
        dir.listFiles()?.forEach { if (it.lastModified() < cutoff) it.delete() }
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
        // по схеме, хосту и порту нашего локального сервера
        if (!isLocalAppUrl(Uri.parse(url))) return
        val name = URLUtil.guessFileName(url, disposition, mime)
        val cookie = CookieManager.getInstance().getCookie(url)
        thread {
            var conn: HttpURLConnection? = null
            val ok = try {
                conn = (URL(url).openConnection() as HttpURLConnection).apply {
                    connectTimeout = 10_000
                    readTimeout = 30_000
                    if (cookie != null) setRequestProperty("Cookie", cookie)
                }
                if (conn.responseCode !in 200..299) return@thread runOnUiThread {
                    Toast.makeText(this, "Не удалось скачать (HTTP ${conn.responseCode})", Toast.LENGTH_LONG).show()
                }
                conn.inputStream.use { input ->
                    val values = ContentValues().apply {
                        put(MediaStore.Downloads.DISPLAY_NAME, name)
                        put(MediaStore.Downloads.MIME_TYPE, mime ?: conn.contentType ?: "application/octet-stream")
                    }
                    val target = contentResolver.insert(MediaStore.Downloads.EXTERNAL_CONTENT_URI, values)!!
                    contentResolver.openOutputStream(target)!!.use { input.copyTo(it) }
                }
                true
            } catch (e: Exception) {
                false
            } finally {
                conn?.disconnect()
            }
            runOnUiThread {
                Toast.makeText(this, if (ok) "Сохранено в «Загрузки»: $name" else "Не удалось скачать", Toast.LENGTH_LONG).show()
            }
        }
    }
}
