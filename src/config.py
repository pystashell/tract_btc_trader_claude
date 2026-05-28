"""项目配置。统一管理被追踪地址、API 地址、本地路径。

数据根目录可以通过环境变量 `PAUL_DATA_ROOT` 自定义（绝对路径）。例如：
    PowerShell  →  $env:PAUL_DATA_ROOT = "<your-drive>:\\paul-data"
    cmd         →  set PAUL_DATA_ROOT=<your-drive>:\\paul-data
    PyCharm Run Configuration 也可以配 env var。
如果不设，默认是项目目录下 `data/`（兼容旧行为）。

Artemis S3 归档下载的临时目录由 `PAUL_ARTEMIS_STAGING` 覆盖，默认是
`<DATA_ROOT>/artemis_staging`（可能上百 GB，所以建议放空间大的盘）。
"""
import os
from pathlib import Path

# 被追踪地址 —— Paul Wei 在 Hyperliquid 的钱包
TARGET_USER = "0xdae4df7207feb3b350e4284c8efe5f7dac37f637"

# Hyperliquid 公共 info 端点
HL_INFO_URL = "https://api.hyperliquid.xyz/info"

# 项目根目录（src/config.py 上两级）
ROOT = Path(__file__).resolve().parent.parent

# 数据根目录（数据库 + 原始 JSON + 导出 JSON）。可被 env 覆盖。
DATA_DIR = Path(os.environ.get("PAUL_DATA_ROOT") or (ROOT / "data")).resolve()
RAW_DIR = DATA_DIR / "raw"
EXPORT_DIR = DATA_DIR / "export"
DB_PATH = DATA_DIR / "paul.db"

# Artemis S3 同步用的暂存目录（parquet 文件可能很大，放空间大的盘）
ARTEMIS_STAGING = Path(
    os.environ.get("PAUL_ARTEMIS_STAGING")
    or (DATA_DIR / "artemis_staging")
).resolve()

# 仪表板永远在项目目录下（要被 git 跟踪 + 跟着代码版本走）
WEB_DIR = ROOT / "web"

# 要拉的 K 线粒度
CANDLE_INTERVALS = ("1d", "4h", "1h")

# K 线起点：Paul 第一次充值之前一点点（2025-11-01）。
# 这之前的 K 线对研究 Paul 行为没有意义，可以减少抓取量。
CANDLE_START_MS = 1761955200000  # 2025-11-01 00:00:00 UTC

# 是否在 SSL 校验失败时回退到 verify=False（企业网络常见问题）。
# 默认开启回退，可通过环境变量 HL_STRICT_SSL=1 强制严格。
SSL_FALLBACK = True

# Hyperliquid 信息端点对单次请求的速率限制：1200 weight/min。
# 设保守 sleep 间隔，多次请求时使用。
REQUEST_SLEEP_S = 0.25

# Artemis S3 桶（Hyperliquid 完整 order lifecycle 归档，requester-pays）
ARTEMIS_BUCKET = "artemis-hyperliquid-data"
ARTEMIS_REGION = "us-east-1"
