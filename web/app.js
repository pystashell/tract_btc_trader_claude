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
  showMakerFills: false,    // 主图是否显示被动（maker）成交箭头；false 时只剩主动箭头。默认仅主动
  showAllOrders: false,     // 是否在主图一次性显示所有订单的完整生命周期（与选中时刻无关）
  orderLineMode: "lifecycle", // 挂单线形态：lifecycle=按真实生命周期分段 / full=选中时刻活跃单铺满整个横屏
  _allOrdersRendered: false, // 全部订单模式下挂单线是否已画（避免 hover 重画导致卡顿）
  // 活跃挂单表的多级排序：按优先级排列，dir: 1 升序 / -1 降序。
  // 默认：买单全部在上、卖单在下（side），组内价格从低到高 → 整表从最低买单排到最高卖单。
  ordersSort: [{ key: "side", dir: 1 }, { key: "price", dir: 1 }],
  // 技术指标：可加多条 EMA/SMA，每条自定义周期。{id, type:'ema'|'sma', period, color, widthLevel, series}
  indicators: [],
  // 挂单线颜色（买/卖可分别设置）
  orderColors: { buy: "#26a69a", sell: "#ef5350" },
  sidebarCollapsed: false,
  chartMaximized: false,   // K 线图是否全屏放大
  replayMode: "overlay",   // overlay = 叠加在完整图上(带十字线) / history = 真历史回放(隐藏未来)
  // 界面语言：优先读上次选择，否则跟随浏览器语言
  lang: (function () {
    try { const s = localStorage.getItem("paul_lang"); if (s === "zh" || s === "en") return s; } catch (e) { /* ignore */ }
    return (navigator.language || "en").toLowerCase().startsWith("zh") ? "zh" : "en";
  })(),
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
  ordersTableBody: document.querySelector("#orders-table tbody"),
  ordersEmpty: $("orders-empty"),
};

