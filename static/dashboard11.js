// dashboard11.js — 前端纯渲染（Phase 3）
// 职责：fetch /api/dashboard → 递归渲染 L1/L2/L3 树 → 前端内存排序
// 禁止：任何 Greeks 计算、正则解析、汇总聚合

// ─── 常量 ───────────────────────────────────────────────────────────────────
const POLL_MS   = 3000;         // 轮询间隔
const TREE_KEYS = ['L1_PRODUCT', 'L2_MONTH', 'L3_CONTRACT'];

// ─── 全局错误捕获 ────────────────────────────────────────────────────────────
window.onerror = function(msg, url, line) {
  console.error('[JS FATAL]', msg, 'Line:', line);
  return false;
};

// ─── 状态 ────────────────────────────────────────────────────────────────────
const State = {
  snapshot:    null,            // 最新快照
  expandedG:   new Set(),       // 展开的 L1 key
  expandedM:   new Set(),       // 展开的 L2 key
  __initial:   true,            // 首次加载标记（auto-expand 只在首次生效）
  sortCol:     'pnl_history',   // 当前排序列
  sortAsc:     false,           // 升序?
  filterTxt:   '',              // 品种筛选
  lastUpdate:  null,
  startTime:   null,
  serverUptime: 0,             // 服务端 uptime 秒（每次 /api/dashboard 刷新）
  uptimeAt:    0,              // 收到该 uptime 的本地时刻（补间用）
  snapshotMode: false,          // 离线快照模式：true 时停止轮询覆盖渲染
};
// ⚠️ 单一列源：表头(renderHead) / L1 / L2 / L3 行全部由此生成，改列只改这里。
// 初始值由后端 _shared_state["column_config"] 提供，页面加载时从 API 同步
let COL_DEF = [
  // 备用默认定义（API 未返回时使用）
  { col: 'symbol',           label: '合约',      fmt: null },
  { col: 'volume',           label: '数量',      fmt: '0',      align: 'R' },
  { col: 'underlying_price', label: '标的价',    fmt: '0.00',  align: 'R' },
  { col: 'last_price',       label: '最新价',    fmt: '0.00',  align: 'R' },
  { col: 'adjust_price',     label: '调整价',    fmt: '0.0000',align: 'R' },
  { col: 'open_price',       label: '开仓价',    fmt: '0.0000',align: 'R' },
  { col: 'iv',               label: 'IV%',       fmt: '0.00',  pct: true, align: 'R' },
  { col: 'delta',            label: 'Δ',         fmt: '0.0000',align: 'R' },
  { col: 'gamma',            label: 'Γ',         fmt: '0.000000',align:'R' },
  { col: 'vega',             label: 'Vega',      fmt: '0.0000',align: 'R' },
  { col: 'deltacash',        label: 'ΔCash',     fmt: '0',     align: 'R' },
  { col: 'gammacash',        label: 'ΓCash',     fmt: '0',     align: 'R' },
  { col: 'vegacash',         label: 'VegaCash',  fmt: '0',     align: 'R' },
  { col: 'thetacash',        label: 'ΘCash',     fmt: '0',     align: 'R' },
  { col: 'days_to_expiry',   label: '剩余天',    fmt: '0',     align: 'R' },
  { col: 'pnl_today',        label: '当日盈亏',  fmt: '0',     align: 'R' },
  { col: 'pnl_history',      label: '浮动盈亏',  fmt: '0',     align: 'R' },
];

// 加载列配置：服务器优先（唯一权威），localStorage 仅离线兜底镜像
async function loadColConfig() {
  try {
    const r = await fetch_json('/api/columns');
    if (r && r.columns && Array.isArray(r.columns) && r.columns.length >= 14) {
      COL_DEF = r.columns.filter(c => c.col !== 'direction');
      localStorage.setItem('col_config', JSON.stringify(COL_DEF));
      return;
    }
  } catch (_) {}
  const local = localStorage.getItem('col_config');
  if (local) {
    try {
      const parsed = JSON.parse(local);
      if (Array.isArray(parsed) && parsed.length >= 14) {
        COL_DEF = parsed.filter(c => c.col !== 'direction');
      }
    } catch (_) {}
  }
}

// 保存列配置：服务器权威（失败重试一次），localStorage 仅做离线镜像
async function saveColConfig() {
  const body = JSON.stringify({ columns: COL_DEF });
  for (let i = 0; i < 2; i++) {
    try {
      const r = await fetch('/api/columns', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body,
      });
      if (r.ok) break;
    } catch (_) {}
  }
  localStorage.setItem('col_config', JSON.stringify(COL_DEF));
}

// 表头由 COL_DEF 生成，首列为树控件列
// ===== 页面边距（09-24规格）=====
// 左右：字段能完整展示 → max(5%视口宽, 20px)；有横向溢出（不完整）→ 20px。下：表格区固定20px
// 三个块统一边距（整体左右对齐）：.header 信息栏、.summary-bar 概览、.wrap 持仓表
// 注：.wrap 未设 overflow-x，宽内容会 visible 穿透，wrap.scrollWidth 测不到——
//     须用表格自身 offsetWidth 与 wrap.clientWidth 比较
function applyTableMargin() {
  const wrap = document.querySelector('.wrap');
  if (!wrap) return;
  const tbl = wrap.querySelector('table');
  wrap.style.marginBottom = '20px';
  const big = Math.max(window.innerWidth * 0.05, 20);
  let m = big;
  wrap.style.marginLeft = wrap.style.marginRight = big + 'px';   // 先按大边距撑开再测
  if (tbl && tbl.offsetWidth - wrap.clientWidth > 1) m = 20;      // 字段不完整 → 回退20px
  const px = m + 'px';
  wrap.style.marginLeft = wrap.style.marginRight = px;
  document.querySelectorAll('.header, .summary-bar').forEach(el => {
    el.style.marginLeft = el.style.marginRight = px;
  });
}

function renderHead() {
  const tr = document.getElementById('headRow');
  if (!tr) return;
  const visible = COL_DEF.filter(c => c.visible !== false && c.col !== 'symbol');
  tr.innerHTML = '<th data-sort-col="tree">合约</th>' + visible.map(c => {
    const align = c.align === 'L' ? 'left' : 'right';
    return `<th data-sort-col="${c.col}" style="text-align:${align}">${c.label}</th>`;
  }).join('');
  tr.querySelectorAll('[data-sort-col]').forEach(el =>
    el.addEventListener('click', () => sortBy(el.dataset.sortCol)));
  updateHeadSortCls();
  renderFoot(visible);
  applyTableMargin();
}

