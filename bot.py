import os
import httpx
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, WebAppInfo
from telegram.ext import (
    ApplicationBuilder, CommandHandler, PreCheckoutQueryHandler,
    MessageHandler, ContextTypes, filters
)

TOKEN = os.getenv("BOT_TOKEN", "").strip()
ADMIN_ID = int(os.getenv("ADMIN_ID", "0"))
WEBAPP_URL = os.getenv("WEBAPP_URL", "https://neon-slots-mini-app.onrender.com").strip().rstrip("/") + "/?v=neon-mines-20261127"
API_URL = os.getenv("API_URL", "https://neon-slots-mini-app.onrender.com").rstrip("/")
PAYMENT_SECRET = os.getenv("PAYMENT_SECRET", "").strip()
TEST_SERVER = os.getenv("TELEGRAM_TEST_SERVER", "1") == "1"

def internal_headers():
    return {"X-Internal-Secret": PAYMENT_SECRET}

async def api_get(path):
    async with httpx.AsyncClient(timeout=60) as client:
        response = await client.get(API_URL + path, headers=internal_headers())
        response.raise_for_status()
        return response.json()

async def api_post(path, data, timeout=20):
    async with httpx.AsyncClient(timeout=timeout) as client:
        response = await client.post(API_URL + path, json=data, headers=internal_headers())
        response.raise_for_status()
        return response.json()

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    try:
        result = await api_post("/api/internal/user", {
            "user_id": user.id, "username": user.username or "",
            "first_name": user.first_name or "Игрок"
        })
        balance = result["balance"]
    except Exception:
        balance = "открой приложение для загрузки"
    keyboard = InlineKeyboardMarkup([[
        InlineKeyboardButton("💣 ОТКРЫТЬ NEON MINES", web_app=WebAppInfo(url=WEBAPP_URL))
    ]])
    await update.message.reply_text(
        f"💣 Добро пожаловать в NEON MINES!\n\n"
        f"👤 Игрок: {user.first_name}\n💰 Баланс: {balance}\n\n"
        "Нажми кнопку ниже, чтобы открыть игру.",
        reply_markup=keyboard
    )

