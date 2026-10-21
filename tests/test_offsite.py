import datetime
import os
import subprocess
import sys

import app as filament

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "deploy"))
import offsite  # noqa: E402


def test_not_configured(tmp_path):
    archive = tmp_path / "a.tar.gz"
    archive.write_bytes(b"x")
    assert offsite.run(str(archive)) == []


def test_encrypt_roundtrip(tmp_path):
    archive = tmp_path / "filament-1.tar.gz"
    archive.write_bytes(b"secret data" * 100)
    enc = offsite.encrypt(str(archive), "pass-phrase-123", str(tmp_path))
    assert b"secret data" not in open(enc, "rb").read()
    out = subprocess.run(["gpg", "--batch", "--quiet", "--pinentry-mode", "loopback", "--passphrase", "pass-phrase-123",
                          "--homedir", str(tmp_path), "-d", enc], stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    assert out.stdout == b"secret data" * 100


def test_remote_names_rotation():
    assert offsite.remote_names(datetime.date(2026, 10, 5)) == ["filament-daily-mon.tar.gz.gpg"]
    assert offsite.remote_names(datetime.date(2026, 11, 1)) == ["filament-daily-sun.tar.gz.gpg",
                                                                 "filament-monthly-2026-11.tar.gz.gpg"]


def test_run_sends(tmp_path, monkeypatch):
    sent = []
    monkeypatch.setattr(offsite, "webdav_put", lambda url, u, p, name, path: sent.append(("dav", name, open(path, "rb").read()[:1])))
    monkeypatch.setattr(offsite, "telegram_document", lambda t, c, path, cap: sent.append(("tg", os.path.basename(path))))
    cfg = filament.load_config()
    cfg.update(telegram_token="t", telegram_chat=1,
               offsite={"passphrase": "p-123", "webdav_url": "https://dav.example/x", "telegram": True})
    filament.save_config(cfg)
    archive = tmp_path / "filament-20261005.tar.gz"
    archive.write_bytes(b"data")
    done = offsite.run(str(archive), today=datetime.date(2026, 10, 5))  # понедельник — и Telegram
    assert done == ["WebDAV: filament-daily-mon.tar.gz.gpg", "Telegram"]
    assert sent[1] == ("tg", "filament-20261005.tar.gz.gpg")
    sent.clear()
    assert offsite.run(str(archive), today=datetime.date(2026, 10, 6)) == ["WebDAV: filament-daily-tue.tar.gz.gpg"]


def test_webdav_requires_https(tmp_path):
    f = tmp_path / "x"
    f.write_bytes(b"1")
    try:
        offsite.webdav_put("http://dav.example/", "u", "p", "x", str(f))
        assert False
    except offsite.OffsiteError as e:
        assert "https" in str(e)
