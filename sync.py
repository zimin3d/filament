"""Синхронизация телефон ↔ сервер.

Каждая синхронизируемая строка получает `uid` (одинаковый на обоих устройствах; числовые id у каждого
свои). Триггеры SQLite пишут в `sync_log` (таблица, uid, время) любое изменение — поэтому код страниц
о синхронизации ничего не знает. Сервер — центр: телефон отправляет свои изменения с прошлого раза,
сервер применяет их (если ту же строку на сервере не меняли позже) и возвращает текущее состояние
всего, что изменилось у него с прошлой синхронизации телефона. Ссылки между таблицами (spool_id,
job_id) передаются как uid. Файлы (фото, STL) — по имени, они уникальны (uuid).

Журнал (audit), входы, настройки (config.json) не синхронизируются — у каждого устройства свои.
"""
import os
import re
import sqlite3

# порядок важен: сначала те, на кого ссылаются
TABLES = ["spools", "profiles", "templates", "maintenance", "tares", "inbox", "jobs", "usage", "prints", "models"]
FKS = {
    "jobs": {"spool_id": "spools"},
    "usage": {"spool_id": "spools"},
    "prints": {"spool_id": "spools", "job_id": "jobs"},
    "models": {"spool_id": "spools", "job_id": "jobs"},
}
FILE_COLUMNS = {"spools": "photo", "prints": "photo", "models": "file"}
FILE_RE = re.compile(r"[0-9a-f]{32}\.(jpg|jpeg|png|webp|gif|stl)")
NEW_UID = "lower(hex(randomblob(16)))"
NOT_APPLYING = "(SELECT v FROM sync_state WHERE k = 'applying') IS NOT '1'"


def setup(db):
    """uid во всех таблицах, журнал изменений и триггеры. Повторный вызов ничего не ломает."""
    db.execute("CREATE TABLE IF NOT EXISTS sync_state (k TEXT PRIMARY KEY, v TEXT)")
    db.execute("CREATE TABLE IF NOT EXISTS sync_log (seq INTEGER PRIMARY KEY AUTOINCREMENT, "
               "tbl TEXT NOT NULL, uid TEXT NOT NULL, ts REAL NOT NULL)")
    db.execute("CREATE UNIQUE INDEX IF NOT EXISTS sync_log_row ON sync_log (tbl, uid)")
    db.execute("DELETE FROM sync_state WHERE k = 'applying'")  # если прошлое применение оборвалось
    # не «журнал пуст»: на телефоне он пуст и после загрузки всего с сервера
    fresh = db.execute("SELECT 1 FROM sync_state WHERE k = 'ready'").fetchone() is None
    for t in TABLES:
        if "uid" not in {r[1] for r in db.execute("PRAGMA table_info(%s)" % t)}:
            db.execute("ALTER TABLE %s ADD COLUMN uid TEXT" % t)
        db.execute("UPDATE %s SET uid = %s WHERE uid IS NULL" % (t, NEW_UID))
        db.execute("CREATE UNIQUE INDEX IF NOT EXISTS %s_uid ON %s (uid)" % (t, t))
        log = ("INSERT OR REPLACE INTO sync_log (tbl, uid, ts) VALUES ('%s', %%s, julianday('now'))" % t)
        # один триггер: сначала uid, потом журнал (порядок разных триггеров SQLite не гарантирует)
        db.execute("CREATE TRIGGER IF NOT EXISTS %s_sync_ins AFTER INSERT ON %s BEGIN "
                   "UPDATE %s SET uid = %s WHERE rowid = NEW.rowid AND uid IS NULL; "
                   "INSERT OR REPLACE INTO sync_log (tbl, uid, ts) SELECT '%s', uid, julianday('now') FROM %s "
                   "WHERE rowid = NEW.rowid AND %s; END" % (t, t, t, NEW_UID, t, t, NOT_APPLYING))
        db.execute("CREATE TRIGGER IF NOT EXISTS %s_sync_upd AFTER UPDATE ON %s WHEN %s AND NEW.uid IS NOT NULL "
                   "BEGIN %s; END" % (t, t, NOT_APPLYING, log % "NEW.uid"))
        db.execute("CREATE TRIGGER IF NOT EXISTS %s_sync_del AFTER DELETE ON %s WHEN %s AND OLD.uid IS NOT NULL "
                   "BEGIN %s; END" % (t, t, NOT_APPLYING, log % "OLD.uid"))
    if fresh:  # первая настройка: всё, что уже есть, считается изменённым — уйдёт при первой синхронизации
        for t in TABLES:
            db.execute("INSERT OR IGNORE INTO sync_log (tbl, uid, ts) SELECT '%s', uid, julianday('now') FROM %s"
                       % (t, t))
        set_state(db, "ready", 1)


