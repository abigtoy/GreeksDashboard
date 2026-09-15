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
  snapshotLabel: '',
};

// ─── 列定义 ──────────────────────────────────────────────────────────────────
// ⚠️ 单一列源：表头(renderHead) / L1 / L2 / L3 行全部由此生成，改列只改这里。
// 初始值由后端 _shared_state["column_config"] 提供，页面加载时从 API 同步
let COL_DEF = [
  // 备用默认定义（API 未返回时使用）
  { col: 'symbol',           label: '合约',      fmt: null },
  { col: 'volume',           label: '数量',      fmt: '0'     },
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
  { col: 'pnl_daily',        label: '盯日盈亏',  fmt: '0',     align: 'R' },
  { col: 'pnl_today',        label: '当日盈亏',  fmt: '0',     align: 'R' },
  { col: 'pnl_history',      label: '浮动盈亏',  fmt: '0',     align: 'R' },
];

// 异步加载列配置：优先 localStorage（需完整18列），其次 API
async function loadColConfig() {
  const local = localStorage.getItem('col_config');
  if (local) {
    try {
      const parsed = JSON.parse(local);
      // 验证完整性：至少14列以上才算有效配置
      if (Array.isArray(parsed) && parsed.length >= 14) {
        COL_DEF = parsed;
        return;
      }
    } catch (_) {}
  }
  try {
    const r = await fetch_json('/api/columns');
    if (r && r.columns && Array.isArray(r.columns) && r.columns.length >= 14) {
      COL_DEF = r.columns;
      localStorage.setItem('col_config', JSON.stringify(COL_DEF));
    }
  } catch (_) {}
}

// 保存列配置到 API + localStorage
async function saveColConfig() {
  try {
    await fetch('/api/columns', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ columns: COL_DEF }),
    });
  } catch (_) {}
  localStorage.setItem('col_config', JSON.stringify(COL_DEF));
}

// 表头由 COL_DEF 生成，首列为树控件列
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
    if (_was_connected) showReconnectModal();
  } else if (st === 'error') {
    ctpEl.textContent = 'CTP连接失败';
    ctpEl.className = 'status-err';
    if (btn) { btn.textContent = '● 失败·重试'; btn.className = 'btn btn-ctp'; btn.disabled = false; }
    if (err) setMsg(err, 'err');
    if (_was_connected) showReconnectModal();
  } else {  // disconnected
    ctpEl.textContent = 'CTP未连接';
    ctpEl.className = 'status-err';
    if (btn) { btn.textContent = '● 连接CTP'; btn.className = 'btn btn-ctp'; btn.disabled = false; }
    if (_was_connected) showReconnectModal();
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
  var result = await fetch_json('/api/ctp/connect', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({
      '用户名':     document.getElementById('f-user').value.trim(),
      '密码':       document.getElementById('f-pass').value,
      '经纪商代码': document.getElementById('f-broker').value.trim(),
      '交易服务器': document.getElementById('f-td').value.trim(),
      '行情服务器': document.getElementById('f-md').value.trim(),
      '产品名称':   document.getElementById('f-product').value.trim(),
      '授权编码':   document.getElementById('f-auth').value.trim(),
    })
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
  } catch(e) {
    console.error('[poll]', e);
  }
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
}

// ─── 树过滤 ──────────────────────────────────────────────────────────────────
function filterTree(tree, txt) {
  if (!txt || !txt.trim()) return tree;
  const q = txt.trim().toLowerCase();
  // 返回 [l1, l2, l3] 全部匹配的节点
  const match = (node) =>
    (node.key && node.key.toLowerCase().includes(q)) ||
    (node.name && node.name.toLowerCase().includes(q)) ||
    (node.symbol && node.symbol.toLowerCase().includes(q));

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
  set('sumDC',    s.total_deltacash,   0);
  set('sumGC',    s.total_gammacash,   0);
  set('sumVC',    s.total_vegacash,    0);
  set('sumTC',    s.total_thetacash,   0);
  set('sumPnlD',  s.total_pnl_daily,   0);
  set('sumPnlT',  s.total_pnl_today,   0);
  set('sumPnlH',  s.total_pnl_history, 0);
  set('posCount', s.position_count,     0);
  set('cnt',      s.position_count,     0);

  // Summary 行颜色
  const pnlH = parseFloat(s.total_pnl_history) || 0;
  const el = document.getElementById('sumPnlH');
  if (el) {
    el.className = pnlCls(pnlH);
    el.textContent = fmt(pnlH, 0);
  }
}

