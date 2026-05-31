/* Paul Wei · Hyperliquid 追踪仪表板
 * 数据来自 ../data/export/*.json，由 update.py 预生成。
 *
 * 关键交互：
 * - 主图（BTC K 线）+ 副图（杠杆）联动 crosshair / 时间轴
 * - 鼠标 hover 显示当时仓位 / 杠杆 / 挂单
 * - 点击或 📌 按钮把时间钉住，鼠标离开不会跳走
 * - 播放：按 K 线节奏向前推进，演示挂单和杠杆变化
 */
"use strict";

const LWC = window.LightweightCharts;
if (!LWC) {
  document.body.innerHTML =
    '<div style="padding:40px;color:#e6edf3;font-family:sans-serif">'
    + '无法加载 lightweight-charts。'
    + '请确认 <code>web/vendor/lightweight-charts.standalone.production.js</code> 存在，'
    + '或重新运行 <code>update.py</code> 时网络可达。</div>';
  throw new Error("lightweight-charts missing");
}

// -------- 全局状态 --------
const state = {
  interval: "4h",           // 当前 K 线粒度
  timeline: null,           // 当前 interval 对应的 timeline 数据
  orders: [],               // 全部订单
  fills: [],                // 全部 fills（含 position_after）
  portfolioAlltime: [],     // [{t, av, pnl}]
  summary: null,
  pinned: false,            // 是否钉住
  selectedT: null,          // 选中的 K 线 t（ms）
  activeOrderSeries: [],    // 主图上目前的挂单线段（每条是一个独立 LineSeries）
  playTimer: null,
  playIndex: -1,
  maxOrderLines: 999,       // 主图最多画几条挂单线段；默认全部，可在控制栏切到 4/8/16/关闭
  showMakerFills: true,     // 主图是否显示被动（maker）成交箭头；false 时只剩主动箭头
  showAllOrders: false,     // 是否在主图一次性显示所有订单的完整生命周期（与选中时刻无关）
  _allOrdersRendered: false, // 全部订单模式下挂单线是否已画（避免 hover 重画导致卡顿）
};
window.state = state;       // 方便控制台调试

// -------- DOM 引用 --------
const $ = (id) => document.getElementById(id);
const els = {
  cardAv: $("card-av"),
  cardAvSource: $("card-av-source"),
  cardPos: $("card-pos"),
  cardPosSide: $("card-pos-side"),
  cardLev: $("card-lev"),
  cardMatch: $("card-match"),
  cardMatchDetail: $("card-match-detail"),
  cardUpdate: $("card-update"),
  cardUpdateDetail: $("card-update-detail"),
  hyperbotLink: $("hyperbot-link"),
  segInterval: $("seg-interval"),
  btnPlay: $("btn-play"),
  btnStepBack: $("btn-step-back"),
  btnStepFwd: $("btn-step-fwd"),
  playSpeed: $("play-speed"),
  btnPin: $("btn-pin"),
  btnPinLatest: $("btn-pin-latest"),
  pinState: $("pin-state"),
  selTime: $("sel-time"),
  selPx: $("sel-px"),
  selPos: $("sel-pos"),
  selNotional: $("sel-notional"),
  selAv: $("sel-av"),
  selLev: $("sel-lev"),
  selFillsCount: $("sel-fillscount"),
  selIncoming: $("sel-incoming"),
  ordersTableBody: document.querySelector("#orders-table tbody"),
  ordersEmpty: $("orders-empty"),
};

// -------- 工具 --------
function fmtTs(ms) {
  if (!ms && ms !== 0) return "—";
  const d = new Date(ms);
  return d.getUTCFullYear() + "-"
    + String(d.getUTCMonth() + 1).padStart(2, "0") + "-"
    + String(d.getUTCDate()).padStart(2, "0") + " "
    + String(d.getUTCHours()).padStart(2, "0") + ":"
    + String(d.getUTCMinutes()).padStart(2, "0") + "Z";
}

function fmtUsd(x, decimals = 2) {
  if (x === null || x === undefined || Number.isNaN(x)) return "—";
  const sign = x < 0 ? "-" : "";
  const abs = Math.abs(x);
  return sign + "$" + abs.toLocaleString(undefined, {
    minimumFractionDigits: decimals,
    maximumFractionDigits: decimals,
  });
}