// 表尾汇总行：与表头列对齐。合约列写"汇总"；数量/ΔCash/ΓCash/VegaCash/ΘCash/当日盈亏/浮动盈亏 各占对应列，其余列留空
function renderFoot(visible) {
  const foot = document.getElementById('sumFoot');
  if (!foot) return;
  const ids = { volume: 'ftVol', deltacash: 'ftDC', gammacash: 'ftGC',
                vegacash: 'ftVC', thetacash: 'ftTC', pnl_today: 'ftPnlT', pnl_history: 'ftPnlH' };
  foot.innerHTML = '<tr style="font-weight: bold; position: sticky; bottom: 0; background: #1a1a1a; border-top: 2px solid #555;">'
    + '<td>汇总</td>'
    + visible.map(c => {
        const align = c.align === 'L' ? 'left' : 'right';
        const id = ids[c.col];
        return `<td ${id ? `id="${id}"` : ''} style="text-align:${align}">-</td>`;
      }).join('')
    + '</tr>';
}

function updateHeadSortCls() {
  document.querySelectorAll('#headRow th[data-sort-col]').forEach(el => {
    const on = State.sortCol === el.dataset.sortCol;
    el.classList.toggle('sorted-asc',  on &&  State.sortAsc);
    el.classList.toggle('sorted-desc', on && !State.sortAsc);
  });
}

// ─── 工具 ────────────────────────────────────────────────────────────────────
// fmt 支持两种模式：
//   dec 参数（legacy）: fmt(v, 2) → 固定小数位
//   fmt 掩码（新）:      fmt(v, null, '0.00%') → 自定义格式
// 掩码规则：'0.00' = 千分位+两位小数，'0.00%' = 百分比，'0' = 整数千分位
function fmt(v, dec, mask, pct) {
  if (v === null || v === undefined) return '-';
  const n = parseFloat(v);
  if (isNaN(n)) return '-';
  if (mask !== undefined && mask !== null && mask !== '') {
    return _fmtMask(n, mask, pct);
  }
  if (dec === null || dec === undefined) return n;
  const s = n.toFixed(dec);
  const parts = s.split('.');
  parts[0] = parts[0].replace(/\B(?=(\d{3})+(?!\d))/g, ',');
  return parts.join('.');
}

