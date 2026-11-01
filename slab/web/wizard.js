/* 首次启动向导：环境检查 → 安装 → 填写 → 启动（独立脚本，复用后端已有的 REST/SSE） */
(() => {
  'use strict';

  const DONE_KEY = 'slab-wizard-done';
  const $ = (sel, root) => (root || document).querySelector(sel);
  const $$ = (sel, root) => Array.from((root || document).querySelectorAll(sel));

  const root = $('#wizard');
  if (!root) return;

  let step = 1;
  let deploying = false;
  let installed = { snowluma: false, astrbot: false };
  let es = null;

  /* ------------------------------------------------------------ 基础工具 */
  async function api(path, options) {
    const init = Object.assign({ cache: 'no-store' }, options || {});
    if (init.body && typeof init.body !== 'string') {
      init.headers = Object.assign({ 'Content-Type': 'application/json' }, init.headers || {});
      init.body = JSON.stringify(init.body);
    }
    const res = await fetch(path, init);
    let data = null;
    try { data = await res.json(); } catch (err) { data = null; }
    if (!res.ok) {
      const msg = (data && (data.error || data.message)) || `HTTP ${res.status}`;
      throw new Error(msg);
    }
    return data;
  }

  function setHint(text, kind) {
    const el = $('#wiz-hint');
    if (!el) return;
    el.textContent = text || '';
    el.dataset.kind = kind || '';
  }

  function goto(next) {
    step = Math.max(1, Math.min(4, next));
    $$('.wiz-pane', root).forEach((pane) => { pane.hidden = Number(pane.dataset.pane) !== step; });
    $$('.wizard-step', root).forEach((item) => {
      const n = Number(item.dataset.step);
      item.classList.toggle('done', n < step);
      item.classList.toggle('active', n === step);
    });
    $('#wiz-prev').disabled = step === 1;
    const nextBtn = $('#wiz-next');
    nextBtn.textContent = step === 4 ? '开始使用' : '下一步';
    nextBtn.disabled = false;
    if (step === 1) runChecks();
    if (step === 2) refreshInstallState();
    if (step === 3) fillDefaults();
    if (step === 4) prepareFinish();
    setHint('');
  }

  function open() {
    root.hidden = false;
    document.body.classList.add('wizard-open');
    goto(1);
  }

  function close(markDone) {
    root.hidden = true;
    document.body.classList.remove('wizard-open');
    if (es) { es.close(); es = null; }
    if (markDone) {
      try { localStorage.setItem(DONE_KEY, '1'); } catch (err) {}
    }
  }

  /* -------------------------------------------------------- 1. 环境检查 */
  async function runChecks() {
    const list = $('#wiz-checks');
    list.textContent = '';
    const add = (ok, label, detail) => {
      const li = document.createElement('li');
      li.className = ok ? 'ok' : 'warn';
      li.innerHTML = '';
      const mark = document.createElement('i');
      mark.textContent = ok ? '✓' : '!';
      const tx = document.createElement('div');
      const b = document.createElement('b');
      b.textContent = label;
      const small = document.createElement('small');
      small.textContent = detail || '';
      tx.append(b, small);
      li.append(mark, tx);
      list.append(li);
    };
    add(true, '正在检查…', '');
    try {
      const env = await api('/api/deploy/env');
      list.textContent = '';
      const tools = env.tools || {};
      add(!!(tools.python && tools.python.ok), 'Python', (tools.python && tools.python.version) || '未找到');
      add(!!(tools.git && tools.git.ok) || !!(tools.uv && tools.uv.ok),
          'Git / uv', [tools.git && tools.git.version, tools.uv && tools.uv.version].filter(Boolean).join(' · ') || '都没有也能用 zip 包安装');
      add(env.free_space_gb > 3, '磁盘空间', `${env.free_space_gb} GB 可用（建议 ≥ 3 GB）`);
      const ports = env.ports || {};
      // console 是控制台自己占的，不算冲突
      const busy = Object.keys(ports).filter((k) => k !== 'console' && ports[k] && ports[k].in_use);
      add(!busy.length, '端口', busy.length ? `被占用：${busy.join(', ')}（下一步可以一键换端口）` : '8790 / 6185 / 5099 / 6199 都空闲');
      (env.warnings || []).forEach((w) => add(false, '提醒', w));
    } catch (err) {
      list.textContent = '';
      add(false, '检查失败', String(err.message || err));
    }
  }

  /* ---------------------------------------------------------- 2. 安装 */
  function progressBar(percent) {
    const bar = $('#wiz-progress-bar');
    if (bar) bar.style.width = `${Math.max(0, Math.min(100, percent))}%`;
  }

  function logLine(text) {
    const box = $('#wiz-deploy-log');
    if (!box) return;
    const line = document.createElement('div');
    line.textContent = text;
    box.append(line);
    while (box.children.length > 120) box.firstElementChild.remove();
    box.scrollTop = box.scrollHeight;
  }

  async function refreshInstallState() {
    try {
      const state = await api('/api/state');
      const env = state.env || {};
      installed = (env.installed) || installed;
      renderInstallState();
    } catch (err) { /* ignore */ }
  }

  function renderInstallState() {
    [['snowluma', '#wiz-snowluma-state'], ['astrbot', '#wiz-astrbot-state']].forEach(([key, sel]) => {
      const el = $(sel);
      if (!el) return;
      el.textContent = installed[key] ? '已安装' : '未安装';
      el.className = `badge ${installed[key] ? 'ok' : 'muted'}`;
    });
    const nextBtn = $('#wiz-next');
    if (step === 2) nextBtn.disabled = !(installed.snowluma && installed.astrbot);
  }

  function subscribeDeploy() {
    if (es) return;
    es = new EventSource('/api/events');
    es.onmessage = (msg) => {
      let ev;
      try { ev = JSON.parse(msg.data); } catch (err) { return; }
      if (ev.type === 'deploy') {
        const pct = ev.progress == null ? null : Math.round(Number(ev.progress) * 100);
        if (pct != null) progressBar(pct);
        if (ev.message) logLine(`[${ev.step || ''}] ${ev.message}`);
        if (ev.step === 'finished') {
          logLine(ev.ok ? `✔ ${ev.target} 完成` : `✘ ${ev.target} 失败`);
          refreshInstallState();
        }
      } else if (ev.type === 'log' && ev.service === 'deploy') {
        logLine(ev.line);
      }
    };
    es.onerror = () => { if (es) { es.close(); es = null; } };
  }

  async function startInstall() {
    if (deploying) return;
    deploying = true;
    subscribeDeploy();
    const btn = $('#wiz-install');
    btn.disabled = true;
    btn.textContent = '正在安装…';
    setHint('安装期间可以看实时输出，或稍后到「终端」页查看', 'info');
    try {
      await api('/api/deploy/both', { method: 'POST', body: {} });
      logLine('已开始安装 SnowLuma 与 AstrBot…');
      const timer = setInterval(async () => {
        await refreshInstallState();
        if (installed.snowluma && installed.astrbot) {
          clearInterval(timer);
          deploying = false;
          btn.textContent = '已安装';
          progressBar(100);
          setHint('两个项目都装好了，点「下一步」填写配置', 'ok');
        }
      }, 2500);
    } catch (err) {
      deploying = false;
      btn.disabled = false;
      btn.textContent = '开始安装';
      setHint(`安装失败：${err.message || err}`, 'error');
    }
  }

  /* -------------------------------------------------------- 3. 填写配置 */
  let defaultsFilled = false;
  async function fillDefaults() {
    if (defaultsFilled) return;
    defaultsFilled = true;
    try {
      const state = await api('/api/state');
      const cfg = state.config || {};
      const qq = (cfg.snowluma && cfg.snowluma.qq_uin) || '';
      const admins = (cfg.astrbot && cfg.astrbot.admins_id) || [];
      const provider = (cfg.astrbot && cfg.astrbot.provider) || {};
      if (qq) $('#wiz-qq').value = qq;
      if (admins.length) $('#wiz-admin').value = admins.join(', ');
      if (provider.base_url) $('#wiz-base-url').value = provider.base_url;
      if (provider.model) $('#wiz-model').value = provider.model;
      renderPortState(state.ports || {});
    } catch (err) { /* ignore */ }
  }

  function renderPortState(ports) {
    const box = $('#wiz-ports');
    if (!box) return;
    box.textContent = '';
    Object.keys(ports).forEach((key) => {
      const info = ports[key] || {};
      const chip = document.createElement('span');
      chip.className = `wiz-chip ${info.in_use ? 'busy' : 'free'}`;
      chip.textContent = `${key} ${info.port || ''}${info.in_use ? ' 占用' : ' 空闲'}`;
      box.append(chip);
    });
  }

  async function fixPorts() {
    const btn = $('#wiz-fix-ports');
    btn.disabled = true;
    try {
      const res = await api('/api/system/fix-ports', { method: 'POST' });
      setHint(res.message || '端口检查完成', res.changes && res.changes.length ? 'ok' : 'info');
      (res.changes || []).forEach((c) => logLine(`端口 ${c.label}：${c.from} → ${c.to}（${c.reason}）`));
      const state = await api('/api/state');
      renderPortState(state.ports || {});
    } catch (err) {
      setHint(`换端口失败：${err.message || err}`, 'error');
    } finally {
      btn.disabled = false;
    }
  }

  async function saveWizardConfig() {
    const qq = $('#wiz-qq').value.trim();
    const admin = $('#wiz-admin').value.trim() || qq;
    const key = $('#wiz-api-key').value.trim();
    const model = $('#wiz-model').value.trim() || 'deepseek-chat';
    const baseUrl = $('#wiz-base-url').value.trim() || 'https://api.deepseek.com/v1';
    if (!/^\d{5,}$/.test(qq)) throw new Error('QQ 号看起来不对（应该是 5 位以上数字）');
    if (key.length < 8) throw new Error('大模型 API Key 太短，请检查是否完整');
    await api('/api/config', {
      method: 'PUT',
      body: {
        snowluma: { qq_uin: qq },
        astrbot: {
          admins_id: admin.split(/[,，\s]+/).filter(Boolean),
          provider: { base_url: baseUrl, model, api_key: key },
        },
      },
    });
  }

  /* ------------------------------------------------------------ 4. 启动 */
  function prepareFinish() {
    const box = $('#wiz-finish-log');
    if (box) box.textContent = '';
  }

  async function finish() {
    const nextBtn = $('#wiz-next');
    nextBtn.disabled = true;
    const log = (t) => {
      const box = $('#wiz-finish-log');
      if (box) {
        const line = document.createElement('div');
        line.textContent = t;
        box.append(line);
      }
    };
    try {
      log('正在把配置写入 SnowLuma / AstrBot…');
      const applied = await api('/api/link/apply', { method: 'POST', body: { restart: false, require_provider: true } });
      (applied.steps || []).forEach((s) => log(`${s.ok ? '✔' : '✘'} ${s.step}：${s.detail || ''}`));
      log('正在启动服务…');
      await api('/api/services/all/start', { method: 'POST' });
      log('✔ 已启动。回到 QQ 私聊机器人，或群里 @它试试。');
      setHint('完成后这个页面会自动刷新', 'ok');
      try { localStorage.setItem(DONE_KEY, '1'); } catch (err) {}
      setTimeout(() => { window.location.hash = '#run'; window.location.reload(); }, 1500);
    } catch (err) {
      log(`✘ ${err.message || err}`);
      setHint(`启动失败：${err.message || err}`, 'error');
      nextBtn.disabled = false;
    }
  }

  /* ------------------------------------------------------------ 事件绑定 */
  $('#wiz-skip') && $('#wiz-skip').addEventListener('click', () => close(true));
  $('#wiz-prev') && $('#wiz-prev').addEventListener('click', () => goto(step - 1));
  $('#wiz-install') && $('#wiz-install').addEventListener('click', startInstall);
  $('#wiz-fix-ports') && $('#wiz-fix-ports').addEventListener('click', fixPorts);
  $('#wiz-next') && $('#wiz-next').addEventListener('click', async () => {
    if (step === 3) {
      const btn = $('#wiz-next');
      btn.disabled = true;
      try {
        await saveWizardConfig();
        goto(4);
      } catch (err) {
        setHint(`保存失败：${err.message || err}`, 'error');
      } finally {
        btn.disabled = false;
      }
      return;
    }
    if (step === 4) { finish(); return; }
    goto(step + 1);
  });
  document.addEventListener('keydown', (ev) => {
    if (ev.key === 'Escape' && !root.hidden) close(false);
  });

  // 首次启动自动弹出：没装完 / 没填 Key 且没标记过"已完成"
  (async function autoOpen() {
    let done = false;
    try { done = localStorage.getItem(DONE_KEY) === '1'; } catch (err) {}
    // ?wizard=0 可强制不弹（截图/演示/调试用）
    try {
      const q = new URLSearchParams(location.search);
      if (q.get('wizard') === '0') return;
    } catch (err) { /* 忽略 */ }
    if (done) return;
    try {
      const state = await api('/api/state');
      const env = state.env || {};
      installed = env.installed || installed;
      const cfg = state.config || {};
      const hasKey = !!(cfg.astrbot && cfg.astrbot.provider && cfg.astrbot.provider.api_key === '***');
      const readyToGo = installed.snowluma && installed.astrbot && hasKey;
      if (!readyToGo) open();
    } catch (err) { /* 后端还没就绪就不弹 */ }
  }());
})();
