"""从 Paul 的全历史 ledger + fills + funding 重建账户价值，跟当前真实 accountValue 对账。

会计等式（按 Hyperliquid 的口径）：

    accountValue ≈ Σ deposits
                 - Σ withdrawals
                 - Σ internal_transfers (转给别人的)
                 + Σ closedPnl           （已实现盈亏）
                 - Σ fees                （成交手续费）
                 + Σ funding_received    （资金费率净流入；payment 字段，负=给出，正=收到）
                 + unrealizedPnl         （当前持仓浮盈，来自 clearinghouseState）

每一项都从权威来源拿：

    deposits / withdrawals / transfers : Hyperliquid userNonFundingLedgerUpdates
    closedPnl                          : 本地 DB 的 fills.closed_pnl 累加（每笔 close 贡献）
    fees                               : 本地 DB 的 fills.fee 累加
    funding                            : Hyperliquid userFunding（每个 funding interval 一条）
    unrealizedPnl                      : Hyperliquid clearinghouseState 最新一份

跑：
    py -3.12 verify_account_value.py
"""
from __future__ import annotations
import sys
from datetime import datetime, timezone

import requests
import urllib3

from src import config, db

urllib3.disable_warnings()


def call_api(body: dict) -> object:
    r = requests.post(config.HL_INFO_URL, json=body, timeout=30, verify=False)
    r.raise_for_status()
    return r.json()


def fetch_all_ledger(user: str) -> list:
    end = int(datetime.now(timezone.utc).timestamp() * 1000)
    return call_api({"type": "userNonFundingLedgerUpdates",
                      "user": user, "startTime": 0, "endTime": end})


def fetch_all_funding(user: str) -> list:
    """userFunding 单次最多 ~500-2000 条；按 30 天一段抓避免截断。"""
    end_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
    out: list = []
    seg = 30 * 24 * 3600 * 1000
    cur = 0  # 从 epoch 开始；Hyperliquid 会从用户最早 funding event 起返回
    # 但单次返回上限是有的，所以走 30 天段
    cur = 1731628800000  # 2024-11-15，提前 1 年；Paul 任何 funding 都在这之后
    while cur < end_ms:
        nxt = min(cur + seg, end_ms)
        chunk = call_api({"type": "userFunding", "user": user,
                          "startTime": cur, "endTime": nxt})
        if isinstance(chunk, list):
            out.extend(chunk)
        cur = nxt
    # 去重（不同段可能重叠）
    seen = set()
    dedup = []
    for x in out:
        # userFunding 没有 unique id；用 (time, coin, delta.usdc) 当 key
        key = (x.get("time"), x.get("delta", {}).get("coin"),
               x.get("delta", {}).get("usdc"))
        if key in seen:
            continue
        seen.add(key)
        dedup.append(x)
    return dedup


def f(x) -> float:
    try:
        return float(x)
    except (TypeError, ValueError):
        return 0.0


def main() -> int:
    user = config.TARGET_USER
    print(f"=== 账户价值对账 for {user} ===\n")

    # 1) Ledger updates → deposits / withdrawals / transfers
    print("[fetch] userNonFundingLedgerUpdates ...")
    ledger = fetch_all_ledger(user)
    deposits = withdrawals = internal_transfer_out = internal_transfer_in = 0.0
    spot_perp_xfer = 0.0
    other_items = []
    for it in ledger:
        d = it.get("delta", {})
        t = d.get("type")
        if t == "deposit":
            deposits += f(d.get("usdc"))
        elif t == "withdraw":
            withdrawals += f(d.get("usdc"))
        elif t in ("accountClassTransfer", "subAccountTransfer",
                   "internalTransfer"):
            amt = f(d.get("usdc"))
            # 假设 usdc>0 = 流入，<0 = 流出（不同事件实际语义可能不同，
            # 这里先按绝对值打印让人工核对）
            if amt > 0:
                internal_transfer_in += amt
            else:
                internal_transfer_out += -amt
        elif t == "spotTransfer":
            spot_perp_xfer += f(d.get("usdc")) - f(d.get("nativeAmount") or 0)
        else:
            other_items.append((t, d))
    print(f"  ledger 共 {len(ledger)} 条:")
    print(f"  + deposits           = ${deposits:,.4f}")
    print(f"  - withdrawals        = ${withdrawals:,.4f}")
    print(f"  internal transfer in = ${internal_transfer_in:,.4f}")
    print(f"  internal transfer out= ${internal_transfer_out:,.4f}")
    if other_items:
        print(f"  其他类型（需人工看）: {[t for t,_ in other_items[:5]]}...")

    # 2) Fills → closedPnl 和 fees
    conn = db.connect()
    db.init_db(conn)
    closed_pnl, fees, fill_count = 0.0, 0.0, 0
    for r in conn.execute(
        "SELECT closed_pnl, fee FROM fills"
    ):
        closed_pnl += r["closed_pnl"] or 0.0
        fees += r["fee"] or 0.0
        fill_count += 1
    print(f"\n[DB] {fill_count} 笔 fills:")
    print(f"  + 累计已实现盈亏 (Σ closedPnl) = ${closed_pnl:,.4f}")
    print(f"  - 累计手续费 (Σ fee)          = ${fees:,.4f}")

    # 3) Funding 累计
    print("\n[fetch] userFunding 按 30 天段拉 ...")
    funding = fetch_all_funding(user)
    funding_net = 0.0
    for x in funding:
        # 资金费率结算：usdc 为正表示账户收到，负表示支付
        amt = f(x.get("delta", {}).get("usdc"))
        funding_net += amt
    print(f"  funding 共 {len(funding)} 条；净收/支 = ${funding_net:,.4f}")

    # 4) 当前 clearinghouseState 拿 unrealizedPnl + accountValue（真值）
    ch = call_api({"type": "clearinghouseState", "user": user})
    ms = ch.get("marginSummary", {}) or {}
    real_av = f(ms.get("accountValue"))
    unrealized = sum(f(p["position"].get("unrealizedPnl"))
                     for p in ch.get("assetPositions", []))
    print(f"\n[clearinghouseState 实时]")
    print(f"  accountValue (真值)  = ${real_av:,.4f}")
    print(f"  当前未实现盈亏       = ${unrealized:,.4f}")

    # 5) 算重建值
    reconstructed = (
        deposits - withdrawals
        + internal_transfer_in - internal_transfer_out
        + closed_pnl - fees
        + funding_net
        + unrealized
    )
    diff = reconstructed - real_av
    tol = max(0.5, abs(real_av) * 0.0005)  # 0.05% 或 $0.5，取大
    ok = abs(diff) <= tol

    print(f"\n=== 对账 ===")
    print(f"  重建 accountValue = ${reconstructed:,.4f}")
    print(f"  实时 accountValue = ${real_av:,.4f}")
    print(f"  差异              = ${diff:,.4f}  (容差 ${tol:.4f})")
    print(f"  匹配              = {'✓' if ok else '✗'}")
    if not ok:
        print()
        print("  差异常见来源：")
        print("    - internal/sub-account 转账方向判定")
        print("    - rejected / liquidation 这些不在 fills 表的事件")
        print("    - 抓 funding 时段分割重叠未完全去重")
        print("    - hyperliquid 内部 fee 折扣（builder fee / referral rebate）")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
