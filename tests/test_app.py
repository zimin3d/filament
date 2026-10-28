import io
import re
import zipfile

import pytest

import app as filament
import importer
from conftest import ADMIN_PW, add_spool, flashes, jpeg

PAGES = ["/", "/?status=all&sort=number", "/stock", "/profiles", "/jobs", "/labels?status=all", "/export.csv"]
ADMIN_PAGES = ["/spool/new", "/import", "/profiles/new"]


# --- страницы и доступ ---------------------------------------------------------

def test_pages_render(anon, admin):
    sid = add_spool(admin, number="7")
    for c in (anon, admin):
        for p in PAGES + ["/spool/%d" % sid]:
            assert c.get(p).status_code == 200, p
    for p in ADMIN_PAGES + ["/spool/%d/edit" % sid]:
        assert admin.get(p).status_code == 200, p
        assert anon.get(p).status_code == 302, p


def test_admin_actions_require_login(anon, admin):
    sid = add_spool(admin)
    anon.get("/")
    with anon.session_transaction() as s:
        s["csrf"] = "t"
    for p in ["/spool/%d/weigh" % sid, "/spool/%d/use" % sid, "/spool/%d/delete" % sid,
              "/spool/%d/prints" % sid, "/spool/%d/dried" % sid]:
        r = anon.post(p, data={"csrf": "t"})
        assert r.status_code == 302 and "/login" in r.location, p


def test_csrf_required(admin):
    r = admin.post("/spool/new", data={"manufacturer": "X", "material": "PLA"})
    assert r.status_code == 400


def test_login_and_redirect_keeps_prefix(anon):
    headers = {"X-Forwarded-Prefix": "/filament", "X-Forwarded-Proto": "https",
               "X-Forwarded-Host": "filament.example", "X-Forwarded-For": "1.2.3.4"}
    anon.get("/login", headers=headers)
    with anon.session_transaction() as s:
        tok = s["csrf"]
    r = anon.post("/login?next=/spool/3", data={"csrf": tok, "password": "wrong"}, headers=headers)
    assert "Неверный пароль" in r.get_data(as_text=True)
    assert "Показать пароль" in r.get_data(as_text=True)
    r = anon.post("/login?next=/spool/3", data={"csrf": tok, "password": ADMIN_PW}, headers=headers)
    assert r.location == "/filament/spool/3"


def test_viewer_password(anon, admin):
    from werkzeug.security import generate_password_hash
    cfg = filament.load_config()
    cfg["viewer_password_hash"] = generate_password_hash("look123", method="pbkdf2:sha256:1000")
    filament.save_config(cfg)
    assert anon.get("/").status_code == 302
    assert anon.get("/static/style.css").status_code == 200
    assert admin.get("/").status_code == 200
    anon.get("/login")
    with anon.session_transaction() as s:
        tok = s["csrf"]
    anon.post("/login", data={"csrf": tok, "password": "look123"})
    assert anon.get("/").status_code == 200
    assert anon.get("/spool/new").status_code == 302  # зритель не админ


# --- катушки -----------------------------------------------------------------

def test_spool_crud_and_search(admin, db):
    sid = add_spool(admin, number="№12", color_name="Чёрный матовый", location="Сухобокс")
    row = db.execute("SELECT * FROM spools WHERE id = ?", (sid,)).fetchone()
    assert row["number"] == "12" and row["weight_left"] == 1000
    for q in ["чёрный", "ЧЁРНЫЙ", "сухобокс", "12", "№12"]:
        assert admin.get("/?q=" + q).get_data(as_text=True).count('class="card status-') == 1, q
    assert admin.get("/spool/new").get_data(as_text=True).count('name="number" value="13"') == 1
    admin.post("/spool/%d/delete" % sid, data={"csrf": "t"})
    assert db.execute("SELECT COUNT(*) FROM spools").fetchone()[0] == 0


def test_bad_input(admin):
    r = admin.post("/spool/new", data={"csrf": "t", "manufacturer": "X", "material": "PLA", "price": "abc"})
    assert "должно быть числом" in r.get_data(as_text=True)
    r = admin.post("/spool/new", data={"csrf": "t", "manufacturer": "X", "material": "PLA",
                                       "purchase_url": "javascript:alert(1)"})
    assert "http" in r.get_data(as_text=True)


def test_weigh_tare_and_history(admin, db):
    a = add_spool(admin)
    b = add_spool(admin)
    r = admin.post("/spool/%d/weigh" % a, data={"csrf": "t", "gross": "1 030", "tare": "230", "tare_for_all": "1"})
    assert ("ok", "Остаток: 800 г (на весах 1030 г − катушка 230 г).") in flashes(admin, r)
    row = db.execute("SELECT * FROM spools WHERE id = ?", (a,)).fetchone()
    assert row["weight_left"] == 800 and row["opened_date"]
    assert db.execute("SELECT weight FROM tares").fetchone()[0] == 230
    assert 'name="tare" inputmode="decimal" required value="230"' in admin.get("/spool/%d" % b).get_data(as_text=True)
    used = db.execute("SELECT used, cost FROM usage").fetchone()
    assert used["used"] == 200 and round(used["cost"]) == 200


def test_use_opened_dried(admin, db):
    sid = add_spool(admin)
    admin.post("/spool/%d/use" % sid, data={"csrf": "t", "grams": "37,5"})
    admin.post("/spool/%d/dried" % sid, data={"csrf": "t"})
    row = db.execute("SELECT * FROM spools WHERE id = ?", (sid,)).fetchone()
    assert row["weight_left"] == 962.5 and row["opened_date"] and row["dried_date"]


def test_needs_drying(admin, db):
    sid = add_spool(admin, material="CR-PETG", opened_date="2020-01-01")
    assert "dry-tag" in admin.get("/").get_data(as_text=True)
    admin.post("/spool/%d/dried" % sid, data={"csrf": "t"})
    assert "dry-tag" not in admin.get("/").get_data(as_text=True)


def test_photo_upload_shrinks_and_thumbs(admin, db):
    r = admin.post("/spool/new", data={"csrf": "t", "manufacturer": "X", "material": "PLA",
                                       "photo": (jpeg((4000, 3000)), "big.jpg")},
                   content_type="multipart/form-data")
    name = db.execute("SELECT photo FROM spools").fetchone()[0]
    from PIL import Image
    assert max(Image.open(filament.os.path.join(filament.UPLOAD_DIR, name)).size) == 2000
    assert admin.get("/thumbs/" + name).status_code == 200
    assert admin.get("/thumbs/../app.py").status_code == 404
    r = admin.post("/spool/new", data={"csrf": "t", "manufacturer": "X", "material": "PLA",
                                       "photo": (io.BytesIO(b"nope"), "x.jpg")}, content_type="multipart/form-data")
    assert "Не удалось прочитать фото" in r.get_data(as_text=True)


