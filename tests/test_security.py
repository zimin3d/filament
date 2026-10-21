import json
import socket

import pytest
from werkzeug.security import generate_password_hash

import app as filament
import importer
from conftest import ADMIN_PW, add_spool

FAST = "pbkdf2:sha256:1000"


@pytest.mark.parametrize("url", ["http://127.0.0.1:7125/server/info", "http://localhost/x", "http://169.254.169.254/latest/",
                                 "http://10.0.0.5/a.csv", "http://[::1]/", "file:///etc/passwd", "ftp://example.com/x"])
def test_ssrf_blocked(url):
    with pytest.raises(importer.SheetError):
        importer.check_public_url(url)


def test_ssrf_redirect_blocked(monkeypatch):
    """Публичный адрес, который перенаправляет во внутреннюю сеть, тоже отвергается."""
    handler = importer._SafeRedirect()
    with pytest.raises(importer.SheetError):
        handler.redirect_request(None, None, 302, "Found", {}, "http://127.0.0.1/secret")


def test_public_url_allowed(monkeypatch):
    monkeypatch.setattr(socket, "getaddrinfo", lambda host, port: [(2, 1, 6, "", ("142.250.74.46", port))])
    importer.check_public_url("https://docs.google.com/spreadsheets/d/x/export")


def test_import_rejects_internal_url(admin):
    r = admin.post("/import", data={"csrf": "t", "sheet_url": "http://127.0.0.1:8080/export.csv"})
    assert "внутренняя сеть" in r.get_data(as_text=True)


def test_color_hex_sanitized(admin, db):
    add_spool(admin, color_hex='red;background:url(//evil)"')
    assert db.execute("SELECT color_hex FROM spools").fetchone()[0] == "#888888"


def test_password_change_kills_sessions(anon):
    anon.get("/login")
    with anon.session_transaction() as s:
        tok = s["csrf"]
    anon.post("/login", data={"csrf": tok, "password": ADMIN_PW})
    assert anon.get("/spool/new").status_code == 200
    filament.set_admin("admin", generate_password_hash("new-password-123", method=FAST))
    assert anon.get("/spool/new").status_code == 302


def test_forged_session_without_password_mark(anon):
    with anon.session_transaction() as s:
        s["admin"] = True  # подделать подпись нельзя, но и старые сессии без метки не принимаются
    assert anon.get("/spool/new").status_code == 302


@pytest.mark.parametrize("nxt,expected", [("/spool/1", "/spool/1"), ("//evil.com", "/"), ("/\\\\evil.com", "/"),
                                          ("/javascript:alert(1)", "/")])
def test_login_redirect_safe(anon, nxt, expected):
    anon.get("/login")
    with anon.session_transaction() as s:
        tok = s["csrf"]
    r = anon.post("/login", query_string={"next": nxt}, data={"csrf": tok, "password": ADMIN_PW})
    assert r.location == expected


def test_printer_api_garbage(anon):
    cfg = filament.load_config()
    cfg["printer_token"] = "secret"
    filament.save_config(cfg)
    h = {"Authorization": "Bearer secret"}
    for weights in [["abc"], "12", [1e9], {"a": 1}]:
        r = anon.post("/api/printer/job", data=json.dumps({"job_id": "1", "weights": weights}),
                      content_type="application/json", headers=h)
        assert r.status_code == 400, weights


def test_zip_bomb_rejected():
    import io
    import zipfile
    from test_app import SHEET, make_xlsx
    z = make_xlsx(SHEET, [3])
    big = io.BytesIO()
    out = zipfile.ZipFile(big, "w", zipfile.ZIP_DEFLATED)
    for info in z.infolist():
        data = z.read(info.filename)
        if info.filename.startswith("xl/media/"):
            data = b"\0" * (importer.PHOTO_MAX_BYTES + 1)  # сжимается в килобайты
        out.writestr(info.filename, data)
    out.close()
    with pytest.raises(ValueError):
        importer.xlsx_photos(zipfile.ZipFile(io.BytesIO(big.getvalue())), SHEET)


def test_csp_no_inline_scripts(admin, anon):
    """CSP без 'unsafe-inline' для скриптов, и ни одна страница не содержит inline-JS."""
    import re
    sid = add_spool(admin)
    r = anon.get("/")
    script_src = re.search(r"script-src ([^;]*)", r.headers["Content-Security-Policy"]).group(1)
    assert "unsafe-inline" not in script_src and "unsafe-eval" not in script_src
    pages = ["/", "/spool/%d" % sid, "/labels?id=%d" % sid, "/cfs", "/stock", "/report", "/history",
             "/login", "/scan", "/stl", "/inbox", "/printer", "/gallery", "/enough", "/fill", "/stats"]
    for client in (admin, anon):
        for p in pages:
            r = client.get(p)
            if r.status_code != 200:
                continue
            html = r.get_data(as_text=True)
            assert not re.search(r"<script(?![^>]*\ssrc=)[^>]*>", html), p
            assert not re.search(r"\son[a-z]+\s*=", html), p
            assert "javascript:" not in html, p
