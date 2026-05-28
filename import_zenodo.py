"""从 Zenodo 公开数据集 "An Open Book" 拉 BTC 2025-12 月的完整订单 lifecycle。

数据来源（**完全免费、HTTP 直链、不要 AWS 账号**）：
    https://zenodo.org/records/18184441
    License: CC BY 4.0
    Citation:
        "An Open Book: Level 4 Order Book Data from the Hyperliquid Exchange"

包含：BTC 12 月份完整订单事件（含撤销 / 改单 / 触发）+ 单独的 rejected 订单文件
+ 完整 trades + book_diffs。本脚本只下 BTC orders（19.3 GB）+ mapdir（10 MB）
按 Paul 的地址过滤后写入本项目 SQLite。

用法：
    py -3.12 import_zenodo.py --download-mapdir            # 第一次先拉 10MB 映射表
    py -3.12 import_zenodo.py --discover-user              # 看 Paul 的 numeric userId
    py -3.12 import_zenodo.py --download orders            # 下载 19.3 GB BTC orders
    py -3.12 import_zenodo.py --import orders              # 解析 + 过滤 + 入库
    py -3.12 import_zenodo.py --download rejected          # 可选：拉 46 GB rejected
    py -3.12 import_zenodo.py --import rejected            # 入库 rejected
    py -3.12 import_zenodo.py --all                        # 一条龙（除 rejected）

下载到 PAUL_ARTEMIS_STAGING（默认 DATA_DIR/artemis_staging），解析完后
你可以手动删除 .tar.xz 释放磁盘。

数据规模（参考 SCHEMA.md）：
- mapdir.tar.xz: 10 MB
- btc_orders_202512.tar.xz: 19.3 GB（含所有 BTC 用户的全部订单事件）
- btc_rejected_202512.tar.xz: 46 GB（rejected 单，可选）

按地址过滤后，Paul 12 月的订单大概几十到几百条。
"""
from __future__ import annotations
import argparse
import gzip
import io
import json
import lzma
import sys
import tarfile
import time
from pathlib import Path
from typing import Iterator
from urllib.parse import urljoin

import numpy as np
import pandas as pd
import requests
import urllib3

from src import config, db


ZENODO_BASE = "https://zenodo.org/records/18184441/files/"
FILES = {
    "mapdir": ("mapdir.tar.xz", 10_200_000),
    "orders": ("btc_orders_202512.tar.xz", 19_300_000_000),
    "rejected": ("btc_rejected_202512.tar.xz", 46_000_000_000),
}


# 54 字节的 record 格式，与 SCHEMA.md 一致
RECORD_DTYPE = np.dtype([
    ("ts",               "<u8"),  # nanoseconds since epoch
    ("userId",           "<u4"),  # uint32, lookup users.csv
    ("isBuilder",        "?"),
    ("statusId",         "<u1"),  # lookup statuses.csv
    ("isAsk",            "?"),    # true = sell (A), false = buy (B)
    ("limitPx",          "<u4"),  # custom uint32 encoding
    ("sz",               "<u4"),
    ("oid",              "<u8"),
    ("timestampDiff",    "<u4"),  # ms since order submission
    ("triggerCondition", "<i4"),
    ("triggered",        "?"),
    ("isTrigger",        "?"),
    ("hasChildren",      "?"),
    ("isPositionTpsl",   "?"),
    ("reduceOnly",       "?"),
    ("orderTypeId",      "<u1"),
    ("tifId",            "<u1"),
    ("triggerPx",        "<u4"),
    ("origSz",           "<u4"),
])
assert RECORD_DTYPE.itemsize == 54


_POWERS = np.array([1, 1e1, 1e2, 1e3, 1e4, 1e5, 1e6, 1e7], dtype=np.float64)


def decode_price(encoded: np.ndarray) -> np.ndarray:
    """uint32 编码：top3 bits = decimals, bottom 29 bits = value。"""
    decimals = (encoded >> 29).astype(np.int64)
    value = (encoded & 0x1FFFFFFF).astype(np.float64)
    return value / _POWERS[decimals]


def decode_signed_price(encoded: np.ndarray) -> np.ndarray:
    """同上，但 bit 28 是 sign flag（仅 triggerCondition 用）。"""
    decimals = (encoded.view(np.uint32) >> 29).astype(np.int64)
    value = (encoded.view(np.uint32) & 0x0FFFFFFF).astype(np.float64)
    is_negative = (encoded.view(np.uint32) & 0x10000000) != 0
    px = value / _POWERS[decimals]
    return np.where(is_negative, -px, px)


# ---------- 下载 ----------

