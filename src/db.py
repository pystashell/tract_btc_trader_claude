"""SQLite 持久化层。

关键设计：
- `raw_responses` 保留所有原始 API 响应，方便日后复查。
- `orders` 按 `oid` 去重，同时记录 first_seen_at / last_seen_at，可以追踪订单生命周期。
- `fills` 按 `tid` 去重，是仓位重建的唯一来源。
- `clearinghouse_snapshots` + `positions_snapshots` 是交易所"真实"侧的快照。
- `open_orders_snapshots` 记录"我们在某个时刻看到的挂单 id 列表"，方便回放。
- `portfolio_history` 用 portfolio 端点返回的 (timestamp, accountValue) 序列做账户价值时间序列。
- `candles` 按 (interval, t) 去重。
"""
from __future__ import annotations
import json
import sqlite3
from pathlib import Path
from typing import Any, Iterable

from . import config


SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

-- 原始 API 响应留存，按时间和端点索引
CREATE TABLE IF NOT EXISTS raw_responses (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    fetched_at   INTEGER NOT NULL,
    endpoint     TEXT NOT NULL,
    request_json TEXT NOT NULL,
    response_json TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_raw_responses_endpoint_time
    ON raw_responses(endpoint, fetched_at);

-- 历史订单（按 oid 去重）
CREATE TABLE IF NOT EXISTS orders (
    oid               INTEGER PRIMARY KEY,
    coin              TEXT NOT NULL,
    side              TEXT NOT NULL,           -- 'B' 买入 / 'A' 卖出
    limit_px          REAL,
    sz                REAL,
    orig_sz           REAL,
    timestamp         INTEGER NOT NULL,        -- 订单创建时间 ms
    status            TEXT NOT NULL,           -- open / filled / canceled / marginCanceled / triggered / rejected ...
    status_timestamp  INTEGER NOT NULL,        -- 状态最近一次变化的时间 ms
    order_type        TEXT,
    tif               TEXT,
    reduce_only       INTEGER,
    is_trigger        INTEGER,
    trigger_px        REAL,
    trigger_condition TEXT,
    is_position_tpsl  INTEGER,
    cloid             TEXT,
    first_seen_at     INTEGER NOT NULL,        -- 本地第一次看到该订单的时间
    last_seen_at      INTEGER NOT NULL,        -- 本地最近一次看到该订单的时间
    last_source       TEXT NOT NULL,           -- 上次更新来源端点
    raw_json          TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_orders_timestamp ON orders(timestamp);
CREATE INDEX IF NOT EXISTS idx_orders_status_ts ON orders(status_timestamp);
CREATE INDEX IF NOT EXISTS idx_orders_coin_status ON orders(coin, status);

-- 成交（按 tid 去重）
CREATE TABLE IF NOT EXISTS fills (
    tid            INTEGER PRIMARY KEY,
    oid            INTEGER,
    coin           TEXT NOT NULL,
    side           TEXT NOT NULL,
    px             REAL NOT NULL,
    sz             REAL NOT NULL,
    time           INTEGER NOT NULL,
    start_position REAL,
    dir            TEXT,
    closed_pnl     REAL,
    fee            REAL,
    fee_token      TEXT,
    crossed        INTEGER,
    hash           TEXT,
    twap_id        TEXT,
    raw_json       TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_fills_time ON fills(time);
CREATE INDEX IF NOT EXISTS idx_fills_coin ON fills(coin);
CREATE INDEX IF NOT EXISTS idx_fills_oid ON fills(oid);

-- clearinghouseState 整体快照
CREATE TABLE IF NOT EXISTS clearinghouse_snapshots (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    fetched_at       INTEGER NOT NULL,
    server_time      INTEGER,
    account_value    REAL,
    total_ntl_pos    REAL,
    total_raw_usd    REAL,
    total_margin_used REAL,
    withdrawable    REAL,
    raw_json         TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_chs_fetched ON clearinghouse_snapshots(fetched_at);

-- 各 coin 在该快照下的仓位
CREATE TABLE IF NOT EXISTS position_snapshots (
    snapshot_id      INTEGER NOT NULL,
    coin             TEXT NOT NULL,
    szi              REAL,
    entry_px         REAL,
    position_value   REAL,
    unrealized_pnl   REAL,
    return_on_equity REAL,
    leverage_type    TEXT,
    leverage_value   REAL,
    liquidation_px   REAL,
    margin_used      REAL,
    raw_json         TEXT,
    PRIMARY KEY (snapshot_id, coin),
    FOREIGN KEY (snapshot_id) REFERENCES clearinghouse_snapshots(id)
);

-- 现货 clearinghouseState 快照（仅留存，不用于永续重建）
CREATE TABLE IF NOT EXISTS spot_clearinghouse_snapshots (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    fetched_at INTEGER NOT NULL,
    raw_json   TEXT NOT NULL
);

-- "在某个时间点看到的活跃挂单 id 列表"，方便后续回放
CREATE TABLE IF NOT EXISTS open_orders_snapshots (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    fetched_at INTEGER NOT NULL,
    source     TEXT NOT NULL,                  -- 'frontendOpenOrders' or 'openOrders'
    oids_json  TEXT NOT NULL                   -- JSON array of oids
);

-- portfolio 端点的账户价值/PnL 序列（按 period+timestamp 去重）
CREATE TABLE IF NOT EXISTS portfolio_history (
    period        TEXT NOT NULL,               -- day / week / month / allTime / perp* / ...
    timestamp     INTEGER NOT NULL,
    account_value REAL,
    pnl           REAL,
    PRIMARY KEY (period, timestamp)
);

-- BTC K 线 (Hyperliquid 自家数据)
CREATE TABLE IF NOT EXISTS candles (
    interval   TEXT NOT NULL,
    t          INTEGER NOT NULL,               -- 开盘时间 ms
    close_time INTEGER NOT NULL,               -- 收盘时间 ms
    o REAL, h REAL, l REAL, c REAL,
    v          REAL,                           -- Hyperliquid 自家成交量（不是 Binance / Coinbase）
    n          INTEGER,                        -- 该 K 线上的成交笔数
    PRIMARY KEY (interval, t)
);

-- 每次完整 update 的元数据
CREATE TABLE IF NOT EXISTS update_runs (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at    INTEGER NOT NULL,
    finished_at   INTEGER,
    success       INTEGER,
    notes         TEXT
);
"""


def connect(path: Path = config.DB_PATH) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.execute("PRAGMA foreign_keys=ON;")
    return conn


def init_db(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA)
    conn.commit()


# ---------- 写入辅助 ----------

def insert_raw(conn: sqlite3.Connection, fetched_at: int, endpoint: str,
               request_body: dict, response: Any) -> int:
    cur = conn.execute(
        "INSERT INTO raw_responses(fetched_at, endpoint, request_json, response_json)"
        " VALUES (?,?,?,?)",
        (fetched_at, endpoint, json.dumps(request_body, ensure_ascii=False),
         json.dumps(response, ensure_ascii=False)),
    )
    return cur.lastrowid


def upsert_order(conn: sqlite3.Connection, order_obj: dict, status: str,
                 status_ts: int, source: str, now_ms: int) -> None:
    """`order_obj` 是 historicalOrders/frontendOpenOrders 等返回里的 `order` 子对象。"""
    oid = int(order_obj["oid"])
    coin = order_obj["coin"]
    side = order_obj["side"]
    limit_px = _to_float(order_obj.get("limitPx"))
    sz = _to_float(order_obj.get("sz"))
    orig_sz = _to_float(order_obj.get("origSz"))
    ts = int(order_obj.get("timestamp", status_ts))
    order_type = order_obj.get("orderType")
    tif = order_obj.get("tif")
    reduce_only = 1 if order_obj.get("reduceOnly") else 0
    is_trigger = 1 if order_obj.get("isTrigger") else 0
    trigger_px = _to_float(order_obj.get("triggerPx"))
    trigger_condition = order_obj.get("triggerCondition")
    is_position_tpsl = 1 if order_obj.get("isPositionTpsl") else 0
    cloid = order_obj.get("cloid")
    raw = json.dumps(order_obj, ensure_ascii=False)

    # 已存在则更新。两个关键不变量：
    # 1) first_seen_at 永远保留最早时间。
    # 2) 终态（filled/canceled/marginCanceled/triggered/rejected）一旦观测到，
    #    就不被后来观测到的 'open' 覆盖。Hyperliquid 的 historicalOrders 有时同一 oid
    #    会出现两条记录：一条 'open'（订单创建）+ 一条终态，且两条 statusTimestamp
    #    完全相同；我们必须让终态胜出，否则会把已成交单错认为还在挂着。
    TERMINAL = "('filled','canceled','marginCanceled','triggered','rejected')"
    conn.execute(
        f"""
        INSERT INTO orders(
            oid, coin, side, limit_px, sz, orig_sz, timestamp,
            status, status_timestamp, order_type, tif, reduce_only,
            is_trigger, trigger_px, trigger_condition, is_position_tpsl,
            cloid, first_seen_at, last_seen_at, last_source, raw_json
        ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        ON CONFLICT(oid) DO UPDATE SET
            coin=excluded.coin,
            side=excluded.side,
            limit_px=excluded.limit_px,
            sz=CASE
                -- 终态的 sz（通常为 0 = 剩余尺寸）不能被 'open' 的 sz 覆盖
                WHEN orders.status IN {TERMINAL} AND excluded.status = 'open'
                THEN orders.sz
                ELSE excluded.sz
            END,
            orig_sz=excluded.orig_sz,
            timestamp=excluded.timestamp,
            status=CASE
                WHEN orders.status IN {TERMINAL} AND excluded.status = 'open'
                THEN orders.status
                WHEN excluded.status_timestamp >= orders.status_timestamp
                THEN excluded.status
                ELSE orders.status
            END,
            status_timestamp=CASE
                WHEN orders.status IN {TERMINAL} AND excluded.status = 'open'
                THEN orders.status_timestamp
                WHEN excluded.status_timestamp >= orders.status_timestamp
                THEN excluded.status_timestamp
                ELSE orders.status_timestamp
            END,
            order_type=excluded.order_type,
            tif=excluded.tif,
            reduce_only=excluded.reduce_only,
            is_trigger=excluded.is_trigger,
            trigger_px=excluded.trigger_px,
            trigger_condition=excluded.trigger_condition,
            is_position_tpsl=excluded.is_position_tpsl,
            cloid=excluded.cloid,
            last_seen_at=excluded.last_seen_at,
            last_source=excluded.last_source,
            raw_json=CASE
                -- raw_json 跟随胜出的 status：终态 vs open 时保留终态的 raw
                WHEN orders.status IN {TERMINAL} AND excluded.status = 'open'
                THEN orders.raw_json
                ELSE excluded.raw_json
            END
        """,
        (oid, coin, side, limit_px, sz, orig_sz, ts, status, status_ts,
         order_type, tif, reduce_only, is_trigger, trigger_px,
         trigger_condition, is_position_tpsl, cloid, now_ms, now_ms,
         source, raw),
    )


def upsert_fill(conn: sqlite3.Connection, fill: dict) -> None:
    tid = int(fill["tid"])
    conn.execute(
        """
        INSERT INTO fills(
            tid, oid, coin, side, px, sz, time, start_position, dir,
            closed_pnl, fee, fee_token, crossed, hash, twap_id, raw_json
        ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        ON CONFLICT(tid) DO UPDATE SET
            raw_json=excluded.raw_json,
            -- 其它字段在重复 tid 时理应不变；以最新观测覆盖以容错。
            oid=excluded.oid,
            px=excluded.px,
            sz=excluded.sz,
            closed_pnl=excluded.closed_pnl,
            fee=excluded.fee
        """,
        (
            tid,
            _to_int(fill.get("oid")),
            fill["coin"],
            fill["side"],
            _to_float(fill["px"]),
            _to_float(fill["sz"]),
            int(fill["time"]),
            _to_float(fill.get("startPosition")),
            fill.get("dir"),
            _to_float(fill.get("closedPnl")),
            _to_float(fill.get("fee")),
            fill.get("feeToken"),
            1 if fill.get("crossed") else 0,
            fill.get("hash"),
            fill.get("twapId"),
            json.dumps(fill, ensure_ascii=False),
        ),
    )


def insert_clearinghouse_snapshot(conn: sqlite3.Connection, fetched_at: int,
                                  payload: dict) -> int:
    ms = payload.get("marginSummary", {}) or {}
    cur = conn.execute(
        """
        INSERT INTO clearinghouse_snapshots(
            fetched_at, server_time, account_value, total_ntl_pos,
            total_raw_usd, total_margin_used, withdrawable, raw_json
        ) VALUES (?,?,?,?,?,?,?,?)
        """,
        (
            fetched_at,
            _to_int(payload.get("time")),
            _to_float(ms.get("accountValue")),
            _to_float(ms.get("totalNtlPos")),
            _to_float(ms.get("totalRawUsd")),
            _to_float(ms.get("totalMarginUsed")),
            _to_float(payload.get("withdrawable")),
            json.dumps(payload, ensure_ascii=False),
        ),
    )
    snap_id = cur.lastrowid
    for ap in payload.get("assetPositions", []):
        p = ap.get("position", {}) or {}
        lev = p.get("leverage", {}) or {}
        conn.execute(
            """
            INSERT INTO position_snapshots(
                snapshot_id, coin, szi, entry_px, position_value, unrealized_pnl,
                return_on_equity, leverage_type, leverage_value, liquidation_px,
                margin_used, raw_json
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                snap_id,
                p.get("coin"),
                _to_float(p.get("szi")),
                _to_float(p.get("entryPx")),
                _to_float(p.get("positionValue")),
                _to_float(p.get("unrealizedPnl")),
                _to_float(p.get("returnOnEquity")),
                lev.get("type"),
                _to_float(lev.get("value")),
                _to_float(p.get("liquidationPx")),
                _to_float(p.get("marginUsed")),
                json.dumps(p, ensure_ascii=False),
            ),
        )
    return snap_id


def insert_spot_snapshot(conn: sqlite3.Connection, fetched_at: int, payload: dict) -> int:
    cur = conn.execute(
        "INSERT INTO spot_clearinghouse_snapshots(fetched_at, raw_json) VALUES (?,?)",
        (fetched_at, json.dumps(payload, ensure_ascii=False)),
    )
    return cur.lastrowid


def insert_open_orders_snapshot(conn: sqlite3.Connection, fetched_at: int,
                                source: str, oids: list[int]) -> int:
    cur = conn.execute(
        "INSERT INTO open_orders_snapshots(fetched_at, source, oids_json) VALUES (?,?,?)",
        (fetched_at, source, json.dumps(sorted(oids))),
    )
    return cur.lastrowid


def upsert_portfolio_point(conn: sqlite3.Connection, period: str, ts: int,
                            account_value: float | None, pnl: float | None) -> None:
    conn.execute(
        """
        INSERT INTO portfolio_history(period, timestamp, account_value, pnl)
        VALUES (?,?,?,?)
        ON CONFLICT(period, timestamp) DO UPDATE SET
            account_value=excluded.account_value,
            pnl=excluded.pnl
        """,
        (period, ts, account_value, pnl),
    )


def upsert_candle(conn: sqlite3.Connection, interval: str, candle: dict) -> None:
    conn.execute(
        """
        INSERT INTO candles(interval, t, close_time, o, h, l, c, v, n)
        VALUES (?,?,?,?,?,?,?,?,?)
        ON CONFLICT(interval, t) DO UPDATE SET
            close_time=excluded.close_time, o=excluded.o, h=excluded.h, l=excluded.l,
            c=excluded.c, v=excluded.v, n=excluded.n
        """,
        (
            interval,
            int(candle["t"]),
            int(candle["T"]),
            _to_float(candle.get("o")),
            _to_float(candle.get("h")),
            _to_float(candle.get("l")),
            _to_float(candle.get("c")),
            _to_float(candle.get("v")),
            _to_int(candle.get("n")),
        ),
    )


def start_run(conn: sqlite3.Connection, now_ms: int) -> int:
    cur = conn.execute(
        "INSERT INTO update_runs(started_at) VALUES (?)", (now_ms,)
    )
    return cur.lastrowid


def finish_run(conn: sqlite3.Connection, run_id: int, finished_at: int,
               success: bool, notes: str) -> None:
    conn.execute(
        "UPDATE update_runs SET finished_at=?, success=?, notes=? WHERE id=?",
        (finished_at, 1 if success else 0, notes, run_id),
    )


# ---------- 读取辅助（给重建/导出/校验用） ----------

def get_latest_clearinghouse(conn: sqlite3.Connection) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT * FROM clearinghouse_snapshots ORDER BY fetched_at DESC LIMIT 1"
    ).fetchone()


def get_positions_for_snapshot(conn: sqlite3.Connection, snap_id: int) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM position_snapshots WHERE snapshot_id=?", (snap_id,)
    ).fetchall()


def iter_fills(conn: sqlite3.Connection, coin: str | None = None) -> Iterable[sqlite3.Row]:
    if coin:
        return conn.execute(
            "SELECT * FROM fills WHERE coin=? ORDER BY time ASC, tid ASC", (coin,)
        ).fetchall()
    return conn.execute(
        "SELECT * FROM fills ORDER BY time ASC, tid ASC"
    ).fetchall()


def all_orders(conn: sqlite3.Connection, coin: str | None = None) -> list[sqlite3.Row]:
    if coin:
        return conn.execute(
            "SELECT * FROM orders WHERE coin=? ORDER BY timestamp ASC", (coin,)
        ).fetchall()
    return conn.execute("SELECT * FROM orders ORDER BY timestamp ASC").fetchall()


def candles_for_interval(conn: sqlite3.Connection, interval: str,
                         since_ms: int | None = None) -> list[sqlite3.Row]:
    if since_ms is None:
        return conn.execute(
            "SELECT * FROM candles WHERE interval=? ORDER BY t ASC", (interval,)
        ).fetchall()
    return conn.execute(
        "SELECT * FROM candles WHERE interval=? AND t>=? ORDER BY t ASC",
        (interval, since_ms),
    ).fetchall()


def portfolio_series(conn: sqlite3.Connection, period: str = "allTime"
                     ) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM portfolio_history WHERE period=? ORDER BY timestamp ASC",
        (period,),
    ).fetchall()


def latest_candle_t(conn: sqlite3.Connection, interval: str) -> int | None:
    row = conn.execute(
        "SELECT MAX(t) AS m FROM candles WHERE interval=?", (interval,)
    ).fetchone()
    return row["m"] if row and row["m"] is not None else None


# ---------- 内部小工具 ----------

def _to_float(x: Any) -> float | None:
    if x is None or x == "":
        return None
    try:
        return float(x)
    except (TypeError, ValueError):
        return None


def _to_int(x: Any) -> int | None:
    if x is None or x == "":
        return None
    try:
        return int(x)
    except (TypeError, ValueError):
        return None
