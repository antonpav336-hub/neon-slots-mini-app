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
from server.extended_casino import initialize_extended_games, register_extended_routes

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
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            banned_at TIMESTAMPTZ,
            ban_reason VARCHAR(240),
            banned_by BIGINT REFERENCES users(id)
        );
        ALTER TABLE users ADD COLUMN IF NOT EXISTS banned_at TIMESTAMPTZ;
        ALTER TABLE users ADD COLUMN IF NOT EXISTS ban_reason VARCHAR(240);
        ALTER TABLE users ADD COLUMN IF NOT EXISTS banned_by BIGINT REFERENCES users(id);
        CREATE TABLE IF NOT EXISTS admin_audit_logs (
            id BIGSERIAL PRIMARY KEY,
            admin_user_id BIGINT NOT NULL REFERENCES users(id),
            target_user_id BIGINT REFERENCES users(id),
            action VARCHAR(40) NOT NULL,
            details JSONB NOT NULL DEFAULT '{}'::jsonb,
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
        );
        CREATE INDEX IF NOT EXISTS admin_audit_created_idx ON admin_audit_logs(created_at DESC);
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
        CREATE TABLE IF NOT EXISTS casino_daily_claims (
            id BIGSERIAL PRIMARY KEY,
            user_id BIGINT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            claim_date DATE NOT NULL,
            streak SMALLINT NOT NULL CHECK (streak BETWEEN 1 AND 7),
            reward_kopecks BIGINT NOT NULL CHECK (reward_kopecks > 0),
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            UNIQUE(user_id, claim_date)
        );
        CREATE INDEX IF NOT EXISTS casino_daily_claims_user_date_idx
            ON casino_daily_claims(user_id, claim_date DESC);
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
        CREATE TABLE IF NOT EXISTS casino_instant_rounds (
            id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            user_id BIGINT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            request_key VARCHAR(64) NOT NULL,
            game VARCHAR(16) NOT NULL CHECK (game IN ('dice','hilo','baccarat','tower')),
            choice VARCHAR(16) NOT NULL,
            bet_kopecks BIGINT NOT NULL CHECK (bet_kopecks > 0 AND bet_kopecks <= 100000000000),
            result JSONB NOT NULL,
            won BOOLEAN NOT NULL,
            multiplier NUMERIC(12,4) NOT NULL CHECK (multiplier >= 0),
            payout_kopecks BIGINT NOT NULL DEFAULT 0 CHECK (payout_kopecks >= 0),
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            UNIQUE(user_id, request_key)
        );
        CREATE INDEX IF NOT EXISTS casino_instant_user_history_idx
            ON casino_instant_rounds(user_id, created_at DESC);
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
    initialize_extended_games(pool)


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
    amount_kopecks: int = Field(gt=0, le=100_000_000_000)
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
            "SELECT id, account_id, username, email, created_at, banned_at, ban_reason FROM users WHERE id = %s",
            (user_id,),
        ).fetchone()
    if not user:
        raise HTTPException(status_code=401, detail="Пользователь не найден")
    if user.get("banned_at") is not None:
        raise HTTPException(status_code=403, detail="Аккаунт заблокирован. Обратись в поддержку.")
    return user


