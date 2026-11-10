/* ══════════════════════════════════════════════
   ForexBot Dashboard — JavaScript
   ══════════════════════════════════════════════ */

const socket = io();

// ── Chart instances ───────────────────────────
let equityChart, priceChart;
const MAX_POINTS = 120;

// ── State ─────────────────────────────────────
let currentMode  = 'simulator';
let botRunning   = false;
let mySessionId  = '';   // set after /api/auth/me

// ─────────────────────────────────────────────
// On Load
// ─────────────────────────────────────────────
window.addEventListener('DOMContentLoaded', async () => {
  initCharts();
  initSpeedSlider();
  startClock();

  // 1. Get our session info
  try {
    const res  = await fetch('/api/auth/me');
    const data = await res.json();
    if (data.error) {
      window.location.href = '/login';
      return;
    }
    mySessionId = data.session_id;
    document.getElementById('userPill').textContent = data.user_name || 'User';

    // Set mode from session
    if (data.mode === 'live') setMode('live');
    if (data.is_running) setBotRunning(true);
  } catch (e) {
    window.location.href = '/login';
    return;
  }

  loadSavedCreds();
  fetchInitialState();
});

async function logout() {
  await fetch('/api/auth/logout', { method: 'POST' });
  window.location.href = '/login';
}

// ─────────────────────────────────────────────
// Charts
// ─────────────────────────────────────────────
function initCharts() {
  const baseScale = {
    grid:  { color: 'rgba(99,120,180,0.08)' },
    ticks: { color: '#4a5d80', font: { family: 'JetBrains Mono', size: 10 }, maxTicksLimit: 8 }
  };
  const baseOpts = {
    animation: false, responsive: true, maintainAspectRatio: false,
    plugins: { legend: { display: false }, tooltip: { mode: 'index', intersect: false } },
    scales: { x: baseScale, y: { ...baseScale } },
    interaction: { intersect: false, mode: 'index' },
  };

  // Equity Chart
  const eCtx = document.getElementById('equityChart').getContext('2d');
  const eqGrad = eCtx.createLinearGradient(0,0,0,180);
  eqGrad.addColorStop(0,'rgba(108,99,255,0.4)'); eqGrad.addColorStop(1,'rgba(108,99,255,0)');
  const balGrad = eCtx.createLinearGradient(0,0,0,180);
  balGrad.addColorStop(0,'rgba(62,207,207,0.2)'); balGrad.addColorStop(1,'rgba(62,207,207,0)');

  equityChart = new Chart(eCtx, {
    type: 'line',
    data: { labels: [], datasets: [
      { label:'Equity',  data:[], borderColor:'#6C63FF', backgroundColor:eqGrad,  borderWidth:2, pointRadius:0, fill:true, tension:0.4 },
      { label:'Balance', data:[], borderColor:'#3ECFCF', backgroundColor:balGrad, borderWidth:1.5, pointRadius:0, fill:true, tension:0.4, borderDash:[4,4] }
    ]},
    options: { ...baseOpts, scales: { ...baseOpts.scales, y: { ...baseScale,
      ticks: { ...baseScale.ticks, callback: v => '$'+v.toFixed(0) }
    }}}
  });

  // Price Chart
  const pCtx = document.getElementById('priceChart').getContext('2d');
  const prGrad = pCtx.createLinearGradient(0,0,0,180);
  prGrad.addColorStop(0,'rgba(62,207,207,0.25)'); prGrad.addColorStop(1,'rgba(62,207,207,0)');

  priceChart = new Chart(pCtx, {
    type: 'line',
    data: { labels: [], datasets: [
      { label:'Price', data:[], borderColor:'#3ECFCF', backgroundColor:prGrad, borderWidth:2, pointRadius:0, fill:true, tension:0.3 }
    ]},
    options: baseOpts
  });
}

// ─────────────────────────────────────────────
// Socket.IO events
// ─────────────────────────────────────────────
socket.on('connect', () => {
  setConnectionStatus(true);
  addLog('Connected to ForexBot server', 'info');
  // Join our private room so only our events arrive
  if (mySessionId) {
    socket.emit('join_session', { session_id: mySessionId });
  }
});
socket.on('joined', (data) => {
  addLog(`Room joined: ${data.user_name}`, 'info');
});
socket.on('disconnect', () => {
  setConnectionStatus(false);
  addLog('⚠️ Disconnected from server', 'warning');
});