function fmtBtc(x, decimals = 5) {
  if (x === null || x === undefined || Number.isNaN(x)) return "—";
  return x.toFixed(decimals);
}

function fmtLev(x) {
  if (x === null || x === undefined || Number.isNaN(x)) return "—";
  return (x * 100).toFixed(2) + "%";
}

function levColor(lev) {
  if (lev === null || lev === undefined || Number.isNaN(lev)) return "";
  if (lev > 0.0005) return "green";
  if (lev < -0.0005) return "red";
  return "";
}

// 二分查找：在数组 arr 中找 arr[i].key <= ts 的最大 i
function bisectLeq(arr, ts, key = "t") {
  let lo = 0, hi = arr.length;
  while (lo < hi) {
    const mid = (lo + hi) >> 1;
    if (arr[mid][key] <= ts) lo = mid + 1;
    else hi = mid;
  }
  return lo - 1;
}

// -------- 数据加载 --------
async function loadJson(name) {
  const r = await fetch(`../data/export/${name}.json`, { cache: "no-store" });
  if (!r.ok) throw new Error(`加载 ${name}.json 失败: HTTP ${r.status}`);
  return r.json();
}

async function loadAll() {
  const [summary, orders, fills, port] = await Promise.all([
    loadJson("summary"),
    loadJson("orders"),
    loadJson("fills"),
    loadJson("portfolio_alltime"),
  ]);
  state.summary = summary;
  state.orders = orders;
  state.fills = fills;
  state.portfolioAlltime = port;
}

async function loadTimeline(interval) {
  const tl = await loadJson(`timeline_${interval}`);
  state.timeline = tl;
}

// -------- 顶部摘要 --------
function renderSummaryCards() {
  const s = state.summary;
  if (!s) return;
  els.hyperbotLink.href = s.hyperbot_url;
  els.hyperbotLink.textContent = s.target_user;

  const ch = s.clearinghouse;
  els.cardAv.textContent = fmtUsd(ch.account_value);
  els.cardAvSource.textContent = `clearinghouseState · ${fmtTs(ch.fetched_at)}`;

  const pos = ch.btc_position || {};
  const szi = pos.szi;
  els.cardPos.textContent = (szi === null || szi === undefined)
    ? "—" : `${fmtBtc(szi)} BTC`;
  els.cardPos.classList.toggle("green", szi > 0);
  els.cardPos.classList.toggle("red", szi < 0);
  const side = szi > 0 ? "多头 Long" : szi < 0 ? "空头 Short" : "无仓位";
  const entry = pos.entry_px ? `· 开仓均价 ${fmtUsd(pos.entry_px, 0)}` : "";
  els.cardPosSide.textContent = `${side} ${entry}`;

  const lev = ch.btc_leverage_signed;
  els.cardLev.textContent = fmtLev(lev);
  els.cardLev.classList.toggle("green", lev > 0.0005);
  els.cardLev.classList.toggle("red", lev < -0.0005);

  const m = s.reconstruction;
  if (m.matches_clearinghouse) {
    els.cardMatch.textContent = "✓ 一致";
    els.cardMatch.classList.add("green");
  } else {
    els.cardMatch.textContent = "✗ 不一致";
    els.cardMatch.classList.add("red");
  }
  els.cardMatchDetail.textContent =
    `重建 ${fmtBtc(m.btc_size)}　·　差 ${fmtBtc(m.diff_vs_clearinghouse)}`;

  const lu = s.latest_update;
  els.cardUpdate.textContent = fmtTs(lu.finished_at || lu.started_at);
  els.cardUpdateDetail.textContent = lu.success ? "上次成功" : "上次失败 / 进行中";
}

// -------- 图表创建 --------
let priceChart, priceSeries, volumeSeries;
let levChart, levSeries;
let crosshairLocked = false;