def _streaming_download(url: str, dst: Path, total_hint: int | None = None) -> None:
    """断点续传地把 url 下到 dst。"""
    dst.parent.mkdir(parents=True, exist_ok=True)
    tmp = dst.with_suffix(dst.suffix + ".part")
    mode = "ab"
    start_byte = tmp.stat().st_size if tmp.exists() else 0
    headers = {"Range": f"bytes={start_byte}-"} if start_byte else {}

    urllib3.disable_warnings()
    print(f"[download] {url}\n  → {dst} (start_byte={start_byte:,})")
    r = requests.get(url, headers=headers, stream=True, timeout=60, verify=False)
    if start_byte and r.status_code == 200:
        # 服务器不支持 Range，重头开始
        start_byte = 0
        mode = "wb"
    elif r.status_code not in (200, 206):
        raise RuntimeError(f"HTTP {r.status_code} for {url}")
    total = int(r.headers.get("Content-Length", 0)) + start_byte
    if total_hint and not total:
        total = total_hint

    last_print = time.time()
    received = start_byte
    chunk = 8 << 20  # 8 MB
    with tmp.open(mode) as f:
        for buf in r.iter_content(chunk_size=chunk):
            if not buf:
                continue
            f.write(buf)
            received += len(buf)
            now = time.time()
            if now - last_print > 2:
                pct = 100.0 * received / total if total else 0.0
                print(f"  ... {received/1e9:.2f} / {total/1e9:.2f} GB ({pct:.1f}%)")
                last_print = now
    tmp.rename(dst)
    print(f"[download] 完成：{dst} ({dst.stat().st_size/1e9:.2f} GB)")


def download_file(name: str) -> Path:
    if name not in FILES:
        raise ValueError(f"未知文件 {name}；可选：{list(FILES)}")
    fname, hint = FILES[name]
    url = urljoin(ZENODO_BASE, fname)
    dst = config.ARTEMIS_STAGING / fname
    if dst.exists() and dst.stat().st_size > 0:
        print(f"[download] 已存在，跳过：{dst} ({dst.stat().st_size/1e9:.2f} GB)")
        return dst
    _streaming_download(url, dst, total_hint=hint)
    return dst


# ---------- mapdir 加载 ----------

def _extract_csv_from_mapdir(mapdir_path: Path, name: str) -> pd.DataFrame:
    """从 mapdir.tar.xz 里抽出指定 csv（不解包到磁盘）。"""
    with tarfile.open(mapdir_path, "r:xz") as tf:
        members = [m for m in tf.getmembers()
                   if m.isfile() and m.name.endswith(f"/{name}.csv")
                   or m.name.endswith(f"{name}.csv")]
        if not members:
            raise FileNotFoundError(f"{name}.csv 不在 {mapdir_path} 里")
        m = members[0]
        f = tf.extractfile(m)
        return pd.read_csv(f, header=None, names=["a", "b"])


def load_user_id(mapdir_path: Path, user_address: str) -> int | None:
    """从 users.csv 找 user_address 对应的数字 userId。"""
    df = _extract_csv_from_mapdir(mapdir_path, "users")
    # df 的两列是 address, id（按 SCHEMA 说 a=address, b=id）
    df.columns = ["address", "id"]
    df["address"] = df["address"].str.lower()
    hits = df[df["address"] == user_address.lower()]
    if hits.empty:
        return None
    return int(hits["id"].iloc[0])


def load_category_maps(mapdir_path: Path) -> dict[str, dict[int, str]]:
    maps = {}
    for name in ("statuses", "order_types", "tifs"):
        df = _extract_csv_from_mapdir(mapdir_path, name)
        df.columns = ["label", "id"]
        maps[name] = dict(zip(df["id"].astype(int), df["label"]))
    return maps


# ---------- 解析 + 过滤 + 入库 ----------

def _iter_data_files(tar_path: Path) -> Iterator[tuple[str, bytes]]:
    """流式遍历 .tar.xz 里的 .data / .data.gz 二进制文件。"""
    with tarfile.open(tar_path, "r:xz") as tf:
        for m in tf:
            if not m.isfile():
                continue
            n = m.name
            if not (n.endswith(".data") or n.endswith(".data.gz")):
                continue
            f = tf.extractfile(m)
            if f is None:
                continue
            buf = f.read()
            if n.endswith(".gz"):
                buf = gzip.decompress(buf)
            yield n, buf


def import_orders(conn, tar_path: Path, mapdir_path: Path, user_address: str,
                  also_rejected: bool = False) -> dict:
    user_id = load_user_id(mapdir_path, user_address)
    if user_id is None:
        return {"error": f"users.csv 里没找到 {user_address}"}
    cat = load_category_maps(mapdir_path)

    counts = {"user_id": user_id, "files": 0, "records": 0,
              "paul_records": 0, "orders_upserted": 0}
    for name, buf in _iter_data_files(tar_path):
        counts["files"] += 1
        if len(buf) % 54 != 0:
            print(f"[import] 警告：{name} 字节数 {len(buf)} 不是 54 的倍数，跳过")
            continue
        recs = np.frombuffer(buf, dtype=RECORD_DTYPE)
        counts["records"] += int(recs.size)
        mask = recs["userId"] == user_id
        if not mask.any():
            continue
        sub = recs[mask]
        counts["paul_records"] += int(sub.size)
        _ingest_records(conn, sub, cat)
        counts["orders_upserted"] += int(sub.size)
        conn.commit()
        print(f"[import] {name}: {sub.size} 条 Paul 记录"
              f"（累计 {counts['paul_records']}）")
    return counts