# --- печати ------------------------------------------------------------------

def test_print_job_with_photo(admin, db):
    sid = add_spool(admin)
    r = admin.post("/spool/%d/prints" % sid, data={"csrf": "t", "caption": "Кронштейн", "grams": "42,5",
                                                   "duration": "1ч 30м", "photos": [(jpeg(), "a.jpg")]},
                   content_type="multipart/form-data")
    assert flashes(admin, r)[0][0] == "ok"
    job = db.execute("SELECT * FROM jobs").fetchone()
    assert (job["grams"], job["minutes"], round(job["cost"], 1)) == (42.5, 90, 42.5)
    assert db.execute("SELECT job_id FROM prints").fetchone()[0] == job["id"]
    assert db.execute("SELECT weight_left FROM spools").fetchone()[0] == 957.5
    assert "Кронштейн" in admin.get("/jobs").get_data(as_text=True)
    r = admin.post("/spool/%d/prints" % sid, data={"csrf": "t", "duration": "abc"})
    assert flashes(admin, r)[0][0] == "error"
    admin.post("/spool/%d/delete" % sid, data={"csrf": "t"})
    assert db.execute("SELECT COUNT(*) FROM jobs").fetchone()[0] == 0
    assert db.execute("SELECT COUNT(*) FROM prints").fetchone()[0] == 0


@pytest.mark.parametrize("raw,minutes", [("1:30", 90), ("90", 90), ("1ч 30м", 90), ("2,5 ч", 150), ("45 мин", 45), ("", None)])
def test_parse_duration(raw, minutes):
    assert filament.parse_duration(raw) == minutes


# --- справочники, остатки, этикетки, выгрузка -----------------------------------

def test_profiles(admin):
    with filament.app.app_context():
        assert filament.find_profile("CR-PETG", "Creality")["material"] == "PETG"
        assert filament.find_profile("EN-PLA+", "X")["material"] == "PLA+"
        assert filament.find_profile("Nylon", "X") is None
    admin.post("/profiles/new", data={"csrf": "t", "material": "PETG", "manufacturer": "Plastiq", "nozzle": "235"})
    with filament.app.app_context():
        assert filament.find_profile("PETG", "plastiq")["nozzle"] == "235"
    r = admin.post("/profiles/new", data={"csrf": "t", "material": "petg", "manufacturer": "PLASTIQ"})
    assert "уже есть" in r.get_data(as_text=True)


def test_stock_and_labels(admin):
    sid = add_spool(admin, number="5")
    admin.post("/spool/%d/use" % sid, data={"csrf": "t", "grams": "900"})
    html = admin.get("/stock").get_data(as_text=True)
    assert "купить" not in html or "100 г" in html
    assert 'class="hbar"' in html
    labels = admin.get("/labels?id=%d" % sid).get_data(as_text=True)
    assert labels.count('class="label"') == 1 and "<svg" in labels and "№5" in labels


def test_export_roundtrip(admin):
    add_spool(admin, number="1", location="Полка", color_name="красный, зеленый")
    text = admin.get("/export.csv?status=all").get_data().decode("utf-8-sig")
    header, body, _ = importer.parse_csv(text)
    mapping = importer.guess_mapping(header, body)
    assert {"number", "manufacturer", "material", "location", "price", "photo_url"} <= set(mapping)
    rows = importer.parse_rows(header, body, mapping, filament.MATERIALS)
    assert rows[0]["data"]["location"] == "Полка" and not rows[0]["errors"]


def test_gradient_swatch(admin):
    add_spool(admin, color_name="красный, зеленый")
    add_spool(admin, color_name="радужный")
    html = admin.get("/").get_data(as_text=True)
    assert html.count('class="swatch" style="background: linear-gradient') == 2
    assert html.count('class="spool-art" style="--sw: linear-gradient') == 2  # и нарисованная катушка


# --- импорт ------------------------------------------------------------------

SHEET = ("3d,,,,\n"
         ",тип пластика,цвет,производитель,комментарий,,\n"
         "16,PLA,черный,мако,,PLA черный пластик для 3D принтера 1 кг,983 ₽\n"
         "19,PETG,Зеленый,,НЕ ЛЕЗЕТ,PETG пластик салатовый для 3D печати,1 389 ₽\n"
         "\n"
         "20,CR-PETG,радужный,Creality,,Катушка CR-PETG пластика Creality 1.75 мм,\n")


def upload(admin, text):
    r = admin.post("/import", data={"csrf": "t", "csv_file": (io.BytesIO(text.encode()), "s.csv")},
                   content_type="multipart/form-data")
    assert r.status_code == 302 and "/import/" in r.location
    return r.location.rsplit("/", 1)[1], admin.get(r.location).get_data(as_text=True)


def run_import(admin, token, html, **extra):
    form = {"csrf": "t", "skip_dups": "1", "do_update": "1", "use_photos": "1", "action": "run"}
    for m in re.finditer(r'<select name="(map_\d+)">(.*?)</select>', html, re.S):
        x = re.search(r'<option value="(\w*)" selected', m.group(2))
        form[m.group(1)] = x.group(1) if x else ""
    form.update(extra)
    return admin.post("/import/" + token, data=form)


def test_import_guess_and_run(admin, db):
    token, html = upload(admin, SHEET)
    assert "Заголовки найдены в строке 2" in html
    assert "Импортировать: новых 3" in html
    r = run_import(admin, token, html)
    assert flashes(admin, r)[0] == ("ok", "Импортировано катушек: 3.")
    rows = {r["number"]: r for r in db.execute("SELECT * FROM spools")}
    assert rows["16"]["price"] == 983 and rows["16"]["color_hex"] == "#1a1a1a"
    assert rows["19"]["manufacturer"] == "Не указан"
    assert rows["19"]["alert"] == "НЕ ЛЕЗЕТ" and "салатовый" in rows["19"]["notes"]
    assert "⚠ НЕ ЛЕЗЕТ" in admin.get("/").get_data(as_text=True)
    # повторный импорт — ничего нового
    token, html = upload(admin, SHEET)
    assert "Импортировать: новых 0" in html


