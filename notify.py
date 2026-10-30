"""Уведомления в Telegram: катушки на исходе, «пора сушить», итоги синхронизации."""
import hashlib
import json
import urllib.error
import urllib.parse
import urllib.request

API = "https://api.telegram.org/bot%s/%s"


class NotifyError(Exception):
    pass


def call(token, method, **params):
    data = urllib.parse.urlencode(params).encode() if params else None
    try:
        with urllib.request.urlopen(API % (token, method), data=data, timeout=15) as resp:
            body = json.load(resp)
    except urllib.error.HTTPError as e:
        try:
            body = json.load(e)
        except ValueError:
            raise NotifyError("Telegram: HTTP %s" % e.code)
    except (urllib.error.URLError, OSError) as e:
        raise NotifyError("Telegram недоступен: %s" % getattr(e, "reason", e))
    if not body.get("ok"):
        raise NotifyError("Telegram: %s" % body.get("description", "ошибка"))
    return body["result"]


def send(token, chat_id, text):
    return call(token, "sendMessage", chat_id=chat_id, text=text, parse_mode="HTML",
                disable_web_page_preview="true")


def find_chat(token):
    """chat_id последнего, кто написал боту (нужно сначала отправить боту /start)."""
    updates = call(token, "getUpdates")
    for u in reversed(updates):
        msg = u.get("message") or u.get("channel_post") or {}
        if msg.get("chat", {}).get("id"):
            chat = msg["chat"]
            return chat["id"], chat.get("username") or chat.get("title") or chat.get("first_name", "")
    raise NotifyError("Боту ещё никто не писал — откройте бота в Telegram и отправьте /start.")


ALL_GOOD = "Всё в порядке: катушек на исходе нет, сушить нечего, обслуживание принтера не нужно."


def connect(token, bot=None):
    """Чат последнего, кто написал боту /start: проверка и приветствие. -> (chat_id, кто, имя бота).
    bot — имя, если уже известно (без лишнего запроса getMe)."""
    bot = bot or call(token, "getMe").get("username", "")
    chat, who = find_chat(token)
    send(token, chat, "✅ Уведомления учёта филамента подключены.")
    return chat, who, bot


def esc(text):
    return str(text).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def digest(spools, threshold, base_url, maintenance=()):
    """Текст сводки или None, если сообщать нечего."""
    def line(s):
        num = "№%s " % s["number"] if s["number"] else ""
        return '• <a href="%sspool/%d">%s%s %s</a> — %s' % (
            base_url, s["id"], esc(num), esc(s["material"]), esc(s["color_name"] or s["manufacturer"]),
            "%d г" % s["weight_left"])
    low = [s for s in spools if 0 < s["weight_left"] < threshold]
    dry = [s for s in spools if s.get("needs_drying")]
    parts = []
    if low:
        parts.append("<b>На исходе (меньше %d г):</b>\n%s" % (threshold, "\n".join(line(s) for s in low)))
    if dry:
        parts.append("<b>Пора сушить:</b>\n%s" % "\n".join(line(s) for s in dry))
    if maintenance:
        parts.append('<b><a href="%sprinter">Обслуживание принтера</a>:</b>\n%s' % (
            base_url, "\n".join("• " + esc(m["name"]) for m in maintenance)))
    return "\n\n".join(parts) or None


def fingerprint(text):
    """Отпечаток сводки без адресов ссылок: та же сводка с другим адресом сайта — то же самое."""
    import re
    return hashlib.sha1(re.sub(r'href="[^"]*"', "", text or "").encode()).hexdigest()[:16]