User = Annotated[dict, Depends(current_user)]
register_extended_routes(app, pool, current_user)


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
<!doctype html><html lang="ru"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><meta name="theme-color" content="#090711"><title>NEON CONTROL</title>
<style>
:root{color-scheme:dark;--bg:#090711;--panel:#151020;--line:#332443;--purple:#a855f7;--pink:#ec4899;--muted:#a99bb9;--text:#f8f2ff;--green:#55e6a5;--red:#ff647f}*{box-sizing:border-box}body{margin:0;background:radial-gradient(ellipse at 50% -20%,#42215e 0,transparent 58%),var(--bg);color:var(--text);font:14px Inter,system-ui,sans-serif;min-height:100vh;padding:24px 14px}main{width:min(1100px,100%);margin:auto}.brand{font-weight:950;letter-spacing:3px;color:#dfc4ff;font-size:13px}.brand span{color:var(--pink)}h1{font-size:30px;margin:10px 0 4px}.sub,.muted{color:var(--muted)}.sub{margin:0 0 22px}.panel{background:linear-gradient(145deg,#1b1428,#100d18);border:1px solid var(--line);border-radius:18px;padding:19px;margin:13px 0;box-shadow:0 14px 40px #0005}.panel h2{font-size:16px;margin:0 0 14px}.grid{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:11px}.stat{background:#100c18;border:1px solid #342444;border-radius:14px;padding:15px}.stat small{color:var(--muted)}.stat strong{display:block;font-size:24px;margin-top:8px;overflow-wrap:anywhere}.field{display:grid;gap:7px;margin:12px 0}.field label{font-size:12px;color:#cbbbd9}.field input,.field select{height:44px;border-radius:11px;border:1px solid #493657;background:#0b0911;color:white;padding:0 12px;font-size:15px;width:100%}button{min-height:43px;border:0;border-radius:11px;padding:0 15px;background:linear-gradient(100deg,#9333ea,#db2777);color:white;font-weight:800;font-size:14px;cursor:pointer;margin:4px 5px 4px 0}button.secondary{background:#30243c;border:1px solid #493657}button.danger{background:linear-gradient(100deg,#a51f45,#e34265)}button.good{background:linear-gradient(100deg,#087e60,#20b987)}button:disabled{opacity:.5}.row{display:flex;align-items:center;justify-content:space-between;gap:12px;flex-wrap:wrap}.hidden{display:none!important}.status{padding:11px;border-radius:10px;margin-top:10px;display:none;overflow-wrap:anywhere}.status.error,.status.ok{display:block}.status.error{color:#ffabb8;background:#351521}.status.ok{color:#b8ffd9;background:#123025}.user{border:1px solid #483253;border-radius:14px;padding:15px;margin-top:15px}.balance{font-size:26px;font-weight:900;color:#dfc4ff;margin:8px 0}.pill{display:inline-block;border-radius:30px;padding:5px 9px;background:#20382e;color:#8ef0bc;font-size:11px;font-weight:800}.pill.banned{background:#421c2a;color:#ff9daf}.tabs{display:flex;gap:7px;flex-wrap:wrap;margin:15px 0}.tabs button{background:#21182c}.tabs button.active{background:linear-gradient(100deg,#9333ea,#db2777)}.tablewrap{overflow:auto}table{width:100%;border-collapse:collapse;min-width:620px}th,td{text-align:left;padding:10px 8px;border-bottom:1px solid #332443;font-size:12px;vertical-align:top}th{color:#cbbbd9}td{color:#eee5f8}.muted{font-size:12px}.split{display:grid;grid-template-columns:1fr 1fr;gap:12px}.dangerText{color:#ff91a5}@media(max-width:650px){body{padding:17px 10px}.panel{padding:14px}.grid{grid-template-columns:repeat(2,minmax(0,1fr))}.stat strong{font-size:20px}.split{grid-template-columns:1fr}h1{font-size:25px}}
</style></head><body><main><div class="brand">NEON <span>CONTROL</span> / ADMIN</div><h1>Центр управления</h1><p class="sub">Пользователи · кошельки · казино · безопасность</p>
<section class="panel" id="loginPanel"><h2>Вход администратора</h2><div class="field"><label>Email</label><input id="email" type="email" autocomplete="username"></div><div class="field"><label>Пароль</label><input id="password" type="password" autocomplete="current-password"></div><button id="loginBtn">Войти</button><div class="status" id="loginStatus"></div></section>
<div id="adminPanel" class="hidden">
<div class="row"><div class="muted">Сводка системы в реальном времени</div><button class="secondary" id="logoutBtn">Выйти</button></div>
<section class="panel"><div class="row"><h2>Обзор системы</h2><button class="secondary" id="refreshBtn">Обновить</button></div><div class="grid">
<div class="stat"><small>Всего аккаунтов</small><strong id="usersCount">—</strong></div><div class="stat"><small>Заблокировано</small><strong id="bannedCount">—</strong></div><div class="stat"><small>Операций за всё время</small><strong id="txCount">—</strong></div><div class="stat"><small>Основные кошельки</small><strong id="walletTotal">—</strong></div><div class="stat"><small>Балансы казино</small><strong id="casinoTotal">—</strong></div><div class="stat"><small>Операций за 24 часа</small><strong id="tx24">—</strong></div>
</div></section>
<div class="tabs"><button class="active" data-tab="accounts">Аккаунты</button><button data-tab="transactions">Операции</button><button data-tab="audit">Действия админов</button></div>
<section class="panel" id="accountsTab"><h2>Поиск пользователя</h2><div class="row"><div class="field" style="flex:1;min-width:210px"><label>ID аккаунта или email</label><input id="accountId" maxlength="254" placeholder="ID или email"></div><button id="searchBtn">Найти</button></div><div class="status" id="searchStatus"></div>
<div class="user hidden" id="userCard"><div class="row"><div><strong id="userName"></strong><div class="muted" id="userEmail"></div><div class="muted" id="userId"></div></div><span id="userStatus" class="pill">АКТИВЕН</span></div>
<div class="grid" style="margin-top:13px"><div class="stat"><small>Основной кошелёк</small><strong id="userBalance">—</strong></div><div class="stat"><small>Казино</small><strong id="casinoBalance">—</strong></div><div class="stat"><small>Регистрация</small><strong id="createdAt" style="font-size:14px">—</strong></div></div>
<div id="banInfo" class="muted dangerText hidden"></div>
<div class="split"><div><h2 style="margin-top:17px">Административное зачисление</h2><div class="field"><label>Сумма, виртуальные ₽</label><input id="amount" type="number" min=".01" max="1000000000" step=".01" placeholder="1000"></div><div class="field"><label>Комментарий</label><input id="reason" maxlength="140" value="Административное пополнение"></div><button id="creditBtn">Зачислить на основной баланс</button><div class="status" id="creditStatus"></div></div>
<div><h2 style="margin-top:17px">Безопасность аккаунта</h2><div class="field"><label>Причина блокировки</label><input id="banReason" maxlength="240" placeholder="Причина бана"></div><button class="danger" id="banBtn">Заблокировать аккаунт</button><button class="good hidden" id="unbanBtn">Разблокировать аккаунт</button><div class="status" id="banStatus"></div></div></div>
<h2 style="margin-top:18px">Последние операции пользователя</h2><div class="tablewrap"><table><thead><tr><th>Дата</th><th>Тип</th><th>Сумма</th><th>Описание</th></tr></thead><tbody id="userTx"></tbody></table></div></div></section>
<section class="panel hidden" id="transactionsTab"><div class="row"><h2>Журнал операций</h2><button class="secondary" id="txRefresh">Обновить</button></div><div class="split"><div class="field"><label>Поиск по ID / email / описанию</label><input id="txQuery" placeholder="Поиск"></div><div class="field"><label>Тип операции</label><select id="txKind"><option value="">Все типы</option><option>transfer</option><option>admin_credit</option><option>casino_deposit</option><option>casino_withdraw</option></select></div></div><button id="txSearch">Применить фильтр</button><div class="tablewrap"><table><thead><tr><th>Дата</th><th>Тип</th><th>Отправитель</th><th>Получатель</th><th>Сумма</th><th>Комментарий</th></tr></thead><tbody id="txRows"></tbody></table></div></section>
<section class="panel hidden" id="auditTab"><div class="row"><h2>Журнал действий администратора</h2><button class="secondary" id="auditRefresh">Обновить</button></div><div class="tablewrap"><table><thead><tr><th>Дата</th><th>Администратор</th><th>Действие</th><th>Аккаунт</th><th>Детали</th></tr></thead><tbody id="auditRows"></tbody></table></div></section>
</div><p class="muted">Все суммы — виртуальные тестовые средства. Административные действия записываются в журнал.</p></main>
<script>
const $=id=>document.getElementById(id);let token=sessionStorage.getItem("neon_admin_token")||"",selected=null;
const money=n=>(Number(n||0)/100).toLocaleString("ru-RU",{minimumFractionDigits:2,maximumFractionDigits:2})+" ₽";
const date=s=>s?new Date(s).toLocaleString("ru-RU"):"—";
function status(id,msg,kind){const el=$(id);el.textContent=msg;el.className="status "+kind}
async function api(path,options={}){const res=await fetch(path,{...options,headers:{"Content-Type":"application/json","Authorization":"Bearer "+token,...(options.headers||{})}});const data=await res.json().catch(()=>({detail:"Ошибка ответа сервера"}));if(!res.ok)throw new Error(data.detail||"Ошибка запроса");return data}
function showAdmin(){$("loginPanel").classList.add("hidden");$("adminPanel").classList.remove("hidden");refreshAll()}
function showLogin(){$("adminPanel").classList.add("hidden");$("loginPanel").classList.remove("hidden")}
async function refreshStats(){const d=await api("/v1/admin/stats");$("usersCount").textContent=Number(d.users_count).toLocaleString("ru-RU");$("bannedCount").textContent=Number(d.banned_count).toLocaleString("ru-RU");$("txCount").textContent=Number(d.transactions_count).toLocaleString("ru-RU");$("walletTotal").textContent=money(d.wallet_total);$("casinoTotal").textContent=money(d.casino_total);$("tx24").textContent=Number(d.transactions_24h).toLocaleString("ru-RU")}
async function loadTx(){const q=encodeURIComponent($("txQuery").value.trim()),k=encodeURIComponent($("txKind").value);const d=await api("/v1/admin/transactions?q="+q+"&kind="+k+"&limit=100");$("txRows").innerHTML=d.items.map(x=>"<tr><td>"+date(x.created_at)+"</td><td>"+x.kind+"</td><td>"+(x.sender_account_id||"—")+"</td><td>"+(x.recipient_account_id||"—")+"</td><td>"+money(x.amount_kopecks)+"</td><td>"+x.description+"</td></tr>").join("")||'<tr><td colspan="6">Операций нет</td></tr>'}
async function loadAudit(){const d=await api("/v1/admin/audit");$("auditRows").innerHTML=d.items.map(x=>"<tr><td>"+date(x.created_at)+"</td><td>"+x.admin_account_id+"</td><td>"+x.action+"</td><td>"+(x.target_account_id||"—")+"</td><td>"+JSON.stringify(x.details||{})+"</td></tr>").join("")||'<tr><td colspan="5">Записей пока нет</td></tr>'}
async function refreshAll(){try{await Promise.all([refreshStats(),loadTx(),loadAudit()])}catch(e){console.error(e)}}
async function loadUser(){const q=$("accountId").value.trim();if(!q)throw new Error("Введи ID или email");const d=await api("/v1/admin/users?q="+encodeURIComponent(q));if(!d.items.length)throw new Error("Аккаунт не найден");selected=d.items[0];const full=await api("/v1/admin/users/"+encodeURIComponent(selected.account_id));selected=full;$("userName").textContent=full.username;$("userEmail").textContent=full.email;$("userId").textContent="ID: "+full.account_id;$("userBalance").textContent=money(full.balance_kopecks);$("casinoBalance").textContent=money(full.casino_balance_kopecks);$("createdAt").textContent=date(full.created_at);$("userStatus").textContent=full.banned?"ЗАБЛОКИРОВАН":"АКТИВЕН";$("userStatus").className="pill"+(full.banned?" banned":"");$("banBtn").classList.toggle("hidden",full.banned);$("unbanBtn").classList.toggle("hidden",!full.banned);$("banInfo").classList.toggle("hidden",!full.banned);$("banInfo").textContent=full.banned?"Причина: "+(full.ban_reason||"не указана")+" · "+date(full.banned_at):"";$("userTx").innerHTML=full.transactions.map(t=>"<tr><td>"+date(t.created_at)+"</td><td>"+t.kind+"</td><td>"+money(t.amount_kopecks)+"</td><td>"+t.description+"</td></tr>").join("")||'<tr><td colspan="4">Истории пока нет</td></tr>';$("userCard").classList.remove("hidden")}
$("loginBtn").onclick=async()=>{const b=$("loginBtn");b.disabled=true;try{const r=await fetch("/v1/auth/login",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({email:$("email").value,password:$("password").value})});const d=await r.json();if(!r.ok)throw new Error(d.detail||"Не удалось войти");token=d.access_token;sessionStorage.setItem("neon_admin_token",token);$("password").value="";await api("/v1/admin/me");showAdmin()}catch(e){token="";sessionStorage.removeItem("neon_admin_token");status("loginStatus",e.message,"error")}finally{b.disabled=false}};
$("logoutBtn").onclick=()=>{token="";sessionStorage.removeItem("neon_admin_token");selected=null;showLogin()};
$("searchBtn").onclick=async()=>{try{await loadUser();status("searchStatus","Аккаунт загружен","ok")}catch(e){$("userCard").classList.add("hidden");status("searchStatus",e.message,"error")}};
$("creditBtn").onclick=async()=>{if(!selected)return;const amount=Number($("amount").value);if(!Number.isFinite(amount)||amount<=0||amount>1000000000)return status("creditStatus","Сумма должна быть от 0,01 до 1 000 000 000 ₽","error");if(!confirm("Зачислить "+amount+" ₽ аккаунту "+selected.account_id+"?"))return;try{const r=await api("/v1/admin/wallet/credit",{method:"POST",body:JSON.stringify({account_id:selected.account_id,amount_kopecks:Math.round(amount*100),description:$("reason").value.trim()||"Административное пополнение",request_key:crypto.randomUUID().replaceAll("-","")})});status("creditStatus","Зачислено "+money(r.amount_kopecks),"ok");await loadUser();await refreshAll()}catch(e){status("creditStatus",e.message,"error")}};
$("banBtn").onclick=async()=>{if(!selected)return;const reason=$("banReason").value.trim();if(!reason)return status("banStatus","Укажи причину блокировки","error");if(!confirm("Заблокировать аккаунт "+selected.account_id+"? Пользователь не сможет войти."))return;try{await api("/v1/admin/users/ban",{method:"POST",body:JSON.stringify({account_id:selected.account_id,reason})});status("banStatus","Аккаунт заблокирован","ok");await loadUser();await refreshAll()}catch(e){status("banStatus",e.message,"error")}};
$("unbanBtn").onclick=async()=>{if(!selected||!confirm("Разблокировать аккаунт "+selected.account_id+"?"))return;try{await api("/v1/admin/users/"+encodeURIComponent(selected.account_id)+"/unban",{method:"POST"});status("banStatus","Аккаунт разблокирован","ok");await loadUser();await refreshAll()}catch(e){status("banStatus",e.message,"error")}};
$("refreshBtn").onclick=refreshAll;$("txRefresh").onclick=loadTx;$("txSearch").onclick=loadTx;$("auditRefresh").onclick=loadAudit;
document.querySelectorAll("[data-tab]").forEach(b=>b.onclick=()=>{document.querySelectorAll("[data-tab]").forEach(x=>x.classList.toggle("active",x===b));["accounts","transactions","audit"].forEach(t=>$(t+"Tab").classList.toggle("hidden",t!==b.dataset.tab))});
async function verify(){if(!token)return showLogin();try{await api("/v1/admin/me");showAdmin()}catch{token="";sessionStorage.removeItem("neon_admin_token");showLogin()}}verify();
</script></body></html>
""")


class AdminBanRequest(BaseModel):
    account_id: str = Field(min_length=6, max_length=12, pattern=r"^[0-9]+$")
    reason: str = Field(default="Нарушение правил", min_length=1, max_length=240)


@app.get("/v1/admin/stats")
def admin_stats(admin: AdminUser):
    with pool.connection() as conn:
        row = conn.execute("""
            SELECT
              (SELECT COUNT(*) FROM users) AS users_count,
              (SELECT COUNT(*) FROM users WHERE banned_at IS NOT NULL) AS banned_count,
              (SELECT COALESCE(SUM(balance_kopecks),0) FROM wallets) AS wallet_total,
              (SELECT COALESCE(SUM(balance_kopecks),0) FROM casino_wallets) AS casino_total,
              (SELECT COUNT(*) FROM transactions) AS transactions_count,
              (SELECT COUNT(*) FROM transactions WHERE created_at >= NOW() - INTERVAL '24 hours') AS transactions_24h
        """).fetchone()
    return dict(row)


@app.get("/v1/admin/transactions")
def admin_transactions(admin: AdminUser, q: str = "", kind: str = "", limit: int = Query(50, ge=1, le=200)):
    with pool.connection() as conn:
        rows = conn.execute("""
            SELECT t.id, t.kind, t.description, t.amount_kopecks, t.created_at,
                   su.account_id AS sender_account_id, su.username AS sender_username,
                   ru.account_id AS recipient_account_id, ru.username AS recipient_username,
                   au.account_id AS admin_account_id
            FROM transactions t
            LEFT JOIN users su ON su.id=t.sender_user_id
            LEFT JOIN users ru ON ru.id=t.recipient_user_id
            LEFT JOIN users au ON au.id=t.admin_user_id
            WHERE (%s = '' OR t.kind = %s)
              AND (%s = '' OR su.account_id ILIKE %s OR ru.account_id ILIKE %s
                   OR su.email ILIKE %s OR ru.email ILIKE %s OR t.description ILIKE %s)
            ORDER BY t.created_at DESC LIMIT %s
        """, (kind, kind, q, f"%{q}%", f"%{q}%", f"%{q}%", f"%{q}%", f"%{q}%", limit)).fetchall()
    return {"items": [{
        "id": str(r["id"]), "kind": r["kind"], "description": r["description"],
        "amount_kopecks": r["amount_kopecks"], "created_at": r["created_at"].isoformat(),
        "sender_account_id": r["sender_account_id"], "sender_username": r["sender_username"],
        "recipient_account_id": r["recipient_account_id"], "recipient_username": r["recipient_username"],
        "admin_account_id": r["admin_account_id"]
    } for r in rows]}


@app.get("/v1/admin/audit")
def admin_audit(admin: AdminUser, limit: int = Query(50, ge=1, le=200)):
    with pool.connection() as conn:
        rows = conn.execute("""
            SELECT a.id, a.action, a.details, a.created_at,
                   au.account_id AS admin_account_id, tu.account_id AS target_account_id
            FROM admin_audit_logs a
            JOIN users au ON au.id=a.admin_user_id
            LEFT JOIN users tu ON tu.id=a.target_user_id
            ORDER BY a.created_at DESC LIMIT %s
        """, (limit,)).fetchall()
    return {"items": [{
        "id": r["id"], "action": r["action"], "details": r["details"],
        "created_at": r["created_at"].isoformat(),
        "admin_account_id": r["admin_account_id"], "target_account_id": r["target_account_id"]
    } for r in rows]}


@app.get("/v1/admin/users/{account_id}")
def admin_user_details(account_id: str, admin: AdminUser):
    with pool.connection() as conn:
        user = conn.execute("""
            SELECT u.id, u.account_id, u.username, u.email, u.created_at, u.banned_at, u.ban_reason,
                   COALESCE(w.balance_kopecks,0) AS balance_kopecks,
                   COALESCE(c.balance_kopecks,0) AS casino_balance_kopecks
            FROM users u LEFT JOIN wallets w ON w.user_id=u.id
            LEFT JOIN casino_wallets c ON c.user_id=u.id
            WHERE u.account_id=%s
        """, (account_id,)).fetchone()
        if not user:
            raise HTTPException(status_code=404, detail="Аккаунт не найден")
        txs = conn.execute("""
            SELECT id, kind, description, amount_kopecks, created_at
            FROM transactions
            WHERE sender_user_id=%s OR recipient_user_id=%s
            ORDER BY created_at DESC LIMIT 20
        """, (user["id"], user["id"])).fetchall()
    return {
        "account_id": user["account_id"], "username": user["username"], "email": user["email"],
        "created_at": user["created_at"].isoformat(),
        "balance_kopecks": user["balance_kopecks"], "casino_balance_kopecks": user["casino_balance_kopecks"],
        "banned": user["banned_at"] is not None,
        "banned_at": user["banned_at"].isoformat() if user["banned_at"] else None,
        "ban_reason": user["ban_reason"],
        "transactions": [{"id": str(t["id"]), "kind": t["kind"], "description": t["description"],
                          "amount_kopecks": t["amount_kopecks"], "created_at": t["created_at"].isoformat()}
                         for t in txs]
    }


@app.post("/v1/admin/users/ban")
def admin_ban_user(body: AdminBanRequest, admin: AdminUser):
    if body.account_id in ADMIN_ACCOUNT_IDS:
        raise HTTPException(status_code=403, detail="Нельзя заблокировать администратора")
    with pool.connection() as conn:
        with conn.transaction():
            target = conn.execute("SELECT id, banned_at FROM users WHERE account_id=%s FOR UPDATE",
                                  (body.account_id,)).fetchone()
            if not target:
                raise HTTPException(status_code=404, detail="Аккаунт не найден")
            if target["banned_at"] is None:
                conn.execute("""UPDATE users SET banned_at=NOW(), ban_reason=%s, banned_by=%s
                                 WHERE id=%s""", (body.reason.strip(), admin["id"], target["id"]))
                conn.execute("""INSERT INTO admin_audit_logs(admin_user_id,target_user_id,action,details)
                                 VALUES (%s,%s,'user_banned',%s::jsonb)""",
                             (admin["id"], target["id"], json.dumps({"reason": body.reason.strip()})))
    return {"status": "banned", "account_id": body.account_id}


@app.post("/v1/admin/users/{account_id}/unban")
def admin_unban_user(account_id: str, admin: AdminUser):
    with pool.connection() as conn:
        with conn.transaction():
            target = conn.execute("SELECT id, banned_at FROM users WHERE account_id=%s FOR UPDATE",
                                  (account_id,)).fetchone()
            if not target:
                raise HTTPException(status_code=404, detail="Аккаунт не найден")
            if target["banned_at"] is not None:
                conn.execute("UPDATE users SET banned_at=NULL, ban_reason=NULL, banned_by=NULL WHERE id=%s",
                             (target["id"],))
                conn.execute("""INSERT INTO admin_audit_logs(admin_user_id,target_user_id,action,details)
                                 VALUES (%s,%s,'user_unbanned','{}'::jsonb)""",
                             (admin["id"], target["id"]))
    return {"status": "active", "account_id": account_id}


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
            """SELECT u.account_id, u.username, u.email, u.created_at, u.banned_at, u.ban_reason,
                      COALESCE(w.balance_kopecks,0) AS balance_kopecks,
                      COALESCE(c.balance_kopecks,0) AS casino_balance_kopecks
               FROM users u LEFT JOIN wallets w ON w.user_id = u.id
               LEFT JOIN casino_wallets c ON c.user_id = u.id
               WHERE u.account_id = %s OR LOWER(u.email) = LOWER(%s)
               LIMIT 10""",
            (query, query),
        ).fetchall()
    return {"items": [{
        "account_id": row["account_id"], "username": row["username"],
        "email": row["email"], "balance_kopecks": row["balance_kopecks"],
        "casino_balance_kopecks": row["casino_balance_kopecks"],
        "created_at": row["created_at"].isoformat(), "banned": row["banned_at"] is not None,
        "ban_reason": row["ban_reason"],
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
    amount_kopecks: int = Field(gt=0, le=100_000_000_000)
    request_key: str = Field(min_length=16, max_length=100, pattern=r"^[a-zA-Z0-9_-]+$")


class CasinoDepositCreateRequest(BaseModel):
    amount_kopecks: int = Field(gt=0, le=100_000_000_000)


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
    bet_kopecks: int = Field(gt=0, le=100_000_000_000)


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




class InstantGameRequest(BaseModel):
    game: str = Field(pattern=r"^(dice|hilo|baccarat|tower)$")
    bet_kopecks: int = Field(gt=0, le=100_000_000_000)
    choice: str = Field(min_length=1, max_length=16)
    request_key: str = Field(min_length=16, max_length=64, pattern=r"^[a-zA-Z0-9_-]+$")


class PlinkoDropRequest(BaseModel):
    bet_kopecks: int = Field(gt=0, le=100_000_000_000)
    request_key: str = Field(min_length=16, max_length=64, pattern=r"^[a-zA-Z0-9_-]+$")


PLINKO_MULTIPLIERS = [8.00, 4.00, 2.00, 1.40, 1.10, 0.80, 0.50, 0.80, 1.10, 1.40, 2.00, 4.00, 8.00]


class MinesStartRequest(BaseModel):
    bet_kopecks: int = Field(gt=0, le=100_000_000_000)
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
    bet_kopecks: int = Field(gt=0, le=100_000_000_000)


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
    bet_kopecks: int = Field(gt=0, le=100_000_000_000)


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
    amount_kopecks: int = Field(gt=0, le=100_000_000_000)


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
    if total_bet > 100_000_000_000:
        raise HTTPException(status_code=422, detail="Общая сумма ставок не может превышать 1 000 000 000 ₽")
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
                """SELECT id, bets, winning_number, total_bet_kopecks, payout_kopecks, created_at
                   FROM casino_roulette_rounds WHERE user_id = %s AND request_key = %s""",
                (user["id"], body.request_key),
            ).fetchone()
            if previous:
                previous_bets = previous["bets"]
                if isinstance(previous_bets, str):
                    previous_bets = json.loads(previous_bets)
                if previous_bets != stored_bets:
                    raise HTTPException(status_code=409, detail="Ключ рулетки уже использован с другими ставками")
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
            # Maximum theoretical return: 1,000,000,000 ₽ × 36 for a straight-up number.
            if payout > 3_600_000_000_000:
                raise HTTPException(status_code=422, detail="Потенциальная выплата превышает системный предел")
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




def baccarat_card_value(rank: int) -> int:
    return 0 if rank >= 10 else rank


def draw_baccarat_hand() -> list[int]:
    return [secrets.randbelow(13) + 1 for _ in range(2)]


def baccarat_total(cards: list[int]) -> int:
    return sum(baccarat_card_value(card) for card in cards) % 10


def resolve_baccarat() -> dict:
    player = draw_baccarat_hand()
    banker = draw_baccarat_hand()
    player_total, banker_total = baccarat_total(player), baccarat_total(banker)
    if player_total not in (8, 9) and banker_total not in (8, 9):
        player_third = None
        if player_total <= 5:
            player_third = secrets.randbelow(13) + 1
            player.append(player_third)
            player_total = baccarat_total(player)
        if player_third is None:
            if banker_total <= 5:
                banker.append(secrets.randbelow(13) + 1)
        elif banker_total <= 2:
            banker.append(secrets.randbelow(13) + 1)
        elif banker_total == 3 and baccarat_card_value(player_third) != 8:
            banker.append(secrets.randbelow(13) + 1)
        elif banker_total == 4 and 2 <= baccarat_card_value(player_third) <= 7:
            banker.append(secrets.randbelow(13) + 1)
        elif banker_total == 5 and 4 <= baccarat_card_value(player_third) <= 7:
            banker.append(secrets.randbelow(13) + 1)
        elif banker_total == 6 and 6 <= baccarat_card_value(player_third) <= 7:
            banker.append(secrets.randbelow(13) + 1)
        banker_total = baccarat_total(banker)
    winner = "tie" if player_total == banker_total else ("player" if player_total > banker_total else "banker")
    return {"player_cards": player, "banker_cards": banker,
            "player_total": player_total, "banker_total": banker_total, "winner": winner}


@app.post("/v1/casino/instant/play", status_code=201)
def play_instant_game(body: InstantGameRequest, user: User):
    allowed = {
        "dice": {"over", "under"},
        "hilo": {"higher", "lower"},
        "baccarat": {"player", "banker", "tie"},
        "tower": {"1", "2", "3", "4", "5"},
    }
    if body.choice not in allowed[body.game]:
        raise HTTPException(status_code=422, detail="Некорректный выбор для этой игры")
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
            previous = conn.execute(
                """SELECT game, choice, bet_kopecks, result, won, multiplier, payout_kopecks, created_at
                   FROM casino_instant_rounds WHERE user_id = %s AND request_key = %s""",
                (user["id"], body.request_key),
            ).fetchone()
            if previous:
                if previous["game"] != body.game or previous["choice"] != body.choice or previous["bet_kopecks"] != body.bet_kopecks:
                    raise HTTPException(status_code=409, detail="Ключ раунда уже использован с другими параметрами")
                return {
                    "game": previous["game"], "choice": previous["choice"],
                    "bet_kopecks": previous["bet_kopecks"], "result": previous["result"],
                    "won": previous["won"], "multiplier": float(previous["multiplier"]),
                    "payout_kopecks": previous["payout_kopecks"],
                    "balance_kopecks": int(wallet["balance_kopecks"]),
                    "created_at": previous["created_at"].isoformat(), "replayed": True,
                }
            if int(wallet["balance_kopecks"]) < body.bet_kopecks:
                raise HTTPException(status_code=409, detail="Недостаточно средств на балансе казино")

            result: dict
            multiplier = 0.0
            won = False
            if body.game == "dice":
                roll = secrets.randbelow(100) + 1
                won = roll > 50 if body.choice == "over" else roll <= 50
                multiplier = 1.94
                result = {"roll": roll, "range": "1–100", "winning_side": "over" if roll > 50 else "under"}
            elif body.game == "hilo":
                ranks = [secrets.randbelow(13) + 2, secrets.randbelow(13) + 2]
                won = ranks[1] > ranks[0] if body.choice == "higher" else ranks[1] < ranks[0]
                multiplier = 1.94
                labels = {11:"J", 12:"Q", 13:"K", 14:"A"}
                result = {"first_card": labels.get(ranks[0], str(ranks[0])),
                          "second_card": labels.get(ranks[1], str(ranks[1])),
                          "first_value": ranks[0], "second_value": ranks[1], "tie": ranks[0] == ranks[1]}
            elif body.game == "baccarat":
                baccarat = resolve_baccarat()
                won = baccarat["winner"] == body.choice
                multiplier = {"player": 2.0, "banker": 1.95, "tie": 8.0}[body.choice]
                result = baccarat
            else:
                floors = int(body.choice)
                chance = 0.48 ** floors
                safe = secrets.randbelow(10_000) < int(chance * 10_000)
                won = safe
                multiplier = 0.96 / chance
                result = {"floors": floors, "safe_floors": floors if safe else secrets.randbelow(floors),
                          "fallen_on": None if safe else "trap", "success_chance_percent": round(chance * 100, 2)}

            payout = int(body.bet_kopecks * multiplier) if won else 0
            new_balance = int(wallet["balance_kopecks"]) - body.bet_kopecks + payout
            conn.execute(
                "UPDATE casino_wallets SET balance_kopecks = %s, updated_at = NOW() WHERE user_id = %s",
                (new_balance, user["id"]),
            )
            conn.execute(
                """INSERT INTO casino_instant_rounds
                   (user_id, request_key, game, choice, bet_kopecks, result, won, multiplier, payout_kopecks)
                   VALUES (%s, %s, %s, %s, %s, %s::jsonb, %s, %s, %s)""",
                (user["id"], body.request_key, body.game, body.choice, body.bet_kopecks,
                 json.dumps(result), won, multiplier if won else 0, payout),
            )
    return {
        "game": body.game, "choice": body.choice, "bet_kopecks": body.bet_kopecks,
        "result": result, "won": won, "multiplier": round(multiplier, 4) if won else 0,
        "payout_kopecks": payout, "net_kopecks": payout - body.bet_kopecks,
        "balance_kopecks": new_balance, "replayed": False,
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
                if previous["bet_kopecks"] != body.bet_kopecks:
                    raise HTTPException(status_code=409, detail="Ключ броска уже использован с другой ставкой")
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


@app.get("/v1/casino/plinko-history")
def plinko_history(user: User, limit: int = 12):
    limit = max(1, min(limit, 30))
    with pool.connection() as conn:
        rows = conn.execute(
            """SELECT id, bet_kopecks, bucket, multiplier, payout_kopecks, created_at
               FROM casino_plinko_rounds
               WHERE user_id = %s ORDER BY created_at DESC LIMIT %s""",
            (user["id"], limit),
        ).fetchall()
    return {"items": [{
        "round_id": str(row["id"]),
        "bet_kopecks": row["bet_kopecks"],
        "bucket": row["bucket"],
        "multiplier": float(row["multiplier"]),
        "payout_kopecks": row["payout_kopecks"],
        "created_at": row["created_at"].isoformat(),
    } for row in rows]}


@app.get("/v1/casino/roulette-history")
def roulette_history(user: User, limit: int = 12):
    limit = max(1, min(limit, 30))
    with pool.connection() as conn:
        rows = conn.execute(
            """SELECT winning_number, total_bet_kopecks, payout_kopecks, created_at
               FROM casino_roulette_rounds
               WHERE user_id = %s ORDER BY created_at DESC LIMIT %s""",
            (user["id"], limit),
        ).fetchall()
    return {"items": [{
        "winning_number": row["winning_number"],
        "color": "green" if row["winning_number"] == 0 else ("red" if row["winning_number"] in ROULETTE_RED_NUMBERS else "black"),
        "bet_kopecks": row["total_bet_kopecks"],
        "payout_kopecks": row["payout_kopecks"],
        "created_at": row["created_at"].isoformat()
    } for row in rows]}



# NEON player progression. All balances and rewards are virtual credits only.
VIP_LEVELS = [
    {"name": "Bronze", "threshold_kopecks": 0, "next_threshold_kopecks": 10_000_000},
    {"name": "Silver", "threshold_kopecks": 10_000_000, "next_threshold_kopecks": 100_000_000},
    {"name": "Gold", "threshold_kopecks": 100_000_000, "next_threshold_kopecks": 1_000_000_000},
    {"name": "Diamond", "threshold_kopecks": 1_000_000_000, "next_threshold_kopecks": None},
]
DAILY_REWARD_BASE_KOPECKS = 10_000  # 100 virtual RUB; streak scales to 7x.


def casino_progress_totals(conn, user_id: int) -> dict:
    row = conn.execute(
        """SELECT
             COALESCE(SUM(bet_kopecks), 0)::BIGINT AS wagered,
             COALESCE(SUM(payout_kopecks), 0)::BIGINT AS paid,
             COUNT(*)::BIGINT AS rounds
           FROM (
             SELECT bet_kopecks, payout_kopecks FROM casino_crash_rounds WHERE user_id = %s AND status <> 'active'
             UNION ALL SELECT bet_kopecks, payout_kopecks FROM casino_mines_rounds WHERE user_id = %s AND status <> 'active'
             UNION ALL SELECT bet_kopecks, payout_kopecks FROM casino_ladder_rounds WHERE user_id = %s AND status <> 'active'
             UNION ALL SELECT bet_kopecks, payout_kopecks FROM casino_blackjack_rounds WHERE user_id = %s AND status <> 'active'
             UNION ALL SELECT bet_kopecks, payout_kopecks FROM casino_plinko_rounds WHERE user_id = %s
             UNION ALL SELECT total_bet_kopecks AS bet_kopecks, payout_kopecks FROM casino_roulette_rounds WHERE user_id = %s
             UNION ALL SELECT bet_kopecks, payout_kopecks FROM casino_instant_rounds WHERE user_id = %s
           ) rounds""",
        (user_id, user_id, user_id, user_id, user_id, user_id, user_id),
    ).fetchone()
    return {"wagered_kopecks": int(row["wagered"]), "payout_kopecks": int(row["paid"]), "rounds": int(row["rounds"])}


def vip_payload(wagered_kopecks: int) -> dict:
    level_index = 0
    for index, level in enumerate(VIP_LEVELS):
        if wagered_kopecks >= level["threshold_kopecks"]:
            level_index = index
    current = VIP_LEVELS[level_index]
    next_level = VIP_LEVELS[level_index + 1] if level_index + 1 < len(VIP_LEVELS) else None
    if next_level:
        progress = min(100, max(0, (wagered_kopecks - current["threshold_kopecks"]) * 100 // (next_level["threshold_kopecks"] - current["threshold_kopecks"])))
    else:
        progress = 100
    return {
        "level": current["name"],
        "level_index": level_index,
        "wagered_kopecks": wagered_kopecks,
        "next_level": next_level["name"] if next_level else None,
        "next_threshold_kopecks": next_level["threshold_kopecks"] if next_level else None,
        "progress_percent": progress,
        "virtual_only": True,
    }


def daily_bonus_payload(conn, user_id: int, vip_level_index: int = 0) -> dict:
    row = conn.execute(
        """SELECT claim_date, streak, reward_kopecks
           FROM casino_daily_claims WHERE user_id = %s
           ORDER BY claim_date DESC LIMIT 1""",
        (user_id,),
    ).fetchone()
    today = datetime.now(timezone.utc).date()
    if not row:
        streak = 0
        claimed_today = False
        next_reward = DAILY_REWARD_BASE_KOPECKS
    else:
        last_date = row["claim_date"]
        claimed_today = last_date == today
        streak = int(row["streak"]) if claimed_today or last_date == today - timedelta(days=1) else 0
        next_streak = min(7, streak + 1)
        next_reward = DAILY_REWARD_BASE_KOPECKS * next_streak
    vip_bonus_bps = [10_000, 11_000, 12_500, 15_000][max(0, min(3, vip_level_index))]
    next_reward = (next_reward * vip_bonus_bps) // 10_000
    return {
        "vip_bonus_percent": (vip_bonus_bps - 10_000) // 100,
        "claimed_today": claimed_today,
        "streak": streak,
        "next_reward_kopecks": next_reward,
        "next_reward_rubles": next_reward / 100,
        "next_claim_date": (today + timedelta(days=1)).isoformat() if claimed_today else today.isoformat(),
    }


@app.get("/v1/casino/player-center")
def casino_player_center(user: User):
    with pool.connection() as conn:
        totals = casino_progress_totals(conn, user["id"])
        vip = vip_payload(totals["wagered_kopecks"])
        daily = daily_bonus_payload(conn, user["id"], vip["level_index"])
    return {"vip": vip, "daily": daily, "totals": totals}


@app.post("/v1/casino/daily-bonus/claim", status_code=201)
def claim_casino_daily_bonus(user: User):
    today = datetime.now(timezone.utc).date()
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
            last = conn.execute(
                """SELECT claim_date, streak FROM casino_daily_claims
                   WHERE user_id = %s ORDER BY claim_date DESC LIMIT 1""",
                (user["id"],),
            ).fetchone()
            if last and last["claim_date"] == today:
                raise HTTPException(status_code=409, detail="Ежедневный бонус уже получен сегодня")
            streak = min(7, int(last["streak"]) + 1) if last and last["claim_date"] == today - timedelta(days=1) else 1
            totals = casino_progress_totals(conn, user["id"])
            vip = vip_payload(totals["wagered_kopecks"])
            vip_bonus_bps = [10_000, 11_000, 12_500, 15_000][vip["level_index"]]
            reward = (DAILY_REWARD_BASE_KOPECKS * streak * vip_bonus_bps) // 10_000
            conn.execute(
                """INSERT INTO casino_daily_claims(user_id, claim_date, streak, reward_kopecks)
                   VALUES (%s, %s, %s, %s)""",
                (user["id"], today, streak, reward),
            )
            conn.execute(
                """UPDATE casino_wallets SET balance_kopecks = balance_kopecks + %s, updated_at = NOW()
                   WHERE user_id = %s""",
                (reward, user["id"]),
            )
            new_balance = int(wallet["balance_kopecks"]) + reward
    return {
        "status": "claimed",
        "reward_kopecks": reward,
        "reward_rubles": reward / 100,
        "streak": streak,
        "vip_level": vip["level"],
        "vip_bonus_percent": (vip_bonus_bps - 10_000) // 100,
        "balance_kopecks": new_balance,
        "virtual_only": True,
    }


@app.get("/v1/casino/leaderboard")
def casino_leaderboard(user: User, period: str = Query("all", pattern=r"^(all|week|day)$"), limit: int = Query(10, ge=1, le=50)):
    interval = {"day": "1 day", "week": "7 days", "all": None}[period]
    time_filter = "TRUE" if interval is None else "created_at >= NOW() - INTERVAL '" + interval + "'"
    with pool.connection() as conn:
        rows = conn.execute(
            f"""WITH rounds AS (
                SELECT user_id, bet_kopecks, payout_kopecks, created_at FROM casino_crash_rounds WHERE status <> 'active' AND {time_filter}
                UNION ALL SELECT user_id, bet_kopecks, payout_kopecks, created_at FROM casino_mines_rounds WHERE status <> 'active' AND {time_filter}
                UNION ALL SELECT user_id, bet_kopecks, payout_kopecks, created_at FROM casino_ladder_rounds WHERE status <> 'active' AND {time_filter}
                UNION ALL SELECT user_id, bet_kopecks, payout_kopecks, created_at FROM casino_blackjack_rounds WHERE status <> 'active' AND {time_filter}
                UNION ALL SELECT user_id, bet_kopecks, payout_kopecks, created_at FROM casino_plinko_rounds WHERE {time_filter}
                UNION ALL SELECT user_id, total_bet_kopecks AS bet_kopecks, payout_kopecks, created_at FROM casino_roulette_rounds WHERE {time_filter}
                UNION ALL SELECT user_id, bet_kopecks, payout_kopecks, created_at FROM casino_instant_rounds WHERE {time_filter}
            )
            SELECT u.account_id, u.username,
                   SUM(r.bet_kopecks)::BIGINT AS wagered_kopecks,
                   SUM(r.payout_kopecks - r.bet_kopecks)::BIGINT AS net_kopecks,
                   COUNT(*)::BIGINT AS rounds
            FROM rounds r JOIN users u ON u.id = r.user_id
            GROUP BY u.id, u.account_id, u.username
            ORDER BY net_kopecks DESC, wagered_kopecks DESC, u.username ASC
            LIMIT %s""",
            (limit,),
        ).fetchall()
    return {"period": period, "items": [
        {"rank": index + 1, "account_id": row["account_id"], "username": row["username"],
         "wagered_kopecks": int(row["wagered_kopecks"]), "net_kopecks": int(row["net_kopecks"]),
         "rounds": int(row["rounds"])}
        for index, row in enumerate(rows)
    ], "virtual_only": True}


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
