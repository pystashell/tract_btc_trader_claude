"""从 Artemis 的 Hyperliquid 完整归档拉 Paul 的全部订单 lifecycle 事件。

来源：s3://artemis-hyperliquid-data/raw/ （requester-pays，需要你自己的 AWS 凭证）
覆盖：2025-08-17 至今，包含 `node_order_statuses` 全部用户级订单事件
      （含从未成交就撤销、被 rejected 的订单，是 historicalOrders 拿不到的）

工作流（三步走）：

    1) 配 AWS 凭证（任选一）
       - PowerShell:  $env:AWS_ACCESS_KEY_ID="..."; $env:AWS_SECRET_ACCESS_KEY="..."
       - 或：放在 ~/.aws/credentials  [default] profile
       - 或：用 IAM Role / SSO（如果你已经有的话）

    2) 先 discover 路径结构（弄清 prefix 怎么 partition、文件多大）
           py -3.12 import_artemis.py --discover

    3) 真正导入（按月跑，断点续传）
           py -3.12 import_artemis.py --start 2025-11-15 --end 2025-12-31
           py -3.12 import_artemis.py --start 2026-01-01 --end 2026-05-15

成本预估：BTC 单一币种的 order events 大约 1-5 GB/天，按月跑一次会下载 30-150 GB
parquet。S3 流量费 ~$0.09/GB，整个 Paul 历史一次性导入大约 $10-30。
DuckDB 只 stream 需要的 row groups，再加上 column projection，实际带宽更省。

实施细节：
- 用 DuckDB 直接 `read_parquet('s3://...')`，不在本地保存 parquet
  （只想保留 parquet 时用 --keep-parquet，写到 PAUL_ARTEMIS_STAGING）
- 推送下推（pushdown）由 DuckDB 处理：filter user 列、只读关键列
- 结果按 oid 去重写进我们已有的 orders 表
"""
from __future__ import annotations
import argparse
import json
import os
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import duckdb

from src import config, db


# ---------- DuckDB 配置 ----------

def _make_duckdb_conn(staging_dir: Path | None = None) -> duckdb.DuckDBPyConnection:
    """开一个 in-memory DuckDB，挂上 httpfs 扩展并配好 AWS / requester-pays。"""
    con = duckdb.connect(":memory:")
    con.execute("INSTALL httpfs; LOAD httpfs;")
    con.execute(f"SET s3_region='{config.ARTEMIS_REGION}';")
    con.execute("SET s3_requester_pays=true;")

    ak = os.environ.get("AWS_ACCESS_KEY_ID")
    sk = os.environ.get("AWS_SECRET_ACCESS_KEY")
    tk = os.environ.get("AWS_SESSION_TOKEN")
    if ak and sk:
        con.execute(f"SET s3_access_key_id='{ak}';")
        con.execute(f"SET s3_secret_access_key='{sk}';")
        if tk:
            con.execute(f"SET s3_session_token='{tk}';")
    else:
        # DuckDB 也能用 ~/.aws/credentials；显式打开 secret manager + 自动凭证
        try:
            con.execute("CREATE SECRET artemis (TYPE S3, PROVIDER CREDENTIAL_CHAIN);")
        except duckdb.Error:
            pass

    if staging_dir:
        staging_dir.mkdir(parents=True, exist_ok=True)
        con.execute(f"SET temp_directory='{staging_dir}';")
    return con


# ---------- 1) discover: 摸清 S3 prefix 结构 ----------

def discover(con: duckdb.DuckDBPyConnection) -> None:
    """列出 raw/ 下的顶层 prefix 让我们看实际 partition 方式。"""
    print(f"[discover] 列出 s3://{config.ARTEMIS_BUCKET}/raw/")
    # DuckDB glob 列文件
    rows = con.execute(
        f"SELECT file FROM glob('s3://{config.ARTEMIS_BUCKET}/raw/*') LIMIT 100"
    ).fetchall()
    if not rows:
        print("[discover] 没列到任何文件。检查 AWS 凭证 / requester-pays 是否开启。")
        return
    print("[discover] 顶层条目（最多 100）：")
    for r in rows:
        print(" ", r[0])

    # 试探 node_order_statuses
    print()
    print("[discover] 看 node_order_statuses 下的文件结构（最多 20）...")
    rows = con.execute(
        f"SELECT file FROM glob('s3://{config.ARTEMIS_BUCKET}/raw/node_order_statuses/**/*.parquet') LIMIT 20"
    ).fetchall()
    for r in rows:
        print(" ", r[0])

    # 看一个文件的 schema
    if rows:
        sample = rows[0][0]
        print()
        print(f"[discover] 读 schema：{sample}")
        try:
            schema = con.execute(
                f"DESCRIBE SELECT * FROM read_parquet('{sample}')"
            ).fetchall()
            for col in schema:
                print(" ", col)
        except duckdb.Error as e:
            print(f"[discover] 读 schema 失败: {e}")


