import os, json, hmac, hashlib, time, sqlite3, secrets
from contextlib import asynccontextmanager
from urllib.parse import parse_qsl
from pathlib import Path
import httpx
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from telegram import Update
from bot import build_application

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
MIN_WITHDRAW = 2500
MIN_DEPOSIT = 100
MAX_BET = int(os.getenv("MAX_BET", "100000"))
INIT_DATA_MAX_AGE = int(os.getenv("INIT_DATA_MAX_AGE", "86400"))

telegram_application = None
WEBHOOK_PATH = "/telegram/webhook"
WEBHOOK_SECRET = os.getenv("WEBHOOK_SECRET", "").strip() or secrets.token_urlsafe(32)

@asynccontextmanager
async def lifespan(fastapi_app):
    global telegram_application
    if BOT_TOKEN and PAYMENT_SECRET and ADMIN_ID:
        candidate = build_application()
        try:
            await candidate.initialize()
            await candidate.start()
            public_url = os.getenv("WEBHOOK_URL", "").strip() or os.getenv("RENDER_EXTERNAL_URL", "").strip()
            if public_url:
                webhook_url = public_url.rstrip("/") + WEBHOOK_PATH
                await candidate.bot.set_webhook(
                    url=webhook_url,
                    secret_token=WEBHOOK_SECRET,
                    allowed_updates=Update.ALL_TYPES,
                    drop_pending_updates=False,
                )
                print("Telegram webhook configured:", webhook_url)
            else:
                print("WARNING: WEBHOOK_URL/RENDER_EXTERNAL_URL is missing; webhook not configured.")
            telegram_application = candidate
        except Exception as exc:
            print("TELEGRAM BOT STARTUP ERROR:", repr(exc))
            try:
                if candidate.running:
                    await candidate.stop()
                await candidate.shutdown()
            except Exception:
                pass
            telegram_application = None
    else:
        print("WARNING: BOT_TOKEN, PAYMENT_SECRET or ADMIN_ID is missing; Telegram bot is disabled.")
    try:
        yield
    finally:
        if telegram_application is not None:
            await telegram_application.stop()
            await telegram_application.shutdown()
            telegram_application = None

