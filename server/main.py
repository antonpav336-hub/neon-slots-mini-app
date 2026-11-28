import os
import math
import secrets
import json
import base64
import binascii
from datetime import datetime, timedelta, timezone
from typing import Annotated

import jwt
import psycopg
from argon2 import PasswordHasher
from argon2.exceptions import VerifyMismatchError
from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from fastapi import Depends, FastAPI, Header, HTTPException, Query, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, EmailStr, Field
from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool

DATABASE_URL = os.environ["DATABASE_URL"]
JWT_SECRET = os.environ["JWT_SECRET"]
JWT_ALGORITHM = "HS256"
TOKEN_TTL_HOURS = int(os.getenv("TOKEN_TTL_HOURS", "168"))
REGISTRATION_BONUS_KOPECKS = int(os.getenv("REGISTRATION_BONUS_KOPECKS", "500000"))
ADMIN_ACCOUNT_IDS = {value.strip() for value in os.getenv("ADMIN_ACCOUNT_IDS", "").split(",") if value.strip()}

app = FastAPI(title="NEON WALLET API", version="1.1.0")
app.add_middleware(
    CORSMiddleware,
    allow_origins=os.getenv("CORS_ORIGINS", "*").split(","),
    allow_credentials=False,
    allow_methods=["GET", "POST"],
    allow_headers=["Authorization", "Content-Type", "Idempotency-Key"],
)

pool = ConnectionPool(DATABASE_URL, min_size=1, max_size=8, kwargs={"row_factory": dict_row})
password_hasher = PasswordHasher()


def initialize_database() -> None:
    with pool.connection() as conn:
        conn.execute("""
        CREATE TABLE IF NOT EXISTS users (
            id BIGSERIAL PRIMARY KEY,
            account_id VARCHAR(12) UNIQUE NOT NULL,
            username VARCHAR(32) UNIQUE NOT NULL,
            email VARCHAR(254) UNIQUE NOT NULL,
            password_hash TEXT NOT NULL,
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
        );
        CREATE TABLE IF NOT EXISTS wallets (
            user_id BIGINT PRIMARY KEY REFERENCES users(id) ON DELETE CASCADE,
            balance_kopecks BIGINT NOT NULL DEFAULT 0 CHECK (balance_kopecks >= 0),
            updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
        );
        CREATE TABLE IF NOT EXISTS transactions (
            id UUID PRIMARY KEY,
            idempotency_key VARCHAR(100) UNIQUE NOT NULL,
            sender_user_id BIGINT REFERENCES users(id),
            recipient_user_id BIGINT REFERENCES users(id),
            amount_kopecks BIGINT NOT NULL CHECK (amount_kopecks > 0),
            kind VARCHAR(24) NOT NULL,
            description VARCHAR(140) NOT NULL,
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
        );
        CREATE INDEX IF NOT EXISTS transactions_sender_created_idx
            ON transactions(sender_user_id, created_at DESC);
        CREATE INDEX IF NOT EXISTS transactions_recipient_created_idx
            ON transactions(recipient_user_id, created_at DESC);
        ALTER TABLE transactions ADD COLUMN IF NOT EXISTS admin_user_id BIGINT REFERENCES users(id);
        CREATE TABLE IF NOT EXISTS wallet_settings (
            user_id BIGINT PRIMARY KEY REFERENCES users(id) ON DELETE CASCADE,
            card_frozen BOOLEAN NOT NULL DEFAULT FALSE,
            card_limit_kopecks BIGINT NOT NULL DEFAULT 5000000 CHECK (card_limit_kopecks >= 0),
            updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
        );
        CREATE TABLE IF NOT EXISTS casino_wallets (
            user_id BIGINT PRIMARY KEY REFERENCES users(id) ON DELETE CASCADE,
            balance_kopecks BIGINT NOT NULL DEFAULT 0 CHECK (balance_kopecks >= 0),
            updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
        );
        CREATE TABLE IF NOT EXISTS casino_deposit_requests (
            id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            user_id BIGINT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            amount_kopecks BIGINT NOT NULL CHECK (amount_kopecks > 0),
            approval_nonce VARCHAR(128) NOT NULL,
            status VARCHAR(16) NOT NULL DEFAULT 'pending'
                CHECK (status IN ('pending', 'approved', 'rejected', 'expired')),
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            expires_at TIMESTAMPTZ NOT NULL DEFAULT (NOW() + INTERVAL '15 minutes'),
            processed_at TIMESTAMPTZ,
            transaction_id UUID
        );
        CREATE TABLE IF NOT EXISTS wallet_devices (
            user_id BIGINT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            device_id VARCHAR(64) NOT NULL,
            public_key_der BYTEA NOT NULL,
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            last_seen_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            PRIMARY KEY (user_id, device_id)
        );
        CREATE TABLE IF NOT EXISTS casino_crash_rounds (
            id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            user_id BIGINT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            bet_kopecks BIGINT NOT NULL CHECK (bet_kopecks > 0),
            crash_multiplier NUMERIC(10,2) NOT NULL CHECK (crash_multiplier >= 1.00),
            status VARCHAR(16) NOT NULL DEFAULT 'active'
                CHECK (status IN ('active', 'crashed', 'cashed_out')),
            started_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            finished_at TIMESTAMPTZ,
            cashout_multiplier NUMERIC(10,2),
            payout_kopecks BIGINT NOT NULL DEFAULT 0 CHECK (payout_kopecks >= 0)
        );
        CREATE INDEX IF NOT EXISTS casino_crash_user_history_idx
            ON casino_crash_rounds(user_id, started_at DESC);
        CREATE TABLE IF NOT EXISTS casino_mines_rounds (
            id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            user_id BIGINT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            bet_kopecks BIGINT NOT NULL CHECK (bet_kopecks > 0),
            mine_count SMALLINT NOT NULL CHECK (mine_count BETWEEN 1 AND 24),
            mine_cells SMALLINT[] NOT NULL,
            opened_cells SMALLINT[] NOT NULL DEFAULT '{}',
            status VARCHAR(16) NOT NULL DEFAULT 'active'
                CHECK (status IN ('active', 'lost', 'cashed_out', 'won')),
            payout_kopecks BIGINT NOT NULL DEFAULT 0 CHECK (payout_kopecks >= 0),
            started_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            finished_at TIMESTAMPTZ
        );
        CREATE INDEX IF NOT EXISTS casino_mines_user_history_idx
            ON casino_mines_rounds(user_id, started_at DESC);
        CREATE UNIQUE INDEX IF NOT EXISTS casino_mines_one_active_per_user_idx
            ON casino_mines_rounds(user_id) WHERE status = 'active';
        CREATE TABLE IF NOT EXISTS casino_ladder_rounds (
            id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            user_id BIGINT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            bet_kopecks BIGINT NOT NULL CHECK (bet_kopecks > 0),
            safe_sides SMALLINT[] NOT NULL,
            reached_steps SMALLINT NOT NULL DEFAULT 0 CHECK (reached_steps BETWEEN 0 AND 8),
            status VARCHAR(16) NOT NULL DEFAULT 'active'
                CHECK (status IN ('active', 'lost', 'cashed_out', 'won')),
            payout_kopecks BIGINT NOT NULL DEFAULT 0 CHECK (payout_kopecks >= 0),
            started_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            finished_at TIMESTAMPTZ
        );
        CREATE INDEX IF NOT EXISTS casino_ladder_user_history_idx
            ON casino_ladder_rounds(user_id, started_at DESC);
        CREATE UNIQUE INDEX IF NOT EXISTS casino_ladder_one_active_per_user_idx
            ON casino_ladder_rounds(user_id) WHERE status = 'active';
        CREATE TABLE IF NOT EXISTS casino_blackjack_rounds (
            id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            user_id BIGINT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            bet_kopecks BIGINT NOT NULL CHECK (bet_kopecks > 0),
            deck JSONB NOT NULL,
            deck_index SMALLINT NOT NULL DEFAULT 0,
            player_cards JSONB NOT NULL,
            dealer_cards JSONB NOT NULL,
            doubled BOOLEAN NOT NULL DEFAULT FALSE,
            status VARCHAR(16) NOT NULL DEFAULT 'active'
                CHECK (status IN ('active', 'won', 'lost', 'push', 'blackjack')),
            payout_kopecks BIGINT NOT NULL DEFAULT 0 CHECK (payout_kopecks >= 0),
            started_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            finished_at TIMESTAMPTZ
        );
        CREATE INDEX IF NOT EXISTS casino_blackjack_user_history_idx
            ON casino_blackjack_rounds(user_id, started_at DESC);
        CREATE UNIQUE INDEX IF NOT EXISTS casino_blackjack_one_active_per_user_idx
            ON casino_blackjack_rounds(user_id) WHERE status = 'active';
        CREATE TABLE IF NOT EXISTS casino_plinko_rounds (
            id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            user_id BIGINT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            request_key VARCHAR(64) NOT NULL,
            bet_kopecks BIGINT NOT NULL CHECK (bet_kopecks > 0),
            path SMALLINT[] NOT NULL CHECK (array_length(path, 1) = 12),
            bucket SMALLINT NOT NULL CHECK (bucket BETWEEN 0 AND 12),
            multiplier NUMERIC(6,2) NOT NULL CHECK (multiplier > 0),
            payout_kopecks BIGINT NOT NULL DEFAULT 0 CHECK (payout_kopecks >= 0),
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
        );
        CREATE UNIQUE INDEX IF NOT EXISTS casino_plinko_request_key_idx
            ON casino_plinko_rounds(user_id, request_key);
        CREATE INDEX IF NOT EXISTS casino_plinko_user_history_idx
            ON casino_plinko_rounds(user_id, created_at DESC);
        CREATE TABLE IF NOT EXISTS casino_roulette_rounds (
            id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            user_id BIGINT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            bets JSONB NOT NULL,
            request_key VARCHAR(64) NOT NULL,
            winning_number SMALLINT NOT NULL CHECK (winning_number BETWEEN 0 AND 36),
            total_bet_kopecks BIGINT NOT NULL CHECK (total_bet_kopecks > 0),
            payout_kopecks BIGINT NOT NULL DEFAULT 0 CHECK (payout_kopecks >= 0),
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
        );
        ALTER TABLE casino_roulette_rounds
            ADD COLUMN IF NOT EXISTS request_key VARCHAR(64);
        UPDATE casino_roulette_rounds SET request_key = gen_random_uuid()::text
            WHERE request_key IS NULL;
        ALTER TABLE casino_roulette_rounds ALTER COLUMN request_key SET NOT NULL;
        CREATE UNIQUE INDEX IF NOT EXISTS casino_roulette_request_key_idx
            ON casino_roulette_rounds(user_id, request_key);
        CREATE INDEX IF NOT EXISTS casino_roulette_user_history_idx
            ON casino_roulette_rounds(user_id, created_at DESC);
        CREATE UNIQUE INDEX IF NOT EXISTS casino_crash_one_active_per_user_idx
            ON casino_crash_rounds(user_id) WHERE status = 'active';
        ALTER TABLE casino_deposit_requests
            ADD COLUMN IF NOT EXISTS approval_nonce VARCHAR(128);
        UPDATE casino_deposit_requests SET approval_nonce = gen_random_uuid()::text
            WHERE approval_nonce IS NULL;
        ALTER TABLE casino_deposit_requests
            ALTER COLUMN approval_nonce SET NOT NULL;
        CREATE INDEX IF NOT EXISTS casino_deposit_user_status_idx
            ON casino_deposit_requests(user_id, status, created_at DESC);
        """)
        conn.commit()


@app.on_event("startup")
def startup() -> None:
    initialize_database()


@app.on_event("shutdown")
def shutdown() -> None:
    pool.close()


class RegisterRequest(BaseModel):
    username: str = Field(min_length=3, max_length=32, pattern=r"^[a-zA-Z0-9_]+$")
    email: EmailStr
    password: str = Field(min_length=10, max_length=128)


class LoginRequest(BaseModel):
    email: EmailStr
    password: str = Field(min_length=1, max_length=128)


class TransferRequest(BaseModel):
    recipient_account_id: str = Field(min_length=6, max_length=12, pattern=r"^[0-9]+$")
    amount_kopecks: int = Field(gt=0, le=100_000_000)
    description: str = Field(default="Перевод", min_length=1, max_length=140)


