"""一键启动仪表板。

在 PyCharm 里右键此文件 → Run，或者点绿色三角即可。

默认行为：
1. 在 127.0.0.1:8765 启动本地 HTTP server，serve 项目根目录。
2. 自动用默认浏览器打开 http://localhost:8765/web/。
3. Ctrl+C 停止。

可选参数：
    py server.py --update         先跑一次 update.py 拉最新数据再开仪表板
    py server.py --port 9000      换端口
    py server.py --no-browser     不自动打开浏览器
    py server.py --host 0.0.0.0   监听所有网卡（如要在同网段其他设备访问）
"""
from __future__ import annotations
import argparse
import http.server
import os
import socket
import socketserver
import subprocess
import sys
import threading
import time
import urllib.parse
import webbrowser
from pathlib import Path

ROOT = Path(__file__).resolve().parent

# 数据目录可以通过 PAUL_DATA_ROOT 放到别处（例如另一块大容量盘）。
# 这里要在 server 启动时读环境变量，让 /data/* URL 路由到那个位置。
DATA_DIR_OVERRIDE = (
    Path(os.environ.get("PAUL_DATA_ROOT")).resolve()
    if os.environ.get("PAUL_DATA_ROOT") else None
)


class RootHandler(http.server.SimpleHTTPRequestHandler):
    """SimpleHTTPRequestHandler 默认 serve 项目根目录；
    /data/* 单独路由到 PAUL_DATA_ROOT（如果设置了），否则还是项目根下 data/。"""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=str(ROOT), **kwargs)

    def translate_path(self, path: str) -> str:
        if DATA_DIR_OVERRIDE:
            # 分离 query / fragment
            parsed = urllib.parse.urlsplit(path)
            url_path = parsed.path
            if url_path.startswith("/data/") or url_path == "/data":
                rel = url_path[len("/data"):].lstrip("/")
                # 防穿越：os.path.normpath 阻止 ../
                rel_safe = os.path.normpath(rel).replace("\\", "/")
                if rel_safe.startswith(".."):
                    return super().translate_path(path)
                return str(DATA_DIR_OVERRIDE / rel_safe)
        return super().translate_path(path)

    def log_message(self, fmt, *args):
        ts = time.strftime("%H:%M:%S")
        sys.stderr.write(f"[{ts}] {self.address_string()} {fmt % args}\n")


class QuietThreadingServer(socketserver.ThreadingMixIn, http.server.HTTPServer):
    daemon_threads = True
    allow_reuse_address = True


def find_free_port(host: str, preferred: int) -> int:
    """端口被占用就向上尝试 9 个，再失败就让 OS 分配。"""
    for p in [preferred] + [preferred + i for i in range(1, 10)]:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            try:
                s.bind((host, p))
                return p
            except OSError:
                continue
    # 全占了，让 OS 选一个
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind((host, 0))
        return s.getsockname()[1]


def maybe_run_update() -> int:
    """同步调用 update.py。失败也只警告，不阻止 server 起来——
    用户至少还能看上次抓到的数据。"""
    py = sys.executable
    cmd = [py, str(ROOT / "update.py")]
    print(f"[server] 先跑一次更新：{' '.join(cmd)}")
    try:
        proc = subprocess.run(cmd, cwd=str(ROOT))
        if proc.returncode != 0:
            print(f"[server] update.py 退出码 {proc.returncode}，"
                  "继续启动仪表板（用已有数据）", file=sys.stderr)
        return proc.returncode
    except FileNotFoundError as e:
        print(f"[server] 没法运行 update.py：{e}", file=sys.stderr)
        return -1


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--host", default="127.0.0.1",
                        help="默认 127.0.0.1（仅本机）；改 0.0.0.0 开放局域网访问")
    parser.add_argument("--no-browser", action="store_true",
                        help="不自动打开浏览器")
    parser.add_argument("--update", action="store_true",
                        help="启动前先跑一次 update.py 拉最新数据")
    args = parser.parse_args()

    # 让 stdout 在 PyCharm 控制台里能正确显示中文
    try:
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    except Exception:
        pass

    if args.update:
        maybe_run_update()

    port = find_free_port(args.host, args.port)
    if port != args.port:
        print(f"[server] 端口 {args.port} 被占用，改用 {port}")

    server = QuietThreadingServer((args.host, port), RootHandler)
    url_host = "localhost" if args.host in ("127.0.0.1", "0.0.0.0") else args.host
    url = f"http://{url_host}:{port}/web/"

    print()
    print("=" * 60)
    print(f"  Paul Wei Hyperliquid 追踪仪表板")
    print("=" * 60)
    print(f"  仪表板:    {url}")
    print(f"  项目根:    {ROOT}")
    if DATA_DIR_OVERRIDE:
        print(f"  数据目录:  {DATA_DIR_OVERRIDE}  (PAUL_DATA_ROOT)")
    else:
        print(f"  数据目录:  {ROOT / 'data'}  (默认)")
    print(f"  停止:      在此窗口按 Ctrl+C")
    print("=" * 60)
    print()

    if not args.no_browser:
        # 等 server 真正开始 accept 之后再开浏览器
        threading.Timer(0.6, lambda: webbrowser.open(url)).start()

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n[server] 收到 Ctrl+C，停止...")
    finally:
        server.shutdown()
        server.server_close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