socket.on('state_sync', (data) => {
  updateMetrics(data);
  updatePositionsTable(data.open_trades || []);
  if (data.mode === 'live') setLiveMode(true);
});

socket.on('mt5_account', (info) => {
  showMT5Bar(info);
});

socket.on('tick', (data) => {
  updateMetrics(data);
  updateHeaderPrice(data.current_price);
  updatePositionsTable(data.open_trades || []);
  const ts = new Date().toLocaleTimeString('en-US', {hour12:false,hour:'2-digit',minute:'2-digit',second:'2-digit'});
  pushDatapoint(equityChart, ts, [data.equity, data.balance]);
  pushDatapoint(priceChart,  ts, [data.current_price]);
});

socket.on('trade_opened', (trade) => {
  addLog(`📂 Level ${trade.level} OPENED | Entry: ${trade.entry_price} | Lot: ${trade.lot_size}`, 'open');
  showToast(`📂 Level ${trade.level} opened @ ${trade.entry_price}`, 'info');
});

socket.on('grid_win', (data) => {
  const s = data.pnl >= 0 ? '+' : '';
  addLog(`🏆 GRID WIN! P/L: ${s}$${data.pnl.toFixed(2)} | Balance: $${data.balance.toFixed(2)}`, 'win');
  showToast(`🏆 Grid WIN! ${s}$${data.pnl.toFixed(2)}`, 'win');
  flashElement('mcEquity');
});

socket.on('max_drawdown_hit', (data) => {
  addLog(`🛑 MAX DRAWDOWN HIT: $${data.drawdown.toFixed(2)} — Bot stopped!`, 'loss');
  showToast('🛑 Max drawdown hit! Bot stopped.', 'loss');
  setBotRunning(false);
});

socket.on('max_levels_reached', (data) => {
  addLog(`⚠️ Max grid levels (${data.levels}) reached!`, 'warning');
});

socket.on('bot_started', () => { setBotRunning(true); });
socket.on('bot_stopped', (data) => {
  setBotRunning(false);
  updatePositionsTable([]);
  const reason = data && data.reason === 'emergency' ? 'Emergency' : 'Stop';
  addLog(`${reason}: Bot stopped`, 'warning');
  if (data && data.pnl_report) showPnlModal(data.pnl_report);
});

socket.on('positions_closed', (report) => {
  showPnlModal(report);
});

socket.on('bot_reset', (data) => {
  setBotRunning(false); clearCharts(); addLog('Reset: Bot reset', 'warning');
  updatePositionsTable([]); resetMetrics();
  document.getElementById('mt5Bar').style.display = 'none';
  if (data && data.pnl_report) showPnlModal(data.pnl_report);
});

socket.on('log', (data) => {
  addLog(data.msg, data.type || 'info');
});
socket.on('server_log', (data) => {
  addLog('[MT5] ' + data.msg, data.type || 'info');
});

// ─────────────────────────────────────────────
// Mode Toggle
// ─────────────────────────────────────────────
function setMode(mode) {
  currentMode = mode;
  document.getElementById('btnSimMode').classList.toggle('active', mode === 'simulator');
  document.getElementById('btnLiveMode').classList.toggle('active', mode === 'live');

  // Show/hide relevant sections
  document.getElementById('mt5CredsGroup').style.display  = mode === 'live' ? '' : 'none';
  document.getElementById('scenarioGroup').style.display  = mode === 'simulator' ? '' : 'none';
  document.getElementById('simSpeedGroup').style.display  = mode === 'simulator' ? '' : 'none';
}

function setLiveMode(live) {
  if (live) setMode('live');
}