function makeCharts() {
  const common = {
    layout: {
      background: { color: "#161b22" },
      textColor: "#cdd9e5",
    },
    grid: {
      vertLines: { color: "#222a35" },
      horzLines: { color: "#222a35" },
    },
    // 固定价格轴最小宽度：让主图(价格标签)和副图(百分比标签)的绘图区
    // 左右边缘对齐，否则两个图的时间轴会错位
    rightPriceScale: { borderColor: "#2a323d", minimumWidth: 84 },
    timeScale: {
      borderColor: "#2a323d",
      timeVisible: true,
      secondsVisible: false,
      rightOffset: 6,  // 右侧留出几根 K 线宽的空白，让"还在挂"的挂单线段有地方延伸
    },
    crosshair: {
      mode: 1, // Magnet 不会让 hover 飘走，会贴到最近的 bar
    },
  };

  priceChart = LWC.createChart($("chart-price"), common);
  priceSeries = priceChart.addCandlestickSeries({
    upColor: "#26a69a",
    downColor: "#ef5350",
    borderUpColor: "#26a69a",
    borderDownColor: "#ef5350",
    wickUpColor: "#26a69a",
    wickDownColor: "#ef5350",
  });
  // 成交量副层（叠在主图，占下方 20%）
  volumeSeries = priceChart.addHistogramSeries({
    priceFormat: { type: "volume" },
    priceScaleId: "",   // overlay
  });
  volumeSeries.priceScale().applyOptions({
    scaleMargins: { top: 0.8, bottom: 0.0 },
  });

  levChart = LWC.createChart($("chart-lev"), {
    ...common,
    timeScale: { ...common.timeScale, visible: true },
    rightPriceScale: {
      ...common.rightPriceScale,
      // 比例尺以 0 为基线
    },
  });
  levSeries = levChart.addBaselineSeries({
    baseValue: { type: "price", price: 0 },
    topLineColor: "rgba(38,166,154,1)",
    topFillColor1: "rgba(38,166,154,0.35)",
    topFillColor2: "rgba(38,166,154,0.05)",
    bottomLineColor: "rgba(239,83,80,1)",
    bottomFillColor1: "rgba(239,83,80,0.05)",
    bottomFillColor2: "rgba(239,83,80,0.35)",
    priceFormat: {
      type: "custom",
      formatter: (v) => (v * 100).toFixed(2) + "%",
      minMove: 0.0001,
    },
  });

  // 同步时间轴
  const syncing = { fwd: false, bwd: false };
  priceChart.timeScale().subscribeVisibleLogicalRangeChange((r) => {
    if (!r || syncing.bwd) return;
    syncing.fwd = true;
    try { levChart.timeScale().setVisibleLogicalRange(r); } finally { syncing.fwd = false; }
  });
  levChart.timeScale().subscribeVisibleLogicalRangeChange((r) => {
    if (!r || syncing.fwd) return;
    syncing.bwd = true;
    try { priceChart.timeScale().setVisibleLogicalRange(r); } finally { syncing.bwd = false; }
  });

  // Crosshair 同步 + hover 选中
  priceChart.subscribeCrosshairMove((param) => onCrosshair(param, "price"));
  levChart.subscribeCrosshairMove((param) => onCrosshair(param, "lev"));

  // resize
  function resize() {
    priceChart.resize($("chart-price").clientWidth, $("chart-price").clientHeight);
    levChart.resize($("chart-lev").clientWidth, $("chart-lev").clientHeight);
  }
  window.addEventListener("resize", resize);
}

function applyTimelineToCharts() {
  const bars = state.timeline.bars;
  if (!bars || !bars.length) return;
  const candleData = bars.map(b => ({
    time: Math.floor(b.t / 1000),
    open: b.o, high: b.h, low: b.l, close: b.c,
  }));
  const volData = bars.map(b => ({
    time: Math.floor(b.t / 1000),
    value: b.v,
    color: b.c >= b.o ? "rgba(38,166,154,0.4)" : "rgba(239,83,80,0.4)",
  }));
  // 杠杆图必须和 K 线图有【完全相同的时间点序列且长度一致】，否则两图的可见区间
  // 按数据点索引同步时会错位。whitespace（只给 time）会被 lightweight-charts 在
  // 序列开头 trim 掉，导致点数不一致 → 仍错位。所以缺杠杆值的 bar 一律填 0：
  // Paul 进场前没有仓位，0% 杠杆本来就是正确语义。
  const levData = bars.map(b => ({
    time: Math.floor(b.t / 1000),
    value: (b.lev !== null && b.lev !== undefined) ? b.lev : 0,
  }));

  priceSeries.setData(candleData);
  volumeSeries.setData(volData);
  levSeries.setData(levData);
  // 杠杆副图按可见区域自动缩放（默认行为，显式打开以防被覆盖）
  levChart.priceScale("right").applyOptions({ autoScale: true });
  priceChart.priceScale("right").applyOptions({ autoScale: true });

  applyFillMarkers();

  // 切了粒度 → K 线坐标系变了，全部订单模式需要按新粒度重画一次
  state._allOrdersRendered = false;
  // 默认选中最后一根
  state.selectedT = bars[bars.length - 1].t;
  renderForSelected();
  // 默认显示最近 ~150 根
  const showN = Math.min(150, bars.length);
  const lastIdx = bars.length - 1;
  priceChart.timeScale().setVisibleLogicalRange({ from: lastIdx - showN, to: lastIdx + 2 });
}