app = FastAPI(title="Neon Slots Mini App", lifespan=lifespan)
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
        CREATE TABLE IF NOT EXISTS mines_games(
            id TEXT PRIMARY KEY, uid INTEGER NOT NULL, bet INTEGER NOT NULL,
            mine_count INTEGER NOT NULL, mine_cells TEXT NOT NULL,
            revealed TEXT NOT NULL DEFAULT '[]', status TEXT NOT NULL DEFAULT 'active',
            payout INTEGER NOT NULL DEFAULT 0, created_at INTEGER NOT NULL
        );
        CREATE TABLE IF NOT EXISTS case_opens(
            id INTEGER PRIMARY KEY AUTOINCREMENT, uid INTEGER NOT NULL,
            case_id TEXT NOT NULL, case_name TEXT NOT NULL, price INTEGER NOT NULL,
            multiplier REAL NOT NULL, payout INTEGER NOT NULL, created_at INTEGER NOT NULL
        );
        CREATE TABLE IF NOT EXISTS slot_spins(
            id INTEGER PRIMARY KEY AUTOINCREMENT, uid INTEGER NOT NULL,
            game_id TEXT NOT NULL, game_name TEXT NOT NULL, bet INTEGER NOT NULL,
            symbols TEXT NOT NULL, multiplier REAL NOT NULL, payout INTEGER NOT NULL,
            created_at INTEGER NOT NULL
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

@app.post(WEBHOOK_PATH)
async def telegram_webhook(request: Request):
    if not hmac.compare_digest(
        request.headers.get("X-Telegram-Bot-Api-Secret-Token", ""),
        WEBHOOK_SECRET,
    ):
        raise HTTPException(403, "Invalid webhook secret")
    if telegram_application is None:
        raise HTTPException(503, "Telegram bot is starting")
    try:
        payload = await request.json()
        update = Update.de_json(payload, telegram_application.bot)
        await telegram_application.process_update(update)
    except Exception as exc:
        print("TELEGRAM WEBHOOK ERROR:", repr(exc))
        raise HTTPException(500, "Webhook processing failed")
    return {"ok": True}

@app.get("/")
async def home():
    return FileResponse(ROOT / "static" / "index.html", headers={"Cache-Control":"no-store, no-cache, must-revalidate, max-age=0","Pragma":"no-cache","Expires":"0"})

@app.get("/api/me")
async def me(request: Request):
    user = await auth(request)
    bal = ensure_user(user)
    uid = int(user["id"])
    with connect() as con:
        games = con.execute("SELECT bet,payout,symbols,created_at FROM games WHERE uid=? ORDER BY id DESC LIMIT 8",
                             (uid,)).fetchall()
        withdrawals = con.execute("SELECT id,amount,status,created_at FROM withdrawals WHERE uid=? ORDER BY id DESC LIMIT 5",
                                  (uid,)).fetchall()
        stats = con.execute("""SELECT COUNT(*) AS games,
            SUM(CASE WHEN status='cashed_out' THEN 1 ELSE 0 END) AS wins,
            SUM(CASE WHEN status='lost' THEN 1 ELSE 0 END) AS losses,
            COALESCE(SUM(payout),0) AS payout,
            COALESCE(SUM(payout-bet),0) AS net_profit,
            COALESCE(ROUND(100.0*SUM(CASE WHEN status='cashed_out' THEN 1 ELSE 0 END)/NULLIF(COUNT(*),0)),0) AS win_rate,
            COALESCE(MAX(CASE WHEN status='cashed_out' AND bet>0 THEN 1.0*payout/bet ELSE 0 END),0) AS best_multiplier
            FROM mines_games WHERE uid=? AND status IN ('lost','cashed_out')""", (uid,)).fetchone()
    return {"user":{"id":uid,"name":user.get("first_name","Игрок"),
                    "username":user.get("username","")},
            "balance":bal, "games":[dict(x) for x in games],
            "stats":dict(stats),
            "withdrawals":[dict(x) for x in withdrawals],
            "coins_per_star":COINS_PER_STAR, "min_withdraw":MIN_WITHDRAW}

class Amount(BaseModel):
    amount: int = Field(ge=1, le=10000000)

@app.get("/api/leaderboard")
async def leaderboard(request: Request):
    await auth(request)
    with connect() as con:
        rows = con.execute("""SELECT u.first_name, u.username,
            COUNT(m.id) AS games,
            SUM(CASE WHEN m.status='cashed_out' THEN 1 ELSE 0 END) AS wins,
            COALESCE(SUM(m.payout-m.bet),0) AS profit
            FROM mines_games m JOIN users u ON u.id=m.uid
            WHERE m.status IN ('lost','cashed_out')
            GROUP BY m.uid
            ORDER BY profit DESC, wins DESC, games DESC
            LIMIT 10""").fetchall()
    items = []
    for row in rows:
        name = row["first_name"] or "Игрок"
        if row["username"]:
            name += " @" + row["username"]
        items.append({"name": name, "games": int(row["games"]),
                      "wins": int(row["wins"] or 0), "profit": int(row["profit"] or 0)})
    return {"items": items}


CASE_CONFIG = {
    "starter": {"name": "STARTER", "price": 50},
    "neon": {"name": "NEON", "price": 100},
    "crystal": {"name": "CRYSTAL", "price": 250},
    "royal": {"name": "ROYAL", "price": 500},
    "legend": {"name": "LEGEND", "price": 1000},
    "cosmic": {"name": "COSMIC", "price": 2500},
}


SLOT_CONFIG = {
    "olympus": {"name": "OLYMPUS", "symbols": ["⚡", "👑", "🏛️", "💎", "🍇", "🛡️"], "multipliers": {"⚡": 12, "👑": 7, "🏛️": 5, "💎": 3, "🍇": 2, "🛡️": 1.5}},
    "sweet": {"name": "SWEET BONANZA", "symbols": ["🍭", "🍬", "🍇", "🍉", "🍌", "🍓"], "weights": [4, 8, 12, 18, 26, 32], "multipliers": {"🍭": 10, "🍬": 7, "🍇": 5, "🍉": 3, "🍌": 2, "🍓": 1.5}},
    "sugar": {"name": "SUGAR RUSH", "symbols": ["🍩", "🧁", "🍫", "🍬", "🍒", "🍓"], "weights": [4, 8, 12, 18, 26, 32], "multipliers": {"🍩": 12, "🧁": 8, "🍫": 5, "🍬": 3, "🍒": 2, "🍓": 1.5}},
}



# Server-authoritative cascade slots. Client receives animation frames only; all RNG,
# payout calculation and balance settlement remain inside this transaction.
with connect() as _con:
    _con.executescript("""
    CREATE TABLE IF NOT EXISTS slot_sessions(
        uid INTEGER NOT NULL, game_id TEXT NOT NULL,
        bonus INTEGER NOT NULL DEFAULT 0, bonus_mode INTEGER NOT NULL DEFAULT 0,
        global_mult INTEGER NOT NULL DEFAULT 0, cell_hits TEXT NOT NULL DEFAULT '[]',
        PRIMARY KEY(uid, game_id)
    );
    CREATE TABLE IF NOT EXISTS slot_round_requests(
        request_id TEXT PRIMARY KEY, uid INTEGER NOT NULL,
        response TEXT NOT NULL, created_at INTEGER NOT NULL
    );
    """)

_SLOT_GAMES = {
    "olympus": {"name":"THUNDER GOD","cols":6,"rows":5,"scatter":"⚡",
        "symbols":["🔵","🟢","🟡","🟣","🔴","🏆","💍","⌛","👑"],
        "high":["🏆","💍","⌛","👑"],"mult_count":15},
    "sweet": {"name":"SWEET BLAST","cols":6,"rows":5,"scatter":"🍭",
        "symbols":["🍌","🍇","🍉","🫐","🍎","🟦","🟩","🟪","❤️"],
        "high":["🟦","🟩","🟪","❤️"],"mult_count":13},
    "sugar": {"name":"SUGAR RUSH","cols":7,"rows":7,"scatter":"🚀",
        "symbols":["🐻","🧸","🐻‍❄️","⭐","🫘","🧡","🍡"],
        "high":["⭐","🫘","🧡","🍡"],"mult_count":13},
}

# Central game math configuration. Adjust these values here to rebalance every game.
# Weights are relative; the backend is the only source of RNG and settlement.
GAME_MATH_CONFIG = {
    "slots": {
        "payout_factor": 0.62,
        "scatter_bps": {"olympus": 30, "sweet": 30, "sugar": 20},
        "ante_scatter_bps": {"olympus": 60, "sweet": 60, "sugar": 40},
        "cascade_multiplier_bps": {"olympus": 300, "sweet": 500},
        "multiplier_weights": [60, 22, 9, 4, 2, 1.2, 0.8, 0.5, 0.25, 0.15, 0.1, 0.05, 0.025, 0.01, 0.005],
        "scatter_payouts": {4: 2, 5: 4, 6: 8},
        "symbol_weights": {"olympus": [14,14,14,14,14,8,8,7,7], "sweet": [14,14,14,14,14,8,8,7,7], "sugar": [15,15,15,14,14,14,13]},
    },
    "cases": {
        "outcomes": [(1500, 0.0), (4500, 0.5), (7500, 0.8), (9200, 1.0), (9900, 1.5), (10000, 5.0)]
    },
    "mines": {"cashout_rtp": 0.90},
    "classic_slots": {"pair_multiplier": 0.7, "triple_multiplier": 3.0, "jackpot_multiplier": 10.0, "payout_factor": 0.85, "symbol_weights": {"olympus": [4,8,12,18,26,32], "sweet": [4,8,12,18,26,32], "sugar": [4,8,12,18,26,32]}, "legacy_spin_weights": [28,26,22,15,9]}
}
_SLOT_MULTS = [2,3,4,5,6,8,10,12,15,20,25,50,100,250,500]
_SLOT_PAYOUT_FACTOR = GAME_MATH_CONFIG["slots"]["payout_factor"]

for _game_id, _game in _SLOT_GAMES.items():
    _game["weights"] = GAME_MATH_CONFIG["slots"]["symbol_weights"][_game_id]

def _slot_weighted_symbol(game):
    weights = game.get("weights")
    return secrets.SystemRandom().choices(game["symbols"], weights=weights, k=1)[0] if weights else secrets.choice(game["symbols"])

def _slot_board(game):
    return [[_slot_weighted_symbol(game) for _ in range(game["cols"])]
            for _ in range(game["rows"])]

def _slot_scatter_count(board, scatter):
    return sum(1 for row in board for symbol in row if symbol == scatter)

def _slot_place_scatters(board, game, count):
    cells = list(range(game["rows"] * game["cols"]))
    secrets.SystemRandom().shuffle(cells)
    for idx in cells[:min(count, len(cells))]:
        board[idx // game["cols"]][idx % game["cols"]] = game["scatter"]

def _slot_find_anywhere(board, game):
    positions = {}
    for idx, symbol in enumerate(sum(board, [])):
        if symbol == game["scatter"]:
            continue
        positions.setdefault(symbol, []).append(idx)
    return [{"symbol":s,"ids":ids} for s,ids in positions.items() if len(ids) >= 8]

def _slot_find_clusters(board, game):
    rows, cols = game["rows"], game["cols"]
    seen, groups = set(), []
    for row in range(rows):
        for col in range(cols):
            symbol = board[row][col]
            start = row * cols + col
            if symbol == game["scatter"] or start in seen:
                continue
            queue, group = [start], []
            seen.add(start)
            while queue:
                idx = queue.pop()
                rr, cc = divmod(idx, cols)
                group.append(idx)
                for nr,nc in ((rr-1,cc),(rr+1,cc),(rr,cc-1),(rr,cc+1)):
                    if 0 <= nr < rows and 0 <= nc < cols:
                        ni = nr * cols + nc
                        if ni not in seen and board[nr][nc] == symbol:
                            seen.add(ni)
                            queue.append(ni)
            if len(group) >= 5:
                groups.append({"symbol":symbol,"ids":group})
    return groups

def _slot_symbol_pay(symbol, count, bet, game):
    high = symbol in game["high"]
    if game is _SLOT_GAMES["sugar"]:
        base = .48 if high else .24
        return bet * base * max(1, count / 5) ** (1.7 if high else 1.4)
    if count >= 12:
        return bet * ((12 + min(38,(count-12)*4)) if high
                      else (2 + min(8,(count-12)*.8))
        )
    return bet * ((1.5 + max(0,count-8)*1.6) if high
                  else (.25 + max(0,count-8)*.25))

def _slot_pick_mult(game):
    weights = GAME_MATH_CONFIG["slots"]["multiplier_weights"][:game["mult_count"]]
    return secrets.SystemRandom().choices(_SLOT_MULTS[:game["mult_count"]],weights=weights,k=1)[0]

def _slot_gravity(board, remove, game):
    cols, rows = game["cols"], game["rows"]
    removed = set(remove)
    next_board = [[None for _ in range(cols)] for _ in range(rows)]
    for col in range(cols):
        kept = [board[row][col] for row in range(rows-1,-1,-1)
                if row*cols+col not in removed]
        while len(kept) < rows:
            kept.append(_slot_weighted_symbol(game))
        for row in range(rows-1,-1,-1):
            next_board[row][col] = kept[rows-1-row]
    return next_board

def _slot_scatter_spins(game_id, count):
    if game_id == "sugar":
        return {3:10,4:12,5:15,6:20,7:30}.get(min(count,7),30)
    return 10 if game_id == "sweet" else 15

def _slot_simulate(game_id, game, bet, ante, buy, session):
    bonus = int(session["bonus"])
    bonus_mode = bool(session["bonus_mode"])
    global_mult = int(session["global_mult"])
    cell_hits = json.loads(session["cell_hits"] or "[]")
    if bonus_mode and bonus <= 0:
        bonus_mode, global_mult, cell_hits = False, 0, []
    is_free = bonus_mode and bonus > 0
    if is_free:
        bonus -= 1
    if game_id == "sugar":
        if not bonus_mode or not cell_hits:
            cell_hits = [0] * (game["rows"] * game["cols"])
    else:
        cell_hits = []

    board = _slot_board(game)
    if buy:
        target = secrets.randbelow(5 if game_id == "sugar" else 3) + (3 if game_id == "sugar" else 4)
        _slot_place_scatters(board, game, target)
    elif secrets.randbelow(10000) < (GAME_MATH_CONFIG["slots"]["ante_scatter_bps"] if ante else GAME_MATH_CONFIG["slots"]["scatter_bps"])[game_id]:
        low = 3 if game_id == "sugar" else 4
        high = 7 if game_id == "sugar" else 6
        _slot_place_scatters(board, game, low + secrets.randbelow(high-low+1))

    initial_scatter = _slot_scatter_count(board, game["scatter"])
    steps = [{"board":[row[:] for row in board],"remove":[],"spots":cell_hits[:],"cascade":0}]
    win_total, all_mult, cascades = 0.0, 0, 0
    spin_mults = []
    while cascades < 12:
        groups = (_slot_find_clusters(board,game) if game_id == "sugar"
                  else _slot_find_anywhere(board,game))
        if not groups:
            break
        remove, step_win = [], 0.0
        for group in groups:
            ids = group["ids"]
            if game_id == "sugar":
                group_mult = 1
                for idx in ids:
                    if cell_hits[idx] > 0:
                        group_mult += 2 ** (cell_hits[idx]-1)
                    cell_hits[idx] = min(8,cell_hits[idx]+1)
                step_win += _slot_symbol_pay(group["symbol"],len(ids),bet,game) * group_mult
            else:
                step_win += _slot_symbol_pay(group["symbol"],len(ids),bet,game)
            remove.extend(ids)
        win_total += step_win
        unique_remove = sorted(set(remove))
        steps.append({"board":[row[:] for row in board],"remove":unique_remove,
                      "spots":cell_hits[:],"cascade":cascades+1})
        if game_id == "olympus" and secrets.randbelow(10000) < GAME_MATH_CONFIG["slots"]["cascade_multiplier_bps"]["olympus"]:
            spin_mults.append(_slot_pick_mult(game))
        elif game_id == "sweet" and bonus_mode and secrets.randbelow(10000) < GAME_MATH_CONFIG["slots"]["cascade_multiplier_bps"]["sweet"]:
            spin_mults.append(_slot_pick_mult(game))
        board = _slot_gravity(board,unique_remove,game)
        cascades += 1
        steps.append({"board":[row[:] for row in board],"remove":[],
                      "spots":cell_hits[:],"cascade":cascades,"drop":True})
    scatter_pay = 0
    if game_id != "sugar" and initial_scatter >= 4:
        scatter_pay = bet * GAME_MATH_CONFIG["slots"]["scatter_payouts"].get(min(initial_scatter,6), 0)
    win_total += scatter_pay
    if game_id == "olympus" and win_total > 0:
        if bonus_mode:
            if spin_mults:
                global_mult += sum(spin_mults)
            all_mult = global_mult
            if global_mult:
                win_total *= global_mult
        elif spin_mults:
            all_mult = sum(spin_mults)
            win_total *= all_mult
    elif game_id == "sweet" and bonus_mode and spin_mults and win_total > 0:
        all_mult = sum(spin_mults)
        win_total *= all_mult
    elif game_id == "sugar" and bonus_mode and win_total > 0:
        all_mult = sum(2 ** (hit-1) for hit in cell_hits if hit > 1)

    # Lower game return as requested; this factor is applied server-side only.
    payout = max(0, int(win_total * _SLOT_PAYOUT_FACTOR))
    if initial_scatter >= 4 and not bonus_mode:
        bonus = _slot_scatter_spins(game_id,initial_scatter)
        bonus_mode = True
        if game_id != "olympus":
            global_mult = 0
    elif bonus_mode and initial_scatter >= 3:
        extra = _slot_scatter_spins(game_id,initial_scatter) if game_id == "sugar" else 5
        bonus += extra
    elif not bonus_mode and game_id == "sugar" and initial_scatter >= 3:
        bonus = _slot_scatter_spins(game_id,initial_scatter)
        bonus_mode = True
    if bonus_mode and bonus == 0:
        bonus_mode, global_mult = False, 0
        if game_id == "sugar":
            cell_hits = []
    return {
        "steps":steps,"final_board":board,"payout":payout,
        "multiplier":all_mult,"bonus":bonus,"bonus_mode":bonus_mode,
        "global_mult":global_mult,"cell_hits":cell_hits,
        "scatters":initial_scatter,"cascades":cascades,
        "is_free":is_free,"spin_mults":spin_mults
    }

@app.post("/api/slots/play")
async def slots_play(body: dict, request: Request):
    user = await auth(request)
    uid = int(user["id"])
    game_id = body.get("game_id")
    game = _SLOT_GAMES.get(game_id) if isinstance(game_id,str) else None
    bet = body.get("bet")
    ante = body.get("ante",False)
    buy = body.get("buy",False)
    request_id = body.get("request_id")
    if not game:
        raise HTTPException(400,"Неизвестный слот.")
    if isinstance(bet,bool) or not isinstance(bet,int) or bet < 1 or bet > 1000:
        raise HTTPException(400,"Ставка должна быть от 1 до 1000 ⭐.")
    if not isinstance(ante,bool) or not isinstance(buy,bool):
        raise HTTPException(400,"Некорректные параметры раунда.")
    if not isinstance(request_id,str) or not 12 <= len(request_id) <= 80:
        raise HTTPException(400,"Не удалось создать идентификатор раунда.")
    ensure_user(user)
    with connect() as con:
        con.execute("BEGIN IMMEDIATE")
        previous = con.execute("SELECT uid,response FROM slot_round_requests WHERE request_id=?",(request_id,)).fetchone()
        if previous:
            if int(previous["uid"]) != uid:
                raise HTTPException(409,"Идентификатор раунда уже использован.")
            return json.loads(previous["response"])
        row = con.execute("SELECT bal FROM users WHERE id=?",(uid,)).fetchone()
        balance = int(row["bal"]) if row else 0
        session = con.execute("SELECT * FROM slot_sessions WHERE uid=? AND game_id=?",(uid,game_id)).fetchone()
        if session is None:
            session = {"bonus":0,"bonus_mode":0,"global_mult":0,"cell_hits":"[]"}
        if buy and bool(session["bonus_mode"]) and int(session["bonus"]) > 0:
            raise HTTPException(409,"Сначала заверши текущую бонусную игру.")
        free_spin = bool(session["bonus_mode"]) and int(session["bonus"]) > 0
        cost = 0 if free_spin else (bet*100 if buy else ((bet*5+3)//4 if ante else bet))
        if balance < cost:
            raise HTTPException(400,"Недостаточно звёзд")
        result = _slot_simulate(game_id,game,bet,ante,buy,session)
        new_balance = balance - cost + int(result["payout"])
        con.execute("UPDATE users SET bal=? WHERE id=?",(new_balance,uid))
        con.execute("""INSERT INTO slot_sessions(uid,game_id,bonus,bonus_mode,global_mult,cell_hits)
            VALUES(?,?,?,?,?,?) ON CONFLICT(uid,game_id) DO UPDATE SET
            bonus=excluded.bonus,bonus_mode=excluded.bonus_mode,
            global_mult=excluded.global_mult,cell_hits=excluded.cell_hits""",
            (uid,game_id,result["bonus"],int(result["bonus_mode"]),result["global_mult"],json.dumps(result["cell_hits"])))
        symbols = " ".join(sum(result["final_board"],[]))
        con.execute("""INSERT INTO slot_spins(uid,game_id,game_name,bet,symbols,multiplier,payout,created_at)
            VALUES(?,?,?,?,?,?,?,?)""",
            (uid,game_id,game["name"],cost,symbols,result["multiplier"],result["payout"],int(time.time())))
        response = {
            "ok":True,"game_id":game_id,"bet":bet,"cost":cost,"balance":new_balance,
            "payout":int(result["payout"]),"multiplier":result["multiplier"],
            "bonus":result["bonus"],"bonus_mode":result["bonus_mode"],
            "global_mult":result["global_mult"],"scatters":result["scatters"],"spin_mults":result["spin_mults"],
            "cascades":result["cascades"],"is_free":result["is_free"],
            "steps":result["steps"],"final_board":result["final_board"]
        }
        con.execute("INSERT INTO slot_round_requests(request_id,uid,response,created_at) VALUES(?,?,?,?)",
                    (request_id,uid,json.dumps(response,ensure_ascii=False),int(time.time())))
    return response

@app.get("/api/slots/state/{game_id}")
async def slots_state(game_id: str, request: Request):
    user = await auth(request)
    ensure_user(user)
    if game_id not in _SLOT_GAMES:
        raise HTTPException(400,"Неизвестный слот.")
    with connect() as con:
        row = con.execute("SELECT bal FROM users WHERE id=?",(int(user["id"]),)).fetchone()
        session = con.execute("SELECT bonus,bonus_mode,global_mult FROM slot_sessions WHERE uid=? AND game_id=?",
                              (int(user["id"]),game_id)).fetchone()
    return {"balance":int(row["bal"]) if row else 0,
            "bonus":int(session["bonus"]) if session else 0,
            "bonus_mode":bool(session["bonus_mode"]) if session else False,
            "global_mult":int(session["global_mult"]) if session else 0}

@app.get("/api/slots/history")
async def slots_history(request: Request):
    user = await auth(request)
    with connect() as con:
        rows = con.execute(
            "SELECT game_name,bet,symbols,multiplier,payout,created_at FROM slot_spins WHERE uid=? ORDER BY id DESC LIMIT 12",
            (int(user["id"]),)
        ).fetchall()
    return {"items": [dict(row) for row in rows]}

@app.post("/api/slots/spin")
async def slots_spin(body: dict, request: Request):
    user = await auth(request)
    uid = int(user["id"])
    game_id = body.get("game_id")
    config = SLOT_CONFIG.get(game_id) if isinstance(game_id, str) else None
    bet = body.get("bet")
    if not config:
        raise HTTPException(400, "Неизвестный слот.")
    if isinstance(bet, bool) or not isinstance(bet, int) or bet < 1 or bet > MAX_BET:
        raise HTTPException(400, f"Ставка должна быть от 1 до {MAX_BET} ⭐.")
    ensure_user(user)
    symbols = [secrets.SystemRandom().choices(config["symbols"], weights=GAME_MATH_CONFIG["classic_slots"]["symbol_weights"][game_id], k=1)[0] for _ in range(3)]
    multiplier = config["multipliers"].get(symbols[0], 0) if symbols.count(symbols[0]) == 3 else 0
    payout = int(bet * multiplier * GAME_MATH_CONFIG["classic_slots"]["payout_factor"])
    now = int(time.time())
    with connect() as con:
        con.execute("BEGIN IMMEDIATE")
        row = con.execute("SELECT bal FROM users WHERE id=?", (uid,)).fetchone()
        balance = int(row["bal"]) if row else 0
        if balance < bet:
            raise HTTPException(400, f"Недостаточно ⭐. Баланс: {balance} ⭐.")
        new_balance = balance - bet + payout
        con.execute("UPDATE users SET bal=? WHERE id=?", (new_balance, uid))
        con.execute(
            "INSERT INTO slot_spins(uid,game_id,game_name,bet,symbols,multiplier,payout,created_at) VALUES(?,?,?,?,?,?,?,?)",
            (uid, game_id, config["name"], bet, " ".join(symbols), multiplier, payout, now)
        )
    return {"ok": True, "game_id": game_id, "game_name": config["name"],
            "bet": bet, "symbols": symbols, "multiplier": multiplier,
            "payout": payout, "net": payout - bet, "balance": new_balance}


@app.get("/api/cases/history")
async def cases_history(request: Request):
    user = await auth(request)
    uid = int(user["id"])
    with connect() as con:
        rows = con.execute(
            "SELECT case_name,price,multiplier,payout,created_at FROM case_opens WHERE uid=? ORDER BY id DESC LIMIT 10",
            (uid,)
        ).fetchall()
    return {"items": [dict(row) for row in rows]}


@app.post("/api/cases/open")
async def cases_open(body: dict, request: Request):
    user = await auth(request)
    uid = int(user["id"])
    case_id = body.get("case_id")
    case = CASE_CONFIG.get(case_id) if isinstance(case_id, str) else None
    if not case:
        raise HTTPException(400, "Неизвестный кейс.")

    # Centralized server-side case outcome table; the same odds apply to all case tiers.
    roll = secrets.randbelow(10000)
    multiplier = next(mult for ceiling, mult in GAME_MATH_CONFIG["cases"]["outcomes"] if roll < ceiling)
    payout = int(case["price"] * multiplier)
    now = int(time.time())

    ensure_user(user)
    with connect() as con:
        con.execute("BEGIN IMMEDIATE")
        row = con.execute("SELECT bal FROM users WHERE id=?", (uid,)).fetchone()
        balance = int(row["bal"]) if row else 0
        if balance < case["price"]:
            raise HTTPException(400, f"Недостаточно ⭐. Нужно {case['price']} ⭐, баланс: {balance} ⭐.")
        new_balance = balance - case["price"] + payout
        con.execute("UPDATE users SET bal=? WHERE id=?", (new_balance, uid))
        con.execute(
            "INSERT INTO case_opens(uid,case_id,case_name,price,multiplier,payout,created_at) VALUES(?,?,?,?,?,?,?)",
            (uid, case_id, case["name"], case["price"], multiplier, payout, now)
        )
    return {
        "ok": True, "case_id": case_id, "case_name": case["name"],
        "price": case["price"], "multiplier": multiplier,
        "payout": payout, "net": payout - case["price"],
        "balance": new_balance
    }


@app.post("/api/deposit")
async def deposit(body: Amount, request: Request):
    user = await auth(request)
    uid = int(user["id"])
    stars = body.amount
    if stars < MIN_DEPOSIT or stars > 10000:
        raise HTTPException(400, f"Минимальное пополнение: {MIN_DEPOSIT} ⭐. Максимум: 10000 ⭐.")
    if not BOT_TOKEN:
        raise HTTPException(503, "Пополнение пока не настроено.")
    payload = "dep_" + secrets.token_urlsafe(18)
    coins = stars * COINS_PER_STAR
    with connect() as con:
        con.execute("INSERT INTO invoices(payload,uid,stars,coins,created_at) VALUES(?,?,?,?,?)",
                    (payload, uid, stars, coins, int(time.time())))
    async with httpx.AsyncClient(timeout=12) as client:
        response = await client.post(telegram_api_url("sendInvoice"), json={
            "chat_id": uid, "title": "Звёзды NEON MINES",
            "description": f"{coins} игровых звёзд",
            "payload": payload, "currency": "XTR",
            "prices": [{"label": f"{coins} ⭐", "amount": stars}]
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
            "SELECT uid,stars FROM payments WHERE charge_id=?", (body.charge_id,)
        ).fetchone()
        if duplicate:
            if duplicate["uid"] != body.user_id or duplicate["stars"] != body.total_amount:
                raise HTTPException(409, "Идентификатор платежа уже использован.")
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


@app.post("/api/mines/start")
async def mines_start(body: dict, request: Request):
    user = await auth(request)
    uid = int(user["id"])
    bet = body.get("amount")
    mine_count = body.get("mines")
    if not isinstance(bet, int) or isinstance(bet, bool) or bet < 1 or bet > MAX_BET:
        raise HTTPException(400, "Некорректная ставка.")
    if not isinstance(mine_count, int) or isinstance(mine_count, bool) or not 1 <= mine_count <= 24:
        raise HTTPException(400, "Выбери от 1 до 24 мин.")
    mine_cells = sorted(secrets.SystemRandom().sample(range(25), mine_count))
    game_id = secrets.token_urlsafe(12)
    now = int(time.time())
    with connect() as con:
        con.execute("BEGIN IMMEDIATE")
        row = con.execute("SELECT bal FROM users WHERE id=?", (uid,)).fetchone()
        balance = int(row["bal"]) if row else 0
        if bet > balance:
            raise HTTPException(400, f"Недостаточно ⭐. Баланс: {balance}")
        con.execute("UPDATE users SET bal=bal-? WHERE id=?", (bet, uid))
        con.execute("""INSERT INTO mines_games(id,uid,bet,mine_count,mine_cells,revealed,status,created_at)
                       VALUES(?,?,?,?,?,"[]","active",?)""", (game_id, uid, bet, mine_count, json.dumps(mine_cells), now))
        new_balance = balance - bet
    return {"ok": True, "game_id": game_id, "bet": bet, "mines": mine_count, "revealed": [], "multiplier": 1.0, "cashout": 0, "balance": new_balance}

def mines_multiplier(mine_count: int, safe_picks: int) -> float:
    probability = 1.0
    for i in range(safe_picks):
        probability *= (25 - mine_count - i) / (25 - i)
    return round(GAME_MATH_CONFIG["mines"]["cashout_rtp"] / probability, 4)

@app.post("/api/mines/pick")
async def mines_pick(body: dict, request: Request):
    user = await auth(request)
    uid = int(user["id"])
    game_id = body.get("game_id")
    cell = body.get("cell")
    if not isinstance(game_id, str) or not isinstance(cell, int) or isinstance(cell, bool) or not 0 <= cell < 25:
        raise HTTPException(400, "Некорректный ход.")
    with connect() as con:
        con.execute("BEGIN IMMEDIATE")
        game = con.execute("SELECT * FROM mines_games WHERE id=? AND uid=?", (game_id, uid)).fetchone()
        if not game or game["status"] != "active":
            raise HTTPException(404, "Активная игра не найдена.")
        revealed = json.loads(game["revealed"])
        if cell in revealed:
            raise HTTPException(400, "Эта клетка уже открыта.")
        mines = json.loads(game["mine_cells"])
        if cell in mines:
            con.execute("UPDATE mines_games SET status='lost' WHERE id=?", (game_id,))
            con.execute("INSERT INTO games(uid,bet,payout,symbols,created_at) VALUES(?,?,0,?,?)", (uid, game["bet"], "💣 Мина", int(time.time())))
            row = con.execute("SELECT bal FROM users WHERE id=?", (uid,)).fetchone()
            return {"status":"lost", "cell":cell, "mines":mines, "revealed":revealed+[cell], "multiplier":0, "cashout":0, "balance":int(row["bal"]) if row else 0}
        revealed.append(cell)
        mult = mines_multiplier(int(game["mine_count"]), len(revealed))
        cashout = int(int(game["bet"]) * mult)
        con.execute("UPDATE mines_games SET revealed=? WHERE id=?", (json.dumps(revealed), game_id))
        row = con.execute("SELECT bal FROM users WHERE id=?", (uid,)).fetchone()
        balance = int(row["bal"]) if row else 0
    return {"status":"safe", "cell":cell, "revealed":revealed, "multiplier":mult, "cashout":cashout, "balance":balance}

@app.post("/api/mines/cashout")
async def mines_cashout(body: dict, request: Request):
    user = await auth(request)
    uid = int(user["id"])
    game_id = body.get("game_id")
    if not isinstance(game_id, str):
        raise HTTPException(400, "Игра не найдена.")
    with connect() as con:
        con.execute("BEGIN IMMEDIATE")
        game = con.execute("SELECT * FROM mines_games WHERE id=? AND uid=?", (game_id, uid)).fetchone()
        if not game or game["status"] != "active":
            raise HTTPException(404, "Активная игра не найдена.")
        revealed = json.loads(game["revealed"])
        if not revealed:
            raise HTTPException(400, "Сначала открой хотя бы одну клетку.")
        mult = mines_multiplier(int(game["mine_count"]), len(revealed))
        payout = int(int(game["bet"]) * mult)
        con.execute("UPDATE users SET bal=bal+? WHERE id=?", (payout, uid))
        con.execute("UPDATE mines_games SET status='cashed_out',payout=? WHERE id=?", (payout, game_id))
        con.execute("INSERT INTO games(uid,bet,payout,symbols,created_at) VALUES(?,?,?,?,?)", (uid, game["bet"], payout, f"💎 {len(revealed)} клеток • x{mult:.2f}", int(time.time())))
        row = con.execute("SELECT bal FROM users WHERE id=?", (uid,)).fetchone()
        balance = int(row["bal"]) if row else 0
    return {"status":"cashed_out", "payout":payout, "multiplier":mult, "balance":balance}

@app.post("/api/spin")
async def spin(body: Amount, request: Request):
    user = await auth(request)
    uid = int(user["id"])
    bet = body.amount
    if bet > MAX_BET:
        raise HTTPException(400, f"Максимальная ставка: {MAX_BET}.")
    symbols = ["🍒","🍋","🍇","🔔","7️⃣"]
    reels = [secrets.SystemRandom().choices(symbols, weights=GAME_MATH_CONFIG["classic_slots"]["legacy_spin_weights"], k=1)[0] for _ in range(3)]
    if reels == ["7️⃣","7️⃣","7️⃣"]:
        mult = GAME_MATH_CONFIG["classic_slots"]["jackpot_multiplier"]
    elif reels[0] == reels[1] == reels[2]:
        mult = GAME_MATH_CONFIG["classic_slots"]["triple_multiplier"]
    elif len(set(reels)) == 2:
        mult = GAME_MATH_CONFIG["classic_slots"]["pair_multiplier"]
    else:
        mult = 0
    payout = int(bet * mult * GAME_MATH_CONFIG["classic_slots"]["payout_factor"])
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
            raise HTTPException(400, f"Недостаточно ⭐. Баланс: {balance} ⭐")
        new_balance = balance - bet + payout
        con.execute("UPDATE users SET bal=? WHERE id=?", (new_balance,uid))
        con.execute("INSERT INTO games(uid,bet,payout,symbols,created_at) VALUES(?,?,?,?,?)",
                    (uid,bet,payout," ".join(reels),int(time.time())))
    outcome = "ВЫИГРЫШ" if payout else "ПРОИГРЫШ"
    await notify_admin(
        f"🎰 {outcome}\nИгрок: {display_user(user)}\nID: {uid}\n"
        f"Ставка: {bet} ⭐\nВыплата: {payout} ⭐\nКомбинация: {' '.join(reels)}\nБаланс: {new_balance}"
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
        raise HTTPException(400, f"Минимальная заявка: {MIN_WITHDRAW} ⭐.")
    with connect() as con:
        con.execute("BEGIN IMMEDIATE")
        row = con.execute("SELECT bal FROM users WHERE id=?", (uid,)).fetchone()
        balance = int(row["bal"]) if row else 0
        if amount > balance:
            raise HTTPException(400, f"Недостаточно ⭐. Баланс: {balance} ⭐")
        con.execute("UPDATE users SET bal=bal-? WHERE id=?", (amount,uid))
        cur = con.execute("INSERT INTO withdrawals(uid,amount,status,created_at) VALUES(?,?,'pending',?)",
                          (uid,amount,int(time.time())))
        wid = cur.lastrowid
        new_balance = balance - amount
    await notify_admin(
        f"📤 НОВАЯ ЗАЯВКА НА ВЫВОД #{wid}\n"
        f"Игрок: {display_user(user)}\nID: {uid}\nСумма: {amount} ⭐\n"
        f"Статус: ожидает решения\n\nКоманды: /approve {wid} или /reject {wid}"
    )
    return {"ok":True,"id":wid,"balance":new_balance,
            "message":f"Заявка #{wid} отправлена администратору. звёзды временно зарезервированы."}
