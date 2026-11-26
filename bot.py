import os
import sqlite3
import time

from telegram import (
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    WebAppInfo
)

from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    PreCheckoutQueryHandler,
    MessageHandler,
    ContextTypes,
    filters
)

# НАСТРОЙКИ
TOKEN = os.getenv("BOT_TOKEN", "")
ADMIN_ID = int(os.getenv("ADMIN_ID", "0"))
WEBAPP_URL = os.getenv("WEBAPP_URL", "")

DB_PATH = os.getenv("DB_PATH", "casino.db")
MIN_WITHDRAW = int(os.getenv("MIN_WITHDRAW", "50"))


# БАЗА ДАННЫХ
def db():
    con = sqlite3.connect(DB_PATH, timeout=15)
    con.row_factory = sqlite3.Row
    return con


def init_db():
    with db() as con:
        con.executescript("""
        CREATE TABLE IF NOT EXISTS users(
            id INTEGER PRIMARY KEY,
            username TEXT,
            first_name TEXT,
            bal INTEGER NOT NULL DEFAULT 100,
            created_at INTEGER
        );

        CREATE TABLE IF NOT EXISTS withdrawals(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            uid INTEGER NOT NULL,
            amount INTEGER NOT NULL,
            status TEXT DEFAULT 'pending',
            created_at INTEGER
        );

        CREATE TABLE IF NOT EXISTS invoices(
            payload TEXT PRIMARY KEY,
            uid INTEGER NOT NULL,
            stars INTEGER NOT NULL,
            coins INTEGER NOT NULL,
            status TEXT DEFAULT 'pending',
            created_at INTEGER
        );

        CREATE TABLE IF NOT EXISTS payments(
            charge_id TEXT PRIMARY KEY,
            uid INTEGER NOT NULL,
            stars INTEGER NOT NULL,
            coins INTEGER NOT NULL,
            created_at INTEGER
        );

        CREATE TABLE IF NOT EXISTS games(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            uid INTEGER NOT NULL,
            bet INTEGER NOT NULL,
            payout INTEGER NOT NULL,
            symbols TEXT NOT NULL,
            created_at INTEGER
        );
        """)


def ensure_user(user):
    with db() as con:
        con.execute(
            """INSERT OR IGNORE INTO users
            (id,username,first_name,bal,created_at)
            VALUES(?,?,?,100,?)""",
            (
                user.id,
                user.username or "",
                user.first_name or "Игрок",
                int(time.time())
            )
        )

        row = con.execute(
            "SELECT bal FROM users WHERE id=?",
            (user.id,)
        ).fetchone()

        return row["bal"]


# /START
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    balance = ensure_user(user)

    keyboard = InlineKeyboardMarkup([
        [
            InlineKeyboardButton(
                "🎰 ОТКРЫТЬ NEON SLOTS",
                web_app=WebAppInfo(url=WEBAPP_URL)
            )
        ]
    ])

    await update.message.reply_text(
        f"🎰 Добро пожаловать в NEON SLOTS!\n\n"
        f"👤 Игрок: {user.first_name}\n"
        f"💰 Баланс: {balance} монет\n\n"
        "Нажми кнопку ниже, чтобы открыть игру.",
        reply_markup=keyboard
    )


# /BALANCE
async def balance(update: Update, context: ContextTypes.DEFAULT_TYPE):
    amount = ensure_user(update.effective_user)

    await update.message.reply_text(
        f"💰 Твой баланс: {amount} монет"
    )


# /MYID
async def myid(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        f"🆔 Твой Telegram ID:\n"
        f"{update.effective_user.id}"
    )


# /TERMS
async def terms(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "📜 NEON SLOTS — тестовый проект.\n"
        "Игровые монеты не являются денежными средствами.\n"
        "Выводы не осуществляются автоматически."
    )


# /SUPPORT
async def support(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "🛠 Поддержка NEON SLOTS\n"
        "Свяжитесь с администратором проекта."
    )


# ПРОВЕРКА ОПЛАТЫ STARS
async def precheckout(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):
    query = update.pre_checkout_query

    with db() as con:
        invoice = con.execute(
            "SELECT uid,stars,status FROM invoices WHERE payload=?",
            (query.invoice_payload,)
        ).fetchone()

    valid = (
        invoice is not None
        and invoice["uid"] == query.from_user.id
        and invoice["stars"] == query.total_amount
        and invoice["status"] == "pending"
    )

    if valid:
        await query.answer(ok=True)
    else:
        await query.answer(
            ok=False,
            error_message="Счёт недействителен."
        )


