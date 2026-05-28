"""把旧项目 (track_paulwei_hyperliquid) 的 weekly snapshot 导入本项目数据库。

旧项目按周保存：data/runs/<date>/current/ 下有 user_snapshot/ 和 account_state/，
分别对应 user 相关和账户相关的原始 API 响应。这些 snapshot 比本项目数据库更早，
能补回已经掉出 Hyperliquid 滚动窗口的早期挂单和成交。

用法：
    py -3.12 import_legacy.py --src <path>             # 指定旧项目 data/runs 目录
    py -3.12 import_legacy.py --reset-marks --src <path>   # 清掉"已导入"标记重跑

也可以通过环境变量 PAUL_LEGACY_SRC 指定，省得每次敲：
    PowerShell:  $env:PAUL_LEGACY_SRC = "<your-path>\\track_paulwei_hyperliquid\\data\\runs"

导入流程对每个 run 只跑一次（用 meta 表里的 marker 防重复）；底层 upsert 已经按
oid / tid 去重，重复跑也安全。导入完后建议跑：
    py -3.12 update.py --rebuild-orders --no-fetch
让 orders 表从全部 raw 历史重新整合（含新导入的早期数据）。
"""
from __future__ import annotations
import argparse
import json
import os
import sys
from datetime import datetime
from pathlib import Path

from src import db


# 默认从环境变量 PAUL_LEGACY_SRC 读；没设就 None，CLI 强制 --src
_env = os.environ.get("PAUL_LEGACY_SRC")
DEFAULT_SRC = Path(_env) if _env else None


def _iso_to_ms(iso: str) -> int:
    # Python 3.11+ fromisoformat 支持 +HH:MM。容错性处理 Z 后缀。
    if iso.endswith("Z"):
        iso = iso[:-1] + "+00:00"
    return int(datetime.fromisoformat(iso).timestamp() * 1000)


def _unwrap(obj):
    """旧项目 account_state/ 下的 JSON 包了一层 {request, downloaded_at_utc, response}。
    自动 unwrap 拿到真实 API response；不是 wrapper 就原样返回。"""
    if isinstance(obj, dict) and "response" in obj and "request" in obj:
        return obj["response"]
    return obj


def already_imported(conn, key: str) -> bool:
    row = conn.execute("SELECT 1 FROM meta WHERE key=?", (key,)).fetchone()
    return bool(row)


def mark_imported(conn, key: str) -> None:
    conn.execute(
        "INSERT OR REPLACE INTO meta(key, value) VALUES(?,?)", (key, "1")
    )


def import_run(conn, run_dir: Path) -> dict:
    us = run_dir / "current" / "user_snapshot"
    accs = run_dir / "current" / "account_state"
    if not us.exists():
        return {"skipped": "no user_snapshot"}

    meta = json.loads((us / "run_meta.json").read_text(encoding="utf-8"))
    user = meta.get("user")
    fetched_iso = meta.get("downloaded_at_utc")
    fetched_at = _iso_to_ms(fetched_iso)
    start_ms = int(meta.get("start_time_ms") or 0)

    key = f"imported_legacy:{run_dir.name}:{fetched_at}"
    if already_imported(conn, key):
        return {"skipped": "already imported", "key": key}

    counts = {"run_dir": run_dir.name, "fetched_at": fetched_iso, "user": user}

    # historicalOrders
    fp = us / "historical_orders.json"
    if fp.exists():
        payload = json.loads(fp.read_text(encoding="utf-8"))
        db.insert_raw(conn, fetched_at, "historicalOrders",
                      {"type": "historicalOrders", "user": user}, payload)
        n_seen = 0
        if isinstance(payload, list):
            for item in payload:
                o = item.get("order")
                status = item.get("status")
                if not o or not status:
                    continue
                status_ts = int(item.get("statusTimestamp",
                                          o.get("timestamp", fetched_at)))
                db.upsert_order(conn, o, status=status, status_ts=status_ts,
                                source="legacy_historicalOrders", now_ms=fetched_at)
                n_seen += 1
        counts["historicalOrders"] = n_seen

    # frontendOpenOrders（同时建立"当时挂单 oid 列表"快照）
    fp = us / "frontend_open_orders.json"
    if fp.exists():
        payload = json.loads(fp.read_text(encoding="utf-8"))
        db.insert_raw(conn, fetched_at, "frontendOpenOrders",
                      {"type": "frontendOpenOrders", "user": user}, payload)
        oids = []
        if isinstance(payload, list):
            for o in payload:
                db.upsert_order(conn, o, status="open",
                                status_ts=int(o.get("timestamp", fetched_at)),
                                source="legacy_frontendOpenOrders",
                                now_ms=fetched_at)
                oids.append(int(o["oid"]))
        db.insert_open_orders_snapshot(conn, fetched_at, "frontendOpenOrders", oids)
        counts["frontendOpenOrders"] = len(oids)

    # openOrders（仅留底）
    fp = us / "open_orders.json"
    if fp.exists():
        payload = json.loads(fp.read_text(encoding="utf-8"))
        db.insert_raw(conn, fetched_at, "openOrders",
                      {"type": "openOrders", "user": user}, payload)

    # userFills (recent 2000)
    fp = us / "recent_fills.json"
    if fp.exists():
        payload = json.loads(fp.read_text(encoding="utf-8"))
        db.insert_raw(conn, fetched_at, "userFills",
                      {"type": "userFills", "user": user}, payload)
        n = 0
        if isinstance(payload, list):
            for f in payload:
                db.upsert_fill(conn, f)
                n += 1
        counts["recent_fills"] = n

    # userFillsByTime（按 start_ms → fetched_at 的完整时间段；已经合并所有 page）
    fp = us / "fills_by_time.json"
    if fp.exists():
        payload = json.loads(fp.read_text(encoding="utf-8"))
        db.insert_raw(conn, fetched_at, "userFillsByTime",
                      {"type": "userFillsByTime", "user": user,
                       "startTime": start_ms, "endTime": fetched_at},
                      payload)
        n = 0
        if isinstance(payload, list):
            for f in payload:
                db.upsert_fill(conn, f)
                n += 1
        counts["fills_by_time"] = n

    # clearinghouseState（账户快照 + 仓位快照） —— account_state/ 下文件需要 unwrap
    fp = accs / "clearinghouse_state.json"
    if fp.exists():
        payload = _unwrap(json.loads(fp.read_text(encoding="utf-8")))
        db.insert_raw(conn, fetched_at, "clearinghouseState",
                      {"type": "clearinghouseState", "user": user}, payload)
        db.insert_clearinghouse_snapshot(conn, fetched_at, payload)
        counts["clearinghouseState"] = 1

    # spotClearinghouseState（留底）
    fp = accs / "spot_clearinghouse_state.json"
    if fp.exists():
        payload = _unwrap(json.loads(fp.read_text(encoding="utf-8")))
        db.insert_raw(conn, fetched_at, "spotClearinghouseState",
                      {"type": "spotClearinghouseState", "user": user}, payload)
        db.insert_spot_snapshot(conn, fetched_at, payload)

    # portfolio（账户价值历史时间序列）
    fp = accs / "portfolio.json"
    if fp.exists():
        payload = _unwrap(json.loads(fp.read_text(encoding="utf-8")))
        db.insert_raw(conn, fetched_at, "portfolio",
                      {"type": "portfolio", "user": user}, payload)
        n_pts = 0
        if isinstance(payload, list):
            for entry in payload:
                if not (isinstance(entry, list) and len(entry) == 2):
                    continue
                period, data = entry
                if not isinstance(data, dict):
                    continue
                avh = data.get("accountValueHistory") or []
                pnlh = data.get("pnlHistory") or []
                pnl_map = {int(p[0]): _to_f(p[1]) for p in pnlh
                           if isinstance(p, list) and len(p) >= 2}
                for p in avh:
                    if not (isinstance(p, list) and len(p) >= 2):
                        continue
                    ts = int(p[0])
                    av = _to_f(p[1])
                    db.upsert_portfolio_point(conn, period, ts, av,
                                              pnl_map.get(ts))
                    n_pts += 1
        counts["portfolio_points"] = n_pts

    mark_imported(conn, key)
    conn.commit()
    return counts


