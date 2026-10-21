import io
import os
import struct

import app as filament
from conftest import add_spool, flashes


def binary_stl(triangles=2):
    data = b"\0" * 80 + struct.pack("<I", triangles)
    for i in range(triangles):
        data += struct.pack("<12fH", 0, 0, 1, 0, 0, 0, 10, 0, 0, 0, 10, i, 0)
    return data


ASCII_STL = b"""solid cube
facet normal 0 0 1
 outer loop
  vertex 0 0 0
  vertex 10 0 0
  vertex 0 10 0
 endloop
endfacet
endsolid cube
"""


def upload(admin, sid, *files, **extra):
    data = {"csrf": "t", "models": [(io.BytesIO(b), n) for b, n in files]}
    data.update(extra)
    return admin.post("/spool/%d/prints" % sid, data=data, content_type="multipart/form-data")


def test_upload_view_delete(admin, anon, db):
    sid = add_spool(admin, color_name="красный")
    r = upload(admin, sid, (binary_stl(3), "Кронштейн.STL"), (ASCII_STL, "cube.stl"), caption="кронштейн", grams="12")
    assert any("Моделей: 2" in m for _, m in flashes(admin, r))
    rows = db.execute("SELECT * FROM models ORDER BY id").fetchall()
    assert [(m["name"], m["triangles"]) for m in rows] == [("Кронштейн.STL", 3), ("cube.stl", 1)]
    assert rows[0]["job_id"] == db.execute("SELECT id FROM jobs").fetchone()[0]
    html = admin.get("/spool/%d" % sid).get_data(as_text=True)
    assert "Кронштейн.STL" in html and "model-thumb" in html
    page = anon.get("/model/%d" % rows[0]["id"]).get_data(as_text=True)
    assert 'class="stl-view"' in page and "vendor/three/three.min.js" in page and rows[0]["file"] in page
    assert anon.get("/uploads/" + rows[0]["file"]).data == binary_stl(3)
    assert "🧊" in admin.get("/jobs").get_data(as_text=True)
    admin.post("/models/%d/delete" % rows[0]["id"], data={"csrf": "t"})
    assert db.execute("SELECT COUNT(*) FROM models").fetchone()[0] == 1
    eid = db.execute("SELECT MAX(id) FROM audit").fetchone()[0]
    admin.post("/history", data={"csrf": "t", "id": eid})  # отмена удаления
    assert db.execute("SELECT COUNT(*) FROM models").fetchone()[0] == 2


def test_only_model_is_enough_and_validation(admin, db):
    sid = add_spool(admin)
    upload(admin, sid, (binary_stl(1), "a.stl"))
    assert db.execute("SELECT COUNT(*) FROM models").fetchone()[0] == 1
    assert db.execute("SELECT COUNT(*) FROM jobs").fetchone()[0] == 0  # без граммов/времени — не печать
    for data, name in [(b"not an stl at all", "x.stl"), (binary_stl(1), "x.obj"), (b"solid x\nendsolid x\n", "e.stl")]:
        r = upload(admin, sid, (data, name))
        assert any(kind == "error" for kind, _ in flashes(admin, r)), name
    assert db.execute("SELECT COUNT(*) FROM models").fetchone()[0] == 1


def test_local_viewer_and_cleanup(admin, db):
    assert 'class="stl-local"' in admin.get("/stl").get_data(as_text=True)
    sid = add_spool(admin)
    upload(admin, sid, (binary_stl(1), "a.stl"))
    f = db.execute("SELECT file FROM models").fetchone()[0]
    old = 0
    os.utime(os.path.join(filament.UPLOAD_DIR, f), (old, old))
    with filament.app.app_context():
        assert filament.cleanup()["uploads"] == 0  # модель используется — не трогаем


def box_stl():
    v = [(x, y, z) for x in (0, 40) for y in (0, 20) for z in (0, 10)]
    quads = [(0, 1, 3, 2), (4, 6, 7, 5), (0, 4, 5, 1), (2, 3, 7, 6), (0, 2, 6, 4), (1, 5, 7, 3)]
    tris = []
    for a, b, c, d in quads:
        tris += [(v[a], v[b], v[c]), (v[a], v[c], v[d])]
    return b"\0" * 80 + struct.pack("<I", len(tris)) + b"".join(struct.pack("<12fH", 0, 0, 0, *a, *b, *c, 0) for a, b, c in tris)


def test_preview_and_gallery(admin, anon, db):
    from PIL import Image
    sid = add_spool(admin, color_name="красный", color_hex="#d32f2f", material="PETG")
    upload(admin, sid, (box_stl(), "Коробка.stl"), (ASCII_STL, "tri.stl"), caption="коробка")
    admin.post("/spool/%d/prints" % sid, data={"csrf": "t", "caption": "фото кубика",
                                               "photos": [(__import__("conftest").jpeg(), "a.jpg")]},
               content_type="multipart/form-data")
    mid = db.execute("SELECT id FROM models WHERE name = 'Коробка.stl'").fetchone()[0]
    r = anon.get("/model/%d/preview.png" % mid)
    img = Image.open(io.BytesIO(r.data))
    assert r.status_code == 200 and img.size == (400, 400) and img.getbbox() is not None  # что-то нарисовано
    assert anon.get("/model/%d/preview.png" % mid).data == r.data  # из кэша
    pixels = img.get_flattened_data() if hasattr(img, "get_flattened_data") else img.getdata()
    red = sum(1 for p in pixels if p[3] and p[0] > p[2])  # модель — цвета катушки
    assert red > 1000
    g = anon.get("/gallery").get_data(as_text=True)
    assert "Коробка.stl" in g and "фото кубика" in g and "preview.png" in g
    assert "фото кубика" not in anon.get("/gallery?kind=model").get_data(as_text=True)
    assert "Коробка.stl" in anon.get("/gallery?q=коробка").get_data(as_text=True)
    assert "Коробка.stl" not in anon.get("/gallery?material=PLA").get_data(as_text=True)
    assert 'class="model-thumb"' in admin.get("/spool/%d" % sid).get_data(as_text=True)


def test_preview_not_built_by_guests(admin, anon, db):
    sid = add_spool(admin)
    upload(admin, sid, (box_stl(), "b.stl"))
    m = db.execute("SELECT id, file FROM models").fetchone()
    path = os.path.join(filament.THUMB_DIR, m["file"].rsplit(".", 1)[0] + ".png")
    assert os.path.exists(path)  # построено при загрузке
    os.remove(path)
    assert anon.get("/model/%d/preview.png" % m["id"]).status_code == 404  # гость не запускает расчёт
    assert admin.get("/model/%d/preview.png" % m["id"]).status_code == 200  # админ — да
    assert anon.get("/model/%d/preview.png" % m["id"]).status_code == 200  # дальше — из готового файла
