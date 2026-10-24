"""Служебное: вход/выход, PWA (сервис-воркер, манифест, офлайн), ошибки."""
import os

from flask import flash, redirect, render_template, request, send_from_directory, session, url_for

from werkzeug.security import check_password_hash
from core import *  # noqa: F401,F403
from logic import *  # noqa: F401,F403

@app.route("/sw.js")
def service_worker():
    """Отдаётся из корня приложения, чтобы охватывать все страницы под /filament/."""
    resp = send_from_directory(os.path.join(BASE_DIR, "static"), "sw.js", max_age=0)
    resp.headers["Cache-Control"] = "no-cache"
    return resp


@app.route("/manifest.webmanifest")
def manifest():
    return send_from_directory(os.path.join(BASE_DIR, "static"), "manifest.webmanifest",
                               mimetype="application/manifest+json", max_age=3600)


@app.route("/offline")
def offline():
    return render_template("offline.html")


@app.route("/login", methods=["GET", "POST"])
def login():
    cfg = load_config()
    users = admin_users(cfg)
    if "password" in request.args:
        # пароль в адресе попадает в логи и историю браузера — не принимаем такой вход
        flash("Пароль нельзя передавать в адресе. Введите его в форму.", "error")
        return redirect(url_for("login"))
    ip = request.remote_addr or ""
    if request.method == "POST" and login_blocked(ip):
        flash("Слишком много неудачных попыток. Подождите %d минут." % LOGIN_WINDOW_MIN, "error")
        return render_template("login.html", viewer_mode=bool(cfg.get("viewer_password_hash")),
                               many=len(users) > 1), 429
    if request.method == "POST":
        password = clean_password(request.form.get("password", ""))
        name = request.form.get("user", "").strip().lower()
        viewer_hash = cfg.get("viewer_password_hash")
        role = user = None
        candidates = [(name, users[name])] if name in users else ([] if name else list(users.items()))
        version = None
        for uname, pw_hash in candidates:
            if check_password_hash(pw_hash, password):
                role, user, version = "admin", uname, pw_version(pw_hash)
                break
        if role is None and viewer_hash and check_password_hash(viewer_hash, password):
            role, version = "viewer", pw_version(viewer_hash)
        if role:
            session.clear()
            session[role] = True
            if user:
                session["user"] = user
            session["pwv"] = version
            session.permanent = True
            nxt = request.args.get("next", "")
            return redirect(request.script_root + nxt if safe_next(nxt) else url_for("index"))
        login_failed(ip)
        flash("Неверное имя или пароль." if len(users) > 1 else "Неверный пароль." if users
              else "Пароль администратора ещё не задан.", "error")
    return render_template("login.html", viewer_mode=bool(cfg.get("viewer_password_hash")), many=len(users) > 1)


@app.route("/logout", methods=["POST"])
def logout():
    session.clear()
    return redirect(url_for("index"))


@app.errorhandler(413)
def too_large(e):
    flash("Файл слишком большой (максимум 15 МБ).", "error")
    return redirect(request.referrer or url_for("index"))


@app.route("/help")
def help_page():
    from views.sync import RELEASES_URL
    return render_template("help.html", releases_url=RELEASES_URL)


THEMES = {"": "Тема: как в системе", "dark": "Тема: тёмная", "light": "Тема: светлая"}


@app.route("/theme")
def theme():
    """Переключение темы по кругу: как в системе → тёмная → светлая. Хранится в cookie на год."""
    order = list(THEMES)
    cur = request.cookies.get("theme", "")
    nxt = order[(order.index(cur) + 1) % len(order)] if cur in order else "dark"
    back = request.args.get("next", "")
    if not safe_next(back):
        back = url_for("index")
    resp = redirect(back)
    resp.set_cookie("theme", nxt, max_age=365 * 86400, samesite="Lax", httponly=True)
    return resp