function _fmtMask(n, mask, pct) {
  // pct=true: 原始值就是百分比（如 IV=36.26），只加%后缀不×100
  // mask 以 % 结尾: 原始值是小数（如 0.3626），需×100再加%
  const isPctFromMask = mask.endsWith('%');
  const isPct = pct || isPctFromMask;
  const base = isPctFromMask ? mask.slice(0, -1) : mask;
  // 判断是否有小数位（.后有多少个0/9）
  const m = base.match(/^([^.]*)(\.(0+|#+))?$/);
  if (!m) return String(n);
  const intPart = m[1] || '';
  // mask 整数部分必须为数字，否则无效
  if (!/^\d*$/.test(intPart)) return String(n);
  const fracPart = m[3] ? m[3] : '';
  // 计算小数位数
  const dec = fracPart.length > 0 ? fracPart.length : (isPct ? 2 : 0);
  let sign = '';
  if (n < 0) { sign = '-'; n = Math.abs(n); }
  // pct=true 且 mask 以 % 结尾 → 原始值是小数(如 0.3626)，需×100
  // pct=true 但 mask 无 % 后缀 → 原始值已是百分比(如 36.26)，不需×100，直接显示
  let result = (isPct && isPctFromMask ? (n * 100).toFixed(dec) : n.toFixed(dec));
  const parts = result.split('.');
  parts[0] = parts[0].replace(/\B(?=(\d{3})+(?!\d))/g, ',');
  return sign + parts.join('.') + (isPct ? '%' : '');
}

function cls(tag) {
  // tag: 'green'/'yellow'/'red' → css class name
  if (!tag || tag === 'green') return '';
  return tag === 'yellow' ? 'tag-yellow' : 'tag-red';
}

function pnlCls(v) {
  v = parseFloat(v);
  if (isNaN(v)) return '';
  if (v < 0)    return 'pnl-neg';
  if (v > 0)    return 'pnl-pos';
  return '';
}

// ─── CTP 连接 ────────────────────────────────────────────────────────────────
var _ctp_connected = false;
var _was_connected = false;      // 曾成功连接过 → 掉线才弹重连窗（首次未连不弹）
var _reconnect_shown = false;
var _last_ctp_error = '';        // 最近一次错误，disconnected 时仍需保留用于弹窗

function showMsg(text, type) {
  type = type || 'info';
  var el = document.getElementById('conn-msg');
  if (!el) return;
  el.textContent = text; el.className = 'msg ' + type;
  clearTimeout(window._msg_timer);
  window._msg_timer = setTimeout(function() { el.textContent = ''; }, 5000);
}

async function fetch_json(url, opts) {
  try {
    var r = await fetch(url, opts);
    var txt = await r.text();
    var j;
    try { j = JSON.parse(txt); } catch { j = { error: txt }; }
    if (!r.ok) throw new Error(j.error || j.message || r.status);
    return j;
  } catch (e) {
    showMsg(e.message, 'err');
    return null;
  }
}

function setMsg(text, type) {
  var el = document.getElementById('conn-msg');
  if (!el) return;
  el.textContent = text || '';
  el.className = 'msg' + (type ? ' ' + type : '');
}

function updateCtpStatus(status) {
  var ctpEl = document.getElementById('ctp-status');
  var btn   = document.getElementById('btn-ctp');
  if (!ctpEl) return;
  var st  = status.status || (status.connected ? 'connected' : 'disconnected');
  var err = status.error || '';
  if (status.login_error_id > 0 && !err) {
    err = '验证失败(' + status.login_error_id + '): ' + (status.login_error_msg || '');
  }
  if (err) _last_ctp_error = err;  // 保存最近错误，disconnected 时仍可显示
  var connected = (st === 'connected');

  if (connected) {
    ctpEl.textContent = 'CTP已连接';
    ctpEl.className = 'status-ok';
    if (btn) { btn.textContent = '● 已连接·断开'; btn.className = 'btn btn-ctp connected'; btn.disabled = false; }
    setMsg('', '');
    _was_connected = true;
    closeReconnectModal();
  } else if (st === 'connecting') {
    ctpEl.textContent = 'CTP连接中';
    ctpEl.className = 'status-conn';
    // 连接中：按钮保持可点，点击即取消（doDisconnect）
    if (btn) { btn.textContent = '○ 连接中·点取消'; btn.className = 'btn btn-ctp pending'; btn.disabled = false; }
    if (err) setMsg(err, 'info');
    if (_was_connected) showReconnectModal(err);
  } else if (st === 'error') {
    ctpEl.textContent = err ? ('CTP连接失败: ' + err) : 'CTP连接失败';
    ctpEl.className = 'status-err';
    if (btn) { btn.textContent = '● 失败·重试'; btn.className = 'btn btn-ctp'; btn.disabled = false; }
    if (err) setMsg(err, 'err');
    if (_was_connected) showReconnectModal(err);
  } else {  // disconnected
    var lastErr = _last_ctp_error;
    ctpEl.textContent = lastErr ? ('CTP掉线: ' + lastErr) : 'CTP未连接';
    ctpEl.className = 'status-err';
    if (btn) { btn.textContent = '● 连接CTP'; btn.className = 'btn btn-ctp'; btn.disabled = false; }
    if (_was_connected && _last_ctp_error) showReconnectModal(_last_ctp_error);
  }
  _ctp_connected = connected;
}

async function onCtpBtnClick() {
  var btn = document.getElementById('btn-ctp');
  // connecting 中再点 = 取消；否则按当前连接态分发
  if (_ctp_connected) await doDisconnect();
  else if (btn && btn.classList.contains('pending')) await doDisconnect();
  else await doConnect();
}

async function doConnect() {
  var btn = document.getElementById('btn-ctp');
  if (!btn || btn.classList.contains('pending')) return;
  btn.disabled = true;
  btn.className = 'btn btn-ctp pending';
  btn.textContent = '○ 连接中…';
  setMsg('正在连接，请等待...', 'info');

  // 表单输入框不存在时，回退到 /api/ctp/config 的已保存配置
  var userEl = document.getElementById('f-user');
  var passEl = document.getElementById('f-pass');
  var brokerEl = document.getElementById('f-broker');
  var tdEl = document.getElementById('f-td');
  var mdEl = document.getElementById('f-md');
  var productEl = document.getElementById('f-product');
  var authEl = document.getElementById('f-auth');

  var payload = {
    '用户名':     (userEl && userEl.value) ? userEl.value.trim() : '',
    '密码':       (passEl && passEl.value) ? passEl.value : '',
    '经纪商代码': (brokerEl && brokerEl.value) ? brokerEl.value.trim() : '',
    '交易服务器': (tdEl && tdEl.value) ? tdEl.value.trim() : '',
    '行情服务器': (mdEl && mdEl.value) ? mdEl.value.trim() : '',
    '产品名称':   (productEl && productEl.value) ? productEl.value.trim() : '',
    '授权编码':   (authEl && authEl.value) ? authEl.value.trim() : '',
  };

  // 若任一字段为空，尝试从后端读取已保存配置补全
  var hasEmpty = Object.values(payload).some(function(v){ return !v; });
  if (hasEmpty) {
    try {
      var cfg = await fetch_json('/api/ctp/config');
      if (cfg) {
        if (!payload['用户名']) payload['用户名'] = cfg['用户名'] || '';
        if (!payload['密码']) payload['密码'] = cfg['密码'] || '';
        if (!payload['经纪商代码']) payload['经纪商代码'] = cfg['经纪商代码'] || '';
        if (!payload['交易服务器']) payload['交易服务器'] = cfg['交易服务器'] || '';
        if (!payload['行情服务器']) payload['行情服务器'] = cfg['行情服务器'] || '';
        if (!payload['产品名称']) payload['产品名称'] = cfg['产品名称'] || '';
        if (!payload['授权编码']) payload['授权编码'] = cfg['授权编码'] || '';
      }
    } catch(e) { console.warn('[ctp] 回退读取配置失败', e); }
  }

  var result = await fetch_json('/api/ctp/connect', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(payload)
  });
  // 成功→3s 轮询接管状态；失败→就地复位
  if (!result || !result.success) {
    btn.className = 'btn btn-ctp'; btn.disabled = false; btn.textContent = '● 连接CTP';
    if (result) setMsg(result.error || '连接请求失败', 'err');
  }
}

async function doDisconnect() {
  var btn = document.getElementById('btn-ctp');
  if (btn) { btn.disabled = true; btn.className = 'btn btn-ctp pending'; btn.textContent = '○ 断开中…'; }
  var result = await fetch_json('/api/ctp/disconnect', { method: 'POST' });
  if (result && result.success) {
    _ctp_connected = false;
    _was_connected = false;      // 手动断开 → 不弹自动重连窗
    _last_ctp_error = '';        // 清除错误记录
    updateCtpStatus({ status: 'disconnected', connected: false });
    setMsg('已断开', 'ok');
  } else if (btn) { btn.disabled = false; btn.className = 'btn btn-ctp connected'; btn.textContent = '● 已连接·断开'; }
}

// ─── 轮询 ────────────────────────────────────────────────────────────────────
async function fetchCtpStatus() {
  var st = await fetch_json('/api/ctp/status?t=' + Date.now());
  if (st) updateCtpStatus(st);
}

async function fetchDashboard() {
  if (State.snapshotMode) return;         // 离线快照模式：不覆盖
  try {
    const r  = await fetch('/api/dashboard?t=' + Date.now());
    if (!r.ok) throw new Error('HTTP ' + r.status);
    State.snapshot = await r.json();
    State.lastUpdate = new Date();
    State.serverUptime = State.snapshot.uptime_seconds || 0;
    State.uptimeAt = Date.now();

    // 首次加载：三层全展开；之后保持用户手动状态不变
    if (State.__initial) {
      State.__initial = false;
      (State.snapshot.tree || []).forEach(l1 => {
        State.expandedG.add(l1.key);
        (l1.children || []).forEach(l2 => State.expandedM.add(l2.key));
      });
    }

    render();
    updateHeader();
    handleAlerts(State.snapshot);
  } catch(e) {
    console.error('[poll]', e);
  }
}

// ─── 监控预警：toast 弹窗 + 告警记录弹窗 ────────────────────────────────────
const _seenAlertIds = new Set();     // 前端去重：同一 popup 只弹一次

// 合约代码 → 品种（后端 contract_und 为权威，MO→IM 等别名由后端归一；缺失时按去分隔符/数字兜底）
function undOf(code) {
  const m = (State.snapshot && State.snapshot.contract_und) || {};
  const bare = String(code || '').split('.')[0];
  if (m[bare]) return m[bare];
  return bare.replace(/[_-].*$/, '').replace(/\d.*$/, '');
}

function handleAlerts(snap) {
  if (!snap) return;
  // 1) 弹窗（后端已做冷却/计数，这里只负责展示 + 前端去重）
  const popups = snap.popups || [];
  for (const p of popups) {
    const key = p.alert_id + '|' + p.ts;
    if (_seenAlertIds.has(key)) continue;
    _seenAlertIds.add(key);
    showAlertToast(p);
  }
  // 2) 告警记录弹窗若开着 → 实时刷新
  const modal = document.getElementById('alert-list-modal');
  if (modal && modal.classList.contains('show')) renderAlertList();
}

function showAlertToast(p) {
  const box = document.getElementById('alert-toasts');
  if (!box) return;
  const div = document.createElement('div');
  div.className = 'alert-toast ' + (p.level === 'danger' ? 'danger' : 'warn');
  div.innerHTML = `<span>${p.msg}</span><span class="toast-close">✕</span>`;
  div.querySelector('.toast-close').addEventListener('click', ev => { ev.stopPropagation(); div.remove(); });
  div.addEventListener('click', () => openAlertList());
  box.appendChild(div);
  // 最多同时挂 6 条，超出丢弃最旧的
  while (box.children.length > 6) box.removeChild(box.firstChild);
}

// ─── 告警记录弹窗（时间排序 / 等级筛选 / 品种筛选）──────────────────────────
let _alertSortDesc = true;

function openAlertList() {
  const m = document.getElementById('alert-list-modal');
  if (!m) return;
  renderAlertList();
  m.classList.add('show');
}

function closeAlertList() {
  const m = document.getElementById('alert-list-modal');
  if (m) m.classList.remove('show');
}

function toggleAlertSort() {
  _alertSortDesc = !_alertSortDesc;
  renderAlertList();
}

function onAlertFilterChange() {
  renderAlertList();
}

// 告警源中文名（弹窗/历史表统一用；conv_delta 源报的是「换算Δ」= 固定 σ_ref 下的 Δ，
// 与看板 Δ 列的市场 Δ 不是一个东西）
const SRC_LABEL = { f_rate: 'F速率', iv_rate: 'IV速率', burn: 'Burn', conv_delta: '换算Δ', margin: '风险度' };

function renderAlertList() {
  const tbody = document.getElementById('alert-list-body');
  if (!tbody) return;
  const all = (State.snapshot && State.snapshot.alerts) || [];
  const lvSel = document.getElementById('alert-f-lv');
  const symSel = document.getElementById('alert-f-sym');
  const wantLv = lvSel ? lvSel.value : '';
  const wantSym = symSel ? symSel.value : '';

  // 品种下拉选项（按当天告警里出现过的品种生成，保留用户已选项）
  if (symSel) {
    const keys = [...new Set(all.map(a => a.underlying || a.symbol || ''))].filter(Boolean).sort();
    const cur = symSel.value;
    symSel.innerHTML = '<option value="">全部</option>' + keys.map(k => `<option value="${k}">${k}</option>`).join('');
    if (keys.includes(cur) || cur === '') symSel.value = cur;
  }

  const rows = all.filter(a => (!wantLv || a.level === wantLv)
    && (!wantSym || (a.underlying || a.symbol) === wantSym));
  rows.sort((a, b) => {
    const t = String(a.ts || '').localeCompare(String(b.ts || ''));
    return _alertSortDesc ? -t : t;
  });

  const cnt = document.getElementById('alert-list-count');
  if (cnt) cnt.textContent = `(${rows.length}/${all.length})`;

  if (!rows.length) {
    tbody.innerHTML = '<tr><td colspan="8" style="color:#666;padding:12px;">暂无告警</td></tr>';
    return;
  }
  tbody.innerHTML = rows.map(a => {
    const lvCls = a.level === 'danger' ? 'ah-lv-danger' : 'ah-lv-warn';
    const lvTxt = a.level === 'danger' ? '红' : '黄';
    const f2 = v => (v === null || v === undefined || v === '') ? '' : Number(v).toFixed(2);
    const span = (a.v_from === null || a.v_from === undefined) ? '' : `${a.v_from} → ${a.v_to}`;
    return `<tr><td>${a.ts ? a.ts.slice(11) : ''}</td><td>${SRC_LABEL[a.source] || a.source || ''}</td>`
         + `<td>${a.underlying || a.symbol || ''}</td><td>${a.symbol || ''}</td>`
         + `<td class="${lvCls}">${lvTxt}</td><td>${f2(a.value)}</td><td>${span}</td>`
         + `<td>${f2(a.threshold)}</td></tr>`;
  }).join('');
}

function updateHeader() {
  const s = State.snapshot;
  if (!s) return;

  if (!State.snapshotMode) {
    const ts = s.last_update || '--:--:--';
    const el = document.getElementById('lastUpdate');
    if (el) el.textContent = ts;
  }

  // 运行时间（锚定服务端启动时间 + 本地补间；非客户端计时）
  if (State.uptimeAt) {
    const sec = State.serverUptime + Math.floor((Date.now() - State.uptimeAt) / 1000);
    const h = Math.floor(sec / 3600);
    const m = Math.floor((sec % 3600) / 60);
    const ss = sec % 60;
    const upEl = document.getElementById('uptime');
    if (upEl) upEl.textContent = h + ':' + String(m).padStart(2,'0') + ':' + String(ss).padStart(2,'0');
  }
}

// ─── 主渲染 ──────────────────────────────────────────────────────────────────
function render() {
  const snap = State.snapshot;
  if (!snap) return;

  const tree = snap.tree || [];
  const filtered = filterTree(tree, State.filterTxt);
  const sorted   = sortTree(filtered, State.sortCol, State.sortAsc);

  const tbody = document.getElementById('positionBody');
  if (!tbody) return;
  tbody.innerHTML = '';

  for (const l1 of sorted) {
    // L1 行
    const l1Row = buildL1Row(l1);
    tbody.appendChild(l1Row);

    // L2 节点（已展开）
    const l1Exp = State.expandedG.has(l1.key);
    for (const l2 of (l1.children || [])) {
      const l2Row = buildL2Row(l2, l1.key, l1Exp);
      tbody.appendChild(l2Row);

      // L3 节点（已展开）
      const l2Exp = State.expandedM.has(l2.key);
      if (l2Exp) {
        for (const l3 of (l2.children || [])) {
          tbody.appendChild(buildL3Row(l3, l2.key));
        }
      }
    }
  }

  // Summary 区
  renderSummary(snap.summary);
  applyTableMargin();
}

// ─── 树过滤 ──────────────────────────────────────────────────────────────────
function filterTree(tree, txt) {
  if (!txt || !txt.trim()) return tree;
  // 支持多关键字：`au sc im` 或 `au&sc&im`，任一命中即保留（OR）
  const keywords = txt.trim().split(/[\s&]+/).map(s => s.toLowerCase()).filter(Boolean);
  if (!keywords.length) return tree;
  const match = (node) =>
    (node.key && keywords.some(k => node.key.toLowerCase().includes(k))) ||
    (node.name && keywords.some(k => node.name.toLowerCase().includes(k))) ||
    (node.symbol && keywords.some(k => node.symbol.toLowerCase().includes(k)));

  return tree
    .map(l1 => {
      // L1 匹配 → 全子树
      if (match(l1)) return { ...l1, children: l1.children || [] };
      // L2/L3 匹配 → 截取匹配子树
      const l1children = (l1.children || [])
        .map(l2 => {
          if (match(l2)) return { ...l2, children: l2.children || [] };
          const l2children = (l2.children || []).filter(l3 => match(l3));
          return l2children.length > 0 ? { ...l2, children: l2children } : null;
        })
        .filter(Boolean);
      return l1children.length > 0 ? { ...l1, children: l1children } : null;
    })
    .filter(Boolean);
}

// ─── 前端内存排序 ─────────────────────────────────────────────────────────────
function sortTree(tree, col, asc) {
  const dir = asc ? 1 : -1;

  const getVal = (node) => {
    if (node.type === 'L3_CONTRACT' || node.symbol) {
      // L3 / 合约节点：直接字段
      return node[col] ?? 0;
    } else if (node.metrics) {
      // L1/L2 聚合节点：从 metrics 取
      return node.metrics[col] ?? 0;
    }
    return 0;
  };

  const sorted = [...tree].map(l1 => ({
    ...l1,
    children: [...(l1.children || [])].map(l2 => ({
      ...l2,
      children: [...(l2.children || [])].sort((a, b) => {
        const va = getVal(a);
        const vb = getVal(b);
        if (typeof va === 'string') return dir * va.localeCompare(vb);
        return dir * ((va * 1) - (vb * 1));
      }),
    })).sort((a, b) => {
      const va = getVal(a);
      const vb = getVal(b);
      if (typeof va === 'string') return dir * va.localeCompare(vb);
      return dir * ((va * 1) - (vb * 1));
    }),
  }));

  // L1 排序（不跨分支，不打散 children）
  return sorted.sort((a, b) => {
    const va = getVal(a);
    const vb = getVal(b);
    if (typeof va === 'string') return dir * va.localeCompare(vb);
    return dir * ((va * 1) - (vb * 1));
  });
}

// ─── Summary 区 ───────────────────────────────────────────────────────────────
function renderSummary(s) {
  if (!s) return;
  const set = (id, v, dec) => {
    const el = document.getElementById(id);
    if (el) el.textContent = fmt(v, dec);
  };
  // Greeks 汇总 + 数量：持仓表表尾汇总行（renderFoot 生成 ft* 单元格，列被隐藏时 set 容错跳过）
  set('ftDC', s.total_deltacash,   0);
  set('ftGC', s.total_gammacash,   0);
  set('ftVC', s.total_vegacash,    0);
  set('ftTC', s.total_thetacash,   0);
  // 汇总行"数量"= 全部持仓手数
  const posAll = (State.snapshot && State.snapshot.positions) || [];
  set('ftVol', posAll.reduce((a, p) => a + Math.abs(p.volume || 0), 0), 0);

  // 市值权益 = 动态权益(CTP balance) + 期权净市值（多头为正、空头为负，义务仓当负债）
  // 已对券商验证：6928813.19 + 79480(多头) - 442580(空头) = 6565713.19 ✓
  let netOptMv = 0;
  const posList = (State.snapshot && State.snapshot.positions) || [];
  for (const p of posList) {
    if (!p.option_type) continue; // 期货 option_type 为空
    const mv = (p.last_price || 0) * Math.abs(p.volume || 0) * (p.size || 0);
    netOptMv += p.direction === 'long' ? mv : -mv;
  }
  set('sumEquity',
      (State.snapshot && State.snapshot.account ? State.snapshot.account.balance : null) !== null
        ? (State.snapshot.account.balance + netOptMv)
        : null,
      0);

  set('ftPnlT',  s.total_pnl_today,   0);
  set('ftPnlH',  s.total_pnl_history, 0);
  set('sumPnlT', s.total_pnl_today,   0);
  set('sumPnlH', s.total_pnl_history, 0);
  // 盈亏国际标准配色：盈利绿 #4ade80 / 亏损红 #f87171 / 零默认白
  const setSign = (id, v) => {
    const el = document.getElementById(id);
    if (!el) return;
    el.style.color = v > 0 ? '#4ade80' : v < 0 ? '#f87171' : '';
  };
  setSign('sumPnlT', s.total_pnl_today);
  setSign('sumPnlH', s.total_pnl_history);
  setSign('ftPnlT', s.total_pnl_today);
  setSign('ftPnlH', s.total_pnl_history);
  // 浮动盈亏/持仓数已从概览移除，此处仅保留风险度
  // 风险度（账户级 Margin：黄 95% / 红 110%，仅提示不阻断）
  const ms = (State.snapshot && State.snapshot.margin_status) || {};
  const mEl = document.getElementById('sumMargin');
  if (mEl) {
    const r = ms.ratio_pct;
    if (r === null || r === undefined) {
      mEl.textContent = '-';
      mEl.className = 'value';
    } else {
      const lv = ms.level;
      mEl.textContent = fmt(r, 1) + '%';
      mEl.className = 'value' + (lv === 'danger' ? ' alert-danger' : lv === 'warn' ? ' alert-warn' : '');
    }
  }
  // 标准风险度 = margin / equity（ms.ratio_pct）
  // 严格风险度 = (margin + obligation_premium) / equity × 100（后端直接算好传 ms.strict_risk_ratio）
  const strictEl = document.getElementById('sumMarginStrict');
  if (strictEl) {
    if (ms && ms.strict_risk_ratio !== null && ms.strict_risk_ratio !== undefined) {
      strictEl.textContent = fmt(ms.strict_risk_ratio, 1) + '%';
      strictEl.className = 'value' + (ms.strict_risk_ratio >= 95 ? ' alert-danger' : ms.strict_risk_ratio >= 85 ? ' alert-warn' : '');
    } else {
      strictEl.textContent = '-';
      strictEl.className = 'value';
    }
  }
}

// ─── 构建 L1/L2 汇总行（列布局由 COL_DEF 驱动，与表头/L3 严格对齐）───────────
function buildAggRow(node, level, expSelf, collapsed, action) {
  const tr = document.createElement('tr');
  // type: L1_PRODUCT → group-row, L2_MONTH → month-row, L3_LEG → pos-row
  const clsMap = { L1_PRODUCT: 'group-row', L2_MONTH: 'month-row', L3_LEG: 'pos-row' };
  const rowCls = clsMap[node.type] || 'group-row';
  tr.className = rowCls + (collapsed ? ' collapsed' : '');
  // 告警变色：L1/L2 行取该行对应品种的最高级（danger > warn）
  const _af2 = (State.snapshot && State.snapshot.active_flags) || {};
  const _key2 = node.key || '';
  let _lv2 = _af2[node.type === 'L1_PRODUCT' ? _key2 : undOf(_key2)];
  if (_lv2 === 'danger') tr.classList.add('alert-danger');
  else if (_lv2 === 'warn') tr.classList.add('alert-warn');
  const m = node.metrics || {};
  const ico = expSelf ? '▼' : '▶';
  // 该行所属品种的单元格级标记：F 速率→标的价列，IV 速率→IV%列，Burn→当日盈亏列
  const _ad2 = (State.snapshot && State.snapshot.active_details) || {};
  const _und3 = node.type === 'L1_PRODUCT' ? _key2 : undOf(_key2);
  const cellLv = (col) => {
    if (col === 'underlying_price') return (_ad2.f_rate || {})[_und3];
    if (col === 'iv')               return (_ad2.iv_rate || {})[_und3];
    if (col === 'pnl_today')        return (_ad2.burn || {})[_und3];
    return null;
  };
  // symbol 列作为树控件格，其余列按 visible 过滤
  const dataCols = COL_DEF.filter(c => c.visible !== false && c.col !== 'symbol');
  const tds = dataCols.map(c => {
    if (c.col === 'symbol') return '';  // 不应出现
    // L1_PRODUCT 品种行不显示 volume（子节点汇总，无参考意义）
    if (node.type === 'L1_PRODUCT' && c.col === 'volume') return '<td></td>';
    const aLv = cellLv(c.col);
    const aCls = aLv === 'danger' ? 'alert-danger' : aLv === 'warn' ? 'alert-warn' : '';
    if (Object.prototype.hasOwnProperty.call(m, c.col)) {
      const v = m[c.col];
      const tag = c.col === 'deltacash' ? cls(tagDC(v))
                : c.col === 'gammacash' ? cls(tagGC(v))
                : c.col.indexOf('pnl') === 0 ? pnlCls(v) : '';
      const defAlign = isNumCol(c.col) ? 'right' : 'left';
      const align = c.align ? `text-align:${c.align==='R'?'right':'left'};` : `text-align:${defAlign};`;
      return `<td class="num ${tag} ${aCls}" style="${align}">${fmt(v, null, c.fmt, c.pct)}</td>`;
    }
    return aCls ? `<td class="${aCls}"></td>` : '<td></td>';
  });
  tr.innerHTML = `<td class="tree-cell"><span class="toggle" data-action="${action}" data-key="${node.key}">${ico}</span> <span class="l${level}-name">${node.name || node.key}</span></td>` + tds.join('');
  // 行头点击 → 折叠/展开
  const toggleFn = action === 'toggleG'
    ? () => toggleG(node.key)
    : () => toggleM(node.key);
  tr.querySelector('[data-action]').addEventListener('click', e => { e.stopPropagation(); toggleFn(); });
  tr.addEventListener('click', toggleFn);
  // L1 字体加粗
  if (level === 1) {
    const nameSpan = tr.querySelector('.l1-name');
    if (nameSpan) nameSpan.style.fontWeight = '700';
  }
  return tr;
}

function buildL1Row(l1) {
  return buildAggRow(l1, 1, State.expandedG.has(l1.key), false, 'toggleG');
}

function buildL2Row(l2, l1Key, l1Exp) {
  return buildAggRow(l2, 2, State.expandedM.has(l2.key), !l1Exp, 'toggleM');
}

// ─── 构建 L3 明细行（td 顺序与 COL_DEF 一致 = 与表头对齐）──────────────────────
function buildL3Row(l3, l2Key) {
  const tr = document.createElement('tr');
  tr.className = 'pos-row l3-indent';
  tr.dataset.key = l3.key;

  // 告警标记：合约级（结构风险 |Δ|）→ 该合约整行；品种级四源 → 只标对应单元格
  const _ad = (State.snapshot && State.snapshot.active_details) || {};
  const _code = String(l3.symbol || '').split('.')[0];
  const _und = undOf(_code);
  const _lvRow = (_ad.conv_delta || {})[_code];
  if (_lvRow === 'danger') tr.classList.add('alert-danger');
  else if (_lvRow === 'warn') tr.classList.add('alert-warn');

  const visible = COL_DEF.filter(c => c.visible !== false);
  // L3 合约名（symbol）显示在树控件格，带缩进
  let html = `<td class="tree-cell"><span class="l3-sym">${l3.symbol}</span></td>`;

  for (const c of visible) {
    if (c.col === 'symbol') {
      // symbol 列不单独显示（首列已由树控件占用）
      continue;
    }
    let v = l3[c.col];

    if (c.col === 'iv' && v !== null && v !== undefined) {
      v = parseFloat(v);
    }

    const numCls = isNumCol(c.col) ? 'num' : '';
    const tagCls = getTagClass(c.col, l3);
    const pnlC   = c.col === 'pnl_history' ? pnlCls(l3.pnl_history)
                  : c.col === 'pnl_today'  ? pnlCls(l3.pnl_today) : '';
    // 单元格级告警：F 速率→标的价列，IV 速率→IV%列，Burn→当日盈亏列
    const aLv = c.col === 'underlying_price' ? (_ad.f_rate  || {})[_und]
              : c.col === 'iv'               ? (_ad.iv_rate || {})[_und]
              : c.col === 'pnl_today'        ? (_ad.burn    || {})[_und]
              : null;
    const aCls = aLv === 'danger' ? 'alert-danger' : aLv === 'warn' ? 'alert-warn' : '';
    // 数字列默认右对齐；文本列默认左对齐
    const defAlign = isNumCol(c.col) ? 'right' : 'left';
    const align = c.align ? `text-align:${c.align==='R'?'right':'left'};` : `text-align:${defAlign};`;
    const cls    = [numCls, tagCls, pnlC, aCls].filter(Boolean).join(' ');

    html += `<td class="${cls}" style="${align}">${fmt(v, null, c.fmt, c.pct)}</td>`;
  }

  tr.innerHTML = html;
  return tr;
}

// ─── 列属性判断 ──────────────────────────────────────────────────────────────
function isNumCol(col) {
  return !['symbol','direction'].includes(col);
}

function getCellClass(col, v, dc, gc, pnlH, deltaTag, gammaTag, pnlTag) {
  if (['deltacash','delta'].includes(col))    return cls(deltaTag);
  if (['gammacash','gamma'].includes(col))    return cls(gammaTag);
  if (['pnl_history','pnl_today'].includes(col)) return pnlCls(v);
  return '';
}

function getTagClass(col, l3) {
  if (['deltacash','delta'].includes(col))   return cls(l3.delta_tag);
  if (['gammacash','gamma'].includes(col))   return cls(l3.gamma_tag);
  if (['pnl_history','pnl_today'].includes(col)) return pnlCls(l3[col]);
  return '';
}

// ─── 阈值标签（用于汇总行）─────────────────────────────────────────────────────
function tagDC(v) { const a=Math.abs(v); if(a>=100000) return 'red'; if(a>=50000) return 'yellow'; return 'green'; }
function tagGC(v) { const a=Math.abs(v); if(a>=60000)  return 'red'; if(a>=30000) return 'yellow'; return 'green'; }

// ─── 折叠 ────────────────────────────────────────────────────────────────────
function toggleG(key) {
  const isExp = State.expandedG.has(key);
  if (isExp) {
    // 折叠：收起 L1 及所有子 L2
    State.expandedG.delete(key);
    const l1 = (State.snapshot?.tree || []).find(n => n.key === key);
    if (l1) {
      for (const l2 of (l1.children || [])) {
        State.expandedM.delete(l2.key);
      }
    }
  } else {
    // 展开：L1 展开，同时展开其下所有 L2
    State.expandedG.add(key);
    const l1 = (State.snapshot?.tree || []).find(n => n.key === key);
    if (l1) {
      for (const l2 of (l1.children || [])) {
        State.expandedM.add(l2.key);
      }
    }
  }
  render();
}

function toggleM(key) {
  const isExp = State.expandedM.has(key);
  if (isExp) {
    State.expandedM.delete(key);
  } else {
    State.expandedM.add(key);
  }
  render();
}

// ─── 列头排序 ────────────────────────────────────────────────────────────────
function sortBy(col) {
  if (State.sortCol === col) {
    State.sortAsc = !State.sortAsc;
  } else {
    State.sortCol = col;
    State.sortAsc = false;
  }
  updateHeadSortCls();
  render();
}

// ─── 筛选 ────────────────────────────────────────────────────────────────────
function onFilter(txt) {
  State.filterTxt = txt;
  render();
}

// ─── 设置面板 ────────────────────────────────────────────────────────────────
function _toggle_settings() {
  document.getElementById('settings-panel').classList.toggle('open');
}

function switchSettingsTab(tab) {
  document.querySelectorAll('.settings-tab').forEach(t => t.classList.remove('active'));
  document.querySelectorAll('.tab-content, .col-config-panel, .settings-tab-content').forEach(t => { t.style.display = 'none'; });
  // 使用 data-tab 匹配避免转义问题
  const tabEl = [...document.querySelectorAll('.settings-tab')].find(t => {
    const txt = t.onclick?.toString() || '';
    return txt.includes("switchSettingsTab('" + tab + "')");
  });
  if (tabEl) tabEl.classList.add('active');
  const contentEl = document.getElementById('tab-' + tab);
  if (contentEl) contentEl.style.display = 'block';
  if (tab === 'cols') renderColConfigPanel();
  if (tab === 'risk') loadRiskThresholds();
  if (tab === 'sigma') loadSigmaRef();
}

// ─── 风控阈值面板 ─────────────────────────────────────────────────────────────
async function loadRiskThresholds() {
  const el = document.getElementById('risk-thresholds');
  if (!el) return;
  try {
    const res = await fetch('/api/alert/settings', {cache: 'no-store'});
    const data = await res.json();
    const cfg = data.settings || {};
    const spec = data.spec || {};
    
    // 品种净 Δ 监控仍禁用（后端 net_delta_warn=10000），故不出现在面板
    const visibleKeys = ['f_rate_warn','f_rate_danger','iv_rate_warn','conv_delta_warn','burn_warn','burn_danger','margin_ratio_warn','margin_ratio_danger'];
    
    let html = '<div style="display:grid;grid-template-columns:repeat(auto-fill,minmax(280px,1fr));gap:12px;">';
    for (const k of visibleKeys) {
      const label = spec[k]?.label || k;
      const val = cfg[k] ?? null;
      const isNum = typeof val === 'number';
      // 不加 min/max/step 限制，用户输入什么就存什么（后端同样不夹）
      html += `<div class="field"><label>${label}</label><input type="${isNum?'number':'text'}" id="th-${k}" value="${isNum?val:''}"> </div>`;
    }
    html += '</div>';
    el.innerHTML = html;
  } catch(e) { console.error(e); }
}

async function saveRiskThresholds() {
  const patch = {};
  // 品种净 Δ 监控仍禁用（后端 net_delta_warn=10000），故不提交
  const keys = ['f_rate_warn','f_rate_danger','iv_rate_warn','conv_delta_warn','burn_warn','burn_danger','margin_ratio_warn','margin_ratio_danger'];
  for (const k of keys) {
    const inp = document.getElementById('th-'+k);
    if (inp && inp.value) {
      const v = parseFloat(inp.value);
      if (!isNaN(v)) patch[k] = v;
    }
  }
  try {
    const res = await fetch('/api/alert/settings', {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify(patch)});
    const ok = await res.json();
    if (ok.ok) {
      await loadRiskThresholds();   // 保存成功后立即重载，确保 UI 与后端一致
      alert('阈值已保存');
    } else {
      alert('保存失败:'+JSON.stringify(ok));
    }
  } catch(e) { alert(e.message); }
}

// ─── σ_ref IV 面板 ──────────────────────────────────────────────────────────
let _sigma_table_rows = [];
async function loadSigmaRef() {
  const tbody = document.getElementById('sigma-body');
  if (!tbody) return;
  try {
    const res = await fetch('/api/sigma_ref');
    const data = await res.json();
    const rows = data.rows || [];
    const def = data.default || 0.25;
    _sigma_table_rows = rows.map(r => ({sym:r.symbol, sigma: (r.sigma_ref == null || isNaN(r.sigma_ref)) ? def*100 : r.sigma_ref*100, held: (r.held ?? true), effective: ((r.effective || def))*100}));
    let html = '';
    for (const r of _sigma_table_rows) {
      const filled = r.held && r.sigma != null && r.sigma > 0;
      const status = r.held && r.sigma == null ? '🟡待填' : (filled ? '✅' : '');
      const inputVal = filled ? Math.round(r.sigma) : '';
      const readonly = !r.held ? 'disabled' : '';
      html += `<tr><td style="padding:6px 8px;text-align:left;font-weight:600;">${r.sym}${r.held&&r.sigma==null?' <span style="color:#fbbf24">🟡</span>':''}</td><td style="padding:6px 8px;text-align:right;"><input type="number" min="5" max="150" step="1" ${readonly} onchange="onSigmaRowChange('${r.sym}',this.value)" value="${inputVal}" style="width:70px;text-align:right;"></td><td style="padding:6px 8px;text-align:center;color:#93c5fd;">${status||''}</td></tr>`;
    }
    tbody.innerHTML = html;
    // 检查弹窗缺失
    await checkSigmaModal();
  } catch(e) { console.error(e); }
}

function onSigmaRowChange(sym, val) {
  const idx = _sigma_table_rows.findIndex(r => r.sym === sym);
  if (idx !== -1) {
    const v = parseInt(val);
    if (v >= 5 && v <= 150) {
      _sigma_table_rows[idx].sigma = v;
    } else {
      _sigma_table_rows[idx].sigma = null;
    }
  }
}

async function saveSigmaRef() {
  // 未填写的行写空值（占位），已填的写数字（百分比整数，后端自动 /100）
  const tableRows = _sigma_table_rows.map(r => {
    const v = (r.sigma == null || r.sigma <= 0) ? '' : Math.round(r.sigma);
    return `${r.sym},${v}`;
  }).join('\n');
  try {
    const res = await fetch('/api/sigma_ref/save', {method:'POST', headers:{'Content-Type':'text/plain'}, body:tableRows});
    const ok = await res.json();
    if (ok.ok) {
      alert(`已保存 ${ok.rows_saved} 行 → ${ok.csv_path}`);
    } else {
      alert('保存失败: ' + (ok.error || JSON.stringify(ok)));
    }
  } catch(e) { alert(e.message); }
}

// ─── σ_ref 弹窗 ───────────────────────────────────────────────────────────
async function checkSigmaModal() {
  try {
    const res = await fetch('/api/sigma_ref/pending');
    const data = await res.json();
    const missing = data.missing || [];
    if (missing.length > 0) {
      const list = document.getElementById('sigma-pending-list');
      const modal = document.getElementById('sigma-modal');
      list.innerHTML = missing.map(s => '<li>'+s+'（当前默认 25%）</li>').join('');
      modal.style.display = 'block';
      window._sigma_pending = missing;
    }
  } catch(e) {}
}

function dismissSigmaModal(ignoreToday) {
  const modal = document.getElementById('sigma-modal');
  modal.style.display = 'none';
  if (ignoreToday && window._sigma_pending && window._sigma_pending.length) {
    fetch('/api/sigma_ref/ack', {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({symbols:window._sigma_pending})}).catch(()=>{});
  }
}


// ─── 列配置面板渲染 ──────────────────────────────────────────────────────────
function renderColConfigPanel() {
  const list = document.getElementById('col-config-list');
  if (!list) return;
  list.innerHTML = COL_DEF.map((c, i) => `
    <div class="col-config-item" data-idx="${i}">
      <input type="checkbox" ${c.visible !== false ? 'checked' : ''}
        onchange="toggleColVisible(${i}, this.checked)">
      <span class="col-name">${c.label || c.col}</span>
      <input type="text" value="${c.fmt || ''}" placeholder="格式"
        onchange="setColFmt(${i}, this.value)">
      <button onclick="moveCol(${i}, -1)" ${i === 0 ? 'disabled' : ''}>←</button>
      <button onclick="moveCol(${i}, 1)" ${i === COL_DEF.length - 1 ? 'disabled' : ''}>→</button>
    </div>
  `).join('');
}

function moveCol(idx, dir) {
  const newIdx = idx + dir;
  if (newIdx < 0 || newIdx >= COL_DEF.length) return;
  const arr = [...COL_DEF];
  [arr[idx], arr[newIdx]] = [arr[newIdx], arr[idx]];
  COL_DEF = arr;
  saveColConfig();
  renderHead();
  renderColConfigPanel();
}

function toggleColVisible(idx, visible) {
  COL_DEF[idx].visible = visible !== false;
  saveColConfig();
  renderHead();
  render();
}

function setColFmt(idx, fmt) {
  COL_DEF[idx].fmt = fmt || null;
  saveColConfig();
  renderHead();
  render();
}

function doFilterByUnder(val) {
  State.filterTxt = val || '';
  render();
}

async function loadConfig() {
  const cfg = await fetch_json('/api/ctp/config');
  if (!cfg) return;
  const set = (id, v) => { const el = document.getElementById(id); if (el) el.value = v || ''; };
  set('f-user', cfg['用户名']); set('f-pass', cfg['密码']); set('f-broker', cfg['经纪商代码']);
  set('f-td', cfg['交易服务器']); set('f-md', cfg['行情服务器']);
  set('f-product', cfg['产品名称']); set('f-auth', cfg['授权编码']);
}

async function populateAccountSelect() {
  const r = await fetch_json('/api/ctp/accounts');
  if (!r) return;
  const sel = document.getElementById('account-select');
  if (!sel) return;
  sel.innerHTML = '';
  (r.accounts || []).forEach(n => {
    const o = document.createElement('option');
    o.value = n; o.textContent = n;
    if (n === r.active) o.selected = true;
    sel.appendChild(o);
  });
}

async function onAccountSelect(name) {
  if (!name) return;
  const r = await fetch_json('/api/ctp/account/load', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ name: name }),
  });
  if (!r || r.status !== 'ok') return;
  const d = r.data || {};
  const set = (id, v) => { const el = document.getElementById(id); if (el) el.value = v || ''; };
  set('f-user', d['用户名']); set('f-pass', d['密码']); set('f-broker', d['经纪商代码']);
  set('f-td', d['交易服务器']); set('f-md', d['行情服务器']);
  set('f-product', d['产品名称']); set('f-auth', d['授权编码']);
}

