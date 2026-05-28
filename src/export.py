"""把数据库内容预计算成给前端用的 JSON 文件。

文件清单：
- export/summary.json
    总体摘要：当前账户价值、当前真实仓位、最近一次更新时间、重建与真实对比。
- export/orders.json
    所有订单的精简列表（含状态、时间区间），前端用它做"某时刻活跃订单"的过滤。
- export/fills.json
    所有 fills，前端用它做 K 线标记。
- export/timeline_<interval>.json
    针对每种 K 线粒度（1d/4h/1h）的"逐 K 线状态序列"。
    每根 K 线带上：开收价、当时仓位、当时账户价值、当时实际杠杆。
    前端用它做主图 + 杠杆副图。
- export/portfolio_alltime.json
    Hyperliquid 原生账户价值历史（用来画底图账户价值参考线，标注来源）。

设计要点：
- 所有时间均为 ms 时间戳。
- 杠杆字段含义：仓位名义价值 / 账户总价值；正负代表多空方向。
"""
from __future__ import annotations
import json
from pathlib import Path

from . import config, db
from . import reconstruct as R


def _dump(path: Path, obj) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(obj, ensure_ascii=False, separators=(",", ":")),
                    encoding="utf-8")


def export_summary(conn) -> dict:
    cmp = R.compare_reconstruction(conn, coin="BTC")
    snap = db.get_latest_clearinghouse(conn)
    fe = conn.execute(
        "SELECT * FROM open_orders_snapshots WHERE source='frontendOpenOrders' "
        "ORDER BY fetched_at DESC LIMIT 1"
    ).fetchone()
    last_open_orders = json.loads(fe["oids_json"]) if fe else []
    last_run = conn.execute(
        "SELECT * FROM update_runs ORDER BY started_at DESC LIMIT 1"
    ).fetchone()

    # 当前真实仓位的名义价值和杠杆
    truth = cmp["truth"] or {}
    pos_value = truth.get("position_value")
    av = truth.get("account_value")
    if av and pos_value is not None and truth.get("szi") is not None:
        # 杠杆带符号（多头正，空头负）
        sign = 1.0 if truth["szi"] >= 0 else -1.0
        signed_lev = sign * (pos_value / av) if av else None
    else:
        signed_lev = None

    summary = {
        "target_user": config.TARGET_USER,
        "hyperbot_url": f"https://hyperbot.network/trader/{config.TARGET_USER}",
        "latest_update": {
            "started_at": last_run["started_at"] if last_run else None,
            "finished_at": last_run["finished_at"] if last_run else None,
            "success": bool(last_run["success"]) if last_run and last_run["success"] is not None else None,
            "notes": last_run["notes"] if last_run else None,
        },
        "clearinghouse": {
            "snapshot_id": snap["id"] if snap else None,
            "fetched_at": snap["fetched_at"] if snap else None,
            "account_value": snap["account_value"] if snap else None,
            "total_ntl_pos": snap["total_ntl_pos"] if snap else None,
            "withdrawable": snap["withdrawable"] if snap else None,
            "btc_position": truth,
            "btc_leverage_signed": signed_lev,
        },
        "reconstruction": {
            "btc_size": cmp["reconstructed"],
            "matches_clearinghouse": cmp["ok"],
            "diff_vs_clearinghouse": cmp["diff"],
        },
        "active_open_orders": last_open_orders,
        "active_open_orders_count": len(last_open_orders),
    }
    _dump(config.EXPORT_DIR / "summary.json", summary)
    return summary


def export_orders(conn) -> int:
    rows = db.all_orders(conn, coin="BTC")
    out = []
    for r in rows:
        out.append({
            "oid": r["oid"],
            "side": r["side"],
            "limit_px": r["limit_px"],
            "sz": r["sz"],
            "orig_sz": r["orig_sz"],
            "timestamp": r["timestamp"],
            "status": r["status"],
            "status_timestamp": r["status_timestamp"],
            "order_type": r["order_type"],
            "tif": r["tif"],
            "reduce_only": bool(r["reduce_only"]) if r["reduce_only"] is not None else False,
            "is_trigger": bool(r["is_trigger"]) if r["is_trigger"] is not None else False,
            "trigger_px": r["trigger_px"],
            "first_seen_at": r["first_seen_at"],
            "last_seen_at": r["last_seen_at"],
            # 是否能精确观测到状态变化时间：如果状态是 open 且 last_seen 不是最近这次更新，
            # 就标注 "推断"；前端可据此提醒。
        })
    _dump(config.EXPORT_DIR / "orders.json", out)
    return len(out)


