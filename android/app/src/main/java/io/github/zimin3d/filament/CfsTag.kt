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

    private fun aes(key: ByteArray, data: ByteArray, mode: Int = Cipher.ENCRYPT_MODE): ByteArray =
        Cipher.getInstance("AES/ECB/NoPadding").run { init(mode, SecretKeySpec(key, "AES")); doFinal(data) }

    private fun ByteArray.hex() = joinToString("") { "%02X".format(it) }
    private fun ByteArray.text() = String(CharArray(size) { i -> this[i].toInt().let { if (it in 32..126) it.toChar() else '.' } })

    /** Метка «уснула» после неверного ключа, а переподключиться телефон не смог (так бывает, например, на Xiaomi). */
    class Retap(msg: String) : IllegalStateException(msg)

    /** Какой ключ уже не подошёл к сектору метки («UID:сектор» -> имя ключа): со следующего касания он идёт последним. */
    private val badKey = HashMap<String, String>()

    /**
     * Открыть сектор. Каждый неверный ключ «усыпляет» метку, поэтому сначала — вероятный: preferUid для сектора 1
     * (Creality и наши метки), заводской — для остальных и для чистых меток. Не подошёл и не переподключиться — Retap.
     */
    private fun open(mfc: MifareClassic, uid: ByteArray, sector: Int, key: ByteArray, preferUid: Boolean): String? {
        val id = "${uid.hex()}:$sector"
        var keys = listOf("из UID" to key, "заводской" to MifareClassic.KEY_DEFAULT)
        if (!preferUid) keys = keys.reversed()
        badKey[id]?.let { bad -> keys = keys.sortedBy { it.first == bad } }
        for ((i, k) in keys.withIndex()) {
            if (mfc.authenticateSectorWithKeyA(sector, k.second)) return k.first
            badKey[id] = k.first
            if (i < keys.lastIndex) {
                try { mfc.close(); mfc.connect() } catch (e: Exception) {
                    throw Retap("метка не ответила после ключа «${k.first}» (сектор $sector) — поднесите её ещё раз, " +
                        "следующая попытка начнёт с другого ключа")
                }
            }
        }
        return null
    }

    /** Протокол чтения метки целиком — для разбора формата. На метку ничего не пишет. */
    fun read(tag: Tag): String {
        val log = StringBuilder()
        log.appendLine("UID: ${tag.id.hex()}")
        log.appendLine("Технологии: ${tag.techList.joinToString { it.substringAfterLast('.') }}")
        val mfc = MifareClassic.get(tag)
            ?: return log.appendLine("MIFARE Classic: нет (метка другая или телефон её не поддерживает)").toString()
        val key = sectorKey(tag.id)
        log.appendLine("MIFARE Classic: тип ${mfc.type}, ${mfc.size} байт, секторов ${mfc.sectorCount}")
        log.appendLine("Ключ из UID: ${key.hex()}")
        if (tag.id.size != 4) log.appendLine("Внимание: UID ${tag.id.size} байт — CFS берёт только метки с UID 4 байта")
        mfc.connect()
        try {
            for (s in 0 until mfc.sectorCount) {
                val how = try { open(mfc, tag.id, s, key, preferUid = s == 1) } catch (e: Retap) {
                    log.appendLine("Сектор $s: ${e.message}"); break  // соединение потеряно — дальше читать нечем
                } catch (e: Exception) { log.appendLine("Сектор $s: ошибка ${e.javaClass.simpleName}: ${e.message}"); continue }
                log.appendLine("Сектор $s: ${how ?: "нет доступа ни одним ключом"}")
                if (how == null) continue
                val first = mfc.sectorToBlock(s)
                val blocks = (0 until mfc.getBlockCountInSector(s)).map { mfc.readBlock(first + it) }
                blocks.forEachIndexed { i, b -> log.appendLine("  блок ${first + i}: ${b.hex()}  ${b.text()}") }
                val data = blocks[0] + blocks[1] + blocks[2]
                if (s == 1) {
                    val plain = aes(DATA_KEY, data, Cipher.DECRYPT_MODE).text()
                    log.appendLine("  расшифровано: $plain")
                    if (plain.length == 48) log.appendLine("  поля: дата ${plain.substring(0, 5)} · производитель ${plain.substring(5, 9)} · " +
                        "партия ${plain.substring(9, 11)} · материал ${plain.substring(11, 17)} · цвет ${plain.substring(17, 24)} · " +
                        "длина ${plain.substring(24, 28)} · серийный ${plain.substring(28, 34)} · резерв ${plain.substring(34)}")
                }
                if (s == 2) log.appendLine("  текст: ${data.text()}")
            }
        } finally {
            try { mfc.close() } catch (_: Exception) {}
        }
        return log.toString()
    }

    /** Данные метки для сканера: 48 символов сектора 1 (расшифрованных, если сектор закрыт ключом из UID). */
    fun payload(tag: Tag): String {
        val mfc = MifareClassic.get(tag)
            ?: throw IllegalStateException("Это не MIFARE Classic (или телефон такие не читает)")
        mfc.connect()
        try {
            val how = open(mfc, tag.id, 1, sectorKey(tag.id), preferUid = true)
                ?: throw IllegalStateException("Метка закрыта неизвестным ключом")
            val data = (4..6).map { mfc.readBlock(it) }.reduce { a, b -> a + b }
            return (if (how == "из UID") aes(DATA_KEY, data, Cipher.DECRYPT_MODE) else data).text()
        } finally {
            try { mfc.close() } catch (_: Exception) {}
        }
    }

    /** Ключ сектора: первые 4 байта UID по кругу до 16, AES, первые 6 байт. */
    fun sectorKey(uid: ByteArray): ByteArray = aes(UID_KEY, ByteArray(16) { uid[it % 4] }).copyOf(6)

    /** Пишет 48 символов (их собирает сервер, logic.creality_tag); шаги — в log. Исключение — с понятным текстом. */
    fun write(tag: Tag, payload: String, log: StringBuilder) {
        log.appendLine("Запись: $payload")
        log.appendLine("UID: ${tag.id.hex()}")
        require(Regex("[0-9A-Z]{48}").matches(payload)) { "Неверные данные метки" }
        // как K2-RFID: ключ считается из 4 байт UID; метки с 7-байтным UID принтер не принимает
        check(tag.id.size == 4) { "У метки UID ${tag.id.size} байт, а CFS нужна MIFARE Classic 1K с UID 4 байта" }
        val mfc = MifareClassic.get(tag)
            ?: throw IllegalStateException("Это не MIFARE Classic, или телефон не умеет с ней работать (нужна метка MIFARE Classic 1K)")
        val key = sectorKey(tag.id)
        mfc.connect()
        try {
            // новая метка — заводской ключ; уже записанная нами или Creality — ключ из UID
            val how = open(mfc, tag.id, 1, key, preferUid = false)  // чистые метки — заводской ключ
                ?: throw IllegalStateException("Метка защищена неизвестным ключом — возьмите чистую")
            log.appendLine("Сектор 1 открыт ключом: $how (ключ из UID ${key.hex()})")
            val data = aes(DATA_KEY, payload.toByteArray(Charsets.US_ASCII))
            for (i in 0 until 3) mfc.writeBlock(4 + i, data.copyOfRange(i * 16, i * 16 + 16))
            log.appendLine("Блоки 4–6 записаны: ${data.hex()}")
            if (how == "заводской") {
                val trailer = mfc.readBlock(7)  // байты 6–9 (биты доступа) остаются как были
                log.appendLine("Трейлер до: ${trailer.hex()}")
                key.copyInto(trailer, 0)
                key.copyInto(trailer, 10)
                mfc.writeBlock(7, trailer)
                log.appendLine("Трейлер после: ${trailer.hex()}")
            }
        } finally {
            try { mfc.close() } catch (_: Exception) {}
        }
    }
}
