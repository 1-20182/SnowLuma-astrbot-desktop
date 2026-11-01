/* 终端页：实时输出流 + 进程监视（不依赖 app.js 的内部实现，独立开一条 SSE） */
(() => {
  'use strict';

  const MAX_LINES = 3000;
  const PROC_INTERVAL = 1500;

  const $ = (sel) => document.querySelector(sel);
  const view = $('#term-view');
  if (!view) return;

  const stat = $('#term-stat');
  const filterSel = $('#term-filter');
  const autoscroll = $('#term-autoscroll');
  const pauseBtn = $('#term-pause');
  const clearBtn = $('#term-clear');
  const downloadBtn = $('#term-download');
  const procBody = $('#proc-body');
  const procStat = $('#proc-stat');
  const procCmd = $('#proc-cmd');
  const page = $('#page-term');

  /** @type {{ts:number, service:string, level:string, text:string, kind:string, meta?:object}[]} */
  let lines = [];
  let pending = [];
  let rafId = 0;
  let es = null;
  let retry = 0;
  let paused = false;
  let procTimer = null;
  let lastRows = [];

  /* ---------------------------------------------------------------- 工具 */
  const pad = (n) => String(n).padStart(2, '0');
  const fmtClock = (ts) => {
    const d = new Date((ts || Date.now() / 1000) * 1000);
    return `${pad(d.getHours())}:${pad(d.getMinutes())}:${pad(d.getSeconds())}`;
  };
  const fmtUptime = (sec) => {
    const s = Math.max(0, Math.floor(sec));
    if (s < 60) return `${s}s`;
    if (s < 3600) return `${Math.floor(s / 60)}m${pad(s % 60)}s`;
    return `${Math.floor(s / 3600)}h${pad(Math.floor((s % 3600) / 60))}m`;
  };
  const level = (l) => {
    const v = String(l || 'info').toLowerCase();
    if (v === 'warning') return 'warn';
    if (v === 'err' || v === 'critical' || v === 'fatal') return 'error';
    return v;
  };
  const esc = (s) => String(s == null ? '' : s);
  // 子进程输出里常带 ANSI 颜色码、以及它们自己的时间戳（我们已经在行首放了一个）
  const ANSI = /\x1b\[[0-9;?]*[A-Za-z]/g;
  const LEADING_TS = /^\s*\d{2}:\d{2}:\d{2}(?:[.,]\d{1,3})?\s+/;
  function cleanText(raw) {
    return String(raw == null ? '' : raw).replace(ANSI, '').replace(LEADING_TS, '');
  }

  /* --------------------------------------------------------- 行渲染/追加 */
  function lineNode(item) {
    const wrap = document.createElement('div');
    wrap.className = `tline ${item.kind || ''} ${item.level || 'info'}`.trim();
    wrap.dataset.service = item.service || '';
    const ts = document.createElement('span');
    ts.className = 'ts';
    ts.textContent = fmtClock(item.ts);
    const svc = document.createElement('span');
    svc.className = 'svc';
    svc.textContent = item.service || 'console';
    const body = document.createElement('span');
    body.className = 'body';
    body.textContent = item.text;
    wrap.append(ts, svc, body);
    return wrap;
  }

  function visible(item) {
    const want = filterSel ? filterSel.value : '';
    if (!want) return true;
    return (item.service || '') === want;
  }

  function append(item) {
    lines.push(item);
    if (lines.length > MAX_LINES) lines.splice(0, lines.length - MAX_LINES);
    if (visible(item)) pending.push(item);
    if (!rafId) rafId = requestAnimationFrame(flush);
  }

  function flush() {
    rafId = 0;
    if (!pending.length) return;
    const frag = document.createDocumentFragment();
    pending.forEach((item) => frag.append(lineNode(item)));
    pending = [];
    const ph = view.querySelector('.term-placeholder');
    if (ph) ph.remove();
    view.append(frag);
    while (view.children.length > MAX_LINES) view.firstElementChild.remove();
    if (stat) stat.textContent = `${lines.length} 行`;
    if (autoscroll && autoscroll.checked) view.scrollTop = view.scrollHeight;
    scheduleDock();
  }

  function rerender() {
    pending = [];
    view.textContent = '';
    const list = lines.filter(visible);
    if (!list.length) {
      const ph = document.createElement('span');
      ph.className = 'term-placeholder';
      ph.textContent = '暂无输出（按当前过滤条件）';
      view.append(ph);
      return;
    }
    const frag = document.createDocumentFragment();
    list.forEach((item) => frag.append(lineNode(item)));
    view.append(frag);
    if (autoscroll && autoscroll.checked) view.scrollTop = view.scrollHeight;
  }

  /* ------------------------------------------------------------- SSE 接入 */
  function pushLog(ev) {
    append({
      ts: ev.ts,
      service: ev.service || 'console',
      level: level(ev.level),
      kind: 'log',
      text: cleanText(ev.line == null ? '' : ev.line),
    });
  }

  function pushDeploy(ev) {
    const pct = ev.progress == null ? null : Math.round(Number(ev.progress) * 100);
    const head = pct == null ? `[deploy:${ev.step || ''}]` : `[deploy:${ev.step || ''} ${pct}%]`;
    const text = ev.message ? `${head} ${ev.message}` : head;
    append({
      ts: ev.ts, service: 'deploy', level: ev.ok === false ? 'error' : 'deploy',
      kind: 'deploy', text,
    });
    // 一开始安装就自动弹出浮动终端：用户点了"安装"，就该看到它在干活
    if (ev.step === 'start' && !dockOpen && !dockUserClosed) {
      toggleDock(true);
    }
  }

  function pushService(ev) {
    if (!ev.status) return;
    const name = ev.name || 'service';
    const map = { starting: '正在启动', running: '已就绪', stopping: '正在停止', stopped: '已停止', failed: '启动失败' };
    const label = map[ev.status] || ev.status;
    const extra = [ev.pid ? `pid=${ev.pid}` : '', ev.detail || ev.error || ''].filter(Boolean).join(' · ');
    append({
      ts: ev.ts, service: name, level: ev.status === 'failed' ? 'error' : 'service',
      kind: 'service', text: `» ${name} ${label}${extra ? ' · ' + extra : ''}`,
    });
  }

  function pushMessage(ev) {
    const dir = ev.direction === 'out' ? '→ QQ' : '← QQ';
    const who = ev.group_id ? `群 ${ev.group_id}` : ev.user || '';
    append({
      ts: ev.ts, service: 'message', level: ev.ok === false ? 'error' : 'message',
      kind: 'message', text: `${dir} ${who} ${ev.text ? '：' + String(ev.text).slice(0, 160) : ''}`,
    });
  }

  function connect() {
    if (es) es.close();
    es = new EventSource('/api/events');
    es.onopen = () => {
      retry = 0;
    };
    es.onmessage = (msg) => {
      let ev;
      try {
        ev = JSON.parse(msg.data);
      } catch (err) {
        return;
      }
      if (ev.type === 'log') pushLog(ev);
      else if (ev.type === 'deploy') pushDeploy(ev);
      else if (ev.type === 'service') pushService(ev);
      else if (ev.type === 'message') pushMessage(ev);
    };
    es.onerror = () => {
      if (es) es.close();
      es = null;
      retry += 1;
      const wait = Math.min(15000, 1000 * retry);
      append({
        ts: Date.now() / 1000, service: 'console', level: 'warn', kind: 'service',
        text: `» 实时通道断开，${Math.round(wait / 1000)} 秒后重连…`,
      });
      setTimeout(connect, wait);
    };
  }

  async function backfill() {
    try {
      const res = await fetch('/api/logs?limit=400', { cache: 'no-store' });
      const data = await res.json();
      const list = (data && data.logs) || [];
      if (!lines.length && list.length) {
        list.forEach(pushLog);
      }
    } catch (err) {
      /* 忽略：SSE 会补上后续输出 */
    }
  }

  /* ------------------------------------------------------------ 进程监视 */
  function procRow(row) {
    const tr = document.createElement('tr');
    tr.dataset.pid = row.pid;
    if (row.service) tr.dataset.svc = row.service;

    const svcTd = document.createElement('td');
    const badge = document.createElement('span');
    badge.className = `svc-badge ${row.service || 'other'}`;
    badge.textContent = row.service || '—';
    svcTd.append(badge);

    const nameTd = document.createElement('td');
    nameTd.className = 'mono';
    nameTd.textContent = row.name || '?';
    nameTd.title = row.cmdline || '';

    const pidTd = document.createElement('td');
    pidTd.className = 'mono num';
    pidTd.textContent = row.pid;

    const cpuTd = document.createElement('td');
    cpuTd.className = 'num';
    cpuTd.textContent = `${row.cpu}%`;

    const memTd = document.createElement('td');
    memTd.className = 'num';
    memTd.textContent = `${row.mem_mb} MB`;

    const upTd = document.createElement('td');
    upTd.className = 'num';
    upTd.textContent = fmtUptime(Date.now() / 1000 - row.created);

    tr.append(svcTd, nameTd, pidTd, cpuTd, memTd, upTd);
    return tr;
  }

  async function pollProcs() {
    try {
      const res = await fetch('/api/procs', { cache: 'no-store' });
      const data = await res.json();
      const rows = (data && data.processes) || [];
      lastRows = rows;
      procBody.textContent = '';
      if (!rows.length) {
        const tr = document.createElement('tr');
        const td = document.createElement('td');
        td.colSpan = 6;
        td.className = 'muted';
        td.textContent = data && data.error ? data.error : '没有子进程（服务都还没启动）';
        tr.append(td);
        procBody.append(tr);
      } else {
        rows.forEach((row) => procBody.append(procRow(row)));
      }
      const mem = rows.reduce((acc, r) => acc + (r.mem_mb || 0), 0);
      if (procStat) procStat.textContent = `${rows.length} 个进程 · 共 ${mem.toFixed(0)} MB`;
      if (procCmd) {
        procCmd.textContent = rows.map((r) => `[${r.pid}] ${r.cmdline || r.name}`).join('\n') || '—';
      }
    } catch (err) {
      if (procStat) procStat.textContent = '读取进程失败';
    }
  }

  function startProcs() {
    stopProcs();
    pollProcs();
    procTimer = setInterval(() => {
      if (document.hidden) return;
      pollProcs();
    }, PROC_INTERVAL);
  }

  function stopProcs() {
    if (procTimer) clearInterval(procTimer);
    procTimer = null;
  }

  /* -------------------------------------------------------------- 交互 */
  if (filterSel) {
    filterSel.addEventListener('change', rerender);
  }
  if (autoscroll) {
    autoscroll.addEventListener('change', () => {
      paused = !autoscroll.checked;
      if (pauseBtn) pauseBtn.textContent = paused ? '继续滚动' : '暂停滚动';
      if (autoscroll.checked) view.scrollTop = view.scrollHeight;
    });
  }
  /* ------------------------------------------------------ 浮动终端窗（dock） */
  const dockBtn = $('#btn-term-dock');
  let dock = null;
  let dockView = null;
  let dockStatEl = null;
  let dockOpen = false;
  let dockUserClosed = false;
  let dockTimer = 0;

  function ensureDock() {
    if (dock) return dock;
    dock = document.createElement('section');
    dock.id = 'term-dock';
    dock.hidden = true;
    dock.setAttribute('aria-label', '浮动终端');

    const head = document.createElement('header');
    const title = document.createElement('span');
    title.className = 'dock-title';
    title.textContent = '终端';
    dockStatEl = document.createElement('span');
    dockStatEl.className = 'dock-stat';
    dockStatEl.textContent = '0 行';
    const spacer = document.createElement('span');
    spacer.className = 'dock-spacer';

    const expand = document.createElement('button');
    expand.type = 'button';
    expand.className = 'dock-btn';
    expand.textContent = '展开';
    expand.title = '在「终端」页查看（含进程监视）';
    expand.addEventListener('click', () => {
      toggleDock(false);
      location.hash = 'term';
    });

    const close = document.createElement('button');
    close.type = 'button';
    close.className = 'dock-btn';
    close.textContent = '×';
    close.title = '关闭（Alt+T）';
    close.addEventListener('click', () => {
      dockUserClosed = true;
      toggleDock(false);
    });

    head.append(title, dockStatEl, spacer, expand, close);

    dockView = document.createElement('pre');
    dockView.className = 'dock-view';
    dock.append(head, dockView);
    document.body.append(dock);
    return dock;
  }

  function renderDock() {
    dockTimer = 0;
    if (!dockOpen || !dockView) return;
    const list = lines.filter(visible).slice(-200);
    dockView.textContent = '';
    if (!list.length) {
      const ph = document.createElement('span');
      ph.className = 'term-placeholder';
      ph.textContent = '等待输出……';
      dockView.append(ph);
    } else {
      const frag = document.createDocumentFragment();
      list.forEach((item) => frag.append(lineNode(item)));
      dockView.append(frag);
    }
    dockView.scrollTop = dockView.scrollHeight;
    if (dockStatEl) dockStatEl.textContent = `${lines.length} 行`;
  }

  function toggleDock(force) {
    ensureDock();
    dockOpen = typeof force === 'boolean' ? force : !dockOpen;
    dock.hidden = !dockOpen;
    if (dockBtn) dockBtn.classList.toggle('on', dockOpen);
    if (dockOpen) renderDock();
  }

  function scheduleDock() {
    if (!dockOpen || dockTimer) return;
    dockTimer = setTimeout(renderDock, 220);
  }

  if (dockBtn) {
    dockBtn.addEventListener('click', () => {
      if (dockOpen) dockUserClosed = true;
      toggleDock();
    });
  }
  document.addEventListener('keydown', (ev) => {
    if (ev.altKey && (ev.key === 't' || ev.key === 'T')) {
      ev.preventDefault();
      if (dockOpen) dockUserClosed = true;
      toggleDock();
    } else if (ev.key === 'Escape' && dockOpen) {
      dockUserClosed = true;
      toggleDock(false);
    }
  });

  if (pauseBtn) {
    pauseBtn.addEventListener('click', () => {
      paused = !paused;
      if (autoscroll) autoscroll.checked = !paused;
      pauseBtn.textContent = paused ? '继续滚动' : '暂停滚动';
      if (!paused) view.scrollTop = view.scrollHeight;
    });
  }
  if (clearBtn) {
    clearBtn.addEventListener('click', () => {
      lines = [];
      pending = [];
      view.textContent = '';
      if (stat) stat.textContent = '0 行';
      const ph = document.createElement('span');
      ph.className = 'term-placeholder';
      ph.textContent = '已清屏，后续输出会继续出现。';
      view.append(ph);
    });
  }
  if (downloadBtn) {
    downloadBtn.addEventListener('click', () => {
      const text = lines
        .map((l) => `${new Date(l.ts * 1000).toLocaleString()} [${l.service}/${l.level}] ${l.text}`)
        .join('\n');
      const blob = new Blob([text || '（暂无输出）'], { type: 'text/plain;charset=utf-8' });
      const a = document.createElement('a');
      const d = new Date();
      const stamp = `${d.getFullYear()}${pad(d.getMonth() + 1)}${pad(d.getDate())}-${pad(d.getHours())}${pad(d.getMinutes())}${pad(d.getSeconds())}`;
      a.href = URL.createObjectURL(blob);
      a.download = `slab-terminal-${stamp}.log`;
      a.click();
      setTimeout(() => URL.revokeObjectURL(a.href), 4000);
    });
  }

  /* 页面进入/离开：只在终端页轮询进程，省 CPU */
  window.addEventListener('slab:term-open', () => {
    startProcs();
    if (autoscroll && autoscroll.checked) view.scrollTop = view.scrollHeight;
  });
  document.addEventListener('visibilitychange', () => {
    if (document.hidden) stopProcs();
    else if (page && !page.hidden) startProcs();
  });

  /* ----------------------------------------------------------------- 启动 */
  connect();
  backfill();
  if (page && !page.hidden) startProcs();
  window.addEventListener('beforeunload', () => {
    if (es) es.close();
    stopProcs();
  });
})();
