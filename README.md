# Paul Wei · Hyperliquid 追踪

本地研究项目：长期追踪地址 `0xdae4df7207feb3b350e4284c8efe5f7dac37f637`
（Paul Wei）在 Hyperliquid 上的 BTC 永续合约交易行为。

把所有原始数据落到本地 SQLite，永久保留；定期增量更新；
配套一个本地仪表板，可以在 1d / 4h / 1h 三种 K 线粒度下，
回看任意时间点的仓位、杠杆、活跃挂单和账户价值。

---

## 一眼看哪里运行

```
server.py        ← IDE 点播放，一键起 server + 自动开浏览器
update.py        ← 每周一次的入口：抓取 + 重建 + 导出
verify.py        ← 和 Hyperliquid 实时状态对照
smoke_test.py    ← 自动化检查，确认数据流没坏
import_legacy.py ← 把旧项目 weekly snapshot 灌进数据库（一次性）
import_artemis.py← 从 Artemis S3 拉完整订单 lifecycle（需 AWS 凭证）
migrate_storage.py← 把数据库搬到别的盘
web/index.html   ← 仪表板，浏览器打开 http://localhost:8765/web/
```

## 数据目录在哪？

默认在项目根的 `data/` 下，但可以通过环境变量 `PAUL_DATA_ROOT` 自定义：

```powershell
$env:PAUL_DATA_ROOT = "<your-drive>:\paul-data"    # 当前 PowerShell 窗口
```

PyCharm 里：Run / Edit Configurations → Environment variables 加
`PAUL_DATA_ROOT=<your-drive>:\paul-data`。`server.py` 启动时会打印实际数据目录。

数据库 + 原始 JSON 总共几十 MB，但如果跑 `import_artemis.py` 会临时下载几十~几百 GB
parquet，所以建议放空间大的盘。换位置只要：

```powershell
py -3.12 migrate_storage.py --to <your-drive>:\paul-data
```

之后设置 `PAUL_DATA_ROOT` 环境变量即可。

## 一次完整的更新（建议每周）

```powershell
# 在项目根目录
py -3.12 update.py                # 拉数据 + 重建 + 导出 JSON
py -3.12 smoke_test.py            # 确认一切正常
py -3.12 -m http.server 8765      # 启个本地 server（保持运行）
# 浏览器打开 http://localhost:8765/web/
```

`update.py` 大约会做这些事（默认全部，互相幂等）：

1. 拉 `clearinghouseState` / `spotClearinghouseState`，存当前快照
2. 拉 `frontendOpenOrders` / `openOrders`，记录"此刻在挂的订单列表"
3. 拉 `historicalOrders`（最近 2000 条），按 `oid` 增量去重
4. 拉 `userFills` + 按月分段 `userFillsByTime`，按 `tid` 增量去重
5. 拉 `portfolio`（账户价值历史时间序列）
6. 增量补 BTC `1d/4h/1h` K 线
7. 根据 fills 重建仓位时间序列、计算每根 K 线对应的杠杆
8. 导出为 `data/export/*.json`，给前端用

> Hyperliquid 的 `historicalOrders` 只保留最近 2000 条；越早抓越好。
> 本项目把抓到的订单全部入库（按 `oid` 主键），重复运行不会重复插。

## 项目结构

```
PROJECT_REBUILD_PROMPT.md     原始需求
README.md                     本文件
update.py                     主入口（抓 + 重建 + 导出）
verify.py                     与 Hyperliquid 对照
smoke_test.py                 自动化检查
src/
  config.py                   被追踪地址、API、路径配置
  api.py                      Hyperliquid info 端点客户端
  db.py                       SQLite schema + 写入辅助
  fetch.py                    各端点抓取 + 去重 + 留底
  reconstruct.py              fills → 仓位 / 杠杆 时间序列
  export.py                   预计算 JSON 给前端
data/
  paul.db                     SQLite 数据库（全部原始 + 结构化数据）
  raw/                        每次抓取的原始 JSON（双重备份）
  export/                     给前端用的预计算 JSON
web/
  index.html                  仪表板入口
  app.js                      交互逻辑
  styles.css                  样式
  vendor/                     vendored lightweight-charts（脱网可用）
```

