"""根据 fills 重建仓位 + 杠杆时间序列。

约定（与 Hyperliquid 协议保持一致）:
- fill.side == 'B'  →  买入，仓位增加 +sz
- fill.side == 'A'  →  卖出，仓位减少 -sz
- fill.startPosition 是该笔 fill 执行前的仓位大小（带符号）。
  我们也用它做内部一致性校验：start_position + signed_sz ≈ new_position
- 杠杆定义： leverage = 仓位名义价值 / 账户总价值
  - 仓位名义价值 = position_size * mark_price，保留符号（多头正，空头负）。
  - 账户总价值取 portfolio.allTime 序列里 ts 之前最近的一点；
    它是 Hyperliquid 自家报的 marginSummary.accountValue 历史值，已计入未实现盈亏。
"""
from __future__ import annotations
import bisect
from dataclasses import dataclass
from typing import Iterable

from . import db


@dataclass
class FillEvent:
    tid: int
    oid: int | None
    time: int                # ms
    side: str                # 'B' or 'A'
    px: float
    sz: float
    start_position: float | None
    closed_pnl: float | None
    crossed: bool
    dir: str | None


@dataclass
class PositionStep:
    """一次 fill 之后 BTC 仓位的状态。"""
    time: int
    size: float              # 累计带符号仓位
    last_fill_px: float
    fill: FillEvent


def signed_size_delta(side: str, sz: float) -> float:
    if side == "B":
        return sz
    if side == "A":
        return -sz
    raise ValueError(f"未知 side: {side}")


def build_position_steps(fills: Iterable[FillEvent]) -> list[PositionStep]:
    """按时间顺序遍历 fills，输出每次成交后的累计仓位。"""
    steps: list[PositionStep] = []
    pos = 0.0
    # 按 time, tid 排序确保稳定
    sorted_fills = sorted(fills, key=lambda f: (f.time, f.tid))
    for f in sorted_fills:
        pos += signed_size_delta(f.side, f.sz)
        steps.append(PositionStep(time=f.time, size=pos, last_fill_px=f.px, fill=f))
    return steps


def position_size_at(steps: list[PositionStep], ts: int) -> float:
    """二分查找 ts 时刻的累计仓位大小（最近一笔成交后）。"""
    if not steps:
        return 0.0
    # 找 time <= ts 的最后一个
    times = [s.time for s in steps]
    i = bisect.bisect_right(times, ts) - 1
    if i < 0:
        return 0.0
    return steps[i].size


def account_value_at(av_series: list[tuple[int, float]], ts: int) -> float | None:
    """阶梯函数：取 ts 之前最近的账户价值点。"""
    if not av_series:
        return None
    times = [t for t, _ in av_series]
    i = bisect.bisect_right(times, ts) - 1
    if i < 0:
        return None
    return av_series[i][1]


# ----------- DB → 输入数据 -----------

def load_fills(conn, coin: str = "BTC") -> list[FillEvent]:
    out: list[FillEvent] = []
    for row in db.iter_fills(conn, coin=coin):
        out.append(FillEvent(
            tid=row["tid"],
            oid=row["oid"],
            time=row["time"],
            side=row["side"],
            px=row["px"],
            sz=row["sz"],
            start_position=row["start_position"],
            closed_pnl=row["closed_pnl"],
            crossed=bool(row["crossed"]),
            dir=row["dir"],
        ))
    return out


def load_account_value_series(conn) -> list[tuple[int, float]]:
    """优先用 portfolio.allTime；如果某点 value 为 None 则跳过。

    portfolio.allTime 是相对稀疏（每天一两个点），但已覆盖完整账期。
    必要时还可以叠加 clearinghouse_snapshots 的实时点提升精度。
    """
    rows = db.portfolio_series(conn, period="allTime")
    out = [(r["timestamp"], r["account_value"]) for r in rows
           if r["account_value"] is not None]
    # 叠加 clearinghouse_snapshots（我们自己抓的实时点）
    ch = list(conn.execute(
        "SELECT fetched_at, account_value FROM clearinghouse_snapshots "
        "WHERE account_value IS NOT NULL ORDER BY fetched_at ASC"
    ).fetchall())
    for r in ch:
        out.append((r["fetched_at"], r["account_value"]))
    out.sort(key=lambda x: x[0])
    return out


# ----------- 校验：重建 vs clearinghouse -----------

def reconstructed_current_position(conn, coin: str = "BTC") -> float:
    fills = load_fills(conn, coin=coin)
    steps = build_position_steps(fills)
    return steps[-1].size if steps else 0.0


def latest_clearinghouse_position(conn, coin: str = "BTC") -> dict | None:
    snap = db.get_latest_clearinghouse(conn)
    if not snap:
        return None
    poss = db.get_positions_for_snapshot(conn, snap["id"])
    for p in poss:
        if p["coin"] == coin:
            return {
                "snapshot_id": snap["id"],
                "fetched_at": snap["fetched_at"],
                "account_value": snap["account_value"],
                "coin": p["coin"],
                "szi": p["szi"],
                "entry_px": p["entry_px"],
                "position_value": p["position_value"],
                "unrealized_pnl": p["unrealized_pnl"],
                "leverage_type": p["leverage_type"],
                "leverage_value": p["leverage_value"],
            }
    # 没有持仓也算一种状态
    return {
        "snapshot_id": snap["id"],
        "fetched_at": snap["fetched_at"],
        "account_value": snap["account_value"],
        "coin": coin,
        "szi": 0.0,
        "entry_px": None,
        "position_value": 0.0,
        "unrealized_pnl": 0.0,
        "leverage_type": None,
        "leverage_value": 0.0,
    }


def compare_reconstruction(conn, coin: str = "BTC", tol: float = 1e-6) -> dict:
    """对比重建仓位和 clearinghouse 真实仓位。"""
    rec = reconstructed_current_position(conn, coin=coin)
    truth = latest_clearinghouse_position(conn, coin=coin)
    truth_size = truth["szi"] if truth and truth["szi"] is not None else 0.0
    diff = rec - truth_size
    ok = abs(diff) <= max(tol, abs(truth_size) * 1e-4)
    return {
        "coin": coin,
        "reconstructed": rec,
        "clearinghouse": truth_size,
        "diff": diff,
        "ok": ok,
        "truth": truth,
    }
