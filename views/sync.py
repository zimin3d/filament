"""Синхронизация с телефоном: API на сервере, клиент и страница настроек — в приложении на телефоне.
Сам механизм (журнал изменений, применение) — в sync.py."""
import json
import os
import re
import secrets
import threading
import time
import urllib.error
import urllib.request
import uuid

from flask import abort, flash, jsonify, redirect, render_template, request, send_from_directory, session, url_for

from core import *  # noqa: F401,F403
from logic import *  # noqa: F401,F403
import sync

SYNC_TIMEOUT = 60
RELEASES_URL = "https://github.com/zimin3d/filament/releases/latest"  # APK для Android
RELEASES_API = "https://api.github.com/repos/zimin3d/filament/releases/latest"
APK_PREFIX = "https://github.com/zimin3d/filament/releases/download/"  # только отсюда приложение скачает APK


def version_key(v):
    """'v0.12' → (0, 12); всё нечисловое — 0 (сборки без метки, 'dev', считаются старыми)."""
    return tuple(int(p) if p.isdigit() else 0 for p in re.findall(r"\d+|[^\d.]+", (v or "").lstrip("vV"))
                 if p != ".") or (0,)


def check_app_update(fetch=None):
    """Последний выпуск на GitHub: {'version', 'apk', 'newer'}; исключение — если не удалось спросить."""
    if fetch is None:
        def fetch(url):
            req = urllib.request.Request(url, headers={"Accept": "application/vnd.github+json"})
            with urllib.request.urlopen(req, timeout=20) as r:
                return r.read()
    data = json.loads(fetch(RELEASES_API))
    tag = str(data.get("tag_name") or "")
    apk = next((a.get("browser_download_url") for a in data.get("assets") or []
                if str(a.get("name", "")).endswith(".apk")), None)
    if apk and not apk.startswith(APK_PREFIX):
        apk = None
    current = os.environ.get("FILAMENT_APP_VERSION", "")
    return {"version": tag, "apk": apk, "current": current,
            "newer": bool(apk) and version_key(tag) > version_key(current)}
_running = threading.Lock()  # автосинхронизация и кнопка не должны идти одновременно


def sync_authorized():
    token = load_config().get("sync_token")
    auth = request.headers.get("Authorization", "")
    return bool(token) and secrets.compare_digest(auth, "Bearer " + token)


# --- сервер -----------------------------------------------------------------

@app.route("/api/sync", methods=["POST"])
def api_sync():
    """Телефон присылает свои изменения, получает изменения сервера с прошлого раза."""
    if PHONE or not sync_authorized():
        abort(403)
    data = request.get_json(silent=True) or {}
    try:
        after = int(data.get("after") or 0)
        changes = data.get("changes") or []
        files = [f for f in data.get("files") or [] if isinstance(f, str) and sync.FILE_RE.fullmatch(f)]
        if not isinstance(changes, list):
            raise ValueError
    except (TypeError, ValueError):
        return jsonify(ok=False, error="неверный запрос"), 400
    db = get_db()
    done, skipped = sync.apply(db, [c for c in changes if isinstance(c, dict)], newer_after=after)
    db.commit()
    need = sorted(f for f in set(files) & sync.referenced_files(db) if not os.path.exists(os.path.join(UPLOAD_DIR, f)))
    return jsonify(ok=True, seq=sync.last_seq(db), changes=sync.export(db, after), need=need,
                   applied=done, skipped=skipped)


@app.route("/api/sync/file/<name>", methods=["GET", "PUT"])
def api_sync_file(name):
    if PHONE or not sync_authorized() or not sync.FILE_RE.fullmatch(name):
        abort(403)
    path = os.path.join(UPLOAD_DIR, name)
    if request.method == "GET":
        return send_from_directory(UPLOAD_DIR, name)
    # принимаем только файлы, на которые ссылаются записи и которых ещё нет
    if os.path.exists(path) or name not in sync.referenced_files(get_db()):
        return jsonify(ok=True, skipped=True)
    os.makedirs(UPLOAD_DIR, exist_ok=True)
    tmp = "%s.%s.tmp" % (path, uuid.uuid4().hex[:8])
    with open(tmp, "wb") as f:
        f.write(request.get_data())
    os.replace(tmp, path)
    return jsonify(ok=True)