async def balance(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        result = await api_get(f"/api/internal/balance/{update.effective_user.id}")
        await update.message.reply_text(f"💰 Твой баланс: {result['balance']} монет")
    except Exception:
        await update.message.reply_text("Не удалось получить баланс. Попробуй позже.")

async def myid(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(f"🆔 Твой Telegram ID: {update.effective_user.id}")

async def terms(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "📜 NEON SLOTS — тестовый проект.\n"
        "Монеты являются виртуальными игровыми единицами, не деньгами.\n"
        "Заявки на вывод — только тестовая функция; автоматические денежные выплаты не выполняются."
    )

async def support(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("🛠 Поддержка NEON SLOTS: напиши администратору проекта.")

async def precheckout(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.pre_checkout_query

    # Verify invoice ownership, amount, currency and pending status before
    # allowing Telegram to collect Stars. Keep this request short because
    # Telegram expects a pre-checkout answer within about 10 seconds.
    try:
        result = await api_post("/api/internal/precheckout", {
            "payload": query.invoice_payload,
            "user_id": query.from_user.id,
            "total_amount": query.total_amount,
            "currency": query.currency
        }, timeout=4)
        valid = bool(result.get("ok"))
        await query.answer(
            ok=valid,
            error_message=None if valid else "Счёт недействителен или уже оплачен. Создай новый счёт."
        )
        print(
            "PRECHECKOUT APPROVED" if valid else "PRECHECKOUT REJECTED",
            "user=", query.from_user.id,
            "payload=", query.invoice_payload
        )
    except Exception as exc:
        print("PRECHECKOUT VERIFICATION ERROR:", repr(exc))
        try:
            await query.answer(
                ok=False,
                error_message="Не удалось проверить счёт. Попробуй создать новый."
            )
        except Exception as answer_exc:
            print("PRECHECKOUT ANSWER ERROR:", repr(answer_exc))

async def paid(update: Update, context: ContextTypes.DEFAULT_TYPE):
    payment = update.message.successful_payment
    user = update.effective_user
    try:
        result = await api_post("/api/internal/payment", {
            "payload": payment.invoice_payload,
            "user_id": user.id,
            "total_amount": payment.total_amount,
            "currency": payment.currency,
            "charge_id": payment.telegram_payment_charge_id
        })
    except Exception:
        await update.message.reply_text(
            "⚠️ Telegram подтвердил оплату, но сервер пока не смог обновить баланс. "
            "Не оплачивай повторно — обратись к администратору."
        )
        if ADMIN_ID:
            await context.bot.send_message(
                ADMIN_ID,
                f"⚠️ Нужна проверка платежа!\nИгрок ID: {user.id}\n"
                f"Payload: {payment.invoice_payload}\nCharge: {payment.telegram_payment_charge_id}"
            )
        return
    await update.message.reply_text(
        f"⭐ Оплата подтверждена!\nЗачислено: {result.get('credited', 0)} монет\n"
        f"Баланс: {result['balance']} монет"
    )
    if ADMIN_ID:
        await context.bot.send_message(
            ADMIN_ID,
            f"⭐ ПОПОЛНЕНИЕ NEON SLOTS\nИгрок: {user.full_name}\n"
            f"ID: {user.id}\nОплата: {payment.total_amount} Stars\n"
            f"Баланс: {result['balance']} монет"
        )

async def withdrawals(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID:
        return
    try:
        data = await api_get("/api/internal/withdrawals")
    except Exception:
        await update.message.reply_text("Не удалось загрузить заявки с сервера.")
        return
    rows = data.get("items", [])
    if not rows:
        await update.message.reply_text("📭 Новых заявок нет.")
        return
    lines = ["📤 ЗАЯВКИ НА ВЫВОД"]
    for row in rows:
        lines.append(
            f"\nЗаявка #{row['id']}\nИгрок ID: {row['uid']}\n"
            f"Сумма: {row['amount']} монет\n/approve {row['id']}\n/reject {row['id']}"
        )
    await update.message.reply_text("\n".join(lines))

async def resolve(update: Update, context: ContextTypes.DEFAULT_TYPE, approved: bool):
    if update.effective_user.id != ADMIN_ID:
        await update.message.reply_text("⛔ Нет доступа.")
        return
    if not context.args or not context.args[0].isdigit():
        await update.message.reply_text("Укажи номер заявки, например: /approve 12")
        return
    wid = int(context.args[0])
    try:
        result = await api_post(f"/api/internal/withdrawals/{wid}/resolve", {"approved": approved})
    except httpx.HTTPStatusError as exc:
        if exc.response.status_code == 404:
            await update.message.reply_text("Заявка не найдена или уже обработана.")
        else:
            await update.message.reply_text("Сервер не смог обработать заявку.")
        return
    except Exception:
        await update.message.reply_text("Не удалось связаться с сервером.")
        return
    status = result["status"]
    user_id = result["user_id"]
    try:
        await context.bot.send_message(
            user_id,
            f"Заявка #{wid}: " + (
                "одобрена. Это тестовая отметка, денежная выплата не производится."
                if approved else "отклонена. Монеты возвращены на баланс."
            )
        )
    except Exception:
        pass
    await update.message.reply_text(f"Заявка #{wid}: {status}. Баланс игрока: {result['balance']} монет.")

async def approve(update, context):
    await resolve(update, context, True)

async def reject(update, context):
    await resolve(update, context, False)

def build_application():
    if not TOKEN or not PAYMENT_SECRET or not ADMIN_ID:
        raise RuntimeError("Нужно задать BOT_TOKEN, ADMIN_ID и PAYMENT_SECRET.")
    builder = ApplicationBuilder().token(TOKEN)
    if TEST_SERVER:
        builder = builder.base_url("https://api.telegram.org/bot{token}/test")
        builder = builder.base_file_url("https://api.telegram.org/file/bot{token}/test")
    application = builder.build()
    handlers = [
        ("start", start), ("balance", balance), ("myid", myid),
        ("terms", terms), ("support", support), ("withdrawals", withdrawals),
        ("approve", approve), ("reject", reject)
    ]
    for command, handler in handlers:
        application.add_handler(CommandHandler(command, handler))
    application.add_handler(PreCheckoutQueryHandler(precheckout))
    application.add_handler(MessageHandler(filters.SUCCESSFUL_PAYMENT, paid))
    print("NEON SLOTS BOT WEBHOOK MODE")
    return application