// -------- 国际化 i18n（中/英）--------
const I18N = {
  "app.title": { zh: "Paul Wei · Hyperliquid BTC 永续追踪", en: "Paul Wei · Hyperliquid BTC Perp Tracker" },
  "hdr.addr": { zh: "地址", en: "Address" },
  "hdr.addr_note": { zh: "（点击跳转 Hyperbot 官方页面对照）", en: "(click to open the Hyperbot page)" },
  "card.av": { zh: "账户价值", en: "Account Value" },
  "card.av_source": { zh: "账户总价值 (Hyperbot 口径)", en: "Account Total Value (Hyperbot)" },
  "card.pos": { zh: "当前 BTC 仓位", en: "Current BTC Position" },
  "card.lev": { zh: "实际杠杆 (有方向)", en: "Actual Leverage (signed)" },
  "card.lev_sub": { zh: "永续合约价值 / 账户价值", en: "Perp Value / Account Value" },
  "card.match": { zh: "校验", en: "Check" },
  "card.match_sub": { zh: "重建 vs 真实", en: "Reconstructed vs Actual" },
  "card.update": { zh: "数据更新", en: "Data Update" },
  "card.update_sub": { zh: "本地最近一次抓取", en: "Last local fetch" },
  "pos.long": { zh: "多头 Long", en: "Long" },
  "pos.short": { zh: "空头 Short", en: "Short" },
  "pos.flat": { zh: "无仓位", en: "No position" },
  "pos.perp": { zh: "永续合约价值", en: "perp value" },
  "check.ok": { zh: "✓ 一致", en: "✓ Match" },
  "check.bad": { zh: "✗ 不一致", en: "✗ Mismatch" },
  "check.detail": { zh: "重建 {0}　·　差 {1}", en: "Rebuilt {0} · diff {1}" },
  "update.ok": { zh: "上次成功", en: "Last: OK" },
  "update.bad": { zh: "上次失败 / 进行中", en: "Last: failed / running" },
  "ctl.interval": { zh: "K 线粒度", en: "Interval" },
  "ctl.replay": { zh: "回放", en: "Replay" },
  "ctl.play": { zh: "▶ 播放", en: "▶ Play" },
  "ctl.pause": { zh: "⏸ 暂停", en: "⏸ Pause" },
  "ctl.speed": { zh: "速度 (根/秒)", en: "Speed (bars/s)" },
  "ctl.show_future": { zh: "显示未来", en: "Show future" },
  "t.show_future": { zh: "开：叠加在完整图上，用十字虚线标出回放位置，能看到未来 K 线/挂单。"
                     + "关：真历史回放，只显示到回放点为止（隐藏未来）。回放中途也可随时切换。",
                     en: "On: overlay on the full chart with a dashed crosshair at the replay point; future candles/orders visible. "
                     + "Off: true history replay — show only up to the replay point (future hidden). Toggle anytime, even mid-replay." },
  "ctl.pin": { zh: "钉住时间", en: "Pin Time" },
  "ctl.pin_toggle": { zh: "📌 切换钉住", en: "📌 Toggle Pin" },
  "ctl.pin_latest": { zh: "最新", en: "Latest" },
  "ctl.order_lines": { zh: "主图挂单线", en: "Order Lines" },
  "ol.off": { zh: "关闭", en: "Off" },
  "ol.top4": { zh: "前 4 大", en: "Top 4" },
  "ol.top8": { zh: "前 8 大", en: "Top 8" },
  "ol.top16": { zh: "前 16 大", en: "Top 16" },
  "ol.all": { zh: "全部", en: "All" },
  "ctl.fills": { zh: "成交标记", en: "Fill Markers" },
  "fills.all": { zh: "全部", en: "All" },
  "fills.taker": { zh: "仅主动", en: "Taker only" },
  "ctl.show_all": { zh: "显示全部订单", en: "Show All Orders" },
  "ctl.order_mode": { zh: "挂单显示", en: "Order Style" },
  "om.lifecycle": { zh: "生命周期", en: "Lifecycle" },
  "om.full": { zh: "铺满横屏", en: "Full-width" },
  "t.om_lifecycle": { zh: "每条挂单只在它自己的生命周期（创建 → 成交/撤销）区间内画线",
                      en: "Draw each order only over its own lifecycle (created → filled/canceled)" },
  "t.om_full": { zh: "把选中时刻仍在挂的订单画成贯穿整个横屏的水平线（红=卖 绿=买，虚线=最终未成交 实线=成交）",
                 en: "Draw orders live at the selected time as full-width horizontal lines (red=sell, green=buy; dotted=unfilled, solid=filled)" },
  "seg.off": { zh: "关", en: "Off" },
  "seg.on": { zh: "开", en: "On" },
  "ctl.indicators": { zh: "技术指标", en: "Indicators" },
  "ind.add": { zh: "+ 添加", en: "+ Add" },
  "ind.edit_hint": { zh: "（在 K 线图左上角图例里改颜色/周期/类型、单独隐藏或删除）",
                     en: "(edit color/period/type, hide or delete each in the chart's top-left legend)" },
  "ind.thick": { zh: "粗", en: "W" },
  "ind.ed_type": { zh: "类型", en: "Type" },
  "ind.ed_period": { zh: "周期", en: "Period" },
  "ind.ed_color": { zh: "颜色", en: "Color" },
  "ind.ed_width": { zh: "粗细", en: "Width" },
  "ind.ed_done": { zh: "完成", en: "Done" },
  "ctl.order_colors": { zh: "挂单颜色", en: "Order Colors" },
  "side.buy": { zh: "买", en: "Buy" },
  "side.sell": { zh: "卖", en: "Sell" },
  "ctl.snapshot": { zh: "历史状态快照", en: "State snapshot" },
  "t.snapshot": { zh: "开=显示右侧历史状态快照栏（选中时间的状态 + 活跃挂单）；关=隐藏，让 K 线占满",
                  en: "On = show the right state-snapshot panel (state at selected time + active orders); Off = hide it to widen the chart" },
  "ctl.source_note": { zh: "成交量、K 线均来自 Hyperliquid 自家（不是 Binance / Coinbase）",
                       en: "Volume & candles are Hyperliquid's own (not Binance / Coinbase)" },
  "pin.off": { zh: "未钉住（跟随鼠标）", en: "Not pinned (follow cursor)" },
  "pin.pinned": { zh: "已钉住 {0}", en: "Pinned {0}" },
  "pin.pinned_click": { zh: "已钉住 {0}（点击 K 线）", en: "Pinned {0} (click chart)" },
  "pin.playing": { zh: "播放中 {0}", en: "Playing {0}" },
  "t.pin": { zh: "把当前 hover 的时间钉住，鼠标离开不跳走", en: "Pin the hovered time so it won't move when the cursor leaves" },
  "t.fills_all": { zh: "主动 + 被动成交全显示", en: "Show both taker and maker fills" },
  "t.fills_taker": { zh: "只显示主动 / taker 成交（Paul 主动发起的）", en: "Only taker fills (Paul-initiated)" },
  "t.show_off": { zh: "只显示选中时刻仍活跃的挂单（默认）", en: "Only orders live at the selected time (default)" },
  "t.show_on": { zh: "一次性把所有订单按各自完整生命周期画在图上，与选中时刻无关——用来总览 Paul 的全部挂单分布",
                 en: "Draw every order over its full lifecycle, independent of the selected time — an overview of all orders" },
  "t.ind_type": { zh: "EMA 更贴近近期价格；SMA 是简单均线", en: "EMA weights recent prices more; SMA is a simple average" },
  "t.ind_period": { zh: "周期（多少根 K 线）", en: "Period (number of candles)" },
  "t.ind_add": { zh: "添加一条均线，可加多条", en: "Add a moving average (multiple allowed)" },
  "t.color_buy": { zh: "买单线颜色", en: "Buy order line color" },
  "t.color_sell": { zh: "卖单线颜色", en: "Sell order line color" },
  "t.collapse": { zh: "隐藏右侧历史状态快照栏（选中时间的状态 + 活跃挂单），让 K 线更宽",
                  en: "Hide the right state-snapshot panel (state at selected time + active orders) to widen the chart" },
  "t.lang": { zh: "Switch to English", en: "切换到中文" },
  "t.ind_color": { zh: "线颜色", en: "Line color" },
  "t.ind_width": { zh: "线粗细（1 最细 → 5 最粗）", en: "Line width (1 thinnest → 5 thickest)" },
  "t.ind_remove": { zh: "移除", en: "Remove" },
  "t.ind_toggle": { zh: "点眼睛：隐藏 / 显示这条线", en: "Eye: hide / show this line" },
  "t.ind_edit": { zh: "点名字：修改这条指标（颜色 / 周期 / 类型 / 粗细）",
                  en: "Click the name to edit this indicator (color / period / type / width)" },
  "t.ind_del": { zh: "删除这条指标", en: "Delete this indicator" },
  "t.fullscreen": { zh: "全屏放大，只看 K 线（再点或按 Esc 退出）",
                    en: "Fullscreen — chart only (click again or press Esc to exit)" },
  "lang.btn": { zh: "EN", en: "中文" },
  "chart.price": { zh: "BTC 价格 · K 线", en: "BTC Price · Candles" },
  "chart.lev": { zh: "实际杠杆率 %（永续合约价值 / 账户价值，有方向）",
                 en: "Actual Leverage % (Perp Value / Account Value, signed)" },
  "panel.state": { zh: "选中时间的状态", en: "State at Selected Time" },
  "kv.time": { zh: "时间", en: "Time" },
  "kv.px": { zh: "BTC 价格 (close)", en: "BTC Price (close)" },
  "kv.pos": { zh: "当时 BTC 仓位", en: "BTC Position" },
  "kv.notional": { zh: "永续合约价值", en: "Perp Contract Value" },
  "kv.av": { zh: "账户价值", en: "Account Value" },
  "kv.lev": { zh: "实际杠杆 (有方向)", en: "Actual Leverage (signed)" },
  "panel.orders": { zh: "活跃挂单（选中时刻在挂的所有订单）", en: "Active Orders (all live at the selected time)" },
  "orders.meta": {
    zh: '百分比 = 订单名义价值 / 当时账户价值。每条订单的最终命运用 badge 标出：'
      + '<span class="badge ok">成交</span> / <span class="badge open">在挂</span> / <span class="badge canceled">撤销</span>。'
      + '点表头排序（再点切换升/降，新点的列成为第一优先级，其余降为次级）。默认：买单在上、卖单在下，组内价格从低到高。 ',
    en: 'Percent = order notional / account value at that time. Each order\'s final fate is tagged: '
      + '<span class="badge ok">Filled</span> / <span class="badge open">Open</span> / <span class="badge canceled">Canceled</span>. '
      + 'Click a header to sort (click again to flip; the clicked column becomes the primary key, the rest demote). Default: buys on top, sells below, price low→high. ' },
  "orders.reset": { zh: "恢复默认排序", en: "Reset sort" },
  "th.side": { zh: "方向", en: "Side" },
  "th.price": { zh: "价格", en: "Price" },
  "th.sz": { zh: "BTC 数量", en: "BTC Size" },
  "th.notional": { zh: "名义价值", en: "Notional" },
  "th.pct": { zh: "占账户%", en: "% of Acct" },
  "th.type": { zh: "类型", en: "Type" },
  "th.created": { zh: "创建", en: "Created" },
  "orders.empty": { zh: "该时刻没有活跃挂单。", en: "No active orders at this time." },
  "tbl.buy": { zh: "买 Long", en: "Buy" },
  "tbl.sell": { zh: "卖 Short", en: "Sell" },
  "badge.filled": { zh: "成交", en: "Filled" },
  "badge.open": { zh: "在挂", en: "Open" },
  "badge.canceled": { zh: "撤销", en: "Canceled" },
  "mark.taker": { zh: "（主动）", en: " (taker)" },
  "footer.summary": { zh: "关于数据 / 指标说明", en: "About the data / metrics" },
  "footer.li1": {
    zh: '<strong>K 线</strong>来自 Hyperliquid <code>candleSnapshot</code>。<strong>成交量是 Hyperliquid 自家</strong>，不是 Binance / Coinbase。',
    en: '<strong>Candles</strong> come from Hyperliquid <code>candleSnapshot</code>. <strong>Volume is Hyperliquid\'s own</strong>, not Binance / Coinbase.' },
  "footer.li2": {
    zh: '<strong>当前 BTC 仓位 / 永续合约价值</strong>来自 Hyperliquid <code>clearinghouseState</code>，是交易所侧的权威值。<strong>账户价值</strong>采用 <code>portfolio</code> 的"账户总价值"(Account Total Value，与 Hyperbot 官方页面口径一致)，<em>不是</em>仅永续账户那 ~3 万的保证金权益。',
    en: '<strong>Current BTC position / perp value</strong> come from Hyperliquid <code>clearinghouseState</code> (exchange-authoritative). <strong>Account value</strong> uses <code>portfolio</code> "Account Total Value" (same basis as Hyperbot), <em>not</em> just the ~30k perp-account margin.' },
  "footer.li3": {
    zh: '<strong>重建仓位</strong>由 <code>fills</code> 逐条累加而来：<code>B</code> 增加多头、<code>A</code> 减少多头（变空头）。如顶部"校验"不匹配，请检查 fills 方向解析或增量是否完整。',
    en: '<strong>Reconstructed position</strong> is summed from <code>fills</code>: <code>B</code> adds long, <code>A</code> reduces (goes short). If "Check" up top mismatches, verify fill-side parsing or completeness.' },
  "footer.li4": {
    zh: '<strong>实际杠杆（带方向）</strong>= 永续合约价值 / 账户价值（账户总价值，Hyperbot 口径）。正值为多头，负值为空头。例：-0.0263 意为 2.63% Short。',
    en: '<strong>Actual leverage (signed)</strong> = perp value / account value (Account Total Value, Hyperbot basis). Positive = long, negative = short. e.g. -0.0263 = 2.63% short.' },
  "footer.li5": {
    zh: '<strong>挂单对账户的百分比</strong>= 订单 <em>名义价值</em> / 当时账户价值，<em>不是</em>占当前持仓的百分比。',
    en: '<strong>Order % of account</strong> = order <em>notional</em> / account value at that time, <em>not</em> a percent of the current position.' },
  "footer.li6": {
    zh: '箭头标记：<span style="color:#26a69a">▲ 暗绿</span> = 被动买，<span style="color:#ffa726">▲ 亮橙</span> = <strong>主动买</strong>（taker buy）；<span style="color:#ef5350">▼ 暗红</span> = 被动卖，<span style="color:#ec407a">▼ 洋红</span> = <strong>主动卖</strong>（taker sell）。主动 = <code>crossed=true</code>，通常对应 market-like 行为。',
    en: 'Arrows: <span style="color:#26a69a">▲ dark green</span> = maker buy, <span style="color:#ffa726">▲ orange</span> = <strong>taker buy</strong>; <span style="color:#ef5350">▼ dark red</span> = maker sell, <span style="color:#ec407a">▼ magenta</span> = <strong>taker sell</strong>. Taker = <code>crossed=true</code>, usually market-like.' },
  "footer.li7": {
    zh: '有些订单只能从 <code>historicalOrders</code> 的 2000 条滚动窗口里取到，<strong>更早的订单可能已经掉出窗口</strong>——数据库会把抓到过的所有订单永久保存。配合 weekly snapshot + fills 反推，Paul 的订单 lifecycle 实际是完整记录的。',
    en: 'Some orders only come from the 2000-record rolling window of <code>historicalOrders</code>; <strong>older ones may have scrolled out</strong> — but the DB keeps every order it ever saw permanently. With weekly snapshots + fill back-derivation, Paul\'s order lifecycle is recorded in full.' },
  "footer.li8": {
    zh: '主图挂单线有两种<strong>显示形态</strong>（控制栏"挂单显示"切换）：<strong>生命周期</strong> = 每条挂单按自己真实的区间（创建 → 成交/撤销）分段画，仍在挂的延伸到"现在"；<strong>铺满横屏</strong> = 把十字线/选中时刻仍在挂的订单画成贯穿整个横屏的水平线（十字线移到哪，就显示当时在挂的单）。两种形态线型一致：<strong>实线</strong> = 最终成交，<strong>点线</strong> = 未成交（撤销或仍在挂）。',
    en: 'Main-chart order lines have two <strong>display styles</strong> (the "Order Style" toggle): <strong>Lifecycle</strong> draws each order over its real interval (created → filled/canceled), still-open ones reaching "now"; <strong>Full-width</strong> draws orders live at the crosshair/selected time as horizontal lines spanning the whole chart (move the crosshair to see what was live then). In both, <strong>solid</strong> = eventually filled, <strong>dotted</strong> = unfilled (canceled or still open).' },
  "footer.li9": {
    zh: '<strong>显示全部订单</strong>（仅"生命周期"形态下可用，默认关）：开启后把所有订单一次性按各自真实生命周期画在主图上，与选中时刻无关，用来总览 Paul 历史上全部挂单的价格分布；切到"铺满横屏"形态时此开关会置灰。',
    en: '<strong>Show All Orders</strong> (only available in the "Lifecycle" style, off by default): draws every order at once over its real lifecycle regardless of the selected time — an overview of Paul\'s whole order price distribution; it is greyed out in the "Full-width" style.' },
  "err.load": {
    zh: '加载数据失败：{0}<br>请确认已经运行 <code>py -3.12 update.py</code> 并通过本地 HTTP 服务访问此页面（直接 file:// 打开浏览器通常会拒绝读 ../data/export 下的 JSON）。',
    en: 'Failed to load data: {0}<br>Make sure <code>py -3.12 update.py</code> has run and this page is served over local HTTP (opening via file:// usually blocks reading the JSON under ../data/export).' },
};

