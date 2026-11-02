/* =========================================================================
 * SnowLuma × AstrBot 联合控制台 —— 前端逻辑（原生 JS，无框架、无构建、无 CDN）
 *
 * 数据来源：
 *   1) SSE  /api/events  —— 实时日志 / 服务状态 / 部署进度 / 收发消息
 *   2) REST /api/*       —— 状态快照、配置读写、服务启停、部署、联调
 *
 * 约定：所有网络请求都走 api()，失败统一抛 ApiError，界面上用 toast 中文提示。
 * ========================================================================= */
'use strict';

(function () {
  /* ====================================================================== *
   * 0. 基础工具
   * ====================================================================== */
  const $ = (sel, root) => (root || document).querySelector(sel);
  const $$ = (sel, root) => Array.from((root || document).querySelectorAll(sel));

  /** 创建 DOM 节点：el('div', {class:'x', text:'hi'}, [child, '文字']) */
  function el(tag, attrs, children) {
    const node = document.createElement(tag);
    if (attrs) {
      for (const key of Object.keys(attrs)) {
        const val = attrs[key];
        if (val === null || val === undefined || val === false) continue;
        if (key === 'class') node.className = val;
        else if (key === 'text') node.textContent = String(val);
        else if (key === 'dataset') Object.assign(node.dataset, val);
        else if (key === 'style') node.setAttribute('style', val);
        else if (key.slice(0, 2) === 'on' && typeof val === 'function') {
          node.addEventListener(key.slice(2).toLowerCase(), val);
        } else node.setAttribute(key, val === true ? '' : String(val));
      }
    }
    append(node, children);
    return node;
  }

  function append(node, children) {
    if (children === null || children === undefined || children === false) return node;
    const list = Array.isArray(children) ? children : [children];
    for (const child of list) {
      if (child === null || child === undefined || child === false || child === '') continue;
      node.append(child instanceof Node ? child : document.createTextNode(String(child)));
    }
    return node;
  }

  function clear(node) {
    while (node.firstChild) node.removeChild(node.firstChild);
    return node;
  }

  function pad2(n) { return String(n).padStart(2, '0'); }

  function fmtClock(ts) {
    if (!ts) return '--:--:--';
    const d = new Date(Number(ts) * 1000);
    if (Number.isNaN(d.getTime())) return '--:--:--';
    return `${pad2(d.getHours())}:${pad2(d.getMinutes())}:${pad2(d.getSeconds())}`;
  }

  function fmtUptime(sec) {
    const s = Math.max(0, Math.round(Number(sec) || 0));
    if (s < 60) return `${s} 秒`;
    if (s < 3600) return `${Math.floor(s / 60)} 分 ${s % 60} 秒`;
    const h = Math.floor(s / 3600);
    return `${h} 时 ${Math.floor((s % 3600) / 60)} 分`;
  }

  function setText(node, text) {
    if (node && node.textContent !== String(text)) node.textContent = String(text);
  }

  function getPath(obj, path) {
    let cur = obj;
    for (const part of String(path).split('.')) {
      if (cur === null || cur === undefined || typeof cur !== 'object') return undefined;
      cur = cur[part];
    }
    return cur;
  }

  /* ====================================================================== *
   * 1. 全局状态
   * ====================================================================== */
  const S = {
    version: '',
    uptime: 0,
    config: null,
    secrets: {},
    services: {},
    env: null,
    ports: {},
    paths: {},
    storage: {},
    issues: [],
    link: null,
    linkErr: null,
    whoami: null,
    messages: [],
    connected: false,
    configDirty: false,
    configFilled: false,
    busy: new Set(),
    deploy: { snowluma: null, astrbot: null },
    lastStateAt: 0,
  };

  const PAGE_KEYS = ['overview', 'deploy', 'config', 'run', 'link', 'term'];
  const MAX_LOG_LINES = 500;
  const MAX_MESSAGES = 100;

  /* 服务展示元数据（后端未注册时也能显示中文名） */
  const SVC_META = {
    astrbot: {
      title: 'AstrBot',
      desc: '大模型机器人框架：收到消息后调用大模型生成回复。',
      panelLabel: '打开 AstrBot 面板',
      portKey: 'astrbot_webui',
    },
    snowluma: {
      title: 'SnowLuma',
      desc: 'QQ 协议端：登录 QQ、收发消息，并转成 OneBot v11 事件。',
      panelLabel: '打开 SnowLuma 面板',
      portKey: 'snowluma_webui',
    },
    bridge: {
      title: '中继服务',
      desc: '控制台内置的 OneBot 中继，仅在「内置中继」联动模式下需要。',
      panelLabel: '',
      portKey: '',
    },
  };

  const PORT_LABELS = {
    console: '控制台',
    astrbot_webui: 'AstrBot 面板',
    snowluma_webui: 'SnowLuma 面板',
    onebot: 'OneBot 联动',
  };

  /* ====================================================================== *
   * 1.5 平台层（前端侧）
   *
   * 规矩：平台判定**只读后端**返回的字段（envcheck.env_report() → /api/state 的 env）：
   *   env.is_linux / env.is_windows  →  env.tag / env.platform_tag  →  env.os  →  env.platform
   * 前端**不嗅探**系统（不看浏览器 UA 字符串、不看 navigator 的平台字段），判不出来就用中性文案，
   * 绝不断言「这台是 Windows」。最近一次判定的结果缓存在 localStorage，断线时也能说对话。
   * ====================================================================== */
  const PLAT = { WIN: 'win', LINUX: 'linux', UNKNOWN: 'unknown' };
  const PLAT_CACHE_KEY = 'slab-platform';

  function platformOf(env) {
    const e = env || {};
    if (e.is_linux === true) return PLAT.LINUX;
    if (e.is_windows === true) return PLAT.WIN;
    const tag = String(e.tag || e.platform_tag || '').toLowerCase();
    if (tag) {
      if (tag.indexOf('linux') === 0) return PLAT.LINUX;
      if (tag.indexOf('win') === 0) return PLAT.WIN;
    }
    const osName = String(e.os || '').toLowerCase();
    if (osName === 'posix') return PLAT.LINUX;   // 目前只支持 Windows / Linux
    if (osName === 'nt') return PLAT.WIN;
    const plat = String(e.platform || '').toLowerCase();
    if (plat.indexOf('linux') >= 0) return PLAT.LINUX;
    if (plat.indexOf('win') >= 0) return PLAT.WIN;
    return PLAT.UNKNOWN;
  }

  function rememberPlatform(env) {
    const found = platformOf(env);
    if (found === PLAT.UNKNOWN) return found;
    try { localStorage.setItem(PLAT_CACHE_KEY, found); } catch (err) { /* 隐私模式忽略 */ }
    return found;
  }

  function currentPlatform() {
    const fromEnv = platformOf(S.env);
    if (fromEnv !== PLAT.UNKNOWN) return fromEnv;
    try {
      const cached = localStorage.getItem(PLAT_CACHE_KEY);
      if (cached === PLAT.LINUX || cached === PLAT.WIN) return cached;
    } catch (err) { /* 忽略 */ }
    return PLAT.UNKNOWN;
  }

  function isLinux() { return currentPlatform() === PLAT.LINUX; }

  /** 按平台选文案：Windows 用第一份，Linux 用第二份，判不出平台时用第三份（中性，不断言系统）。 */
  function platCopy(winText, linuxText, neutralText) {
    const plat = currentPlatform();
    if (plat === PLAT.LINUX) return linuxText === undefined ? winText : linuxText;
    if (plat === PLAT.WIN) return winText;
    return neutralText === undefined ? winText : neutralText;
  }

  /** 展示用路径拼接（Linux 用 /，Windows 用 \）。 */
  function pathJoin(base, name) {
    const head = String(base || '');
    if (!head) return String(name || '');
    if (/[\\/]$/.test(head)) return head + name;
    return head + platCopy('\\', '/') + name;
  }

  function setById(sel, text) {
    const node = $(sel);
    if (node) setText(node, text);
    return node;
  }

  function setHtmlById(sel, html) {
    const node = $(sel);
    if (node) node.innerHTML = html;
    return node;
  }

  /* 版本比较（"Python 3.12.3" / "v24.22.1" 都能吃） */
  function versionParts(text) {
    const m = /(\d+)\.(\d+)(?:\.(\d+))?/.exec(String(text || ''));
    return m ? [Number(m[1]), Number(m[2]), Number(m[3] || 0)] : null;
  }

  function versionAtLeast(text, min) {
    const v = versionParts(text);
    if (!v) return null;
    for (let i = 0; i < 3; i += 1) {
      if (v[i] > min[i]) return true;
      if (v[i] < min[i]) return false;
    }
    return true;
  }

  /** SnowLuma 的 Node 门槛：``^24.22.1 || >=26.12.1``（见 slab/installer.py NODE_REQUIRED）。 */
  function nodeVersionOk(text) {
    const v = versionParts(text);
    if (!v) return null;
    if (v[0] === 24) return versionAtLeast(text, [24, 22, 1]);
    if (v[0] >= 26) return versionAtLeast(text, [26, 12, 1]);
    return false;
  }

  /* 静态 HTML 里那几句 Windows 专有说法：只在确定是 Linux 时改写，
     Windows / 未知平台保持原文（Windows 行为一个字不变）。 */
  let platformCopyApplied = null;

  /* QQ 多开形态由**后端**决定：/api/qq/summary 的 clone_mode
     （'home' = Linux 独立 HOME 多开 / 'inject' = Windows DLL 分身）。
     后端没给时退回平台判定；不要只看 is_windows 猜能力。 */
  let qqCloneMode = '';

  function qqHomeMode() {
    if (qqCloneMode === 'home') return true;
    if (qqCloneMode === 'inject') return false;
    return isLinux();
  }

  /** 按 clone_mode 切换 QQ 卡片的多开措辞与入口（幂等，可在每次刷新后调用）。 */
  function applyQqModeCopy() {
    const launch = $('#qq-launch');
    const hint = $('#qq-linux-hint');
    const sel = $('#qq-install-select');
    if (qqHomeMode()) {
      if (launch) {
        setText(launch, '独立 HOME 多开');
        launch.title = '用独立 HOME 启动一个新的 QQ 实例（Linux 没有 DLL 注入这回事）';
      }
      if (sel) sel.title = '选择要多开的 QQ 安装';
      if (hint) {
        hint.hidden = false;
        setText(hint, 'Linux 上没有「深度分身（DLL 注入）」：多开用独立 HOME，'
          + '注入前需要给 node 打上 cap_sys_ptrace，并保证 ptrace_scope ≤ 2。');
      }
    } else {
      if (launch) {
        setText(launch, '隔离启动 QQ');
        launch.title = '';
      }
      if (sel) sel.title = '选择要隔离启动的 QQ 安装';
      if (hint) hint.hidden = true;
    }
    /* 官方 Linux QQ 安装指引：只在 home 模式且确实没检测到安装时出现 */
    const install = $('#qq-linux-install');
    if (install) {
      const missing = qqHomeMode() && !qqInstalls.length;
      install.hidden = !missing;
      if (missing) {
        setText(install, '未检测到官方 Linux QQ：先装官方 QQNT deb（apt install -y ./QQ_*.deb，'
          + 'x64 用 amd64 包、arm64 用 arm64 包，默认装到 /opt/QQ），再在图形会话里扫码登录。');
      }
    }
  }

  function applyPlatformCopy(force) {
    const plat = currentPlatform();
    if (!force && platformCopyApplied === plat) return plat;
    platformCopyApplied = plat;
    if (plat !== PLAT.LINUX) return plat;

    /* ---- 部署页 ---- */
    setHtmlById('#deploy-silent-note',
      '说明：所有下载/安装都在后台静默执行，<b>不会额外弹出终端窗口</b>；进度与输出都显示在下方日志里。');
    setById('#drop-backups', '清空备份（data/backups）');
    setById('#dep-uninstall-note',
      '「卸载」会把目录备份到 data/backups（可回滚）；「彻底删除」直接删掉安装目录和它的历史备份，不可恢复。');
    [['#dep-snowluma-dir', '默认：程序目录/runtime/snowluma'],
     ['#dep-astrbot-dir', '默认：程序目录/runtime/astrbot'],
     ['#cfg-snowluma-dir', '留空 = 程序目录/runtime/snowluma'],
     ['#cfg-astrbot-dir', '留空 = 程序目录/runtime/astrbot']].forEach(([sel, text]) => {
      const input = $(sel);
      if (input) input.placeholder = text;
    });
    $$('[data-open-dir]').forEach((btn) => { btn.title = '在文件管理器里打开'; });

    /* ---- 配置页 ---- */
    setById('#cfg-paths-head', '想放到别的挂载点就改这里；留空用默认位置');
    setById('#upgrade-backup-li',
      '程序文件：被新版本覆盖，覆盖前自动备份到 data/backups/program-v…');

    /* ---- 运行页 · 接入 QQ（多开形态由后端 /api/qq/summary 的 clone_mode 决定） ---- */
    setById('#qq-step-login',
      '用机器人账号登录 QQ 客户端：先用官方 Linux QQ（默认装在 /opt/QQ）扫码登录；'
      + '登录后别退出、别切号，让 QQ 留在后台运行。没有图形会话（DISPLAY 为空）时扫码做不了'
      + '（见 docs/LINUX.md 第 7 节）。');
    setById('#qq-trouble-perm',
      '权限不一致：控制台与 QQ 必须是同一个 Linux 用户（同一个 uid），两边都别用 sudo。');
    setById('#qq-trouble-antivirus',
      '注入被拒：给 node 打上 cap_sys_ptrace，并确认 ptrace_scope 不是 3（见 docs/LINUX.md 第 4 节）。');
    applyQqModeCopy();

    /* ---- 帮助弹窗 / 向导 ---- */
    setById('#help-step-qq',
      '登录 QQ：用机器人小号在官方 Linux QQ（默认 /opt/QQ）里扫码登录，登录后保持 QQ 在后台运行。'
      + 'SnowLuma 不会弹二维码——它通过 ptrace 注入已登录的 QQ 进程来接管会话，所以扫码动作在 QQ 客户端里完成。');
    setById('#help-faq-antivirus',
      '注入失败：多半是缺 cap_sys_ptrace 或 ptrace_scope=3，见 docs/LINUX.md 第 4 节。');
    setById('#wiz-install-note', '大约 3–10 分钟；输出都显示在下方日志里');
    return plat;
  }

  /* ====================================================================== *
   * 2. Toast 提示
   * ====================================================================== */
  const toastHost = $('#toasts');

  function toast(message, kind, detail) {
    const type = kind || 'info';
    const icon = { ok: '✓', warn: '!', error: '×', info: 'i' }[type] || 'i';
    const node = el('div', { class: `toast ${type}`, role: 'status' }, [
      el('span', { class: 'ti', text: icon }),
      el('div', { class: 'tm' }, [
        el('span', {}, [String(message || '')]),
        detail ? el('small', {}, [String(detail)]) : null,
      ]),
    ]);
    let gone = false;
    const dismiss = () => {
      if (gone) return;
      gone = true;
      node.classList.add('leaving');
      setTimeout(() => node.remove(), 220);
    };
    node.addEventListener('click', dismiss);
    toastHost.append(node);
    const limit = 4;
    while (toastHost.children.length > limit) toastHost.firstElementChild.remove();
    const ttl = type === 'error' ? 8000 : type === 'warn' ? 6000 : 3800;
    setTimeout(dismiss, detail ? ttl + 2600 : ttl);
    return node;
  }

  /* ====================================================================== *
   * 3. 请求层
   * ====================================================================== */
  class ApiError extends Error {
    constructor(message, status, data) {
      super(message);
      this.name = 'ApiError';
      this.status = status || 0;
      this.data = data || null;
    }
  }

  async function api(path, options) {
    const opt = options || {};
    const init = {
      method: opt.method || 'GET',
      cache: 'no-store',
      headers: {},
    };
    if (opt.body !== undefined && opt.body !== null) {
      init.headers['Content-Type'] = 'application/json';
      init.body = JSON.stringify(opt.body);
    }
    let res;
    try {
      res = await fetch(path, init);
    } catch (err) {
      // 后端连不上时可能还没拿到 env（不知道平台）→ 用中性说法，并把两种启动方式都写清楚
      throw new ApiError(platCopy(
        '连不上控制台后端，请确认黑色命令行窗口没有关闭（可重新双击「一键启动.bat」）',
        '连不上控制台后端，请确认终端里控制台进程还在运行（可重新运行「一键启动.sh」）',
        '连不上控制台后端，请确认控制台进程没有退出（Windows 双击「一键启动.bat」，Linux 运行「一键启动.sh」）',
      ), 0, err);
    }
    let data = null;
    const raw = await res.text().catch(() => '');
    if (raw) {
      try { data = JSON.parse(raw); } catch (_) { data = { ok: res.ok, raw: raw }; }
    }
    if (!res.ok || (data && data.ok === false)) {
      const msg = (data && (data.error || data.detail)) || `请求失败（HTTP ${res.status}）`;
      throw new ApiError(String(msg), res.status, data);
    }
    return data || { ok: true };
  }

  /** 统一的错误提示：区分「功能还没做完 / 接口不存在 / 其它」 */
  function fail(err, fallback) {
    if (err instanceof ApiError) {
      if (err.status === 501) {
        toast('该功能的后端模块还没就绪', 'warn', err.message || fallback);
        return;
      }
      if (err.status === 404) {
        toast(fallback || '接口不存在', 'error', err.message);
        return;
      }
      toast(fallback || '操作失败', 'error', err.message);
      return;
    }
    toast(fallback || '操作失败', 'error', err && err.message ? err.message : String(err));
  }

  /** 按钮忙碌状态包装：期间禁用，防止重复点击 */
  async function withBusy(btn, fn) {
    if (btn && btn.disabled) return undefined;
    if (btn) {
      btn.disabled = true;
      btn.classList.add('loading');
    }
    try {
      return await fn();
    } finally {
      if (btn) {
        btn.disabled = false;
        btn.classList.remove('loading');
      }
    }
  }

  /** 局部保存配置（只发一个片段），成功后静默刷新 */
  async function savePartial(patch, btn, okMessage) {
    return withBusy(btn, async () => {
      try {
        const res = await api('/api/config', { method: 'PUT', body: patch });
        if (res.config) applyConfig(res.config, res.issues);
        if (okMessage) toast(okMessage, 'ok');
        return res;
      } catch (err) {
        fail(err, '保存配置失败');
        return null;
      }
    });
  }

  function randomToken(bytes) {
    const arr = new Uint8Array(bytes || 24);
    if (window.crypto && window.crypto.getRandomValues) window.crypto.getRandomValues(arr);
    else for (let i = 0; i < arr.length; i += 1) arr[i] = Math.floor(Math.random() * 256);
    let bin = '';
    for (const b of arr) bin += String.fromCharCode(b);
    return btoa(bin).replace(/\+/g, '-').replace(/\//g, '_').replace(/=+$/, '');
  }

  async function copyText(text, okMessage) {
    const value = String(text || '');
    if (!value) { toast('没有可复制的内容', 'warn'); return false; }
    try {
      if (navigator.clipboard && window.isSecureContext) {
        await navigator.clipboard.writeText(value);
      } else {
        const ta = el('textarea', { style: 'position:fixed;left:-9999px;top:0' });
        ta.value = value;
        document.body.append(ta);
        ta.select();
        document.execCommand('copy');
        ta.remove();
      }
      toast(okMessage || '已复制到剪贴板', 'ok');
      return true;
    } catch (err) {
      toast('复制失败，请手动选择文本复制', 'error', err && err.message);
      return false;
    }
  }

  /* ====================================================================== *
   * 4. 路由 / 导航
   * ====================================================================== */
  function currentPage() {
    const key = (location.hash || '').replace(/^#\/?/, '');
    return PAGE_KEYS.includes(key) ? key : 'overview';
  }

  function go(page, opts) {
    const target = PAGE_KEYS.includes(page) ? page : 'overview';
    if (opts && opts.replace) history.replaceState(null, '', `#${target}`);
    else if (location.hash !== `#${target}`) location.hash = target;

    PAGE_KEYS.forEach((key) => {
      const section = $(`#page-${key}`);
      if (section) section.hidden = key !== target;
    });
    $$('.step').forEach((btn) => {
      if (btn.dataset.page === target) btn.setAttribute('aria-current', 'page');
      else btn.removeAttribute('aria-current');
    });
    window.scrollTo({ top: 0, behavior: 'auto' });

    if (target === 'overview') refreshState({ silent: true });
    if (target === 'deploy') {
      // 首屏先画骨架；版本列表/占用等"重"请求放到空闲时段
      const idle = window.requestIdleCallback || ((fn) => setTimeout(fn, 60));
      idle(() => { loadVersions(); loadMirrors(); });
      syncDeployPaths();
      // 先拿状态（安装情况/路径），再渲染占用与卸载卡片，否则会先闪一下"未安装"
      refreshState({ silent: true }).then(() => { syncDeployPaths(); loadUsage(); });
    }
    if (target === 'config') fillConfigForm();
    if (target === 'run') { refreshState({ silent: true }); loadLogs(); window.dispatchEvent(new Event('slab:run-open')); }
    if (target === 'link') refreshLink();
    // 终端页的逻辑在 term.js 里，靠这个事件通知它"进场了"
    if (target === 'term') window.dispatchEvent(new CustomEvent('slab:term-open'));
  }

  function bindNav() {
    $$('.step').forEach((btn) => {
      btn.addEventListener('click', () => go(btn.dataset.page));
    });
    window.addEventListener('hashchange', () => go(currentPage(), { replace: true }));
  }

  /* ====================================================================== *
   * 5. 状态读取与渲染
   * ====================================================================== */
  let stateTimer = null;
  let stateDebounce = null;

  function scheduleState(delay) {
    clearTimeout(stateDebounce);
    stateDebounce = setTimeout(() => refreshState({ silent: true }), delay || 1200);
  }

  async function refreshState(opts) {
    const options = opts || {};
    try {
      const data = await api('/api/state');
      applyState(data);
      if (!options.silent && options.toast) toast('状态已刷新', 'ok');
      return data;
    } catch (err) {
      setConn('offline', '与后端断开');
      if (!options.silent) fail(err, '读取状态失败');
      return null;
    }
  }

  function applyState(data) {
    if (!data) return;
    S.lastStateAt = Date.now();
    S.version = data.version || S.version;
    S.uptime = Number(data.uptime || 0);
    // 用服务端 started_at 作为基准，前端自己每秒推算（后端每 15s 才推一次 state，
    // 直接渲染 uptime 会出现"1 秒 → 5 秒 → 7 秒"的跳秒）
    S.startedAt = Number(data.started_at || (Date.now() / 1000 - S.uptime));
    S.services = data.services || {};
    S.env = data.env || S.env;
    // 平台判定来自后端字段：先记住，再按平台改写静态文案（Windows 上什么都不改）
    rememberPlatform(data.env);
    applyPlatformCopy();
    S.ports = data.ports || {};
    S.paths = data.paths || {};
    S.storage = data.storage || S.storage || {};
    S.issues = Array.isArray(data.issues) ? data.issues : [];
    if (data.config) applyConfig(data.config, data.issues, true);

    setText($('#app-version'), S.version ? `v${S.version}` : 'v—');
    tickUptimes();
    startUptimeTicker();
    renderOverview();
    renderRunCards();
    renderPaths();
    renderDeployBadges();
    renderStorage();
  }

  /* --------------------------------------------------------------- 运行时长 */
  function tickUptimes() {
    const now = Date.now() / 1000;
    const up = $('#uptime-pill');
    if (up && S.startedAt) {
      const sec = Math.max(0, now - S.startedAt);
      setText(up, `运行 ${fmtUptime(sec)}`);
      up.title = `控制台已运行 ${fmtUptime(sec)}`;
    }
    Object.keys(S.services || {}).forEach((name) => {
      const card = document.querySelector(`[data-svc="${name}"]`);
      if (!card) return;
      const cell = card.querySelector('[data-role="uptime"]');
      if (!cell) return;
      const st = S.services[name] || {};
      const startedAt = Number(st.started_at || 0);
      cell.textContent = st.status === 'running' && startedAt
        ? fmtUptime(Math.max(0, now - startedAt))
        : '—';
    });
  }

  let uptimeTimer = null;
  function startUptimeTicker() {
    if (uptimeTimer) return;
    uptimeTimer = setInterval(() => {
      if (!document.hidden) tickUptimes();
    }, 1000);
  }

  function applyConfig(cfg, issues, keepDirty) {
    if (!cfg) return;
    S.config = cfg;
    S.secrets = cfg._secrets_configured || S.secrets || {};
    if (Array.isArray(issues)) S.issues = issues;
    updateSecretBadges();
    // 用户正在编辑时不要覆盖表单内容，避免把输入冲掉
    if (!S.configDirty) fillConfigForm();
    renderRunCards();
    renderOverview();
  }

  /* --------------------------------------------------------------- 总览 */
  function svcView(state) {
    if (!state) return { label: '未知', cls: 'muted', tone: 'idle' };
    switch (state.status) {
      case 'running':
        return state.ready
          ? { label: '运行中', cls: 'ok', tone: 'ok' }
          : { label: '运行中（未就绪）', cls: 'warn', tone: 'warn' };
      case 'starting': return { label: '启动中…', cls: 'warn', tone: 'warn' };
      case 'stopping': return { label: '停止中…', cls: 'warn', tone: 'warn' };
      case 'failed': return { label: '异常退出', cls: 'err', tone: 'err' };
      default: return { label: '未运行', cls: 'muted', tone: 'idle' };
    }
  }

  function renderOverview() {
    renderStatusLights();
    renderPorts();
    renderEnv();
    renderAdvice();
  }

  function statusCard(opts) {
    return el('div', { class: 'status-card', dataset: { tone: opts.tone || 'idle' } }, [
      el('div', { class: 'sc-head' }, [
        el('span', { class: 'sc-title', text: opts.title }),
        el('span', { class: `badge ${opts.cls || 'muted'}`, text: opts.badge }),
      ]),
      el('div', { class: 'sc-value', text: opts.value || '—' }),
      el('div', { class: 'sc-note', text: opts.note || '' }),
    ]);
  }

  function renderStatusLights() {
    const host = $('#status-lights');
    if (!host) return;
    clear(host);

    const snow = S.services.snowluma;
    const astr = S.services.astrbot;
    const brg = S.services.bridge;
    const mode = (S.config && S.config.link && S.config.link.mode) || 'native';

    const snowV = svcView(snow);
    host.append(statusCard({
      title: 'SnowLuma 协议端',
      badge: snowV.label,
      cls: snowV.cls,
      tone: snowV.tone,
      value: snow && snow.pid ? `PID ${snow.pid}` : (snow && snow.status === 'running' ? '运行中' : '—'),
      note: snow && snow.ready_port ? `就绪端口 ${snow.ready_port} · 首次登录需扫码` : '在「运行」页启动；首次登录需扫码',
    }));

    const astrV = svcView(astr);
    host.append(statusCard({
      title: 'AstrBot 框架',
      badge: astrV.label,
      cls: astrV.cls,
      tone: astrV.tone,
      value: astr && astr.pid ? `PID ${astr.pid}` : (astr && astr.status === 'running' ? '运行中' : '—'),
      note: astr && astr.ready_port ? `面板端口 ${astr.ready_port}` : '在「运行」页启动',
    }));

    const brgV = svcView(brg);
    const bridgeOn = brg && (brg.status === 'running' || brg.status === 'starting');
    host.append(statusCard({
      title: '中继服务',
      badge: mode === 'bridge' ? brgV.label : (bridgeOn ? '运行中' : '未启用'),
      cls: mode === 'bridge' ? brgV.cls : (bridgeOn ? 'ok' : 'muted'),
      tone: mode === 'bridge' ? brgV.tone : (bridgeOn ? 'ok' : 'idle'),
      value: mode === 'bridge' ? '内置中继模式' : '原生直连模式',
      note: mode === 'bridge'
        ? '消息会经过控制台转发，可在「联调」页看到流水'
        : '无需中继进程，SnowLuma 直连 AstrBot',
    }));

    const onebot = renderOnebotLight();
    host.append(onebot);
  }

  /** 从联动状态里取出「连接列表」：多开 QQ 时每个账号各一条连接 */
  function onebotConnList(info) {
    if (!info || typeof info !== 'object') return null;
    const keys = ['connections', 'clients', 'links', 'accounts'];
    for (const key of keys) {
      if (Array.isArray(info[key])) return info[key];
    }
    return null;
  }

  function connUsable(conn) {
    if (!conn || typeof conn !== 'object') return !!conn;
    if (conn.connected === true || conn.ok === true || conn.ready === true) return true;
    return ['running', 'ready', 'connected', 'online', 'active'].includes(String(conn.status || conn.state || '').toLowerCase());
  }

  function renderOnebotLight() {
    const info = (S.ports && S.ports.onebot) || null;
    const port = info && info.port ? info.port : (S.config && S.config.link ? S.config.link.ws_port : '—');
    const linkOnebot = S.link && S.link.onebot ? S.link.onebot : null;
    const multiNote = '多开 QQ 时，每个已登录账号各有一条连接；任意一条正常即算连通';

    // 0) 中继模式：接进 AstrBot 的那条连接由内置中继提供，SnowLuma 的 wsClients
    //    天生为空。直接看后端按中继上游算出来的 clients_connected，别用空列表误报。
    if (linkOnebot && linkOnebot.clients_source === 'bridge') {
      const up = Number(linkOnebot.clients_connected || 0) > 0;
      return statusCard({
        title: 'OneBot 连接',
        badge: up ? '已连接' : '未连接',
        cls: up ? 'ok' : 'warn',
        tone: up ? 'ok' : 'warn',
        value: `端口 ${port}`,
        note: up
          ? '中继已作为 OneBot 客户端接入 AstrBot（SnowLuma 侧不需要 wsClient）'
          : '中继还没连上 AstrBot，去「联调」页点「修复发送链路」重新登记',
      });
    }

    // 1) 后端给了连接列表：至少一条正常 → 绿灯
    const list = onebotConnList(linkOnebot);
    if (list) {
      const good = list.filter(connUsable);
      if (good.length) {
        return statusCard({
          title: 'OneBot 连接',
          badge: `${good.length}/${list.length} 条正常`,
          cls: 'ok',
          tone: 'ok',
          value: `端口 ${port}`,
          note: multiNote,
        });
      }
      return statusCard({
        title: 'OneBot 连接',
        badge: list.length ? '连接异常' : '未连接',
        cls: list.length ? 'warn' : 'muted',
        tone: list.length ? 'warn' : 'idle',
        value: `端口 ${port}`,
        note: list.length ? '已建立连接但尚未握手成功，稍等或看「运行」页日志' : multiNote,
      });
    }

    // 2) 后端只给了单条状态
    const linkOk = linkOnebot && typeof linkOnebot === 'object'
      ? (linkOnebot.connected === true || linkOnebot.ok === true
        || ['running', 'ready', 'connected', 'online'].includes(String(linkOnebot.status || '').toLowerCase()))
      : null;
    if (linkOk === true) {
      return statusCard({
        title: 'OneBot 连接',
        badge: '已连接',
        cls: 'ok',
        tone: 'ok',
        value: `端口 ${port}`,
        note: multiNote,
      });
    }
    if (info && info.in_use) {
      return statusCard({
        title: 'OneBot 连接',
        badge: '端口监听中',
        cls: 'warn',
        tone: 'warn',
        value: `端口 ${port}`,
        note: '端口已被占用，但尚未确认握手成功',
      });
    }
    return statusCard({
      title: 'OneBot 连接',
      badge: '未连接',
      cls: 'muted',
      tone: 'idle',
      value: `端口 ${port}`,
      note: '两端服务都启动后才会建立连接；多开 QQ 时每个账号各一条',
    });
  }

  function renderPorts() {
    const tbody = $('#ports-table tbody');
    if (!tbody) return;
    clear(tbody);
    const keys = ['console', 'astrbot_webui', 'snowluma_webui', 'onebot'];
    for (const key of keys) {
      const info = S.ports[key];
      if (!info) continue;
      const isSelf = key === 'console';
      const inUse = !!info.in_use;
      let badge;
      if (isSelf && inUse) badge = el('span', { class: 'badge ok', text: '本控制台' });
      else if (inUse) badge = el('span', { class: 'badge warn', text: '被占用' });
      else badge = el('span', { class: 'badge muted', text: '空闲' });
      tbody.append(el('tr', {}, [
        el('td', {}, [PORT_LABELS[key] || key]),
        el('td', { class: 'mono', text: String(info.port) }),
        el('td', {}, [badge]),
        el('td', { class: 'mono path-cell', text: info.owner || (inUse ? '—' : '') }),
      ]));
    }
    if (!tbody.children.length) {
      tbody.append(el('tr', {}, [el('td', { colspan: '4', class: 'muted', text: '正在读取端口信息…' })]));
    }
  }

  /* --------------------------------------------------------------- 环境自检 */
  /* 行标题由前端持有（中英两套词条可控），数据来自后端 env.checks[]。
     state 语义：true=通过 / false=不通过 / 'warn'=建议修 / null=未知（例如没装 getcap） */
  const ENV_ROW_LABELS = {
    python: 'Python（≥ 3.12，控制台与 AstrBot 都要）',
    python3: 'python3（AstrBot 需要 ≥ 3.12）',
    git: 'Git（克隆 AstrBot 需要）',
    uv: 'uv（更快的 Python 包管理器，可选）',
    node: 'Node.js（SnowLuma lite 版需要）',
    pnpm: 'pnpm（可选）',
    disk: '可用磁盘空间（建议 ≥ 3 GB）',
    venv: 'python3-venv（创建虚拟环境需要）',
    ptrace_scope: 'Yama ptrace_scope（= 3 时禁止注入）',
    node_cap_ptrace: 'node 的 cap_sys_ptrace（注入权限）',
    display: '图形会话（DISPLAY，扫码登录需要）',
    uid: '与 QQ 同一个用户（uid）',
  };

  function envRow(name, state, value, hint) {
    let tone = 'warn';
    let mark = '?';
    if (state === true) { tone = 'ok'; mark = '✓'; }
    else if (state === false) { tone = 'bad'; mark = '×'; }
    else if (state === 'warn') { tone = 'warn'; mark = '!'; }
    return el('li', { title: hint || '' }, [
      el('span', { class: `ico ${tone}`, text: mark }),
      el('span', { class: 'name', text: name }),
      el('span', { class: 'val', text: value || '' }),
    ]);
  }

  /* 修复指引：后端 env.checks[i].fix 是一条可直接复制的命令 */
  function envFixRow(fix) {
    return el('li', { class: 'env-fix' }, [
      el('span', { class: 'ico warn', text: '!' }),
      el('span', { class: 'name', text: '修复：' }),
      el('code', { class: 'val', text: String(fix) }),
    ]);
  }

  function appendEnvCheck(list, row) {
    if (!row || typeof row !== 'object') return;
    const key = String(row.key || '');
    /* 后端的约定：ok=null 表示「未知」（例如没装 getcap），只算建议；
       ok=false 但 level=warn 的项（例如 node，只有 lite 版才需要）也不能显示成红色错误。 */
    const soft = row.level === 'warn';
    let state = null;
    if (row.ok === true) state = true;
    else if (row.ok === false) state = soft ? 'warn' : false;
    else if (soft) state = 'warn';
    const raw = row.value === undefined || row.value === null ? '' : String(row.value);
    list.append(envRow(
      ENV_ROW_LABELS[key] || String(row.label || key || ''),
      state,
      // 后端会带「当前 3.12.3」这类前缀；前端统一去掉，避免英文界面里夹中文
      raw.replace(/^当前\s*/, '') || (state === false ? '未检测到' : ''),
      row.hint ? String(row.hint) : '',
    ));
    const fix = String(row.fix || '').trim();
    if (fix && state !== true) list.append(envFixRow(fix));
  }

  /* 后端还没给 env.checks[] 时的兜底（Linux 专属项自己推一遍；推不出就显示未知） */
  function linuxEnvFallback(env) {
    const lx = env.linux || {};
    const tools = env.tools || {};
    const rows = [];

    const pyVer = lx.python_version || (tools.python && tools.python.version) || '';
    const pyOk = typeof lx.python_ok === 'boolean' ? lx.python_ok : versionAtLeast(pyVer, [3, 12, 0]);
    rows.push({
      key: 'python3', ok: pyOk, value: pyVer || '未知',
      fix: 'sudo apt install -y python3.12 python3.12-venv（或先装 uv 再 uv python install 3.12）',
    });

    const venvOk = typeof lx.venv_ok === 'boolean' ? lx.venv_ok : null;
    rows.push({
      key: 'venv', ok: venvOk, value: venvOk === false ? '不可用' : (venvOk === true ? '可用' : '未知'),
      fix: 'sudo apt install -y python3-venv',
    });

    const scope = Number(lx.ptrace_scope);
    const scopeKnown = Number.isFinite(scope);
    rows.push({
      key: 'ptrace_scope',
      ok: scopeKnown ? scope <= 2 : null,
      value: scopeKnown ? String(scope) : '未知',
      fix: 'sudo sysctl -w kernel.yama.ptrace_scope=1（永久：写入 /etc/sysctl.d/99-snowluma.conf）',
    });

    const cap = lx.node_cap_ptrace;
    rows.push({
      key: 'node_cap_ptrace',
      ok: typeof cap === 'boolean' ? cap : null,
      value: cap === true ? 'cap_sys_ptrace=ep' : (cap === false ? '未设置' : '未知（缺 libcap2-bin 时无法确认）'),
      fix: 'sudo setcap cap_sys_ptrace=ep <SnowLuma 安装目录>/node（用 readlink -f 取真实路径）',
    });

    const nodeVer = lx.node_version || (tools.node && tools.node.version) || '';
    const nodeOk = typeof lx.node_ok === 'boolean' ? lx.node_ok : nodeVersionOk(nodeVer);
    rows.push({
      key: 'node', ok: nodeOk, value: nodeVer || '未知',
      fix: '改用 full 版（自带 node），或装 Node ^24.22.1 / >=26.12.1',
    });

    const display = String(lx.display || '');
    const wayland = String(lx.wayland || '');
    rows.push({
      key: 'display', ok: (display || wayland) ? true : 'warn',
      value: display || wayland || '未设置',
      fix: '在桌面会话里启动控制台；无头服务器用 Xvfb + VNC（见 docs/LINUX.md 第 7 节）',
    });
    return rows;
  }

  function renderEnv() {
    const list = $('#env-list');
    if (!list) return;
    clear(list);
    const env = S.env;
    if (!env) {
      list.append(el('li', { class: 'muted', text: '正在检测环境…' }));
      return;
    }
    const tools = env.tools || {};
    const checks = Array.isArray(env.checks) && env.checks.length ? env.checks : null;
    const order = [
      ['python', 'Python（控制台自身）'],
      ['git', 'Git（克隆 AstrBot 需要）'],
      ['uv', 'uv（更快的 Python 包管理器，可选）'],
      ['node', 'Node.js（SnowLuma lite 版需要）'],
      ['pnpm', 'pnpm（可选）'],
    ];
    const seen = new Set();

    if (checks) {
      /* 后端「整包」模式：顺序即展示顺序，Linux 专属项（ptrace/setcap/DISPLAY/venv）也在里面 */
      checks.forEach((row) => {
        if (row && typeof row === 'object' && row.key) seen.add(String(row.key));
        appendEnvCheck(list, row);
      });
      /* 补漏：后端 checks 没覆盖到的工具（例如 uv / pnpm）仍用 tools 显示一行 */
      for (const [key, label] of order) {
        if (seen.has(key)) continue;
        const info = tools[key] || {};
        const ver = info.version && info.version.indexOf('<失败') === 0 ? info.version : (info.version || '');
        list.append(envRow(label, !!info.ok, info.ok ? (ver || '已安装') : '未检测到', info.path || ''));
      }
    } else {
      for (const [key, label] of order) {
        const info = tools[key] || {};
        const ver = info.version && info.version.indexOf('<失败') === 0 ? info.version : (info.version || '');
        list.append(envRow(label, !!info.ok, info.ok ? (ver || '已安装') : '未检测到', info.path || ''));
      }
      if (isLinux()) linuxEnvFallback(env).forEach((row) => appendEnvCheck(list, row));
    }

    /* 磁盘：后端 checks 里已有「disk」就不再重复一行 */
    if (!seen.has('disk')) {
      const free = Number(env.free_space_gb);
      const freeOk = Number.isFinite(free) ? free >= 3 : false;
      list.append(envRow(
        '磁盘剩余空间（建议 ≥ 3 GB）',
        freeOk,
        Number.isFinite(free) && free >= 0 ? `${free} GB` : '未知',
        '安装两个项目约占用 1–3 GB',
      ));
    }
    const installed = env.installed || {};
    list.append(envRow('SnowLuma 已安装', !!installed.snowluma, installed.snowluma ? '是' : '否'));
    list.append(envRow('AstrBot 已安装', !!installed.astrbot, installed.astrbot ? '是' : '否'));

    (env.warnings || []).forEach((w) => {
      list.append(el('li', {}, [
        el('span', { class: 'ico warn', text: '!' }),
        el('span', { class: 'name', text: String(w) }),
      ]));
    });
  }

  function adviceItem(parts, level, action) {
    const li = el('li', { class: level || '' }, parts);
    if (action && action.page) {
      li.append(el('button', {
        class: 'go',
        type: 'button',
        text: action.label || '去处理',
        onclick: () => go(action.page),
      }));
    }
    return li;
  }

  function renderAdvice() {
    const list = $('#advice-list');
    if (!list) return;
    clear(list);

    const cfg = S.config || {};
    const env = S.env || {};
    const installed = env.installed || {};
    const secrets = S.secrets || {};
    const items = [];

    if (!installed.snowluma) {
      items.push(adviceItem([el('b', {}, ['安装 SnowLuma']), '：去「部署」页点「下载安装」，建议选 full 版（自带 Node）。'], 'err', { page: 'deploy', label: '去部署' }));
    }
    if (!installed.astrbot) {
      items.push(adviceItem([el('b', {}, ['安装 AstrBot']), '：点「克隆并安装依赖」，需要本机有 Git。'], installed.snowluma ? 'err' : '', { page: 'deploy', label: '去部署' }));
    }
    if (!String((cfg.snowluma || {}).qq_uin || '').trim()) {
      items.push(adviceItem([el('b', {}, ['填写 QQ 号']), '：建议用一个小号，登录协议端有封号风险。'], 'warn', { page: 'config', label: '去填写' }));
    }
    if (!secrets.astrbot_api_key) {
      items.push(adviceItem([el('b', {}, ['填写大模型 API Key']), '：没有 Key 机器人不会回复。可选 DeepSeek / OpenAI 兼容接口。'], 'warn', { page: 'config', label: '去填写' }));
    }
    S.issues.forEach((issue) => {
      items.push(adviceItem([el('b', {}, ['配置待修正']), `：${issue}`], 'err', { page: 'config', label: '去修改' }));
    });

    const portWarn = Object.keys(S.ports).some((key) => key !== 'console' && S.ports[key] && S.ports[key].in_use);
    if (portWarn) {
      const used = Object.keys(S.ports)
        .filter((key) => key !== 'console' && S.ports[key] && S.ports[key].in_use)
        .map((key) => `${PORT_LABELS[key] || key}=${S.ports[key].port}${S.ports[key].owner ? `（${S.ports[key].owner}）` : ''}`);
      items.push(adviceItem([el('b', {}, ['端口被占用']), `：${used.join('、')} 已被别的程序占用，换一个端口后重新「写入联动配置」。`], 'warn', { page: 'config', label: '换端口' }));
    }

    const anyRunning = Object.keys(S.services).some((key) => {
      const st = S.services[key];
      return st && (st.status === 'running' || st.status === 'starting');
    });
    if (installed.snowluma && installed.astrbot && !anyRunning) {
      items.push(adviceItem([el('b', {}, ['启动服务']), '：两个项目都已安装，去「运行」页点「一键启动全部」。'], 'warn', { page: 'run', label: '去启动' }));
    }
    const snowReady = S.services.snowluma && S.services.snowluma.status === 'running' && S.services.snowluma.ready;
    const astrReady = S.services.astrbot && S.services.astrbot.status === 'running' && S.services.astrbot.ready;
    if (anyRunning && !snowReady) {
      items.push(adviceItem(['等待 SnowLuma 就绪：首次启动要扫码登录 QQ（面板端口 ' + ((S.ports.snowluma_webui || {}).port || '—') + '）。'], 'warn', { page: 'run', label: '看日志' }));
    }
    if (snowReady && astrReady && !portWarn) {
      items.push(adviceItem([
        '链路看起来已经打通：去 QQ 里 ',
        el('b', {}, ['@机器人']),
        ' 或发一条 ',
        el('code', {}, ['/你好']),
        '，然后到「联调」页看消息流水。',
      ], 'done', { page: 'link', label: '去联调' }));
    }
    if (!items.length) {
      items.push(adviceItem(['一切正常。想改端口 / 换模型就在「配置」页调整，需要看细节就去「运行」页的实时日志。'], 'done'));
    }
    items.forEach((item) => list.append(item));
  }

  /* --------------------------------------------------------------- 运行页 */
  function panelUrl(meta) {
    if (!meta || !meta.portKey) return '';
    const portInfo = S.ports[meta.portKey];
    const cfg = S.config || {};
    let port = portInfo && portInfo.port;
    if (!port) {
      if (meta.portKey === 'astrbot_webui') port = (cfg.astrbot || {}).webui_port;
      if (meta.portKey === 'snowluma_webui') port = (cfg.snowluma || {}).webui_port;
    }
    if (!port) return '';
    let host = (cfg.app || {}).host || '127.0.0.1';
    if (host === '0.0.0.0' || host === '::' || host === '') host = '127.0.0.1';
    return `http://${host}:${port}`;
  }

  function ensureServiceCard(name) {
    const grid = $('#service-cards');
    if (!grid) return null;
    let card = grid.querySelector(`[data-svc="${name}"]`);
    if (card) return card;
    const meta = SVC_META[name] || { title: name, desc: '', panelLabel: '', portKey: '' };

    const head = el('div', { class: 'svc-head' }, [
      el('h2', { text: meta.title }),
      el('span', { class: 'badge muted', dataset: { role: 'badge' }, text: '未运行' }),
    ]);
    const dl = el('dl', { class: 'svc-meta' }, [
      el('dt', { text: '状态' }), el('dd', { dataset: { role: 'status' }, text: '—' }),
      el('dt', { text: 'PID' }), el('dd', { dataset: { role: 'pid' }, text: '—' }),
      el('dt', { text: '运行时长' }), el('dd', { dataset: { role: 'uptime' }, text: '—' }),
      el('dt', { text: '就绪' }), el('dd', { dataset: { role: 'ready' }, text: '—' }),
      el('dt', { text: '说明' }), el('dd', { text: meta.desc }),
    ]);

    const actions = el('div', { class: 'svc-actions' });
    ['start', 'stop', 'restart'].forEach((action) => {
      actions.append(el('button', {
        class: 'btn' + (action === 'start' ? ' primary' : ''),
        type: 'button',
        text: { start: '启动', stop: '停止', restart: '重启' }[action],
        dataset: { action, svc: name },
        onclick: (ev) => serviceAction(name, action, ev.currentTarget),
      }));
    });
    const url = panelUrl(meta);
    if (url && meta.panelLabel) {
      actions.append(el('a', {
        class: 'btn',
        href: url,
        target: '_blank',
        rel: 'noopener',
        text: meta.panelLabel,
        title: url,
      }));
    }

    card = el('section', { class: 'card service-card', dataset: { svc: name } }, [head, dl, actions]);
    grid.append(card);
    return card;
  }

  const STATUS_LABEL = {
    stopped: '已停止',
    starting: '启动中',
    running: '运行中',
    stopping: '停止中',
    failed: '异常退出',
  };

  function renderRunCards() {
    const grid = $('#service-cards');
    if (!grid) return;
    const names = Object.keys(S.services || {});
    if (!names.length) {
      if (!grid.children.length) {
        grid.append(el('section', { class: 'card service-card' }, [
          el('h2', { text: '服务尚未注册' }),
          el('p', { class: 'muted', text: '控制台后端还没有注册任何服务，请先完成部署（或稍等后端初始化）。' }),
        ]));
      }
      return;
    }
    if (grid.querySelector('[data-svc=""], .service-card:not([data-svc])')) clear(grid);
    names.forEach((name) => {
      const card = ensureServiceCard(name);
      if (!card) return;
      const st = S.services[name] || {};
      const view = svcView(st);
      const badge = card.querySelector('[data-role="badge"]');
      if (badge) {
        badge.className = `badge ${view.cls}`;
        setText(badge, view.label);
      }
      setText(card.querySelector('[data-role="status"]'), STATUS_LABEL[st.status] || st.status || '—');
      setText(card.querySelector('[data-role="pid"]'), st.pid ? String(st.pid) : '—');
      // 运行时长按 started_at 现算（配合每秒 tick，避免跳秒）
      const startedAt = Number(st.started_at || 0);
      setText(
        card.querySelector('[data-role="uptime"]'),
        st.status === 'running' && startedAt ? fmtUptime(Math.max(0, Date.now() / 1000 - startedAt)) : '—',
      );
      setText(card.querySelector('[data-role="ready"]'), st.ready ? '是' : '否');
      const running = st.status === 'running' || st.status === 'starting';
      card.querySelectorAll('[data-action]').forEach((btn) => {
        const action = btn.dataset.action;
        const key = `${name}:${action}`;
        btn.disabled = S.busy.has(key) || (action === 'start' ? running : !running);
      });
    });
  }

  function renderDeployBadges() {
    const installed = (S.env && S.env.installed) || {};
    const map = [['snowluma', '#dep-snowluma-badge'], ['astrbot', '#dep-astrbot-badge']];
    for (const [key, sel] of map) {
      const badge = $(sel);
      if (!badge) continue;
      const ok = !!installed[key];
      badge.className = `badge ${ok ? 'ok' : 'muted'}`;
      setText(badge, ok ? '已安装' : '未安装');
    }
    const table = $('#paths-table tbody');
    if (table) {
      clear(table);
      const rows = [
        ['SnowLuma', (S.paths || {}).snowluma, !!installed.snowluma],
        ['AstrBot', (S.paths || {}).astrbot, !!installed.astrbot],
        ['数据目录', (S.paths || {}).data, true],
        ['运行时目录', (S.paths || {}).runtime, true],
      ];
      for (const [name, path, ok] of rows) {
        const cell = el('td', { class: 'path-cell mono', text: path || '—' });
        const btn = el('button', {
          class: 'btn sm',
          type: 'button',
          text: '复制路径',
          onclick: () => copyText(path, platCopy(
            '路径已复制，可粘贴到资源管理器地址栏',
            '路径已复制，可粘贴到文件管理器地址栏',
            '路径已复制',
          )),
        });
        if (!path) btn.disabled = true;
        table.append(el('tr', {}, [
          el('td', { text: name }),
          cell,
          el('td', {}, [el('span', { class: `badge ${ok ? 'ok' : 'muted'}`, text: ok ? '就绪' : '未安装' })]),
          el('td', {}, [btn]),
        ]));
      }
    }
  }

  /* 存储规划里的「盘」：Windows 是盘符（C/D…），Linux 是挂载点（/ 、/home、/opt…） */
  function driveLabel(drive) {
    if (!drive || typeof drive !== 'object') return '';
    const name = drive.mount || drive.mountpoint || drive.path || drive.letter
      || drive.drive || drive.name || drive.device || '';
    return String(name || '');
  }

  function renderStorage() {
    const st = S.storage || {};
    const note = $('#storage-note');
    if (note) {
      const drives = (st.drives || [])
        .map((d) => {
          const label = driveLabel(d);
          const free = d.free_gb === undefined ? '' : ` ${d.free_gb} GB 可用`;
          return `${label}${free}`;
        })
        .filter((text) => text.trim())
        .join(' · ');
      const reason = st.reason || (st.base ? `放在 ${st.base}` : '');
      note.textContent = reason ? `存储规划：${reason}${drives ? `（${drives}）` : ''}` : '';
      note.title = [
        `数据目录：${st.data_dir || ''}`,
        `安装目录：${st.runtime_dir || ''}`,
        `下载缓存：${st.downloads_dir || ''}`,
      ].join('\n');
    }
    const rt = st.runtime_dir || (S.paths && S.paths.runtime) || '';
    if (rt) {
      [['#dep-snowluma-dir', 'snowluma'], ['#dep-astrbot-dir', 'astrbot'],
       ['#cfg-snowluma-dir', 'snowluma'], ['#cfg-astrbot-dir', 'astrbot']].forEach(([sel, name]) => {
        const input = $(sel);
        if (input && !input.value) input.placeholder = `默认：${pathJoin(rt, name)}`;
      });
    }
  }

  function renderPaths() { renderDeployBadges(); }

  /* ====================================================================== *
   * 6. 服务启停
   * ====================================================================== */
  async function serviceAction(name, action, btn) {
    const key = `${name}:${action}`;
    if (S.busy.has(key)) return;
    S.busy.add(key);
    const label = { start: '启动', stop: '停止', restart: '重启' }[action] || action;
    if (btn) { btn.disabled = true; btn.classList.add('loading'); }
    try {
      const res = await api(`/api/services/${name}/${action}`, { method: 'POST' });
      if (res && res.services) S.services = res.services;
      renderRunCards();
      renderOverview();
      if (name === 'all') {
        const failed = Object.keys(res.results || {}).filter((k) => String(res.results[k]).indexOf('failed') === 0);
        if (failed.length) {
          toast(`部分服务启动失败：${failed.join('、')}`, 'error', '请看「运行」页日志');
        } else {
          toast(`${label}全部服务：成功`, 'ok');
        }
      } else {
        toast(`${(SVC_META[name] || {}).title || name} ${label}指令已下发`, 'ok');
      }
      scheduleState(900);
    } catch (err) {
      fail(err, `${label}失败`);
      scheduleState(400);
    } finally {
      S.busy.delete(key);
      if (btn) { btn.disabled = false; btn.classList.remove('loading'); }
      renderRunCards();
    }
  }

  async function serviceActionAll(action, btn) {
    const names = Object.keys(S.services || {});
    if (!names.length) {
      toast('还没有已注册的服务，请先完成部署', 'warn');
      return;
    }
    const label = { start: '启动', stop: '停止', restart: '重启' }[action] || action;
    await withBusy(btn, async () => {
      try {
        const res = await api(`/api/services/all/${action}`, { method: 'POST' });
        if (res && res.services) S.services = res.services;
        renderRunCards();
        renderOverview();
        const failed = Object.keys(res.results || {}).filter((k) => String(res.results[k]).indexOf('failed') === 0);
        if (failed.length) toast(`${label}完成，但 ${failed.join('、')} 失败`, 'error', '请到「运行」页查看日志');
        else toast(`${label}全部服务成功`, 'ok');
        scheduleState(1000);
      } catch (err) {
        fail(err, `${label}全部服务失败`);
      }
    });
  }

  /* ====================================================================== *
   * 7. 配置表单
   * ====================================================================== */
  function cfgInputs(root) {
    return $$('[data-cfg]', root || document);
  }

  function setDirty(dirty) {
    S.configDirty = !!dirty;
    const flag = $('#cfg-dirty');
    if (flag) flag.hidden = !S.configDirty;
  }

  function updateSecretBadges() {
    $$('[data-secret-badge]').forEach((node) => {
      const key = node.dataset.secretBadge;
      const ok = !!(S.secrets && S.secrets[key]);
      node.className = `secret-badge ${ok ? 'ok' : ''}`.trim();
      setText(node, ok ? '已配置（回显为 ***）' : '未设置');
    });
  }

  function fillConfigForm() {
    const cfg = S.config;
    if (!cfg) return;
    cfgInputs().forEach((input) => {
      const path = input.dataset.cfg;
      const value = getPath(cfg, path);
      if (input.type === 'radio') {
        input.checked = String(value) === input.value;
        return;
      }
      if (input.type === 'checkbox') {
        input.checked = !!value;
        return;
      }
      if (input.dataset.secret === '1') {
        // 后端只会回显 ***，这里留空，保存时自动跳过
        input.value = '';
        return;
      }
      if (input.dataset.type === 'list') {
        input.value = Array.isArray(value) ? value.join(', ') : (value || '');
        return;
      }
      input.value = value === null || value === undefined ? '' : String(value);
    });
    S.configFilled = true;
    // 数据目录是只读展示（跟随程序位置，不随配置变）
    const dataDir = $('#cfg-data-dir');
    if (dataDir) dataDir.value = (S.paths && S.paths.data) || '';

    setDirty(false);
    updateEditionHint();
    updateModeDependent();
  }

  /** 「内置中继」才需要的字段：根据 link.mode 显示/隐藏 */
  function updateModeDependent() {
    const picked = $$('[data-cfg="link.mode"]').find((item) => item.checked);
    const mode = picked ? picked.value : ((S.config && S.config.link && S.config.link.mode) || 'native');
    $$('[data-mode-only]').forEach((node) => {
      node.hidden = node.dataset.modeOnly !== mode;
    });
  }

  function collectConfigPatch() {
    const patch = {};
    const radiosSeen = new Set();
    cfgInputs($('#config-form')).forEach((input) => {
      const path = input.dataset.cfg;
      if (!path) return;
      if (input.type === 'radio') {
        if (radiosSeen.has(path)) return;
        radiosSeen.add(path);
        const group = $$(`[data-cfg="${path}"]`, $('#config-form'));
        const picked = group.find((item) => item.checked);
        if (picked) putPath(patch, path, picked.value);
        return;
      }
      if (input.type === 'checkbox') { putPath(patch, path, !!input.checked); return; }
      if (input.dataset.secret === '1') {
        const val = input.value.trim();
        if (val && val !== '***') putPath(patch, path, val);
        return;
      }
      const raw = input.value.trim();
      if (input.dataset.type === 'list') {
        const list = raw.split(/[,，\s]+/).map((s) => s.trim()).filter(Boolean);
        putPath(patch, path, list);
        return;
      }
      if (input.type === 'number') {
        const num = Number(raw);
        if (!Number.isFinite(num)) return;
        putPath(patch, path, Math.round(num));
        return;
      }
      if (raw !== '') putPath(patch, path, raw);
    });
    return patch;
  }

  function putPath(obj, path, value) {
    const parts = String(path).split('.');
    let cur = obj;
    for (let i = 0; i < parts.length - 1; i += 1) {
      if (!cur[parts[i]] || typeof cur[parts[i]] !== 'object') cur[parts[i]] = {};
      cur = cur[parts[i]];
    }
    cur[parts[parts.length - 1]] = value;
  }

  async function saveConfig(btn) {
    const patch = collectConfigPatch();
    if (!Object.keys(patch).length) {
      toast('没有需要保存的修改', 'info');
      return;
    }
    await withBusy(btn, async () => {
      try {
        const res = await api('/api/config', { method: 'PUT', body: patch });
        setDirty(false);
        S.configFilled = true;
        if (res.config) applyConfig(res.config, res.issues);
        if (Array.isArray(res.issues) && res.issues.length) {
          toast('已保存，但还有需要处理的地方', 'warn', res.issues.join('；'));
        } else {
          toast('设置已保存', 'ok');
        }
        scheduleState(600);
      } catch (err) {
        fail(err, '保存设置失败');
      }
    });
  }

  async function resetConfig(btn) {
    await withBusy(btn, async () => {
      try {
        const res = await api('/api/config/reset', { method: 'POST' });
        if (res.config) applyConfig(res.config, res.issues);
        S.configFilled = false;
        fillConfigForm();
        toast('已恢复默认配置（敏感字段会重新生成）', 'ok');
        scheduleState(600);
      } catch (err) {
        fail(err, '恢复默认失败');
      }
    });
  }

  function updateEditionHint() {
    const sel = $('#dep-snowluma-edition');
    const hint = $('#dep-snowluma-edition-hint');
    if (!sel || !hint) return;
    hint.textContent = sel.value === 'lite'
      ? platCopy(
        'lite 版体积更小，但要求本机已安装 Node.js ≥ 24.22.1，否则启动会失败。',
        'lite 版体积更小，但要求本机 Node 满足 ^24.22.1 || >=26.12.1（发行版自带的 node 基本都不满足），否则启动会失败。',
      )
      : platCopy(
        'full 版自带 node.exe，不用自己装 Node，小白首选；lite 版体积小，但要求本机已有 Node。',
        'full 版自带 node，不用自己装 Node，小白首选（发行版仓库里的 node 版本普遍不够）；lite 版体积小，但要求本机 Node 满足 ^24.22.1 || >=26.12.1。',
      );
  }

  async function generateToken(btn) {
    const token = randomToken(24);
    const input = $('#cfg-token');
    if (input) { input.value = token; input.dataset.secret = '1'; }
    await savePartial({ link: { token } }, btn, '已生成新的联动 Token，记得去「联调」页重新写入联动配置');
    if (input) input.value = token;
  }

  /* ------------------------------------------------- 配置页：账号同步 / 连通性检查 */
  async function syncLoggedInAccount(btn) {
    await withBusy(btn, async () => {
      const note = $('#cfg-sync-note');
      try {
        const res = await api('/api/link/sync-account', { method: 'POST' });
        const input = $('#cfg-qq-uin');
        if (input) input.value = res.qq_uin;
        if (note) note.textContent = res.message || '';
        toast(`机器人 QQ 号已同步：${res.qq_uin}`, 'ok', '别忘了点右上角「保存设置」');
        scheduleState(600);
      } catch (err) {
        if (note) note.textContent = err.message || String(err);
        fail(err, '读取已登录账号失败');
      }
    });
  }

  async function importProviderFromAstrBot(btn) {
    await withBusy(btn, async () => {
      const note = $('#cfg-models-note');
      try {
        const res = await api('/api/link/provider/import', { method: 'POST' });
        const baseInput = $('#cfg-base-url');
        const modelInput = $('#cfg-model');
        if (baseInput && res.base_url) baseInput.value = res.base_url;
        if (modelInput && res.model) modelInput.value = res.model;
        if (note) {
          note.textContent = `已读回：${res.model} @ ${res.base_url}${res.has_key ? '（含 Key）' : ''}`;
          note.style.color = 'var(--ok)';
        }
        toast('已从 AstrBot 读回模型配置', 'ok', '控制台里显示的就是实际在跑的配置');
        scheduleState(600);
      } catch (err) {
        if (note) note.textContent = err.message || String(err);
        fail(err, '读回失败');
      }
    });
  }

  async function fetchProviderModels(btn) {
    await withBusy(btn, async () => {
      const note = $('#cfg-models-note');
      if (note) note.textContent = '正在读取模型列表…';
      try {
        // 用表单里刚填的 Base URL / Key 去问，不必先保存
        const base = ($('#cfg-base-url') || {}).value || '';
        const key = ($('#cfg-api-key') || {}).value || '';
        const res = await api('/api/link/provider/models', {
          method: 'POST',
          body: { base_url: base, api_key: key },
        });
        const list = $('#model-options');
        if (list) {
          clear(list);
          (res.models || []).forEach((id) => list.append(el('option', { value: id })));
        }
        const input = $('#cfg-model');
        if (note) {
          const count = (res.models || []).length;
          const where = res.source === 'api' ? '来自接口' : '接口不支持，按服务商给的建议';
          note.textContent = count
            ? `${res.message}（${where}）· 点模型输入框右侧箭头选择`
            : (res.message || '没读到模型');
          note.style.color = res.ok ? 'var(--ok)' : 'var(--danger)';
        }
        // 当前模型不在列表里就自动选第一个，省得用户再手填
        if (input && (res.models || []).length) {
          const cur = (input.value || '').trim();
          if (!cur || !res.models.includes(cur)) {
            input.value = res.models.includes('deepseek-chat') ? 'deepseek-chat' : res.models[0];
          }
        }
        if ((res.models || []).length) toast(`可用模型 ${res.models.length} 个`, 'ok', '已填好，记得点「保存设置」');
        else toast(res.message || '没读到模型', 'warn');
      } catch (err) {
        if (note) note.textContent = err.message || String(err);
        fail(err, '读取模型列表失败');
      }
    });
  }

  async function testProvider(btn) {
    await withBusy(btn, async () => {
      const note = $('#cfg-test-note');
      if (note) note.textContent = '正在测试…';
      try {
        // 先把当前表单（含刚填的 Key）存下来，再拿后端配置去测
        const save = $('#btn-save-config') || $('#cfg-save');
        if (typeof saveConfig === 'function') {
          try { await saveConfig(null); } catch (err) { /* 保存失败也继续测，用已存的配置 */ }
        }
        const res = await api('/api/link/provider/test', { method: 'POST' });
        if (note) {
          note.textContent = res.message || (res.ok ? '连接正常' : '测试失败');
          note.style.color = res.ok ? 'var(--ok)' : 'var(--danger)';
        }
        toast(res.ok ? '大模型连接正常' : '大模型连接失败', res.ok ? 'ok' : 'error', res.message || '');
      } catch (err) {
        if (note) note.textContent = err.message || String(err);
        fail(err, '测试失败');
      }
    });
  }

  function bindConfigForm() {
    const form = $('#config-form');
    if (form) {
      form.addEventListener('input', (ev) => {
        if (ev.target && ev.target.dataset && ev.target.dataset.cfg) setDirty(true);
      });
      form.addEventListener('change', (ev) => {
        if (ev.target && ev.target.dataset && ev.target.dataset.cfg) setDirty(true);
        if (ev.target && ev.target.dataset && ev.target.dataset.cfg === 'link.mode') updateModeDependent();
      });
    }
    const save1 = $('#cfg-save');
    const save2 = $('#cfg-save-bottom');
    if (save1) save1.addEventListener('click', () => saveConfig(save1));
    if (save2) save2.addEventListener('click', () => saveConfig(save2));
    const reset = $('#cfg-reset');
    if (reset) {
      reset.addEventListener('click', () => {
        if (window.confirm('确定要把所有配置恢复成默认值吗？已填写的 QQ 号与大模型 Key 会被清空。')) resetConfig(reset);
      });
    }
    const gen = $('#cfg-token-gen');
    if (gen) gen.addEventListener('click', () => generateToken(gen));
    const copyTok = $('#cfg-token-copy');
    if (copyTok) {
      copyTok.addEventListener('click', () => {
        const input = $('#cfg-token');
        const val = input ? input.value.trim() : '';
        if (val && val !== '***') copyText(val, 'Token 已复制');
        else toast('出于安全考虑，已保存的 Token 不会回显', 'warn', '点「重新生成」会得到新 Token 并显示出来');
      });
    }

    // 安装目录：选择 / 打开
    $$('[data-pick-dir]').forEach((btn) => {
      btn.addEventListener('click', () => pickInstallDir(btn.dataset.pickDir, btn));
    });
    $$('[data-open-dir]').forEach((btn) => {
      btn.addEventListener('click', () => openDir(btn.dataset.openDir, btn));
    });
    // 维护：立即清理 / 检查端口
    const cleanBtn = $('#maint-cleanup');
    if (cleanBtn) {
      cleanBtn.addEventListener('click', async () => {
        await withBusy(cleanBtn, async () => {
          try {
            const res = await api('/api/system/cleanup', { method: 'POST' });
            const note = $('#maint-note');
            if (note) note.textContent = `已清理 ${res.removed_count} 个文件，释放 ${res.freed_mb} MB`;
            toast(res.removed_count ? `已清理 ${res.removed_count} 个过期文件` : '没有需要清理的文件', 'ok',
                  res.removed_count ? `释放 ${res.freed_mb} MB` : '日志与安装包都在保留期内');
          } catch (err) {
            fail(err, '清理失败');
          }
        });
      });
    }
    const fixBtn = $('#maint-fix-ports');
    if (fixBtn) {
      fixBtn.addEventListener('click', async () => {
        await withBusy(fixBtn, async () => {
          try {
            const res = await api('/api/system/fix-ports', { method: 'POST' });
            if (res.config) applyConfig(res.config, res.issues);
            const note = $('#maint-note');
            if (note) note.textContent = res.message || '';
            (res.changes || []).forEach((c) => toast(`${c.label}：${c.from} → ${c.to}`, 'warn', c.reason));
            toast(res.changes && res.changes.length ? '端口已自动避让' : '端口都空闲，无需调整',
                  res.changes && res.changes.length ? 'warn' : 'ok', res.message);
            if (res.restart_required) toast('控制台端口变了，请手动重启控制台', 'warn');
          } catch (err) {
            fail(err, '检查端口失败');
          }
        });
      });
    }

    const orphanBtn = $('#maint-orphans');
    if (orphanBtn) {
      orphanBtn.addEventListener('click', async () => {
        await withBusy(orphanBtn, async () => {
          const note = $('#maint-note');
          try {
            const preview = await api('/api/system/cleanup-orphans?dry_run=true', { method: 'POST' });
            const items = preview.items || [];
            if (!items.length) {
              if (note) note.textContent = '没有残留进程，很干净';
              toast('没有发现残留进程', 'ok');
              return;
            }
            const list = items
              .map((i) => `· PID ${i.pid}（${i.kind}${i.port ? ` :${i.port}` : ''}）`)
              .join('\n');
            if (!window.confirm(`发现 ${items.length} 个上次没退干净的进程：\n\n${list}\n\n全部结束掉吗？`)) return;
            const res = await api('/api/system/cleanup-orphans', { method: 'POST' });
            if (note) note.textContent = res.message || '';
            toast(res.message || '已清理', 'ok', '现在可以去「运行」页重新启动服务');
            scheduleState(600);
          } catch (err) {
            fail(err, '清理残留进程失败');
          }
        });
      });
    }

    const exportBtn = $('#cfg-export');
    if (exportBtn) exportBtn.addEventListener('click', () => exportConfig(exportBtn));
    const importBtn = $('#cfg-import-btn');
    const importFile = $('#cfg-import-file');
    if (importBtn && importFile) {
      importBtn.addEventListener('click', () => importFile.click());
      importFile.addEventListener('change', () => {
        const file = importFile.files && importFile.files[0];
        if (file) importConfig(file, importFile);
      });
    }
  }

  /* ---------------------------------------------------- 目录选择 / 打开目录 */
  const DIR_LABEL = { snowluma: 'SnowLuma', astrbot: 'AstrBot', data: '数据目录' };

  async function pickInstallDir(target, btn) {
    const inputSel = (btn && btn.dataset && btn.dataset.pickInput)
      ? `#${btn.dataset.pickInput}`
      : `#cfg-${target}-dir`;
    const input = $(inputSel);
    const current = input ? input.value.trim() : '';
    // 桌面壳里有原生对话框；浏览器里退回后端 PowerShell 对话框
    if (window.slabDesktop && typeof window.slabDesktop.pickDirectory === 'function') {
      const picked = await window.slabDesktop.pickDirectory(DIR_LABEL[target] || '选择目录', current);
      if (picked) {
        input.value = picked;
        setDirty(true);
        toast(`${DIR_LABEL[target] || target} 安装目录已选择`, 'ok', '保存后生效；重新部署会装到新位置');
      }
      return;
    }
    const note = $('#cfg-io-note');
    try {
      toast('正在等待系统目录选择框…', 'info', '如果没看到对话框，请看一下任务栏');
      const res = await api('/api/deploy/pick-dir', {
        method: 'POST',
        body: { title: `选择 ${DIR_LABEL[target] || ''} 安装目录`, initial: current },
      });
      if (res && res.ok && res.path) {
        input.value = res.path;
        setDirty(true);
        toast(`${DIR_LABEL[target] || target} 安装目录已选择`, 'ok', '保存后生效；重新部署会装到新位置');
      } else if (res && res.cancelled) {
        toast('已取消选择', 'info');
      }
    } catch (err) {
      if (note) {
        note.hidden = false;
        // 浏览器版在 Linux 上没有 zenity/kdialog 时后端返回 501，这里给手输路径的例子
        const example = platCopy('D:\\bots\\snowluma', '/opt/bots/snowluma', '<可写目录>');
        note.textContent = `无法打开系统目录选择框（${err.message || err}）。可以直接在输入框里粘贴路径，例如 ${example}`;
      }
      fail(err, '打开目录选择框失败');
    }
  }

  async function openDir(target, btn) {
    try {
      await api('/api/deploy/open-dir', { method: 'POST', body: { target } });
    } catch (err) {
      fail(err, '打开目录失败');
    }
  }

  async function exportConfig(btn) {
    await withBusy(btn, async () => {
      try {
        const res = await fetch('/api/config/export', { cache: 'no-store' });
        if (!res.ok) throw new Error(`HTTP ${res.status}`);
        const blob = await res.blob();
        const stamp = new Date().toISOString().slice(0, 19).replace(/[:T]/g, '-');
        const a = document.createElement('a');
        a.href = URL.createObjectURL(blob);
        a.download = `slab-config-${stamp}.json`;
        a.click();
        setTimeout(() => URL.revokeObjectURL(a.href), 4000);
        toast('配置已导出', 'ok', '文件里含密钥，注意保管');
      } catch (err) {
        fail(err, '导出配置失败');
      }
    });
  }

  async function importConfig(file, input) {
    const note = $('#cfg-io-note');
    try {
      const text = await file.text();
      let data;
      try {
        data = JSON.parse(text);
      } catch (err) {
        throw new Error('文件不是合法的 JSON');
      }
      if (!window.confirm('导入会整份覆盖当前配置（含端口、QQ 号、API Key、安装目录），确定吗？')) return;
      const res = await api('/api/config/import', { method: 'POST', body: data });
      applyConfig(res.config, res.issues);
      fillConfigForm();
      toast('配置已导入', 'ok');
      if (note) {
        note.hidden = false;
        note.textContent = '导入完成。如果改了端口或安装目录，建议重启一次控制台让服务按新配置启动。';
      }
    } catch (err) {
      fail(err, '导入配置失败');
    } finally {
      if (input) input.value = '';
    }
  }

  /* ====================================================================== *
   * 8. 部署
   * ====================================================================== */
  async function loadVersions() {
    try {
      const res = await api('/api/deploy/versions');
      const snow = $('#dep-snowluma-version');
      if (snow && Array.isArray(res.snowluma) && res.snowluma.length) {
        const current = (S.config && S.config.snowluma && S.config.snowluma.version) || 'latest';
        clear(snow);
        snow.append(el('option', { value: 'latest', text: 'latest（最新稳定版）' }));
        res.snowluma.forEach((tag) => snow.append(el('option', { value: String(tag), text: String(tag) })));
        snow.value = Array.from(snow.options).some((o) => o.value === current) ? current : 'latest';
      }
      const astr = $('#dep-astrbot-version');
      if (astr) {
        const list = Array.isArray(res.astrbot) ? res.astrbot : (res.astrbot ? [res.astrbot] : []);
        if (list.length) {
          const current = (S.config && S.config.astrbot && S.config.astrbot.version) || 'main';
          clear(astr);
          list.forEach((v) => astr.append(el('option', { value: String(v), text: String(v) })));
          astr.value = Array.from(astr.options).some((o) => o.value === current) ? current : String(list[0]);
        }
      }
    } catch (err) {
      // 版本列表拿不到时保留默认选项，不打扰用户
      console.warn('获取版本列表失败:', err && err.message);
    }
  }

  function deployBox(target) {
    return {
      card: $(`[data-target="${target}"]`),
      progress: $(`#dep-${target}-progress`),
      log: $(`#dep-${target}-log`),
      button: $(`#dep-${target}-go`),
    };
  }

  function appendDeployLog(target, line) {
    const box = deployBox(target);
    if (!box.log) return;
    const time = fmtClock(Date.now() / 1000);
    box.log.append(el('div', {}, [`[${time}] ${line}`]));
    while (box.log.children.length > 400) box.log.firstElementChild.remove();
    box.log.scrollTop = box.log.scrollHeight;
  }

  function setDeployRunning(target, running, stepText) {
    const targets = target === 'both' ? ['snowluma', 'astrbot'] : [target];
    targets.forEach((name) => {
      const box = deployBox(name);
      const state = S.deploy[name] || (S.deploy[name] = { progress: 0, step: '' });
      state.running = !!running;
      if (running) { state.progress = 0; state.step = stepText || '准备中…'; }
      if (box.progress) box.progress.hidden = !running && !(state.progress > 0 && state.progress < 1);
      const bar = box.progress ? box.progress.querySelector('.bar > i') : null;
      const pct = box.progress ? box.progress.querySelector('.pct') : null;
      const step = box.progress ? box.progress.querySelector('.step-text') : null;
      if (bar) bar.style.width = `${Math.round((state.progress || 0) * 100)}%`;
      if (pct) pct.textContent = `${Math.round((state.progress || 0) * 100)}%`;
      if (step && state.step) step.textContent = state.step;
      if (box.button) box.button.disabled = !!running;
    });
  }

  function onDeployEvent(ev) {
    const target = ev.target || 'snowluma';
    const state = S.deploy[target] || (S.deploy[target] = { progress: 0, step: '' });
    if (typeof ev.progress === 'number') {
      state.progress = Math.max(0, Math.min(1, ev.progress));
    }
    const message = ev.message || ev.step || '';
    if (message) state.step = message;
    const box = deployBox(target);
    if (box.progress) {
      box.progress.hidden = false;
      const bar = box.progress.querySelector('.bar > i');
      const pct = box.progress.querySelector('.pct');
      const step = box.progress.querySelector('.step-text');
      if (bar) bar.style.width = `${Math.round(state.progress * 100)}%`;
      if (pct) pct.textContent = `${Math.round(state.progress * 100)}%`;
      if (step) step.textContent = [ev.step, message].filter(Boolean).join(' · ') || '进行中…';
    }
    if (message) appendDeployLog(target, `[${ev.step || '进度'}] ${message}`);
  }

  async function deployTarget(target, btn) {
    const label = target === 'both' ? '两个项目' : ((SVC_META[target] || {}).title || target);
    setDeployRunning(target, true, '正在连接部署模块…');
    appendDeployLog(target === 'both' ? 'snowluma' : target, `开始安装 ${label}…`);
    if (target === 'both') appendDeployLog('astrbot', '开始安装 AstrBot…');
    await withBusy(btn, async () => {
      try {
        const timeStart = Date.now();
        const res = await api(`/api/deploy/${target}`, { method: 'POST' });
        const cost = Math.round((Date.now() - timeStart) / 1000);
        setDeployRunning(target, false, '完成');
        const targets = target === 'both' ? ['snowluma', 'astrbot'] : [target];
        targets.forEach((name) => {
          const st = S.deploy[name] || {};
          st.progress = 1; st.running = false;
          const box = deployBox(name);
          if (box.progress) {
            box.progress.hidden = false;
            const bar = box.progress.querySelector('.bar > i');
            const pct = box.progress.querySelector('.pct');
            const step = box.progress.querySelector('.step-text');
            if (bar) bar.style.width = '100%';
            if (pct) pct.textContent = '100%';
            if (step) step.textContent = '安装完成';
          }
          appendDeployLog(name, '安装流程结束');
        });
        const summary = res && (res.message || (res.result && res.result.message) || '');
        toast(`${label} 安装完成（耗时 ${cost} 秒）`, 'ok', summary || undefined);
        await refreshState({ silent: true });
      } catch (err) {
        setDeployRunning(target, false, '失败');
        const targets = target === 'both' ? ['snowluma', 'astrbot'] : [target];
        targets.forEach((name) => appendDeployLog(name, `安装失败：${err && err.message ? err.message : err}`));
        fail(err, `安装 ${label} 失败`);
      }
    });
  }

  async function removeTarget(target, btn) {
    const label = (SVC_META[target] || {}).title || target;
    if (!window.confirm(`确定卸载 ${label} 吗？安装目录会被删除（data 目录里的配置不受影响）。`)) return;
    await withBusy(btn, async () => {
      try {
        await api(`/api/deploy/${target}`, { method: 'DELETE' });
        toast(`${label} 已卸载`, 'ok');
        appendDeployLog(target, '已卸载');
        await refreshState({ silent: true });
      } catch (err) {
        fail(err, `卸载 ${label} 失败`);
      }
    });
  }

  /* ------------------------------------------- 下载源 / 测速 / 卸载清理 */
  async function loadMirrors() {
    const sel = $('#mirror-select');
    if (!sel) return;
    try {
      const res = await api('/api/deploy/sources');
      const current = sel.value;
      clear(sel);
      sel.append(el('option', { value: 'auto', text: '自动（测速挑最快的）' }));
      (res.mirrors || []).forEach((m) => {
        sel.append(el('option', { value: m.id, text: m.label }));
      });
      sel.value = (S.config && S.config.app && S.config.app.download_mirror) || res.current || current || 'auto';
      const hint = $('#mirror-hint');
      if (hint && res.test_asset) hint.title = `测速样本：${res.test_asset}`;
    } catch (err) {
      /* 静默：拿不到源清单不影响部署 */
    }
  }

  function renderSpeedResults(res) {
    const box = $('#mirror-results');
    if (!box) return;
    clear(box);
    const rows = res.results || [];
    if (!rows.length) {
      box.append(el('p', { class: 'muted sm', text: '没有拿到测速结果' }));
      return;
    }
    const max = Math.max(1, ...rows.map((r) => Number(r.mbps) || 0));
    rows.forEach((r) => {
      const best = r.id === res.recommended;
      const row = el('div', { class: `mirror-row${best ? ' best' : ''}` });
      row.append(el('span', { class: 'm-name', text: r.label }));
      const bar = el('span', { class: 'm-bar' });
      const fill = el('i', {});
      fill.style.width = r.ok ? `${Math.max(4, Math.round((Number(r.mbps) / max) * 100))}%` : '0%';
      bar.append(fill);
      row.append(bar);
      row.append(el('span', { class: 'm-mbps', text: r.ok ? `${r.mbps} MB/s` : '不可用' }));
      row.append(el('span', { class: 'm-lat', text: r.ok ? `${r.latency_ms} ms` : (r.error || '') }));
      if (best) row.append(el('span', { class: 'badge ok', text: '推荐' }));
      const use = el('button', { class: 'btn ghost sm', type: 'button', text: '用这个源' });
      use.addEventListener('click', async () => {
        await savePartial({ app: { download_mirror: r.id } }, use, `下载源已切换为 ${r.label}`);
        await loadMirrors();
      });
      row.append(use);
      box.append(row);
    });
  }

  async function runSpeedtest(btn) {
    const box = $('#mirror-results');
    await withBusy(btn, async () => {
      if (box) {
        clear(box);
        box.append(el('p', { class: 'muted sm', text: '正在测速（每个源最多 8 秒）…' }));
      }
      try {
        const res = await api('/api/deploy/sources/speedtest', { method: 'POST' });
        renderSpeedResults(res);
        const best = (res.results || []).find((r) => r.id === res.recommended);
        if (best) toast(`最快：${best.label}`, 'ok', `${best.mbps} MB/s · 首字节 ${best.latency_ms} ms`);
        else toast('所有源都不可用，建议先用官方源重试', 'warn');
      } catch (err) {
        fail(err, '测速失败');
      }
    });
  }

  function syncDeployPaths() {
    const cfg = S.config || {};
    const map = [['snowluma', '#dep-snowluma-dir'], ['astrbot', '#dep-astrbot-dir']];
    map.forEach(([key, sel]) => {
      const input = $(sel);
      if (input) input.value = ((cfg[key] || {}).install_dir) || '';
    });
  }

  function bindDeployPaths() {
    [['snowluma', '#dep-snowluma-dir'], ['astrbot', '#dep-astrbot-dir']].forEach(([key, sel]) => {
      const input = $(sel);
      if (!input) return;
      input.addEventListener('change', async () => {
        const value = input.value.trim();
        await savePartial({ [key]: { install_dir: value } }, null,
          value ? `${key} 安装目录已保存（重新部署会装到新位置）` : `${key} 已恢复默认安装目录`);
      });
    });
  }

  async function loadUsage() {
    const box = $('#uninstall-list');
    if (!box) return;
    try {
      const res = await api('/api/deploy/usage');
      clear(box);
      const installedMap = (S.env && S.env.installed) || {};
      [['snowluma', 'SnowLuma'], ['astrbot', 'AstrBot']].forEach(([key, label]) => {
        const item = (res.items || {})[key] || {};
        const installed = !!installedMap[key];
        const row = el('div', { class: 'uninstall-row' });
        row.append(el('div', { class: 'u-head' }, [
          el('b', { text: label }),
          el('span', { class: `badge ${installed ? 'ok' : 'muted'}`, text: installed ? '已安装' : '未安装' }),
          el('span', { class: 'muted sm', text: `${item.mb || 0} MB` }),
        ]));
        row.append(el('div', { class: 'u-path', text: item.path || '', title: item.path || '' }));
        const actions = el('div', { class: 'u-actions' });
        const soft = el('button', { class: 'btn ghost sm', type: 'button', text: '卸载（保留备份）' });
        soft.addEventListener('click', () => uninstallTarget(key, false, soft));
        const hard = el('button', { class: 'btn danger sm', type: 'button', text: '彻底删除' });
        hard.addEventListener('click', () => uninstallTarget(key, true, hard));
        actions.append(soft, hard);
        row.append(actions);
        box.append(row);
      });
      const total = $('#usage-total');
      if (total) {
        total.textContent = `共占用 ${res.total_mb || 0} MB · 备份 ${(res.backups || []).length} 个 · 下载缓存 ${(res.items && res.items.downloads && res.items.downloads.mb) || 0} MB`;
      }
    } catch (err) {
      clear(box);
      box.append(el('p', { class: 'muted sm', text: `读取占用失败：${err.message || err}` }));
    }
  }

  async function uninstallTarget(target, purge, btn) {
    const label = (SVC_META[target] || {}).title || target;
    const message = purge
      ? `确定彻底删除 ${label} 吗？\n\n安装目录 + 它的历史备份都会被删掉，不可恢复；data 目录里的配置与日志不受影响。`
      : `确定卸载 ${label} 吗？\n\n安装目录会移到 data\\backups（可回滚）。`;
    if (!window.confirm(message)) return;
    await withBusy(btn, async () => {
      try {
        const res = await api(`/api/deploy/${target}?purge=${purge ? 'true' : 'false'}`, { method: 'DELETE' });
        toast(res.message || `${label} 已处理`, 'ok');
        appendDeployLog(target, res.message || '已卸载');
        await refreshState({ silent: true });
        await loadUsage();
      } catch (err) {
        fail(err, `卸载 ${label} 失败`);
      }
    });
  }

  async function dropBackups(btn) {
    if (!window.confirm('清空 data\\backups 里的所有旧目录？删除后无法恢复。')) return;
    await withBusy(btn, async () => {
      try {
        const res = await api('/api/deploy/backups/clear', { method: 'DELETE' });
        toast(`已清理 ${res.removed} 个备份`, 'ok', `释放 ${res.freed_mb} MB`);
        await loadUsage();
      } catch (err) {
        fail(err, '清理备份失败');
      }
    });
  }

  function bindDeploy() {
    const both = $('#dep-install-both');
    if (both) both.addEventListener('click', () => deployTarget('both', both));
    ['snowluma', 'astrbot'].forEach((target) => {
      const go = $(`#dep-${target}-go`);
      if (go) go.addEventListener('click', () => deployTarget(target, go));
      const copy = $(`#dep-${target}-copy`);
      if (copy) {
        copy.addEventListener('click', () => {
          const path = (S.paths || {})[target] || '';
          if (!path) toast('还没有安装目录信息，请先刷新状态', 'warn');
          else copyText(path, `${target} 安装目录已复制，可粘贴到资源管理器地址栏`);
        });
      }
      const rm = $(`#dep-${target}-remove`);
      if (rm) rm.addEventListener('click', () => removeTarget(target, rm));
    });

    bindDeployPaths();
    const mirrorSel = $('#mirror-select');
    if (mirrorSel) {
      mirrorSel.addEventListener('change', async () => {
        const value = mirrorSel.value;
        await savePartial({ app: { download_mirror: value } }, null,
          value === 'auto' ? '下载源：自动测速' : `下载源：${mirrorSel.options[mirrorSel.selectedIndex].text}`);
      });
    }
    const mirrorTest = $('#mirror-test');
    if (mirrorTest) mirrorTest.addEventListener('click', () => runSpeedtest(mirrorTest));
    const openGh = $('#mirror-open-github');
    if (openGh) {
      openGh.addEventListener('click', () => {
        window.open('https://github.com/SnowLuma/SnowLuma/releases', '_blank', 'noopener');
      });
    }
    const usageRefresh = $('#usage-refresh');
    if (usageRefresh) usageRefresh.addEventListener('click', () => withBusy(usageRefresh, loadUsage));
    const dropBk = $('#drop-backups');
    if (dropBk) dropBk.addEventListener('click', () => dropBackups(dropBk));

    const edition = $('#dep-snowluma-edition');
    if (edition) {
      edition.addEventListener('change', async () => {
        updateEditionHint();
        await savePartial({ snowluma: { edition: edition.value } }, null, 'SnowLuma 安装类型已保存');
      });
    }
    const version = $('#dep-snowluma-version');
    if (version) {
      version.addEventListener('change', () => savePartial({ snowluma: { version: version.value } }, null, 'SnowLuma 版本已保存'));
    }
    const aversion = $('#dep-astrbot-version');
    if (aversion) {
      aversion.addEventListener('change', () => savePartial({ astrbot: { version: aversion.value } }, null, 'AstrBot 版本已保存'));
    }
  }

  /* ====================================================================== *
   * 9. 日志
   * ====================================================================== */
  function normLevel(level) {
    const l = String(level || 'info').toLowerCase();
    if (l === 'warning') return 'warn';
    if (l === 'err' || l === 'critical' || l === 'fatal') return 'error';
    if (l === 'debug' || l === 'trace') return 'info';
    return l;
  }

  function logPass(ev) {
    const service = $('#log-service');
    const level = $('#log-level');
    const wantSvc = service ? service.value : '';
    const wantLevel = level ? level.value : 'all';
    if (wantSvc && String(ev.service || '') !== wantSvc) return false;
    if (wantLevel !== 'all' && normLevel(ev.level) !== wantLevel) return false;
    return true;
  }

  function logLineNode(ev) {
    const level = normLevel(ev.level);
    const cls = level === 'warn' ? 'ln warn' : level === 'error' ? 'ln error' : 'ln';
    return el('span', { class: cls }, [
      el('span', { class: 't', text: fmtClock(ev.ts) }),
      el('span', { class: 's', text: String(ev.service || 'console') }),
      document.createTextNode(String(ev.line === undefined ? '' : ev.line)),
    ]);
  }

  function trimLogView() {
    const view = $('#log-view');
    if (!view) return;
    while (view.children.length > MAX_LOG_LINES) view.firstElementChild.remove();
  }

  function appendLogLine(ev) {
    const view = $('#log-view');
    if (!view || !logPass(ev)) return;
    const placeholder = view.querySelector('.placeholder');
    if (placeholder) placeholder.remove();
    view.append(logLineNode(ev));
    trimLogView();
    const auto = $('#log-autoscroll');
    if (!auto || auto.checked) view.scrollTop = view.scrollHeight;
  }

  function renderLogStatic(lines) {
    const view = $('#log-view');
    if (!view) return;
    clear(view);
    if (!lines.length) {
      view.append(el('span', { class: 'placeholder', text: '暂无日志。启动服务后，这里会实时滚动输出。' }));
      return;
    }
    lines.forEach((ev) => view.append(logLineNode(ev)));
    trimLogView();
    const auto = $('#log-autoscroll');
    if (!auto || auto.checked) view.scrollTop = view.scrollHeight;
  }

  function syncLogServiceOptions(services) {
    const sel = $('#log-service');
    if (!sel) return;
    const names = new Set(services || []);
    Object.keys(S.services || {}).forEach((k) => names.add(k));
    ['console', 'deploy'].forEach((k) => names.add(k));
    const current = sel.value;
    clear(sel);
    sel.append(el('option', { value: '', text: '全部服务' }));
    Array.from(names).sort().forEach((name) => sel.append(el('option', { value: name, text: name })));
    sel.value = Array.from(sel.options).some((o) => o.value === current) ? current : '';
  }

  async function loadLogs() {
    const sel = $('#log-service');
    const service = sel ? sel.value : '';
    try {
      const res = await api(`/api/logs?service=${encodeURIComponent(service)}&limit=300`);
      syncLogServiceOptions(res.services || []);
      if (sel && sel.value !== service) sel.value = service;
      const lines = Array.isArray(res.logs) ? res.logs : [];
      renderLogStatic(lines.filter(logPass));
    } catch (err) {
      renderLogStatic([]);
      fail(err, '读取日志失败');
    }
  }

  function bindLogs() {
    const svc = $('#log-service');
    if (svc) svc.addEventListener('change', () => loadLogs());
    const lvl = $('#log-level');
    if (lvl) lvl.addEventListener('change', () => loadLogs());
    const reload = $('#log-reload');
    if (reload) reload.addEventListener('click', () => loadLogs());
    const clearBtn = $('#log-clear');
    if (clearBtn) {
      clearBtn.addEventListener('click', () => {
        renderLogStatic([]);
        toast('日志窗口已清空（服务端记录不受影响）', 'info');
      });
    }
  }

  /* ====================================================================== *
   * 10. 联调
   * ====================================================================== */
  function flattenObject(obj, prefix) {
    const out = [];
    const walk = (node, path) => {
      if (node === null || node === undefined) return;
      if (typeof node !== 'object') { out.push([path, node]); return; }
      if (Array.isArray(node)) {
        if (!node.length) { out.push([path, '（空）']); return; }
        node.forEach((item, i) => walk(item, path ? `${path}[${i}]` : String(i)));
        return;
      }
      const keys = Object.keys(node);
      if (!keys.length) { out.push([path, '（空）']); return; }
      keys.forEach((key) => walk(node[key], path ? `${path}.${key}` : key));
    };
    walk(obj, prefix || '');
    return out;
  }

  function probeTone(obj) {
    if (!obj || typeof obj !== 'object') return { label: '未知', cls: 'muted', tone: 'idle' };
    if (obj.ok === false) return { label: '异常', cls: 'err', tone: 'err' };
    const status = String(obj.status || obj.state || '').toLowerCase();
    if (['running', 'ready', 'connected', 'ok', 'online', 'active'].includes(status)) {
      return { label: status === 'running' ? '运行中' : '正常', cls: 'ok', tone: 'ok' };
    }
    if (['starting', 'pending', 'warn', 'warning', 'degraded'].includes(status)) return { label: status, cls: 'warn', tone: 'warn' };
    if (['stopped', 'disconnected', 'offline', 'error', 'failed'].includes(status)) {
      return { label: status === 'stopped' ? '未运行' : status, cls: status === 'error' || status === 'failed' ? 'err' : 'muted', tone: status === 'error' || status === 'failed' ? 'err' : 'idle' };
    }
    if (obj.connected === true || obj.ready === true) return { label: '正常', cls: 'ok', tone: 'ok' };
    if (obj.connected === false || obj.ready === false) return { label: '未连接', cls: 'muted', tone: 'idle' };
    if (obj.enabled === false) return { label: '未启用', cls: 'muted', tone: 'idle' };
    if (obj.ok === true) return { label: '正常', cls: 'ok', tone: 'ok' };
    return { label: '未知', cls: 'muted', tone: 'idle' };
  }

  const LINK_BLOCKS = [
    ['snowluma', 'SnowLuma 协议端'],
    ['astrbot', 'AstrBot'],
    ['onebot', 'OneBot 连接'],
    ['bridge', '中继服务'],
  ];

  function renderLinkBlocks() {
    const host = $('#link-status-blocks');
    if (!host) return;
    clear(host);
    const data = S.link || {};
    for (const [key, title] of LINK_BLOCKS) {
      const info = data[key];
      const tone = probeTone(info);
      const dl = el('dl');
      if (info && typeof info === 'object') {
        const entries = flattenObject(info).slice(0, 10);
        if (!entries.length) dl.append(el('dt', { text: '状态' }), el('dd', { text: '无数据' }));
        for (const [k, v] of entries) {
          dl.append(el('dt', { text: k }), el('dd', { text: String(v) }));
        }
      } else {
        dl.append(el('dt', { text: '状态' }), el('dd', { text: '尚无数据，点右上角「刷新」或先写入联动配置' }));
      }
      host.append(el('section', { class: 'link-block' }, [
        el('div', { class: 'lb-head' }, [
          el('b', { text: title }),
          el('span', { class: `badge ${tone.cls}`, text: tone.label }),
        ]),
        dl,
      ]));
    }
  }

  async function refreshLink() {
    try {
      const res = await api('/api/link/status');
      S.link = res || {};
      S.linkErr = null;
    } catch (err) {
      S.link = null;
      S.linkErr = err;
      if (err instanceof ApiError && err.status !== 501) fail(err, '读取联动状态失败');
    }
    renderLinkBlocks();
    renderStatusLights();
  }

  function renderLinkResult(title, lines) {
    const box = $('#link-result');
    if (!box) return;
    clear(box);
    box.append(el('div', { class: 'lr-title', text: title }));
    if (!lines || !lines.length) {
      box.append(el('div', { class: 'lr-line muted', text: '（无详细信息）' }));
      return;
    }
    lines.forEach((line) => box.append(el('div', { class: 'lr-line' }, [
      el('span', { class: 'lr-key', text: line[0] }),
      el('span', { class: 'lr-val', text: String(line[1] === undefined || line[1] === null ? '' : line[1]) }),
    ])));
  }

  async function linkApply(btn) {
    await withBusy(btn, async () => {
      try {
        const res = await api('/api/link/apply', { method: 'POST' });
        const lines = [];
        const collect = (prefix, value) => {
          if (value && typeof value === 'object' && !Array.isArray(value)) {
            Object.keys(value).forEach((k) => collect(prefix ? `${prefix}.${k}` : k, value[k]));
          } else if (Array.isArray(value)) {
            lines.push([prefix, value.map((v) => (v && typeof v === 'object' ? JSON.stringify(v) : String(v))).join(' / ')]);
          } else {
            lines.push([prefix, value]);
          }
        };
        collect('结果', res);
        renderLinkResult('最近一次「写入联动配置」结果', lines.slice(0, 16));
        toast('联动配置已写入两个项目', 'ok', '如服务正在运行，建议到「运行」页重启一次');
        await refreshLink();
        scheduleState(800);
      } catch (err) {
        renderLinkResult('写入联动配置失败', [['错误', err && err.message ? err.message : String(err)]]);
        fail(err, '写入联动配置失败');
      }
    });
  }

  async function sendTest(btn) {
    const type = ($('#link-target-type') || {}).value || 'group';
    const idEl = $('#link-target-id');
    const textEl = $('#link-text');
    const targetId = idEl ? idEl.value.trim() : '';
    const text = textEl ? textEl.value.trim() : '';
    if (!/^\d{5,12}$/.test(targetId)) {
      toast('请填写正确的群号或 QQ 号（5–12 位数字）', 'warn');
      if (idEl) idEl.focus();
      return;
    }
    if (!text) {
      toast('请先填写要发送的内容', 'warn');
      if (textEl) textEl.focus();
      return;
    }
    await withBusy(btn, async () => {
      try {
        const res = await api('/api/link/test-send', {
          method: 'POST',
          body: { target_type: type, target_id: targetId, text },
        });
        const line = [['发送到', `${type === 'group' ? '群' : 'QQ'} ${targetId}`], ['内容', text]];
        Object.keys(res || {}).forEach((k) => { if (k !== 'ok') line.push([k, typeof res[k] === 'object' ? JSON.stringify(res[k]) : res[k]]); });
        renderLinkResult('最近一次测试消息', line);
        toast('测试消息已提交，去 QQ 里看看有没有收到', 'ok');
      } catch (err) {
        renderLinkResult('测试消息发送失败', [['错误', err && err.message ? err.message : String(err)]]);
        fail(err, '发送测试消息失败');
      }
    });
  }

  async function loadWhoami(btn) {
    await withBusy(btn, async () => {
      try {
        const res = await api('/api/link/whoami');
        S.whoami = res;
        const view = $('#link-whoami-view');
        if (view) {
          clear(view);
          const entries = flattenObject(Object.assign({}, res)).filter(([k]) => k !== 'ok');
          if (!entries.length) view.append(el('dt', { text: '状态' }), el('dd', { text: '接口返回为空' }));
          const labels = {
            user_id: 'QQ 号', uin: 'QQ 号', nickname: '昵称', name: '昵称', avatar: '头像',
            ok: '状态', status: '状态', online: '在线', login: '登录',
          };
          entries.forEach(([k, v]) => {
            view.append(el('dt', { text: labels[k] || k }), el('dd', { text: String(v) }));
          });
        }
        toast('已读取 QQ 账号信息', 'ok');
      } catch (err) {
        const view = $('#link-whoami-view');
        if (view) {
          clear(view);
          view.append(el('dt', { text: '状态' }), el('dd', { text: err && err.message ? err.message : '读取失败' }));
        }
        fail(err, '读取 QQ 账号信息失败');
      }
    });
  }

  function appendMessage(ev) {
    const stream = $('#msg-stream');
    if (!stream) return;
    const empty = stream.querySelector('.empty');
    if (empty) empty.remove();

    const dir = ev.direction === 'out' ? 'out' : 'in';
    const row = el('div', { class: 'msg-row', dataset: { dir } }, [
      el('span', { class: 'mt', text: fmtClock(ev.ts || Date.now() / 1000) }),
      el('span', { class: 'dir', text: dir === 'in' ? '收到' : '回复' }),
      el('div', { class: 'body' }, [
        ev.user ? el('span', { class: 'who', text: String(ev.user) }) : null,
        el('span', { class: 'txt', text: String(ev.text === undefined || ev.text === null ? '' : ev.text) }),
      ]),
    ]);
    stream.append(row);
    S.messages.push(ev);
    while (stream.children.length > MAX_MESSAGES) stream.firstElementChild.remove();
    while (S.messages.length > MAX_MESSAGES) S.messages.shift();
    stream.scrollTop = stream.scrollHeight;
    const count = $('#msg-count');
    if (count) count.textContent = `${stream.children.length} 条`;
  }

  async function repairSendLink(btn) {
    await withBusy(btn, async () => {
      try {
        const res = await api('/api/link/repair', { method: 'POST' });
        (res.steps || []).forEach((st) => toast(`${st.ok ? '✓' : '✗'} ${st.name}`, st.ok ? 'ok' : 'error', st.detail || ''));
        toast(res.ok ? '发送链路已恢复' : '修复未成功', res.ok ? 'ok' : 'warn', res.message || '');
        scheduleState(800);
      } catch (err) {
        fail(err, '修复失败');
      }
    });
  }

  async function runLinkSelftest(btn) {
    await withBusy(btn, async () => {
      try {
        const res = await api('/api/link/selftest', { method: 'POST' });
        (res.steps || []).forEach((st) => {
          toast(`${st.ok ? '✓' : '✗'} ${st.name}`, st.ok ? 'ok' : 'error', st.detail || '');
        });
        toast(res.ok ? '双向链路正常' : '自检未通过', res.ok ? 'ok' : 'warn', res.message || '');
      } catch (err) {
        fail(err, '双向自检失败');
      }
    });
  }

  function bindLink() {
    const repairBtn = $('#link-repair');
    if (repairBtn) repairBtn.addEventListener('click', (ev) => repairSendLink(ev.currentTarget));
    const selftestBtn = $('#link-selftest');
    if (selftestBtn) selftestBtn.addEventListener('click', (ev) => runLinkSelftest(ev.currentTarget));
    const apply = $('#link-apply');
    if (apply) apply.addEventListener('click', () => linkApply(apply));
    const refresh = $('#link-refresh');
    if (refresh) refresh.addEventListener('click', () => withBusy(refresh, async () => { await refreshLink(); toast('联动状态已刷新', 'ok'); }));
    const send = $('#link-send');
    if (send) send.addEventListener('click', () => sendTest(send));
    const who = $('#link-whoami');
    if (who) who.addEventListener('click', () => loadWhoami(who));
    const clearBtn = $('#msg-clear');
    if (clearBtn) {
      clearBtn.addEventListener('click', () => {
        const stream = $('#msg-stream');
        if (stream) {
          clear(stream);
          stream.append(el('p', { class: 'empty', text: '还没有消息。去 QQ 里给机器人发一句话，这里会实时出现「收到 / 回复」。' }));
        }
        S.messages = [];
        const count = $('#msg-count');
        if (count) count.textContent = '0 条';
      });
    }
  }

  /* ====================================================================== *
   * 11. SSE 实时通道
   * ====================================================================== */
  let es = null;
  let sseRetry = 0;
  let sseTimer = null;

  function setConn(state, text) {
    const pill = $('#sse-pill');
    const label = $('#sse-text');
    if (!pill) return;
    pill.dataset.state = state;
    const map = { online: '已连接', connecting: '正在连接…', offline: '已断开，重连中…' };
    setText(label, text || map[state] || state);
    S.connected = state === 'online';
  }

  function scheduleReconnect() {
    clearTimeout(sseTimer);
    const delay = Math.min(15000, 1000 * Math.pow(2, Math.min(sseRetry, 4)));
    sseRetry += 1;
    sseTimer = setTimeout(connectSSE, delay);
  }

  function connectSSE() {
    if (es) {
      try { es.close(); } catch (_) { /* 忽略 */ }
      es = null;
    }
    setConn('connecting');
    try {
      es = new EventSource('/api/events');
    } catch (err) {
      setConn('offline');
      scheduleReconnect();
      return;
    }

    es.onopen = () => {
      sseRetry = 0;
      setConn('online');
    };

    es.onerror = () => {
      if (!es) return;
      if (es.readyState === EventSource.CLOSED) {
        try { es.close(); } catch (_) { /* 忽略 */ }
        es = null;
        setConn('offline');
        scheduleReconnect();
      } else {
        setConn('connecting', '连接不稳定，正在重连…');
      }
    };

    es.onmessage = (msg) => {
      if (!msg || !msg.data) return;
      let ev;
      try { ev = JSON.parse(msg.data); } catch (_) { return; }
      handleEvent(ev);
    };
  }

  function handleEvent(ev) {
    if (!ev || !ev.type) return;
    switch (ev.type) {
      case 'hello': {
        if (ev.services) S.services = ev.services;
        if (ev.config && !S.configDirty) applyConfig(ev.config);
        (ev.recent || []).forEach((item) => {
          if (item.type === 'log') appendLogLine(item);
          if (item.type === 'message') appendMessage(item);
        });
        renderRunCards();
        renderOverview();
        break;
      }
      case 'log': {
        appendLogLine(ev);
        if (ev.service === 'deploy') {
          const m = /^(snowluma|astrbot)/i.exec(String(ev.line || ''));
          appendDeployLog(m ? m[1].toLowerCase() : 'snowluma', String(ev.line || ''));
        }
        break;
      }
      case 'service': {
        if (ev.name) {
          const prev = S.services[ev.name] || {};
          S.services[ev.name] = Object.assign({}, prev, {
            name: ev.name,
            status: ev.status || prev.status,
            pid: ev.pid === undefined ? prev.pid : ev.pid,
            ready: ev.ready === undefined ? prev.ready : ev.ready,
            last_error: ev.error || prev.last_error,
          });
        }
        renderRunCards();
        renderOverview();
        scheduleState(1500);
        break;
      }
      case 'deploy': {
        onDeployEvent(ev);
        break;
      }
      case 'message': {
        appendMessage(ev);
        break;
      }
      case 'link': {
        scheduleLinkRefresh();
        break;
      }
      case 'config': {
        if (!S.configDirty) refreshState({ silent: true });
        break;
      }
      default:
        break;
    }
  }

  let linkTimer = null;
  function scheduleLinkRefresh() {
    clearTimeout(linkTimer);
    linkTimer = setTimeout(() => { if (currentPage() === 'link') refreshLink(); }, 1200);
  }

  /* ====================================================================== *
   * 12. 帮助 / 顶栏
   * ====================================================================== */
  function bindChrome() {
    const help = $('#btn-help');
    const dlg = $('#help-dialog');
    if (help && dlg) {
      help.addEventListener('click', () => {
        if (typeof dlg.showModal === 'function') dlg.showModal();
        else dlg.setAttribute('open', '');
      });
    }
    const close = $('#help-close');
    if (close && dlg) close.addEventListener('click', () => { if (typeof dlg.close === 'function') dlg.close(); else dlg.removeAttribute('open'); });

    const restartTop = $('#btn-restart-all');
    if (restartTop) restartTop.addEventListener('click', () => serviceActionAll('restart', restartTop));
    const restartRun = $('#run-restart-all');
    if (restartRun) restartRun.addEventListener('click', () => serviceActionAll('restart', restartRun));
    const startAll = $('#btn-start-all');
    if (startAll) startAll.addEventListener('click', () => serviceActionAll('start', startAll));
    const stopAll = $('#btn-stop-all');
    if (stopAll) stopAll.addEventListener('click', () => serviceActionAll('stop', stopAll));
    const refresh = $('#btn-refresh-state');
    if (refresh) refresh.addEventListener('click', () => withBusy(refresh, () => refreshState({ toast: true })));

    document.addEventListener('visibilitychange', () => {
      if (!document.hidden) refreshState({ silent: true });
    });
  }

  /* ====================================================================== *
   * 13. 启动
   * ====================================================================== */
  function startPolling() {
    clearInterval(stateTimer);
    stateTimer = setInterval(() => {
      if (!document.hidden) refreshState({ silent: true });
    }, 15000);
  }

  function renderSkeleton() {
    const host = $('#status-lights');
    if (!host || host.children.length) return;
    ['SnowLuma 协议端', 'AstrBot 框架', '中继服务', 'OneBot 连接'].forEach((title) => {
      host.append(statusCard({ title, badge: '读取中…', cls: 'muted', tone: 'idle', value: '—', note: '正在获取状态…' }));
    });
    const list = $('#env-list');
    if (list && !list.children.length) list.append(el('li', { class: 'muted', text: '正在检测环境…' }));
    const ports = $('#ports-table tbody');
    if (ports && !ports.children.length) {
      ports.append(el('tr', {}, [el('td', { colspan: '4', class: 'muted', text: '正在读取端口信息…' })]));
    }
  }

  /* ====================================================================== *
   * QQ 客户端：进程列表 / 只注入选中进程 / 隔离启动
   * ====================================================================== */
  let qqInstalls = [];

  function qqRow(row) {
    const wrap = el('div', { class: 'qq-row' });
    const left = el('div', { class: 'qq-main' }, [
      el('b', { text: `PID ${row.pid}` }),
      row.isolated ? el('span', { class: 'badge ok', text: '隔离实例' }) : null,
      el('span', { class: 'muted sm', text: row.age_seconds > 90 ? `${Math.round(row.age_seconds / 60)} 分钟前启动` : '刚启动' }),
    ]);
    const path = el('div', { class: 'qq-path', text: row.exe || row.dir || '', title: row.cmdline || row.exe || '' });
    const actions = el('div', { class: 'u-actions' });
    const use = el('button', { class: 'btn primary sm', type: 'button', text: '用这个' });
    use.addEventListener('click', () => injectQq(row.pid, use));
    const off = el('button', { class: 'btn ghost sm', type: 'button', text: '解除' });
    off.addEventListener('click', () => unloadQq(row.pid, off));
    actions.append(use, off);
    wrap.append(left, path, actions);
    return wrap;
  }

  function renderQq(data) {
    const box = $('#qq-processes');
    if (!box) return;
    clear(box);
    const rows = (data && data.processes) || [];
    if (!rows.length) {
      box.append(el('p', { class: 'muted sm', text: '没有检测到正在运行的 QQ。先打开 QQ 并登录，再点「刷新进程」。' }));
    } else {
      rows.forEach((row) => box.append(qqRow(row)));
    }
    const sel = $('#qq-install-select');
    qqInstalls = (data && data.installs) || [];
    if (sel) {
      clear(sel);
      if (!qqInstalls.length) {
        sel.append(el('option', { value: '', text: '未检测到已安装的 QQ' }));
      } else {
        qqInstalls.forEach((item, index) => {
          sel.append(el('option', {
            value: item.exe,
            text: `${item.version || 'QQ'} · ${item.dir}`,
            selected: index === 0,
          }));
        });
      }
    }
    /* 多开形态以后端为准：clone_mode = 'home'（Linux 独立 HOME）/ 'inject'（Windows DLL 分身）。
       兼容旧后端（没有该字段）时 applyQqModeCopy 会退回平台判定。 */
    qqCloneMode = String((data && data.clone_mode) || '');
    applyQqModeCopy();
    const modeHint = $('#qq-mode-hint');
    const mode = (S.config && S.config.snowluma && S.config.snowluma.hook_mode) || 'manual';
    if (modeHint) {
      const small = (S.config && S.config.snowluma && S.config.snowluma.qq_uin) || '';
      modeHint.textContent = mode === 'auto'
        ? '当前是「自动注入」：SnowLuma 会接管所有 QQ（配置页可改）'
        : `当前是「手动注入」：只注入你点「用这个」的那个 QQ${small ? `；不是小号 ${small} 会自动解除` : ''}`;
    }
  }

  async function loadQq(btn) {
    const box = $('#qq-processes');
    const run = async () => {
      try {
        const data = await api('/api/qq/summary');
        renderQq(data);
      } catch (err) {
        if (box) {
          clear(box);
          box.append(el('p', { class: 'muted sm', text: `读取 QQ 失败：${err.message || err}` }));
        }
      }
    };
    if (btn) await withBusy(btn, run);
    else await run();
  }

  async function injectQq(pid, btn) {
    await withBusy(btn, async () => {
      try {
        const expect = (S.config && S.config.snowluma && S.config.snowluma.qq_uin) || '';
        const res = await api('/api/qq/inject', { method: 'POST', body: { pid, expect_uin: expect } });
        if (res.uin) toast(`已注入 PID ${pid}（账号 ${res.uin}）`, 'ok', '看日志：[Hook] login detected 即接入成功');
        else toast(`已请求注入 PID ${pid}`, 'ok', '看日志：[Hook] login detected 即接入成功');

        scheduleState(800);
      } catch (err) {
        fail(err, '注入失败');
      }
    });
  }

  async function unloadQq(pid, btn) {
    await withBusy(btn, async () => {
      try {
        await api('/api/qq/unload', { method: 'POST', body: { pid } });
        toast(`已解除 PID ${pid} 的注入`, 'ok');
      } catch (err) {
        fail(err, '解除注入失败');
      }
    });
  }

  async function launchIsolatedQq(btn) {
    const sel = $('#qq-install-select');
    const source = sel ? sel.value : '';
    await withBusy(btn, async () => {
      /* 独立 HOME 模式（clone_mode='home'，Linux）：没有「深度分身」（DLL 注入），多开靠独立 HOME。
         QQ 的单实例锁在 $HOME/.config/QQ/SingletonLock，换 HOME 才能起第二个实例。 */
      if (qqHomeMode()) {
        try {
          const res = await api('/api/qq/launch-isolated', { method: 'POST', body: { source_exe: source } });
          toast('已用独立 HOME 启动多开 QQ', 'ok', res.note || '在窗口里登录小号，再回来点「刷新进程 → 用这个」');
          await loadQq();
          scheduleState(1000);
        } catch (err) {
          fail(err, '独立 HOME 多开失败');
        }
        return;
      }
      // Windows：优先用"深度分身"（注入 sandbox DLL：互斥体改名 + 已知文件夹重定向），
      // 组件缺失时退化为只改环境变量的轻量隔离。
      try {
        const status = await api('/api/qq/clone/status');
        if (status && status.sandbox_ready) {
          const res = await api('/api/qq/clone/launch', { method: 'POST', body: { source_exe: source } });
          toast('已启动深度分身 QQ', 'ok', `pid ${res.pid} · ${res.note || ''}`);
          await loadQq();
          scheduleState(1200);
          return;
        }
      } catch (err) {
        /* 落到轻量隔离 */
      }
      try {
        const res = await api('/api/qq/launch-isolated', { method: 'POST', body: { source_exe: source } });
        toast('已启动轻量隔离 QQ', 'warn', res.note || '缺少沙箱组件，只做了环境变量隔离');
        await loadQq();
        scheduleState(1000);
      } catch (err) {
        fail(err, '隔离启动失败');
      }
    });
  }

  /* ------------------------------------------------------------ 面板凭据 */
  async function loadPanels() {
    const box = $('#panel-list');
    if (!box) return;
    try {
      const data = await api('/api/panels');
      clear(box);
      [['snowluma', data.snowluma], ['astrbot', data.astrbot]].forEach(([key, item]) => {
        if (!item) return;
        const row = el('div', { class: 'panel-row' });
        row.append(el('div', { class: 'p-head' }, [
          el('b', { text: item.label }),
          el('span', { class: 'badge ' + (item.ok ? 'ok' : 'muted'), text: item.ok ? '可用' : '未知' }),
        ]));
        row.append(el('div', { class: 'p-meta' }, [
          el('span', { class: 'mono', text: item.url }),
          el('span', { class: 'muted sm', text: `用户名 ${item.username || '-'}` }),
        ]));
        const pw = el('code', { class: 'panel-pw', text: item.password || '（未知，见日志）' });
        row.append(el('div', { class: 'p-pw-row' }, [el('span', { class: 'muted sm', text: '密码' }), pw]));
        const actions = el('div', { class: 'u-actions' });
        const copy = el('button', { class: 'btn ghost sm', type: 'button', text: '复制密码' });
        copy.addEventListener('click', () => copyText(item.password || '', `${item.label} 密码已复制`));
        const open = el('button', { class: 'btn ghost sm', type: 'button', text: '打开' });
        open.addEventListener('click', () => window.open(item.url, '_blank', 'noopener'));
        const login = el('button', { class: 'btn primary sm', type: 'button', text: '一键登录' });
        login.addEventListener('click', () => oneClickLogin(item));
        actions.append(copy, open, login);
        if (key === 'astrbot') {
          const reset = el('button', { class: 'btn ghost sm', type: 'button', text: '重置密码' });
          reset.title = 'AstrBot 的初始口令只在首次启动打印一次；这里直接按它的格式重设';
          reset.addEventListener('click', () => resetAstrBotPassword(reset));
          actions.append(reset);
        }
        row.append(actions);
        box.append(row);
      });
    } catch (err) {
      clear(box);
      box.append(el('p', { class: 'muted sm', text: `读取面板凭据失败：${err.message || err}` }));
    }
  }

  async function resetAstrBotPassword(btn) {
    if (!window.confirm('重置 AstrBot 面板密码？\nAstrBot 会被重启一次（约 5~10 秒），机器人在这个期间不回复。')) return;
    await withBusy(btn, async () => {
      try {
        const res = await api('/api/panels/astrbot-password', { method: 'POST' });
        toast('AstrBot 面板密码已重置', 'ok', `新密码：${res.password}`);
        await loadPanels();
        scheduleState(1000);
      } catch (err) {
        fail(err, '重置密码失败');
      }
    });
  }

  async function oneClickLogin(item) {
    const desktop = window.slabDesktop;
    if (desktop && typeof desktop.openPanel === 'function') {
      try {
        await desktop.openPanel(item.url, { username: item.username, password: item.password });
        return;
      } catch (err) {
        toast(`内嵌登录失败：${err.message || err}`, 'warn', '已改为在浏览器中打开');
      }
    }
    window.open(item.url, '_blank', 'noopener');
    toast('已在浏览器打开面板', 'info', '浏览器模式无法自动填表，请复制密码手动登录');
  }

  /* ----------------------------------------------------------- 升级控制台 */
  async function loadUpgrade() {
    if (!$('#upgrade-box')) return;
    try {
      const plan = await api('/api/upgrade/plan');
      const cur = $('#upgrade-current');
      if (cur) cur.textContent = `当前版本 v${plan.current_version}`;
      const keep = $('#upgrade-keep');
      if (keep) {
        const names = (plan.keep || []).map((p) => String(p).split(/[\\/]/).pop());
        keep.textContent = names.length ? `保留：${names.join(' / ')}` : '';
      }
    } catch (err) {
      /* 拿不到就保持默认文案 */
    }
  }

  async function pickUpgradeZip() {
    const desktop = window.slabDesktop;
    if (desktop && typeof desktop.pickZip === 'function') {
      try {
        const res = await desktop.pickZip();
        if (res && res.ok) {
          const input = $('#upgrade-zip');
          if (input) input.value = res.path;
          return true;
        }
      } catch (err) {
        /* 落到手输 */
      }
    }
    toast('浏览器模式：把 zip 路径粘到输入框里', 'info');
    return false;
  }

  async function checkUpgrade(btn) {
    const input = $('#upgrade-zip');
    const zip = (input && input.value || '').trim();
    if (!zip) { toast('先选择升级包（.zip）', 'warn'); return; }
    await withBusy(btn, async () => {
      try {
        const info = await api('/api/upgrade/inspect', { method: 'POST', body: { zip } });
        const infoEl = $('#upgrade-info');
        if (infoEl) {
          let tail = '比当前版本旧，需要强制才能装';
          if (info.newer) tail = '比当前版本新，可以升级';
          else if (info.same) tail = '与当前版本相同，可用于修复损坏文件';
          infoEl.textContent = `升级包 v${info.version}（${info.size_mb} MB / ${info.files} 个文件）· 当前 v${info.current} — ${tail}`;
        }
        const run = $('#upgrade-run');
        if (run) run.disabled = !(info.newer || info.same);
      } catch (err) {
        fail(err, '升级包检查失败');
      }
    });
  }

  async function runUpgrade(btn) {
    const input = $('#upgrade-zip');
    const zip = (input && input.value || '').trim();
    if (!zip) return;
    const ok = window.confirm(
      '升级流程：备份程序文件 → 程序自动退出 → 用新版本覆盖 → 自动重启。\n\n'
      + '配置、SnowLuma / AstrBot 的安装、下载缓存都不会动。\n现在开始吗？'
    );
    if (!ok) return;
    await withBusy(btn, async () => {
      try {
        const res = await api('/api/upgrade/apply', { method: 'POST', body: { zip } });
        toast('正在升级，程序马上会自己重启', 'info', res.message || '');
        setTimeout(async () => {
          const desktop = window.slabDesktop;
          if (desktop && typeof desktop.quit === 'function') {
            try { await desktop.quit(); return; } catch (err) { /* 落到后端退出 */ }
          }
          try { await fetch('/api/system/shutdown', { method: 'POST' }); } catch (err) { /* 忽略 */ }
        }, 1600);
      } catch (err) {
        fail(err, '升级未能开始');
      }
    });
  }

  function bindUpgrade() {
    if (!$('#upgrade-box')) return;
    $('#upgrade-pick')?.addEventListener('click', () => pickUpgradeZip());
    $('#upgrade-check')?.addEventListener('click', (ev) => checkUpgrade(ev.currentTarget));
    $('#upgrade-run')?.addEventListener('click', (ev) => runUpgrade(ev.currentTarget));
    loadUpgrade();
  }

  /* ------------------------------------------------- 桌面壳：无边框窗口控制 */
  function bindLongPress(el, ms, done) {
    if (!el) return;
    const ring = el.querySelector('.ring');
    let raf = 0;
    let start = 0;
    const paint = (p) => { if (ring) ring.style.setProperty('--p', String(p * 100)); };
    const stop = () => {
      if (raf) cancelAnimationFrame(raf);
      raf = 0;
      el.classList.remove('holding');
      paint(0);
    };
    const tick = () => {
      const p = Math.min(1, (performance.now() - start) / ms);
      paint(p);
      if (p >= 1) {
        stop();
        el.classList.add('fired');
        setTimeout(() => el.classList.remove('fired'), 420);
        done();
        return;
      }
      raf = requestAnimationFrame(tick);
    };
    el.addEventListener('pointerdown', (ev) => {
      ev.preventDefault();
      el.classList.add('holding');
      start = performance.now();
      tick();
    });
    ['pointerup', 'pointerleave', 'pointercancel'].forEach((type) => el.addEventListener(type, stop));
    el.addEventListener('keydown', (ev) => { if (ev.key === 'Enter' || ev.key === ' ') { ev.preventDefault(); done(); } });
  }

  function bindWinControls() {
    const desktop = window.slabDesktop;
    const box = $('#win-controls');
    if (!desktop || !box) return;
    box.hidden = false;
    document.body.classList.add('desktop-shell');
    const legacyQuit = $('#btn-quit');
    if (legacyQuit) legacyQuit.hidden = true;   // 桌面壳不再用传统"退出"按钮

    const min = $('#win-min');
    if (min) min.addEventListener('click', () => desktop.winAction('minimize'));
    const maxBtn = $('#win-max');
    if (maxBtn) {
      maxBtn.addEventListener('click', async () => {
        try {
          const res = await desktop.winAction('maximize');
          maxBtn.setAttribute('aria-pressed', res && res.maximized ? 'true' : 'false');
        } catch (err) { /* 忽略 */ }
      });
    }
    const pin = $('#win-pin');
    if (pin) {
      pin.addEventListener('click', async () => {
        try {
          const res = await desktop.winAction('pin');
          pin.setAttribute('aria-pressed', res && res.pinned ? 'true' : 'false');
          toast(res && res.pinned ? '窗口已置顶' : '取消置顶', 'info');
        } catch (err) { /* 忽略 */ }
      });
    }

    const dialog = $('#power-dialog');
    const openSheet = () => { if (dialog && typeof dialog.showModal === 'function') dialog.showModal(); };
    bindLongPress($('#win-power'), 1000, openSheet);
    if (dialog) {
      $('#power-cancel')?.addEventListener('click', () => dialog.close());
      $('#power-min')?.addEventListener('click', async () => {
        dialog.close();
        await desktop.winAction('minimize');
      });
      $('#power-quit')?.addEventListener('click', async () => {
        dialog.close();
        toast('正在停止服务并退出…', 'info');
        try { await desktop.quit(); } catch (err) { toast(`退出失败：${err.message || err}`, 'error'); }
      });
    }

    // Ctrl+Q 走同一套"要收起还是退出"的流程，避免误关
    document.addEventListener('keydown', (ev) => {
      if ((ev.ctrlKey || ev.metaKey) && ev.key.toLowerCase() === 'q') {
        ev.preventDefault();
        openSheet();
      }
    });
  }

  function bindQq() {
    const refresh = $('#qq-refresh');
    if (refresh) refresh.addEventListener('click', () => loadQq(refresh));
    const launch = $('#qq-launch');
    if (launch) launch.addEventListener('click', () => launchIsolatedQq(launch));
    window.addEventListener('slab:run-open', () => { loadQq(); loadPanels(); });
  }

  async function boot() {
    const t0 = performance.now();
    const mark = (label) => {
      try { console.info(`[boot] ${label}: ${Math.round(performance.now() - t0)}ms`); } catch (e) {}
    };
    window.__slabBootMark = mark;
    // 首屏绘制耗时（判断"窗口出来了但一片空白"是不是渲染卡住）
    try {
      new PerformanceObserver((list) => {
        list.getEntries().forEach((e) => {
          if (e.name === 'first-contentful-paint') mark('first-paint');
        });
      }).observe({ type: 'paint', buffered: true });
    } catch (err) { /* 老内核忽略 */ }
    bindChrome();
    bindNav();
    bindConfigForm();
    bindDeploy();
    bindLogs();
    bindLink();
    bindQq();
    bindWinControls();
    bindUpgrade();
    $('#cfg-sync-account')?.addEventListener('click', (ev) => syncLoggedInAccount(ev.currentTarget));
    $('#cfg-test-provider')?.addEventListener('click', (ev) => testProvider(ev.currentTarget));
    $('#cfg-fetch-models')?.addEventListener('click', (ev) => fetchProviderModels(ev.currentTarget));
    setConn('connecting');
    go(currentPage(), { replace: true });
    renderSkeleton();
    // 上次会话记住的平台（localStorage）先把文案切对，state 回来后再校准一次
    applyPlatformCopy();
    // 先连实时通道，再取状态快照：环境自检较慢时页面也不会一片空白
    connectSSE();
    startPolling();
    mark('shell-ready');
    await refreshState({ silent: true });
    mark('state-ready');
    // 打开页面时给一句欢迎提示，避免用户不知道从哪开始
    setTimeout(() => {
      if (!S.config || !(S.env && S.env.installed && S.env.installed.snowluma)) {
        toast('第一次使用？先去「部署」页安装两个项目', 'info');
      }
    }, 1200);
  }

  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', boot);
  else boot();
}());