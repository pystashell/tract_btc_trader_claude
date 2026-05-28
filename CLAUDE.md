# 项目规则（给 Claude / AI 协作者）

本文件是这个仓库的"contributor 守则"。改代码前请先读一遍。

## 路径与可移植性

- **代码里不要写任何绝对路径**——不写 `D:\...`、`F:\...`、`/Users/...`、`/home/...`。
  目录由 `src/config.py` 集中算（基于 `__file__` 推项目根），需要让用户自定义的部分
  一律通过环境变量（`PAUL_DATA_ROOT`、`PAUL_ARTEMIS_STAGING`、`PAUL_LEGACY_SRC` 等）。
- **代码里也不要写跟特定用户机器有关的路径**（用户名、个人文件夹）。文档里的示例
  必须用占位符（`<your-drive>`、`<path>`），不要把开发者本机路径留在仓库里。
- **不要把硬编码 host / port / API key / 凭证** 写进源码。Hyperliquid 的公开 info URL
  是例外（已经在 `config.py` 里）。任何 AWS / 第三方凭证必须通过 env var 或
  `~/.aws/credentials` 拿。
- 默认 `data/` 在项目根下。**不要修改这个默认**——让用户自己用 `PAUL_DATA_ROOT`
  改到大盘符。任何代码读写数据都从 `config.DATA_DIR / RAW_DIR / EXPORT_DIR / DB_PATH`
  这几个常量出发，不要在别处重新拼。

## 数据完整性

- **绝不删除 `raw_responses` 表的历史记录**——它是所有可重建数据的最后一道防线，
  即使 schema 改了也要能从 raw_responses 重新 build 整个 orders 表
  (参见 `fetch.rebuild_orders_from_raw`)。
- **upsert 时终态优先**：filled / canceled / marginCanceled / triggered / rejected
  这些状态一旦写入，就不被后来观测到的 `open` 覆盖（参见 `db.upsert_order` 注释，
  踩过的坑）。
- **fills 和 orders 都按主键去重**（fills 用 `tid`，orders 用 `oid`）。重跑任何抓取
  都不能产生重复行。
- **历史快照只增不改**：clearinghouse_snapshots / position_snapshots / open_orders_snapshots
  每次抓取追加一条带 `fetched_at` 的新行。

## 仪表板对齐 / 同步

- **杠杆图和 K 线图必须有完全相同的时间点序列**（数据点数量、时间值都一致），
  否则 Lightweight Charts 的 visible logical range 同步会按索引错位。缺杠杆值的
  bar 用 0 填充（Paul 进场前 0% 杠杆本就是正确语义）。
- **两图的 `rightPriceScale.minimumWidth` 必须设一样**，否则上图（价格 5 位数）和
  下图（百分比 3 位数）轴宽不同 → 绘图区左右边缘错位 → 时间轴错位。

## 性能

- Lightweight Charts 的 `addLineSeries / removeSeries` 比较贵。hover 同步挂单线段时
  必须：(1) 同一根 K 线不重画；(2) `requestAnimationFrame` 节流；(3) 复用 series pool
  只更新 data/options，不反复 add/remove。参考 `drawActiveOrderLines`。

## 写代码风格

- 中文注释 + 中文 README + 中文 UI 文案。这个项目用户偏好中文。
- 不要随便加 emoji，除非用户明确要求。
- 不要无中生有创建文档文件（README/CHANGELOG 类）。
- 编辑现有文件优先；新建文件要符合现有结构（脚本入口在根，模块在 `src/`，前端在 `web/`）。

## 不要做的事

- 不擅自跑 destructive 命令（`git push --force`、`rm -rf`、`Remove-Item -Recurse -Force` 等），
  也不要 commit 任何未经用户明确同意的内容。
- 不要在用户没要求时跑 `git commit` / `git push`。
- 不要 vendor 大文件进仓库（除了已有的 `web/vendor/lightweight-charts...js` ~160 KB 是必要的脱网依赖）。
- 不写测试覆盖率 / lint 配置等用户没要的东西。
