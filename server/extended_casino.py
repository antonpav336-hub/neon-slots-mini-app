"""Additive server-authoritative game routes for NEON CASINO.

All money values are integer kopecks. Every state transition and wallet mutation
runs in one PostgreSQL transaction. Existing game routes are intentionally untouched.
"""
import math
import secrets
import uuid
from typing import Annotated

from fastapi import Depends, FastAPI, Header, HTTPException
from pydantic import BaseModel, Field
from psycopg.types.json import Jsonb


MAX_BET = 100_000_000_000
MAX_PAYOUT = 9_000_000_000_000_000
CASES = {
    "neon": {"price": 100_00, "items": [(0.55, 25_00), (0.28, 50_00), (0.12, 100_00), (0.045, 500_00), (0.005, 5000_00)]},
    "cyber": {"price": 500_00, "items": [(0.50, 100_00), (0.30, 250_00), (0.15, 750_00), (0.045, 2500_00), (0.005, 25000_00)]},
    "quantum": {"price": 2500_00, "items": [(0.50, 500_00), (0.30, 1250_00), (0.15, 3750_00), (0.045, 12500_00), (0.005, 125000_00)]},
}
SYMBOLS = [
    {"id": "crystal", "weight": 30, "mult": 0.35},
    {"id": "bolt", "weight": 25, "mult": 0.50},
    {"id": "crown", "weight": 15, "mult": 0.85},
    {"id": "gem", "weight": 12, "mult": 1.20},
    {"id": "wild", "weight": 5, "mult": 2.00},
    {"id": "x2", "weight": 8, "mult": 0.0},
    {"id": "x5", "weight": 5, "mult": 0.0},
]


class Stake(BaseModel):
    bet_kopecks: int = Field(gt=0, le=MAX_BET)
    request_key: str = Field(min_length=16, max_length=64, pattern=r"^[a-zA-Z0-9_-]+$")


class DiceStake(Stake):
    threshold: int = Field(2, ge=2, le=98)
    direction: str = Field("over", pattern=r"^(over|under)$")


class HiloStart(Stake):
    pass


class ActionKey(BaseModel):
    request_key: str = Field(min_length=16, max_length=64, pattern=r"^[a-zA-Z0-9_-]+$")


class HiloPick(BaseModel):
    choice: str = Field(pattern=r"^(higher|lower|cashout)$")
    request_key: str = Field(min_length=16, max_length=64, pattern=r"^[a-zA-Z0-9_-]+$")


class TowerStart(Stake):
    rows: int = Field(ge=3, le=12)
    columns: int = Field(ge=2, le=5)


class TowerPick(BaseModel):
    column: int = Field(ge=0, le=4)
    request_key: str = Field(min_length=16, max_length=64, pattern=r"^[a-zA-Z0-9_-]+$")


class CaseOpen(BaseModel):
    case_id: str = Field(pattern=r"^(neon|cyber|quantum)$")
    quantity: int = Field(1, ge=1, le=10)
    request_key: str = Field(min_length=16, max_length=64, pattern=r"^[a-zA-Z0-9_-]+$")


class SlotSpin(Stake):
    turbo: bool = False


def initialize_extended_games(pool):
    with pool.connection() as conn:
        conn.execute("""
        CREATE TABLE IF NOT EXISTS casino_extended_rounds (
          id UUID PRIMARY KEY,
          user_id BIGINT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
          game VARCHAR(16) NOT NULL CHECK (game IN ('hilo','tower')),
          bet_kopecks BIGINT NOT NULL CHECK (bet_kopecks > 0 AND bet_kopecks <= 100000000000),
          state JSONB NOT NULL,
          status VARCHAR(16) NOT NULL CHECK (status IN ('active','won','lost','cashed_out')),
          payout_kopecks BIGINT NOT NULL DEFAULT 0 CHECK (payout_kopecks >= 0),
          created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
          updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
        );
        DROP INDEX IF EXISTS casino_extended_one_active;
        CREATE UNIQUE INDEX IF NOT EXISTS casino_extended_one_active_user
          ON casino_extended_rounds(user_id) WHERE status='active';
        CREATE INDEX IF NOT EXISTS casino_extended_user_history
          ON casino_extended_rounds(user_id, created_at DESC);
        CREATE TABLE IF NOT EXISTS casino_extended_actions (
          user_id BIGINT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
          request_key VARCHAR(64) NOT NULL,
          action VARCHAR(24) NOT NULL,
          response JSONB NOT NULL,
          created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
          PRIMARY KEY(user_id, request_key)
        );
        CREATE TABLE IF NOT EXISTS casino_case_opens (
          id UUID PRIMARY KEY,
          user_id BIGINT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
          case_id VARCHAR(16) NOT NULL,
          price_kopecks BIGINT NOT NULL,
          prize_kopecks BIGINT NOT NULL,
          prize_tier SMALLINT NOT NULL,
          request_key VARCHAR(64) NOT NULL,
          created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
          UNIQUE(user_id, request_key)
        );
        CREATE TABLE IF NOT EXISTS casino_slot_spins (
          id UUID PRIMARY KEY,
          user_id BIGINT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
          bet_kopecks BIGINT NOT NULL,
          grid JSONB NOT NULL,
          cascades JSONB NOT NULL,
          payout_kopecks BIGINT NOT NULL,
          request_key VARCHAR(64) NOT NULL,
          created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
          UNIQUE(user_id, request_key)
        );
        """)
        conn.commit()


