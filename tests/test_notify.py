import app as filament
import notify
from conftest import add_spool


def test_digest_and_dedup(admin, db, monkeypatch, capsys):
    sent = []
    monkeypatch.setattr(notify, "send", lambda token, chat, text: sent.append(text))
    cfg = filament.load_config()
    cfg.update(telegram_token="t", telegram_chat=1)
    filament.save_config(cfg)

    filament.cli_notify()
    assert sent == [] and "нечего" in capsys.readouterr().out

    sid = add_spool(admin, number="5", color_name="красный <b>")
    admin.post("/spool/%d/use" % sid, data={"csrf": "t", "grams": "900"})
    add_spool(admin, material="PA", opened_date="2020-01-01")
    filament.cli_notify()
    assert len(sent) == 1
    assert "На исходе" in sent[0] and "№5" in sent[0] and "&lt;b&gt;" in sent[0] and "Пора сушить" in sent[0]
    assert "https://filament.example/filament/spool/%d" % sid in sent[0]

    filament.cli_notify()  # ничего не изменилось
    assert len(sent) == 1
    filament.cli_notify(force=True)
    assert len(sent) == 2


def test_not_configured(capsys):
    assert filament.telegram("x") is False


def test_telegram_page(admin, anon, monkeypatch):
    """Подключение бота со страницы сайта: токен → чат → пробная сводка; без консоли."""
    import app as filament
    import notify
    calls = []

    def fake_call(token, method, **params):
        calls.append((method, params))
        if method == "getMe":
            return {"username": "filament_bot"}
        if method == "getUpdates":
            return [{"message": {"chat": {"id": 42, "first_name": "Я"}}}]
        return {}
    monkeypatch.setattr(notify, "call", fake_call)
    assert anon.get("/telegram").status_code == 302
    assert "Telegram" in admin.get("/").get_data(as_text=True)
    token = "123456789:" + "A" * 35
    admin.post("/telegram", data={"csrf": "t", "action": "token", "token": "мусор"})
    assert not filament.load_config().get("telegram_token")
    admin.post("/telegram", data={"csrf": "t", "action": "token", "token": token})
    assert filament.load_config()["telegram_bot"] == "filament_bot"
    admin.post("/telegram", data={"csrf": "t", "action": "chat"})
    assert filament.load_config()["telegram_chat"] == 42
    admin.post("/telegram", data={"csrf": "t", "action": "test"})
    assert calls[-1][0] == "sendMessage" and calls[-1][1]["chat_id"] == 42
    html = admin.get("/telegram").get_data(as_text=True)
    assert "@filament_bot" in html and token not in html  # токен на страницу не выводится
    admin.post("/telegram", data={"csrf": "t", "action": "off"})
    assert not filament.load_config().get("telegram_chat")


def test_notify_quiet_without_telegram(capsys):
    import cli
    cli.cli_notify(quiet=True)  # ночная копия: не подключён — ни ошибки, ни вывода
    assert capsys.readouterr().out == ""