function t(key, ...args) {
  const e = I18N[key];
  let s = e ? (e[state.lang] || e.zh || key) : key;
  args.forEach((a, i) => { s = s.split("{" + i + "}").join(a); });
  return s;
}

// 把所有带 data-i18n* 标注的静态元素刷成当前语言
function applyStaticI18n(root = document) {
  root.querySelectorAll("[data-i18n]").forEach(el => { el.textContent = t(el.dataset.i18n); });
  root.querySelectorAll("[data-i18n-html]").forEach(el => { el.innerHTML = t(el.dataset.i18nHtml); });
  root.querySelectorAll("[data-i18n-title]").forEach(el => { el.title = t(el.dataset.i18nTitle); });
  root.querySelectorAll("[data-i18n-label]").forEach(el => { el.dataset.label = t(el.dataset.i18nLabel); });
}

// 右侧"钉住状态"文案（随语言/播放/钉住状态刷新）
function renderPinState() {
  if (!els.pinState) return;
  if (state.playTimer) els.pinState.textContent = t("pin.playing", fmtTs(state.selectedT));
  else if (state.pinned) els.pinState.textContent = t("pin.pinned", fmtTs(state.selectedT));
  else els.pinState.textContent = t("pin.off");
}

// 整体应用语言：静态文案 + 动态区域全部重刷
function applyLang() {
  document.documentElement.lang = state.lang === "en" ? "en" : "zh-CN";
  document.title = t("app.title");
  applyStaticI18n();
  updateOrdersSortIndicators();                         // 表头用翻译后的 data-label 重建
  if (state.summary) renderSummaryCards();              // 卡片动态文案
  if (state.timeline && state.timeline.bars) {          // 挂单表 + 成交箭头文案
    renderForSelected();
    applyFillMarkers();
  }
  renderIndicatorLegend();                              // 指标图例的 tooltip 文案随语言刷新
  renderPinState();
  if (els.btnPlay) els.btnPlay.textContent = state.playTimer ? t("ctl.pause") : t("ctl.play");
  const lb = $("btn-lang");
  if (lb) lb.textContent = t("lang.btn");
}

function setLang(lang) {
  state.lang = (lang === "en") ? "en" : "zh";
  try { localStorage.setItem("paul_lang", state.lang); } catch (e) { /* ignore */ }
  applyLang();
}

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