// ─────────────────────────────────────────────
// MT5 Test Connection
// ─────────────────────────────────────────────
async function testMT5() {
  const result = document.getElementById('mt5ConnResult');
  const btnTest = document.getElementById('btnTest');

  const login    = document.getElementById('inputLogin').value;
  const password = document.getElementById('inputPassword').value;
  const server   = document.getElementById('inputServer').value;
  const path     = document.getElementById('inputPath').value;

  if (!login || !password || !server) {
    result.className = 'mt5-conn-result err';
    result.textContent = '⚠ Please fill Login, Password and Server first.';
    return;
  }

  result.className = 'mt5-conn-result testing';
  result.textContent = '🔌 Testing connection to MT5...';
  btnTest.disabled = true;

  try {
    const res = await fetch('/api/mt5/test', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ login: parseInt(login), password, server, path }),
    });
    const data = await res.json();

    if (data.status === 'connected') {
      result.className = 'mt5-conn-result ok';
      const info = data.account;
      result.innerHTML =
        `✅ <strong>Connected!</strong><br>` +
        `Account: ${info.login} | ${info.company}<br>` +
        `Balance: ${info.currency} ${info.balance.toFixed(2)} | Leverage: 1:${info.leverage}<br>` +
        `Server: ${info.server}`;
      showMT5Bar(info);
      if (data.price) {
        updateHeaderPrice(data.price);
      }
      showToast('✅ Connection successful!', 'win');
      addLog(`✅ Test OK: ${info.company} | Acc ${info.login} | Gold: ${data.price ? data.price.toFixed(2) : 'OK'}`, 'win');
    } else {
      result.className = 'mt5-conn-result err';
      result.textContent = '❌ ' + data.message;
      addLog('❌ MT5 Test failed: ' + data.message, 'loss');
    }
  } catch (e) {
    result.className = 'mt5-conn-result err';
    result.textContent = '❌ Network error: ' + e.message;
  } finally {
    btnTest.disabled = false;
  }
}

// ─────────────────────────────────────────────
// Bot Controls
// ─────────────────────────────────────────────
async function startBot() {
  const speedMs = parseInt(document.getElementById('speedSlider').value);
  let body = { mode: currentMode, speed_ms: speedMs };

  if (currentMode === 'simulator') {
    body.start_price = parseFloat(document.getElementById('inputStartPrice')?.value || 2650);
    body.scenario    = document.getElementById('selectScenario').value;
  } else {
    // Live mode — read credentials (optional if already provided at login)
    const login    = document.getElementById('inputLogin').value;
    const password = document.getElementById('inputPassword').value;
    const server   = document.getElementById('inputServer').value;
    const path     = document.getElementById('inputPath').value;

    if (login) body.login = parseInt(login);
    if (password) body.password = password;
    if (server) body.server = server;
    if (path) body.path = path;
    body.tick_ms  = 500;

    // Save creds in localStorage for convenience
    if (login)  localStorage.setItem('mt5_login',  login);
    if (server) localStorage.setItem('mt5_server', server);
    if (path)   localStorage.setItem('mt5_path',   path);
  }

  addLog(`▶ Starting bot in ${currentMode} mode...`, 'info');

  try {
    const res = await fetch('/api/start', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body),
    });
    const data = await res.json();
    if (data.status === 'started') {
      setBotRunning(true);
      if (data.price && data.price > 0) {
        updateHeaderPrice(data.price);
      }
      if (data.mode === 'live' && data.account) {
        showMT5Bar(data.account);
        addLog(`▶ Live Bot started | ${data.account.company} @ ${data.price ? data.price.toFixed(2) : 'Live'}`, 'open');
        showToast('▶ Live Bot started!', 'win');
      } else {
        addLog(`▶ Simulator started @ ${data.price}`, 'info');
      }
    } else {
      addLog('❌ Start failed: ' + data.message, 'loss');
      showToast('❌ ' + data.message, 'loss');
    }
  } catch (e) {
    addLog('❌ Network error: ' + e.message, 'loss');
  }
}

async function stopBot() {
  const confirmed = confirm('Stop bot? All open MT5 positions will be CLOSED.');
  if (!confirmed) return;

  addLog('Stopping bot and closing all positions...', 'warning');
  document.getElementById('btnStop').disabled = true;

  try {
    const res  = await fetch('/api/stop', { method: 'POST' });
    const data = await res.json();
    setBotRunning(false);
    if (data.pnl_report) {
      showPnlModal(data.pnl_report);
    } else {
      addLog('Bot stopped (simulator mode)', 'warning');
    }
  } catch (e) {
    addLog('Stop error: ' + e.message, 'loss');
  }
}

async function resetBot() {
  if (!confirm('Reset bot and clear all history?')) return;
  try {
    await fetch('/api/reset', { method: 'POST' });
    addLog('↺ Reset sent', 'warning');
  } catch (e) {
    addLog('❌ Reset error: ' + e.message, 'loss');
  }
}