def export_fills(conn) -> int:
    fills = R.load_fills(conn, coin="BTC")
    steps = R.build_position_steps(fills)
    # 把仓位累积值附加到每条 fill
    out = []
    for s in steps:
        f = s.fill
        out.append({
            "tid": f.tid,
            "oid": f.oid,
            "time": f.time,
            "side": f.side,
            "px": f.px,
            "sz": f.sz,
            "start_position": f.start_position,
            "closed_pnl": f.closed_pnl,
            "crossed": f.crossed,
            "dir": f.dir,
            "position_after": s.size,
        })
    _dump(config.EXPORT_DIR / "fills.json", out)
    return len(out)


def export_portfolio(conn) -> int:
    rows = db.portfolio_series(conn, period="allTime")
    out = [{"t": r["timestamp"], "av": r["account_value"], "pnl": r["pnl"]} for r in rows]
    _dump(config.EXPORT_DIR / "portfolio_alltime.json", out)
    return len(out)


def export_timeline(conn, interval: str) -> int:
    candles = db.candles_for_interval(conn, interval)
    if not candles:
        _dump(config.EXPORT_DIR / f"timeline_{interval}.json",
              {"interval": interval, "bars": []})
        return 0
    fills = R.load_fills(conn, coin="BTC")
    steps = R.build_position_steps(fills)
    av_series = R.load_account_value_series(conn)

    # 预备：找出每根 K 线时间区间内"刚发生的 fills"，方便前端做 marker
    # K 线区间用 [t, close_time+1) 这种半开区间
    fills_by_bar: dict[int, list[dict]] = {}
    bar_ts = [c["t"] for c in candles]
    bar_T = {c["t"]: c["close_time"] for c in candles}
    if fills:
        for f in fills:
            # 找该 fill 落在哪根 K 线
            import bisect
            i = bisect.bisect_right(bar_ts, f.time) - 1
            if i < 0:
                continue
            t0 = bar_ts[i]
            if f.time > bar_T[t0]:
                # 落在 K 线之外（理论不会发生），跳过
                continue
            fills_by_bar.setdefault(t0, []).append({
                "tid": f.tid,
                "time": f.time,
                "side": f.side,
                "px": f.px,
                "sz": f.sz,
                "crossed": f.crossed,
                "dir": f.dir,
                "closed_pnl": f.closed_pnl,
            })

    bars = []
    for c in candles:
        t = c["t"]
        T = c["close_time"]
        # 用 K 线收盘时间 T 来评估当时的状态（K 线已经走完）
        eval_ts = T
        pos = R.position_size_at(steps, eval_ts)
        av = R.account_value_at(av_series, eval_ts)
        close = c["c"]
        notional = pos * close if (pos is not None and close is not None) else None
        if av and notional is not None and av != 0:
            lev = notional / av
        else:
            lev = None
        bars.append({
            "t": t,
            "T": T,
            "o": c["o"], "h": c["h"], "l": c["l"], "c": c["c"],
            "v": c["v"], "n": c["n"],
            "pos": pos,                  # 累计带符号 BTC 持仓
            "notional": notional,        # 名义价值 = pos * close
            "av": av,                    # 账户价值
            "lev": lev,                  # 实际杠杆，带符号
            "fills": fills_by_bar.get(t, []),
        })
    _dump(config.EXPORT_DIR / f"timeline_{interval}.json", {
        "interval": interval,
        "source": "Hyperliquid candleSnapshot (成交量为 Hyperliquid 自家成交量)",
        "bars": bars,
    })
    return len(bars)


def run_all(conn) -> dict:
    out = {}
    out["summary"] = export_summary(conn)
    out["orders"] = export_orders(conn)
    out["fills"] = export_fills(conn)
    out["portfolio"] = export_portfolio(conn)
    for iv in config.CANDLE_INTERVALS:
        out[f"timeline_{iv}"] = export_timeline(conn, iv)
    return out
