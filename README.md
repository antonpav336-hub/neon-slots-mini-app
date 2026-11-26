# NEON SLOTS — Telegram Mini App

Тестовый проект: веб-интерфейс Mini App, FastAPI API и Telegram-бот-компаньон.

## Файлы
- `static/index.html` — адаптивный интерфейс с анимацией барабанов.
- `app.py` — API, проверка Telegram WebApp `initData`, SQLite, игровая логика, создание Stars invoice и заявки на вывод.
- `bot.py` — Telegram-бот, обработка pre-checkout и успешной оплаты Stars, команды администратора.
- `requirements.txt` — зависимости.
- `.env.example` — шаблон переменных окружения.

## Важно
- Не загружайте `.env`, токен бота или базу данных в GitHub. Файл `.gitignore` исключает их.
- Если ранее токен публиковался в переписке или где-либо ещё, отзовите его через @BotFather командой `/revoke` и используйте новый.
- Это технический тестовый шаблон. Не включайте реальные денежные выплаты и не принимайте реальные платежи, пока не проверили применимое законодательство, возрастные ограничения и правила Telegram.
- Одобрение заявки на вывод меняет только её статус. Автоматического перевода денег код не выполняет.
- Telegram Mini Apps требуют публичный HTTPS URL.
- Для реального запуска нужны сервер, HTTPS-домен и отдельные процессы для API и бота.

## Установка
Требуется Python 3.11+.

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
```

Заполните `.env`:
- `BOT_TOKEN` — новый токен от @BotFather.
- `ADMIN_ID` — числовой Telegram ID администратора (команда бота `/myid`).
- `WEBAPP_URL` — публичный HTTPS URL сайта.
- `DB_PATH` — абсолютный путь к общей SQLite базе, одинаковый для API и бота.

Запустите API и бота в двух отдельных терминалах из корня проекта:

```bash
set -a; source .env; set +a
uvicorn app:app --host 0.0.0.0 --port 8000
```

```bash
set -a; source .env; set +a
python bot.py
```

Настройте HTTPS reverse proxy (например, Caddy или Nginx) на порт 8000. Укажите тот же HTTPS URL в `WEBAPP_URL` и в настройках Mini App через @BotFather.

## Команды бота
- `/start` — открыть Mini App
- `/balance` — баланс
- `/myid` — Telegram ID
- `/withdrawals` — ожидающие заявки (администратор)
- `/approve ID` — одобрить заявку (без автоматической выплаты)
- `/reject ID` — отклонить заявку и вернуть монеты
- `/terms`, `/support` — правила и поддержка

## Проверка
Сначала проверьте приложение в Telegram test environment и используйте только тестовые сценарии. Перед публичным запуском добавьте rate limiting, мониторинг, резервное копирование базы и полноценные правила/политику конфиденциальности.
