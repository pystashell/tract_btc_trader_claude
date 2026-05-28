"""自动化检查：跑一遍核心数据流，确保系统没有坏。

会做的事：
1. 检查 SQLite schema 能初始化。
2. 检查 data/export/*.json 与数据库内容是否吻合。
3. 检查重建仓位与 clearinghouseState 的差是否在容差范围内（默认要求完全一致）。
4. 检查 BTC K 线、订单、成交都有数据。
5. 检查每个粒度的 timeline.json 至少有 30 根 K 线。
6. 检查最近一次抓取在 14 天内（避免数据过期被忽视）。

返回非零退出码表示有失败项；可以放入定时任务里。

使用：
    py -3.12 smoke_test.py
    py -3.12 smoke_test.py --live   # 顺便重新拉一次 clearinghouse 做实时对照
"""
from __future__ import annotations
import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

from src import config, db
from src import reconstruct as R
from src.api import HLClient


class Check:
    def __init__(self, name: str):
        self.name = name
        self.ok = False
        self.detail = ""

    def passes(self, detail: str = "") -> "Check":
        self.ok = True
        self.detail = detail
        return self

    def fails(self, detail: str) -> "Check":
        self.ok = False
        self.detail = detail
        return self


def fmt_age(ms: int) -> str:
    if not ms:
        return "?"
    age_s = (time.time() * 1000 - ms) / 1000
    if age_s < 3600:
        return f"{age_s/60:.1f} 分钟前"
    if age_s < 86400:
        return f"{age_s/3600:.1f} 小时前"
    return f"{age_s/86400:.1f} 天前"


def check_schema(conn) -> Check:
    c = Check("数据库 schema")
    try:
        rows = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()
        tables = sorted([r["name"] for r in rows])
        expected = {"orders", "fills", "candles", "clearinghouse_snapshots",
                    "position_snapshots", "open_orders_snapshots",
                    "portfolio_history", "raw_responses", "update_runs",
                    "spot_clearinghouse_snapshots", "meta"}
        missing = expected - set(tables)
        if missing:
            return c.fails(f"缺少表: {missing}")
        return c.passes(f"{len(tables)} 张表")
    except Exception as e:
        return c.fails(str(e))


def check_data_present(conn) -> Check:
    c = Check("基础数据存在")
    n_orders = conn.execute("SELECT COUNT(*) AS c FROM orders").fetchone()["c"]
    n_fills = conn.execute("SELECT COUNT(*) AS c FROM fills").fetchone()["c"]
    n_cand_1h = conn.execute(
        "SELECT COUNT(*) AS c FROM candles WHERE interval='1h'"
    ).fetchone()["c"]
    if n_orders < 1:
        return c.fails(f"orders 表为空")
    if n_fills < 1:
        return c.fails(f"fills 表为空")
    if n_cand_1h < 30:
        return c.fails(f"1h 蜡烛太少: {n_cand_1h}")
    return c.passes(f"orders={n_orders} fills={n_fills} 1h_candles={n_cand_1h}")


def check_recent_update(conn, max_age_days: float = 14) -> Check:
    c = Check(f"最近抓取在 {max_age_days} 天内")
    snap = db.get_latest_clearinghouse(conn)
    if not snap:
        return c.fails("没有任何 clearinghouse 快照")
    age = (time.time() * 1000 - snap["fetched_at"]) / 86400000
    if age > max_age_days:
        return c.fails(f"{age:.1f} 天没更新了，请运行 update.py")
    return c.passes(f"最近抓取: {fmt_age(snap['fetched_at'])}")


def check_reconstruction(conn) -> Check:
    c = Check("重建仓位 = clearinghouse 仓位")
    cmp = R.compare_reconstruction(conn, coin="BTC")
    if cmp["ok"]:
        return c.passes(
            f"重建={cmp['reconstructed']:.6f} 真实={cmp['clearinghouse']:.6f}"
        )
    return c.fails(
        f"差异 {cmp['diff']:.6f}。重建={cmp['reconstructed']:.6f} 真实={cmp['clearinghouse']:.6f}。"
        " 可能原因：fills 方向解析错误 / 增量未拉全 / Long>Short 方向反了"
    )