// 重新计算 + 套上主图的 fill 箭头。受 state.showMakerFills 影响。
// 拆成 4 种箭头：被动买 / 主动买 / 被动卖 / 主动卖，每根 K 线最多 4 个标记。
// 标记文字显示该笔成交的"占账户百分比"（fill_notional / account_value），
// 而不是 BTC 数量——绝对量没有指导价值，占账户比例才能直观看出冲击大小。
function applyFillMarkers() {
  if (!state.timeline) return;
  const bars = state.timeline.bars;
  // av fallback：Paul 进场最初几天 portfolio.allTime 还没记录账户价值，
  // 直接用 null 会让早期标记显示 "—"。用 bars 里第一个有 av 的值当 initialAv，
  // 然后向前传播一个 lastAv，保证所有 fill 都能算出合理的占账户百分比。
  let firstAv = null;
  for (const b of bars) { if (b.av && b.av > 0) { firstAv = b.av; break; } }
  let lastAv = firstAv;
  const markers = [];
  for (const b of bars) {
    if (b.av && b.av > 0) lastAv = b.av;
    if (!b.fills || !b.fills.length) continue;
    let makerBuyN = 0, takerBuyN = 0, makerSellN = 0, takerSellN = 0;  // notional
    for (const f of b.fills) {
      const ntl = (f.sz || 0) * (f.px || 0);
      if (f.side === "B") {
        if (f.crossed) takerBuyN += ntl; else makerBuyN += ntl;
      } else if (f.side === "A") {
        if (f.crossed) takerSellN += ntl; else makerSellN += ntl;
      }
    }
    const tSec = Math.floor(b.t / 1000);
    const av = (b.av && b.av > 0) ? b.av : lastAv;
    const pct = (n) => (av && av > 0) ? `${(n / av * 100).toFixed(2)}%` : "—";
    if (state.showMakerFills && makerBuyN > 0) {
      markers.push({ time: tSec, position: "belowBar", color: "#26a69a",
                     shape: "arrowUp", text: `B ${pct(makerBuyN)}` });
    }
    if (takerBuyN > 0) {
      markers.push({ time: tSec, position: "belowBar", color: "#ffa726",
                     shape: "arrowUp", text: `B ${pct(takerBuyN)}（主动）` });
    }
    if (state.showMakerFills && makerSellN > 0) {
      markers.push({ time: tSec, position: "aboveBar", color: "#ef5350",
                     shape: "arrowDown", text: `S ${pct(makerSellN)}` });
    }
    if (takerSellN > 0) {
      markers.push({ time: tSec, position: "aboveBar", color: "#ec407a",
                     shape: "arrowDown", text: `S ${pct(takerSellN)}（主动）` });
    }
  }
  priceSeries.setMarkers(markers);
}

// -------- crosshair 处理 --------
let _rafPending = false;
function onCrosshair(param, sourceChart) {
  if (state.pinned) return;
  if (!param || !param.time) return;
  const tSec = typeof param.time === "number" ? param.time : null;
  if (tSec === null) return;
  const tMs = tSec * 1000;
  const idx = bisectLeq(state.timeline.bars, tMs, "t");
  if (idx < 0) return;
  const bar = state.timeline.bars[idx];

  // 同步另一图的 crosshair —— 这是廉价操作，每次都做
  const otherChart = sourceChart === "price" ? levChart : priceChart;
  const otherSeries = sourceChart === "price" ? levSeries : priceSeries;
  try {
    otherChart.setCrosshairPosition(0, param.time, otherSeries);
  } catch (e) { /* 老版本兜底 */ }

  // 只有当前选中 K 线真正变化时才重绘（重绘要 add/remove 多个 lineSeries，昂贵）
  if (state.selectedT === bar.t) return;
  state.selectedT = bar.t;

  // requestAnimationFrame 节流：1 帧内合并多次 crosshair 事件
  if (_rafPending) return;
  _rafPending = true;
  requestAnimationFrame(() => {
    _rafPending = false;
    renderForSelected();
  });
}