class AdminCreditRequest(BaseModel):
    account_id: str = Field(min_length=6, max_length=12, pattern=r"^[0-9]+$")
    amount_kopecks: int = Field(gt=0, le=100_000_000)
    description: str = Field(default="Административное пополнение", min_length=1, max_length=140)
    request_key: str = Field(min_length=16, max_length=64, pattern=r"^[a-zA-Z0-9_-]+$")


def issue_token(user_id: int) -> str:
    now = datetime.now(timezone.utc)
    return jwt.encode(
        {"sub": str(user_id), "iat": now, "exp": now + timedelta(hours=TOKEN_TTL_HOURS)},
        JWT_SECRET,
        algorithm=JWT_ALGORITHM,
    )


def current_user(authorization: Annotated[str | None, Header()] = None) -> dict:
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Требуется авторизация")
    try:
        payload = jwt.decode(authorization[7:], JWT_SECRET, algorithms=[JWT_ALGORITHM])
        user_id = int(payload["sub"])
    except (jwt.PyJWTError, KeyError, ValueError):
        raise HTTPException(status_code=401, detail="Сессия недействительна или истекла")
    with pool.connection() as conn:
        user = conn.execute(
            "SELECT id, account_id, username, email, created_at FROM users WHERE id = %s",
            (user_id,),
        ).fetchone()
    if not user:
        raise HTTPException(status_code=401, detail="Пользователь не найден")
    return user


User = Annotated[dict, Depends(current_user)]


def require_admin(user: User) -> dict:
    if not ADMIN_ACCOUNT_IDS or user["account_id"] not in ADMIN_ACCOUNT_IDS:
        raise HTTPException(status_code=403, detail="Нет доступа администратора")
    return user


AdminUser = Annotated[dict, Depends(require_admin)]


def public_user(user: dict) -> dict:
    return {
        "account_id": user["account_id"],
        "username": user["username"],
        "email": user["email"],
        "created_at": user["created_at"].isoformat(),
    }


@app.get("/admin", response_class=HTMLResponse)
def admin_panel():
    return HTMLResponse(r"""
<!doctype html><html lang="ru"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><meta name="theme-color" content="#090711"><title>NEON WALLET Admin</title>
<style>
:root{color-scheme:dark;--bg:#090711;--panel:#141020;--line:#302342;--purple:#a855f7;--pink:#ec4899;--muted:#a79bb8;--text:#f7f1ff}*{box-sizing:border-box}body{margin:0;background:radial-gradient(ellipse at 50% -20%,#39205b 0,transparent 55%),var(--bg);color:var(--text);font:15px Inter,system-ui,sans-serif;min-height:100vh;padding:28px 16px}main{width:min(760px,100%);margin:0 auto}.brand{font-weight:900;letter-spacing:3px;color:#d9b8ff;font-size:13px}.brand span{color:var(--pink)}h1{font-size:30px;margin:12px 0 6px}.sub{color:var(--muted);margin:0 0 24px}.panel{background:linear-gradient(145deg,#191326,#100d19);border:1px solid var(--line);border-radius:20px;padding:22px;margin:14px 0;box-shadow:0 16px 50px #0005}.panel h2{font-size:17px;margin:0 0 16px}.field{display:grid;gap:7px;margin:13px 0}.field label{font-size:13px;color:#c9bdd7}.field input{width:100%;height:48px;border-radius:12px;border:1px solid #443455;background:#0b0911;color:white;padding:0 14px;font-size:16px;outline:none}.field input:focus{border-color:var(--purple);box-shadow:0 0 0 3px #a855f722}button{height:48px;border:0;border-radius:12px;padding:0 18px;background:linear-gradient(100deg,#9333ea,#db2777);color:white;font-weight:800;font-size:15px;width:100%;cursor:pointer;margin-top:8px}button:disabled{opacity:.55;cursor:wait}.muted{color:var(--muted);font-size:13px}.status{padding:12px;border-radius:12px;background:#21182c;margin-top:14px;display:none;overflow-wrap:anywhere}.status.error,.status.ok{display:block}.status.error{color:#ff9eae;background:#351521}.status.ok{color:#b8ffd9;background:#123025}.hidden{display:none!important}.user{border:1px solid #3b2b4d;border-radius:14px;padding:15px;margin-top:15px}.balance{font-size:26px;font-weight:900;color:#d9b8ff;margin:8px 0}.row{display:flex;justify-content:space-between;gap:12px;flex-wrap:wrap}@media(max-width:520px){body{padding:20px 12px}.panel{padding:17px}h1{font-size:26px}}
</style></head><body><main><div class="brand">NEON <span>WALLET</span> / ADMIN</div><h1>Панель управления</h1><p class="sub">Администрирование виртуальных балансов и операций.</p>
<section class="panel" id="loginPanel"><h2>Вход администратора</h2><div class="field"><label for="email">Email аккаунта</label><input id="email" type="email" autocomplete="username" placeholder="name@example.com"></div><div class="field"><label for="password">Пароль кошелька</label><input id="password" type="password" autocomplete="current-password"></div><button id="loginBtn">Войти</button><div class="status" id="loginStatus" role="status"></div><p class="muted">Пароль проверяется сервером и не сохраняется в панели.</p></section>
<section class="panel hidden" id="adminPanel"><div class="row"><h2>Аккаунты</h2><button id="logoutBtn" style="width:auto;margin:0;background:#30243c">Выйти</button></div><div class="field"><label for="accountId">ID аккаунта или email</label><input id="accountId" maxlength="254" placeholder="ID или email"></div><button id="searchBtn">Найти аккаунт</button><div class="status" id="searchStatus" role="status"></div>
<div class="user hidden" id="userCard"><div class="row"><strong id="userName"></strong><span class="muted" id="userEmail"></span></div><div class="muted" id="userId"></div><div class="balance" id="userBalance">—</div><h2>Зачислить виртуальные ₽</h2><div class="field"><label for="amount">Сумма в рублях</label><input id="amount" type="number" min="0.01" max="1000000" step="0.01" placeholder="100000"></div><div class="field"><label for="reason">Комментарий операции</label><input id="reason" maxlength="140" value="Административное пополнение"></div><button id="creditBtn">Зачислить на баланс</button><div class="status" id="creditStatus" role="status"></div></div></section><p class="muted">Все суммы — виртуальные тестовые средства. Каждое зачисление записывается в историю операций.</p></main>
<script>
const $=id=>document.getElementById(id);let token=sessionStorage.getItem("neon_admin_token")||"",selected=null;
function status(id,msg,kind){const el=$(id);el.textContent=msg;el.className="status "+kind}
async function api(path,options={}){const res=await fetch(path,{...options,headers:{"Content-Type":"application/json","Authorization":"Bearer "+token,...(options.headers||{})}});const data=await res.json().catch(()=>({detail:"Ошибка ответа сервера"}));if(!res.ok)throw new Error(data.detail||"Ошибка запроса");return data}
function showAdmin(){$("loginPanel").classList.add("hidden");$("adminPanel").classList.remove("hidden")}
function showLogin(){$("adminPanel").classList.add("hidden");$("loginPanel").classList.remove("hidden")}
async function verify(){if(!token)return showLogin();try{await api("/v1/admin/me");showAdmin()}catch{token="";sessionStorage.removeItem("neon_admin_token");showLogin()}}
$("loginBtn").onclick=async()=>{const btn=$("loginBtn");btn.disabled=true;try{const r=await fetch("/v1/auth/login",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({email:$("email").value,password:$("password").value})});const d=await r.json();if(!r.ok)throw new Error(d.detail||"Не удалось войти");token=d.access_token;sessionStorage.setItem("neon_admin_token",token);$("password").value="";await api("/v1/admin/me");showAdmin()}catch(e){token="";sessionStorage.removeItem("neon_admin_token");status("loginStatus",e.message,"error")}finally{btn.disabled=false}};
$("logoutBtn").onclick=()=>{token="";sessionStorage.removeItem("neon_admin_token");selected=null;$("userCard").classList.add("hidden");showLogin()};
$("searchBtn").onclick=async()=>{const btn=$("searchBtn");btn.disabled=true;try{const q=$("accountId").value.trim();const r=await api("/v1/admin/users?q="+encodeURIComponent(q));if(!r.items.length)throw new Error("Аккаунт не найден");selected=r.items[0];$("userName").textContent=selected.username;$("userEmail").textContent=selected.email;$("userId").textContent="ID: "+selected.account_id;$("userBalance").textContent=(selected.balance_kopecks/100).toLocaleString("ru-RU",{minimumFractionDigits:2})+" ₽";$("userCard").classList.remove("hidden");status("searchStatus","Аккаунт найден","ok")}catch(e){selected=null;$("userCard").classList.add("hidden");status("searchStatus",e.message,"error")}finally{btn.disabled=false}};
$("creditBtn").onclick=async()=>{if(!selected)return;const amount=Number($("amount").value);if(!Number.isFinite(amount)||amount<=0||amount>1000000)return status("creditStatus","Укажи сумму от 0,01 до 1 000 000 ₽","error");if(!confirm("Зачислить "+amount.toLocaleString("ru-RU")+" ₽ на аккаунт "+selected.account_id+"?"))return;const btn=$("creditBtn");btn.disabled=true;try{const r=await api("/v1/admin/wallet/credit",{method:"POST",body:JSON.stringify({account_id:selected.account_id,amount_kopecks:Math.round(amount*100),description:$("reason").value.trim()||"Административное пополнение",request_key:crypto.randomUUID().replaceAll("-","")})});$("userBalance").textContent=(r.balance_kopecks/100).toLocaleString("ru-RU",{minimumFractionDigits:2})+" ₽";status("creditStatus","Зачислено: "+(r.amount_kopecks/100).toLocaleString("ru-RU")+" ₽. Операция № "+r.transaction_id,"ok")}catch(e){status("creditStatus",e.message,"error")}finally{btn.disabled=false}};
verify();
</script></body></html>
""")


@app.get("/v1/admin/me")
def admin_me(user: AdminUser):
    return {"admin": True, "account_id": user["account_id"], "username": user["username"]}


@app.get("/v1/admin/users")
def admin_search_users(q: str, user: AdminUser):
    query = q.strip()
    if not query or len(query) > 254:
        raise HTTPException(status_code=400, detail="Укажи ID или email аккаунта")
    with pool.connection() as conn:
        rows = conn.execute(
            """SELECT u.account_id, u.username, u.email, w.balance_kopecks
               FROM users u JOIN wallets w ON w.user_id = u.id
               WHERE u.account_id = %s OR LOWER(u.email) = LOWER(%s)
               LIMIT 10""",
            (query, query),
        ).fetchall()
    return {"items": [{
        "account_id": row["account_id"], "username": row["username"],
        "email": row["email"], "balance_kopecks": row["balance_kopecks"],
    } for row in rows]}


