import os, json, hmac, hashlib, time, sqlite3, secrets
from urllib.parse import parse_qsl
from pathlib import Path
import httpx
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

ROOT = Path(__file__).resolve().parent
DB_PATH = os.getenv("DB_PATH", str(ROOT / "casino.db"))
BOT_TOKEN = os.getenv("BOT_TOKEN", "")
ADMIN_ID = os.getenv("ADMIN_ID", "")
PAYMENT_SECRET = os.getenv("PAYMENT_SECRET", "")
TELEGRAM_TEST_SERVER = os.getenv("TELEGRAM_TEST_SERVER", "1") == "1"


def telegram_api_url(method: str) -> str:
    suffix = "/test" if TELEGRAM_TEST_SERVER else ""
    return f"https://api.telegram.org/bot{BOT_TOKEN}{suffix}/{method}"
COINS_PER_STAR = int(os.getenv("COINS_PER_STAR", "1"))
MIN_WITHDRAW = int(os.getenv("MIN_WITHDRAW", "50"))
MAX_BET = int(os.getenv("MAX_BET", "100000"))
INIT_DATA_MAX_AGE = int(os.getenv("INIT_DATA_MAX_AGE", "86400"))

app = FastAPI(title="Neon Slots Mini App")
app.mount("/static", StaticFiles(directory=ROOT / "static"), name="static")

def connect():
    con = sqlite3.connect(DB_PATH, timeout=15)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA journal_mode=WAL")
    return con

def init_db():
    with connect() as con:
        con.executescript("""
        CREATE TABLE IF NOT EXISTS users(
            id INTEGER PRIMARY KEY, username TEXT, first_name TEXT,
            bal INTEGER NOT NULL DEFAULT 100, created_at INTEGER NOT NULL
        );
        CREATE TABLE IF NOT EXISTS withdrawals(
            id INTEGER PRIMARY KEY AUTOINCREMENT, uid INTEGER NOT NULL,
            amount INTEGER NOT NULL, status TEXT NOT NULL DEFAULT 'pending',
            created_at INTEGER NOT NULL
        );
        CREATE TABLE IF NOT EXISTS invoices(
            payload TEXT PRIMARY KEY, uid INTEGER NOT NULL, stars INTEGER NOT NULL,
            coins INTEGER NOT NULL, status TEXT NOT NULL DEFAULT 'pending',
            created_at INTEGER NOT NULL
        );
        CREATE TABLE IF NOT EXISTS payments(
            charge_id TEXT PRIMARY KEY, uid INTEGER NOT NULL,
            stars INTEGER NOT NULL, coins INTEGER NOT NULL, created_at INTEGER NOT NULL
        );
        CREATE TABLE IF NOT EXISTS games(
            id INTEGER PRIMARY KEY AUTOINCREMENT, uid INTEGER NOT NULL,
            bet INTEGER NOT NULL, payout INTEGER NOT NULL, symbols TEXT NOT NULL,
            created_at INTEGER NOT NULL
        );
        CREATE TABLE IF NOT EXISTS miner(
            uid INTEGER PRIMARY KEY, points INTEGER NOT NULL DEFAULT 0,
            last_claim INTEGER NOT NULL DEFAULT 0, total_claims INTEGER NOT NULL DEFAULT 0
        );
        """)
init_db()