## 数据模型与关键字段

详见 `src/db.py`。要点：

- `orders` 按 **`oid`** 去重，记录 `first_seen_at` / `last_seen_at`，
  状态以"`status_timestamp` 更新的为准"，旧观测不会覆盖新观测
- `fills` 按 **`tid`** 去重；`side='B'` 是买入，`side='A'` 是卖出
- `clearinghouse_snapshots` + `position_snapshots` 是每次抓取的真实状态
- `open_orders_snapshots` 存"那一刻挂在板上的 oid 列表"
- `portfolio_history` 是账户价值时间序列（来自 Hyperliquid 自家）
- `candles` 按 (interval, t) 去重，开盘时间为主键

## 仓位 + 杠杆怎么重建

`src/reconstruct.py`：

- 按时间顺序遍历 fills；`B` 加上 `+sz`、`A` 加上 `-sz`，累计得到带符号仓位
- 用 `clearinghouseState` 最新一笔做最终一致性校验（smoke test 自动跑）
- 杠杆 = `仓位名义价值 / 账户总价值`，**带符号**
  - 正数代表多头，负数代表空头
  - 例：`-0.0263` ≈ "2.62% Short"
- 历史每根 K 线的账户价值来自 `portfolio.allTime` + 我们自己抓的
  `clearinghouse_snapshots` 实时点；用阶梯函数取最近的一点

## 仪表板能做什么

打开 `http://localhost:8765/web/` 后：

- **顶部摘要**：当前账户价值、真实 BTC 仓位、带方向杠杆、重建是否吻合、上次更新时间
- **K 线粒度切换**：1d / 4h / 1h
- **主图**：BTC 蜡烛 + 成交量 + fills 标记
  - 蓝绿色 / 红色箭头：被动成交
  - 橙色 △ / ▽：**主动 / taker 成交**（`crossed=true`），通常对应市价行为
- **副图**：杠杆 baseline（基线为 0，多头绿色、空头红色）
- **右侧"选中时间状态"**：当时仓位、名义价值、账户价值、杠杆、即将到来的市价成交（前瞻 3 根 K 线）
- **右侧挂单表**：当时活跃挂单，按"占账户总价值百分比"从大到小排序
  - 状态为 `open` 但 `status_timestamp` 早于选中时刻的订单会被标记 **推断**
  - 占比分母是**当时账户价值**，不是当前持仓——回答的是"这单相对整个账户有多大"
- **主图水平线**：当前选中时刻的活跃挂单（默认显示前 8 大，可调）
  - 实线 = 直接观测的活跃，虚线 = 推断仍活跃
- **交互**：
  - 鼠标 hover：跟随移动
  - 点击某根 K 线 / `📌 切换钉住` / `最新`：钉住时间，鼠标离开不跳走
  - `▶ 播放` / `⏮ ⏭`：按 K 线步进，演示挂单和杠杆变化（带速度档位）

## 与 Hyperbot 对账

仪表板顶部"地址"是 Hyperbot 直链，方便人工核对。
脚本核对：

```powershell
py -3.12 verify.py          # 显示本地最新快照 + 重建 vs 真实
py -3.12 verify.py --live   # 顺便再实时拉一次和数据库比对
```

`smoke_test.py` 也会自动检查：

- 重建仓位 == clearinghouse 仓位
- summary.json 中的账户价值 == 数据库中的账户价值
- 每个 timeline 文件至少 30 根 K 线
- 最近一次抓取在 14 天内（避免数据陈旧没人发现）

## 用户偏好

- **不一致 = 优先排查**：fills 方向是否解析反、增量是否拉全、
  把 reconstructed 与 clearinghouse 当前真实混在一起
- **数据来源**：成交量是 Hyperliquid 自家、不是 Binance / Coinbase；
  仪表板上有醒目提示