async function emergencyClose() {
  if (!confirm('EMERGENCY: Close ALL open positions on MT5 RIGHT NOW?')) return;

  addLog('EMERGENCY CLOSE — closing all positions...', 'loss');
  try {
    const res  = await fetch('/api/emergency_close', { method: 'POST' });
    const data = await res.json();
    if (data.status === 'closed') {
      showToast('Emergency close executed!', 'loss');
      setBotRunning(false);
      if (data.pnl_report) showPnlModal(data.pnl_report);
    } else {
      addLog('Emergency close failed: ' + data.message, 'loss');
      showToast('Failed: ' + data.message, 'loss');
    }
  } catch (e) {
    addLog('Emergency close error: ' + e.message, 'loss');
  }
}

// ─────────────────────────────────────────────
// UI Helpers
// ─────────────────────────────────────────────
function showMT5Bar(info) {
  document.getElementById('mt5Bar').style.display     = 'flex';
  document.getElementById('mt5Login').textContent     = '#' + info.login;
  document.getElementById('mt5Company').textContent   = info.company || '—';
  document.getElementById('mt5Server').textContent    = info.server || '—';
  document.getElementById('mt5Leverage').textContent  = '1:' + (info.leverage || '—');
  document.getElementById('mt5Currency').textContent  = info.currency || '—';
  document.getElementById('mt5FreeMargin').textContent= info.free_margin
    ? info.currency + ' ' + info.free_margin.toFixed(2) : '—';
}

function loadSavedCreds() {
  const login  = localStorage.getItem('mt5_login');
  const server = localStorage.getItem('mt5_server');
  const path   = localStorage.getItem('mt5_path');
  if (login)  document.getElementById('inputLogin').value  = login;
  if (server) document.getElementById('inputServer').value = server;
  if (path)   document.getElementById('inputPath').value   = path;
}

function updateMetrics(data) {
  setText('mBalance', '$' + fmtNum(data.balance));
  setText('mEquity',  '$' + fmtNum(data.equity));

  const unr   = data.unrealized_pnl || 0;
  const unrEl = document.getElementById('mUnrealized');
  unrEl.textContent = (unr >= 0 ? '+$' : '-$') + Math.abs(unr).toFixed(2);
  unrEl.className   = 'metric-value ' + (unr >= 0 ? 'profit' : 'loss');

  setText('mProfit',  '+$' + fmtNum(data.total_profit));
  setText('mLevels',  `${data.grid_levels || 0} / ${data.max_levels || 9}`);
  setText('mWinLoss', `${data.win_count || 0} / ${data.loss_count || 0}`);

  // TP Target card
  if (data.tp_target && data.tp_target > 0) {
    setText('mTpTarget', data.tp_target.toFixed(2));
    setText('mTpRule',   data.tp_rule || '');
  }
}

function updateHeaderPrice(price) {
  document.getElementById('headerPrice').textContent = price ? price.toFixed(2) : '— —';
}

function updatePositionsTable(trades) {
  const tbody   = document.getElementById('positionsBody');
  const countEl = document.getElementById('posCount');
  countEl.textContent = `${trades.length} open`;

  if (!trades.length) {
    tbody.innerHTML = '<tr class="empty-row"><td colspan="9">No open positions · Waiting for bot to start</td></tr>';
    return;
  }

  const curPrice = parseFloat(document.getElementById('headerPrice').textContent) || 0;
  tbody.innerHTML = trades.map(t => {
    const pnlPts = t.direction === 'BUY' ? curPrice - t.entry_price : t.entry_price - curPrice;
    const pnlUsd = pnlPts * 10 * t.lot_size;
    const c      = pnlPts >= 0 ? 'pnl-pos' : 'pnl-neg';
    const s      = pnlPts >= 0 ? '+' : '';
    return `<tr>
      <td><span class="level-badge">${t.level}</span></td>
      <td><span class="dir-badge dir-${t.direction.toLowerCase()}">${t.direction}</span></td>
      <td>${t.entry_price.toFixed(2)}</td>
      <td><strong>${t.lot_size.toFixed(2)}</strong></td>
      <td>${t.take_profit.toFixed(2)}</td>
      <td class="${c}">${s}${pnlPts.toFixed(1)}</td>
      <td class="${c}">${s}$${Math.abs(pnlUsd).toFixed(2)}</td>
      <td>${t.opened_at}</td>
      <td><span class="status-open"></span></td>
    </tr>`;
  }).join('');
}