def _legacy_active(conn, user_id):
    return conn.execute("""
        SELECT id FROM casino_crash_rounds WHERE user_id=%s AND status='active'
        UNION ALL SELECT id FROM casino_mines_rounds WHERE user_id=%s AND status='active'
        UNION ALL SELECT id FROM casino_ladder_rounds WHERE user_id=%s AND status='active'
        UNION ALL SELECT id FROM casino_blackjack_rounds WHERE user_id=%s AND status='active'
        LIMIT 1
    """, (user_id,user_id,user_id,user_id)).fetchone()


def _wallet(conn, user_id):
    conn.execute("INSERT INTO casino_wallets(user_id,balance_kopecks) VALUES (%s,0) ON CONFLICT DO NOTHING", (user_id,))
    row = conn.execute("SELECT balance_kopecks FROM casino_wallets WHERE user_id=%s FOR UPDATE", (user_id,)).fetchone()
    return int(row["balance_kopecks"])


def _debit(conn, user_id, amount):
    balance = _wallet(conn, user_id)
    if balance < amount:
        raise HTTPException(409, "Недостаточно средств на балансе казино")
    conn.execute("UPDATE casino_wallets SET balance_kopecks=balance_kopecks-%s,updated_at=NOW() WHERE user_id=%s", (amount,user_id))


def _credit(conn, user_id, amount):
    if amount < 0 or amount > MAX_PAYOUT:
        raise HTTPException(422, "Недопустимый размер выплаты")
    conn.execute("UPDATE casino_wallets SET balance_kopecks=balance_kopecks+%s,updated_at=NOW() WHERE user_id=%s", (amount,user_id))


def _replay(conn, user_id, key, action):
    row=conn.execute("SELECT action,response FROM casino_extended_actions WHERE user_id=%s AND request_key=%s",(user_id,key)).fetchone()
    if row and row["action"] != action:
        raise HTTPException(409, "Ключ запроса уже использован для другого действия")
    return row["response"] if row else None


def _remember(conn,user_id,key,action,response):
    conn.execute("INSERT INTO casino_extended_actions(user_id,request_key,action,response) VALUES (%s,%s,%s,%s)",(user_id,key,action,Jsonb(response)))


def _card():
    return secrets.randbelow(13)+1


def _card_value(card):
    return min(card,10)


def _hilo_multiplier(card, choice):
    # 13 equally likely ranks; equal cards are a loss. Apply a 1% house edge.
    favorable=(13-card if choice=="higher" else card-1)
    probability=max(1/13, favorable/13)
    return math.floor((0.99/probability)*10000)/10000


def _round_payload(row):
    state=row["state"]
    return {"round_id":str(row["id"]),"game":row["game"],"status":row["status"],
            "bet_kopecks":row["bet_kopecks"],"step":state.get("step",0),
            "current_card":state.get("current_card"),"history":state.get("history",[]),
            "rows":state.get("rows"),"columns":state.get("columns"),
            "opened":state.get("opened",[]),"multiplier":state.get("multiplier",1),
            "payout_kopecks":row["payout_kopecks"]}