# --- телефон: клиент --------------------------------------------------------

def _request(url, token, data=None, method=None, raw=False):
    body = None
    headers = {"Authorization": "Bearer " + token}
    if data is not None and not raw:
        body = json.dumps(data).encode()
        headers["Content-Type"] = "application/json"
    elif raw:
        body = data
        headers["Content-Type"] = "application/octet-stream"
    req = urllib.request.Request(url, data=body, headers=headers, method=method)
    with urllib.request.urlopen(req, timeout=SYNC_TIMEOUT) as r:
        return r.read()


def run_sync(transport=None):
    """Один обмен с сервером. transport(path, data=None, method=None, raw=False) -> bytes; по умолчанию HTTP.
    Возвращает словарь-итог; исключение — если сервер недоступен или отказал."""
    if not _running.acquire(timeout=SYNC_TIMEOUT * 2):
        raise ValueError("Синхронизация уже идёт.")
    try:
        return _run_sync(transport)
    finally:
        _running.release()


def _run_sync(transport):
    cfg = load_config()
    url, token = (cfg.get("sync_url") or "").rstrip("/") + "/", cfg.get("sync_token") or ""
    if transport is None:
        if not url.startswith(("http://", "https://")) or not token:
            raise ValueError("Не настроено: адрес сервера и ключ.")
        transport = lambda path, data=None, method=None, raw=False: _request(url + path, token, data, method, raw)  # noqa: E731
    db = get_db()
    after = int(sync.state(db, "server_seq", 0))
    if after == 0 and not db.execute("SELECT 1 FROM spools LIMIT 1").fetchone():
        # новое устройство: свои стартовые профили/регламент не нужны — всё придёт с сервера
        sync.set_state(db, "applying", 1)
        for t in reversed(sync.TABLES):
            db.execute("DELETE FROM %s" % t)
        db.execute("DELETE FROM sync_log")
        db.execute("DELETE FROM sync_state WHERE k = 'applying'")
        db.commit()
    local_after = int(sync.state(db, "local_seq", 0))
    mine = sync.export(db, local_after)
    local_mark = sync.last_seq(db)
    have = sorted(f for f in os.listdir(UPLOAD_DIR) if sync.FILE_RE.fullmatch(f)) if os.path.isdir(UPLOAD_DIR) else []
    resp = json.loads(transport("api/sync", {"after": after, "changes": mine, "files": have}))
    if not resp.get("ok"):
        raise ValueError(resp.get("error") or "сервер отказал")
    got, _ = sync.apply(db, resp.get("changes") or [], log=False)
    sync.set_state(db, "server_seq", int(resp["seq"]))
    sync.set_state(db, "local_seq", local_mark)
    db.commit()
    sent_files = got_files = 0
    for name in resp.get("need") or []:
        if sync.FILE_RE.fullmatch(name) and os.path.exists(os.path.join(UPLOAD_DIR, name)):
            with open(os.path.join(UPLOAD_DIR, name), "rb") as f:
                transport("api/sync/file/" + name, f.read(), "PUT", True)
            sent_files += 1
    os.makedirs(UPLOAD_DIR, exist_ok=True)
    for name in sync.missing_files(db, UPLOAD_DIR):
        try:
            content = transport("api/sync/file/" + name)
        except urllib.error.HTTPError:
            continue  # на сервере тоже нет — пропускаем
        tmp = os.path.join(UPLOAD_DIR, "%s.%s.tmp" % (name, uuid.uuid4().hex[:8]))
        with open(tmp, "wb") as f:
            f.write(content)
        os.replace(tmp, os.path.join(UPLOAD_DIR, name))
        got_files += 1
    result = {"sent": len(mine), "received": got, "sent_files": sent_files, "got_files": got_files,
              "at": time.strftime("%Y-%m-%d %H:%M")}
    sync.set_state(db, "last_result", json.dumps(result))
    db.commit()
    return result