function setBotRunning(running) {
  botRunning = running;
  const badge    = document.getElementById('botStatusBadge');
  const btnStart = document.getElementById('btnStart');
  const btnStop  = document.getElementById('btnStop');
  badge.textContent = running ? 'RUNNING' : 'STOPPED';
  badge.className   = 'bot-status-badge ' + (running ? 'running' : 'stopped');
  btnStart.disabled = running;
  btnStop.disabled  = !running;
}

function setConnectionStatus(connected) {
  const status  = document.getElementById('connStatus');
  status.innerHTML = `<span class="conn-dot ${connected ? 'connected' : 'disconnected'}"></span> ${connected ? 'Connected' : 'Disconnected'}`;
}

function setText(id, val) {
  const el = document.getElementById(id);
  if (el) el.textContent = val;
}

function fmtNum(n) {
  if (n === undefined || n === null) return '0.00';
  return parseFloat(n).toLocaleString('en-US', { minimumFractionDigits:2, maximumFractionDigits:2 });
}

function flashElement(id) {
  const el = document.getElementById(id);
  if (!el) return;
  el.classList.remove('flash');
  void el.offsetWidth;
  el.classList.add('flash');
}

function pushDatapoint(chart, label, values) {
  chart.data.labels.push(label);
  values.forEach((v,i) => chart.data.datasets[i].data.push(v));
  if (chart.data.labels.length > MAX_POINTS) {
    chart.data.labels.shift();
    chart.data.datasets.forEach(ds => ds.data.shift());
  }
  chart.update('none');
}

function clearCharts() {
  [equityChart, priceChart].forEach(c => {
    c.data.labels = [];
    c.data.datasets.forEach(ds => ds.data = []);
    c.update('none');
  });
}

function resetMetrics() {
  setText('mBalance', '$10,000.00');
  setText('mEquity',  '$10,000.00');
  const u = document.getElementById('mUnrealized');
  if (u) { u.textContent = '$0.00'; u.className = 'metric-value'; }
  setText('mProfit',  '$0.00');
  setText('mLevels',  '0 / 8');
  setText('mWinLoss', '0 / 0');
  updateHeaderPrice(null);
}

// ─────────────────────────────────────────────
// Log
// ─────────────────────────────────────────────
function addLog(msg, type = 'info') {
  const wrap = document.getElementById('logWrap');
  const ts   = new Date().toLocaleTimeString('en-US', { hour12: false });
  const div  = document.createElement('div');
  div.className  = `log-entry ${type}`;
  div.textContent = `[${ts}] ${msg}`;
  wrap.appendChild(div);
  wrap.scrollTop = wrap.scrollHeight;
  while (wrap.children.length > 300) wrap.removeChild(wrap.firstChild);
}

function clearLog() {
  document.getElementById('logWrap').innerHTML = '';
  addLog('Log cleared', 'info');
}

// ─────────────────────────────────────────────
// Toast
// ─────────────────────────────────────────────
let toastTimer;
function showToast(msg, type = 'info') {
  const el = document.getElementById('toast');
  el.textContent  = msg;
  el.className    = `toast ${type} show`;
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => el.classList.remove('show'), 3500);
}

// ─────────────────────────────────────────────
// Speed Slider
// ─────────────────────────────────────────────
function initSpeedSlider() {
  const slider = document.getElementById('speedSlider');
  const val    = document.getElementById('speedVal');
  slider.addEventListener('input', () => { val.textContent = slider.value + 'ms'; });
}

// ─────────────────────────────────────────────
// Clock
// ─────────────────────────────────────────────
function startClock() {
  setInterval(() => {
    document.getElementById('serverTime').textContent =
      new Date().toLocaleTimeString('en-US', { hour12: false });
  }, 1000);
}

// ─────────────────────────────────────────────
// Initial state fetch
// ─────────────────────────────────────────────
async function fetchInitialState() {
  try {
    const res  = await fetch('/api/state');
    const data = await res.json();
    if (data && !data.error) {
      updateMetrics(data);
      updatePositionsTable(data.open_trades || []);
      if (data.is_running) setBotRunning(true);
      if (data.mode === 'live') setMode('live');
    }
  } catch (e) { /* server not yet up */ }
}