def telegram_user(init_data: str):
    if not BOT_TOKEN:
        raise HTTPException(503, "Сервер ещё не настроен: BOT_TOKEN отсутствует")
    try:
        pairs = dict(parse_qsl(init_data, keep_blank_values=True))
        supplied_hash = pairs.pop("hash")
        auth_date = int(pairs["auth_date"])
        if time.time() - auth_date > INIT_DATA_MAX_AGE or auth_date > time.time() + 60:
            raise ValueError("expired")
        data_check = "\n".join(f"{k}={v}" for k, v in sorted(pairs.items()))
        secret = hmac.new(b"WebAppData", BOT_TOKEN.encode(), hashlib.sha256).digest()
        expected = hmac.new(secret, data_check.encode(), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(expected, supplied_hash):
            raise ValueError("bad signature")
        return json.loads(pairs["user"])
    except Exception:
        raise HTTPException(401, "Не удалось проверить Telegram-сессию. Откройте приложение из бота.")

async def auth(request: Request):
    raw = request.headers.get("X-Telegram-Init-Data", "")
    if not raw:
        raise HTTPException(401, "Откройте Mini App через Telegram-бота.")
    return telegram_user(raw)

async def notify_admin(message: str):
    if not BOT_TOKEN or not ADMIN_ID:
        return
    try:
        async with httpx.AsyncClient(timeout=8) as client:
            await client.post(telegram_api_url("sendMessage"),
                              json={"chat_id": int(ADMIN_ID), "text": message})
    except Exception:
        pass

def ensure_user(user):
    uid = int(user["id"])
    username = user.get("username") or ""
    first = user.get("first_name") or "Игрок"
    with connect() as con:
        con.execute("""INSERT OR IGNORE INTO users(id, username, first_name, bal, created_at)
                       VALUES(?,?,?,100,?)""", (uid, username, first, int(time.time())))
        con.execute("UPDATE users SET username=?, first_name=? WHERE id=?",
                    (username, first, uid))
        row = con.execute("SELECT bal FROM users WHERE id=?", (uid,)).fetchone()
    return int(row["bal"])

def display_user(user):
    return f"{user.get('first_name','Игрок')} (@{user.get('username')})" if user.get("username") else f"{user.get('first_name','Игрок')} (ID {user['id']})"

@app.get("/")
async def home():
    return FileResponse(ROOT / "static" / "index.html")

@app.get("/api/me")
async def me(request: Request):
    user = await auth(request)
    bal = ensure_user(user)
    with connect() as con:
        games = con.execute("SELECT bet,payout,symbols,created_at FROM games WHERE uid=? ORDER BY id DESC LIMIT 8",
                             (int(user["id"]),)).fetchall()
        withdrawals = con.execute("SELECT id,amount,status,created_at FROM withdrawals WHERE uid=? ORDER BY id DESC LIMIT 5",
                                  (int(user["id"]),)).fetchall()
    return {"user":{"id":int(user["id"]),"name":user.get("first_name","Игрок"),
                    "username":user.get("username","")},
            "balance":bal, "games":[dict(x) for x in games],
            "withdrawals":[dict(x) for x in withdrawals],
            "coins_per_star":COINS_PER_STAR, "min_withdraw":MIN_WITHDRAW}

class Amount(BaseModel):
    amount: int = Field(ge=1, le=10000000)

@app.post("/api/deposit")
async def deposit(body: Amount, request: Request):
    user = await auth(request)
    uid = int(user["id"])
    stars = body.amount
    if stars < 1 or stars > 10000:
        raise HTTPException(400, "Можно купить от 1 до 10000 Stars за один раз.")
    if not BOT_TOKEN:
        raise HTTPException(503, "Пополнение пока не настроено.")
    payload = "dep_" + secrets.token_urlsafe(18)
    coins = stars * COINS_PER_STAR
    with connect() as con:
        con.execute("INSERT INTO invoices(payload,uid,stars,coins,created_at) VALUES(?,?,?,?,?)",
                    (payload, uid, stars, coins, int(time.time())))
    async with httpx.AsyncClient(timeout=12) as client:
        response = await client.post(telegram_api_url("sendInvoice"), json={
            "chat_id": uid, "title": "Монеты Neon Slots",
            "description": f"{coins} игровых монет",
            "payload": payload, "currency": "XTR",
            "prices": [{"label": f"{coins} монет", "amount": stars}]
        })
    data = response.json()
    if not data.get("ok"):
        with connect() as con:
            con.execute("DELETE FROM invoices WHERE payload=? AND status='pending'", (payload,))
        raise HTTPException(502, "Telegram не смог создать счёт. Сначала нажмите /start у бота.")
    return {"ok":True,"message":"Счёт отправлен в чат с ботом. Оплатите его там."}


# ВНУТРЕННИЕ ENDPOINTS: бот обращается к единой базе сайта на Render.
def check_internal_secret(request: Request):
    supplied = request.headers.get("X-Internal-Secret", "")
    if not PAYMENT_SECRET or not hmac.compare_digest(supplied, PAYMENT_SECRET):
        raise HTTPException(403, "Недостаточно прав.")


class InternalUser(BaseModel):
    user_id: int
    username: str = ""
    first_name: str = "Игрок"


class InternalPaymentCheck(BaseModel):
    payload: str
    user_id: int
    total_amount: int
    currency: str


class InternalPayment(InternalPaymentCheck):
    charge_id: str


class InternalWithdrawalDecision(BaseModel):
    approved: bool


@app.post("/api/internal/user")
async def internal_user(body: InternalUser, request: Request):
    check_internal_secret(request)
    user = {"id": body.user_id, "username": body.username, "first_name": body.first_name}
    balance = ensure_user(user)
    return {"ok": True, "balance": balance}


@app.get("/api/internal/balance/{user_id}")
async def internal_balance(user_id: int, request: Request):
    check_internal_secret(request)
    with connect() as con:
        row = con.execute("SELECT bal FROM users WHERE id=?", (user_id,)).fetchone()
    return {"balance": int(row["bal"]) if row else 0}


@app.post("/api/internal/precheckout")
async def internal_precheckout(body: InternalPaymentCheck, request: Request):
    check_internal_secret(request)
    if body.currency != "XTR":
        return {"ok": False}
    with connect() as con:
        invoice = con.execute(
            "SELECT uid,stars,status FROM invoices WHERE payload=?", (body.payload,)
        ).fetchone()
    valid = bool(
        invoice and invoice["uid"] == body.user_id
        and invoice["stars"] == body.total_amount
        and invoice["status"] == "pending"
    )
    return {"ok": valid}


@app.post("/api/internal/payment")
async def internal_payment(body: InternalPayment, request: Request):
    check_internal_secret(request)
    if body.currency != "XTR" or not body.charge_id:
        raise HTTPException(400, "Некорректные данные платежа.")
    with connect() as con:
        con.execute("BEGIN IMMEDIATE")
        duplicate = con.execute(
            "SELECT uid FROM payments WHERE charge_id=?", (body.charge_id,)
        ).fetchone()
        if duplicate:
            row = con.execute("SELECT bal FROM users WHERE id=?", (body.user_id,)).fetchone()
            return {"ok": True, "duplicate": True, "balance": int(row["bal"]) if row else 0}
        invoice = con.execute(
            "SELECT * FROM invoices WHERE payload=?", (body.payload,)
        ).fetchone()
        if (not invoice or invoice["uid"] != body.user_id
                or invoice["stars"] != body.total_amount
                or invoice["status"] != "pending"):
            raise HTTPException(400, "Счёт не найден, уже оплачен или не совпадает сумма.")
        con.execute(
            "INSERT INTO payments(charge_id,uid,stars,coins,created_at) VALUES(?,?,?,?,?)",
            (body.charge_id, body.user_id, body.total_amount, invoice["coins"], int(time.time()))
        )
        con.execute("UPDATE users SET bal=bal+? WHERE id=?",
                    (invoice["coins"], body.user_id))
        con.execute("UPDATE invoices SET status='paid' WHERE payload=?", (body.payload,))
        row = con.execute("SELECT bal FROM users WHERE id=?", (body.user_id,)).fetchone()
        new_balance = int(row["bal"])
    return {"ok": True, "credited": invoice["coins"], "balance": new_balance}


@app.get("/api/internal/withdrawals")
async def internal_withdrawals(request: Request):
    check_internal_secret(request)
    with connect() as con:
        rows = con.execute(
            "SELECT id,uid,amount FROM withdrawals WHERE status='pending' ORDER BY id DESC LIMIT 20"
        ).fetchall()
    return {"items": [dict(row) for row in rows]}


@app.post("/api/internal/withdrawals/{withdrawal_id}/resolve")
async def internal_resolve_withdrawal(withdrawal_id: int, body: InternalWithdrawalDecision, request: Request):
    check_internal_secret(request)
    with connect() as con:
        con.execute("BEGIN IMMEDIATE")
        row = con.execute("SELECT * FROM withdrawals WHERE id=?", (withdrawal_id,)).fetchone()
        if not row or row["status"] != "pending":
            raise HTTPException(404, "Заявка не найдена или уже обработана.")
        status = "approved" if body.approved else "rejected"
        con.execute("UPDATE withdrawals SET status=? WHERE id=?", (status, withdrawal_id))
        if not body.approved:
            con.execute("UPDATE users SET bal=bal+? WHERE id=?", (row["amount"], row["uid"]))
        balance_row = con.execute("SELECT bal FROM users WHERE id=?", (row["uid"],)).fetchone()
        balance = int(balance_row["bal"]) if balance_row else 0
    return {"ok": True, "status": status, "user_id": row["uid"], "amount": row["amount"], "balance": balance}


@app.post("/api/spin")
async def spin(body: Amount, request: Request):
    user = await auth(request)
    uid = int(user["id"])
    bet = body.amount
    if bet > MAX_BET:
        raise HTTPException(400, f"Максимальная ставка: {MAX_BET}.")
    symbols = ["🍒","🍋","🍇","🔔","7️⃣"]
    reels = [secrets.choice(symbols) for _ in range(3)]
    if reels == ["7️⃣","7️⃣","7️⃣"]:
        mult = 10
    elif reels[0] == reels[1] == reels[2]:
        mult = 5
    elif len(set(reels)) == 2:
        mult = 1
    else:
        mult = 0
    payout = bet * mult
    with connect() as con:
        con.execute("BEGIN IMMEDIATE")
        row = con.execute("SELECT bal FROM users WHERE id=?", (uid,)).fetchone()
        if row is None:
            con.execute("INSERT INTO users(id,username,first_name,bal,created_at) VALUES(?,?,?,?,?)",
                        (uid,user.get("username",""),user.get("first_name","Игрок"),100,int(time.time())))
            balance = 100
        else:
            balance = int(row["bal"])
        if bet > balance:
            raise HTTPException(400, f"Недостаточно монет. Баланс: {balance}")
        new_balance = balance - bet + payout
        con.execute("UPDATE users SET bal=? WHERE id=?", (new_balance,uid))
        con.execute("INSERT INTO games(uid,bet,payout,symbols,created_at) VALUES(?,?,?,?,?)",
                    (uid,bet,payout," ".join(reels),int(time.time())))
    outcome = "ВЫИГРЫШ" if payout else "ПРОИГРЫШ"
    await notify_admin(
        f"🎰 {outcome}\nИгрок: {display_user(user)}\nID: {uid}\n"
        f"Ставка: {bet} монет\nВыплата: {payout} монет\nКомбинация: {' '.join(reels)}\nБаланс: {new_balance}"
    )
    return {"symbols":reels,"bet":bet,"multiplier":mult,"payout":payout,
            "balance":new_balance,"result":outcome}

@app.get("/api/miner")
async def miner_status(request: Request):
    user = await auth(request)
    uid = int(user["id"])
    now = int(time.time())
    cooldown = 60
    with connect() as con:
        con.execute("INSERT OR IGNORE INTO miner(uid,points,last_claim,total_claims) VALUES(?,0,0,0)", (uid,))
        row = con.execute("SELECT points,last_claim,total_claims FROM miner WHERE uid=?", (uid,)).fetchone()
    remaining = max(0, cooldown - (now - int(row["last_claim"]))) if row["last_claim"] else 0
    return {"points": int(row["points"]), "remaining": remaining,
            "total_claims": int(row["total_claims"]), "cooldown": cooldown}


@app.post("/api/miner/claim")
async def miner_claim(request: Request):
    user = await auth(request)
    uid = int(user["id"])
    now = int(time.time())
    cooldown = 60
    reward = 2 + secrets.randbelow(7)
    with connect() as con:
        con.execute("BEGIN IMMEDIATE")
        con.execute("INSERT OR IGNORE INTO miner(uid,points,last_claim,total_claims) VALUES(?,0,0,0)", (uid,))
        row = con.execute("SELECT points,last_claim,total_claims FROM miner WHERE uid=?", (uid,)).fetchone()
        remaining = cooldown - (now - int(row["last_claim"])) if row["last_claim"] else 0
        if remaining > 0:
            raise HTTPException(429, f"Майнер заряжается. Подожди {remaining} сек.")
        con.execute("UPDATE miner SET points=points+?,last_claim=?,total_claims=total_claims+1 WHERE uid=?",
                    (reward, now, uid))
        points = int(row["points"]) + reward
        claims = int(row["total_claims"]) + 1
    return {"ok": True, "reward": reward, "points": points,
            "remaining": cooldown, "total_claims": claims}


@app.post("/api/withdraw")
async def withdraw(body: Amount, request: Request):
    user = await auth(request)
    uid = int(user["id"])
    amount = body.amount
    if amount < MIN_WITHDRAW:
        raise HTTPException(400, f"Минимальная заявка: {MIN_WITHDRAW} монет.")
    with connect() as con:
        con.execute("BEGIN IMMEDIATE")
        row = con.execute("SELECT bal FROM users WHERE id=?", (uid,)).fetchone()
        balance = int(row["bal"]) if row else 0
        if amount > balance:
            raise HTTPException(400, f"Недостаточно монет. Баланс: {balance}")
        con.execute("UPDATE users SET bal=bal-? WHERE id=?", (amount,uid))
        cur = con.execute("INSERT INTO withdrawals(uid,amount,status,created_at) VALUES(?,?,'pending',?)",
                          (uid,amount,int(time.time())))
        wid = cur.lastrowid
        new_balance = balance - amount
    await notify_admin(
        f"📤 НОВАЯ ЗАЯВКА НА ВЫВОД #{wid}\n"
        f"Игрок: {display_user(user)}\nID: {uid}\nСумма: {amount} монет\n"
        f"Статус: ожидает решения\n\nКоманды: /approve {wid} или /reject {wid}"
    )
    return {"ok":True,"id":wid,"balance":new_balance,
            "message":f"Заявка #{wid} отправлена администратору. Монеты временно зарезервированы."}
