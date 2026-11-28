import os
import secrets
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
from pydantic import BaseModel, EmailStr, Field
from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool

DATABASE_URL = os.environ["DATABASE_URL"]
JWT_SECRET = os.environ["JWT_SECRET"]
JWT_ALGORITHM = "HS256"
TOKEN_TTL_HOURS = int(os.getenv("TOKEN_TTL_HOURS", "168"))
REGISTRATION_BONUS_KOPECKS = int(os.getenv("REGISTRATION_BONUS_KOPECKS", "500000"))

app = FastAPI(title="NEON WALLET API", version="1.0.0")
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


def public_user(user: dict) -> dict:
    return {
        "account_id": user["account_id"],
        "username": user["username"],
        "email": user["email"],
        "created_at": user["created_at"].isoformat(),
    }


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
                conn.execute(
                    """INSERT INTO casino_wallets(user_id, balance_kopecks) VALUES (%s, 0)
                       ON CONFLICT (user_id) DO NOTHING""",
                    (user["id"],),
                )
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
