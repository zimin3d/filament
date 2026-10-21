from conftest import add_spool, jpeg


def undo_last(admin, db, spool_id):
    eid = db.execute("SELECT MAX(id) FROM audit WHERE spool_id = ? AND undone = 0", (spool_id,)).fetchone()[0]
    return admin.post("/history", data={"csrf": "t", "id": eid})


def test_undo_edit_weigh_print(admin, db):
    sid = add_spool(admin, price="1000")
    admin.post("/spool/%d/weigh" % sid, data={"csrf": "t", "gross": "1000", "tare": "250"})
    admin.post("/spool/%d/prints" % sid, data={"csrf": "t", "caption": "X", "grams": "50"})
    assert db.execute("SELECT weight_left FROM spools").fetchone()[0] == 700
    html = admin.get("/history").get_data(as_text=True)
    assert "Печать" in html and "Взвешена" in html and "↶ Отменить" in html
    undo_last(admin, db, sid)  # печать
    assert db.execute("SELECT weight_left FROM spools").fetchone()[0] == 750
    assert db.execute("SELECT COUNT(*) FROM jobs").fetchone()[0] == 0
    undo_last(admin, db, sid)  # взвешивание
    assert tuple(db.execute("SELECT weight_left, opened_date FROM spools").fetchone()) == (1000, None)
    assert db.execute("SELECT COUNT(*) FROM usage").fetchone()[0] == 0


def test_only_last_action_undoable(admin, db):
    sid = add_spool(admin)
    admin.post("/spool/%d/dried" % sid, data={"csrf": "t"})
    admin.post("/spool/%d/open" % sid, data={"csrf": "t"})
    first = db.execute("SELECT MIN(id) FROM audit WHERE action = 'Высушена'").fetchone()[0]
    r = admin.post("/history", data={"csrf": "t", "id": first})
    assert "только последнее" in admin.get(r.location).get_data(as_text=True)
    assert db.execute("SELECT dried_date FROM spools").fetchone()[0]


def test_undo_delete_restores_everything(admin, db):
    sid = add_spool(admin, number="9", photo=(jpeg(), "a.jpg"))
    admin.post("/spool/%d/prints" % sid, data={"csrf": "t", "grams": "10", "photos": [(jpeg(), "p.jpg")]},
               content_type="multipart/form-data")
    admin.post("/spool/%d/delete" % sid, data={"csrf": "t"})
    assert db.execute("SELECT COUNT(*) FROM spools").fetchone()[0] == 0
    undo_last(admin, db, sid)
    row = db.execute("SELECT * FROM spools").fetchone()
    assert (row["id"], row["number"], row["weight_left"]) == (sid, "9", 990)
    assert db.execute("SELECT COUNT(*) FROM jobs").fetchone()[0] == 1
    assert db.execute("SELECT COUNT(*) FROM prints").fetchone()[0] == 1
    assert admin.get("/uploads/" + row["photo"]).status_code == 200
    assert admin.get("/spool/%d" % sid).status_code == 200


def test_undo_create(admin, db):
    sid = add_spool(admin)
    undo_last(admin, db, sid)
    assert db.execute("SELECT COUNT(*) FROM spools").fetchone()[0] == 0