// ─── 构建 L1/L2 汇总行（列布局由 COL_DEF 驱动，与表头/L3 严格对齐）───────────
function buildAggRow(node, level, expSelf, collapsed, action) {
  const tr = document.createElement('tr');
  // type: L1_PRODUCT → group-row, L2_MONTH → month-row, L3_LEG → pos-row
  const clsMap = { L1_PRODUCT: 'group-row', L2_MONTH: 'month-row', L3_LEG: 'pos-row' };
  const rowCls = clsMap[node.type] || 'group-row';
  tr.className = rowCls + (collapsed ? ' collapsed' : '');
  const m = node.metrics || {};
  const ico = expSelf ? '▼' : '▶';
  // symbol 列作为树控件格，其余列按 visible 过滤
  const dataCols = COL_DEF.filter(c => c.visible !== false && c.col !== 'symbol');
  const tds = dataCols.map(c => {
    if (c.col === 'symbol') return '';  // 不应出现
    // L1_PRODUCT 品种行不显示 volume（子节点汇总，无参考意义）
    if (node.type === 'L1_PRODUCT' && c.col === 'volume') return '<td></td>';
    if (Object.prototype.hasOwnProperty.call(m, c.col)) {
      const v = m[c.col];
      const tag = c.col === 'deltacash' ? cls(tagDC(v))
                : c.col === 'gammacash' ? cls(tagGC(v))
                : c.col.indexOf('pnl') === 0 ? pnlCls(v) : '';
      const defAlign = isNumCol(c.col) ? 'right' : 'left';
      const align = c.align ? `text-align:${c.align==='R'?'right':'left'};` : `text-align:${defAlign};`;
      return `<td class="num ${tag}" style="${align}">${fmt(v, null, c.fmt, c.pct)}</td>`;
    }
    return '<td></td>';
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

  const visible = COL_DEF.filter(c => c.visible !== false);
  // L3 合约名（symbol）显示在树控件格，带缩进
  let html = `<td class="tree-cell"><span class="l3-sym">${l3.name || l3.symbol}</span></td>`;

  for (const c of visible) {
    if (c.col === 'symbol') {
      // symbol 列不单独显示（首列已由树控件占用）
      continue;
    }
    let v = l3[c.col];

    if (c.col === 'direction') {
      v = l3.direction || '';
    } else if (c.col === 'iv' && v !== null && v !== undefined) {
      v = parseFloat(v);
    }

    const numCls = isNumCol(c.col) ? 'num' : '';
    const tagCls = getTagClass(c.col, l3);
    const pnlC   = c.col === 'pnl_history' ? pnlCls(l3.pnl_history)
                  : c.col === 'pnl_today'  ? pnlCls(l3.pnl_today)
                  : c.col === 'pnl_daily'  ? pnlCls(l3.pnl_daily) : '';
    // 数字列默认右对齐；文本列默认左对齐
    const defAlign = isNumCol(c.col) ? 'right' : 'left';
    const align = c.align ? `text-align:${c.align==='R'?'right':'left'};` : `text-align:${defAlign};`;
    const cls    = [numCls, tagCls, pnlC].filter(Boolean).join(' ');

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
  if (['pnl_history','pnl_today','pnl_daily'].includes(col)) return pnlCls(v);
  return '';
}

function getTagClass(col, l3) {
  if (['deltacash','delta'].includes(col))   return cls(l3.delta_tag);
  if (['gammacash','gamma'].includes(col))   return cls(l3.gamma_tag);
  if (['pnl_history','pnl_today','pnl_daily'].includes(col)) return pnlCls(l3[col]);
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
  document.querySelectorAll('.tab-content, .col-config-panel').forEach(t => { t.style.display = 'none'; });
  const tabEl = document.querySelector('.settings-tab[onclick="switchSettingsTab(\'' + tab + '\')"]');
  if (tabEl) tabEl.classList.add('active');
  const contentEl = document.getElementById('tab-' + tab);
  if (contentEl) contentEl.style.display = 'block';
  if (tab === 'cols') renderColConfigPanel();
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
function showReconnectModal() {
  if (_reconnect_shown) return;
  _reconnect_shown = true;
  const m = document.getElementById('reconnect-modal');
  if (m) m.classList.add('show');
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

// ─── 切片弹窗 ─────────────────────────────────────────────────────────────────
let _snapSelDate = '';
let _snapSelSess = '';

function openSnapModal() {
  _snapSelDate = '';
  _snapSelSess = '';
  const modal = document.getElementById('snap-modal');
  const dateIn = document.getElementById('snap-date');
  const sessBtns = document.getElementById('snap-session-btns');
  const msg = document.getElementById('snap-msg');
  // 默认选今天
  const today = new Date();
  dateIn.value = today.toISOString().slice(0, 10);
  _snapSelDate = dateIn.value;
  sessBtns.style.display = 'none';
  msg.textContent = '';
  document.querySelectorAll('.snap-sess-btn').forEach(b => b.style.borderColor = '#2a3a4a');
  modal.style.display = 'flex';
  // 触发日期检查
  onSnapDateChange();
}

function onSnapDateChange() {
  const dateVal = document.getElementById('snap-date').value;
  _snapSelDate = dateVal;
  _snapSelSess = '';
  const sessBtns = document.getElementById('snap-session-btns');
  const msg = document.getElementById('snap-msg');
  if (!dateVal) {
    sessBtns.style.display = 'none';
    msg.textContent = '请选择日期';
    return;
  }
  // 查询该日期有哪些切片
  fetch_json('/api/snapshots?t=' + Date.now()).then(data => {
    // trading_date 格式是 YYYYMMDD，date input 值格式是 YYYY-MM-DD，统一转换
    const dateYMD = dateVal.replace(/-/g, '');
    const hasN = !!(data || []).find(s => s.trading_date === dateYMD && s.session === 'N');
    const hasA = !!(data || []).find(s => s.trading_date === dateYMD && s.session === 'A');
    const hasP = !!(data || []).find(s => s.trading_date === dateYMD && s.session === 'P');
    sessBtns.style.display = 'flex';
    document.querySelectorAll('.snap-sess-btn').forEach(b => {
      const s = b.dataset.sess;
      const exists = (s === 'N' && hasN) || (s === 'A' && hasA) || (s === 'P' && hasP);
      b.style.opacity = exists ? '1' : '0.3';
      b.style.cursor = exists ? 'pointer' : 'not-allowed';
      b.style.borderColor = '#2a3a4a';
    });
    if (!hasN && !hasA && !hasP) {
      msg.textContent = '该日期无切片数据';
    } else {
      msg.textContent = '';
    }
  });
}

function selectSnapSess(sess) {
  const btn = document.querySelector(`.snap-sess-btn[data-sess="${sess}"]`);
  if (!btn || btn.style.cursor === 'not-allowed') return;
  _snapSelSess = sess;
  document.querySelectorAll('.snap-sess-btn').forEach(b => b.style.borderColor = '#2a3a4a');
  btn.style.borderColor = '#4a9eff';
  document.getElementById('snap-msg').textContent = '';
}

async function confirmSnapLoad() {
  if (!_snapSelDate || !_snapSelSess) {
    document.getElementById('snap-msg').textContent = '请先选择日期和场次';
    return;
  }
  const name = `data_snapshot_${_snapSelDate.replace(/-/g, '')}_${_snapSelSess}.json`;
  const data = await fetch_json('/api/snapshot/load', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ name }),
  });
  if (!data || data.status !== 'ok') {
    document.getElementById('snap-msg').textContent = '加载失败';
    return;
  }
  State.snapshotMode = true;
  State.snapshotLabel = `${_snapSelSess === 'N' ? '夜盘' : _snapSelSess === 'A' ? '早盘' : '午盘'} ${_snapSelDate}`;
  State.snapshot = {
    status: 'snapshot',
    tree: data.tree || [],
    summary: data.summary || {},
    positions: data.positions || [],
  };
  State.expandedG.clear();
  State.expandedM.clear();
  (data.tree || []).forEach(l1 => {
    State.expandedG.add(l1.key);
    (l1.children || []).forEach(l2 => State.expandedM.add(l2.key));
  });
  closeSnapModal();
  render();
  renderSummary(data.summary || {});
}

function closeSnapModal() {
  document.getElementById('snap-modal').style.display = 'none';
}

function returnToLive() {
  State.snapshotMode = false;
  fetchDashboard();
}

// ─── 初始化 ──────────────────────────────────────────────────────────────────
async function init() {
  State.startTime = Date.now();

  // 加载列配置（优先 localStorage，其次 API）
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

  // uptime 时钟
  setInterval(updateHeader, 1000);
}

// DOM Ready
if (document.readyState === 'loading') {
  document.addEventListener('DOMContentLoaded', init);
} else {
  init();
}