def _to_f(x):
    try:
        return float(x)
    except (TypeError, ValueError):
        return None


def main() -> int:
    parser = argparse.ArgumentParser(description="导入旧项目的 weekly snapshot")
    default_help = f"默认从环境变量 PAUL_LEGACY_SRC（当前 {DEFAULT_SRC}）"
    parser.add_argument("--src", default=str(DEFAULT_SRC) if DEFAULT_SRC else None,
                        help=f"旧项目的 data/runs 目录；{default_help}")
    parser.add_argument("--reset-marks", action="store_true",
                        help="先清掉所有 'imported_legacy:' 标记后再导入")
    args = parser.parse_args()

    if not args.src or args.src == "None":
        print("[import] 没指定来源目录。用 --src <path> 或设置环境变量 "
              "PAUL_LEGACY_SRC=<your-path>", file=sys.stderr)
        return 2
    src = Path(args.src)
    if not src.exists():
        print(f"[import] 来源目录不存在: {src}", file=sys.stderr)
        return 2

    conn = db.connect()
    db.init_db(conn)

    if args.reset_marks:
        conn.execute("DELETE FROM meta WHERE key LIKE 'imported_legacy:%'")
        # 清掉之前因为没 unwrap 而写入的坏 clearinghouse / spot 快照
        bad_chs = conn.execute(
            "DELETE FROM clearinghouse_snapshots WHERE account_value IS NULL"
        ).rowcount
        conn.execute(
            "DELETE FROM position_snapshots WHERE snapshot_id NOT IN "
            "(SELECT id FROM clearinghouse_snapshots)"
        )
        bad_spot = conn.execute(
            "DELETE FROM spot_clearinghouse_snapshots WHERE raw_json LIKE '%\"request\":%'"
        ).rowcount
        conn.commit()
        print(f"[import] 已清除 marker + {bad_chs} 个坏 clearinghouse 快照 "
              f"+ {bad_spot} 个坏 spot 快照")

    # 找到所有 run 目录（按文件名排序，跳过非日期目录）
    run_dirs = []
    for child in sorted(src.iterdir()):
        if not child.is_dir():
            continue
        # 形如 2026-04-29
        if len(child.name) == 10 and child.name[4] == "-" and child.name[7] == "-":
            run_dirs.append(child)

    print(f"[import] 来源 {src}")
    print(f"[import] 找到 {len(run_dirs)} 个 weekly run: {[d.name for d in run_dirs]}")
    total = {}
    for d in run_dirs:
        try:
            r = import_run(conn, d)
            print(f"[import] {d.name}: {r}")
            for k, v in r.items():
                if isinstance(v, int):
                    total[k] = total.get(k, 0) + v
        except Exception as e:
            print(f"[import] {d.name} 失败: {e}", file=sys.stderr)
    print(f"[import] 累计: {total}")
    print()
    print("下一步建议：重建 orders 表 + 重新导出 JSON：")
    print("    py -3.12 update.py --rebuild-orders --no-fetch")
    return 0


if __name__ == "__main__":
    sys.exit(main())