def test_import_updates_by_number(admin, db):
    token, html = upload(admin, SHEET)
    run_import(admin, token, html)
    db.execute("UPDATE spools SET weight_left = 300 WHERE number = '16'")
    db.commit()
    edited = SHEET.replace("983 ₽", "1 100 ₽").replace("16,PLA,черный", "16,PLA,бордовый")
    token, html = upload(admin, edited)
    assert "обновить 1" in html and "1100" in html
    r = run_import(admin, token, html)
    assert "Обновлено катушек: 1." in flashes(admin, r)[0][1]
    row = db.execute("SELECT * FROM spools WHERE number = '16'").fetchone()
    assert (row["price"], row["color_name"], row["weight_left"]) == (1100, "бордовый", 300)
    # пустая ячейка не стирает
    token, html = upload(admin, SHEET.replace("983 ₽", "").replace("16,PLA,черный", "16,PLA,бордовый"))
    assert "обновить" not in html.split("Импортировать:")[1].split("<")[0]


def test_saved_mapping_with_appended_columns(fresh_data):
    cfg = filament.load_config()
    cfg["import_mapping"] = {"header": ["", "тип", "x"], "mapping": ["number", "material", "price"]}
    filament.save_config(cfg)
    body = [(2, ["1", "PLA", "900", "", "01.09.2026"])]
    # прежние колонки — как сохранено (даже если угадалось бы иначе), новые — угадываются
    assert filament.saved_mapping(["", "тип", "x", "", "покупка"], body) == ["number", "material", "price", "", "purchase_date"]
    assert filament.saved_mapping(["другое"], [(2, ["1"])]) == importer.guess_mapping(["другое"], [(2, ["1"])])


def make_data_xlsx(rows, date_cells=()):
    """Минимальный .xlsx: строки — списки значений (str — общие строки, числа — числа)."""
    shared, sheet = [], []
    for r, row in enumerate(rows, 1):
        cells = []
        for c, v in enumerate(row):
            ref = "%s%d" % (chr(65 + c), r)
            if isinstance(v, str):
                shared.append(v)
                cells.append('<c r="%s" t="s"><v>%d</v></c>' % (ref, len(shared) - 1))
            else:
                style = ' s="1"' if ref in date_cells else ""
                cells.append('<c r="%s"%s><v>%s</v></c>' % (ref, style, v))
        sheet.append('<row r="%d">%s</row>' % (r, "".join(cells)))
    b = io.BytesIO()
    with zipfile.ZipFile(b, "w") as z:
        z.writestr("xl/workbook.xml", '<workbook><sheets><sheet name="A" sheetId="1" r:id="rId1"/></sheets></workbook>')
        z.writestr("xl/_rels/workbook.xml.rels",
                   '<Relationships><Relationship Id="rId1" Target="worksheets/sheet1.xml"/></Relationships>')
        z.writestr("xl/worksheets/sheet1.xml", "<worksheet><sheetData>%s</sheetData></worksheet>" % "".join(sheet))
        z.writestr("xl/sharedStrings.xml", "<sst>%s</sst>" % "".join("<si><t>%s</t></si>" % v for v in shared))
        z.writestr("xl/styles.xml", '<styleSheet><cellXfs count="2"><xf numFmtId="0"/><xf numFmtId="14"/></cellXfs></styleSheet>')
    return b.getvalue()


def test_import_xlsx_file(admin, db):
    raw = make_data_xlsx([["№", "Производитель", "Тип пластика", "Цвет", "Цена", "Дата покупки"],
                     [1, "Bestfilament", "PETG", "Синий &amp; белый", 1290, 46000]], date_cells={"F2"})
    r = admin.post("/import", data={"csrf": "t", "csv_file": (io.BytesIO(raw), "spools.xlsx")},
                   content_type="multipart/form-data")
    assert r.status_code == 302 and "/import/" in r.location
    token = r.location.rstrip("/").split("/")[-1]
    text = open(filament.os.path.join(filament.IMPORT_DIR, token, "sheet.csv"), encoding="utf-8").read()
    assert "Синий & белый" in text and "2025-12-09" in text and ",1290," in text
    bad = admin.post("/import", data={"csrf": "t", "csv_file": (io.BytesIO(b"PK\x03\x04junk"), "x.xlsx")},
                     content_type="multipart/form-data")
    assert "не файл Excel" in bad.get_data(as_text=True)


def test_import_cp1251_semicolon_units(admin, db):
    text = ("Бренд;Материал;Цвет;Вес;Остаток;Цена;Дата покупки;Вскрыта\n"
            "SUNLU;pla+;Белый;1 кг;0,4 кг;1 389,00 руб;12.03.2025;да\n")
    r = admin.post("/import", data={"csrf": "t", "csv_file": (io.BytesIO(text.encode("cp1251")), "s.csv")},
                   content_type="multipart/form-data")
    html = admin.get(r.location).get_data(as_text=True)
    run_import(admin, r.location.rsplit("/", 1)[1], html)
    row = db.execute("SELECT * FROM spools").fetchone()
    assert (row["material"], row["weight_total"], row["weight_left"], row["price"]) == ("PLA+", 1000, 400, 1389)
    assert (row["purchase_date"], row["opened_date"]) == ("2025-03-12", "2025-03-12")


def make_xlsx(csv_text, photo_rows):
    """Минимальный xlsx как у Google: картинки поверх ячеек, привязка по строке (с нуля)."""
    b = io.BytesIO()
    z = zipfile.ZipFile(b, "w")
    strings = sorted({c for line in csv_text.splitlines() for c in line.split(",") if c})
    z.writestr("xl/sharedStrings.xml", "<sst>%s</sst>" % "".join("<si><t>%s</t></si>" % s for s in strings))
    z.writestr("xl/workbook.xml", '<workbook><sheets><sheet name="Лист1" r:id="rId1"/></sheets></workbook>')
    z.writestr("xl/_rels/workbook.xml.rels",
               '<Relationships><Relationship Id="rId1" Target="worksheets/sheet1.xml"/></Relationships>')
    cells = "".join('<c t="s"><v>%d</v></c>' % i for i in range(len(strings)))
    z.writestr("xl/worksheets/sheet1.xml", '<worksheet><sheetData><row>%s</row></sheetData><drawing r:id="rId1"/></worksheet>' % cells)
    z.writestr("xl/worksheets/_rels/sheet1.xml.rels",
               '<Relationships><Relationship Id="rId1" Target="../drawings/drawing1.xml"/></Relationships>')
    anchors, rels = "", ""
    for i, row in enumerate(photo_rows, 1):
        anchors += ('<xdr:oneCellAnchor><xdr:from><xdr:col>7</xdr:col><xdr:row>%d</xdr:row></xdr:from>'
                    '<xdr:pic><a:blip r:embed="rId%d"/></xdr:pic></xdr:oneCellAnchor>' % (row - 1, i))
        rels += '<Relationship Id="rId%d" Target="../media/image%d.jpg"/>' % (i, i)
        z.writestr("xl/media/image%d.jpg" % i, jpeg(color=["red", "green", "blue"][i % 3]).getvalue())
    z.writestr("xl/drawings/drawing1.xml", "<xdr:wsDr>%s</xdr:wsDr>" % anchors)
    z.writestr("xl/drawings/_rels/drawing1.xml.rels", "<Relationships>%s</Relationships>" % rels)
    z.close()
    return zipfile.ZipFile(io.BytesIO(b.getvalue()))