// ─── 断线重连弹窗 ─────────────────────────────────────────────────────────────
function showReconnectModal(errMsg) {
  if (_reconnect_shown) return;
  _reconnect_shown = true;
  const m = document.getElementById('reconnect-modal');
  if (m) {
    const p = m.querySelector('.modal-box p');
    if (p && errMsg) {
      p.textContent = errMsg;
    } else if (p) {
      p.textContent = '网络连接已中断，是否尝试重新连接？';
    }
    m.classList.add('show');
  }
}

function closeReconnectModal() {
  _reconnect_shown = false;
  const m = document.getElementById('reconnect-modal');
  if (m) m.classList.remove('show');
}

async function doReconnectFromModal() {
  closeReconnectModal();
  await doConnect();
}

// ─── 切片下拉：后端列什么就选什么，按 name 直接加载 ──────────────────────────
async function loadSnapOptions() {
  if (State.snapshotMode) return;              // 看切片期间不重绘，避免选中项被清
  const sel = document.getElementById('snap-select');
  if (!sel) return;
  const data = await fetch_json('/api/snapshots?t=' + Date.now());
  sel.innerHTML = '<option value="">← 实时</option>' + (data || []).map(s => {
    const tag = s.kind === 'close' ? '收盘' : (s.session || s.kind);
    return `<option value="${s.name}">${s.trading_date} ${tag} ${s.leaves || s.position_count || 0}条</option>`;
  }).join('');
}