// -------- 选中状态渲染 --------
function renderForSelected() {
  const bars = state.timeline.bars;
  if (!bars.length) return;
  const idx = bisectLeq(bars, state.selectedT, "t");
  if (idx < 0) return;
  const bar = bars[idx];

  els.selTime.textContent = fmtTs(bar.t);
  els.selPx.textContent = fmtUsd(bar.c, 1);
  els.selPos.textContent = bar.pos === null ? "—" : `${fmtBtc(bar.pos)} BTC`;
  els.selPos.className = "v " + (bar.pos > 0 ? "green" : bar.pos < 0 ? "red" : "");
  els.selNotional.textContent = bar.notional === null ? "—" : fmtUsd(bar.notional, 0);
  els.selAv.textContent = bar.av === null ? "—" : fmtUsd(bar.av, 0);
  els.selLev.textContent = fmtLev(bar.lev);
  els.selLev.className = "v " + levColor(bar.lev);
  els.selFillsCount.textContent = (bar.fills?.length || 0).toString();

  // 即将到来的市价成交：在 selectedT 之后 3 根 K 线内出现 crossed=true
  const incomingHorizonBars = 3;
  const horizonTs = idx + incomingHorizonBars < bars.length
    ? bars[idx + incomingHorizonBars].t : Infinity;
  const upcoming = [];
  for (let i = idx + 1; i < bars.length; i++) {
    if (bars[i].t > horizonTs) break;
    for (const f of (bars[i].fills || [])) {
      if (f.crossed) {
        upcoming.push({ bar_t: bars[i].t, ...f });
      }
    }
  }
  if (upcoming.length) {
    const f = upcoming[0];
    const dir = f.side === "B" ? "买入(Long)" : "卖出(Short)";
    els.selIncoming.textContent =
      `${i_bars(idx, bisectLeq(bars, f.bar_t, "t"))} 根 K 线后 主动${dir} ${fmtBtc(f.sz)} @ ${fmtUsd(f.px, 1)}`;
    els.selIncoming.className = "v yellow";
  } else {
    els.selIncoming.textContent = "（无）";
    els.selIncoming.className = "v";
  }

  // 主图上挂单的水平线。
  // 全部订单模式下，线条与选中时刻无关，hover 时不必重画——
  // 只在模式 / 粒度变化时画一次（用 _allOrdersRendered 标记缓存）。
  if (state.showAllOrders) {
    if (!state._allOrdersRendered) {
      drawActiveOrderLines(bar);
      state._allOrdersRendered = true;
    }
  } else {
    state._allOrdersRendered = false;
    drawActiveOrderLines(bar);
  }
  // 表格
  renderOrdersTable(bar);
}

function i_bars(a, b) { return Math.max(0, b - a); }

// 计算在某时刻 t 仍然活跃的订单（不再有"推断"概念——我们的数据完整覆盖了
// Paul 的全部订单 lifecycle，每个订单的真实状态都是直接观测的）
function activeOrdersAt(tMs) {
  const out = [];
  for (const o of state.orders) {
    if (o.timestamp > tMs) continue;             // 还没下
    if (o.status === "open") {
      // 仍在挂（已知事实，不是推断）
      out.push(o);
    } else {
      // 终态：filled / canceled / marginCanceled / triggered / rejected
      // 在 t 时刻该订单已经经历完生命周期？只有 status_timestamp > t 时算活跃
      if (o.status_timestamp > tMs) {
        out.push(o);
      }
    }
  }
  return out;
}

