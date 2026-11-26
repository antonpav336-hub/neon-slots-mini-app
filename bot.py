# Telegram bot companion for NEON SLOTS Mini App.
# Keep BOT_TOKEN and ADMIN_ID in environment variables, never in source control.
import os, sqlite3, time
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, WebAppInfo
from telegram.ext import ApplicationBuilder, CommandHandler, PreCheckoutQueryHandler, MessageHandler, ContextTypes, filters

TOKEN = os.environ["BOT_TOKEN"]
ADMIN_ID = int(os.getenv("ADMIN_ID", "0"))
WEBAPP_URL = os.environ["WEBAPP_URL"]
DB_PATH = os.getenv("DB_PATH", "casino.db")
MIN_WITHDRAW = int(os.getenv("MIN_WITHDRAW", "50"))

def db():
    con = sqlite3.connect(DB_PATH, timeout=15)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA journal_mode=WAL")
    return con

def init_db():
    with db() as con:
        con.executescript("""
        CREATE TABLE IF NOT EXISTS users(id INTEGER PRIMARY KEY, username TEXT, first_name TEXT, bal INTEGER NOT NULL DEFAULT 100, created_at INTEGER);
        CREATE TABLE IF NOT EXISTS withdrawals(id INTEGER PRIMARY KEY AUTOINCREMENT, uid INTEGER NOT NULL, amount INTEGER NOT NULL, status TEXT NOT NULL DEFAULT 'pending', created_at INTEGER);
        CREATE TABLE IF NOT EXISTS invoices(payload TEXT PRIMARY KEY, uid INTEGER NOT NULL, stars INTEGER NOT NULL, coins INTEGER NOT NULL, status TEXT NOT NULL DEFAULT 'pending', created_at INTEGER);
        CREATE TABLE IF NOT EXISTS payments(charge_id TEXT PRIMARY KEY, uid INTEGER NOT NULL, stars INTEGER NOT NULL, coins INTEGER NOT NULL, created_at INTEGER);
        CREATE TABLE IF NOT EXISTS games(id INTEGER PRIMARY KEY AUTOINCREMENT, uid INTEGER NOT NULL, bet INTEGER NOT NULL, payout INTEGER NOT NULL, symbols TEXT NOT NULL, created_at INTEGER);
        """)

def ensure_user(u):
    with db() as con:
        con.execute("INSERT OR IGNORE INTO users(id,username,first_name,bal,created_at) VALUES(?,?,?,100,?)",
                    (u.id,u.username or "",u.first_name or "Игрок",int(time.time())))
        return con.execute("SELECT bal FROM users WHERE id=?", (u.id,)).fetchone()["bal"]

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    u = update.effective_user
    bal = ensure_user(u)
    kb = InlineKeyboardMarkup([[InlineKeyboardButton("🎰 Открыть NEON SLOTS", web_app=WebAppInfo(url=WEBAPP_URL))]])
    await update.message.reply_text(
        f"🎰 Добро пожаловать в NEON SLOTS!\nБаланс: {bal} монет\n\nОткрой мини-приложение кнопкой ниже.",
        reply_markup=kb)