def test_xlsx_photos_by_row():
    photos = importer.xlsx_photos(make_xlsx(SHEET, [3, 6]), SHEET)
    assert sorted(photos) == [3, 6] and photos[3][0] == "jpg"


def test_import_from_link_with_photos(admin, db, monkeypatch):
    """Ссылка на Google Таблицу: CSV + фото из xlsx; к существующей без фото фото добавляется."""
    monkeypatch.setattr(importer, "fetch_sheet", lambda url: SHEET)
    monkeypatch.setattr(importer, "fetch_sheet_photos", lambda url, text: importer.xlsx_photos(make_xlsx(SHEET, [3, 6]), text))
    url = "https://docs.google.com/spreadsheets/d/abc/edit"
    r = admin.post("/import", data={"csrf": "t", "sheet_url": url})
    token = r.location.rsplit("/", 1)[1]
    html = admin.get(r.location).get_data(as_text=True)
    assert "фото из таблицы (2 шт.)" in html
    assert admin.get("/import/%s/photo/3" % token).status_code == 200
    run_import(admin, token, html)
    photos = {r["number"]: r["photo"] for r in db.execute("SELECT number, photo FROM spools")}
    assert photos["16"] and photos["20"] and not photos["19"]
    assert filament.load_config()["sheet_url"] == url
    assert "↻ Из таблицы" in admin.get("/").get_data(as_text=True)


def test_sheet_url_conversion():
    assert importer.sheet_csv_url("https://docs.google.com/spreadsheets/d/AbC-1/edit#gid=42") == \
        "https://docs.google.com/spreadsheets/d/AbC-1/export?format=csv&gid=42"
    assert "output=xlsx" in importer.sheet_csv_url("https://docs.google.com/spreadsheets/d/e/2PACX-x/pubhtml", "xlsx")
    with pytest.raises(importer.SheetError):
        importer.sheet_csv_url("not a link")


@pytest.mark.parametrize("raw,grams", [("1 кг", 1000), ("0,75 кг", 750), ("750", 750), ("1", 1000), ("1kg", 1000)])
def test_parse_weight(raw, grams):
    assert importer.parse_weight(raw) == grams


@pytest.mark.parametrize("name,colors", [("черный, темно-зеленый", ["#1a1a1a", "#388e3c"]), ("серебряный", ["#bdbdbd"]),
                                         ("зеленый прозрачный", ["#388e3c"]), ("радужный", importer.RAINBOW)])
def test_colors_from_name(name, colors):
    assert importer.colors_from_name(name) == colors


def test_cli_sync(admin, db, monkeypatch, capsys):
    monkeypatch.setattr(importer, "fetch_sheet", lambda url: SHEET)
    monkeypatch.setattr(importer, "fetch_sheet_photos", lambda url, text: {})
    cfg = filament.load_config()
    cfg["sheet_url"] = "https://docs.google.com/spreadsheets/d/abc/edit"
    filament.save_config(cfg)
    filament.cli_sync()
    assert db.execute("SELECT COUNT(*) FROM spools").fetchone()[0] == 3
    db.execute("UPDATE spools SET price = 1 WHERE number = '16'")
    db.commit()
    filament.cli_sync()
    out = capsys.readouterr().out
    assert "Ждут подтверждения изменений: 1." in out
    assert db.execute("SELECT price FROM spools WHERE number = '16'").fetchone()[0] == 1  # ночью не обновляем


def test_pwa(anon):
    assert anon.get("/manifest.webmanifest").get_json()["start_url"] == "./"
    sw = anon.get("/sw.js")
    assert sw.status_code == 200 and "no-cache" in sw.headers["Cache-Control"]
    assert anon.get("/offline").status_code == 200
    html = anon.get("/").get_data(as_text=True)
    assert 'rel="manifest"' in html and "data-sw=" in html and "app.js" in html


def test_reorder_and_arrive(admin, db):
    sid = add_spool(admin, number="3", purchase_url="https://ozon.ru/x",
                    photo=(jpeg(), "a.jpg"))
    admin.post("/spool/%d/use" % sid, data={"csrf": "t", "grams": "950"})
    assert "🛒 заказал" in admin.get("/stock").get_data(as_text=True)
    r = admin.post("/spool/%d/reorder" % sid, data={"csrf": "t"})
    new = int(r.location.rsplit("/", 1)[1])
    row = db.execute("SELECT * FROM spools WHERE id = ?", (new,)).fetchone()
    assert (row["ordered"], row["weight_left"], row["number"], row["purchase_url"]) == (1, 1000, "4", "https://ozon.ru/x")
    assert row["photo"] and row["photo"] != db.execute("SELECT photo FROM spools WHERE id = ?", (sid,)).fetchone()[0]
    assert admin.get("/").get_data(as_text=True).count('class="card status-') == 1  # в наличии — только старая
    assert admin.get("/?status=ordered").get_data(as_text=True).count('class="card status-') == 1
    stock = admin.get("/stock").get_data(as_text=True)
    assert "Заказано (1)" in stock and "уже заказана" in stock
    admin.post("/spool/%d/arrived" % new, data={"csrf": "t"})
    assert admin.get("/").get_data(as_text=True).count('class="card status-') == 2


def test_stats_page(admin):
    a = add_spool(admin, material="PETG", color_name="красный", price="1000", purchase_date="2026-09-03")
    add_spool(admin, material="PETG", manufacturer="Other", price="1500", purchase_date="2026-09-20")
    admin.post("/spool/%d/use" % a, data={"csrf": "t", "grams": "100"})
    html = admin.get("/stats").get_data(as_text=True)
    assert "09.2026" in html and "2 шт · 2 500 ₽" in html
    assert "<b>PETG</b>" in html and "<b>красный</b>" in html and "100 г" in html
    assert "1 500 ₽" in html  # цена за кг Other
    assert filament.app.test_client().get("/stats").status_code == 200


def test_price_comparison(admin):
    cheap = add_spool(admin, manufacturer="Дешёвый", material="PETG", price="700")
    dear = add_spool(admin, manufacturer="Дорогой", material="CR-PETG", price="1300")
    add_spool(admin, manufacturer="Другой", material="PLA", price="100")  # другой материал не учитывается
    html = admin.get("/spool/%d" % dear).get_data(as_text=True)
    assert "на 30% дороже" in html and "средней по PETG (1 000 ₽)" in html and "Дешёвый</a>, 700 ₽/кг" in html
    assert "самая дешёвая" in admin.get("/spool/%d" % cheap).get_data(as_text=True)
    admin.post("/spool/%d/use" % dear, data={"csrf": "t", "grams": "950"})
    assert "дешевле: Дешёвый, 700 ₽/кг" in admin.get("/stock").get_data(as_text=True)
    with filament.app.app_context():
        assert [filament.base_material(m) for m in ["EN-PLA+", "PETG+", "Silk PLA", "Nylon"]] == ["PLA+", "PETG", "PLA", "Nylon"]