function drawActiveOrderLines(bar) {
  const tMs = bar.T || bar.t;
  // 两种数据来源：
  //   - 普通模式：只取"选中时刻仍活跃"的挂单，从选中时刻向右延伸（右=未来视角）
  //   - 全部订单模式：取所有订单，各自画在真实生命周期区间，与选中时刻无关
  //     （用来一次性总览 Paul 的全部挂单分布）
  const orders = state.showAllOrders ? state.orders : activeOrdersAt(tMs);
  const av = bar.av;
  const withPct = orders.map(o => {
    const px = o.is_trigger ? o.trigger_px : o.limit_px;
    const sz = o.sz;
    const notional = (px || 0) * (sz || 0);
    const pct = (av && av > 0) ? (notional / av) : null;
    return { ...o, _px: px, _notional: notional, _pct: pct };
  }).sort((a, b) => (b._notional || 0) - (a._notional || 0));

  // 先把要画的"数据描述"算好，再决定怎么复用 series
  const draws = [];
  // 全部订单模式不受"主图挂单线 N 条"限制（那个上限是给单时刻视图防遮挡用的）
  if (state.maxOrderLines > 0 || state.showAllOrders) {
    const topN = state.showAllOrders ? withPct : withPct.slice(0, state.maxOrderLines);
    const bars = state.timeline.bars;
    if (state._candleTimesCache !== bars) {
      state._candleTimes = bars.map(b => Math.floor(b.t / 1000));
      state._candleTimesCache = bars;
      state._barIntervalSec = bars.length >= 2
        ? state._candleTimes[1] - state._candleTimes[0] : 4 * 3600;
    }
    const candleTimes = state._candleTimes;
    const chartEnd = candleTimes[candleTimes.length - 1];
    const openExtendTime = chartEnd + 6 * state._barIntervalSec;

    for (const o of topN) {
      if (!o._px) continue;
      // 起点：
      //   - 全部订单模式 → 订单自己的 timestamp（真实生命周期，与选中时刻无关）
      //   - 普通模式     → 选中时刻（从钉住点向右延伸的"未来"视角）
      const segStartMs = state.showAllOrders ? o.timestamp : bar.t;
      const segStart = Math.floor(segStartMs / 1000);
      let endTime;
      if (o.status === "open") endTime = openExtendTime;
      else {
        endTime = Math.floor(o.status_timestamp / 1000);
        if (endTime > chartEnd) endTime = chartEnd;
      }
      if (endTime < segStart) continue;
      // 起点和终点都贴到 <= 该时刻的最近 K 线（用 ≤ 而不是 ≥），
      // 否则当订单生命周期跨度小于一根 K 线粒度时，
      // candleAtOrAfter(start) 会跳到下一根 K 线 → 超过 endCandle → 整段被丢。
      const startCandle = candleAtOrBefore(candleTimes, segStart);
      if (startCandle === null) continue;
      const endCandle = o.status === "open"
        ? openExtendTime : candleAtOrBefore(candleTimes, endTime);
      if (endCandle === null || endCandle < startCandle) continue;

      const isBuy = o.side === "B";
      const color = isBuy ? "rgba(38,166,154,0.9)" : "rgba(239,83,80,0.9)";
      // 线型按订单自身结局：成交 = 实线，未成交（撤销 / 仍在挂）= 点线
      const isFilled = o.status === "filled";
      const data = startCandle === endCandle
        ? [{ time: startCandle, value: o._px }]
        : [{ time: startCandle, value: o._px }, { time: endCandle, value: o._px }];
      draws.push({
        data,
        color,
        lineStyle: isFilled ? LWC.LineStyle.Solid : LWC.LineStyle.Dotted,
      });
    }
  }

  // 复用 pool：前 N 个 series 已存在就改 options/data，多余的 removeSeries。
  // 主要为 hover 性能服务：连续 hover 时几乎不发生 add/removeSeries。
  for (let i = 0; i < draws.length; i++) {
    let s = state.activeOrderSeries[i];
    if (!s) {
      s = priceChart.addLineSeries({
        lineWidth: 2,
        lastValueVisible: false,
        priceLineVisible: false,
        crosshairMarkerVisible: false,
        pointMarkersVisible: false,
      });
      state.activeOrderSeries.push(s);
    }
    s.applyOptions({ color: draws[i].color, lineStyle: draws[i].lineStyle });
    s.setData(draws[i].data);
    // 清空之前复用的 marker（如果有），让线段保持干净
    try { s.setMarkers([]); } catch (e) { /* ignore */ }
  }
  while (state.activeOrderSeries.length > draws.length) {
    const s = state.activeOrderSeries.pop();
    try { priceChart.removeSeries(s); } catch (e) { /* ignore */ }
  }
}