- **滚动窗口**：`historicalOrders` / `userFills` 都是滚动窗口，
  Hyperliquid 不会永久保留——本项目把所有抓到的数据落 SQLite 永久存
- **诚实标注**：无法精确观测的状态（推断仍在挂）会带 "推断" 标签

## 从 Artemis S3 拉完整订单 lifecycle（可选）

Hyperliquid 的 `historicalOrders` 是 2000 条滚动窗口，早期订单会掉出去。
[Artemis Analytics](https://www.artemis.ai/docs/snowflake-share/tables/hyperliquid)
把 Hyperliquid 自家节点的 `node_order_statuses` 完整归档到了一个开源 S3 桶
（`s3://artemis-hyperliquid-data/`，**requester pays**），覆盖 2025-08-17 至今。
这里有所有 user 的全部订单事件，包括"下单后立刻撤销""被 rejected"
"修改"——这些 historicalOrders 都拿不到。

**前置：配 AWS 凭证**（任选）

```powershell
# 临时（仅当前窗口）
$env:AWS_ACCESS_KEY_ID = "AKIA..."
$env:AWS_SECRET_ACCESS_KEY = "..."

# 或者放 ~/.aws/credentials（永久）
# [default]
# aws_access_key_id = ...
# aws_secret_access_key = ...
```

**步骤 1：先 discover 看 prefix 结构**

```powershell
py -3.12 import_artemis.py --discover
```

输出会告诉你 `node_order_statuses/` 下文件实际是怎么 partition 的
（按 `date=YYYY-MM-DD/` 还是 `YYYY/MM/DD/` 还是别的）。

**步骤 2：按月分段拉**

```powershell
# 把 Paul 11 月的全部订单（含撤销 / rejected）拉下来
py -3.12 import_artemis.py --start 2025-11-15 --end 2025-11-30

# 12 月
py -3.12 import_artemis.py --start 2025-12-01 --end 2025-12-31

# ...一个月一个月接着跑
```

**步骤 3：整合并重新导出**

```powershell
py -3.12 update.py --rebuild-orders --no-fetch
```

**成本预估**：DuckDB 用 column pushdown + row group pruning 读 parquet，
不会真把每天几十 GB 全下来；BTC 单一币种、按 user filter，一次性导入整段历史
估计花 $5–30 AWS 流量费。`--keep-parquet` 选项可以把下载的 parquet 留底到
`PAUL_ARTEMIS_STAGING`（默认在 DATA_DIR 下）。

## 依赖

- Python 3.12（用 `py -3.12` 调用；其他 3.9+ 也行，类型注解用了 `|` 语法）
- `requests`（核心抓取）
- `duckdb` + `boto3`（**仅** import_artemis.py 用，运行 `pip install duckdb boto3`）
- 前端无构建步骤，`web/vendor/lightweight-charts.standalone.production.js` 已 vendored

如果遇到 SSL 证书校验失败（企业网络常见），`src/api.py` 会自动回退到
`verify=False` 并打印提示。要强制严格，设置 `HL_STRICT_SSL=1`。

## 已知限制 / 诚实声明

- `historicalOrders` 在 Paul 开仓初期可能已经掉出 Hyperliquid 的滚动窗口。
  本项目会从此刻起持续追加，但起步前的订单可能永远拿不到——
  此类订单不会出现在仪表板上（不会捏造）。
- 账户价值的历史精度受限于 `portfolio.allTime` 的稀疏点（每天 1～2 个）；
  每运行一次 `update.py` 会额外贡献一个高精度点（来自 clearinghouse）。
- 挂单状态对账：如果一个订单在 `last_seen_at` 之后被改动但我们没再抓到，
  在选中时间晚于 `status_timestamp` 时会被标记 **推断**。
- 仪表板对 1h 粒度（~5000 根 K 线）在普通笔记本上完全流畅；
  对超过 30 条挂单的时刻，可用控制栏 "主图挂单线 = 关闭" 把水平线关掉，
  挂单细节仍能在右侧表格看到全部。