# ---------- 2) import: 真正按时间段拉数据 ----------

def import_range(con: duckdb.DuckDBPyConnection, conn_db, start: date,
                 end: date, user: str, coin_filter: str | None = "BTC",
                 prefix_template: str | None = None,
                 keep_parquet: bool = False) -> dict:
    """按 start..end（含端点）逐日拉 user 的 order_statuses 事件。

    prefix_template 用来匹配 partition 路径，例如
      's3://artemis-hyperliquid-data/raw/node_order_statuses/date={d}/*.parquet'
    或
      's3://.../node_order_statuses/{d}/**/*.parquet'
    具体格式要根据 --discover 的输出确认。"""
    counts = {"days_scanned": 0, "rows_seen": 0, "orders_inserted": 0}
    if prefix_template is None:
        # 多猜一些常见 partition 风格；按需补充
        templates = [
            "s3://{bucket}/raw/node_order_statuses/date={d}/*.parquet",
            "s3://{bucket}/raw/node_order_statuses/{d}/**/*.parquet",
            "s3://{bucket}/raw/node_order_statuses/dt={d}/*.parquet",
            "s3://{bucket}/raw/node_order_statuses/year={y}/month={m}/day={dd}/*.parquet",
        ]
    else:
        templates = [prefix_template]

    user_lower = user.lower()
    cur = start
    while cur <= end:
        d_str = cur.strftime("%Y-%m-%d")
        y, m, dd = cur.strftime("%Y"), cur.strftime("%m"), cur.strftime("%d")
        found_any = False
        for tpl in templates:
            url = tpl.format(bucket=config.ARTEMIS_BUCKET, d=d_str,
                              y=y, m=m, dd=dd)
            try:
                # 用 SELECT COUNT(*) 探一下文件是否存在
                test = con.execute(
                    f"SELECT COUNT(*) FROM glob('{url}')"
                ).fetchone()
                if not test or not test[0]:
                    continue
            except duckdb.Error:
                continue
            found_any = True
            print(f"[import] {d_str} ← {url}")
            try:
                sql = f"""
                    SELECT *
                    FROM read_parquet('{url}', union_by_name=true)
                    WHERE LOWER(user) = '{user_lower}'
                """
                if coin_filter:
                    # 兼容 order.coin 或顶层 coin
                    sql += (
                        f" AND ("
                        f"  TRY_CAST(\"order\".coin AS VARCHAR) = '{coin_filter}'"
                        f"  OR coin = '{coin_filter}'"
                        f")"
                    )
                df = con.execute(sql).fetchall()
                cols = [c[0] for c in con.description]
                counts["rows_seen"] += len(df)
                for row in df:
                    rec = dict(zip(cols, row))
                    _insert_artemis_row(conn_db, rec)
                    counts["orders_inserted"] += 1
                conn_db.commit()
            except duckdb.Error as e:
                print(f"[import] {d_str} 读 parquet 失败: {e}")
            break  # 试到一个模板能用就停
        if not found_any:
            print(f"[import] {d_str} 没找到匹配文件（尝试过 {len(templates)} 个模板）")
        counts["days_scanned"] += 1
        cur += timedelta(days=1)
    return counts