// 返回 candleTimes 中 >= ts 的最小元素；不存在则 null
function candleAtOrAfter(candleTimes, ts) {
  let lo = 0, hi = candleTimes.length;
  while (lo < hi) {
    const mid = (lo + hi) >> 1;
    if (candleTimes[mid] < ts) lo = mid + 1;
    else hi = mid;
  }
  return lo < candleTimes.length ? candleTimes[lo] : null;
}

// 返回 candleTimes 中 <= ts 的最大元素；不存在则 null
function candleAtOrBefore(candleTimes, ts) {
  let lo = 0, hi = candleTimes.length;
  while (lo < hi) {
    const mid = (lo + hi) >> 1;
    if (candleTimes[mid] <= ts) lo = mid + 1;
    else hi = mid;
  }
  return lo > 0 ? candleTimes[lo - 1] : null;
}

function renderOrdersTable(bar) {
  const orders = activeOrdersAt(bar.T || bar.t);
  els.ordersTableBody.innerHTML = "";
  if (!orders.length) {
    els.ordersEmpty.hidden = false;
    return;
  }
  els.ordersEmpty.hidden = true;
  // 排序：按账户占比从大到小
  const av = bar.av;
  const rows = orders.map(o => {
    const px = o.is_trigger ? o.trigger_px : o.limit_px;
    const sz = o.sz;
    const notional = (px || 0) * (sz || 0);
    const pct = (av && av > 0) ? (notional / av) : null;
    return { ...o, _px: px, _notional: notional, _pct: pct };
  }).sort((a, b) => (b._notional || 0) - (a._notional || 0));

  for (const o of rows) {
    const tr = document.createElement("tr");
    tr.className = o.side === "B" ? "buy" : "sell";
    const pctStr = o._pct === null ? "—" : (o._pct * 100).toFixed(2) + "%";
    const typeStr = o.is_trigger ? `${o.order_type || "Trigger"} @${fmtUsd(o.trigger_px, 0)}`
      : (o.order_type || "Limit");
    // status badge：成交=绿，撤销=灰，在挂=黄
    const statusBadge = o.status === "filled"
      ? '<span class="badge ok">成交</span>'
      : (o.status === "open" || o.status === "triggered")
        ? '<span class="badge open">在挂</span>'
        : '<span class="badge canceled">撤销</span>';
    tr.innerHTML = `
      <td class="left side">${o.side === "B" ? "买 Long" : "卖 Short"} ${statusBadge}</td>
      <td>${fmtUsd(o._px, 0)}</td>
      <td>${fmtBtc(o.sz, 5)}</td>
      <td>${fmtUsd(o._notional, 0)}</td>
      <td>${pctStr}</td>
      <td class="left">${typeStr}${o.reduce_only ? " · RO" : ""}</td>
      <td class="left muted small">${fmtTs(o.timestamp)}</td>
    `;
    els.ordersTableBody.appendChild(tr);
  }
}

