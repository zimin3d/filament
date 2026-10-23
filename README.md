# Учёт филамента

Веб-сервис для учёта катушек пластика для 3D-принтера: производитель, тип, цвет,
фото, ссылка на покупку, даты покупки и вскрытия, цена, остаток и его стоимость.

Без входа всё доступно только для чтения; изменять может один администратор.

## Структура

- `app.py` — точка входа (`gunicorn app:app`) и команды `app.py …`
- `core.py` — пути, схема БД, конфигурация, Flask-приложение, вход/CSRF/CSP, фильтры шаблонов
- `logic.py` — предметная логика: фильтры списка, цвета, журнал с отменой, расход, фото/модели/превью
- `views/` — страницы по разделам: `spools`, `reports`, `printer`, `models`, `importing`, `misc`
- `cli.py` — команды обслуживания (пароли, sync, notify, cleanup, offsite)
- `slicer.py`, `stlpreview.py`, `labelpng.py`, `notify.py` — разбор файлов слайсера, превью STL, PNG-этикетки, Telegram
- `importer.py` — импорт из Google Таблиц (по ссылке) и CSV: страница «Импорт» у администратора
- `templates/`, `static/` — интерфейс
- `data/` — база `filament.db`, фото `uploads/`, временные файлы импорта `import/`, `config.json` (секрет сессий и хэш пароля). **Бэкапить эту папку.**
- `deploy/` — unit systemd, конфиг nginx, скрипт установки; настройки сервера (адрес, сайт nginx) — в `deploy/site.env` (образец `site.env.example`, в git не входит)

## Окружение

Python 3.12 из uv (без sudo, лежит в `~/.local/share/uv/python`), зависимости закреплены в `requirements.txt`:

```bash
~/.local/uv-tool/bin/uv venv --python 3.12 venv
VIRTUAL_ENV=$PWD/venv ~/.local/uv-tool/bin/uv pip install -r requirements-dev.txt
```

## Команды

```bash
venv/bin/python app.py set-password [имя]  # задать/сменить пароль администратора (по умолчанию admin)
venv/bin/python app.py add-admin имя        # ещё один администратор; remove-admin имя, list-admins
venv/bin/python app.py set-viewer-password   # закрыть просмотр паролем (clear-viewer-password — открыть)
venv/bin/python app.py set-telegram     # подключить Telegram-бота (токен от @BotFather)
venv/bin/python app.py notify           # сводка в Telegram, если список изменился (--force — всегда)
venv/bin/python app.py cleanup          # убрать неиспользуемые фото (--dry-run — только показать)
venv/bin/python app.py sync             # подтянуть из сохранённой таблицы новые катушки и фото
venv/bin/python app.py                  # локальный запуск для разработки на 127.0.0.1:8080
cp deploy/site.env.example deploy/site.env  # адрес и сайт nginx этого сервера — поправить
sudo bash deploy/install.sh             # установить сервис и подключить к nginx
sudo systemctl restart filament         # перезапустить после изменения кода
journalctl -u filament -f               # логи
```

Адрес — `FILAMENT_URL` из `deploy/site.env`.

## Тесты

```bash
venv/bin/pytest -q            # ~секунды, без сети
```

## Принтер (Creality K2 / Klipper)

Сервер не видит домашнюю сеть, поэтому печати присылает агент `agent/k2_agent.py`
(только стандартный Python 3), запущенный на любом компьютере рядом с принтером:

```bash
venv/bin/python app.py printer-token                          # на сервере: выдать ключ
python3 k2_agent.py --server https://example.com/filament --printer 192.168.1.50 --token КЛЮЧ  # дома: работает постоянно
```

Однокрасочная печать списывается с катушки, отмеченной «🖨 Поставил на принтер»;
многоцветные — во «Входящие» (кнопка в шапке), там выбираются катушки.

## Приложение для Android

Тот же код работает на телефоне без сети: Python (Chaquopy) запускает приложение на 127.0.0.1,
окно приложения — WebView. Данные — на телефоне; синхронизация с сервером — на странице «Синхронизация»
(кнопка и автоматически раз в 15 минут, пока приложение открыто).

- `sync.py` — журнал изменений (триггеры SQLite, `uid` у строк) и применение; `views/sync.py` — API
  `/api/sync` на сервере и клиент на телефоне. Сервер главный; при правке одной строки на обоих
  устройствах остаётся более поздняя. Журнал действий (отмена) у каждого устройства свой.
- `android/` — проект Gradle; `android/launcher.py` — запуск на телефоне; `android/prepare.sh` копирует код в проект.
- Сборка — GitHub Actions (`.github/workflows/android.yml`): метка `v*` → APK в Releases.
  Нужны секреты репозитория `KEYSTORE_B64` и `KEYSTORE_PASSWORD` (постоянный ключ подписи —
  иначе обновление не встанет поверх установленного).
- Настройка: на сайте «Синхронизация» → «Создать ключ»; в приложении — адрес сайта и этот ключ.

## Копия вне сервера

`venv/bin/python app.py offsite-setup` — пароль шифрования (сохранить в менеджере паролей!) и куда слать:
WebDAV (Яндекс Диск: `https://webdav.yandex.ru/папка`, пароль приложения) — каждую ночь, 7 дневных по кругу + месячные;
Telegram — раз в неделю (нужен `set-telegram`). Восстановление: `gpg -d filament-daily-mon.tar.gz.gpg > backup.tar.gz`,
затем распаковать в `data/`.