@app.post("/v1/admin/wallet/credit", status_code=201)
def admin_credit_wallet(body: AdminCreditRequest, admin: AdminUser):
    idempotency_key = f"admin-credit:{admin['id']}:{body.request_key}"
    with pool.connection() as conn:
        with conn.transaction():
            existing = conn.execute(
                """SELECT id, recipient_user_id, amount_kopecks, created_at
                   FROM transactions WHERE idempotency_key = %s""",
                (idempotency_key,),
            ).fetchone()
            if existing:
                if existing["recipient_user_id"] is None or existing["amount_kopecks"] != body.amount_kopecks:
                    raise HTTPException(status_code=409, detail="Ключ операции уже использован с другими параметрами")
                target = conn.execute(
                    "SELECT account_id FROM users WHERE id = %s", (existing["recipient_user_id"],)
                ).fetchone()
                if not target or target["account_id"] != body.account_id:
                    raise HTTPException(status_code=409, detail="Ключ операции уже использован для другого аккаунта")
                balance = conn.execute(
                    "SELECT balance_kopecks FROM wallets WHERE user_id = %s",
                    (existing["recipient_user_id"],),
                ).fetchone()
                return {"transaction_id": str(existing["id"]), "status": "completed",
                        "amount_kopecks": existing["amount_kopecks"], "balance_kopecks": balance["balance_kopecks"],
                        "created_at": existing["created_at"].isoformat(), "replayed": True}
            target = conn.execute(
                "SELECT id, account_id FROM users WHERE account_id = %s", (body.account_id,)
            ).fetchone()
            if not target:
                raise HTTPException(status_code=404, detail="Аккаунт не найден")
            conn.execute(
                "INSERT INTO wallets(user_id, balance_kopecks) VALUES (%s, 0) ON CONFLICT (user_id) DO NOTHING",
                (target["id"],),
            )
            conn.execute("SELECT user_id FROM wallets WHERE user_id = %s FOR UPDATE", (target["id"],))
            wallet = conn.execute(
                """UPDATE wallets SET balance_kopecks = balance_kopecks + %s, updated_at = NOW()
                   WHERE user_id = %s RETURNING balance_kopecks""",
                (body.amount_kopecks, target["id"]),
            ).fetchone()
            tx = conn.execute(
                """INSERT INTO transactions(id, idempotency_key, recipient_user_id, admin_user_id,
                   amount_kopecks, kind, description)
                   VALUES (gen_random_uuid(), %s, %s, %s, %s, 'admin_credit', %s)
                   RETURNING id, created_at""",
                (idempotency_key, target["id"], admin["id"], body.amount_kopecks, body.description),
            ).fetchone()
    return {"transaction_id": str(tx["id"]), "status": "completed",
            "amount_kopecks": body.amount_kopecks, "balance_kopecks": wallet["balance_kopecks"],
            "created_at": tx["created_at"].isoformat(), "replayed": False}


@app.get("/health")
def health():
    with pool.connection() as conn:
        conn.execute("SELECT 1")
    return {"status": "ok", "service": "neon-wallet-api"}


@app.post("/v1/auth/register", status_code=201)
def register(body: RegisterRequest):
    password_hash = password_hasher.hash(body.password)
    with pool.connection() as conn:
        with conn.transaction():
            for _ in range(10):
                account_id = str(secrets.randbelow(900_000_000) + 100_000_000)
                exists = conn.execute("SELECT 1 FROM users WHERE account_id = %s", (account_id,)).fetchone()
                if not exists:
                    break
            else:
                raise HTTPException(status_code=503, detail="Не удалось создать ID аккаунта")
            try:
                user = conn.execute(
                    """INSERT INTO users(account_id, username, email, password_hash)
                       VALUES (%s, %s, %s, %s)
                       RETURNING id, account_id, username, email, created_at""",
                    (account_id, body.username, body.email.lower(), password_hash),
                ).fetchone()
                conn.execute(
                    "INSERT INTO wallets(user_id, balance_kopecks) VALUES (%s, %s)",
                    (user["id"], REGISTRATION_BONUS_KOPECKS),
                )
                conn.execute(
                    "INSERT INTO casino_wallets(user_id, balance_kopecks) VALUES (%s, 0)",
                    (user["id"],),
                )
                conn.execute(
                    """INSERT INTO transactions(id, idempotency_key, recipient_user_id,
                       amount_kopecks, kind, description)
                       VALUES (gen_random_uuid(), %s, %s, %s, 'signup_bonus', 'Бонус за регистрацию')""",
                    (f"signup:{user['id']}", user["id"], REGISTRATION_BONUS_KOPECKS),
                )
            except psycopg.errors.UniqueViolation:
                raise HTTPException(status_code=409, detail="Имя пользователя или email уже заняты")
    return {
        "access_token": issue_token(user["id"]),
        "token_type": "bearer",
        "user": public_user(user),
        "wallet": {"balance_kopecks": REGISTRATION_BONUS_KOPECKS},
    }


@app.post("/v1/auth/login")
def login(body: LoginRequest):
    with pool.connection() as conn:
        user = conn.execute(
            """SELECT id, account_id, username, email, password_hash, created_at
               FROM users WHERE email = %s""",
            (body.email.lower(),),
        ).fetchone()
    if not user:
        raise HTTPException(status_code=401, detail="Неверный email или пароль")
    try:
        password_hasher.verify(user["password_hash"], body.password)
    except VerifyMismatchError:
        raise HTTPException(status_code=401, detail="Неверный email или пароль")
    return {
        "access_token": issue_token(user["id"]),
        "token_type": "bearer",
        "user": public_user(user),
    }


@app.get("/v1/me")
def me(user: User):
    return {"user": public_user(user)}


@app.get("/v1/recipients/search")
def search_recipients(user: User, q: str = Query(min_length=3, max_length=32)):
    query = q.strip()
    if not query:
        return {"items": []}
    with pool.connection() as conn:
        rows = conn.execute(
            """SELECT account_id, username FROM users
               WHERE id <> %s AND (account_id = %s OR username ILIKE %s)
               ORDER BY CASE WHEN account_id = %s THEN 0 ELSE 1 END, username
               LIMIT 10""",
            (user["id"], query, query + "%", query),
        ).fetchall()
    return {"items": [{"account_id": row["account_id"], "username": row["username"]} for row in rows]}


@app.get("/v1/wallet")
def wallet(user: User):
    with pool.connection() as conn:
        row = conn.execute(
            "SELECT balance_kopecks, updated_at FROM wallets WHERE user_id = %s",
            (user["id"],),
        ).fetchone()
    return {
        "account_id": user["account_id"],
        "currency": "VIRTUAL_RUB",
        "balance_kopecks": row["balance_kopecks"],
        "updated_at": row["updated_at"].isoformat(),
    }

class CasinoWithdrawRequest(BaseModel):
    amount_kopecks: int = Field(gt=0, le=100_000_000)
    request_key: str = Field(min_length=16, max_length=100, pattern=r"^[a-zA-Z0-9_-]+$")


class CasinoDepositCreateRequest(BaseModel):
    amount_kopecks: int = Field(gt=0, le=100_000_000)


@app.get("/v1/casino/wallet")
def casino_wallet(user: User):
    with pool.connection() as conn:
        conn.execute(
            "INSERT INTO casino_wallets(user_id, balance_kopecks) VALUES (%s, 0) ON CONFLICT (user_id) DO NOTHING",
            (user["id"],),
        )
        row = conn.execute(
            "SELECT balance_kopecks, updated_at FROM casino_wallets WHERE user_id = %s",
            (user["id"],),
        ).fetchone()
    return {
        "currency": "VIRTUAL_RUB",
        "balance_kopecks": row["balance_kopecks"],
        "updated_at": row["updated_at"].isoformat(),
    }


@app.post("/v1/casino/deposit-requests", status_code=201)
def create_casino_deposit_request(body: CasinoDepositCreateRequest, user: User):
    with pool.connection() as conn:
        with conn.transaction():
            conn.execute(
                "INSERT INTO casino_wallets(user_id, balance_kopecks) VALUES (%s, 0) ON CONFLICT (user_id) DO NOTHING",
                (user["id"],),
            )
            # Lock the per-user casino wallet row so simultaneous requests cannot
            # both observe an empty pending-request set and create duplicates.
            conn.execute(
                "SELECT user_id FROM casino_wallets WHERE user_id = %s FOR UPDATE",
                (user["id"],),
            )
            existing = conn.execute(
                """SELECT id FROM casino_deposit_requests
                   WHERE user_id = %s AND status = 'pending' AND expires_at > NOW()
                   ORDER BY created_at DESC LIMIT 1 FOR UPDATE""",
                (user["id"],),
            ).fetchone()
            if existing:
                raise HTTPException(status_code=409, detail="У тебя уже есть ожидающий запрос. Подтверди или отклони его в NEON WALLET.")
            approval_nonce = secrets.token_urlsafe(32)
            row = conn.execute(
                """INSERT INTO casino_deposit_requests(user_id, amount_kopecks, approval_nonce)
                   VALUES (%s, %s, %s)
                   RETURNING id, amount_kopecks, status, created_at, expires_at""",
                (user["id"], body.amount_kopecks, approval_nonce),
            ).fetchone()
    return {
        "request_id": str(row["id"]),
        "amount_kopecks": row["amount_kopecks"],
        "status": row["status"],
        "created_at": row["created_at"].isoformat(),
        "expires_at": row["expires_at"].isoformat(),
        "message": "Открой NEON WALLET и подтверди запрос на перевод.",
    }


@app.get("/v1/casino/deposit-requests/pending")
def pending_casino_deposits(user: User):
    with pool.connection() as conn:
        conn.execute(
            """UPDATE casino_deposit_requests SET status = 'expired', processed_at = NOW()
               WHERE user_id = %s AND status = 'pending' AND expires_at <= NOW()""",
            (user["id"],),
        )
        rows = conn.execute(
            """SELECT id, amount_kopecks, status, created_at, expires_at, approval_nonce
               FROM casino_deposit_requests
               WHERE user_id = %s AND status = 'pending' AND expires_at > NOW()
               ORDER BY created_at ASC""",
            (user["id"],),
        ).fetchall()
    return {"items": [{
        "request_id": str(row["id"]),
        "amount_kopecks": row["amount_kopecks"],
        "status": row["status"],
        "created_at": row["created_at"].isoformat(),
        "expires_at": row["expires_at"].isoformat(),
        "approval_challenge": row["approval_nonce"],
        "sender": "NEON CASINO",
    } for row in rows]}


@app.get("/v1/casino/deposit-requests/{request_id}")
def get_casino_deposit_request(request_id: str, user: User):
    with pool.connection() as conn:
        row = conn.execute(
            """SELECT id, amount_kopecks, status, created_at, expires_at, processed_at
               FROM casino_deposit_requests WHERE id = %s::uuid AND user_id = %s""",
            (request_id, user["id"]),
        ).fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="Запрос на пополнение не найден")
    return {
        "request_id": str(row["id"]),
        "amount_kopecks": row["amount_kopecks"],
        "status": row["status"],
        "created_at": row["created_at"].isoformat(),
        "expires_at": row["expires_at"].isoformat(),
        "processed_at": row["processed_at"].isoformat() if row["processed_at"] else None,
    }



class CrashStartRequest(BaseModel):
    bet_kopecks: int = Field(gt=0, le=100_000_000)


def crash_multiplier_for_elapsed(started_at: datetime) -> float:
    elapsed = max(0.0, (datetime.now(timezone.utc) - started_at).total_seconds())
    return min(100.0, math.exp((elapsed / 12.0) * math.log(2.0)))


def create_crash_point() -> float:
    # Server-only crash point with a 1% edge and a 100x cap.
    roll = secrets.randbelow(10_000_000)
    value = min(100.0, max(1.0, (0.99 * 10_000_000) / (10_000_000 - roll)))
    return math.floor(value * 100) / 100


def crash_round_payload(row: dict, now_multiplier: float | None = None) -> dict:
    multiplier = now_multiplier if now_multiplier is not None else float(row["crash_multiplier"])
    return {
        "round_id": str(row["id"]),
        "status": row["status"],
        "bet_kopecks": row["bet_kopecks"],
        "current_multiplier": round(multiplier, 2),
        "final_multiplier": float(row["cashout_multiplier"]) if row["cashout_multiplier"] is not None else (float(row["crash_multiplier"]) if row["status"] == "crashed" else None),
        "payout_kopecks": row["payout_kopecks"],
        "started_at": row["started_at"].isoformat(),
        "finished_at": row["finished_at"].isoformat() if row["finished_at"] else None,
    }