// ─────────────────────────────────────────────
// P/L Close Report Modal
// ─────────────────────────────────────────────
function showPnlModal(report) {
  if (!report) return;

  const totalPnl    = report.total_pnl || 0;
  const isProfit    = totalPnl >= 0;
  const pnlColor    = isProfit ? '#22d3a0' : '#ff5e7a';
  const pnlSign     = isProfit ? '+' : '';
  const trades      = report.closed || [];
  const reason      = report.reason || 'Stop';
  const ts          = report.timestamp || '';

  const rows = trades.map(t => {
    const pnl   = t.pnl || 0;
    const color = pnl >= 0 ? '#22d3a0' : '#ff5e7a';
    const sign  = pnl >= 0 ? '+' : '';
    return `<tr>
      <td>${t.ticket}</td>
      <td><span style="color:${t.direction==='BUY'?'#60a5fa':'#f87171'}">${t.direction}</span></td>
      <td>${t.lot}</td>
      <td>${t.open_price}</td>
      <td>${t.close_price}</td>
      <td style="color:${color};font-weight:700">${sign}$${Math.abs(pnl).toFixed(2)}</td>
      <td style="color:${t.status==='closed'?'#22d3a0':'#ff5e7a'}">${t.status.toUpperCase()}</td>
    </tr>`;
  }).join('');

  // Log summary
  addLog(`--- Positions Closed [${reason}] @ ${ts} ---`, 'warning');
  trades.forEach(t => {
    const sign = t.pnl >= 0 ? '+' : '';
    addLog(`  Ticket ${t.ticket} | ${t.direction} ${t.lot} lots | P/L: ${sign}$${Math.abs(t.pnl).toFixed(2)} | ${t.status.toUpperCase()}`, t.pnl >= 0 ? 'win' : 'loss');
  });
  addLog(`  TOTAL P/L: ${pnlSign}$${Math.abs(totalPnl).toFixed(2)} | ${report.success_count} closed, ${report.failed_count} failed`, isProfit ? 'win' : 'loss');

  // Create modal
  const existing = document.getElementById('pnlModal');
  if (existing) existing.remove();

  const modal = document.createElement('div');
  modal.id = 'pnlModal';
  modal.innerHTML = `
    <div class="pnl-overlay" onclick="closePnlModal()"></div>
    <div class="pnl-modal">
      <div class="pnl-modal-header">
        <div>
          <div class="pnl-modal-title">${reason === 'EmergencyClose' ? 'Emergency Close' : 'Bot Stopped'} — P/L Report</div>
          <div class="pnl-modal-sub">${ts}</div>
        </div>
        <button class="pnl-close-btn" onclick="closePnlModal()">x</button>
      </div>

      <div class="pnl-summary-row">
        <div class="pnl-summary-card">
          <div class="pnl-summary-label">Total P/L</div>
          <div class="pnl-summary-val" style="color:${pnlColor}">${pnlSign}$${Math.abs(totalPnl).toFixed(2)}</div>
        </div>
        <div class="pnl-summary-card">
          <div class="pnl-summary-label">Trades Closed</div>
          <div class="pnl-summary-val" style="color:#60a5fa">${report.success_count} / ${report.total_trades}</div>
        </div>
        <div class="pnl-summary-card">
          <div class="pnl-summary-label">Failed</div>
          <div class="pnl-summary-val" style="color:${report.failed_count>0?'#ff5e7a':'#22d3a0'}">${report.failed_count}</div>
        </div>
        <div class="pnl-summary-card">
          <div class="pnl-summary-label">Result</div>
          <div class="pnl-summary-val" style="color:${pnlColor}">${isProfit ? 'PROFIT' : 'LOSS'}</div>
        </div>
      </div>

      ${trades.length ? `
      <div class="pnl-table-wrap">
        <table class="pnl-table">
          <thead><tr>
            <th>Ticket</th><th>Dir</th><th>Lots</th>
            <th>Open</th><th>Close</th><th>P/L</th><th>Status</th>
          </tr></thead>
          <tbody>${rows}</tbody>
        </table>
      </div>` : '<div style="text-align:center;padding:20px;color:#4a5d80">No open positions were found.</div>'}

      <div style="text-align:center;margin-top:16px">
        <button class="btn btn-start" onclick="closePnlModal()" style="width:120px">OK</button>
      </div>
    </div>`;
  document.body.appendChild(modal);
  showToast(`Bot stopped | P/L: ${pnlSign}$${Math.abs(totalPnl).toFixed(2)}`, isProfit ? 'win' : 'loss');
}

function closePnlModal() {
  const m = document.getElementById('pnlModal');
  if (m) m.remove();
}