def test_back_to_list_keeps_filters(admin):
    sid = add_spool(admin, material="PETG")
    admin.get("/?material=PETG&status=all&sort=number&q=")
    for page in ["/labels?material=PETG&status=all", "/spool/%d" % sid]:
        html = admin.get(page).get_data(as_text=True)
        back = re.search(r'<a (?:class="btn back-btn" )?href="([^"]+)"(?: class="back")?>← Все катушки</a>', html).group(1).replace("&amp;", "&")
        assert "material=PETG" in back and "status=all" in back and "sort=number" in back and "q=" not in back, page
    admin.get("/")  # без фильтров — и ссылка без них
    assert '<a class="btn back-btn" href="/">← Все катушки</a>' in admin.get("/spool/%d" % sid).get_data(as_text=True)


def test_reset_filters_button(admin):
    add_spool(admin)
    assert "✕ Сбросить" not in admin.get("/").get_data(as_text=True)
    assert "✕ Сбросить" not in admin.get("/?status=active&sort=new&q=").get_data(as_text=True)
    assert "✕ Сбросить" in admin.get("/?material=PETG").get_data(as_text=True)
    assert "✕ Сбросить" in admin.get("/?sort=number").get_data(as_text=True)


def test_empty_page(admin):
    add_spool(admin, color_name="Полная")
    add_spool(admin, color_name="Пустая", weight_left="0")
    for url in ("/", "/?status=all", "/?status=empty"):
        html = admin.get(url).get_data(as_text=True)
        assert "Полная" in html and "Пустая" not in html
    html = admin.get("/empty").get_data(as_text=True)
    assert "Пустая" in html and "Полная" not in html
    assert 'name="status"' not in html and "✕ Сбросить" not in html
    assert "Пустая" in admin.get("/export.csv?status=empty").get_data(as_text=True)


def test_start_block_when_no_spools(admin):
    html = admin.get("/").get_data(as_text=True)
    assert "С чего начать" in html and "/import" in html and "Подключить сервер" not in html  # не телефон
    add_spool(admin)
    assert "С чего начать" not in admin.get("/").get_data(as_text=True)


def test_theme_toggle(anon):
    assert "data-theme" not in anon.get("/").get_data(as_text=True).split(">")[1]
    assert '<meta name="color-scheme" content="dark">' in anon.get("/").get_data(as_text=True)  # без cookie — тёмная
    r = anon.get("/theme?next=/stats")
    assert r.status_code == 302 and r.location.endswith("/stats") and "theme=light" in r.headers["Set-Cookie"]
    assert '<html lang="ru" class="no-js" data-theme="light">' in anon.get("/").get_data(as_text=True)
    assert "theme=system" in anon.get("/theme").headers["Set-Cookie"]
    assert '<meta name="color-scheme" content="light dark">' in anon.get("/").get_data(as_text=True)
    assert "theme=dark" in anon.get("/theme").headers["Set-Cookie"]  # по кругу
    assert anon.get("/theme?next=//evil.example").location.endswith("/")  # чужой адрес не принимаем
    assert "evil" not in anon.get("/theme?next=//evil.example").location


def test_qr_labels_button(admin):
    add_spool(admin, color_name="Есть")
    add_spool(admin, color_name="Пустая", weight_left="0")
    html = admin.get("/").get_data(as_text=True)
    assert "status=active" in html and "QR-этикетки" in html
    labels = admin.get("/labels?status=active").get_data(as_text=True)
    assert "Есть" in labels and "Пустая" not in labels


def test_pasted_password_cleanup(anon, monkeypatch):
    answers = iter(["\x1b[200~secret-pass-123\x1b[201~", " secret-pass-123\n"])
    import cli
    monkeypatch.setattr(cli, "getpass", lambda prompt: next(answers))
    filament.set_admin("admin", cli.ask_password("x"))
    anon.get("/login")
    with anon.session_transaction() as s:
        tok = s["csrf"]
    r = anon.post("/login", data={"csrf": tok, "password": "secret-pass-123 "})
    assert r.status_code == 302 and "/login" not in r.location


def test_help_page(anon):
    html = anon.get("/help").get_data(as_text=True)
    assert "Play Защита" in html and "Всё равно установить" in html and "releases/latest" in html
    assert "/help" in anon.get("/").get_data(as_text=True)


def test_fill_table(admin, db):
    a = add_spool(admin, number="1", price="")
    b = add_spool(admin, number="2", price="900", purchase_date="2026-01-01")
    html = admin.get("/fill").get_data(as_text=True)
    assert "f_%d_price" % a in html and "f_%d_price" % b not in html  # только с пробелами
    assert "f_%d_price" % b in admin.get("/fill?all=1").get_data(as_text=True)
    form = {"csrf": "t", "f_%d_price" % a: "1 290,50 ₽", "f_%d_purchase_date" % a: "2026-09-01",
            "f_%d_weight_left" % a: "640", "f_%d_location" % a: "Полка", "f_%d_number" % a: "1",
            "f_%d_weight_total" % a: "1000", "f_%d_opened_date" % a: ""}
    r = admin.post("/fill", data=form)
    assert ("ok", "Сохранено катушек: 1.") in flashes(admin, r)
    row = db.execute("SELECT * FROM spools WHERE id = ?", (a,)).fetchone()
    assert (row["price"], row["purchase_date"], row["weight_left"], row["location"]) == (1290.5, "2026-09-01", 640, "Полка")
    assert db.execute("SELECT used FROM usage").fetchone()[0] == 360
    assert db.execute("SELECT action FROM audit ORDER BY id DESC").fetchone()[0] == "Быстрое заполнение"
    # ошибка в одной строке не мешает другим и ничего в ней не меняет
    r = admin.post("/fill", data={"csrf": "t", "f_%d_price" % b: "abc", "f_%d_weight_left" % a: "5000",
                                  "f_%d_location" % b: "Ящик"})
    msgs = flashes(admin, r)
    assert msgs[0][0] == "error" and "№2" in msgs[0][1] and "№1" in msgs[0][1]
    assert tuple(db.execute("SELECT price, location FROM spools WHERE id = ?", (b,)).fetchone()) == (900, "")
    r = admin.post("/fill", data={"csrf": "t", "f_%d_price" % b: "900"})
    assert ("ok", "Изменений нет.") in flashes(admin, r)