@app.post("/v1/casino/crash/start", status_code=201)
def start_crash_round(body: CrashStartRequest, user: User):
    with pool.connection() as conn:
        with conn.transaction():
            conn.execute(
                "INSERT INTO casino_wallets(user_id, balance_kopecks) VALUES (%s, 0) ON CONFLICT (user_id) DO NOTHING",
                (user["id"],),
            )
            wallet = conn.execute(
                "SELECT balance_kopecks FROM casino_wallets WHERE user_id = %s FOR UPDATE",
                (user["id"],),
            ).fetchone()
            active = conn.execute(
                "SELECT id FROM casino_crash_rounds WHERE user_id = %s AND status = 'active'",
                (user["id"],),
            ).fetchone()
            if active:
                raise HTTPException(status_code=409, detail="У тебя уже идёт раунд CRASH")
            active_mines = conn.execute(
                "SELECT id FROM casino_mines_rounds WHERE user_id = %s AND status = 'active'",
                (user["id"],),
            ).fetchone()
            if active_mines:
                raise HTTPException(status_code=409, detail="Сначала заверши активный раунд MINES")
            active_ladder = conn.execute(
                "SELECT id FROM casino_ladder_rounds WHERE user_id = %s AND status = 'active'",
                (user["id"],),
            ).fetchone()
            if active_ladder:
                raise HTTPException(status_code=409, detail="Сначала заверши активную ЛЕСЕНКУ")
            active_blackjack = conn.execute(
                "SELECT id FROM casino_blackjack_rounds WHERE user_id = %s AND status = 'active'",
                (user["id"],),
            ).fetchone()
            if active_blackjack:
                raise HTTPException(status_code=409, detail="Сначала заверши активный BLACKJACK")
            if wallet["balance_kopecks"] < body.bet_kopecks:
                raise HTTPException(status_code=409, detail="Недостаточно средств на балансе казино")
            conn.execute(
                "UPDATE casino_wallets SET balance_kopecks = balance_kopecks - %s, updated_at = NOW() WHERE user_id = %s",
                (body.bet_kopecks, user["id"]),
            )
            row = conn.execute(
                """INSERT INTO casino_crash_rounds(user_id, bet_kopecks, crash_multiplier)
                   VALUES (%s, %s, %s)
                   RETURNING id, user_id, bet_kopecks, crash_multiplier, status,
                             started_at, finished_at, cashout_multiplier, payout_kopecks""",
                (user["id"], body.bet_kopecks, create_crash_point()),
            ).fetchone()
    return {
        "round_id": str(row["id"]),
        "status": "active",
        "bet_kopecks": row["bet_kopecks"],
        "current_multiplier": 1.00,
        "started_at": row["started_at"].isoformat(),
        "message": "Раунд начался",
    }



@app.get("/v1/casino/crash-active")
def get_active_crash_round(user: User):
    with pool.connection() as conn:
        with conn.transaction():
            row = conn.execute(
                """SELECT id, user_id, bet_kopecks, crash_multiplier, status, started_at,
                          finished_at, cashout_multiplier, payout_kopecks
                   FROM casino_crash_rounds WHERE user_id = %s AND status = 'active'
                   ORDER BY started_at DESC LIMIT 1 FOR UPDATE""",
                (user["id"],),
            ).fetchone()
            if not row:
                return {"round": None}
            current = crash_multiplier_for_elapsed(row["started_at"])
            if current >= float(row["crash_multiplier"]):
                row = conn.execute(
                    """UPDATE casino_crash_rounds SET status = 'crashed', finished_at = NOW()
                       WHERE id = %s RETURNING id, user_id, bet_kopecks, crash_multiplier, status,
                         started_at, finished_at, cashout_multiplier, payout_kopecks""",
                    (row["id"],),
                ).fetchone()
                return {"round": crash_round_payload(row)}
            return {"round": crash_round_payload(row, current)}


@app.get("/v1/casino/crash/{round_id}")
def get_crash_round(round_id: str, user: User):
    with pool.connection() as conn:
        with conn.transaction():
            row = conn.execute(
                """SELECT id, user_id, bet_kopecks, crash_multiplier, status, started_at,
                          finished_at, cashout_multiplier, payout_kopecks
                   FROM casino_crash_rounds WHERE id = %s::uuid AND user_id = %s FOR UPDATE""",
                (round_id, user["id"]),
            ).fetchone()
            if not row:
                raise HTTPException(status_code=404, detail="Раунд CRASH не найден")
            current = crash_multiplier_for_elapsed(row["started_at"])
            if row["status"] == "active" and current >= float(row["crash_multiplier"]):
                row = conn.execute(
                    """UPDATE casino_crash_rounds SET status = 'crashed', finished_at = NOW()
                       WHERE id = %s RETURNING id, user_id, bet_kopecks, crash_multiplier, status,
                         started_at, finished_at, cashout_multiplier, payout_kopecks""",
                    (row["id"],),
                ).fetchone()
            elif row["status"] == "active":
                return crash_round_payload(row, current)
    return crash_round_payload(row)


@app.post("/v1/casino/crash/{round_id}/cashout")
def cashout_crash_round(round_id: str, user: User):
    with pool.connection() as conn:
        with conn.transaction():
            row = conn.execute(
                """SELECT id, user_id, bet_kopecks, crash_multiplier, status, started_at,
                          finished_at, cashout_multiplier, payout_kopecks
                   FROM casino_crash_rounds WHERE id = %s::uuid AND user_id = %s FOR UPDATE""",
                (round_id, user["id"]),
            ).fetchone()
            if not row:
                raise HTTPException(status_code=404, detail="Раунд CRASH не найден")
            if row["status"] != "active":
                raise HTTPException(status_code=409, detail="Раунд уже завершён")
            current = crash_multiplier_for_elapsed(row["started_at"])
            if current >= float(row["crash_multiplier"]):
                row = conn.execute(
                    """UPDATE casino_crash_rounds SET status = 'crashed', finished_at = NOW()
                       WHERE id = %s RETURNING id, user_id, bet_kopecks, crash_multiplier, status,
                         started_at, finished_at, cashout_multiplier, payout_kopecks""",
                    (row["id"],),
                ).fetchone()
                result = crash_round_payload(row)
                result["message"] = "Не успел забрать — раунд завершился"
            else:
                multiplier = math.floor(current * 100) / 100
                payout = int(row["bet_kopecks"] * multiplier)
                conn.execute(
                    "UPDATE casino_wallets SET balance_kopecks = balance_kopecks + %s, updated_at = NOW() WHERE user_id = %s",
                    (payout, user["id"]),
                )
                row = conn.execute(
                    """UPDATE casino_crash_rounds SET status = 'cashed_out', finished_at = NOW(),
                         cashout_multiplier = %s, payout_kopecks = %s
                       WHERE id = %s RETURNING id, user_id, bet_kopecks, crash_multiplier, status,
                         started_at, finished_at, cashout_multiplier, payout_kopecks""",
                    (multiplier, payout, row["id"]),
                ).fetchone()
                result = crash_round_payload(row)
                result["message"] = "Выигрыш зачислен на баланс казино"
    return result


@app.get("/v1/casino/crash-history")
def crash_history(user: User, limit: int = 8):
    limit = max(1, min(limit, 30))
    with pool.connection() as conn:
        rows = conn.execute(
            """SELECT id, bet_kopecks, status, crash_multiplier, cashout_multiplier,
                      payout_kopecks, started_at
               FROM casino_crash_rounds WHERE user_id = %s
               ORDER BY started_at DESC LIMIT %s""",
            (user["id"], limit),
        ).fetchall()
    return {"items": [{
        "round_id": str(row["id"]),
        "status": row["status"],
        "bet_kopecks": row["bet_kopecks"],
        "final_multiplier": float(row["cashout_multiplier"]) if row["status"] == "cashed_out" else float(row["crash_multiplier"]),
        "payout_kopecks": row["payout_kopecks"],
        "started_at": row["started_at"].isoformat(),
    } for row in rows]}




class PlinkoDropRequest(BaseModel):
    bet_kopecks: int = Field(gt=0, le=100_000_000)
    request_key: str = Field(min_length=16, max_length=64, pattern=r"^[a-zA-Z0-9_-]+$")


PLINKO_MULTIPLIERS = [8.00, 4.00, 2.00, 1.40, 1.10, 0.80, 0.50, 0.80, 1.10, 1.40, 2.00, 4.00, 8.00]


class MinesStartRequest(BaseModel):
    bet_kopecks: int = Field(gt=0, le=100_000_000)
    mine_count: int = Field(ge=1, le=24)


class MinesPickRequest(BaseModel):
    cell: int = Field(ge=0, le=24)


def mines_multiplier(mine_count: int, opened_count: int) -> float:
    if opened_count <= 0:
        return 1.0
    survival = 1.0
    for index in range(opened_count):
        survival *= (25 - mine_count - index) / (25 - index)
    return math.floor((0.99 / survival) * 100) / 100


def mines_round_payload(row: dict, reveal: bool = False) -> dict:
    opened = [int(value) for value in (row["opened_cells"] or [])]
    payload = {
        "round_id": str(row["id"]),
        "status": row["status"],
        "bet_kopecks": row["bet_kopecks"],
        "mine_count": row["mine_count"],
        "opened_cells": opened,
        "safe_count": len(opened),
        "multiplier": mines_multiplier(row["mine_count"], len(opened)),
        "payout_kopecks": row["payout_kopecks"],
        "started_at": row["started_at"].isoformat(),
        "finished_at": row["finished_at"].isoformat() if row["finished_at"] else None,
    }
    if reveal:
        payload["mine_cells"] = [int(value) for value in row["mine_cells"]]
    return payload


@app.post("/v1/casino/mines/start", status_code=201)
def start_mines_round(body: MinesStartRequest, user: User):
    cells = list(range(25))
    mine_cells = secrets.SystemRandom().sample(cells, body.mine_count)
    with pool.connection() as conn:
        with conn.transaction():
            conn.execute(
                "INSERT INTO casino_wallets(user_id, balance_kopecks) VALUES (%s, 0) ON CONFLICT (user_id) DO NOTHING",
                (user["id"],),
            )
            wallet = conn.execute(
                "SELECT balance_kopecks FROM casino_wallets WHERE user_id = %s FOR UPDATE",
                (user["id"],),
            ).fetchone()
            active_crash = conn.execute(
                "SELECT id FROM casino_crash_rounds WHERE user_id = %s AND status = 'active'",
                (user["id"],),
            ).fetchone()
            active_mines = conn.execute(
                "SELECT id FROM casino_mines_rounds WHERE user_id = %s AND status = 'active'",
                (user["id"],),
            ).fetchone()
            active_ladder = conn.execute(
                "SELECT id FROM casino_ladder_rounds WHERE user_id = %s AND status = 'active'",
                (user["id"],),
            ).fetchone()
            active_blackjack = conn.execute(
                "SELECT id FROM casino_blackjack_rounds WHERE user_id = %s AND status = 'active'",
                (user["id"],),
            ).fetchone()
            if active_crash or active_mines or active_ladder or active_blackjack:
                raise HTTPException(status_code=409, detail="Сначала заверши активную игру")
            if wallet["balance_kopecks"] < body.bet_kopecks:
                raise HTTPException(status_code=409, detail="Недостаточно средств на балансе казино")
            conn.execute(
                "UPDATE casino_wallets SET balance_kopecks = balance_kopecks - %s, updated_at = NOW() WHERE user_id = %s",
                (body.bet_kopecks, user["id"]),
            )
            row = conn.execute(
                """INSERT INTO casino_mines_rounds(user_id, bet_kopecks, mine_count, mine_cells)
                   VALUES (%s, %s, %s, %s)
                   RETURNING id, user_id, bet_kopecks, mine_count, mine_cells, opened_cells,
                             status, payout_kopecks, started_at, finished_at""",
                (user["id"], body.bet_kopecks, body.mine_count, mine_cells),
            ).fetchone()
    return mines_round_payload(row)


@app.get("/v1/casino/mines-active")
def get_active_mines_round(user: User):
    with pool.connection() as conn:
        row = conn.execute(
            """SELECT id, user_id, bet_kopecks, mine_count, mine_cells, opened_cells,
                      status, payout_kopecks, started_at, finished_at
               FROM casino_mines_rounds WHERE user_id = %s AND status = 'active'
               ORDER BY started_at DESC LIMIT 1""",
            (user["id"],),
        ).fetchone()
    return {"round": mines_round_payload(row) if row else None}