def state(db, key, default=None):
    row = db.execute("SELECT v FROM sync_state WHERE k = ?", (key,)).fetchone()
    return row[0] if row else default


def set_state(db, key, value):
    db.execute("INSERT OR REPLACE INTO sync_state (k, v) VALUES (?, ?)", (key, str(value)))


def last_seq(db):
    return db.execute("SELECT COALESCE(MAX(seq), 0) FROM sync_log").fetchone()[0]


def _columns(db, t):
    return [r[1] for r in db.execute("PRAGMA table_info(%s)" % t) if r[1] not in ("id", "uid")]


def export(db, after):
    """Текущее состояние строк, изменённых после seq=after: [{t, u, ts, row|del}], родители раньше детей."""
    rows, dels = [], []
    order = {t: i for i, t in enumerate(TABLES)}
    entries = db.execute("SELECT tbl, uid, ts FROM sync_log WHERE seq > ? ORDER BY seq", (after,)).fetchall()
    for tbl, uid, ts in sorted(entries, key=lambda e: order.get(e[0], 99)):
        if tbl not in order:
            continue
        cur = db.execute("SELECT * FROM %s WHERE uid = ?" % tbl, (uid,))
        r = cur.fetchone()
        if r is None:
            dels.append({"t": tbl, "u": uid, "ts": ts, "del": 1})
            continue
        row = {d[0]: v for d, v in zip(cur.description, r) if d[0] not in ("id", "uid")}
        for col, parent in FKS.get(tbl, {}).items():
            if row.get(col) is not None:
                ref = db.execute("SELECT uid FROM %s WHERE id = ?" % parent, (row[col],)).fetchone()
                row[col] = ref[0] if ref else None
        rows.append({"t": tbl, "u": uid, "ts": ts, "row": row})
    dels.sort(key=lambda c: -order[c["t"]])  # дети удаляются раньше родителей
    return rows + dels


def apply(db, changes, newer_after=None, log=True, conflicts=None):
    """Применяет изменения с другого устройства. newer_after (на сервере) — seq, который телефон уже видел:
    строку, изменённую здесь позже этого и позже, чем на телефоне, не трогаем.
    log=False (на телефоне) — применённое не попадает в журнал, чтобы не уйти обратно.
    conflicts — список, куда добавить (таблица, uid, строка с другого устройства), где победило изменение здесь.
    Возвращает (применено, пропущено)."""
    order = {t: i for i, t in enumerate(TABLES)}
    done = skipped = 0
    if not log:
        set_state(db, "applying", 1)
    try:
        for c in changes:
            t, uid = c.get("t"), c.get("u")
            if t not in order or not isinstance(uid, str) or not re.fullmatch(r"[0-9a-f]{32}", uid):
                skipped += 1
                continue
            ts, row = c.get("ts") or 0, c.get("row") or {}
            if not isinstance(ts, (int, float)) or not isinstance(row, dict):
                skipped += 1
                continue
            if newer_after is not None:
                mine = db.execute("SELECT ts FROM sync_log WHERE tbl = ? AND uid = ? AND seq > ?",
                                  (t, uid, newer_after)).fetchone()
                if mine and mine[0] > ts:
                    skipped += 1
                    if conflicts is not None:
                        conflicts.append((t, uid, row))
                    continue
            if c.get("del"):
                db.execute("DELETE FROM %s WHERE uid = ?" % t, (uid,))
                done += 1
                continue
            row = _local_row(db, t, row)
            if row is None:  # ссылается на то, чего здесь нет (удалено)
                skipped += 1
                continue
            if db.execute("SELECT 1 FROM %s WHERE uid = ?" % t, (uid,)).fetchone():
                if row:
                    db.execute("UPDATE %s SET %s WHERE uid = ?" % (t, ", ".join("%s = ?" % k for k in row)),
                               list(row.values()) + [uid])
            else:
                cols = list(row) + ["uid"]
                sql = "INTO %s (%s) VALUES (%s)" % (t, ", ".join(cols), ", ".join("?" * len(cols)))
                try:
                    db.execute("INSERT " + sql, list(row.values()) + [uid])
                except sqlite3.IntegrityError:  # та же запись (имя шаблона, профиль…) заведена на обоих
                    db.execute("INSERT OR REPLACE " + sql, list(row.values()) + [uid])
            done += 1
    finally:
        if not log:
            db.execute("DELETE FROM sync_state WHERE k = 'applying'")
    return done, skipped


