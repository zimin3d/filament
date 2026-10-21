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