@app.post("/v1/casino/mines/{round_id}/pick")
def pick_mines_cell(round_id: str, body: MinesPickRequest, user: User):
    with pool.connection() as conn:
        with conn.transaction():
            row = conn.execute(
                """SELECT id, user_id, bet_kopecks, mine_count, mine_cells, opened_cells,
                          status, payout_kopecks, started_at, finished_at
                   FROM casino_mines_rounds WHERE id = %s::uuid AND user_id = %s FOR UPDATE""",
                (round_id, user["id"]),
            ).fetchone()
            if not row:
                raise HTTPException(status_code=404, detail="Раунд MINES не найден")
            if row["status"] != "active":
                raise HTTPException(status_code=409, detail="Раунд уже завершён")
            opened = [int(value) for value in (row["opened_cells"] or [])]
            if body.cell in opened:
                raise HTTPException(status_code=409, detail="Эта клетка уже открыта")
            mines = [int(value) for value in row["mine_cells"]]
            if body.cell in mines:
                row = conn.execute(
                    """UPDATE casino_mines_rounds SET status = 'lost', finished_at = NOW()
                       WHERE id = %s
                       RETURNING id, user_id, bet_kopecks, mine_count, mine_cells, opened_cells,
                                 status, payout_kopecks, started_at, finished_at""",
                    (row["id"],),
                ).fetchone()
                return mines_round_payload(row, reveal=True)
            opened.append(body.cell)
            status_value = "active"
            payout = 0
            if len(opened) == 25 - row["mine_count"]:
                multiplier = mines_multiplier(row["mine_count"], len(opened))
                payout = int(row["bet_kopecks"] * multiplier)
                status_value = "won"
                conn.execute(
                    "UPDATE casino_wallets SET balance_kopecks = balance_kopecks + %s, updated_at = NOW() WHERE user_id = %s",
                    (payout, user["id"]),
                )
            row = conn.execute(
                """UPDATE casino_mines_rounds
                   SET opened_cells = %s, status = %s, payout_kopecks = %s,
                       finished_at = CASE WHEN %s <> 'active' THEN NOW() ELSE NULL END
                   WHERE id = %s
                   RETURNING id, user_id, bet_kopecks, mine_count, mine_cells, opened_cells,
                             status, payout_kopecks, started_at, finished_at""",
                (opened, status_value, payout, status_value, row["id"]),
            ).fetchone()
    return mines_round_payload(row, reveal=(row["status"] != "active"))


@app.post("/v1/casino/mines/{round_id}/cashout")
def cashout_mines_round(round_id: str, user: User):
    with pool.connection() as conn:
        with conn.transaction():
            row = conn.execute(
                """SELECT id, user_id, bet_kopecks, mine_count, mine_cells, opened_cells,
                          status, payout_kopecks, started_at, finished_at
                   FROM casino_mines_rounds WHERE id = %s::uuid AND user_id = %s FOR UPDATE""",
                (round_id, user["id"]),
            ).fetchone()
            if not row:
                raise HTTPException(status_code=404, detail="Раунд MINES не найден")
            if row["status"] != "active":
                raise HTTPException(status_code=409, detail="Раунд уже завершён")
            opened_count = len(row["opened_cells"] or [])
            if opened_count <= 0:
                raise HTTPException(status_code=409, detail="Открой хотя бы одну безопасную клетку")
            multiplier = mines_multiplier(row["mine_count"], opened_count)
            payout = int(row["bet_kopecks"] * multiplier)
            conn.execute(
                "UPDATE casino_wallets SET balance_kopecks = balance_kopecks + %s, updated_at = NOW() WHERE user_id = %s",
                (payout, user["id"]),
            )
            row = conn.execute(
                """UPDATE casino_mines_rounds
                   SET status = 'cashed_out', payout_kopecks = %s, finished_at = NOW()
                   WHERE id = %s
                   RETURNING id, user_id, bet_kopecks, mine_count, mine_cells, opened_cells,
                             status, payout_kopecks, started_at, finished_at""",
                (payout, row["id"]),
            ).fetchone()
    return mines_round_payload(row, reveal=True)


@app.get("/v1/casino/mines-history")
def mines_history(user: User, limit: int = 10):
    limit = max(1, min(limit, 30))
    with pool.connection() as conn:
        rows = conn.execute(
            """SELECT id, bet_kopecks, mine_count, opened_cells, mine_cells, status,
                      payout_kopecks, started_at
               FROM casino_mines_rounds WHERE user_id = %s
               ORDER BY started_at DESC LIMIT %s""",
            (user["id"], limit),
        ).fetchall()
    return {"items": [{
        "round_id": str(row["id"]),
        "status": row["status"],
        "bet_kopecks": row["bet_kopecks"],
        "mine_count": row["mine_count"],
        "safe_count": len(row["opened_cells"] or []),
        "payout_kopecks": row["payout_kopecks"],
        "started_at": row["started_at"].isoformat(),
    } for row in rows]}


class LadderStartRequest(BaseModel):
    bet_kopecks: int = Field(gt=0, le=100_000_000)


class LadderPickRequest(BaseModel):
    step: int = Field(ge=0, le=7)
    side: int = Field(ge=0, le=1)


def ladder_multiplier(steps: int) -> float:
    if steps <= 0:
        return 1.0
    return math.floor((1.98 ** steps) * 100) / 100


def ladder_round_payload(row: dict, reveal: bool = False) -> dict:
    payload = {
        "round_id": str(row["id"]),
        "status": row["status"],
        "bet_kopecks": row["bet_kopecks"],
        "reached_steps": row["reached_steps"],
        "total_steps": 8,
        "multiplier": ladder_multiplier(row["reached_steps"]),
        "payout_kopecks": row["payout_kopecks"],
        "started_at": row["started_at"].isoformat(),
        "finished_at": row["finished_at"].isoformat() if row["finished_at"] else None,
    }
    if reveal:
        payload["safe_sides"] = [int(value) for value in row["safe_sides"]]
    return payload


@app.post("/v1/casino/ladder/start", status_code=201)
def start_ladder_round(body: LadderStartRequest, user: User):
    safe_sides = [secrets.randbelow(2) for _ in range(8)]
    with pool.connection() as conn:
        with conn.transaction():
            conn.execute(
                "INSERT INTO casino_wallets(user_id, balance_kopecks) VALUES (%s, 0) ON CONFLICT (user_id) DO NOTHING",
                (user["id"],),
            )
            wallet = conn.execute(
                "SELECT balance_kopecks FROM casino_wallets WHERE user_id = %s FOR UPDATE",
                (user["id"],),
            ).fetchone()
            active_crash = conn.execute(
                "SELECT id FROM casino_crash_rounds WHERE user_id = %s AND status = 'active'",
                (user["id"],),
            ).fetchone()
            active_mines = conn.execute(
                "SELECT id FROM casino_mines_rounds WHERE user_id = %s AND status = 'active'",
                (user["id"],),
            ).fetchone()
            active_ladder = conn.execute(
                "SELECT id FROM casino_ladder_rounds WHERE user_id = %s AND status = 'active'",
                (user["id"],),
            ).fetchone()
            active_blackjack = conn.execute(
                "SELECT id FROM casino_blackjack_rounds WHERE user_id = %s AND status = 'active'",
                (user["id"],),
            ).fetchone()
            if active_crash or active_mines or active_ladder or active_blackjack:
                raise HTTPException(status_code=409, detail="Сначала заверши активную игру")
            if wallet["balance_kopecks"] < body.bet_kopecks:
                raise HTTPException(status_code=409, detail="Недостаточно средств на балансе казино")
            conn.execute(
                "UPDATE casino_wallets SET balance_kopecks = balance_kopecks - %s, updated_at = NOW() WHERE user_id = %s",
                (body.bet_kopecks, user["id"]),
            )
            row = conn.execute(
                """INSERT INTO casino_ladder_rounds(user_id, bet_kopecks, safe_sides)
                   VALUES (%s, %s, %s)
                   RETURNING id, user_id, bet_kopecks, safe_sides, reached_steps, status,
                             payout_kopecks, started_at, finished_at""",
                (user["id"], body.bet_kopecks, safe_sides),
            ).fetchone()
    return ladder_round_payload(row)


@app.get("/v1/casino/ladder-active")
def get_active_ladder_round(user: User):
    with pool.connection() as conn:
        row = conn.execute(
            """SELECT id, user_id, bet_kopecks, safe_sides, reached_steps, status,
                      payout_kopecks, started_at, finished_at
               FROM casino_ladder_rounds WHERE user_id = %s AND status = 'active'
               ORDER BY started_at DESC LIMIT 1""",
            (user["id"],),
        ).fetchone()
    return {"round": ladder_round_payload(row) if row else None}


@app.post("/v1/casino/ladder/{round_id}/pick")
def pick_ladder_step(round_id: str, body: LadderPickRequest, user: User):
    with pool.connection() as conn:
        with conn.transaction():
            row = conn.execute(
                """SELECT id, user_id, bet_kopecks, safe_sides, reached_steps, status,
                          payout_kopecks, started_at, finished_at
                   FROM casino_ladder_rounds WHERE id = %s::uuid AND user_id = %s FOR UPDATE""",
                (round_id, user["id"]),
            ).fetchone()
            if not row:
                raise HTTPException(status_code=404, detail="Раунд ЛЕСЕНКА не найден")
            if row["status"] != "active":
                raise HTTPException(status_code=409, detail="Раунд уже завершён")
            if body.step != row["reached_steps"]:
                raise HTTPException(status_code=409, detail="Сначала выбери текущую ступень")
            if body.side != int(row["safe_sides"][body.step]):
                row = conn.execute(
                    """UPDATE casino_ladder_rounds SET status = 'lost', finished_at = NOW()
                       WHERE id = %s
                       RETURNING id, user_id, bet_kopecks, safe_sides, reached_steps, status,
                                 payout_kopecks, started_at, finished_at""",
                    (row["id"],),
                ).fetchone()
                return ladder_round_payload(row, reveal=True)
            reached = row["reached_steps"] + 1
            payout = 0
            status_value = "active"
            if reached == 8:
                payout = int(row["bet_kopecks"] * ladder_multiplier(reached))
                status_value = "won"
                conn.execute(
                    "UPDATE casino_wallets SET balance_kopecks = balance_kopecks + %s, updated_at = NOW() WHERE user_id = %s",
                    (payout, user["id"]),
                )
            row = conn.execute(
                """UPDATE casino_ladder_rounds
                   SET reached_steps = %s, status = %s, payout_kopecks = %s,
                       finished_at = CASE WHEN %s <> 'active' THEN NOW() ELSE NULL END
                   WHERE id = %s
                   RETURNING id, user_id, bet_kopecks, safe_sides, reached_steps, status,
                             payout_kopecks, started_at, finished_at""",
                (reached, status_value, payout, status_value, row["id"]),
            ).fetchone()
    return ladder_round_payload(row, reveal=(row["status"] != "active"))


@app.post("/v1/casino/ladder/{round_id}/cashout")
def cashout_ladder_round(round_id: str, user: User):
    with pool.connection() as conn:
        with conn.transaction():
            row = conn.execute(
                """SELECT id, user_id, bet_kopecks, safe_sides, reached_steps, status,
                          payout_kopecks, started_at, finished_at
                   FROM casino_ladder_rounds WHERE id = %s::uuid AND user_id = %s FOR UPDATE""",
                (round_id, user["id"]),
            ).fetchone()
            if not row:
                raise HTTPException(status_code=404, detail="Раунд ЛЕСЕНКА не найден")
            if row["status"] != "active":
                raise HTTPException(status_code=409, detail="Раунд уже завершён")
            if row["reached_steps"] <= 0:
                raise HTTPException(status_code=409, detail="Поднимись хотя бы на одну ступень")
            payout = int(row["bet_kopecks"] * ladder_multiplier(row["reached_steps"]))
            conn.execute(
                "UPDATE casino_wallets SET balance_kopecks = balance_kopecks + %s, updated_at = NOW() WHERE user_id = %s",
                (payout, user["id"]),
            )
            row = conn.execute(
                """UPDATE casino_ladder_rounds
                   SET status = 'cashed_out', payout_kopecks = %s, finished_at = NOW()
                   WHERE id = %s
                   RETURNING id, user_id, bet_kopecks, safe_sides, reached_steps, status,
                             payout_kopecks, started_at, finished_at""",
                (payout, row["id"]),
            ).fetchone()
    return ladder_round_payload(row, reveal=True)