def _ingest_records(conn, recs: np.ndarray, cat: dict[str, dict[int, str]]) -> None:
    # 批量 decode 价格 / 尺寸
    limit_px = decode_price(recs["limitPx"])
    sz = decode_price(recs["sz"])
    orig_sz = decode_price(recs["origSz"])
    trigger_px = decode_price(recs["triggerPx"])
    trigger_cond = decode_signed_price(recs["triggerCondition"])
    ts_ms = (recs["ts"] // 1_000_000).astype(np.int64)  # ns → ms

    for i in range(len(recs)):
        r = recs[i]
        status = cat["statuses"].get(int(r["statusId"]), "unknown")
        otype = cat["order_types"].get(int(r["orderTypeId"]))
        tif = cat["tifs"].get(int(r["tifId"]))
        side = "A" if r["isAsk"] else "B"
        # ts 是事件时间；订单创建时间约等于 ts - timestampDiff(ms)
        evt_ts = int(ts_ms[i])
        create_ts = evt_ts - int(r["timestampDiff"])
        order_obj = {
            "coin": "BTC",
            "side": side,
            "limitPx": str(float(limit_px[i])),
            "sz": str(float(sz[i])),
            "origSz": str(float(orig_sz[i])),
            "oid": int(r["oid"]),
            "timestamp": create_ts,
            "triggerCondition": str(float(trigger_cond[i])) if r["isTrigger"] else "N/A",
            "isTrigger": bool(r["isTrigger"]),
            "triggerPx": str(float(trigger_px[i])),
            "children": [],
            "isPositionTpsl": bool(r["isPositionTpsl"]),
            "reduceOnly": bool(r["reduceOnly"]),
            "orderType": otype,
            "tif": tif,
            "cloid": None,
        }
        db.upsert_order(conn, order_obj, status=status, status_ts=evt_ts,
                        source="zenodo_open_book", now_ms=evt_ts)


# ---------- CLI ----------

def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                      formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--download-mapdir", action="store_true",
                        help="下载 mapdir.tar.xz（10 MB）")
    parser.add_argument("--discover-user", action="store_true",
                        help="只查 Paul 的 numeric userId，不下大文件")
    parser.add_argument("--download", choices=["orders", "rejected"],
                        help="下载 BTC orders（19.3 GB）或 rejected（46 GB）")
    parser.add_argument("--import-from", choices=["orders", "rejected"],
                        dest="import_from", help="解析并入库")
    parser.add_argument("--all", action="store_true",
                        help="下 mapdir + orders，然后入库（不包含 rejected）")
    parser.add_argument("--user", default=config.TARGET_USER)
    args = parser.parse_args()

    print(f"[zenodo] STAGING = {config.ARTEMIS_STAGING}")
    print(f"[zenodo] user    = {args.user}")
    config.ARTEMIS_STAGING.mkdir(parents=True, exist_ok=True)

    if args.discover_user:
        mp = download_file("mapdir")
        uid = load_user_id(mp, args.user)
        print(f"[zenodo] users.csv 里 {args.user} → userId = {uid}")
        if uid is None:
            return 1
        return 0

    if args.all:
        download_file("mapdir")
        tar = download_file("orders")
        conn = db.connect(); db.init_db(conn)
        mp = config.ARTEMIS_STAGING / FILES["mapdir"][0]
        c = import_orders(conn, tar, mp, args.user)
        print(f"[zenodo] 完成：{c}")
        return 0

    if args.download_mapdir:
        download_file("mapdir")

    if args.download:
        download_file(args.download)

    if args.import_from:
        tar = config.ARTEMIS_STAGING / FILES[args.import_from][0]
        mp = config.ARTEMIS_STAGING / FILES["mapdir"][0]
        if not mp.exists():
            print("[zenodo] mapdir 还没下，先跑 --download-mapdir", file=sys.stderr)
            return 1
        if not tar.exists():
            print(f"[zenodo] {tar.name} 还没下，先跑 --download {args.import_from}",
                  file=sys.stderr)
            return 1
        conn = db.connect(); db.init_db(conn)
        c = import_orders(conn, tar, mp, args.user)
        print(f"[zenodo] 完成：{c}")
        print()
        print("接下来：")
        print("    py -3.12 update.py --rebuild-orders --no-fetch")
    return 0


if __name__ == "__main__":
    sys.exit(main())
