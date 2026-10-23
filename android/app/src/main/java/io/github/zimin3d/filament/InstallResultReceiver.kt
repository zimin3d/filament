package io.github.zimin3d.filament

import android.content.BroadcastReceiver
import android.content.Context
import android.content.Intent
import android.content.pm.PackageInstaller
import android.os.Build
import android.widget.Toast

/** Итог установки обновления от PackageInstaller: показать подтверждение системы или ошибку. */
class InstallResultReceiver : BroadcastReceiver() {
    override fun onReceive(context: Context, intent: Intent) {
        when (val status = intent.getIntExtra(PackageInstaller.EXTRA_STATUS, PackageInstaller.STATUS_FAILURE)) {
            PackageInstaller.STATUS_PENDING_USER_ACTION -> {
                val confirm = if (Build.VERSION.SDK_INT >= 33)
                    intent.getParcelableExtra(Intent.EXTRA_INTENT, Intent::class.java)
                else @Suppress("DEPRECATION") intent.getParcelableExtra(Intent.EXTRA_INTENT)
                confirm?.addFlags(Intent.FLAG_ACTIVITY_NEW_TASK)?.let { context.startActivity(it) }
            }
            PackageInstaller.STATUS_SUCCESS -> Unit  // приложение перезапустится уже новой версией
            else -> {
                val why = when (status) {
                    PackageInstaller.STATUS_FAILURE_ABORTED -> "отменено"
                    PackageInstaller.STATUS_FAILURE_CONFLICT -> "конфликт с установленной версией (другой ключ подписи?)"
                    PackageInstaller.STATUS_FAILURE_INCOMPATIBLE -> "несовместимо с этим телефоном"
                    PackageInstaller.STATUS_FAILURE_STORAGE -> "мало места"
                    PackageInstaller.STATUS_FAILURE_BLOCKED -> "установка запрещена"
                    PackageInstaller.STATUS_FAILURE_INVALID -> "файл повреждён"
                    else -> "ошибка"
                }
                val msg = intent.getStringExtra(PackageInstaller.EXTRA_STATUS_MESSAGE)
                Toast.makeText(context, "Обновление не установлено: $why" + (msg?.let { " ($it)" } ?: ""),
                    Toast.LENGTH_LONG).show()
            }
        }
    }
}