def sync_summary(r):
    return "Отправлено изменений: %d, получено: %d, фото/файлов: ↑%d ↓%d." % (
        r["sent"], r["received"], r["sent_files"], r["got_files"])


# --- страницы ---------------------------------------------------------------

@app.route("/app-update/install")
@admin_required
def app_update_install():
    """Эту ссылку перехватывает окно приложения (открывает APK в браузере). Сюда попадаем,
    только если открыли её не из приложения."""
    flash("Обновление скачивается только из приложения на телефоне.", "error")
    return redirect(url_for("sync_page"))


@app.route("/phone-start")
def phone_start():
    """Приложение на телефоне открывает эту страницу с ключом запуска — так входит только оно."""
    key = os.environ.get("FILAMENT_PHONE_KEY", "")
    if not PHONE or not key or not secrets.compare_digest(request.args.get("key", ""), key):
        abort(403)
    session.clear()
    session.update(admin=True, phone=True, user="телефон")
    return redirect(url_for("index"))


@app.route("/sync", methods=["GET", "POST"])
@admin_required
def sync_page():
    db = get_db()
    cfg = load_config()
    if request.method == "POST":
        action = request.form.get("action")
        if PHONE and action == "save":
            url = request.form.get("url", "").strip()
            if url and not url.startswith(("http://", "https://")):
                flash("Адрес должен начинаться с https://", "error")
                return redirect(url_for("sync_page"))
            if url != cfg.get("sync_url"):  # другой сервер — начинаем обмен заново
                sync.set_state(db, "server_seq", 0)
                sync.set_state(db, "local_seq", 0)
                db.commit()
            cfg["sync_url"] = url
            if request.form.get("token", "").strip():
                cfg["sync_token"] = request.form["token"].strip()
            save_config(cfg)
            flash("Сохранено.", "ok")
        elif PHONE and action == "run":
            try:
                flash("Синхронизировано. " + sync_summary(run_sync()), "ok")
            except (OSError, ValueError, KeyError) as e:
                flash("Не удалось: %s" % (getattr(e, "reason", None) or e), "error")
        elif PHONE and action == "update":
            try:
                upd = check_app_update()
            except (OSError, ValueError) as e:
                flash("Не удалось проверить: %s" % (getattr(e, "reason", None) or e), "error")
                return redirect(url_for("sync_page"))
            if not upd["newer"]:
                flash("Установлена последняя версия (%s)." % (upd["current"] or "?"), "ok")
                return redirect(url_for("sync_page"))
            return render_sync(db, cfg, update=upd)
        elif not PHONE and action == "token":
            cfg["sync_token"] = secrets.token_urlsafe(24)
            save_config(cfg)
            flash("Новый ключ создан — старый больше не действует.", "ok")
            return render_template("sync.html", phone=False, token=cfg["sync_token"], has_token=True,
                                   releases_url=RELEASES_URL)
        return redirect(url_for("sync_page"))
    return render_sync(db, cfg)


def render_sync(db, cfg, update=None):
    last = sync.state(db, "last_result")
    return render_template("sync.html", phone=PHONE, cfg=cfg, releases_url=RELEASES_URL, update=update,
                           app_version=os.environ.get("FILAMENT_APP_VERSION", ""), last=json.loads(last) if last else None,
                           has_token=bool(cfg.get("sync_token")), token=None,
                           pending=db.execute("SELECT COUNT(*) FROM sync_log WHERE seq > ?",
                                              (int(sync.state(db, "local_seq", 0)),)).fetchone()[0],
                           summary=sync_summary)
