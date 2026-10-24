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
                                          ("/javascript:alert(1)", "/"), ("/\t/evil.com", "/"),
                                          ("/\n/evil.com", "/")])
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


def test_theme_redirect_tab_bypass(anon):
    # браузер выкидывает табуляцию: «/\t/evil.com» → «//evil.com»
    for nxt in ("/\t/evil.com", "/\r\n/evil.com", "/ /evil.com"):
        assert "evil" not in anon.get("/theme", query_string={"next": nxt}).location


def test_xlsx_huge_cell_ref():
    import io
    import zipfile
    b = io.BytesIO()
    with zipfile.ZipFile(b, "w") as z:
        z.writestr("xl/workbook.xml", '<workbook><sheets><sheet name="1" r:id="rId1"/></sheets></workbook>')
        z.writestr("xl/_rels/workbook.xml.rels",
                   '<Relationships><Relationship Id="rId1" Target="worksheets/sheet1.xml"/></Relationships>')
        z.writestr("xl/worksheets/sheet1.xml", '<worksheet><sheetData><row><c r="XFD1048576" t="inlineStr">'
                   '<is><t>x</t></is></c></row></sheetData></worksheet>')
    with pytest.raises(importer.SheetError):
        importer.xlsx_read(b.getvalue())


def test_sync_api_garbage_and_unsafe_values(anon):
    cfg = filament.load_config()
    cfg["sync_token"] = "tok"
    filament.save_config(cfg)
    h = {"Authorization": "Bearer tok"}
    bad = [{"t": "spools", "u": "a" * 32, "ts": "x", "row": {}}, {"t": "spools", "u": "b" * 32, "row": [1]}]
    r = anon.post("/api/sync", json={"after": 0, "changes": bad}, headers=h)
    assert r.status_code == 200 and r.get_json()["skipped"] == 2
    row = {"manufacturer": "M", "material": "PLA", "color_hex": 'red;background:url(//evil)"',
           "purchase_url": "javascript:alert(1)"}
    r = anon.post("/api/sync", json={"after": 0, "changes": [{"t": "spools", "u": "c" * 32, "ts": 1, "row": row}]},
                  headers=h)
    assert r.get_json()["applied"] == 1
    with filament.app.app_context():
        s = filament.get_db().execute("SELECT color_hex, purchase_url FROM spools WHERE uid = ?", ("c" * 32,)).fetchone()
    assert tuple(s) == ("#888888", "")