def test_bulk_edit(admin, db):
    a, b, c = add_spool(admin, number="1"), add_spool(admin, number="2"), add_spool(admin, number="3")
    html = admin.get("/").get_data(as_text=True)
    assert 'name="ids" value="%d"' % a in html and "☑ Выбрать" in html
    r = admin.post("/bulk", data={"csrf": "t", "action": "location", "value": "Сухобокс", "ids": [a, b]})
    assert "изменено катушек: 2 из 2" in flashes(admin, r)[0][1]
    admin.post("/bulk", data={"csrf": "t", "action": "opened", "ids": [a, b]})
    admin.post("/bulk", data={"csrf": "t", "action": "opened", "ids": [a]})  # уже вскрыта — не меняется
    rows = {r["id"]: r for r in db.execute("SELECT * FROM spools")}
    assert rows[a]["location"] == rows[b]["location"] == "Сухобокс" and rows[c]["location"] == ""
    assert rows[a]["opened_date"] and rows[b]["opened_date"] and not rows[c]["opened_date"]
    assert db.execute("SELECT COUNT(*) FROM audit WHERE action LIKE 'Массовая правка%'").fetchone()[0] == 4
    r = admin.post("/bulk", data={"csrf": "t", "action": "price", "value": "abc", "ids": [a]})
    assert any(kind == "error" for kind, _ in flashes(admin, r))
    eid = db.execute("SELECT MAX(id) FROM audit WHERE spool_id = ?", (b,)).fetchone()[0]
    admin.post("/history", data={"csrf": "t", "id": eid})  # отмена «вскрыты» у одной
    assert db.execute("SELECT opened_date FROM spools WHERE id = ?", (b,)).fetchone()[0] is None
    assert "☑ Выбрать" not in filament.app.test_client().get("/").get_data(as_text=True)


def test_enough(admin, anon, db):
    opened = add_spool(admin, number="1", material="PETG", color_name="Чёрный", opened_date="2026-01-01")
    small = add_spool(admin, number="2", material="CR-PETG", color_name="черный матовый")
    big = add_spool(admin, number="3", material="PETG", color_name="черный")
    add_spool(admin, number="4", material="PLA", color_name="черный")
    add_spool(admin, number="5", material="PETG", color_name="белый")
    tight = add_spool(admin, number="6", material="PETG", color_name="черный")
    db.execute("UPDATE spools SET weight_left = 300 WHERE id = ?", (small,))
    db.execute("UPDATE spools SET weight_left = 600 WHERE id = ?", (opened,))
    db.execute("UPDATE spools SET weight_left = 200 WHERE id = ?", (tight,))
    db.commit()
    html = anon.get("/enough?grams=250&material=PETG&color=чёрн").get_data(as_text=True)
    fit = html.split("Впритык")[0]
    order = re.findall(r"<tr><td>(\d+)</td>", fit)
    assert order == ["1", "2", "3"]  # вскрытая первой, затем по возрастанию остатка; PLA и белый — нет
    assert "Впритык" in html and "не хватит 50 г" in html  # №6
    r = anon.post("/slice", data={"csrf": "x", "kind": "gcode", "text": "; total filament used [g] = 12.5"})
    assert r.status_code == 400  # без CSRF из сессии
    anon.get("/enough")
    with anon.session_transaction() as s:
        tok = s["csrf"]
    r = anon.post("/slice", data={"csrf": tok, "kind": "gcode", "text": "; total filament used [g] = 12.5"})
    assert r.get_json()["grams"] == 12.5


def test_color_search(admin):
    add_spool(admin, number="1", color_name="красный")
    add_spool(admin, number="2", color_name="бордовый")
    add_spool(admin, number="3", color_name="синий")
    add_spool(admin, number="4", color_name="черный, красный")  # многоцветная — тоже «красная»
    with filament.app.app_context():
        assert filament.color_distance("#d32f2f", "#d32f2f") == 0
        assert filament.color_distance("#d32f2f", "#1976d2") > filament.COLOR_NEAR
    html = admin.get("/?status=all").get_data(as_text=True)
    assert html.count('class="pal') >= 3
    html = admin.get("/?status=all&color=%23d32f2f").get_data(as_text=True)
    nums = re.findall(r'num-tag">№(\d+)', html)
    assert set(nums) == {"1", "4"}  # бордовый и синий — уже другие оттенки
    assert "✕ Сбросить" in html
    assert admin.get("/?color=javascript:x").status_code == 200  # мусор игнорируется


def test_meters_left(admin):
    with filament.app.app_context():
        assert round(filament.grams_to_meters(1000, "PLA", 1.75)) == 335
        assert round(filament.grams_to_meters(1000, "CR-PETG", 1.75)) == 327
        assert filament.grams_to_meters(0, "PLA", 1.75) == 0
    sid = add_spool(admin, material="PLA")
    assert "≈ 335 м" in admin.get("/spool/%d" % sid).get_data(as_text=True)



def test_login_security(anon):
    r = anon.get("/login?password=secret")
    assert r.status_code == 302 and "password" not in r.location
    r = anon.get("/login", base_url="https://localhost/")
    assert r.headers["Cache-Control"] == "no-store"
    assert "frame-ancestors 'none'" in r.headers["Content-Security-Policy"]
    assert "X-Frame-Options" not in r.headers  # ставит nginx; дубль конфликтует
    assert 'method="post"' in r.get_data(as_text=True)
    with anon.session_transaction() as s:
        tok = s["csrf"]
    for _ in range(filament.LOGIN_MAX_FAILS):
        anon.post("/login", data={"csrf": tok, "password": "wrong"})
    r = anon.post("/login", data={"csrf": tok, "password": ADMIN_PW})
    assert r.status_code == 429  # даже верный пароль — после серии неудач


def test_back_button_everywhere(admin):
    sid = add_spool(admin)
    for page in ["/spool/%d" % sid, "/stock", "/stats", "/enough", "/fill", "/history", "/jobs"]:
        assert "← Все катушки" in admin.get(page).get_data(as_text=True), page
    assert "← Все катушки" not in admin.get("/").get_data(as_text=True)


