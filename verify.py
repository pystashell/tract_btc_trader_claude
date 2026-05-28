"""与 Hyperliquid / Hyperbot 比对当前状态。

不依赖前端：直接打印对账信息到 stdout。
"""
from __future__ import annotations
import sys
from datetime import datetime, timezone

from src import config, db
from src import reconstruct as R
from src.api import HLClient


def fmt_ts(ms: int | None) -> str:
    if not ms:
        return "-"
    return datetime.fromtimestamp(ms/1000, tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%SZ")


def main() -> int:
    conn = db.connect()
    db.init_db(conn)

    # 1) 数据库内最新快照
    snap = db.get_latest_clearinghouse(conn)
    if not snap:
        print("数据库里还没有 clearinghouse 快照，请先运行 update.py")
        return 1
    print(f"=== 本地数据库最新快照 (fetched_at={fmt_ts(snap['fetched_at'])}) ===")
    print(f"  account_value = ${snap['account_value']}")
    print(f"  total_ntl_pos = ${snap['total_ntl_pos']}")
    print(f"  withdrawable  = ${snap['withdrawable']}")
    poss = db.get_positions_for_snapshot(conn, snap["id"])
    for p in poss:
        szi = p["szi"]
        pv = p["position_value"]
        av = snap["account_value"]
        sign = 1 if szi >= 0 else -1
        signed_lev = sign * (pv / av) if av else None
        print(f"  position[{p['coin']}]: szi={szi} entry={p['entry_px']} "
              f"posValue={pv} signed_lev={signed_lev:.6f}")

    # 2) 重建 vs 真实
    cmp = R.compare_reconstruction(conn, coin="BTC")
    print()
    print("=== 重建（来自 fills）vs 真实（来自 clearinghouseState）===")
    print(f"  重建 BTC 仓位:        {cmp['reconstructed']:.6f}")
    print(f"  clearinghouse 仓位:   {cmp['clearinghouse']:.6f}")
    print(f"  差异:                 {cmp['diff']:.6f}")
    print(f"  匹配:                 {'✓' if cmp['ok'] else '✗ —— 请检查 fills 方向 / 增量是否完整'}")

    # 3) 实时再拉一次和数据库做比对（可选）
    if "--live" in sys.argv:
        print()
        print("=== 实时 Hyperliquid API 拉取（--live） ===")
        client = HLClient()
        live = client.clearinghouse_state(config.TARGET_USER)
        print(f"  serverTime = {fmt_ts(live.get('time'))}")
        ms_ = live.get("marginSummary", {})
        print(f"  accountValue = ${ms_.get('accountValue')}")
        for ap in live.get("assetPositions", []):
            p = ap.get("position", {})
            print(f"  position[{p.get('coin')}]: szi={p.get('szi')} "
                  f"posValue={p.get('positionValue')} entry={p.get('entryPx')}")

    print()
    print(f"Hyperbot 对照页面：https://hyperbot.network/trader/{config.TARGET_USER}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