@app.get("/v1/casino/ladder-history")
def ladder_history(user: User, limit: int = 10):
    limit = max(1, min(limit, 30))
    with pool.connection() as conn:
        rows = conn.execute(
            """SELECT id, bet_kopecks, reached_steps, status, payout_kopecks, started_at
               FROM casino_ladder_rounds WHERE user_id = %s
               ORDER BY started_at DESC LIMIT %s""",
            (user["id"], limit),
        ).fetchall()
    return {"items": [{
        "round_id": str(row["id"]), "status": row["status"],
        "bet_kopecks": row["bet_kopecks"], "reached_steps": row["reached_steps"],
        "multiplier": ladder_multiplier(row["reached_steps"]),
        "payout_kopecks": row["payout_kopecks"],
        "started_at": row["started_at"].isoformat(),
    } for row in rows]}





class BlackjackStartRequest(BaseModel):
    bet_kopecks: int = Field(gt=0, le=100_000_000)


class BlackjackActionRequest(BaseModel):
    action: str = Field(pattern=r"^(hit|stand|double)$")


def blackjack_deck() -> list[dict]:
    ranks = ["2", "3", "4", "5", "6", "7", "8", "9", "10", "J", "Q", "K", "A"]
    suits = ["♠", "♥", "♦", "♣"]
    deck = [{"rank": rank, "suit": suit} for _ in range(6) for suit in suits for rank in ranks]
    secrets.SystemRandom().shuffle(deck)
    return deck


def blackjack_score(cards: list[dict]) -> tuple[int, bool]:
    total, aces = 0, 0
    for card in cards:
        rank = card["rank"]
        if rank == "A":
            total += 11
            aces += 1
        elif rank in ("K", "Q", "J"):
            total += 10
        else:
            total += int(rank)
    while total > 21 and aces:
        total -= 10
        aces -= 1
    return total, aces > 0


def blackjack_natural(cards: list[dict]) -> bool:
    return len(cards) == 2 and blackjack_score(cards)[0] == 21


def blackjack_draw(deck: list[dict], index: int) -> tuple[dict, int]:
    if index >= len(deck):
        raise HTTPException(status_code=409, detail="Колода закончилась — начни новый раунд")
    return deck[index], index + 1


def blackjack_payload(row: dict, reveal: bool = False) -> dict:
    player = row["player_cards"] or []
    dealer = row["dealer_cards"] or []
    return {
        "round_id": str(row["id"]), "status": row["status"],
        "bet_kopecks": row["bet_kopecks"], "doubled": bool(row["doubled"]),
        "player_cards": player, "dealer_cards": dealer if reveal else dealer[:1],
        "dealer_hidden": not reveal, "player_value": blackjack_score(player)[0],
        "dealer_value": blackjack_score(dealer)[0] if reveal else None,
        "payout_kopecks": row["payout_kopecks"],
        "started_at": row["started_at"].isoformat(),
        "finished_at": row["finished_at"].isoformat() if row["finished_at"] else None,
    }


def blackjack_settle(conn, row: dict, result: str, payout: int) -> dict:
    if payout:
        conn.execute(
            "UPDATE casino_wallets SET balance_kopecks = balance_kopecks + %s, updated_at = NOW() WHERE user_id = %s",
            (payout, row["user_id"]),
        )
    return conn.execute(
        """UPDATE casino_blackjack_rounds SET status = %s, payout_kopecks = %s, finished_at = NOW()
           WHERE id = %s
           RETURNING id, user_id, bet_kopecks, doubled, player_cards, dealer_cards,
                     status, payout_kopecks, started_at, finished_at""",
        (result, payout, row["id"]),
    ).fetchone()


@app.post("/v1/casino/blackjack/start", status_code=201)
def start_blackjack_round(body: BlackjackStartRequest, user: User):
    deck = blackjack_deck()
    player, dealer, index = [], [], 0
    for target in (player, dealer, player, dealer):
        card, index = blackjack_draw(deck, index)
        target.append(card)
    player_nat, dealer_nat = blackjack_natural(player), blackjack_natural(dealer)
    result, payout = "active", 0
    if player_nat and dealer_nat:
        result, payout = "push", body.bet_kopecks * 2
    elif player_nat:
        result, payout = "blackjack", body.bet_kopecks * 5 // 2
    elif dealer_nat:
        result = "lost"

    with pool.connection() as conn:
        with conn.transaction():
            conn.execute(
                "INSERT INTO casino_wallets(user_id, balance_kopecks) VALUES (%s, 0) ON CONFLICT (user_id) DO NOTHING",
                (user["id"],),
            )
            wallet = conn.execute(
                "SELECT balance_kopecks FROM casino_wallets WHERE user_id = %s FOR UPDATE",
                (user["id"],),
            ).fetchone()
            for table, game in [
                ("casino_crash_rounds", "CRASH"), ("casino_mines_rounds", "MINES"),
                ("casino_ladder_rounds", "ЛЕСЕНКА"), ("casino_blackjack_rounds", "BLACKJACK")
            ]:
                active = conn.execute(
                    f"SELECT id FROM {table} WHERE user_id = %s AND status = 'active'",
                    (user["id"],),
                ).fetchone()
                if active:
                    raise HTTPException(status_code=409, detail=f"Сначала заверши активную игру: {game}")
            if wallet["balance_kopecks"] < body.bet_kopecks:
                raise HTTPException(status_code=409, detail="Недостаточно средств на балансе казино")
            conn.execute(
                "UPDATE casino_wallets SET balance_kopecks = balance_kopecks - %s, updated_at = NOW() WHERE user_id = %s",
                (body.bet_kopecks, user["id"]),
            )
            row = conn.execute(
                """INSERT INTO casino_blackjack_rounds(
                       user_id, bet_kopecks, deck, deck_index, player_cards, dealer_cards, status, payout_kopecks
                   ) VALUES (%s, %s, %s::jsonb, %s, %s::jsonb, %s::jsonb, %s, %s)
                   RETURNING id, user_id, bet_kopecks, doubled, player_cards, dealer_cards,
                             status, payout_kopecks, started_at, finished_at""",
                (user["id"], body.bet_kopecks, json.dumps(deck), index,
                 json.dumps(player), json.dumps(dealer), result, payout),
            ).fetchone()
            if payout:
                conn.execute(
                    "UPDATE casino_wallets SET balance_kopecks = balance_kopecks + %s, updated_at = NOW() WHERE user_id = %s",
                    (payout, user["id"]),
                )
    return blackjack_payload(row, reveal=(result != "active"))


@app.get("/v1/casino/blackjack-active")
def get_active_blackjack_round(user: User):
    with pool.connection() as conn:
        row = conn.execute(
            """SELECT id, user_id, bet_kopecks, deck, deck_index, player_cards, dealer_cards,
                      doubled, status, payout_kopecks, started_at, finished_at
               FROM casino_blackjack_rounds WHERE user_id = %s AND status = 'active'
               ORDER BY started_at DESC LIMIT 1""",
            (user["id"],),
        ).fetchone()
    return {"round": blackjack_payload(row) if row else None}


@app.post("/v1/casino/blackjack/{round_id}/action")
def blackjack_action(round_id: str, body: BlackjackActionRequest, user: User):
    with pool.connection() as conn:
        with conn.transaction():
            row = conn.execute(
                """SELECT id, user_id, bet_kopecks, deck, deck_index, player_cards, dealer_cards,
                          doubled, status, payout_kopecks, started_at, finished_at
                   FROM casino_blackjack_rounds WHERE id = %s::uuid AND user_id = %s FOR UPDATE""",
                (round_id, user["id"]),
            ).fetchone()
            if not row:
                raise HTTPException(status_code=404, detail="Раунд BLACKJACK не найден")
            if row["status"] != "active":
                raise HTTPException(status_code=409, detail="Раунд уже завершён")
            deck, index = row["deck"], row["deck_index"]
            player, dealer = row["player_cards"], row["dealer_cards"]
            bet, action = row["bet_kopecks"], body.action
            doubled = bool(row["doubled"])

            if action == "double":
                if len(player) != 2 or doubled:
                    raise HTTPException(status_code=409, detail="Удвоение доступно только на первых двух картах")
                wallet = conn.execute(
                    "SELECT balance_kopecks FROM casino_wallets WHERE user_id = %s FOR UPDATE",
                    (user["id"],),
                ).fetchone()
                if wallet["balance_kopecks"] < bet:
                    raise HTTPException(status_code=409, detail="Недостаточно средств для удвоения")
                conn.execute(
                    "UPDATE casino_wallets SET balance_kopecks = balance_kopecks - %s, updated_at = NOW() WHERE user_id = %s",
                    (bet, user["id"]),
                )
                bet *= 2
                doubled = True
                card, index = blackjack_draw(deck, index)
                player = player + [card]
                if blackjack_score(player)[0] > 21:
                    updated = conn.execute(
                        """UPDATE casino_blackjack_rounds SET bet_kopecks = %s, deck_index = %s,
                               player_cards = %s::jsonb, doubled = TRUE, status = 'lost', finished_at = NOW()
                           WHERE id = %s
                           RETURNING id, user_id, bet_kopecks, doubled, player_cards, dealer_cards,
                                     status, payout_kopecks, started_at, finished_at""",
                        (bet, index, json.dumps(player), row["id"]),
                    ).fetchone()
                    return blackjack_payload(updated, reveal=True)
                action = "stand"
            elif action == "hit":
                card, index = blackjack_draw(deck, index)
                player = player + [card]
                if blackjack_score(player)[0] > 21:
                    updated = conn.execute(
                        """UPDATE casino_blackjack_rounds SET deck_index = %s, player_cards = %s::jsonb,
                               status = 'lost', finished_at = NOW()
                           WHERE id = %s
                           RETURNING id, user_id, bet_kopecks, doubled, player_cards, dealer_cards,
                                     status, payout_kopecks, started_at, finished_at""",
                        (index, json.dumps(player), row["id"]),
                    ).fetchone()
                    return blackjack_payload(updated, reveal=True)
                updated = conn.execute(
                    """UPDATE casino_blackjack_rounds SET deck_index = %s, player_cards = %s::jsonb
                       WHERE id = %s
                       RETURNING id, user_id, bet_kopecks, doubled, player_cards, dealer_cards,
                                 status, payout_kopecks, started_at, finished_at""",
                    (index, json.dumps(player), row["id"]),
                ).fetchone()
                return blackjack_payload(updated)

            while blackjack_score(dealer)[0] < 17:
                card, index = blackjack_draw(deck, index)
                dealer = dealer + [card]
            pv, dv = blackjack_score(player)[0], blackjack_score(dealer)[0]
            if dv > 21 or pv > dv:
                result, payout = "won", bet * 2
            elif pv == dv:
                result, payout = "push", bet
            else:
                result, payout = "lost", 0
            updated = conn.execute(
                """UPDATE casino_blackjack_rounds SET bet_kopecks = %s, deck_index = %s,
                       player_cards = %s::jsonb, dealer_cards = %s::jsonb, doubled = %s
                   WHERE id = %s
                   RETURNING id, user_id, bet_kopecks, doubled, player_cards, dealer_cards,
                             status, payout_kopecks, started_at, finished_at""",
                (bet, index, json.dumps(player), json.dumps(dealer), doubled, row["id"]),
            ).fetchone()
            updated = blackjack_settle(conn, updated, result, payout)
    return blackjack_payload(updated, reveal=True)


@app.get("/v1/casino/blackjack-history")
def blackjack_history(user: User, limit: int = 10):
    limit = max(1, min(limit, 30))
    with pool.connection() as conn:
        rows = conn.execute(
            """SELECT id, bet_kopecks, status, payout_kopecks, started_at
               FROM casino_blackjack_rounds WHERE user_id = %s
               ORDER BY started_at DESC LIMIT %s""",
            (user["id"], limit),
        ).fetchall()
    return {"items": [{
        "round_id": str(row["id"]), "status": row["status"],
        "bet_kopecks": row["bet_kopecks"], "payout_kopecks": row["payout_kopecks"],
        "started_at": row["started_at"].isoformat(),
    } for row in rows]}



class RouletteBetRequest(BaseModel):
    selection: str = Field(pattern=r"^(number|color|parity|half|dozen|column)$")
    value: str = Field(min_length=1, max_length=8)
    amount_kopecks: int = Field(gt=0, le=100_000_000)


class RouletteSpinRequest(BaseModel):
    bets: list[RouletteBetRequest] = Field(min_length=1, max_length=36)
    request_key: str = Field(min_length=16, max_length=64, pattern=r"^[a-zA-Z0-9-]+$")