// -------- 控件交互 --------
function bindControls() {
  // 粒度切换
  els.segInterval.querySelectorAll("button").forEach(btn => {
    btn.addEventListener("click", async () => {
      const v = btn.dataset.val;
      if (v === state.interval) return;
      els.segInterval.querySelectorAll("button").forEach(b => b.classList.remove("active"));
      btn.classList.add("active");
      state.interval = v;
      stopPlay();
      await loadTimeline(v);
      applyTimelineToCharts();
    });
  });

  // 钉住
  els.btnPin.addEventListener("click", () => {
    state.pinned = !state.pinned;
    els.pinState.textContent = state.pinned
      ? `已钉住 ${fmtTs(state.selectedT)}` : "未钉住（跟随鼠标）";
    els.btnPin.classList.toggle("active", state.pinned);
  });
  els.btnPinLatest.addEventListener("click", () => {
    const bars = state.timeline.bars;
    if (!bars.length) return;
    state.selectedT = bars[bars.length - 1].t;
    state.pinned = true;
    renderForSelected();
    els.pinState.textContent = `已钉住 ${fmtTs(state.selectedT)}`;
  });

  // 单步
  els.btnStepBack.addEventListener("click", () => stepBy(-1));
  els.btnStepFwd.addEventListener("click", () => stepBy(1));

  // 播放
  els.btnPlay.addEventListener("click", () => {
    if (state.playTimer) stopPlay();
    else startPlay();
  });

  // 挂单线数量
  $("order-lines-count").addEventListener("change", (e) => {
    state.maxOrderLines = Number(e.target.value) || 0;
    state._allOrdersRendered = false;  // 数量变化 → 全部订单模式也重画一次
    renderForSelected();
  });

  // 成交标记过滤：全部 / 仅主动
  $("seg-fills-filter").querySelectorAll("button").forEach(btn => {
    btn.addEventListener("click", () => {
      $("seg-fills-filter").querySelectorAll("button").forEach(b => b.classList.remove("active"));
      btn.classList.add("active");
      state.showMakerFills = btn.dataset.val === "all";
      applyFillMarkers();
    });
  });

  // 显示全部订单：关 / 开
  $("seg-show-all").querySelectorAll("button").forEach(btn => {
    btn.addEventListener("click", () => {
      $("seg-show-all").querySelectorAll("button").forEach(b => b.classList.remove("active"));
      btn.classList.add("active");
      state.showAllOrders = btn.dataset.val === "on";
      state._allOrdersRendered = false;  // 模式切换 → 强制重画一次
      renderForSelected();
    });
  });

  // 点击主图任意位置 → 切换钉住到该 K 线
  // lightweight-charts 没有直接的 click，但 subscribeClick 在 4.x 有
  priceChart.subscribeClick(param => {
    if (!param || !param.time) return;
    const tMs = param.time * 1000;
    const idx = bisectLeq(state.timeline.bars, tMs, "t");
    if (idx < 0) return;
    state.selectedT = state.timeline.bars[idx].t;
    state.pinned = true;
    renderForSelected();
    els.pinState.textContent = `已钉住 ${fmtTs(state.selectedT)}（点击 K 线）`;
  });
}

function stepBy(delta) {
  const bars = state.timeline.bars;
  const idx = bisectLeq(bars, state.selectedT, "t");
  const next = Math.max(0, Math.min(bars.length - 1, idx + delta));
  state.selectedT = bars[next].t;
  state.pinned = true;
  renderForSelected();
  els.pinState.textContent = `已钉住 ${fmtTs(state.selectedT)}`;
  scrollToSelected();
}

function scrollToSelected() {
  // 如果选中的 bar 在可见区外，把它居中
  const bars = state.timeline.bars;
  const idx = bisectLeq(bars, state.selectedT, "t");
  if (idx < 0) return;
  const r = priceChart.timeScale().getVisibleLogicalRange();
  if (!r) return;
  const margin = (r.to - r.from) * 0.1;
  if (idx < r.from + margin || idx > r.to - margin) {
    const span = r.to - r.from;
    priceChart.timeScale().setVisibleLogicalRange({
      from: idx - span / 2,
      to: idx + span / 2,
    });
  }
}

function startPlay() {
  state.pinned = true;
  const bars = state.timeline.bars;
  const idx = bisectLeq(bars, state.selectedT, "t");
  state.playIndex = Math.max(0, idx);
  els.btnPlay.textContent = "⏸ 暂停";
  const tick = () => {
    if (state.playIndex >= bars.length - 1) {
      stopPlay();
      return;
    }
    state.playIndex++;
    state.selectedT = bars[state.playIndex].t;
    renderForSelected();
    scrollToSelected();
    els.pinState.textContent = `播放中 ${fmtTs(state.selectedT)}`;
  };
  const speed = Number(els.playSpeed.value) || 2;
  const intervalMs = Math.max(40, 600 / speed);
  state.playTimer = setInterval(tick, intervalMs);
}

function stopPlay() {
  if (state.playTimer) {
    clearInterval(state.playTimer);
    state.playTimer = null;
  }
  els.btnPlay.textContent = "▶ 播放";
}

// -------- 启动 --------
async function main() {
  try {
    makeCharts();
    bindControls();
    await loadAll();
    renderSummaryCards();
    await loadTimeline(state.interval);
    applyTimelineToCharts();
  } catch (e) {
    console.error(e);
    document.body.insertAdjacentHTML("afterbegin",
      `<div style="background:#5a1a1a;color:#fff;padding:12px;font-family:monospace">`
      + `加载数据失败：${String(e.message || e)}<br>`
      + `请确认已经运行 <code>py -3.12 update.py</code> 并通过本地 HTTP 服务访问此页面（直接 file:// 打开浏览器通常会拒绝读 ../data/export 下的 JSON）。`
      + `</div>`);
  }
}

main();