def _insert_artemis_row(conn, rec: dict) -> None:
    """把 Artemis 的一条 order status 事件喂给我们已有的 upsert。

    Artemis schema 大致是：
      time (timestamp), user (string), status (string), order (struct)
    具体字段名可能是 `order` 或 `orderInfo` 之类；按 discover 阶段看到的来调整。
    """
    # 字段名规范化
    status = rec.get("status") or rec.get("Status")
    o = rec.get("order") or rec.get("orderInfo") or rec.get("orderData")
    if o is None or not status:
        return
    # DuckDB 把 struct 返回成 dict
    if not isinstance(o, dict):
        return

    # time 可能是 datetime/Timestamp/str；统一成 ms
    t = rec.get("time") or rec.get("timestamp") or rec.get("statusTimestamp")
    status_ts_ms = _to_ms(t)
    if status_ts_ms is None:
        return

    # order_obj 字段：尽量兼容 historicalOrders 用过的 key
    order_obj = {
        "coin": o.get("coin"),
        "side": o.get("side"),
        "limitPx": str(o.get("limitPx") if o.get("limitPx") is not None else o.get("limit_px") or ""),
        "sz": str(o.get("sz") if o.get("sz") is not None else ""),
        "origSz": str(o.get("origSz") if o.get("origSz") is not None else o.get("orig_sz") or ""),
        "oid": o.get("oid"),
        "timestamp": _to_ms(o.get("timestamp")) or status_ts_ms,
        "triggerCondition": o.get("triggerCondition") or "N/A",
        "isTrigger": bool(o.get("isTrigger")),
        "triggerPx": str(o.get("triggerPx") if o.get("triggerPx") is not None else "0.0"),
        "children": o.get("children") or [],
        "isPositionTpsl": bool(o.get("isPositionTpsl")),
        "reduceOnly": bool(o.get("reduceOnly")),
        "orderType": o.get("orderType"),
        "tif": o.get("tif"),
        "cloid": o.get("cloid"),
    }
    if order_obj["oid"] is None:
        return
    db.upsert_order(conn, order_obj, status=status, status_ts=status_ts_ms,
                    source="artemis_node_order_statuses",
                    now_ms=status_ts_ms)


def _to_ms(x) -> int | None:
    if x is None:
        return None
    if isinstance(x, (int, float)):
        v = float(x)
        # 启发式：>1e12 当 ms，>1e9 当 s
        if v > 1e12:
            return int(v)
        if v > 1e9:
            return int(v * 1000)
        return int(v)
    if isinstance(x, str):
        # ISO 时间字符串
        s = x.replace("Z", "+00:00")
        try:
            return int(datetime.fromisoformat(s).timestamp() * 1000)
        except ValueError:
            try:
                return int(x)
            except ValueError:
                return None
    # datetime / pandas Timestamp
    try:
        if hasattr(x, "timestamp"):
            return int(x.timestamp() * 1000)
    except Exception:
        return None
    return None


# ---------- CLI ----------

def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--discover", action="store_true",
                        help="只列 S3 路径结构 + 看 schema，不导入")
    parser.add_argument("--start", help="导入起始日期 YYYY-MM-DD")
    parser.add_argument("--end", help="导入结束日期 YYYY-MM-DD（含）")
    parser.add_argument("--user", default=config.TARGET_USER)
    parser.add_argument("--coin", default="BTC", help="只导某个币种；填 '' 拉所有")
    parser.add_argument("--prefix-template",
                        help="覆盖默认 S3 prefix 模板（discover 后定下来再用）")
    parser.add_argument("--keep-parquet", action="store_true",
                        help="把下载到的 parquet 也存到 PAUL_ARTEMIS_STAGING 留底")
    args = parser.parse_args()

    print(f"[artemis] DATA_DIR = {config.DATA_DIR}")
    print(f"[artemis] ARTEMIS_STAGING = {config.ARTEMIS_STAGING}")
    print(f"[artemis] bucket = {config.ARTEMIS_BUCKET} (requester pays)")

    con = _make_duckdb_conn(config.ARTEMIS_STAGING if args.keep_parquet else None)

    if args.discover:
        discover(con)
        return 0

    if not args.start or not args.end:
        print("[artemis] 需要 --start / --end，或者用 --discover。", file=sys.stderr)
        return 1

    start = datetime.strptime(args.start, "%Y-%m-%d").date()
    end = datetime.strptime(args.end, "%Y-%m-%d").date()
    if start > end:
        print("[artemis] start 必须 <= end", file=sys.stderr)
        return 1

    conn_db = db.connect()
    db.init_db(conn_db)
    coin = args.coin if args.coin else None
    counts = import_range(con, conn_db, start, end, args.user, coin_filter=coin,
                           prefix_template=args.prefix_template,
                           keep_parquet=args.keep_parquet)
    print(f"[artemis] 完成：{counts}")
    print()
    print("接下来建议：")
    print("    py -3.12 update.py --rebuild-orders --no-fetch")
    print("把新订单整合到导出 JSON 里。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