// 紧凑时间（订单表"创建"列用，省得撑破窄侧栏）：YY-MM-DD HH:MM，分两行显示
function fmtTsShort(ms) {
  if (!ms && ms !== 0) return "—";
  const d = new Date(ms);
  const p = n => String(n).padStart(2, "0");
  return `${String(d.getUTCFullYear()).slice(2)}-${p(d.getUTCMonth() + 1)}-${p(d.getUTCDate())}`
    + `<br>${p(d.getUTCHours())}:${p(d.getUTCMinutes())}`;
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

// 杠杆文字颜色分档（与下方杠杆副图一致）：<0 空头=红；0–100%=绿；>100%=黄。
// 返回 CSS class 名（.v.green / .v.red / .v.yellow、.value.green/...）。
function levColor(lev) {
  if (lev === null || lev === undefined || Number.isNaN(lev)) return "";
  if (lev < -0.0005) return "red";    // 空头
  if (lev > 1) return "yellow";       // 杠杆 > 100%
  if (lev > 0.0005) return "green";   // 0–100%
  return "";                           // ≈ 0：无仓位
}

// 杠杆副图每根柱子的颜色（lightweight-charts 需要 hex；取值同 --red/--green/--yellow）
function levBarColor(lev) {
  if (lev < -0.000001) return "#ef5350";  // 空头：红
  if (lev > 1) return "#d4ac00";          // > 100%：黄
  return "#26a69a";                        // 0–100%（含 0）：绿
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
  // 账户价值用"账户总价值"（= Hyperbot Account Total Value），不是仅永续账户权益
  els.cardAv.textContent = fmtUsd(ch.account_total_value);
  els.cardAvSource.textContent = `${t("card.av_source")} · ${fmtTs(ch.account_total_as_of)}`;

  const pos = ch.btc_position || {};
  const szi = pos.szi;
  els.cardPos.textContent = (szi === null || szi === undefined)
    ? "—" : `${fmtBtc(szi)} BTC`;
  els.cardPos.classList.toggle("green", szi > 0);
  els.cardPos.classList.toggle("red", szi < 0);
  const side = szi > 0 ? t("pos.long") : szi < 0 ? t("pos.short") : t("pos.flat");
  // 同时显示永续合约价值（杠杆的分子），与右侧"选中时间的状态"口径一致
  const pv = ch.position_value != null ? `· ${t("pos.perp")} ${fmtUsd(ch.position_value, 0)}` : "";
  els.cardPosSide.textContent = `${side} ${pv}`;

  const lev = ch.btc_leverage_signed;
  els.cardLev.textContent = fmtLev(lev);
  els.cardLev.classList.remove("green", "red", "yellow");
  const levCls = levColor(lev);
  if (levCls) els.cardLev.classList.add(levCls);

  const m = s.reconstruction;
  els.cardMatch.classList.remove("green", "red");
  if (m.matches_clearinghouse) {
    els.cardMatch.textContent = t("check.ok");
    els.cardMatch.classList.add("green");
  } else {
    els.cardMatch.textContent = t("check.bad");
    els.cardMatch.classList.add("red");
  }
  els.cardMatchDetail.textContent =
    t("check.detail", fmtBtc(m.btc_size), fmtBtc(m.diff_vs_clearinghouse));

  const lu = s.latest_update;
  els.cardUpdate.textContent = fmtTs(lu.finished_at || lu.started_at);
  els.cardUpdateDetail.textContent = lu.success ? t("update.ok") : t("update.bad");
}

// -------- 图表创建 --------
let priceChart, priceSeries, volumeSeries;
let levChart, levSeries;
let crosshairLocked = false;
let orderLinesPrimitive = null;   // "显示全部订单"模式用一个 canvas primitive 一次性画所有线段

function makeCharts() {
  const common = {
    autoSize: true,   // 用 ResizeObserver 自动贴合容器大小：K 线图用 flex 撑满视口，随窗口/布局变化自适应
    layout: {
      background: { color: "#161b22" },
      textColor: "#cdd9e5",
      attributionLogo: false,   // 去掉右下角 TradingView 水印图标
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
  // "显示全部订单"模式的挂单线：用一个自定义 primitive 在 canvas 上一次性画，
  // 避免为上千个订单各建一个 LineSeries（那样切粒度时 setData×N 会卡好几秒）。
  orderLinesPrimitive = makeOrderLinesPrimitive();
  priceSeries.attachPrimitive(orderLinesPrimitive);
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
  // 杠杆副图用直方图：可以按"每根柱子的杠杆值"分三档上色
  //（baseline 只能围绕一条基线分两色，无法表达 <0 / 0–100% / >100% 三档）。
  // 每根柱子的颜色在 applyTimelineToCharts 里用 levBarColor 单独给。
  levSeries = levChart.addHistogramSeries({
    base: 0,   // 从 0 轴起算：>0 向上、<0 向下
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
  const levData = bars.map(b => {
    const lev = (b.lev !== null && b.lev !== undefined) ? b.lev : 0;
    return { time: Math.floor(b.t / 1000), value: lev, color: levBarColor(lev) };
  });

  priceSeries.setData(candleData);
  volumeSeries.setData(volData);
  levSeries.setData(levData);
  refreshIndicatorData();   // 粒度变了 → 用新 bars 重算所有技术指标
  // 杠杆副图按可见区域自动缩放（默认行为，显式打开以防被覆盖）
  levChart.priceScale("right").applyOptions({ autoScale: true });
  priceChart.priceScale("right").applyOptions({ autoScale: true });

  applyFillMarkers();

  // 切了粒度 → K 线坐标系变了，全部订单模式需要按新粒度重画一次
  state._allOrdersRendered = false;
  state._allOrderSegments = null;   // 端点贴 K 线，粒度变了要重算
  // 默认选中最后一根
  state.selectedT = bars[bars.length - 1].t;
  renderForSelected();
  // 默认显示最近 ~150 根
  const showN = Math.min(150, bars.length);
  const lastIdx = bars.length - 1;
  priceChart.timeScale().setVisibleLogicalRange({ from: lastIdx - showN, to: lastIdx + 2 });
}

// -------- 技术指标（EMA / SMA，主图叠加，可多条、各自自定义周期）--------
// 颜色池：避开 K 线绿/红、fill 箭头橙/洋红，尽量互相可区分。
const IND_COLORS = ["#ffd54f", "#4fc3f7", "#ba68c8", "#81c784", "#f06292",
                    "#58a6ff", "#ffb74d", "#4db6ac", "#e57373", "#9575cd"];
let _indColorIdx = 0;
// 线粗档位 1–5 → 实际线宽（档 1 比过去的最细还细一级；不做 5 档以上，太粗）
const IND_WIDTHS = [0.5, 1, 1.5, 2, 3];
const indWidthPx = (level) => IND_WIDTHS[Math.min(5, Math.max(1, level || 2)) - 1];

// 从 bars 的收盘价算 MA。返回 [{time, value}]，前 period-1 根没有值（不输出）。
function computeMA(bars, type, period) {
  const out = [];
  if (!bars || !bars.length || !(period >= 1)) return out;
  if (type === "sma") {
    let sum = 0;
    for (let i = 0; i < bars.length; i++) {
      sum += bars[i].c;
      if (i >= period) sum -= bars[i - period].c;
      if (i >= period - 1) out.push({ time: Math.floor(bars[i].t / 1000), value: sum / period });
    }
  } else { // ema：先用前 period 根的 SMA 作种子，再按 k=2/(period+1) 递推
    const k = 2 / (period + 1);
    let ema = null, seed = 0;
    for (let i = 0; i < bars.length; i++) {
      if (i < period - 1) { seed += bars[i].c; continue; }
      if (i === period - 1) { seed += bars[i].c; ema = seed / period; }
      else { ema = bars[i].c * k + ema * (1 - k); }
      out.push({ time: Math.floor(bars[i].t / 1000), value: ema });
    }
  }
  return out;
}

function makeIndicatorSeries(ind) {
  ind.series = priceChart.addLineSeries({
    color: ind.color,
    lineWidth: indWidthPx(ind.widthLevel),
    visible: !ind.hidden,
    priceLineVisible: false,
    lastValueVisible: !ind.hidden,   // 价格轴上显示该线最新值（带颜色，便于分辨哪条是哪条）
    crosshairMarkerVisible: false,
    title: `${ind.type.toUpperCase()}${ind.period}`,
  });
}

function refreshIndicatorData() {
  const bars = state.timeline && state.timeline.bars;
  if (!bars) return;
  for (const ind of state.indicators) {
    if (!ind.series) makeIndicatorSeries(ind);
    ind.series.setData(computeMA(bars, ind.type, ind.period));
  }
}

function addIndicator(type, period) {
  const ind = { id: _indColorIdx + "-" + period + "-" + type + "-" + state.indicators.length,
                type, period, widthLevel: 2, hidden: false,
                color: IND_COLORS[_indColorIdx++ % IND_COLORS.length], series: null };
  state.indicators.push(ind);
  makeIndicatorSeries(ind);
  if (state.timeline && state.timeline.bars) ind.series.setData(computeMA(state.timeline.bars, type, period));
  renderIndicatorLegend();
}

function removeIndicator(id) {
  const i = state.indicators.findIndex(x => x.id === id);
  if (i === -1) return;
  if (_indEditor && _indEditor._indId === id) closeIndicatorEditor();   // 正在编辑它 → 关掉小框
  if (state.indicators[i].series) priceChart.removeSeries(state.indicators[i].series);
  state.indicators.splice(i, 1);
  renderIndicatorLegend();
}

// 改某条指标的周期（就地重算，复用同一条 series）
function setIndicatorPeriod(id, period) {
  const ind = state.indicators.find(x => x.id === id);
  if (!ind || !(period >= 1) || period > 1000) return;
  ind.period = period;
  ind.series.applyOptions({ title: `${ind.type.toUpperCase()}${period}` });
  if (state.timeline && state.timeline.bars) ind.series.setData(computeMA(state.timeline.bars, ind.type, period));
  renderIndicatorLegend();
}
// 改颜色 / 线粗细（只改样式，不必重算数据）
function setIndicatorColor(id, color) {
  const ind = state.indicators.find(x => x.id === id);
  if (!ind || !color) return;
  ind.color = color;
  ind.series.applyOptions({ color });   // 颜色输入框本身就是那个色块，不用重建 chip
  renderIndicatorLegend();
}
function setIndicatorWidthLevel(id, level) {
  const ind = state.indicators.find(x => x.id === id);
  if (!ind || !(level >= 1) || level > 5) return;
  ind.widthLevel = level;
  ind.series.applyOptions({ lineWidth: indWidthPx(level) });
}
// 单独隐藏/显示某条指标线（图上左侧图例的眼睛）
function toggleIndicatorVisible(id) {
  const ind = state.indicators.find(x => x.id === id);
  if (!ind || !ind.series) return;
  ind.hidden = !ind.hidden;
  ind.series.applyOptions({ visible: !ind.hidden, lastValueVisible: !ind.hidden });
  renderIndicatorLegend();
}

// 改某条指标的类型（EMA↔SMA，需重算数据）
function setIndicatorType(id, type) {
  const ind = state.indicators.find(x => x.id === id);
  if (!ind || (type !== "ema" && type !== "sma")) return;
  ind.type = type;
  ind.series.applyOptions({ title: `${type.toUpperCase()}${ind.period}` });
  if (state.timeline && state.timeline.bars) ind.series.setData(computeMA(state.timeline.bars, type, ind.period));
  renderIndicatorLegend();
}

// 图左上角的指标图例（仿 TradingView），是指标的唯一交互入口：
//   眼睛 = 单独隐藏/显示；名字 = 点开小框改设置；× = 单独删除。
function renderIndicatorLegend() {
  const el = $("chart-legend");
  if (!el) return;
  el.innerHTML = "";
  for (const ind of state.indicators) {
    const row = document.createElement("div");
    row.className = "legend-item" + (ind.hidden ? " off" : "");
    const eye = document.createElement("button");
    eye.className = "legend-eye";
    eye.title = t("t.ind_toggle");
    eye.textContent = ind.hidden ? "🚫" : "👁";
    eye.addEventListener("click", (e) => { e.stopPropagation(); toggleIndicatorVisible(ind.id); });
    const name = document.createElement("span");
    name.className = "legend-name";
    name.style.color = ind.color;
    name.textContent = `${ind.type.toUpperCase()} ${ind.period}`;
    name.title = t("t.ind_edit");
    name.addEventListener("click", (e) => { e.stopPropagation(); openIndicatorEditor(ind, name); });
    const del = document.createElement("button");
    del.className = "legend-x";
    del.title = t("t.ind_del");
    del.textContent = "×";
    del.addEventListener("click", (e) => { e.stopPropagation(); removeIndicator(ind.id); });
    row.appendChild(eye);
    row.appendChild(name);
    row.appendChild(del);
    el.appendChild(row);
  }
}

// 点击图例名字弹出的小编辑框（颜色 / 周期 / 类型 / 粗细）
let _indEditor = null;
function _indEditorOutside(e) {
  if (_indEditor && !_indEditor.contains(e.target) && !e.target.classList.contains("legend-name")) {
    closeIndicatorEditor();
  }
}
function closeIndicatorEditor() {
  if (_indEditor) { _indEditor.remove(); _indEditor = null; }
  document.removeEventListener("mousedown", _indEditorOutside, true);
}
function openIndicatorEditor(ind, anchorEl) {
  const wrap = $("chart-price").closest(".chart-wrap");
  if (!wrap) return;
  // 再点同一条名字 → 收起（切换）
  if (_indEditor && _indEditor._indId === ind.id) { closeIndicatorEditor(); return; }
  closeIndicatorEditor();
  const pop = document.createElement("div");
  pop.className = "ind-editor";
  pop._indId = ind.id;
  pop.innerHTML =
      `<div class="ind-ed-row"><span>${t("ind.ed_type")}</span>`
    + `<select class="ind-ed-type"><option value="ema"${ind.type === "ema" ? " selected" : ""}>EMA</option>`
    + `<option value="sma"${ind.type === "sma" ? " selected" : ""}>SMA</option></select></div>`
    + `<div class="ind-ed-row"><span>${t("ind.ed_period")}</span>`
    + `<input class="ind-ed-period" type="number" min="1" max="1000" value="${ind.period}"></div>`
    + `<div class="ind-ed-row"><span>${t("ind.ed_color")}</span>`
    + `<input class="ind-ed-color" type="color" value="${ind.color}"></div>`
    + `<div class="ind-ed-row"><span>${t("ind.ed_width")}</span>`
    + `<select class="ind-ed-width">`
    +   [1, 2, 3, 4, 5].map(l => `<option value="${l}"${(ind.widthLevel || 2) === l ? " selected" : ""}>${l}</option>`).join("")
    + `</select></div>`
    + `<button class="ind-ed-done">${t("ind.ed_done")}</button>`;
  wrap.appendChild(pop);
  // 贴在被点名字的正下方
  const wr = wrap.getBoundingClientRect(), ar = anchorEl.getBoundingClientRect();
  pop.style.left = Math.round(ar.left - wr.left) + "px";
  pop.style.top = Math.round(ar.bottom - wr.top + 4) + "px";
  pop.querySelector(".ind-ed-type").addEventListener("change", e => setIndicatorType(ind.id, e.target.value));
  pop.querySelector(".ind-ed-period").addEventListener("change", e => setIndicatorPeriod(ind.id, parseInt(e.target.value, 10)));
  pop.querySelector(".ind-ed-color").addEventListener("input", e => setIndicatorColor(ind.id, e.target.value));
  pop.querySelector(".ind-ed-width").addEventListener("change", e => setIndicatorWidthLevel(ind.id, parseInt(e.target.value, 10)));
  pop.querySelector(".ind-ed-done").addEventListener("click", closeIndicatorEditor);
  _indEditor = pop;
  // 延一拍再挂 outside 监听，避免这次点击立刻把自己关掉
  setTimeout(() => document.addEventListener("mousedown", _indEditorOutside, true), 0);
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
                     shape: "arrowUp", text: `B ${pct(takerBuyN)}${t("mark.taker")}` });
    }
    if (state.showMakerFills && makerSellN > 0) {
      markers.push({ time: tSec, position: "aboveBar", color: "#ef5350",
                     shape: "arrowDown", text: `S ${pct(makerSellN)}` });
    }
    if (takerSellN > 0) {
      markers.push({ time: tSec, position: "aboveBar", color: "#ec407a",
                     shape: "arrowDown", text: `S ${pct(takerSellN)}${t("mark.taker")}` });
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

  // 主图上挂单的水平线（内部按"挂单显示"形态选择 primitive / per-order 路径）。
  // 铺满 / 仅活跃形态都随选中时刻变化，每次都重画；primitive 路径本身很便宜。
  drawActiveOrderLines(bar);
  // 表格
  renderOrdersTable(bar);
}


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

// ---- "显示全部订单"模式：一个 canvas primitive 画所有订单线段 ----
// 4 组（画的时候每组只 stroke 一次）：0 买-成交(实) / 1 买-未成交(点) /
// 2 卖-成交(实) / 3 卖-未成交(点)。颜色在画的时候按 state.orderColors 取（买/卖可自定义）。
const ORDERLINE_GROUPS = [
  { side: "buy",  dash: false },
  { side: "buy",  dash: true },
  { side: "sell", dash: false },
  { side: "sell", dash: true },
];

// 把所有订单预算成线段，返回 4 个数组（每元素 {t1, t2, open, px}）。
// 注意：lightweight-charts 的 timeToCoordinate 只认"落在 K 线时间点上"的时间，
// 非 K 线时刻会返回 null，所以线段端点必须【贴到 K 线时间】——因此结果与粒度有关，
// 切粒度时要重算（很便宜，纯二分查找）。未成交单 t2=0 表示画到图右端。
function computeAllOrderSegments() {
  const bars = state.timeline && state.timeline.bars;
  const groups = [[], [], [], []];
  if (!bars || !bars.length) return groups;
  const candleTimes = bars.map(b => Math.floor(b.t / 1000));
  for (const o of state.orders) {
    const px = o.is_trigger ? o.trigger_px : o.limit_px;
    if (!px) continue;
    const startCandle = candleAtOrBefore(candleTimes, Math.floor(o.timestamp / 1000));
    if (startCandle === null) continue;                        // 早于首根 K 线，跳过
    const filled = o.status === "filled";
    const gi = (o.side === "B" ? 0 : 2) + (filled ? 0 : 1);
    if (o.status === "open") {
      groups[gi].push({ t1: startCandle, t2: 0, open: true, px });   // 仍在挂 → 画到右端
    } else {
      let endCandle = candleAtOrBefore(candleTimes, Math.floor(o.status_timestamp / 1000));
      if (endCandle === null || endCandle < startCandle) endCandle = startCandle;
      groups[gi].push({ t1: startCandle, t2: endCandle, open: false, px });
    }
  }
  return groups;
}

// "现在"是哪一秒：默认 = 最后一根 K 线（不把线延伸进右侧空白未来）；
// history 回放模式下钉住时 = 回放到的那根（截止到当时）。
function replayNowSec() {
  const bars = state.timeline && state.timeline.bars;
  if (!bars || !bars.length) return null;
  if (state.replayMode === "history" && state.pinned && state.selectedT != null) {
    return Math.floor(state.selectedT / 1000);
  }
  return Math.floor(bars[bars.length - 1].t / 1000);
}

function makeOrderLinesPrimitive() {
  const st = { groups: [[], [], [], []], chart: null, series: null, requestUpdate: null };
  const renderer = {
    draw(target) {
      const chart = st.chart, series = st.series;
      if (!chart || !series) return;
      const ts = chart.timeScale();
      const nowSec = replayNowSec();   // 线只画到"现在"，不延伸进未来空白（隐藏未来时截到回放点）
      // 十字线/选中时刻：铺满模式下决定"当时有哪些单在挂"
      const refSec = (state.selectedT != null) ? Math.floor(state.selectedT / 1000) : nowSec;
      target.useBitmapCoordinateSpace(scope => {
        const ctx = scope.context;
        const hpr = scope.horizontalPixelRatio, vpr = scope.verticalPixelRatio;
        const rightEdge = scope.mediaSize.width * hpr;
        ctx.save();
        ctx.lineWidth = Math.max(1, Math.round(2 * vpr));
        // "铺满横屏"形态：把选中时刻仍挂着的单画成【贯穿全图的整条横线】。
        // "生命周期"形态：按订单真实区间分段画（此分支用于"显示全部订单"）。
        const fullLineMode = (state.orderLineMode === "full");
        for (let g = 0; g < 4; g++) {
          const arr = st.groups[g];
          if (!arr || !arr.length) continue;
          ctx.strokeStyle = state.orderColors[ORDERLINE_GROUPS[g].side];
          ctx.setLineDash(ORDERLINE_GROUPS[g].dash ? [Math.round(1.5 * hpr), Math.round(3 * hpr)] : []);
          ctx.beginPath();
          for (const seg of arr) {
            if (nowSec != null && seg.t1 > nowSec) continue;   // 那时还没下这个单 → 不画
            const y = series.priceToCoordinate(seg.px);
            if (y == null) continue;
            if (fullLineMode) {
              // 只画"十字线所在时刻仍挂着"的单：创建 ≤ ref 且 (未了结 或 了结在 ref 之后)
              const activeAtRef = (refSec == null) || ((seg.t1 <= refSec) && (seg.open || seg.t2 > refSec));
              if (!activeAtRef) continue;
              ctx.moveTo(0, y * vpr);
              ctx.lineTo(rightEdge, y * vpr);
              continue;
            }
            const x1 = ts.timeToCoordinate(seg.t1);
            if (x1 == null) continue;
            // 右端截止到"现在"：未成交单画到 now；已了结单画到 min(结束, now)
            let endSec = seg.open ? nowSec : seg.t2;
            if (nowSec != null && (endSec == null || endSec > nowSec)) endSec = nowSec;
            const x2 = (endSec != null) ? ts.timeToCoordinate(endSec) : scope.mediaSize.width;
            if (x2 == null) continue;
            let bx1 = x1 * hpr, bx2 = x2 * hpr, by = y * vpr;
            if (bx2 - bx1 < 2 * hpr) bx2 = bx1 + 2 * hpr;      // 极短生命周期也保证可见
            if (bx2 > rightEdge) bx2 = rightEdge;              // 不越过绘图区右缘
            ctx.moveTo(bx1, by);
            ctx.lineTo(bx2, by);
          }
          ctx.stroke();
        }
        ctx.restore();
      });
    },
  };
  const paneView = { renderer: () => renderer, zOrder: () => "top" };
  return {
    attached(p) { st.chart = p.chart; st.series = p.series; st.requestUpdate = p.requestUpdate; },
    detached() { st.chart = null; st.series = null; },
    paneViews() { return [paneView]; },
    updateAllViews() {},
    setGroups(groups) { st.groups = groups; if (st.requestUpdate) st.requestUpdate(); },
  };
}

// 主图挂单线的总入口：按"挂单显示"形态 + "显示全部订单"选择渲染路径。
//   · 铺满横屏(full)         → canvas primitive，选中时刻活跃单画成贯穿全图的横线
//   · 生命周期 + 显示全部订单 → canvas primitive，所有订单各按真实区间分段
//   · 生命周期 + 仅活跃       → per-order LineSeries（支持"前 N 条"限制、随图平移）
function drawActiveOrderLines(bar) {
  const fullMode = state.orderLineMode === "full";
  // "关闭"主图挂单线：非全部订单 / 非铺满模式下直接清空不画
  //（全部订单模式历史上无视这个开关，铺满模式的语义就是要显示，故都不受"关闭"影响）
  if (state.maxOrderLines === 0 && !state.showAllOrders && !fullMode) {
    clearPrimitiveLines();
    clearPerOrderPool();
    return;
  }
  if (fullMode || state.showAllOrders) {
    if (!state._allOrderSegments) state._allOrderSegments = computeAllOrderSegments();
    orderLinesPrimitive.setGroups(state._allOrderSegments);
    state._primitiveActive = true;
    clearPerOrderPool();   // 两条路径不叠加
    return;
  }
  clearPrimitiveLines();
  drawLifecyclePerOrder(bar);
}

// 清空 primitive 上的挂单线（仅在它之前有内容时才清，避免多触发一次重绘）
function clearPrimitiveLines() {
  if (state._primitiveActive && orderLinesPrimitive) {
    orderLinesPrimitive.setGroups([[], [], [], []]);
    state._primitiveActive = false;
  }
}
// 清空 per-order LineSeries pool
function clearPerOrderPool() {
  while (state.activeOrderSeries.length) {
    try { priceChart.removeSeries(state.activeOrderSeries.pop()); } catch (e) { /* ignore */ }
  }
}

// 生命周期 + 仅活跃：把"选中时刻仍活跃"的挂单，各自按真实生命周期（创建 → 成交/撤销）分段画，
// 右端截到"现在"（隐藏未来时截到回放点）。受"主图挂单线 前 N 条"限制。
function drawLifecyclePerOrder(bar) {
  const tMs = bar.T || bar.t;
  const withPct = activeOrdersAt(tMs).map(o => {
    const px = o.is_trigger ? o.trigger_px : o.limit_px;
    const sz = o.orig_sz || o.sz;  // 用原始下单量：成交/撤销单的 sz(剩余量)=0，否则名义价值会算成 0
    const notional = (px || 0) * (sz || 0);
    return { ...o, _px: px, _notional: notional };
  }).sort((a, b) => (b._notional || 0) - (a._notional || 0));

  const draws = [];
  if (state.maxOrderLines > 0) {
    const topN = withPct.slice(0, state.maxOrderLines);
    const bars = state.timeline.bars;
    if (state._candleTimesCache !== bars) {
      state._candleTimes = bars.map(b => Math.floor(b.t / 1000));
      state._candleTimesCache = bars;
    }
    const candleTimes = state._candleTimes;
    const chartEnd = candleTimes[candleTimes.length - 1];
    const nowSec = replayNowSec();
    const nowClip = (nowSec != null) ? Math.min(nowSec, chartEnd) : chartEnd;
    for (const o of topN) {
      if (!o._px) continue;
      const segStart = Math.floor(o.timestamp / 1000);   // 真实生命周期起点
      // 起终点都贴到 <= 该时刻的最近 K 线（用 ≤ 而不是 ≥），
      // 否则生命周期跨度 < 一根 K 线粒度时端点会错位、整段被丢。
      const startCandle = candleAtOrBefore(candleTimes, segStart);
      if (startCandle === null) continue;
      // 结束：仍在挂 → 画到"现在"；已了结 → min(了结时刻, 现在)
      let endSec = o.status === "open" ? nowClip : Math.floor(o.status_timestamp / 1000);
      if (endSec > nowClip) endSec = nowClip;
      const endCandle = candleAtOrBefore(candleTimes, endSec);
      if (endCandle === null || endCandle < startCandle) continue;

      const isBuy = o.side === "B";
      const color = state.orderColors[isBuy ? "buy" : "sell"];
      // 线型按订单自身结局：成交 = 实线，未成交（撤销 / 仍在挂）= 点线
      const isFilled = o.status === "filled";
      const data = startCandle === endCandle
        ? [{ time: startCandle, value: o._px }]
        : [{ time: startCandle, value: o._px }, { time: endCandle, value: o._px }];
      draws.push({ data, color, lineStyle: isFilled ? LWC.LineStyle.Solid : LWC.LineStyle.Dotted });
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

// ---- 活跃挂单表：多级排序 ----
// 每列的取值函数；比较时依次按 state.ordersSort 的优先级走，直到分出先后。
const ORDER_SORT_GETTERS = {
  side: o => (o.side === "B" ? 0 : 1),          // 买=0 在前，卖=1 在后（升序时买单在上）
  price: o => (o._px ?? 0),
  sz: o => (o.orig_sz || o.sz || 0),
  notional: o => (o._notional || 0),
  pct: o => (o._pct ?? -1),
  type: o => `${o.order_type || ""}${o.is_trigger ? " trigger" : ""}${o.reduce_only ? " ro" : ""}`,
  created: o => (o.timestamp || 0),
};

function compareOrders(a, b) {
  for (const { key, dir } of state.ordersSort) {
    const get = ORDER_SORT_GETTERS[key];
    if (!get) continue;
    const va = get(a), vb = get(b);
    if (va < vb) return -dir;
    if (va > vb) return dir;
  }
  return 0;
}

// 表头箭头指示：▲升 / ▼降，多级时带优先级序号（¹²³）
function updateOrdersSortIndicators() {
  document.querySelectorAll("#orders-table thead th[data-sort]").forEach(th => {
    const idx = state.ordersSort.findIndex(s => s.key === th.dataset.sort);
    if (idx === -1) { th.innerHTML = th.dataset.label; return; }
    const s = state.ordersSort[idx];
    const lvl = state.ordersSort.length > 1 ? `<sup>${idx + 1}</sup>` : "";
    th.innerHTML = `${th.dataset.label} <span class="sort-ind">${s.dir === 1 ? "▲" : "▼"}${lvl}</span>`;
  });
}

let _lastOrdersBar = null;   // 记住最近一次渲染表格用的 bar，排序变化时原地重排

function renderOrdersTable(bar) {
  _lastOrdersBar = bar;
  const orders = activeOrdersAt(bar.T || bar.t);
  els.ordersTableBody.innerHTML = "";
  if (!orders.length) {
    els.ordersEmpty.hidden = false;
    return;
  }
  els.ordersEmpty.hidden = true;
  const av = bar.av;
  const rows = orders.map(o => {
    const px = o.is_trigger ? o.trigger_px : o.limit_px;
    const sz = o.orig_sz || o.sz;  // 用原始下单量：成交/撤销单的 sz(剩余量)=0，否则名义价值会算成 0
    const notional = (px || 0) * (sz || 0);
    const pct = (av && av > 0) ? (notional / av) : null;
    return { ...o, _px: px, _notional: notional, _pct: pct };
  }).sort(compareOrders);

  for (const o of rows) {
    const tr = document.createElement("tr");
    tr.className = o.side === "B" ? "buy" : "sell";
    const pctStr = o._pct === null ? "—" : (o._pct * 100).toFixed(2) + "%";
    const typeStr = o.is_trigger ? `${o.order_type || "Trigger"} @${fmtUsd(o.trigger_px, 0)}`
      : (o.order_type || "Limit");
    // status badge：成交=绿，撤销=灰，在挂=黄
    const statusBadge = o.status === "filled"
      ? `<span class="badge ok">${t("badge.filled")}</span>`
      : (o.status === "open" || o.status === "triggered")
        ? `<span class="badge open">${t("badge.open")}</span>`
        : `<span class="badge canceled">${t("badge.canceled")}</span>`;
    tr.innerHTML = `
      <td class="left side">${o.side === "B" ? t("tbl.buy") : t("tbl.sell")} ${statusBadge}</td>
      <td>${fmtUsd(o._px, 0)}</td>
      <td>${fmtBtc(o.orig_sz || o.sz, 5)}</td>
      <td>${fmtUsd(o._notional, 0)}</td>
      <td>${pctStr}</td>
      <td class="left">${typeStr}${o.reduce_only ? " · RO" : ""}</td>
      <td class="left muted small nowrap">${fmtTsShort(o.timestamp)}</td>
    `;
    els.ordersTableBody.appendChild(tr);
  }
}

// "铺满横屏"形态下把"显示全部订单"置灰（此时全部订单都铺满，再叠加会太乱）；
// 只有"生命周期"形态下"显示全部订单"才生效。
function updateOrderModeUI() {
  const seg = $("seg-show-all");
  if (seg) seg.classList.toggle("disabled", state.orderLineMode === "full");
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
    renderPinState();
    els.btnPin.classList.toggle("active", state.pinned);
    // 钉住→显示回放点十字线 + 隐藏未来时截到该点；取消→恢复最新价线 / 展开到最新
    replayScrollAndMark();
  });
  els.btnPinLatest.addEventListener("click", () => {
    const bars = state.timeline.bars;
    if (!bars.length) return;
    state.selectedT = bars[bars.length - 1].t;
    state.pinned = true;
    renderForSelected();
    renderPinState();
    replayScrollAndMark();
  });

  // 单步
  els.btnStepBack.addEventListener("click", () => stepBy(-1));
  els.btnStepFwd.addEventListener("click", () => stepBy(1));

  // 播放
  els.btnPlay.addEventListener("click", () => {
    if (state.playTimer) stopPlay();
    else startPlay();
  });

  // 显示未来 开/关（回放中途也可切换）
  $("chk-show-future").addEventListener("change", (e) => setShowFuture(e.target.checked));

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

  // 挂单显示形态：生命周期 / 铺满横屏
  $("seg-order-mode").querySelectorAll("button").forEach(btn => {
    btn.addEventListener("click", () => {
      $("seg-order-mode").querySelectorAll("button").forEach(b => b.classList.remove("active"));
      btn.classList.add("active");
      state.orderLineMode = btn.dataset.val === "full" ? "full" : "lifecycle";
      updateOrderModeUI();               // 铺满 → 置灰"显示全部订单"
      state._allOrdersRendered = false;
      renderForSelected();
    });
  });

  // 显示全部订单：关 / 开
  $("seg-show-all").querySelectorAll("button").forEach(btn => {
    btn.addEventListener("click", () => {
      if ($("seg-show-all").classList.contains("disabled")) return;   // 铺满模式下不可用
      $("seg-show-all").querySelectorAll("button").forEach(b => b.classList.remove("active"));
      btn.classList.add("active");
      state.showAllOrders = btn.dataset.val === "on";
      state._allOrdersRendered = false;  // 模式切换 → 强制重画一次
      renderForSelected();
    });
  });
  updateOrderModeUI();   // 初始化"显示全部订单"的可用状态

  // 技术指标：添加一条 EMA/SMA（周期取输入框的值）
  $("ind-add").addEventListener("click", () => {
    const type = $("ind-type").value;
    const period = parseInt($("ind-period").value, 10);
    if (!(period >= 1) || period > 1000) return;
    addIndicator(type, period);
  });
  $("ind-period").addEventListener("keydown", e => { if (e.key === "Enter") $("ind-add").click(); });

  // 挂单颜色：买 / 卖 分别设置，改完立即重画挂单线
  const redrawOrders = () => { state._allOrdersRendered = false; renderForSelected(); };
  $("order-color-buy").addEventListener("input", e => { state.orderColors.buy = e.target.value; redrawOrders(); });
  $("order-color-sell").addEventListener("input", e => { state.orderColors.sell = e.target.value; redrawOrders(); });

  // 中英文切换
  $("btn-lang").addEventListener("click", () => setLang(state.lang === "zh" ? "en" : "zh"));

  // K 线全屏放大（只看 K 线）：点按钮或按 Esc 退出
  const setChartMaximized = (on) => {
    state.chartMaximized = on;
    const wrap = $("chart-price").closest(".chart-wrap");
    if (wrap) wrap.classList.toggle("maximized", on);
    $("btn-chart-full").textContent = on ? "✕" : "⛶";
    requestAnimationFrame(() => window.dispatchEvent(new Event("resize")));
  };
  $("btn-chart-full").addEventListener("click", () => setChartMaximized(!state.chartMaximized));
  document.addEventListener("keydown", (e) => { if (e.key === "Escape" && state.chartMaximized) setChartMaximized(false); });

  // 历史状态快照 开 / 关（开=显示右侧信息栏，关=隐藏、K 线占满整行）
  $("seg-snapshot").querySelectorAll("button").forEach(btn => {
    btn.addEventListener("click", () => {
      $("seg-snapshot").querySelectorAll("button").forEach(b => b.classList.remove("active"));
      btn.classList.add("active");
      state.sidebarCollapsed = (btn.dataset.val === "hide");
      document.querySelector("main").classList.toggle("sidebar-collapsed", state.sidebarCollapsed);
      // 布局变了 → 让两张图按新宽度重排（复用 window resize 里绑定的 resize 逻辑）
      requestAnimationFrame(() => window.dispatchEvent(new Event("resize")));
    });
  });

  // 活跃挂单表：点表头排序。规则：点已是第一优先级的列 → 翻转升/降；
  // 点其他列 → 该列升为第一优先级，原有各级顺次降级（保留成多级排序）。
  document.querySelectorAll("#orders-table thead th[data-sort]").forEach(th => {
    th.addEventListener("click", () => {
      const key = th.dataset.sort;
      if (state.ordersSort.length && state.ordersSort[0].key === key) {
        state.ordersSort[0].dir = -state.ordersSort[0].dir;
      } else {
        state.ordersSort = [{ key, dir: 1 },
                            ...state.ordersSort.filter(s => s.key !== key)];
      }
      updateOrdersSortIndicators();
      if (_lastOrdersBar) renderOrdersTable(_lastOrdersBar);
    });
  });
  $("orders-sort-reset").addEventListener("click", () => {
    state.ordersSort = [{ key: "side", dir: 1 }, { key: "price", dir: 1 }];
    updateOrdersSortIndicators();
    if (_lastOrdersBar) renderOrdersTable(_lastOrdersBar);
  });
  updateOrdersSortIndicators();   // 启动时先画一次默认指示箭头

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
    els.pinState.textContent = t("pin.pinned_click", fmtTs(state.selectedT));
    // 点击某点：隐藏未来时截到该点（该点之后的 K 线不再显示）
    replayScrollAndMark();
  });
}

function stepBy(delta) {
  const bars = state.timeline.bars;
  const idx = bisectLeq(bars, state.selectedT, "t");
  const next = Math.max(0, Math.min(bars.length - 1, idx + delta));
  state.selectedT = bars[next].t;
  state.pinned = true;
  renderForSelected();
  renderPinState();
  replayScrollAndMark();   // 隐藏未来时：单步也把右端截到新选中点
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

// 回放时显示"回放位置"的十字虚线（横线=当时价格，竖线=走到哪了）——仅 overlay 模式
function showReplayMarker(bar) {
  const tSec = Math.floor(bar.t / 1000);
  try { priceChart.setCrosshairPosition(bar.c, tSec, priceSeries); } catch (e) { /* ignore */ }
  try { if (bar.lev != null) levChart.setCrosshairPosition(bar.lev, tSec, levSeries); } catch (e) { /* ignore */ }
}
function clearReplayMarker() {
  try { priceChart.clearCrosshairPosition(); } catch (e) { /* ignore */ }
  try { levChart.clearCrosshairPosition(); } catch (e) { /* ignore */ }
}

// "回放位置线 vs 最新真实价线"的总控，跟随【是否钉住】而不是【是否在播放】：
//  - overlay 模式且已钉住（播放中/暂停/手动钉）→ 隐藏"最新真实价"那条横虚线，改用回放点的十字虚线；
//  - 未钉住（跟随最新）或 history 模式 → 恢复最新价线、清掉十字线。
// 这样暂停后不会再出现"停在回放点、却显示最新价格那条线"的问题。
function updateOverlayMarker() {
  const bars = state.timeline && state.timeline.bars;
  if (state.replayMode !== "history" && state.pinned && bars && bars.length) {
    const i = bisectLeq(bars, state.selectedT, "t");
    if (bars[i]) {
      priceSeries.applyOptions({ priceLineVisible: false });
      showReplayMarker(bars[i]);
      return;
    }
  }
  priceSeries.applyOptions({ priceLineVisible: true });
  clearReplayMarker();
}

// 每帧回放：先统一处理回放线/最新价线，再按模式滚动。history 模式把右端截到回放点，
// 但【保留用户当前的缩放跨度】(只移动右端到"现在")，不强制变回某个固定窗口。
function replayScrollAndMark() {
  const bars = state.timeline && state.timeline.bars;
  if (!bars || !bars.length) return;
  updateOverlayMarker();
  if (state.replayMode === "history") {
    // 隐藏未来：把可见区右端截到"现在"（钉住点或最新），保留当前缩放跨度。
    // 无论是否在播放——只要处于隐藏未来 + 钉住某点，就截掉该点之后的 K 线。
    const nowSec = replayNowSec();
    const i = bisectLeq(bars, nowSec * 1000, "t");
    if (i < 0) return;
    const r = priceChart.timeScale().getVisibleLogicalRange();
    const span = (r && r.to > r.from) ? (r.to - r.from) : Math.min(150, bars.length);
    priceChart.timeScale().setVisibleLogicalRange({ from: i - span + 1, to: i + 1 });
  } else if (state.pinned) {
    scrollToSelected();
  }
}

// 显示未来 开/关 —— 回放中途也能切；切完立刻按新模式刷新视图。
// 关（history）：截到"现在"隐藏未来；开（overlay）：钉住时把选中点重新居中，露出未来。
function setShowFuture(showFuture) {
  state.replayMode = showFuture ? "overlay" : "history";
  state._allOrdersRendered = false;
  renderForSelected();
  replayScrollAndMark();
}

// 播放：用递归 setTimeout 而不是 setInterval —— 这样每一跳都重新读速度，
// 回放中途改速度立刻生效（也支持任意自定义值）。
function currentSpeed() {
  const v = Number(els.playSpeed.value);
  return (v && v > 0) ? v : 2;   // 根/秒
}
function scheduleNextPlayTick() {
  const speed = currentSpeed();
  const stepPerTick = speed > 40 ? Math.max(1, Math.round(speed / 30)) : 1;  // 高速时每帧多走几根
  const intervalMs = Math.max(16, Math.round(stepPerTick * 1000 / speed));
  state.playTimer = setTimeout(() => {
    const bars = state.timeline.bars;
    if (state.playIndex >= bars.length - 1) { stopPlay(); return; }
    state.playIndex = Math.min(bars.length - 1, state.playIndex + stepPerTick);
    state.selectedT = bars[state.playIndex].t;
    renderForSelected();
    replayScrollAndMark();
    els.pinState.textContent = t("pin.playing", fmtTs(state.selectedT));
    scheduleNextPlayTick();   // 下一跳重新读速度
  }, intervalMs);
}

function startPlay() {
  state.pinned = true;
  const bars = state.timeline.bars;
  const idx = bisectLeq(bars, state.selectedT, "t");
  state.playIndex = Math.max(0, idx);
  els.btnPlay.textContent = t("ctl.pause");
  updateOverlayMarker();   // overlay：隐藏"最新真实价"线，改用回放点十字线
  scheduleNextPlayTick();
}

function stopPlay() {
  if (state.playTimer) {
    clearTimeout(state.playTimer);
    state.playTimer = null;
  }
  els.btnPlay.textContent = t("ctl.play");
  renderPinState();         // 暂停后文案不再停留在"播放中"
  updateOverlayMarker();    // 仍钉在回放点 → 保留十字线、继续隐藏最新价线
}

// -------- 启动 --------
async function main() {
  applyStaticI18n();                    // 先按当前语言刷一遍静态文案（数据还没来也先本地化）
  document.title = t("app.title");
  try {
    makeCharts();
    bindControls();
    await loadAll();
    renderSummaryCards();
    await loadTimeline(state.interval);
    applyTimelineToCharts();
    applyLang();                        // 数据到位后，动态区域也按语言刷一遍
  } catch (e) {
    console.error(e);
    document.body.insertAdjacentHTML("afterbegin",
      `<div style="background:#5a1a1a;color:#fff;padding:12px;font-family:monospace">`
      + t("err.load", String(e.message || e))
      + `</div>`);
  }
}

main();