async function onSnapSelect(name) {
  if (!name) { returnToLive(); return; }
  const data = await fetch_json('/api/snapshot/load', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ name }),
  });
  if (!data || data.status !== 'ok') {
    document.getElementById('snap-select').value = '';   // fetch_json 已 showMsg 报错
    return;
  }
  State.snapshotMode = true;
  const snapshot = (data.dashboard && typeof data.dashboard === 'object'
    && Object.keys(data.dashboard).length > 0)
    ? data.dashboard
    : {
        status: 'snapshot',
        tree: data.tree || [],
        summary: data.summary || {},
        positions: data.positions || [],
        account: data.account || {},
        margin_status: {},
        underlying_prices: data.underlying_prices || {},
        active_flags: {},
        active_details: {},
        contract_und: {},
        alerts: [],
        popups: [],
      };
  State.snapshot = snapshot;
  State.expandedG.clear();
  State.expandedM.clear();
  (State.snapshot.tree || []).forEach(l1 => {
    State.expandedG.add(l1.key);
    (l1.children || []).forEach(l2 => State.expandedM.add(l2.key));
  });
  render();
  renderSummary(data.summary || {});
}

function returnToLive() {
  State.snapshotMode = false;
  fetchDashboard();
  loadSnapOptions();
}

// ─── 初始化 ──────────────────────────────────────────────────────────────────
async function init() {
  State.startTime = Date.now();

  // 加载列配置（服务器优先，本地仅兜底）
  await loadColConfig();

  // 列头点击排序
  renderHead();

  // 折叠按钮
  document.getElementById('btn-expand')?.addEventListener('click', e => {
    e.stopPropagation();
    const snap = State.snapshot;
    if (!snap) return;
    for (const l1 of snap.tree || []) {
      State.expandedG.add(l1.key);
      for (const l2 of (l1.children || [])) {
        State.expandedM.add(l2.key);
      }
    }
    render();
  });

  document.getElementById('btn-collapse')?.addEventListener('click', e => {
    e.stopPropagation();
    State.expandedG.clear();
    State.expandedM.clear();
    render();
  });

  // 设置按钮
  document.getElementById('btn-settings').onclick = _toggle_settings;

  // 回填 CTP 凭证
  loadConfig();
  populateAccountSelect();

  // CTP 状态轮询（驱动断线重连弹窗）
  fetchCtpStatus();
  setInterval(fetchCtpStatus, POLL_MS);

  // 看板轮询
  fetchDashboard();
  setInterval(fetchDashboard, POLL_MS);

  // 切片下拉（列表一天只变一次，60s 刷够用；避免每 3s 重读 20 份 JSON）
  loadSnapOptions();
  setInterval(loadSnapOptions, 60000);

  // uptime 时钟
  setInterval(updateHeader, 1000);
}

// DOM Ready
if (document.readyState === 'loading') {
  document.addEventListener('DOMContentLoaded', init);
} else {
  init();
}
