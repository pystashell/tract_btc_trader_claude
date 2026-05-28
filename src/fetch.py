"""一次完整的抓取流程。

设计目标：
1. 把每个原始响应都落到 raw_responses 表，同时也存成 data/raw/*.json 文件，
   双层留底防止数据库损坏后无法回溯。
2. 历史订单 / 成交按主键去重；多次运行不会产生重复记录。
3. 对挂单和当前账户状态，每次跑都生成新快照，方便回放。
4. K 线增量抓取：只补足上次最后一根到现在的窗口。
5. userFills 端点会被截断，所以也用 userFillsByTime 做长期回补。
"""
from __future__ import annotations
import json
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from . import config, db
from .api import HLClient


def now_ms() -> int:
    return int(time.time() * 1000)


def _save_raw_file(payload: Any, endpoint: str, fetched_at: int) -> Path:
    """同时落地一份原始 JSON 文件做双重保险。"""
    config.RAW_DIR.mkdir(parents=True, exist_ok=True)
    ts = datetime.fromtimestamp(fetched_at / 1000, tz=timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    fname = f"{ts}_{endpoint}.json"
    path = config.RAW_DIR / fname
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return path


def _record(conn, endpoint: str, body: dict, response: Any, fetched_at: int) -> None:
    db.insert_raw(conn, fetched_at, endpoint, body, response)
    _save_raw_file(response, endpoint, fetched_at)


# -------------------------- 各部分的抓取 --------------------------

def fetch_clearinghouse(conn, client: HLClient, user: str) -> dict:
    fetched_at = now_ms()
    payload = client.clearinghouse_state(user)
    _record(conn, "clearinghouseState", {"type": "clearinghouseState", "user": user},
            payload, fetched_at)
    db.insert_clearinghouse_snapshot(conn, fetched_at, payload)
    conn.commit()
    return payload


def fetch_spot_clearinghouse(conn, client: HLClient, user: str) -> dict:
    fetched_at = now_ms()
    payload = client.spot_clearinghouse_state(user)
    _record(conn, "spotClearinghouseState",
            {"type": "spotClearinghouseState", "user": user}, payload, fetched_at)
    db.insert_spot_snapshot(conn, fetched_at, payload)
    conn.commit()
    return payload


def fetch_open_orders(conn, client: HLClient, user: str) -> tuple[list, list]:
    """前端 / 普通两份都拉，前端版有更多字段。"""
    fetched_at = now_ms()
    fe = client.frontend_open_orders(user)
    _record(conn, "frontendOpenOrders",
            {"type": "frontendOpenOrders", "user": user}, fe, fetched_at)
    oids: list[int] = []
    for o in fe:
        # frontendOpenOrders 返回的是完整 order 对象（无 wrapper）
        db.upsert_order(
            conn, o,
            status="open",
            status_ts=int(o.get("timestamp", fetched_at)),
            source="frontendOpenOrders",
            now_ms=fetched_at,
        )
        oids.append(int(o["oid"]))
    db.insert_open_orders_snapshot(conn, fetched_at, "frontendOpenOrders", oids)

    fetched_at2 = now_ms()
    oo = client.open_orders(user)
    _record(conn, "openOrders", {"type": "openOrders", "user": user}, oo, fetched_at2)
    conn.commit()
    return fe, oo


def backfill_orders_from_fills(conn) -> dict:
    """从 fills 表反推"已知存在但 orders 表缺失"的订单。

    场景：某些 Paul 早期下的单已经从 `historicalOrders` 滚动窗口里掉出去，
    我们无论从 API 还是 weekly snapshot 都拿不到完整 order record。
    但只要这些订单成交过哪怕一次，fills 表里就保留了 oid 和成交信息。
    我们用 fills 反推出最小订单 record（status=filled, sz=summed fills, ...），
    标 source='backfill_from_fills' 表示这是反推的不完整数据。

    限制（必须诚实说明）：
    - timestamp 用最早一笔 fill 的 time，而非订单实际下单时间（限价单可能挂了很久）
    - status_timestamp 用最后一笔 fill 的 time
    - limit_px 取最早一笔 fill 的 px（maker fill 等于 limit 价；taker fill 是市价，
      会与真实 limit 价有差异，但至少是一个有意义的价格点）
    - 假设订单已 filled。被部分成交后撤销 / 修改的订单看不出来
    """
    fill_oids = set(r["oid"] for r in conn.execute(
        "SELECT DISTINCT oid FROM fills WHERE oid IS NOT NULL"))
    order_oids = set(r["oid"] for r in conn.execute("SELECT oid FROM orders"))
    missing = fill_oids - order_oids
    inserted = 0
    for oid in missing:
        rows = list(conn.execute(
            "SELECT * FROM fills WHERE oid=? ORDER BY time ASC", (oid,)
        ))
        if not rows:
            continue
        first = rows[0]
        last = rows[-1]
        total_sz = sum(r["sz"] for r in rows)
        synthetic = {
            "oid": oid,
            "coin": first["coin"],
            "side": first["side"],
            "limitPx": str(first["px"]),         # 最佳近似：第一笔成交价
            "sz": "0.0",                         # filled → 剩余为 0
            "origSz": str(total_sz),             # 所有 fill 的累计 sz
            "timestamp": first["time"],          # 反推：以首次成交时间为创建时间
            "triggerCondition": "N/A",
            "isTrigger": False,
            "triggerPx": "0.0",
            "children": [],
            "isPositionTpsl": False,
            "reduceOnly": False,
            "orderType": "Limit",
            "tif": None,
            "cloid": None,
        }
        db.upsert_order(conn, synthetic, status="filled",
                        status_ts=last["time"],
                        source="backfill_from_fills",
                        now_ms=last["time"])
        inserted += 1
    conn.commit()
    return {"missing_oids": len(missing), "inserted": inserted}


def rebuild_orders_from_raw(conn) -> dict:
    """从 raw_responses 表里所有 historicalOrders / frontendOpenOrders 重新生成 orders 表。

    用途：当 upsert 逻辑修了 bug、或者怀疑 orders 表数据错乱时调用。
    会清空 orders 表然后重放所有 raw 数据，按 fetched_at 升序放入；同一 oid 的
    多条记录会被 upsert 合并，终态会赢过 open（参见 db.upsert_order 的注释）。
    """
    counts = {"historicalOrders_rows": 0, "frontendOpenOrders_rows": 0,
              "orders_before": 0, "orders_after": 0}
    counts["orders_before"] = conn.execute("SELECT COUNT(*) FROM orders").fetchone()[0]
    conn.execute("DELETE FROM orders")
    rows = conn.execute(
        "SELECT id, fetched_at, endpoint, response_json FROM raw_responses "
        "WHERE endpoint IN ('historicalOrders','frontendOpenOrders') "
        "ORDER BY fetched_at ASC, id ASC"
    ).fetchall()
    for r in rows:
        fetched_at = r["fetched_at"]
        payload = json.loads(r["response_json"])
        if r["endpoint"] == "historicalOrders":
            if not isinstance(payload, list):
                continue
            for item in payload:
                o = item.get("order")
                status = item.get("status")
                if not o or not status:
                    continue
                status_ts = int(item.get("statusTimestamp", o.get("timestamp", fetched_at)))
                db.upsert_order(conn, o, status=status, status_ts=status_ts,
                                source="historicalOrders", now_ms=fetched_at)
                counts["historicalOrders_rows"] += 1
        else:  # frontendOpenOrders
            if not isinstance(payload, list):
                continue
            for o in payload:
                db.upsert_order(conn, o, status="open",
                                status_ts=int(o.get("timestamp", fetched_at)),
                                source="frontendOpenOrders", now_ms=fetched_at)
                counts["frontendOpenOrders_rows"] += 1
    conn.commit()
    counts["orders_after"] = conn.execute("SELECT COUNT(*) FROM orders").fetchone()[0]
    return counts


def fetch_historical_orders(conn, client: HLClient, user: str) -> list:
    fetched_at = now_ms()
    payload = client.historical_orders(user)
    _record(conn, "historicalOrders",
            {"type": "historicalOrders", "user": user}, payload, fetched_at)
    if isinstance(payload, list):
        for item in payload:
            o = item.get("order")
            status = item.get("status")
            status_ts = int(item.get("statusTimestamp", o.get("timestamp", fetched_at)))
            if not o or not status:
                continue
            db.upsert_order(conn, o, status=status, status_ts=status_ts,
                            source="historicalOrders", now_ms=fetched_at)
    conn.commit()
    return payload if isinstance(payload, list) else []


def fetch_user_fills(conn, client: HLClient, user: str) -> list:
    """先拉最近 userFills（最多 2000 条），再用 userFillsByTime 回填到我们已知最早的成交。

    userFills 是滚动窗口，所以一定要把抓到的存好；以后老数据可能掉出窗口。
    """
    # 1) 最近窗口
    fetched_at = now_ms()
    recent = client.user_fills(user)
    _record(conn, "userFills", {"type": "userFills", "user": user}, recent, fetched_at)
    if isinstance(recent, list):
        for f in recent:
            db.upsert_fill(conn, f)
    conn.commit()

    # 2) 增量回填：从 1970 一路到现在，但只插入新 tid（依赖 ON CONFLICT 自动去重）
    #    我们做分段拉取以避免单次拉太多被截断。Hyperliquid 的 userFillsByTime
    #    单次返回上限约 2000 条；分段 30 天一段比较稳。
    SEG = 30 * 24 * 3600 * 1000
    # 起点：取已有最早 fill 之前若干天；如果没有数据，从一年前开始向前扫
    row = conn.execute("SELECT MIN(time) AS m FROM fills").fetchone()
    earliest = row["m"] if row and row["m"] else None
    end = now_ms()
    if earliest is None:
        start = end - 365 * 24 * 3600 * 1000
    else:
        # 已经有数据，从最早成交之前留 14 天缓冲再向前扫
        start = max(0, earliest - 14 * 24 * 3600 * 1000)
        # 同时也要补足 earliest 到 now 之间任何漏掉的：
        # userFills 已经覆盖了最近 2000 条；如果 2000 条不足以覆盖到 earliest，
        # 则下面循环会顺便补上中间窗口。
    cur_start = start
    seg_count = 0
    while cur_start < end:
        cur_end = min(cur_start + SEG, end)
        fetched_at = now_ms()
        try:
            seg = client.user_fills_by_time(user, cur_start, cur_end)
        except Exception as e:
            print(f"[fetch] userFillsByTime {cur_start}-{cur_end} 失败: {e}")
            break
        _record(conn, "userFillsByTime",
                {"type": "userFillsByTime", "user": user,
                 "startTime": cur_start, "endTime": cur_end},
                seg, fetched_at)
        if isinstance(seg, list):
            for f in seg:
                db.upsert_fill(conn, f)
        conn.commit()
        seg_count += 1
        # 如果分段返回为空且我们已经早于已知最早成交太多，停。
        if isinstance(seg, list) and not seg and earliest and cur_end < earliest - 60 * 24 * 3600 * 1000:
            break
        cur_start = cur_end
    return recent if isinstance(recent, list) else []


def fetch_portfolio(conn, client: HLClient, user: str) -> list:
    fetched_at = now_ms()
    payload = client.portfolio(user)
    _record(conn, "portfolio", {"type": "portfolio", "user": user}, payload, fetched_at)
    if isinstance(payload, list):
        for entry in payload:
            if not (isinstance(entry, list) and len(entry) == 2):
                continue
            period, data = entry
            if not isinstance(data, dict):
                continue
            avh = data.get("accountValueHistory") or []
            pnlh = data.get("pnlHistory") or []
            # 把两条平行序列合并；按 timestamp 对齐
            pnl_map = {int(p[0]): _safe_float(p[1]) for p in pnlh if isinstance(p, list)}
            for p in avh:
                if not (isinstance(p, list) and len(p) >= 2):
                    continue
                ts = int(p[0])
                av = _safe_float(p[1])
                db.upsert_portfolio_point(conn, period, ts, av, pnl_map.get(ts))
    conn.commit()
    return payload if isinstance(payload, list) else []


def fetch_candles(conn, client: HLClient, intervals=config.CANDLE_INTERVALS,
                  coin: str = "BTC") -> dict:
    """增量抓 K 线。每次只补足上次最后一根到现在。"""
    results: dict[str, int] = {}
    end = now_ms()
    for interval in intervals:
        # 起点：已有最后一根的 t（向前留一根重叠避免边界），否则从配置起点
        latest_t = db.latest_candle_t(conn, interval)
        if latest_t is None:
            start = config.CANDLE_START_MS
        else:
            start = latest_t  # 重叠最后一根，让 upsert 覆盖未收盘的那一根
        # Hyperliquid 单次返回 5000 根。分段抓。
        bar_ms = _interval_ms(interval)
        chunk_span = bar_ms * 4500
        cur_start = start
        inserted = 0
        while cur_start < end:
            cur_end = min(cur_start + chunk_span, end)
            fetched_at = now_ms()
            payload = client.candle_snapshot(coin, interval, cur_start, cur_end)
            _record(conn, "candleSnapshot",
                    {"type": "candleSnapshot",
                     "req": {"coin": coin, "interval": interval,
                             "startTime": cur_start, "endTime": cur_end}},
                    payload, fetched_at)
            if isinstance(payload, list):
                for c in payload:
                    db.upsert_candle(conn, interval, c)
                    inserted += 1
            conn.commit()
            if not payload:
                break
            cur_start = cur_end
        results[interval] = inserted
    return results


# -------------------------- 全流程入口 --------------------------

def run_full_fetch(conn, client: HLClient | None = None, user: str = config.TARGET_USER
                   ) -> dict:
    if client is None:
        client = HLClient()
    started = now_ms()
    run_id = db.start_run(conn, started)
    summary = {"started_at": started}
    notes: list[str] = []
    try:
        ch = fetch_clearinghouse(conn, client, user)
        ms_ = ch.get("marginSummary", {}) or {}
        summary["account_value"] = ms_.get("accountValue")
        notes.append(f"clearinghouse OK account_value={ms_.get('accountValue')}")

        fetch_spot_clearinghouse(conn, client, user)
        notes.append("spotClearinghouse OK")

        fe, oo = fetch_open_orders(conn, client, user)
        summary["open_orders"] = len(fe)
        notes.append(f"open_orders={len(fe)}")

        ho = fetch_historical_orders(conn, client, user)
        summary["historical_orders"] = len(ho)
        notes.append(f"historicalOrders={len(ho)}")

        uf = fetch_user_fills(conn, client, user)
        summary["recent_fills"] = len(uf)
        notes.append(f"recent_fills={len(uf)}")

        po = fetch_portfolio(conn, client, user)
        summary["portfolio_periods"] = len(po)
        notes.append(f"portfolio_periods={len(po)}")

        c_summary = fetch_candles(conn, client)
        summary["candles_inserted"] = c_summary
        notes.append(f"candles_inserted={c_summary}")

        db.finish_run(conn, run_id, now_ms(), True, " | ".join(notes))
        summary["success"] = True
    except Exception as e:
        notes.append(f"FAILED: {e}")
        db.finish_run(conn, run_id, now_ms(), False, " | ".join(notes))
        summary["success"] = False
        summary["error"] = str(e)
        conn.commit()
        raise
    conn.commit()
    return summary


# -------------------------- 工具 --------------------------

def _interval_ms(interval: str) -> int:
    if interval.endswith("m"):
        return int(interval[:-1]) * 60_000
    if interval.endswith("h"):
        return int(interval[:-1]) * 3600_000
    if interval.endswith("d"):
        return int(interval[:-1]) * 86_400_000
    raise ValueError(f"未知 interval: {interval}")


def _safe_float(x: Any) -> float | None:
    try:
        return float(x)
    except (TypeError, ValueError):
        return None
