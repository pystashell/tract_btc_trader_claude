"""把本项目的数据目录搬到指定盘符（例如从默认 data/ 搬到大容量盘）。

用法（PowerShell）：
    py -3.12 migrate_storage.py --to <your-drive>:\\paul-data

它做这些事：
1. 拷贝当前 DATA_DIR 的全部内容（paul.db + raw/ + export/）到目标路径。
2. 验证 SQLite 数据库在新位置能打开、行数匹配。
3. 打印你需要在 IDE / 终端里设置的环境变量，让以后 update.py 等脚本指向新位置。
4. 默认不会自动删除老数据——你确认新位置工作正常后手动删 `data\\` 即可。
   想自动删用 `--delete-source`。

注意：迁移期间不要并行运行 update.py / server.py，避免 paul.db 被同时写。
"""
from __future__ import annotations
import argparse
import os
import shutil
import sqlite3
import sys
from pathlib import Path

from src import config


def copy_tree(src: Path, dst: Path) -> dict:
    """递归复制；如果目标存在则报错避免覆盖。"""
    if dst.exists() and any(dst.iterdir()):
        raise FileExistsError(f"目标已存在且非空，请先清空或换个路径：{dst}")
    dst.parent.mkdir(parents=True, exist_ok=True)
    print(f"[migrate] 复制 {src} → {dst}")
    shutil.copytree(src, dst)
    # 简单大小核对
    src_size = sum(p.stat().st_size for p in src.rglob("*") if p.is_file())
    dst_size = sum(p.stat().st_size for p in dst.rglob("*") if p.is_file())
    return {"src_bytes": src_size, "dst_bytes": dst_size, "match": src_size == dst_size}


def verify_db(old_db: Path, new_db: Path) -> dict:
    """对 SQLite 文件做最小一致性检查：表数 + 行数完全一致。"""
    if not new_db.exists():
        return {"ok": False, "reason": f"新位置没有 paul.db: {new_db}"}
    old = sqlite3.connect(f"file:{old_db}?mode=ro", uri=True)
    new = sqlite3.connect(f"file:{new_db}?mode=ro", uri=True)
    try:
        tables = [r[0] for r in old.execute(
            "SELECT name FROM sqlite_master WHERE type='table'").fetchall()]
        details = {}
        ok = True
        for t in tables:
            oc = old.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
            nc = new.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
            details[t] = (oc, nc)
            if oc != nc:
                ok = False
        return {"ok": ok, "tables": details}
    finally:
        old.close(); new.close()


def main() -> int:
    parser = argparse.ArgumentParser(description="把数据目录搬到新盘")
    parser.add_argument("--to", required=True,
                        help="新的数据根目录绝对路径（形如 <drive>:\\paul-data，或 Linux/Mac 的 /mnt/.../paul-data）")
    parser.add_argument("--delete-source", action="store_true",
                        help="迁移成功后自动删除老数据（默认保留）")
    args = parser.parse_args()

    old = config.DATA_DIR
    new = Path(args.to).resolve()

    if old == new:
        print(f"[migrate] 新旧路径相同：{old}", file=sys.stderr)
        return 1
    if not old.exists():
        print(f"[migrate] 当前 DATA_DIR 不存在，无需迁移：{old}", file=sys.stderr)
        return 1

    print(f"[migrate] 当前 DATA_DIR = {old}")
    print(f"[migrate] 目标       = {new}")

    # 大小估算
    total = sum(p.stat().st_size for p in old.rglob("*") if p.is_file())
    n_files = sum(1 for _ in old.rglob("*") if _.is_file())
    print(f"[migrate] 待迁移 {n_files} 个文件，约 {total/1e9:.2f} GB")

    info = copy_tree(old, new)
    print(f"[migrate] 复制完成：src={info['src_bytes']:,}B dst={info['dst_bytes']:,}B match={info['match']}")

    ver = verify_db(old / "paul.db", new / "paul.db")
    print(f"[migrate] 数据库一致性检查：{ver}")
    if not ver["ok"]:
        print("[migrate] 检查未通过，停在这一步，不删源数据。", file=sys.stderr)
        return 2

    print()
    print("=" * 70)
    print("迁移成功。下一步：让脚本知道新位置。")
    print("=" * 70)
    print(f"在 PowerShell 里设置（仅当前窗口生效）：")
    print(f'    $env:PAUL_DATA_ROOT = "{new}"')
    print()
    print(f"在 PyCharm 里：Run / Edit Configurations → Environment variables：")
    print(f"    PAUL_DATA_ROOT={new}")
    print()
    print(f"想永久生效，在 Windows 控制面板 → 系统 → 环境变量里加：")
    print(f"    PAUL_DATA_ROOT = {new}")
    print()

    if args.delete_source:
        print(f"[migrate] --delete-source 已指定，删除老目录 {old}")
        shutil.rmtree(old)
    else:
        print(f"[migrate] 老数据保留在 {old}。新位置确认能跑 update.py 后再手动删。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
