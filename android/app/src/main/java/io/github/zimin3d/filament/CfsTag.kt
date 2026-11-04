package io.github.zimin3d.filament

import android.nfc.Tag
import android.nfc.tech.MifareClassic
import javax.crypto.Cipher
import javax.crypto.spec.SecretKeySpec

/**
 * Метка RFID для CFS Creality (MIFARE Classic 1K). Формат восстановлен сообществом:
 * github.com/DnG-Crafts/K2-RFID (Utils.createKey / cipherData, MainActivity.WriteTag).
 * Сектор 1, блоки 4–6: 48 символов данных, зашифрованных AES; ключ A/B сектора выводится из UID.
 * Биты доступа в трейлере не трогаем — меняем только ключи, как образец: сектор не «запирается».
 */
object CfsTag {
    private val UID_KEY = byteArrayOf(113, 51, 98, 117, 94, 116, 49, 110, 113, 102, 90, 40, 112, 102, 36, 49)
    private val DATA_KEY = byteArrayOf(72, 64, 67, 70, 107, 82, 110, 122, 64, 75, 65, 116, 66, 74, 112, 50)

    private fun aes(key: ByteArray, data: ByteArray): ByteArray =
        Cipher.getInstance("AES/ECB/NoPadding").run { init(Cipher.ENCRYPT_MODE, SecretKeySpec(key, "AES")); doFinal(data) }

    /** Ключ сектора: первые 4 байта UID по кругу до 16, AES, первые 6 байт. */
    fun sectorKey(uid: ByteArray): ByteArray = aes(UID_KEY, ByteArray(16) { uid[it % 4] }).copyOf(6)

    /** Пишет 48 символов (их собирает сервер, logic.creality_tag). Исключение — с понятным текстом. */
    fun write(tag: Tag, payload: String) {
        require(Regex("[0-9A-Z]{48}").matches(payload)) { "Неверные данные метки" }
        val mfc = MifareClassic.get(tag)
            ?: throw IllegalStateException("Это не MIFARE Classic, или телефон не умеет с ней работать (нужна метка MIFARE Classic 1K)")
        val key = sectorKey(tag.id)
        mfc.connect()
        try {
            // новая метка — заводской ключ; уже записанная нами или Creality — ключ из UID
            val fresh = mfc.authenticateSectorWithKeyA(1, MifareClassic.KEY_DEFAULT)
            if (!fresh) {
                mfc.close(); mfc.connect()  // после неудачной попытки метка «засыпает» — переподключиться
                check(mfc.authenticateSectorWithKeyA(1, key)) { "Метка защищена неизвестным ключом — возьмите чистую" }
            }
            val data = aes(DATA_KEY, payload.toByteArray(Charsets.US_ASCII))
            for (i in 0 until 3) mfc.writeBlock(4 + i, data.copyOfRange(i * 16, i * 16 + 16))
            if (fresh) {
                val trailer = mfc.readBlock(7)  // байты 6–9 (биты доступа) остаются как были
                key.copyInto(trailer, 0)
                key.copyInto(trailer, 10)
                mfc.writeBlock(7, trailer)
            }
        } finally {
            try { mfc.close() } catch (_: Exception) {}
        }
    }
}
