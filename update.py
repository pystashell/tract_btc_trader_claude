"""主入口：抓取 → 持久化 → 重建 → 导出。

每周运行一次：
    py -3.12 update.py

只想刷新导出（不重新抓取）：
    py -3.12 update.py --no-fetch

只想抓某个粒度的 K 线：
    py -3.12 update.py --candles 1h

完成后，仪表板数据在 data/export/，打开 web/index.html 即可。
"""
from __future__ import annotations
import argparse
import json
import sys
import time
from datetime import datetime, timezone

from src import config, db, fetch, export
from src.api import HLClient


def _human(ms: int | None) -> str:
    if ms is None:
        return "-"
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%SZ")


def main() -> int:
    parser = argparse.ArgumentParser(description="Paul Wei Hyperliquid 追踪：抓取 + 导出")
    parser.add_argument("--no-fetch", action="store_true",
                        help="跳过 API 抓取，只重新生成导出文件")
    parser.add_argument("--no-export", action="store_true",
                        help="只抓取数据，不重新生成导出文件")
    parser.add_argument("--rebuild-orders", action="store_true",
                        help="清空 orders 表后从 raw_responses 重新填充（修了 upsert bug 后用一次即可）")
    parser.add_argument("--backfill-from-fills", action="store_true",
                        help="把 fills 表里出现过但 orders 表里没有的 oid 反推成虚拟订单")
    parser.add_argument("--user", default=config.TARGET_USER, help="覆盖被追踪地址")
    parser.add_argument("--max-age-days", type=int, default=None,
                        help="（给定时任务用的保底开关）只有距上次成功更新 ≥ N 天才真正抓取；"
                             "数据仍新则什么都不做、直接退出。手动运行不带此参数时行为不变，永远全量抓取。")
    args = parser.parse_args()

    conn = db.connect()
    db.init_db(conn)

    if args.rebuild_orders:
        print("[update] 从 raw_responses 重建 orders 表 ...")
        counts = fetch.rebuild_orders_from_raw(conn)
        print(f"[update] 重建完成: {counts}")

    if args.backfill_from_fills:
        print("[update] 从 fills 反推缺失订单 ...")
        counts = fetch.backfill_orders_from_fills(conn)
        print(f"[update] 反推完成: {counts}")

    # 定时任务保底闸门：只有传了 --max-age-days 才生效。距上次「成功」更新不到 N 天，
    # 就直接退出、什么都不做（负责高频"检查"的那个每周任务用它）。上次是失败/从没成功过
    # → 照常抓取（自愈）。手动运行不带此参数 → 走下面正常流程，行为和以前一模一样，永远抓。
    if args.max_age_days is not None and not args.no_fetch:
        row = conn.execute(
            "SELECT finished_at FROM update_runs WHERE success=1 AND finished_at IS NOT NULL "
            "ORDER BY finished_at DESC LIMIT 1"
        ).fetchone()
        last_ok = row["finished_at"] if row else None
        if last_ok is not None:
            age_days = (int(time.time() * 1000) - last_ok) / 86_400_000
            if age_days < args.max_age_days:
                print(f"[update] 数据仍新鲜：上次成功更新 {_human(last_ok)}"
                      f"（{age_days:.1f} 天前）< {args.max_age_days} 天阈值 → 只检查、不抓取。")
                return 0
            print(f"[update] 数据已过期：上次成功更新 {_human(last_ok)}"
                  f"（{age_days:.1f} 天前）≥ {args.max_age_days} 天阈值 → 开始抓取。")
        else:
            print("[update] 没有成功的更新记录 → 开始抓取。")

    fetch_summary = None
    if not args.no_fetch:
        client = HLClient()
        print(f"[update] 开始抓取 user={args.user}")
        try:
            fetch_summary = fetch.run_full_fetch(conn, client, user=args.user)
        except Exception as e:
            print(f"[update] 抓取失败：{e}", file=sys.stderr)
            return 2
        print(f"[update] 抓取完成：account_value={fetch_summary.get('account_value')}")
    else:
        print("[update] 跳过抓取（--no-fetch）")

    if not args.no_export:
        print("[update] 生成导出 JSON ...")
        result = export.run_all(conn)
        print(f"[update] 导出完成：orders={result['orders']} fills={result['fills']} "
              f"timelines={ {iv: result[f'timeline_{iv}'] for iv in config.CANDLE_INTERVALS} }")
        s = result["summary"]
        print(f"[update] 当前账户价值：${s['clearinghouse']['account_value']}")
        cb = s["clearinghouse"]["btc_position"]
        if cb:
            print(f"[update] 真实 BTC 仓位：szi={cb.get('szi')} entry={cb.get('entry_px')}"
                  f" pos_value={cb.get('position_value')} signed_lev="
                  f"{s['clearinghouse']['btc_leverage_signed']}")
        rc = s["reconstruction"]
        ok_str = "✓ 一致" if rc["matches_clearinghouse"] else "✗ 不一致"
        print(f"[update] 重建仓位：{rc['btc_size']:.6f} BTC  diff={rc['diff_vs_clearinghouse']:.6f} {ok_str}")
        # 把核心数据也回显在 stdout 的最后
        print()
        print(f"打开仪表板：在浏览器中打开 web/index.html")
        print(f"如果浏览器拒绝直接读本地 JSON，请在项目根目录运行：")
        print(f"    py -3.12 -m http.server 8765")
        print(f"然后访问 http://localhost:8765/web/")
    return 0


if __name__ == "__main__":
    sys.exit(main())