# УСПЕШНАЯ ОПЛАТА
async def paid(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):
    payment = update.message.successful_payment
    user = update.effective_user

    payload = payment.invoice_payload
    charge = payment.telegram_payment_charge_id

    with db() as con:
        con.execute("BEGIN IMMEDIATE")

        invoice = con.execute(
            "SELECT * FROM invoices WHERE payload=?",
            (payload,)
        ).fetchone()

        if (
            not invoice
            or invoice["uid"] != user.id
            or invoice["stars"] != payment.total_amount
            or invoice["status"] != "pending"
        ):
            await update.message.reply_text(
                "⚠️ Не удалось сопоставить платёж. Обратитесь в поддержку."
            )
            return

        if con.execute(
            "SELECT 1 FROM payments WHERE charge_id=?",
            (charge,)
        ).fetchone():
            return

        con.execute(
            """INSERT INTO payments
            (charge_id,uid,stars,coins,created_at)
            VALUES(?,?,?,?,?)""",
            (
                charge,
                user.id,
                payment.total_amount,
                invoice["coins"],
                int(time.time())
            )
        )

        con.execute(
            "UPDATE users SET bal=bal+? WHERE id=?",
            (invoice["coins"], user.id)
        )

        con.execute(
            "UPDATE invoices SET status='paid' WHERE payload=?",
            (payload,)
        )

        new_balance = con.execute(
            "SELECT bal FROM users WHERE id=?",
            (user.id,)
        ).fetchone()["bal"]

    await update.message.reply_text(
        f"⭐ Оплата прошла успешно!\n"
        f"Зачислено: {invoice['coins']} монет\n"
        f"Баланс: {new_balance}"
    )

    if ADMIN_ID:
        await context.bot.send_message(
            ADMIN_ID,
            f"⭐ ПОПОЛНЕНИЕ NEON SLOTS\n\n"
            f"Игрок: {user.full_name}\n"
            f"Username: @{user.username or 'нет'}\n"
            f"ID: {user.id}\n"
            f"Оплата: {payment.total_amount} Stars\n"
            f"Зачислено: {invoice['coins']} монет"
        )


# ЗАЯВКИ НА ВЫВОД
async def withdrawals(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):
    if update.effective_user.id != ADMIN_ID:
        return

    with db() as con:
        rows = con.execute(
            """SELECT id,uid,amount FROM withdrawals
            WHERE status='pending'
            ORDER BY id DESC LIMIT 20"""
        ).fetchall()

    if not rows:
        await update.message.reply_text(
            "📭 Новых заявок нет."
        )
        return

    lines = ["📤 ЗАЯВКИ НА ВЫВОД"]

    for row in rows:
        lines.append(
            f"\nЗаявка #{row['id']}\n"
            f"Игрок ID: {row['uid']}\n"
            f"Сумма: {row['amount']}\n"
            f"/approve {row['id']}\n"
            f"/reject {row['id']}"
        )

    await update.message.reply_text("\n".join(lines))


# ОБРАБОТКА ЗАЯВОК
async def resolve(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    approved: bool
):
    if update.effective_user.id != ADMIN_ID:
        await update.message.reply_text("⛔ Нет доступа.")
        return

    if not context.args or not context.args[0].isdigit():
        await update.message.reply_text(
            "Укажи номер заявки: /approve 12"
        )
        return

    wid = int(context.args[0])

    with db() as con:
        con.execute("BEGIN IMMEDIATE")

        row = con.execute(
            "SELECT * FROM withdrawals WHERE id=?",
            (wid,)
        ).fetchone()

        if not row or row["status"] != "pending":
            await update.message.reply_text(
                "Заявка не найдена или уже обработана."
            )
            return

        status = "approved" if approved else "rejected"

        con.execute(
            "UPDATE withdrawals SET status=? WHERE id=?",
            (status, wid)
        )

        if not approved:
            con.execute(
                "UPDATE users SET bal=bal+? WHERE id=?",
                (row["amount"], row["uid"])
            )

    try:
        await context.bot.send_message(
            row["uid"],
            f"Заявка #{wid}: "
            + (
                "одобрена. Выплата производится отдельно."
                if approved
                else "отклонена. Монеты возвращены."
            )
        )
    except Exception:
        pass

    await update.message.reply_text(
        f"Заявка #{wid}: {status}"
    )


async def approve(update, context):
    await resolve(update, context, True)


async def reject(update, context):
    await resolve(update, context, False)


# ЗАПУСК
def main():
    init_db()

    application = (
        ApplicationBuilder()
        .token(TOKEN)
        .build()
    )

    handlers = [
        ("start", start),
        ("balance", balance),
        ("myid", myid),
        ("terms", terms),
        ("support", support),
        ("withdrawals", withdrawals),
        ("approve", approve),
        ("reject", reject)
    ]

    for command, handler in handlers:
        application.add_handler(
            CommandHandler(command, handler)
        )

    application.add_handler(
        PreCheckoutQueryHandler(precheckout)
    )

    application.add_handler(
        MessageHandler(filters.SUCCESSFUL_PAYMENT, paid)
    )

    print("NEON SLOTS BOT STARTED")

    application.run_polling()


if __name__ == "__main__":
    main()
