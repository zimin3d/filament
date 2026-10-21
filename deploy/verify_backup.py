"""Проверка резервной копии: распаковать во временную папку, проверить базу и фото,
открыть приложение на этих данных. Код возврата 0 — копия годная.

    venv/bin/python deploy/verify_backup.py [архив.tar.gz]   # без аргумента — самый свежий
"""
import glob
import os
import sqlite3
import subprocess
import sys
import tarfile
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BACKUPS = os.environ.get("FILAMENT_BACKUPS", os.path.expanduser("~/filament-backups"))

CHECK_APP = """
import app, sys
c = app.app.test_client()
with c.session_transaction() as s:
    name, pw_hash = next(iter(app.admin_users().items()))
    s.update(admin=True, user=name, pwv=app.pw_version(pw_hash))
ids = [r[0] for r in app.sqlite3.connect(app.DB_PATH).execute("SELECT id FROM spools")]
pages = ["/", "/?status=all", "/stock", "/jobs", "/history", "/export.csv"] + ["/spool/%d" % i for i in ids]
bad = [(p, c.get(p).status_code) for p in pages]
bad = [b for b in bad if b[1] != 200]
print("страниц проверено: %d" % len(pages))
sys.exit("ошибки страниц: %s" % bad if bad else 0)
"""


def verify(archive):
    problems = []
    with tempfile.TemporaryDirectory(prefix="filament-verify-") as tmp:
        with tarfile.open(archive) as tar:
            names = tar.getnames()
            for n in names:
                if n.startswith("/") or ".." in n.split("/"):
                    return ["подозрительный путь в архиве: %s" % n]
            if hasattr(tarfile, "data_filter"):  # Python 3.12+: запрет ссылок/прав/путей наружу
                tar.extractall(tmp, filter="data")
            else:
                tar.extractall(tmp)
        for need in ("filament.db", "config.json"):
            if need not in names:
                problems.append("нет %s" % need)
        if problems:
            return problems
        db = sqlite3.connect(os.path.join(tmp, "filament.db"))
        ok = db.execute("PRAGMA integrity_check").fetchone()[0]
        if ok != "ok":
            problems.append("integrity_check: %s" % ok)
        spools = db.execute("SELECT COUNT(*) FROM spools").fetchone()[0]
        tables = {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
        sql = "SELECT photo FROM spools WHERE photo IS NOT NULL"
        sql += " UNION SELECT photo FROM prints" if "prints" in tables else ""
        sql += " UNION SELECT file FROM models" if "models" in tables else ""  # таблицы из новых версий
        photos = {r[0] for r in db.execute(sql)}
        missing = [p for p in photos if not os.path.exists(os.path.join(tmp, "uploads", p))]
        if missing:
            problems.append("нет файлов фото: %d (например %s)" % (len(missing), missing[0]))
        db.close()
        env = dict(os.environ, FILAMENT_DATA=tmp)
        r = subprocess.run([sys.executable, "-c", CHECK_APP], cwd=ROOT, env=env,
                           stdout=subprocess.PIPE, stderr=subprocess.STDOUT, universal_newlines=True)
        if r.returncode:
            problems.append("приложение на копии: %s" % r.stdout.strip().splitlines()[-1:])
        print("%s: катушек %d, фото %d, %s" % (os.path.basename(archive), spools, len(photos),
                                               r.stdout.strip().splitlines()[-1] if r.stdout.strip() else ""))
    return problems


def main():
    archive = sys.argv[1] if len(sys.argv) > 1 else max(glob.glob(os.path.join(BACKUPS, "filament-*.tar.gz")),
                                                         key=os.path.getmtime, default=None)
    if not archive:
        sys.exit("Копий нет в %s" % BACKUPS)
    problems = verify(archive)
    if problems:
        msg = "⚠️ Резервная копия НЕ прошла проверку (%s):\n%s" % (os.path.basename(archive), "\n".join(problems))
        print(msg)
        try:  # сообщить в Telegram, если настроен
            sys.path.insert(0, ROOT)
            os.environ.setdefault("FILAMENT_DATA", os.path.join(ROOT, "data"))
            import app
            app.telegram(app.notify.esc(msg))
        except Exception as e:  # уведомление — не главное
            print("Telegram: %s" % e)
        sys.exit(1)
    print("OK")


if __name__ == "__main__":
    main()