def register_extended_routes(app: FastAPI, pool, current_user):
    User = Annotated[dict, Depends(current_user)]

    @app.post("/v1/casino/dice/roll")
    def extended_dice(body: DiceStake, user: User):
        with pool.connection() as conn, conn.transaction():
            replay=_replay(conn,user["id"],body.request_key,"dice")
            if replay is not None: return replay
            _debit(conn,user["id"],body.bet_kopecks)
            roll=secrets.randbelow(100)+1
            won=(roll>body.threshold if body.direction=="over" else roll<body.threshold)
            chance=(100-body.threshold if body.direction=="over" else body.threshold-1)
            multiplier=math.floor((0.99/(chance/100))*10000)/10000 if won else 0
            payout=min(MAX_PAYOUT,int(body.bet_kopecks*multiplier)) if won else 0
            _credit(conn,user["id"],payout)
            result={"game":"dice","roll":roll,"threshold":body.threshold,"direction":body.direction,
                    "won":won,"multiplier":multiplier,"bet_kopecks":body.bet_kopecks,"payout_kopecks":payout}
            _remember(conn,user["id"],body.request_key,"dice",result)
            return result

    @app.post("/v1/casino/hilo/start",status_code=201)
    def hilo_start(body: HiloStart,user: User):
        with pool.connection() as conn, conn.transaction():
            replay=_replay(conn,user["id"],body.request_key,"hilo_start")
            if replay is not None:return replay
            if _legacy_active(conn,user["id"]): raise HTTPException(409,"Сначала заверши активную игру")
            active=conn.execute("SELECT id FROM casino_extended_rounds WHERE user_id=%s AND status='active' FOR UPDATE",(user["id"],)).fetchone()
            if active:raise HTTPException(409,"Сначала заверши активную игру")
            _debit(conn,user["id"],body.bet_kopecks)
            state={"step":0,"current_card":_card(),"history":[],"multiplier":1.0}
            row=conn.execute("INSERT INTO casino_extended_rounds(id,user_id,game,bet_kopecks,state,status) VALUES (%s,%s,'hilo',%s,%s,'active') RETURNING *",(uuid.uuid4(),user["id"],body.bet_kopecks,Jsonb(state))).fetchone()
            result=_round_payload(row);_remember(conn,user["id"],body.request_key,"hilo_start",result);return result

    @app.get("/v1/casino/hilo/active")
    def hilo_active(user: User):
        with pool.connection() as conn:
            row=conn.execute("SELECT * FROM casino_extended_rounds WHERE user_id=%s AND game='hilo' AND status='active' ORDER BY created_at DESC LIMIT 1",(user["id"],)).fetchone()
        return {"round":_round_payload(row) if row else None}

    @app.post("/v1/casino/hilo/{round_id}/pick")
    def hilo_pick(round_id: str,body: HiloPick,user: User):
        with pool.connection() as conn,conn.transaction():
            replay=_replay(conn,user["id"],body.request_key,"hilo_pick")
            if replay is not None:return replay
            row=conn.execute("SELECT * FROM casino_extended_rounds WHERE id=%s AND user_id=%s AND game='hilo' FOR UPDATE",(round_id,user["id"])).fetchone()
            if not row or row["status"]!="active":raise HTTPException(409,"Раунд Hi-Lo не активен")
            state=dict(row["state"])
            if body.choice=="cashout":
                if state["step"]<1:raise HTTPException(409,"Сначала сделай прогноз")
                payout=int(row["bet_kopecks"]*state["multiplier"]);_credit(conn,user["id"],payout);status_value="cashed_out"
            else:
                old=state["current_card"];new=_card()
                won=(new>old if body.choice=="higher" else new<old)
                mult=_hilo_multiplier(old,body.choice)
                state["history"].append({"from":old,"choice":body.choice,"to":new,"won":won})
                state["step"]+=1
                if won:
                    state["current_card"]=new;state["multiplier"]=min(round(state["multiplier"]*mult,4), MAX_PAYOUT / row["bet_kopecks"]);status_value="active";payout=0
                else:status_value="lost";payout=0
            row=conn.execute("UPDATE casino_extended_rounds SET state=%s,status=%s,payout_kopecks=%s,updated_at=NOW() WHERE id=%s RETURNING *",(Jsonb(state),status_value,payout,row["id"])).fetchone()
            result=_round_payload(row);_remember(conn,user["id"],body.request_key,"hilo_pick",result);return result

    @app.post("/v1/casino/tower/start",status_code=201)
    def tower_start(body: TowerStart,user: User):
        with pool.connection() as conn,conn.transaction():
            replay=_replay(conn,user["id"],body.request_key,"tower_start")
            if replay is not None:return replay
            if _legacy_active(conn,user["id"]): raise HTTPException(409,"Сначала заверши активную игру")
            active=conn.execute("SELECT id FROM casino_extended_rounds WHERE user_id=%s AND status='active' FOR UPDATE",(user["id"],)).fetchone()
            if active:raise HTTPException(409,"Сначала заверши активную игру")
            _debit(conn,user["id"],body.bet_kopecks)
            traps=[[secrets.randbelow(body.columns)] for _ in range(body.rows)]
            state={"step":0,"rows":body.rows,"columns":body.columns,"traps":traps,"opened":[],"multiplier":1.0}
            row=conn.execute("INSERT INTO casino_extended_rounds(id,user_id,game,bet_kopecks,state,status) VALUES (%s,%s,'tower',%s,%s,'active') RETURNING *",(uuid.uuid4(),user["id"],body.bet_kopecks,Jsonb(state))).fetchone()
            result=_round_payload(row);_remember(conn,user["id"],body.request_key,"tower_start",result);return result

    @app.get("/v1/casino/tower/active")
    def tower_active(user: User):
        with pool.connection() as conn:
            row=conn.execute("SELECT * FROM casino_extended_rounds WHERE user_id=%s AND game='tower' AND status='active' ORDER BY created_at DESC LIMIT 1",(user["id"],)).fetchone()
        return {"round":_round_payload(row) if row else None}

    @app.post("/v1/casino/tower/{round_id}/pick")
    def tower_pick(round_id: str,body: TowerPick,user: User):
        with pool.connection() as conn,conn.transaction():
            replay=_replay(conn,user["id"],body.request_key,"tower_pick")
            if replay is not None:return replay
            row=conn.execute("SELECT * FROM casino_extended_rounds WHERE id=%s AND user_id=%s AND game='tower' FOR UPDATE",(round_id,user["id"])).fetchone()
            if not row or row["status"]!="active":raise HTTPException(409,"Башня не активна")
            state=dict(row["state"]);step=state["step"]
            if body.column>=state["columns"]:raise HTTPException(422,"Недопустимая колонка")
            if body.column==state["traps"][step][0]:
                status_value="lost";payout=0
            else:
                state["opened"].append(body.column);state["step"]+=1
                state["multiplier"]=math.floor((((0.99*state["columns"]/(state["columns"]-1))**state["step"])*100))/100
                status_value="won" if state["step"]==state["rows"] else "active"
                payout=int(row["bet_kopecks"]*state["multiplier"]) if status_value=="won" else 0
                if payout:_credit(conn,user["id"],payout)
            row=conn.execute("UPDATE casino_extended_rounds SET state=%s,status=%s,payout_kopecks=%s,updated_at=NOW() WHERE id=%s RETURNING *",(Jsonb(state),status_value,payout,row["id"])).fetchone()
            result=_round_payload(row);_remember(conn,user["id"],body.request_key,"tower_pick",result);return result

    @app.post("/v1/casino/tower/{round_id}/cashout")
    def tower_cashout(round_id: str,body: ActionKey,user: User):
        with pool.connection() as conn,conn.transaction():
            replay=_replay(conn,user["id"],body.request_key,"tower_cashout")
            if replay is not None:return replay
            row=conn.execute("SELECT * FROM casino_extended_rounds WHERE id=%s AND user_id=%s AND game='tower' FOR UPDATE",(round_id,user["id"])).fetchone()
            if not row or row["status"]!="active":raise HTTPException(409,"Башня не активна")
            state=dict(row["state"])
            if state["step"]<1:raise HTTPException(409,"Поднимись хотя бы на один этаж")
            payout=int(row["bet_kopecks"]*state["multiplier"]);_credit(conn,user["id"],payout)
            row=conn.execute("UPDATE casino_extended_rounds SET status='cashed_out',payout_kopecks=%s,updated_at=NOW() WHERE id=%s RETURNING *",(payout,row["id"])).fetchone()
            result=_round_payload(row);_remember(conn,user["id"],body.request_key,"tower_cashout",result);return result

    @app.get("/v1/casino/cases/catalog")
    def cases_catalog(user: User):
        return {"items":[{"id":key,"price_kopecks":value["price"],"prizes":[{"tier":i+1,"chance":p,"payout_kopecks":v} for i,(p,v) in enumerate(value["items"])]} for key,value in CASES.items()]}

    @app.post("/v1/casino/cases/open")
    def cases_open(body: CaseOpen,user: User):
        with pool.connection() as conn,conn.transaction():
            replay=_replay(conn,user["id"],body.request_key,"case_open")
            if replay is not None:return replay
            config=CASES[body.case_id];total=config["price"]*body.quantity
            _debit(conn,user["id"],total);prizes=[]
            for _ in range(body.quantity):
                draw=secrets.randbelow(1_000_000)/1_000_000;acc=0;selected=config["items"][-1]
                for item in config["items"]:
                    acc+=item[0]
                    if draw<acc:selected=item;break
                prizes.append({"tier":config["items"].index(selected)+1,"payout_kopecks":selected[1]})
            payout=sum(x["payout_kopecks"] for x in prizes);_credit(conn,user["id"],payout)
            result={"case_id":body.case_id,"quantity":body.quantity,"price_kopecks":total,"prizes":prizes,"payout_kopecks":payout}
            conn.execute("INSERT INTO casino_case_opens(id,user_id,case_id,price_kopecks,prize_kopecks,prize_tier,request_key) VALUES (%s,%s,%s,%s,%s,%s,%s)",(uuid.uuid4(),user["id"],body.case_id,total,payout,max(x["tier"] for x in prizes),body.request_key))
            _remember(conn,user["id"],body.request_key,"case_open",result);return result

    @app.post("/v1/casino/slots/spin")
    def slots_spin(body: SlotSpin,user: User):
        with pool.connection() as conn,conn.transaction():
            replay=_replay(conn,user["id"],body.request_key,"slot_spin")
            if replay is not None:return replay
            _debit(conn,user["id"],body.bet_kopecks)
            weights=[x["weight"] for x in SYMBOLS];ids=[x["id"] for x in SYMBOLS]
            def symbol(): return _weighted_symbol()
            def _weighted_symbol():
                n=secrets.randbelow(sum(weights))
                for item in SYMBOLS:
                    n-=item["weight"]
                    if n<0:return item["id"]
                return "crystal"
            grid=[[symbol() for _ in range(6)] for _ in range(6)]
            cascades=[];total_payout=0;multiplier=1
            for cascade_index in range(5):
                hits=[]
                for y,row_symbols in enumerate(grid):
                    x=0
                    while x<6:
                        end=x+1
                        while end<6 and row_symbols[end]==row_symbols[x]:end+=1
                        if end-x>=3 and row_symbols[x] not in ("x2","x5"):
                            hits.append({"row":y,"start":x,"length":end-x,"symbol":row_symbols[x]})
                        x=end
                if not hits:break
                cleared=set()
                for hit in hits:
                    sym=next(s for s in SYMBOLS if s["id"]==hit["symbol"])
                    line=int(body.bet_kopecks*sym["mult"]*(hit["length"]-2)*multiplier)
                    total_payout+=line
                    for x in range(hit["start"],hit["start"]+hit["length"]):cleared.add((hit["row"],x))
                bonus_multiplier=1
                for y in range(6):
                    for x in range(6):
                        token=grid[y][x]
                        if token in ("x2","x5"):
                            bonus_multiplier*=2 if token=="x2" else 5
                            cleared.add((y,x))
                multiplier=min(multiplier*bonus_multiplier,25)
                cascades.append({"index":cascade_index+1,"wins":hits,"cleared":len(cleared),"multiplier":multiplier,"bonus_multiplier":bonus_multiplier})
                for x in range(6):
                    remaining=[grid[y][x] for y in range(6) if (y,x) not in cleared]
                    refill=[symbol() for _ in range(6-len(remaining))]
                    col=refill+remaining
                    for y in range(6):grid[y][x]=col[y]
                multiplier=min(multiplier*1.5,10)
            total_payout=min(MAX_PAYOUT,total_payout);_credit(conn,user["id"],total_payout)
            result={"spin_id":str(uuid.uuid4()),"grid":grid,"cascades":cascades,"bet_kopecks":body.bet_kopecks,"payout_kopecks":total_payout,"win":total_payout>body.bet_kopecks}
            conn.execute("INSERT INTO casino_slot_spins(id,user_id,bet_kopecks,grid,cascades,payout_kopecks,request_key) VALUES (%s,%s,%s,%s,%s,%s,%s)",(result["spin_id"],user["id"],body.bet_kopecks,Jsonb(grid),Jsonb(cascades),total_payout,body.request_key))
            _remember(conn,user["id"],body.request_key,"slot_spin",result);return result
