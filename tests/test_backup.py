import os
import shutil
import sys
import tarfile

import app as filament
from conftest import add_spool, jpeg

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "deploy"))
import verify_backup  # noqa: E402


def make_archive(path, drop_photo=False):
    with tarfile.open(path, "w:gz") as tar:
        tar.add(filament.DB_PATH, "filament.db")
        tar.add(filament.CONFIG_PATH, "config.json")
        for name in os.listdir(filament.UPLOAD_DIR):
            if not drop_photo:
                tar.add(os.path.join(filament.UPLOAD_DIR, name), "uploads/" + name)


def test_verify_ok_and_missing_photo(admin, tmp_path):
    add_spool(admin, photo=(jpeg(), "a.jpg"))
    good, bad = str(tmp_path / "good.tar.gz"), str(tmp_path / "bad.tar.gz")
    make_archive(good)
    make_archive(bad, drop_photo=True)
    assert verify_backup.verify(good) == []
    problems = verify_backup.verify(bad)
    assert problems and "нет файлов фото" in problems[0]


def test_cleanup_keeps_undoable_and_live(admin, db, monkeypatch):
    import time
    keep = add_spool(admin, photo=(jpeg(), "a.jpg"))
    gone = add_spool(admin, photo=(jpeg(), "b.jpg"))
    keep_photo = db.execute("SELECT photo FROM spools WHERE id = ?", (keep,)).fetchone()[0]
    gone_photo = db.execute("SELECT photo FROM spools WHERE id = ?", (gone,)).fetchone()[0]
    admin.get("/thumbs/" + gone_photo)
    admin.post("/spool/%d/delete" % gone, data={"csrf": "t"})
    orphan = os.path.join(filament.UPLOAD_DIR, "0" * 32 + ".jpg")
    shutil.copyfile(os.path.join(filament.UPLOAD_DIR, keep_photo), orphan)
    old = time.time() - 7200
    for n in os.listdir(filament.UPLOAD_DIR):
        os.utime(os.path.join(filament.UPLOAD_DIR, n), (old, old))
    with filament.app.app_context():
        r = filament.cleanup()
    assert r["uploads"] == 1 and not os.path.exists(orphan)  # сирота удалена
    assert os.path.exists(os.path.join(filament.UPLOAD_DIR, gone_photo))  # удаление ещё можно отменить
    # после того как отмена стала невозможна — удаляется и фото, и миниатюра
    db.execute("UPDATE audit SET ts = '2000-01-01 00:00:00'")
    db.commit()
    with filament.app.app_context():
        r = filament.cleanup()
    assert r["uploads"] == 1 and r["thumbs"] == 1
    assert os.path.exists(os.path.join(filament.UPLOAD_DIR, keep_photo))
