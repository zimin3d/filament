"""Запуск приложения на телефоне (вызывается из MainActivity через Chaquopy).

Тот же Flask, что и на сервере, слушает только 127.0.0.1 на случайном порту. Вход — по ключу
запуска, который знает только окно приложения. Раз в 15 минут, пока приложение открыто, —
синхронизация с сервером (если она настроена на странице «Синхронизация»).
"""
import os
import secrets
import threading
import time

SYNC_EVERY = 15 * 60
_url = None
_lock = threading.Lock()


def start(data_dir):
    """Запускает сервер один раз; возвращает адрес первой страницы (с ключом входа)."""
    global _url
    with _lock:
        if _url:
            return _url
        key = secrets.token_urlsafe(24)
        os.environ.update(FILAMENT_DATA=data_dir, FILAMENT_MODE="phone", FILAMENT_PHONE_KEY=key,
                          FILAMENT_SECURE_COOKIE="0")
        import app  # noqa: E402 — только после переменных окружения
        from werkzeug.serving import make_server

        srv = make_server("127.0.0.1", 0, app.app, threaded=True)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        threading.Thread(target=_auto_sync, args=(app,), daemon=True).start()
        _url = "http://127.0.0.1:%d/phone-start?key=%s" % (srv.server_port, key)
        return _url


def _auto_sync(app):
    time.sleep(5)
    while True:
        cfg = app.load_config()
        if cfg.get("sync_url") and cfg.get("sync_token"):
            try:
                with app.app.test_request_context():
                    app.views.sync.run_sync()
            except Exception as e:  # нет сети и т.п. — попробуем позже
                print("sync:", e)
        time.sleep(SYNC_EVERY)