def test_templates(admin, db):
    sid = add_spool(admin, number="1", manufacturer="Plastiq", material="PETG", color_name="белый", price="760",
                    purchase_url="https://ozon.ru/p", alert="не лезет в CFS")
    admin.post("/spool/%d/template" % sid, data={"csrf": "t"})
    admin.post("/spool/%d/template" % sid, data={"csrf": "t"})  # повтор — обновление, не дубль
    tpl = db.execute("SELECT id, name FROM templates").fetchall()
    assert len(tpl) == 1 and tpl[0]["name"] == "Plastiq PETG белый"
    html = admin.get("/spool/new").get_data(as_text=True)
    assert "Plastiq PETG белый" in html
    form = admin.get("/spool/new?template=%d" % tpl[0]["id"]).get_data(as_text=True)
    assert 'value="760"' in form and 'value="https://ozon.ru/p"' in form and "не лезет в CFS" in form
    assert 'name="number" value="2"' in form  # номер — следующий, не из шаблона
    admin.post("/templates/%d/delete" % tpl[0]["id"], data={"csrf": "t"})
    assert db.execute("SELECT COUNT(*) FROM templates").fetchone()[0] == 0


def test_over_limit_needs_confirmation(admin, db):
    sid = add_spool(admin, alert="не лезет в CFS")
    db.execute("UPDATE spools SET weight_left = 100 WHERE id = ?", (sid,))
    db.commit()
    html = admin.get("/spool/%d" % sid).get_data(as_text=True)
    assert "всё равно записать" in html and html.count("⚠ не лезет в CFS") >= 2  # и у формы печати
    r = admin.post("/spool/%d/use" % sid, data={"csrf": "t", "grams": "150"})
    assert any("по учёту 100 г" in m for _, m in flashes(admin, r))
    r = admin.post("/spool/%d/prints" % sid, data={"csrf": "t", "grams": "150", "caption": "X"})
    assert db.execute("SELECT weight_left FROM spools").fetchone()[0] == 100
    assert db.execute("SELECT COUNT(*) FROM jobs").fetchone()[0] == 0
    admin.post("/spool/%d/prints" % sid, data={"csrf": "t", "grams": "150", "confirm_over": "1"})
    assert db.execute("SELECT weight_left FROM spools").fetchone()[0] == 0
    admin.post("/spool/%d/use" % sid, data={"csrf": "t", "grams": "0.3"})  # в пределах погрешности — без вопросов


def test_year_report(admin, db):
    a = add_spool(admin, price="1000", purchase_date="2026-03-10", material="PETG")
    add_spool(admin, price="500", purchase_date="2025-05-01")
    admin.post("/spool/%d/use" % a, data={"csrf": "t", "grams": "1000"})  # закончилась
    db.execute("UPDATE usage SET day = '2026-04-02'")
    db.commit()
    html = admin.get("/report?year=2026").get_data(as_text=True)
    assert "отчёт за 2026 год" in html and "1 000 ₽" in html and "Закончились в 2026 (1)" in html
    assert "<b>PETG</b>" in html and '<option selected>2026</option>' in html
    assert "1 шт · 500 ₽" in admin.get("/report?year=2025").get_data(as_text=True)
    assert "Годовой отчёт" in admin.get("/stats").get_data(as_text=True)


def test_showcase(admin, anon, db):
    add_spool(admin, color_name="красный", price="999", purchase_url="https://ozon.ru/secret",
              notes="тайная заметка", location="Шкаф", alert="сломана")
    assert "Включить витрину" in admin.get("/stock").get_data(as_text=True)
    admin.post("/showcase-settings", data={"csrf": "t", "action": "new"})
    token = filament.load_config()["showcase_token"]
    html = anon.get("/showcase/" + token).get_data(as_text=True)
    assert "красный" in html and "полная" in html
    for secret in ["999", "ozon.ru", "тайная", "Шкаф", "сломана", "csrf"]:
        assert secret not in html, secret
    assert anon.get("/showcase/wrong").status_code == 404
    # работает и при пароле на просмотр
    from werkzeug.security import generate_password_hash
    cfg = filament.load_config()
    cfg["viewer_password_hash"] = generate_password_hash("look-1234567", method="pbkdf2:sha256:1000")
    filament.save_config(cfg)
    assert anon.get("/").status_code == 302 and anon.get("/showcase/" + token).status_code == 200
    admin.post("/showcase-settings", data={"csrf": "t", "action": "off"})
    assert anon.get("/showcase/" + token).status_code == 404


def test_slicer_vs_scale(admin, db):
    sid = add_spool(admin, manufacturer="Plastiq", material="CR-PETG")
    admin.post("/spool/%d/weigh" % sid, data={"csrf": "t", "gross": "1250", "tare": "250"})  # исходная точка 1000
    admin.post("/spool/%d/prints" % sid, data={"csrf": "t", "grams": "100", "caption": "A"})
    admin.post("/spool/%d/use" % sid, data={"csrf": "t", "grams": "100"})
    admin.post("/spool/%d/weigh" % sid, data={"csrf": "t", "gross": "1030", "tare": "250"})  # факт: 220 вместо 200
    with filament.app.app_context():
        acc = filament.slicer_accuracy(sid)
    assert [(a["slicer"], a["actual"]) for a in acc] == [(200, 220)]
    html = admin.get("/stats").get_data(as_text=True)
    assert "Слайсер против весов" in html and "+10%" in html and "<b>PETG</b>" in html
    assert "по весам 220 г" in admin.get("/spool/%d" % sid).get_data(as_text=True)
    # ручная правка остатка обрывает интервал
    admin.post("/spool/%d/use" % sid, data={"csrf": "t", "grams": "50"})
    admin.post("/fill", data={"csrf": "t", "f_%d_weight_left" % sid: "500"})
    admin.post("/spool/%d/weigh" % sid, data={"csrf": "t", "gross": "700", "tare": "250"})
    with filament.app.app_context():
        assert len(filament.slicer_accuracy(sid)) == 1


def test_printer_maintenance(admin, anon, db):
    assert db.execute("SELECT COUNT(*) FROM maintenance").fetchone()[0] == 7  # стартовый регламент
    sid = add_spool(admin)
    for _ in range(3):  # 3 печати по 20 ч = 60 ч > «протереть стол» (50 ч)
        admin.post("/spool/%d/prints" % sid, data={"csrf": "t", "duration": "20:00", "caption": "x"})
    html = admin.get("/printer").get_data(as_text=True)
    assert "60.0 ч" in html and "пора" in html
    assert 'data-badge="maint">1<' in admin.get("/").get_data(as_text=True)
    table = db.execute("SELECT id FROM maintenance WHERE name LIKE 'Протереть%'").fetchone()[0]
    admin.post("/printer", data={"csrf": "t", "action": "done", "id": table})
    with filament.app.app_context():
        hours, items = filament.maintenance_status()
    assert hours == 60 and not any(m["due"] for m in items)
    admin.post("/printer", data={"csrf": "t", "action": "add", "name": "PTFE", "every_days": "1"})
    db.execute("UPDATE maintenance SET last_date = '2020-01-01' WHERE name = 'PTFE'")
    db.commit()
    assert 'data-badge="maint">1<' in admin.get("/").get_data(as_text=True)
    assert admin.post("/printer", data={"csrf": "t", "action": "add", "name": "x"}).status_code == 302
    assert anon.get("/printer").status_code == 200 and "✓ Сделано" not in anon.get("/printer").get_data(as_text=True)
    anon.get("/login")
    with anon.session_transaction() as s:
        tok = s["csrf"]
    assert anon.post("/printer", data={"csrf": tok, "action": "done", "id": table}).status_code == 403