async def balance(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(f"Баланс: {ensure_user(update.effective_user)} монет")

async def myid(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(f"Твой Telegram ID: {update.effective_user.id}\nУкажи это число в ADMIN_ID на сервере.")

async def terms(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Правила NEON SLOTS: это тестовый проект. Игровые монеты не являются деньгами или финансовым активом. "
        "Перед любыми реальными платежами или выплатами проверьте законодательство и правила Telegram.")

async def support(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Поддержка: напишите администратору проекта.")

async def precheckout(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.pre_checkout_query
    with db() as con:
        row = con.execute("SELECT uid,stars,status FROM invoices WHERE payload=?", (q.invoice_payload,)).fetchone()
    if row and row["uid"] == q.from_user.id and row["stars"] == q.total_amount and row["status"] == "pending":
        await q.answer(ok=True)
    else:
        await q.answer(ok=False, error_message="Счёт недействителен. Создайте новый.")

async def paid(update: Update, context: ContextTypes.DEFAULT_TYPE):
    payment = update.message.successful_payment
    uid, payload = update.effective_user.id, payment.invoice_payload
    charge = payment.telegram_payment_charge_id
    with db() as con:
        con.execute("BEGIN IMMEDIATE")
        invoice = con.execute("SELECT * FROM invoices WHERE payload=?", (payload,)).fetchone()
        if not invoice or invoice["uid"] != uid or invoice["stars"] != payment.total_amount or invoice["status"] != "pending":
            await update.message.reply_text("Платёж получен, но счёт не удалось сопоставить. Обратитесь в поддержку.")
            return
        if con.execute("SELECT 1 FROM payments WHERE charge_id=?", (charge,)).fetchone():
            await update.message.reply_text("Этот платёж уже был обработан.")
            return
        con.execute("INSERT INTO payments(charge_id,uid,stars,coins,created_at) VALUES(?,?,?,?,?)",
                    (charge,uid,payment.total_amount,invoice["coins"],int(time.time())))
        con.execute("UPDATE users SET bal=bal+? WHERE id=?", (invoice["coins"],uid))
        con.execute("UPDATE invoices SET status='paid' WHERE payload=?", (payload,))
        row = con.execute("SELECT bal FROM users WHERE id=?", (uid,)).fetchone()
        bal = row["bal"] if row else 0
    await update.message.reply_text(f"⭐ Оплата подтверждена!\nЗачислено: {invoice['coins']} монет\nБаланс: {bal}")
    if ADMIN_ID:
        await context.bot.send_message(ADMIN_ID,
            f"⭐ ПОПОЛНЕНИЕ\nИгрок: {update.effective_user.full_name}\nID: {uid}\n"
            f"Оплачено: {payment.total_amount} Stars\nЗачислено: {invoice['coins']} монет")

async def withdrawals(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID:
        return
    with db() as con:
        rows = con.execute("SELECT id,uid,amount FROM withdrawals WHERE status='pending' ORDER BY id DESC LIMIT 20").fetchall()
    if not rows:
        await update.message.reply_text("Ожидающих заявок нет.")
        return
    lines = ["📤 Ожидающие заявки:"]
    for r in rows:
        lines.append(f"#{r['id']} • {r['amount']} монет • ID {r['uid']}\n/approve {r['id']} или /reject {r['id']}")
    await update.message.reply_text("\n\n".join(lines))

async def resolve_withdrawal(update: Update, context: ContextTypes.DEFAULT_TYPE, approve: bool):
    if update.effective_user.id != ADMIN_ID:
        await update.message.reply_text("Команда доступна только администратору.")
        return
    if not context.args or not context.args[0].isdigit():
        await update.message.reply_text("Укажи номер: /approve 12 или /reject 12")
        return
    wid = int(context.args[0])
    with db() as con:
        con.execute("BEGIN IMMEDIATE")
        row = con.execute("SELECT * FROM withdrawals WHERE id=?", (wid,)).fetchone()
        if not row or row["status"] != "pending":
            await update.message.reply_text("Заявка не найдена или уже обработана.")
            return
        status = "approved" if approve else "rejected"
        con.execute("UPDATE withdrawals SET status=? WHERE id=?", (status,wid))
        if not approve:
            con.execute("UPDATE users SET bal=bal+? WHERE id=?", (row["amount"],row["uid"]))
    try:
        await context.bot.send_message(row["uid"],
            f"Заявка #{wid}: {'одобрена администратором. Фактическая выплата производится отдельно.' if approve else 'отклонена. Монеты возвращены на баланс.'}")
    except Exception:
        pass
    await update.message.reply_text(f"Заявка #{wid}: {status}. " +
        ("Статус изменён; автоматического перевода денег нет." if approve else "Монеты возвращены."))

async def approve(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await resolve_withdrawal(update, context, True)

async def reject(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await resolve_withdrawal(update, context, False)

def main():
    init_db()
    application = ApplicationBuilder().token(TOKEN).build()
    for command, handler in [
        ("start", start), ("balance", balance), ("myid", myid),
        ("terms", terms), ("support", support), ("withdrawals", withdrawals),
        ("approve", approve), ("reject", reject)
    ]:
        application.add_handler(CommandHandler(command, handler))
    application.add_handler(PreCheckoutQueryHandler(precheckout))
    application.add_handler(MessageHandler(filters.SUCCESSFUL_PAYMENT, paid))
    print("NEON SLOTS bot is running...")
    application.run_polling()

if __name__ == "__main__":
    main()