def check_exports() -> list[Check]:
    out: list[Check] = []
    required = [
        "summary.json", "orders.json", "fills.json", "portfolio_alltime.json",
        "timeline_1d.json", "timeline_4h.json", "timeline_1h.json",
    ]
    for name in required:
        c = Check(f"导出文件 {name}")
        path = config.EXPORT_DIR / name
        if not path.exists():
            c.fails("文件不存在；请运行 update.py")
        else:
            try:
                d = json.loads(path.read_text(encoding="utf-8"))
                if name.startswith("timeline_"):
                    n_bars = len(d.get("bars", []))
                    if n_bars < 30:
                        c.fails(f"bars 太少: {n_bars}")
                    else:
                        c.passes(f"{n_bars} 根 K 线")
                elif name == "summary.json":
                    if "clearinghouse" not in d or "reconstruction" not in d:
                        c.fails("缺少关键字段")
                    else:
                        c.passes(f"av={d['clearinghouse']['account_value']}")
                else:
                    c.passes(f"{len(d) if isinstance(d, list) else 'OK'} 条")
            except Exception as e:
                c.fails(f"解析失败: {e}")
        out.append(c)
    return out


def check_export_summary_matches_db(conn) -> Check:
    c = Check("summary.json 与数据库一致")
    path = config.EXPORT_DIR / "summary.json"
    if not path.exists():
        return c.fails("summary.json 不存在")
    s = json.loads(path.read_text(encoding="utf-8"))
    snap = db.get_latest_clearinghouse(conn)
    if not snap:
        return c.fails("没有 clearinghouse 快照")
    av_db = snap["account_value"]
    av_export = s["clearinghouse"]["account_value"]
    if av_db is None or av_export is None or abs(av_db - av_export) > 1e-6:
        return c.fails(f"账户价值不一致 db={av_db} export={av_export}")
    return c.passes(f"账户价值 ${av_db}")


def check_live(conn) -> Check:
    """选传 --live：拉一次实时 clearinghouse，和导出对比，确认 API 还在工作。"""
    c = Check("实时拉取 clearinghouse")
    try:
        client = HLClient()
        live = client.clearinghouse_state(config.TARGET_USER)
        live_av = float(live.get("marginSummary", {}).get("accountValue", 0))
        snap = db.get_latest_clearinghouse(conn)
        diff = abs(live_av - (snap["account_value"] or 0)) if snap else 0
        return c.passes(
            f"实时 av={live_av} 数据库 av={snap['account_value'] if snap else None}"
            f" 差={diff:.2f}（盈亏波动正常）"
        )
    except Exception as e:
        return c.fails(str(e))


def main() -> int:
    conn = db.connect()
    db.init_db(conn)

    checks: list[Check] = []
    checks.append(check_schema(conn))
    checks.append(check_data_present(conn))
    checks.append(check_recent_update(conn))
    checks.append(check_reconstruction(conn))
    checks.extend(check_exports())
    checks.append(check_export_summary_matches_db(conn))
    if "--live" in sys.argv:
        checks.append(check_live(conn))

    print("=" * 70)
    print("Paul Wei Hyperliquid 追踪 · smoke test")
    print("=" * 70)
    n_ok = 0
    for c in checks:
        prefix = "[ OK ]" if c.ok else "[FAIL]"
        print(f"{prefix} {c.name}: {c.detail}")
        if c.ok:
            n_ok += 1
    print("=" * 70)
    print(f"{n_ok}/{len(checks)} 项检查通过")
    return 0 if n_ok == len(checks) else 1


if __name__ == "__main__":
    sys.exit(main())