def test_thermal_labels(admin, anon):
    a, b = add_spool(admin, number="7", alert="не лезет"), add_spool(admin, number="8")
    one = admin.get("/labels?id=%d&print=1" % a).get_data(as_text=True)
    assert one.count('class="label"') == 1 and "data-autoprint" in one  # из карточки — сразу печать
    assert "🖨 только эту" not in one
    both = admin.get("/labels?id=%d&id=%d" % (a, b)).get_data(as_text=True)
    assert both.count('class="label"') == 2 and both.count("🖨 только эту") == 2 and "window.print, 300" not in both
    assert "58 × 40 мм" in both and "@page" in both and "page-break-after: always" in both
    svg = re.search(r"<svg class=\"qr\".*?</svg>", both, re.S).group(0)
    assert 'shape-rendering="crispEdges"' in svg and "id=" not in svg  # без дублей id
    assert anon.get("/s/%d" % a).location.endswith("/spool/%d" % a)
    assert "🖨 Этикетка" in admin.get("/spool/%d" % a).get_data(as_text=True)
    assert 'id="pick-labels"' in admin.get("/").get_data(as_text=True)


def test_qr_svg_decodes():
    """QR из SVG-контура (как его растеризует термопринтер) читается настоящим декодером."""
    zxingcpp = pytest.importorskip("zxingcpp")
    from PIL import Image, ImageDraw
    for url in ["https://filament.example/filament/s/1", "https://filament.example/filament/s/12345"]:
        svg = str(filament.qr_svg(url))
        n = int(re.search(r'viewBox="0 0 (\d+)', svg).group(1))
        k = 8
        img = Image.new("L", (n * k, n * k), 255)
        draw = ImageDraw.Draw(img)
        for x, y, w in re.findall(r"M(\d+) (\d+)h(\d+)v1h-\d+z", svg):
            x, y, w = int(x), int(y), int(w)
            draw.rectangle([x * k, y * k, (x + w) * k - 1, (y + 1) * k - 1], fill=0)
        found = zxingcpp.read_barcodes(img)
        assert found and found[0].text == url


def test_scanner_page(admin, anon):
    html = anon.get("/scan").get_data(as_text=True)
    assert 'id="scan-video"' in html and "vendor/jsqr/jsQR.js" in html and 'data-base="/"' in html
    assert 'aria-label="Сканер"' in anon.get("/").get_data(as_text=True)
    sid = add_spool(admin)
    detail = admin.get("/spool/%d?scan=1" % sid).get_data(as_text=True)
    assert 'id="weigh"' in detail and 'id="spool-left"' in detail


def test_label_png_for_niimbot(admin, anon):
    zxingcpp = pytest.importorskip("zxingcpp")
    from PIL import Image
    sid = add_spool(admin, number="12", material="PETG", color_name="Оранжевый", alert="не лезет в CFS")
    for w, h in [(40, 30), (50, 30), (30, 20), (58, 40)]:
        r = admin.get("/label/%d.png?w=%d&h=%d" % (sid, w, h))
        assert r.status_code == 200 and r.mimetype == "image/png"
        img = Image.open(io.BytesIO(r.data))
        assert img.mode == "1" and abs(img.size[0] - w * 203 / 25.4) <= 1 and abs(img.size[1] - h * 203 / 25.4) <= 1
        found = zxingcpp.read_barcodes(img.convert("L"))
        assert found and found[0].text.endswith("/s/%d" % sid), (w, h)
    assert "📤 В Niimbot" in admin.get("/labels?id=%d" % sid).get_data(as_text=True)
    assert anon.get("/label/999.png").status_code == 404


def test_cfs_plan(admin, anon, db):
    red_opened = add_spool(admin, number="1", material="PETG", color_name="красный", opened_date="2026-01-01")
    red_cfs = add_spool(admin, number="2", material="PETG", color_name="красный", alert="НЕ ЛЕЗЕТ В CMS")
    add_spool(admin, number="3", material="PETG", color_name="белый")
    add_spool(admin, number="4", material="PLA", color_name="белый")
    small_white = add_spool(admin, number="5", material="PETG", color_name="белый")
    db.execute("UPDATE spools SET weight_left = 50 WHERE id = ?", (small_white,))
    db.commit()
    html = anon.get("/cfs?c1=%23d32f2f&m1=PETG&g1=300&c2=%23f5f5f5&m2=PETG&g2=100&c3=%23d32f2f&m3=PETG&g3=10").get_data(as_text=True)
    rows = re.findall(r"<tr class=\"[^\"]*\">\s*<td>(\d+)</td>.*?(?:№(\d+) |нет подходящей)", html, re.S)
    got = dict(rows)
    assert got["1"] == "1"  # вскрытая красная, не та, что «не лезет»
    assert got["2"] == "3"  # белый PETG с остатком (№5 — мало, №4 — PLA)
    assert got["3"] == "2" and "НЕ ЛЕЗЕТ" in html  # второй красной осталась только «не лезет» — с пометкой, строка красная
    assert anon.get("/cfs").status_code == 200 and 'class="slice-file"' in anon.get("/cfs").get_data(as_text=True)


def test_detail_panels_open_after_error(admin):
    sid = add_spool(admin, weight_left="100")
    admin.post("/spool/%d/prints" % sid, data={"csrf": "t", "grams": "10"})  # есть печать — панель записи свёрнута
    html = admin.get("/spool/%d" % sid).get_data(as_text=True)
    assert html.count('<details class="panel" open>') == 0 and '<details class="panel" id="record">' in html
    # списание больше остатка — открыта только панель списания
    r = admin.post("/spool/%d/use" % sid, data={"csrf": "t", "grams": "500"})
    assert "panel=use" in r.location
    html = admin.get(r.location).get_data(as_text=True)
    assert '<details class="panel" open>\n      <summary>…или списать' in html and '<details class="panel" id="record">' in html
    # запись печати с ошибкой — открыта только форма записи
    r = admin.post("/spool/%d/prints" % sid, data={"csrf": "t", "grams": "500"})
    assert "panel=print" in r.location and r.location.endswith("#record")
    html = admin.get(r.location).get_data(as_text=True)
    assert '<details class="panel" id="record" open>' in html and '<details class="panel" open>' not in html