def _local_row(db, t, row):
    cols = set(_columns(db, t))
    out = {k: v for k, v in row.items() if k in cols and isinstance(v, (str, int, float, type(None)))}
    # попадают в style= и href= — те же проверки, что у формы
    if "color_hex" in out and not re.fullmatch(r"#[0-9a-fA-F]{6}", str(out["color_hex"])):
        del out["color_hex"]
    if out.get("purchase_url") and not str(out["purchase_url"]).startswith(("http://", "https://")):
        out["purchase_url"] = ""
    for col, parent in FKS.get(t, {}).items():
        if out.get(col) is not None:
            ref = db.execute("SELECT id FROM %s WHERE uid = ?" % parent, (str(out[col]),)).fetchone()
            if ref is None:
                if col == "spool_id":
                    return None
                out[col] = None
            else:
                out[col] = ref[0]
    return out


LABELS = {"spools": "катушка", "usage": "расход", "jobs": "печать", "prints": "фото", "models": "модель",
          "profiles": "настройки печати", "templates": "шаблон", "maintenance": "обслуживание", "tares": "вес катушки",
          "inbox": "печать с принтера"}


def describe(db, t, uid, row=None):
    """Человеческое имя строки для списка конфликтов на телефоне: «№12 PETG красный — расход».
    Вызывается на сервере; строки здесь уже нет (удалили) — имя из row, присланной телефоном."""
    def name(r):
        return " ".join(filter(None, ["№" + str(r[0]) if r[0] else "", r[1], r[2]]))
    gone = " (удалена на сервере)"
    row = row if isinstance(row, dict) else {}
    if t == "spools":
        r = db.execute("SELECT number, material, color_name FROM spools WHERE uid = ?", (uid,)).fetchone()
        return name(r) if r else (name((row.get("number"), row.get("material"), row.get("color_name"))) or "катушка") + gone
    if "spool_id" in {c[1] for c in db.execute("PRAGMA table_info(%s)" % t)}:
        r = db.execute("SELECT s.number, s.material, s.color_name FROM %s x JOIN spools s ON s.id = x.spool_id "
                       "WHERE x.uid = ?" % t, (uid,)).fetchone()
        if r:
            return "%s — %s" % (name(r), LABELS.get(t, t))
        spool = db.execute("SELECT number, material, color_name FROM spools WHERE uid = ?",
                           (str(row.get("spool_id") or ""),)).fetchone()
        return ("%s — %s" % (name(spool), LABELS.get(t, t)) if spool else LABELS.get(t, t)) + gone
    return LABELS.get(t, t)


def referenced_files(db):
    names = set()
    for t, col in FILE_COLUMNS.items():
        names.update(r[0] for r in db.execute("SELECT %s FROM %s WHERE %s IS NOT NULL" % (col, t, col)))
    return {n for n in names if FILE_RE.fullmatch(n or "")}


def missing_files(db, upload_dir):
    return sorted(n for n in referenced_files(db) if not os.path.exists(os.path.join(upload_dir, n)))