ROULETTE_RED_NUMBERS = {1, 3, 5, 7, 9, 12, 14, 16, 18, 19, 21, 23, 25, 27, 30, 32, 34, 36}


def roulette_bet_wins(bet: RouletteBetRequest, number: int) -> tuple[bool, int]:
    selection, value = bet.selection, bet.value
    if selection == "number":
        if not value.isdigit() or not 0 <= int(value) <= 36:
            raise HTTPException(status_code=422, detail="Некорректное число рулетки")
        return int(value) == number, 36
    if selection == "color":
        if value not in ("red", "black"):
            raise HTTPException(status_code=422, detail="Некорректный цвет")
        return number != 0 and ((number in ROULETTE_RED_NUMBERS) == (value == "red")), 2
    if selection == "parity":
        if value not in ("odd", "even"):
            raise HTTPException(status_code=422, detail="Некорректная чётность")
        return number != 0 and (("odd" if number % 2 else "even") == value), 2
    if selection == "half":
        if value not in ("low", "high"):
            raise HTTPException(status_code=422, detail="Некорректный диапазон")
        return (1 <= number <= 18 if value == "low" else 19 <= number <= 36), 2
    if selection in ("dozen", "column"):
        if value not in ("1", "2", "3"):
            raise HTTPException(status_code=422, detail="Некорректная дюжина или колонка")
        target = int(value)
        actual = ((number - 1) // 12 + 1) if selection == "dozen" and number else (((number - 1) % 3 + 1) if number else 0)
        return number != 0 and actual == target, 3
    raise HTTPException(status_code=422, detail="Неизвестный тип ставки")


@app.post("/v1/casino/roulette/spin", status_code=201)
def spin_roulette(body: RouletteSpinRequest, user: User):
    total_bet = sum(bet.amount_kopecks for bet in body.bets)
    if total_bet > 100_000_000:
        raise HTTPException(status_code=422, detail="Общая сумма ставок не может превышать 1 000 000 ₽")
    stored_bets = [bet.model_dump() for bet in body.bets]
    with pool.connection() as conn:
        with conn.transaction():
            conn.execute(
                "INSERT INTO casino_wallets(user_id, balance_kopecks) VALUES (%s, 0) ON CONFLICT (user_id) DO NOTHING",
                (user["id"],),
            )
            wallet = conn.execute(
                "SELECT balance_kopecks FROM casino_wallets WHERE user_id = %s FOR UPDATE",
                (user["id"],),
            ).fetchone()
            # A retried HTTP request returns the original spin and never charges twice.
            previous = conn.execute(
                """SELECT id, winning_number, total_bet_kopecks, payout_kopecks, created_at
                   FROM casino_roulette_rounds WHERE user_id = %s AND request_key = %s""",
                (user["id"], body.request_key),
            ).fetchone()
            if previous:
                n = previous["winning_number"]
                color = "green" if n == 0 else ("red" if n in ROULETTE_RED_NUMBERS else "black")
                return {
                    "round_id": str(previous["id"]), "winning_number": n, "color": color,
                    "total_bet_kopecks": previous["total_bet_kopecks"],
                    "payout_kopecks": previous["payout_kopecks"],
                    "net_kopecks": previous["payout_kopecks"] - previous["total_bet_kopecks"],
                    "created_at": previous["created_at"].isoformat()
                }
            for table, game in [
                ("casino_crash_rounds", "CRASH"), ("casino_mines_rounds", "MINES"),
                ("casino_ladder_rounds", "ЛЕСЕНКА"), ("casino_blackjack_rounds", "BLACKJACK")
            ]:
                active = conn.execute(
                    f"SELECT id FROM {table} WHERE user_id = %s AND status = 'active'",
                    (user["id"],),
                ).fetchone()
                if active:
                    raise HTTPException(status_code=409, detail=f"Сначала заверши активную игру: {game}")
            if wallet["balance_kopecks"] < total_bet:
                raise HTTPException(status_code=409, detail="Недостаточно средств на балансе казино")
            winning_number = secrets.randbelow(37)
            payout = 0
            for bet in body.bets:
                won, odds = roulette_bet_wins(bet, winning_number)
                if won:
                    payout += bet.amount_kopecks * odds
            if payout > 2_000_000_000:
                raise HTTPException(status_code=422, detail="Слишком большая потенциальная выплата")
            conn.execute(
                "UPDATE casino_wallets SET balance_kopecks = balance_kopecks - %s + %s, updated_at = NOW() WHERE user_id = %s",
                (total_bet, payout, user["id"]),
            )
            row = conn.execute(
                """INSERT INTO casino_roulette_rounds(user_id, bets, request_key, winning_number, total_bet_kopecks, payout_kopecks)
                   VALUES (%s, %s::jsonb, %s, %s, %s, %s)
                   RETURNING id, created_at""",
                (user["id"], json.dumps(stored_bets), body.request_key, winning_number, total_bet, payout),
            ).fetchone()
    color = "green" if winning_number == 0 else ("red" if winning_number in ROULETTE_RED_NUMBERS else "black")
    return {
        "round_id": str(row["id"]), "winning_number": winning_number, "color": color,
        "total_bet_kopecks": total_bet, "payout_kopecks": payout,
        "net_kopecks": payout - total_bet, "created_at": row["created_at"].isoformat()
    }



@app.post("/v1/casino/plinko/drop", status_code=201)
def drop_plinko_ball(body: PlinkoDropRequest, user: User):
    # The complete result and wallet movement are decided atomically on the server.
    with pool.connection() as conn:
        with conn.transaction():
            conn.execute(
                "INSERT INTO casino_wallets(user_id, balance_kopecks) VALUES (%s, 0) ON CONFLICT (user_id) DO NOTHING",
                (user["id"],),
            )
            wallet = conn.execute(
                "SELECT balance_kopecks FROM casino_wallets WHERE user_id = %s FOR UPDATE",
                (user["id"],),
            ).fetchone()

            # Idempotency protects against a repeated request after a dropped connection.
            previous = conn.execute(
                """SELECT id, path, bucket, multiplier, bet_kopecks, payout_kopecks, created_at
                   FROM casino_plinko_rounds WHERE user_id = %s AND request_key = %s""",
                (user["id"], body.request_key),
            ).fetchone()
            if previous:
                return {
                    "round_id": str(previous["id"]),
                    "path": [int(move) for move in previous["path"]],
                    "bucket": previous["bucket"],
                    "multiplier": float(previous["multiplier"]),
                    "bet_kopecks": previous["bet_kopecks"],
                    "payout_kopecks": previous["payout_kopecks"],
                    "created_at": previous["created_at"].isoformat(),
                }

            for table, game in [
                ("casino_crash_rounds", "CRASH"),
                ("casino_mines_rounds", "MINES"),
                ("casino_ladder_rounds", "ЛЕСЕНКА"),
                ("casino_blackjack_rounds", "BLACKJACK"),
            ]:
                active = conn.execute(
                    f"SELECT id FROM {table} WHERE user_id = %s AND status = 'active'",
                    (user["id"],),
                ).fetchone()
                if active:
                    raise HTTPException(status_code=409, detail=f"Сначала заверши активную игру: {game}")

            if wallet["balance_kopecks"] < body.bet_kopecks:
                raise HTTPException(status_code=409, detail="Недостаточно средств на балансе казино")

            path = [secrets.randbelow(2) for _ in range(12)]
            bucket = sum(path)
            multiplier = PLINKO_MULTIPLIERS[bucket]
            payout = int(body.bet_kopecks * multiplier)

            conn.execute(
                """UPDATE casino_wallets
                   SET balance_kopecks = balance_kopecks - %s + %s, updated_at = NOW()
                   WHERE user_id = %s""",
                (body.bet_kopecks, payout, user["id"]),
            )
            row = conn.execute(
                """INSERT INTO casino_plinko_rounds
                   (user_id, request_key, bet_kopecks, path, bucket, multiplier, payout_kopecks)
                   VALUES (%s, %s, %s, %s, %s, %s, %s)
                   RETURNING id, path, bucket, multiplier, bet_kopecks, payout_kopecks, created_at""",
                (user["id"], body.request_key, body.bet_kopecks, path, bucket, multiplier, payout),
            ).fetchone()

    return {
        "round_id": str(row["id"]),
        "path": [int(move) for move in row["path"]],
        "bucket": row["bucket"],
        "multiplier": float(row["multiplier"]),
        "bet_kopecks": row["bet_kopecks"],
        "payout_kopecks": row["payout_kopecks"],
        "created_at": row["created_at"].isoformat(),
    }


@app.get("/v1/casino/roulette-history")
def roulette_history(user: User, limit: int = 12):
    limit = max(1, min(limit, 30))
    with pool.connection() as conn:
        rows = conn.execute(
            """SELECT winning_number, created_at FROM casino_roulette_rounds
               WHERE user_id = %s ORDER BY created_at DESC LIMIT %s""",
            (user["id"], limit),
        ).fetchall()
    return {"items": [{
        "winning_number": row["winning_number"],
        "color": "green" if row["winning_number"] == 0 else ("red" if row["winning_number"] in ROULETTE_RED_NUMBERS else "black"),
        "created_at": row["created_at"].isoformat()
    } for row in rows]}


class WalletDeviceRegisterRequest(BaseModel):
    device_id: str = Field(min_length=16, max_length=64, pattern=r"^[a-zA-Z0-9-]+$")
    public_key: str = Field(min_length=80, max_length=2000)


class CasinoDepositApprovalRequest(BaseModel):
    device_id: str = Field(min_length=16, max_length=64, pattern=r"^[a-zA-Z0-9-]+$")
    signature: str = Field(min_length=40, max_length=2000)


@app.post("/v1/wallet/devices/register")
def register_wallet_device(body: WalletDeviceRegisterRequest, user: User):
    try:
        public_key_der = base64.b64decode(body.public_key, validate=True)
        public_key = serialization.load_der_public_key(public_key_der)
        if not isinstance(public_key, ec.EllipticCurvePublicKey) or public_key.curve.name != "secp256r1":
            raise ValueError("Unsupported key type")
    except (ValueError, binascii.Error):
        raise HTTPException(status_code=400, detail="Некорректный ключ устройства")
    with pool.connection() as conn:
        with conn.transaction():
            conn.execute(
                """INSERT INTO wallet_devices(user_id, device_id, public_key_der)
                   VALUES (%s, %s, %s)
                   ON CONFLICT (user_id, device_id) DO UPDATE SET
                     public_key_der = EXCLUDED.public_key_der, last_seen_at = NOW()""",
                (user["id"], body.device_id, public_key_der),
            )
    return {"device_id": body.device_id, "registered": True}


@app.post("/v1/casino/deposit-requests/{request_id}/approve")
def approve_casino_deposit(request_id: str, body: CasinoDepositApprovalRequest, user: User):
    with pool.connection() as conn:
        with conn.transaction():
            # Use the same lock order as request creation: casino wallet, then request.
            # This serializes approval against a concurrent attempt to create another request.
            conn.execute(
                "INSERT INTO casino_wallets(user_id, balance_kopecks) VALUES (%s, 0) ON CONFLICT (user_id) DO NOTHING",
                (user["id"],),
            )
            conn.execute(
                "SELECT user_id FROM casino_wallets WHERE user_id = %s FOR UPDATE",
                (user["id"],),
            )
            request_row = conn.execute(
                """SELECT id, amount_kopecks, status, expires_at, approval_nonce
                   FROM casino_deposit_requests
                   WHERE id = %s::uuid AND user_id = %s FOR UPDATE""",
                (request_id, user["id"]),
            ).fetchone()
            if not request_row:
                raise HTTPException(status_code=404, detail="Запрос на пополнение не найден")
            if request_row["status"] != "pending":
                raise HTTPException(status_code=409, detail="Этот запрос уже обработан")
            device = conn.execute(
                """SELECT public_key_der FROM wallet_devices
                   WHERE user_id = %s AND device_id = %s""",
                (user["id"], body.device_id),
            ).fetchone()
            if not device:
                raise HTTPException(status_code=403, detail="Это устройство не зарегистрировано в NEON WALLET")
            signed_message = (
                f"NEON-CASINO-DEPOSIT\n{request_row['id']}\n"
                f"{request_row['amount_kopecks']}\n{request_row['approval_nonce']}"
            )
            try:
                public_key = serialization.load_der_public_key(bytes(device["public_key_der"]))
                signature = base64.b64decode(body.signature, validate=True)
                public_key.verify(signature, signed_message.encode("utf-8"), ec.ECDSA(hashes.SHA256()))
            except (ValueError, binascii.Error, InvalidSignature):
                raise HTTPException(status_code=403, detail="Не удалось проверить подтверждение устройства")
            if request_row["expires_at"] <= datetime.now(timezone.utc):
                conn.execute(
                    "UPDATE casino_deposit_requests SET status = 'expired', processed_at = NOW() WHERE id = %s::uuid",
                    (request_id,),
                )
                result = {"status": "expired"}
            else:
                settings = conn.execute(
                    "SELECT card_frozen FROM wallet_settings WHERE user_id = %s",
                    (user["id"],),
                ).fetchone()
                if settings and settings["card_frozen"]:
                    raise HTTPException(status_code=409, detail="Карта заморожена. Исходящие операции заблокированы.")
                amount = request_row["amount_kopecks"]
                debit = conn.execute(
                    """UPDATE wallets SET balance_kopecks = balance_kopecks - %s, updated_at = NOW()
                       WHERE user_id = %s AND balance_kopecks >= %s RETURNING balance_kopecks""",
                    (amount, user["id"], amount),
                ).fetchone()
                if not debit:
                    raise HTTPException(status_code=409, detail="Недостаточно средств в NEON WALLET")
                casino = conn.execute(
                    """UPDATE casino_wallets SET balance_kopecks = balance_kopecks + %s, updated_at = NOW()
                       WHERE user_id = %s RETURNING balance_kopecks""",
                    (amount, user["id"]),
                ).fetchone()
                tx = conn.execute(
                    """INSERT INTO transactions(id, idempotency_key, sender_user_id,
                       amount_kopecks, kind, description)
                       VALUES (gen_random_uuid(), %s, %s, %s, 'casino_deposit', 'Пополнение NEON CASINO')
                       RETURNING id, created_at""",
                    (f"casino-deposit:{request_id}", user["id"], amount),
                ).fetchone()
                conn.execute(
                    """UPDATE casino_deposit_requests SET status = 'approved',
                       processed_at = NOW(), transaction_id = %s WHERE id = %s::uuid""",
                    (tx["id"], request_id),
                )
                result = {
                    "status": "approved",
                    "transaction_id": str(tx["id"]),
                    "amount_kopecks": amount,
                    "wallet_balance_kopecks": debit["balance_kopecks"],
                    "casino_balance_kopecks": casino["balance_kopecks"],
                    "processed_at": tx["created_at"].isoformat(),
                }
    return result


@app.post("/v1/casino/deposit-requests/{request_id}/reject")
def reject_casino_deposit(request_id: str, user: User):
    with pool.connection() as conn:
        with conn.transaction():
            row = conn.execute(
                """UPDATE casino_deposit_requests SET status = 'rejected', processed_at = NOW()
                   WHERE id = %s::uuid AND user_id = %s AND status = 'pending'
                   RETURNING id""",
                (request_id, user["id"]),
            ).fetchone()
            if not row:
                raise HTTPException(status_code=404, detail="Ожидающий запрос не найден")
    return {"request_id": request_id, "status": "rejected"}



@app.get("/v1/transactions")
def transactions(user: User, limit: int = 50):
    limit = max(1, min(limit, 100))
    with pool.connection() as conn:
        rows = conn.execute(
            """SELECT t.id, t.kind, t.description, t.amount_kopecks, t.created_at,
                      s.account_id AS sender_account_id, r.account_id AS recipient_account_id
               FROM transactions t
               LEFT JOIN users s ON s.id = t.sender_user_id
               LEFT JOIN users r ON r.id = t.recipient_user_id
               WHERE t.sender_user_id = %s OR t.recipient_user_id = %s
               ORDER BY t.created_at DESC LIMIT %s""",
            (user["id"], user["id"], limit),
        ).fetchall()
    return {"items": [{
        "id": str(row["id"]), "kind": row["kind"], "description": row["description"],
        "amount_kopecks": row["amount_kopecks"], "created_at": row["created_at"].isoformat(),
        "sender_account_id": row["sender_account_id"],
        "recipient_account_id": row["recipient_account_id"],
    } for row in rows]}



@app.post("/v1/casino/withdraw", status_code=201)
def withdraw_casino_balance(
    body: CasinoWithdrawRequest,
    user: User,
):
    # This is an internal transfer of virtual credits between the same user's
    # casino and NEON WALLET balances. No external payment rails are involved.
    idempotency_key = f"casino-withdraw:{user['id']}:{body.request_key}"
    with pool.connection() as conn:
        with conn.transaction():
            existing = conn.execute(
                """SELECT id, amount_kopecks, created_at FROM transactions
                   WHERE idempotency_key = %s""",
                (idempotency_key,),
            ).fetchone()
            if existing:
                casino = conn.execute(
                    "SELECT balance_kopecks FROM casino_wallets WHERE user_id = %s",
                    (user["id"],),
                ).fetchone()
                wallet = conn.execute(
                    "SELECT balance_kopecks FROM wallets WHERE user_id = %s",
                    (user["id"],),
                ).fetchone()
                return {
                    "transaction_id": str(existing["id"]),
                    "status": "completed",
                    "amount_kopecks": existing["amount_kopecks"],
                    "casino_balance_kopecks": casino["balance_kopecks"],
                    "wallet_balance_kopecks": wallet["balance_kopecks"],
                    "created_at": existing["created_at"].isoformat(),
                    "replayed": True,
                }

            conn.execute(
                "INSERT INTO casino_wallets(user_id, balance_kopecks) VALUES (%s, 0) ON CONFLICT (user_id) DO NOTHING",
                (user["id"],),
            )
            # Lock both accounts in a stable order and settle in one transaction.
            casino = conn.execute(
                "SELECT balance_kopecks FROM casino_wallets WHERE user_id = %s FOR UPDATE",
                (user["id"],),
            ).fetchone()
            wallet = conn.execute(
                "SELECT balance_kopecks FROM wallets WHERE user_id = %s FOR UPDATE",
                (user["id"],),
            ).fetchone()
            if not wallet:
                raise HTTPException(status_code=404, detail="Счёт NEON WALLET не найден")
            if casino["balance_kopecks"] < body.amount_kopecks:
                raise HTTPException(status_code=409, detail="Недостаточно средств на балансе казино")

            debit = conn.execute(
                """UPDATE casino_wallets SET balance_kopecks = balance_kopecks - %s,
                   updated_at = NOW() WHERE user_id = %s AND balance_kopecks >= %s
                   RETURNING balance_kopecks""",
                (body.amount_kopecks, user["id"], body.amount_kopecks),
            ).fetchone()
            if not debit:
                raise HTTPException(status_code=409, detail="Недостаточно средств на балансе казино")
            credit = conn.execute(
                """UPDATE wallets SET balance_kopecks = balance_kopecks + %s,
                   updated_at = NOW() WHERE user_id = %s RETURNING balance_kopecks""",
                (body.amount_kopecks, user["id"]),
            ).fetchone()
            tx = conn.execute(
                """INSERT INTO transactions(id, idempotency_key, sender_user_id,
                   amount_kopecks, kind, description)
                   VALUES (gen_random_uuid(), %s, %s, %s, 'casino_withdrawal', 'Вывод из NEON CASINO')
                   RETURNING id, created_at""",
                (idempotency_key, user["id"], body.amount_kopecks),
            ).fetchone()

    return {
        "transaction_id": str(tx["id"]),
        "status": "completed",
        "amount_kopecks": body.amount_kopecks,
        "casino_balance_kopecks": debit["balance_kopecks"],
        "wallet_balance_kopecks": credit["balance_kopecks"],
        "created_at": tx["created_at"].isoformat(),
        "replayed": False,
    }


@app.post("/v1/transfers")
def transfer(
    body: TransferRequest,
    user: User,
    idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key")] = None,
):
    if not idempotency_key or len(idempotency_key) > 100:
        raise HTTPException(status_code=400, detail="Передайте уникальный Idempotency-Key")
    if body.recipient_account_id == user["account_id"]:
        raise HTTPException(status_code=400, detail="Нельзя перевести средства самому себе")

    with pool.connection() as conn:
        with conn.transaction():
            existing = conn.execute(
                "SELECT id FROM transactions WHERE idempotency_key = %s", (idempotency_key,)
            ).fetchone()
            if existing:
                raise HTTPException(status_code=409, detail="Этот запрос уже был обработан")
            recipient = conn.execute(
                "SELECT id FROM users WHERE account_id = %s", (body.recipient_account_id,)
            ).fetchone()
            if not recipient:
                raise HTTPException(status_code=404, detail="Получатель не найден")

            settings = conn.execute(
                "SELECT card_frozen FROM wallet_settings WHERE user_id = %s", (user["id"],)
            ).fetchone()
            if settings and settings["card_frozen"]:
                raise HTTPException(status_code=409, detail="Карта заморожена. Исходящие операции заблокированы.")

            debit = conn.execute(
                """UPDATE wallets SET balance_kopecks = balance_kopecks - %s, updated_at = NOW()
                   WHERE user_id = %s AND balance_kopecks >= %s RETURNING balance_kopecks""",
                (body.amount_kopecks, user["id"], body.amount_kopecks),
            ).fetchone()
            if not debit:
                raise HTTPException(status_code=409, detail="Недостаточно виртуальных средств")

            conn.execute(
                "UPDATE wallets SET balance_kopecks = balance_kopecks + %s, updated_at = NOW() WHERE user_id = %s",
                (body.amount_kopecks, recipient["id"]),
            )
            tx = conn.execute(
                """INSERT INTO transactions(id, idempotency_key, sender_user_id,
                   recipient_user_id, amount_kopecks, kind, description)
                   VALUES (gen_random_uuid(), %s, %s, %s, %s, 'transfer', %s)
                   RETURNING id, created_at""",
                (idempotency_key, user["id"], recipient["id"], body.amount_kopecks, body.description),
            ).fetchone()

    return {
        "transaction_id": str(tx["id"]),
        "status": "completed",
        "amount_kopecks": body.amount_kopecks,
        "balance_kopecks": debit["balance_kopecks"],
        "created_at": tx["created_at"].isoformat(),
    }


class WalletSettingsRequest(BaseModel):
    card_frozen: bool
    card_limit_kopecks: int = Field(ge=0, le=1_000_000_000)


@app.get("/v1/wallet/settings")
def get_wallet_settings(user: User):
    with pool.connection() as conn:
        conn.execute(
            "INSERT INTO wallet_settings(user_id) VALUES (%s) ON CONFLICT (user_id) DO NOTHING",
            (user["id"],),
        )
        row = conn.execute(
            "SELECT card_frozen, card_limit_kopecks, updated_at FROM wallet_settings WHERE user_id = %s",
            (user["id"],),
        ).fetchone()
    return {
        "card_frozen": row["card_frozen"],
        "card_limit_kopecks": row["card_limit_kopecks"],
        "updated_at": row["updated_at"].isoformat(),
    }


@app.put("/v1/wallet/settings")
def update_wallet_settings(body: WalletSettingsRequest, user: User):
    with pool.connection() as conn:
        row = conn.execute(
            """INSERT INTO wallet_settings(user_id, card_frozen, card_limit_kopecks)
               VALUES (%s, %s, %s)
               ON CONFLICT (user_id) DO UPDATE SET
                 card_frozen = EXCLUDED.card_frozen,
                 card_limit_kopecks = EXCLUDED.card_limit_kopecks,
                 updated_at = NOW()
               RETURNING card_frozen, card_limit_kopecks, updated_at""",
            (user["id"], body.card_frozen, body.card_limit_kopecks),
        ).fetchone()
        conn.commit()
    return {
        "card_frozen": row["card_frozen"],
        "card_limit_kopecks": row["card_limit_kopecks"],
        "updated_at": row["updated_at"].isoformat(),
    }
