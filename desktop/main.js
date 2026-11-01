/**
 * Electron 桌面外壳：把已经做好的 FastAPI 控制台包进一个原生窗口里。
 *
 * 为什么要这层壳？
 *   浏览器模式（直接开 http://127.0.0.1:8790）关掉标签页后，后台的 python 控制台
 *   和它拉起的 SnowLuma / AstrBot 还在跑，用户找不到关的地方。
 *   桌面壳把「窗口生命周期」和「后端进程」绑在一起：
 *     - 启动：拉起后端（隐藏窗口）→ 等 /api/health 就绪 → 打开窗口
 *     - 关窗：先调 /api/system/shutdown 让后端优雅停服务，再 taskkill 整棵进程树
 *     - 后端意外退出：弹窗告知并关闭窗口（不留僵尸窗口）
 *
 * 后端来源（按优先级）：
 *   1. 环境变量 SLAB_BACKEND_EXE
 *   2. resources/backend/SnowLuma-AstrBot-Console.exe（打包后）
 *   3. ../dist/SnowLuma-AstrBot-Console/SnowLuma-AstrBot-Console.exe（开发时）
 */

const { app, BrowserWindow, Menu, dialog, shell, ipcMain } = require('electron');
const { spawn, execFile, execFileSync } = require('child_process');
const fs = require('fs');
const http = require('http');
const net = require('net');
const path = require('path');

const SMOKE = process.argv.includes('--smoke-test');
const IS_WIN = process.platform === 'win32';
const APP_TITLE = 'SnowLuma × AstrBot 联合控制台';

let backend = null;          // 后端子进程
let backendPort = 0;
let win = null;              // 主窗口
let splash = null;           // 启动画面
let quitting = false;        // 是否处于主动退出流程
let backendExitHandled = false;

/* ------------------------------------------------------------------ 工具 */
function log(...args) {
  console.log('[desktop]', ...args);
}

function notify(title, message) {
  try {
    dialog.showErrorBox(title, message);
  } catch (err) {
    log('dialog failed', err && err.message);
  }
}

function resolveBackendExe() {
  const candidates = [
    process.env.SLAB_BACKEND_EXE,
    path.join(process.resourcesPath || '', 'backend', 'SnowLuma-AstrBot-Console.exe'),
    path.join(__dirname, '..', 'dist', 'SnowLuma-AstrBot-Console', 'SnowLuma-AstrBot-Console.exe'),
  ].filter(Boolean);

  for (const candidate of candidates) {
    if (fs.existsSync(candidate)) return candidate;
  }
  return null;
}

function getFreePort(preferred) {
  return new Promise((resolve) => {
    const tryBind = (port) => {
      const srv = net.createServer();
      srv.once('error', () => {
        if (port < preferred + 40) tryBind(port + 1);
        else resolve(0);
      });
      srv.once('listening', () => {
        srv.close(() => resolve(port));
      });
      srv.listen(port, '127.0.0.1');
    };
    tryBind(preferred);
  });
}

function httpGet(port, urlPath, timeoutMs = 1500) {
  return new Promise((resolve) => {
    const req = http.get({ host: '127.0.0.1', port, path: urlPath, timeout: timeoutMs }, (res) => {
      let body = '';
      res.on('data', (chunk) => { body += chunk; });
      res.on('end', () => resolve({ status: res.statusCode, body }));
    });
    req.on('timeout', () => { req.destroy(); resolve({ status: 0, body: '' }); });
    req.on('error', () => resolve({ status: 0, body: '' }));
  });
}

function httpPost(port, urlPath, timeoutMs = 2500) {
  return new Promise((resolve) => {
    const req = http.request(
      { host: '127.0.0.1', port, path: urlPath, method: 'POST', timeout: timeoutMs },
      (res) => { res.resume(); res.on('end', () => resolve(res.statusCode)); },
    );
    req.on('timeout', () => { req.destroy(); resolve(0); });
    req.on('error', () => resolve(0));
    req.end();
  });
}

async function waitHealthy(port, timeoutMs = 45000) {
  const started = Date.now();
  while (Date.now() - started < timeoutMs) {
    const res = await httpGet(port, '/api/health');
    if (res.status === 200 && res.body.includes('"ok"')) return true;
    await new Promise((r) => setTimeout(r, 350));
  }
  return false;
}

function killTree(pid) {
  if (!pid) return;
  if (IS_WIN) {
    // /T 连带子进程（SnowLuma 的 node.exe、AstrBot 的 python.exe）
    execFile('taskkill', ['/PID', String(pid), '/T', '/F'], { windowsHide: true }, (err) => {
      if (err) log('taskkill:', err.message);
    });
  } else {
    try { process.kill(-pid, 'SIGTERM'); } catch (e) { log('kill:', e.message); }
  }
}

/* --------------------------------------------------------- 启动 / 停止后端 */
/**
 * 选一个"放数据/安装"的根目录：默认非 C 盘（剩余空间最大的本地固定盘），
 * 只有一块盘时才用 C。结果形如 D:\SnowLumaAstrBot。
 */
function pickStorageRoot() {
  const env = (process.env.SLAB_STORAGE_ROOT || '').trim();
  if (env) return env;

  // 不用 PowerShell 枚举磁盘：execFileSync 会阻塞主线程 1~2 秒，
  // 那几秒里 splash / 窗口就是"未响应"状态。fs.statfsSync 是毫秒级的系统调用。
  const drives = [];
  for (let code = 68; code <= 90; code += 1) {   // D: .. Z:
    const root = `${String.fromCharCode(code)}:\\`;
    try {
      if (!fs.existsSync(root)) continue;
      const st = fs.statfsSync(root);
      const free = Number(st.bavail) * Number(st.bsize);
      if (free > 0) drives.push({ root, free });
    } catch (err) { /* 光驱/网络盘读不到就跳过 */ }
  }
  drives.sort((a, b) => b.free - a.free);
  const base = drives.length ? drives[0].root : 'C:\\';
  log('storage root:', base, '（非系统盘候选:', drives.map((d) => d.root).join(',') || '无', '）');
  return path.join(base, 'SnowLumaAstrBot');
}

async function startBackend() {
  const exe = resolveBackendExe();
  if (!exe) {
    notify(APP_TITLE, '找不到控制台后端程序。\n\n请确认 resources\\backend\\SnowLuma-AstrBot-Console.exe 存在，'
      + '或设置环境变量 SLAB_BACKEND_EXE 指向它。');
    return false;
  }

  backendPort = await getFreePort(8790);
  if (!backendPort) {
    notify(APP_TITLE, '找不到可用端口（8790-8830 都被占用）。请先关掉占用端口的程序再试。');
    return false;
  }

  // 数据 / 运行时 / 下载缓存：默认放到**非 C 盘**（只有一块盘时才用 C）；
  // 老用户（userData 里已有 data 或 runtime）沿用原位置，升级不丢配置。
  const userData = app.getPath('userData');
  const storageRoot = pickStorageRoot();
  const legacyData = fs.existsSync(path.join(userData, 'data', 'config.json'));
  const legacyRuntime = ['snowluma', 'astrbot'].some((n) => fs.existsSync(path.join(userData, 'runtime', n)));

  const dataDir = process.env.SLAB_DATA_DIR
    || (legacyData ? path.join(userData, 'data') : path.join(storageRoot, 'data'));
  const runtimeDir = process.env.SLAB_RUNTIME
    || (legacyRuntime ? path.join(userData, 'runtime') : path.join(storageRoot, 'runtime'));
  const downloadsDir = process.env.SLAB_DOWNLOAD_DIR || path.join(storageRoot, 'downloads');

  for (const dir of [dataDir, runtimeDir, downloadsDir]) {
    try {
      fs.mkdirSync(dir, { recursive: true });
    } catch (err) {
      log(`创建目录失败（回退到用户数据目录）: ${dir}: ${err.message}`);
    }
  }
  log('storage:', { root: storageRoot, dataDir, runtimeDir, downloadsDir,
                    legacyData, legacyRuntime });

  log('backend:', exe, 'port:', backendPort);
  backend = spawn(exe, ['serve', '--port', String(backendPort), '--no-browser'], {
    env: {
      ...process.env,
      SLAB_DATA_DIR: dataDir,
      SLAB_RUNTIME: runtimeDir,
      SLAB_DOWNLOAD_DIR: downloadsDir,
      SLAB_DESKTOP: '1',
      PYTHONUNBUFFERED: '1',
      PYTHONIOENCODING: 'utf-8',
    },
    windowsHide: true,
    stdio: ['ignore', 'pipe', 'pipe'],
  });

  backend.stdout.on('data', (d) => log('[py]', String(d).trim()));
  backend.stderr.on('data', (d) => log('[py!]', String(d).trim()));
  backend.on('exit', (code, signal) => {
    log('backend exited', code, signal);
    backend = null;
    if (quitting || backendExitHandled) return;
    backendExitHandled = true;
    notify(APP_TITLE, `控制台后端意外退出（退出码 ${code}）。\n\n可以在用户数据目录的 data\\logs\\console.log 里查看原因。`);
    app.quit();
  });

  const ok = await waitHealthy(backendPort);
  if (!ok) {
    notify(APP_TITLE, '控制台后端启动超时（45 秒内没就绪）。\n\n可能是端口被占用、杀软拦截，或缺少运行库。');
    return false;
  }
  return true;
}

async function stopBackend() {
  if (!backend) return;
  const pid = backend.pid;
  // 1) 优雅：让后端停掉它拉起的 SnowLuma / AstrBot，再退出
  try {
    await httpPost(backendPort, '/api/system/shutdown');
  } catch (err) {
    log('shutdown request failed', err && err.message);
  }
  // 2) 等它自己退（最多 6 秒），仍然活着就强杀整棵树兜底
  const deadline = Date.now() + 6000;
  let alive = true;
  while (Date.now() < deadline) {
    if (!backend || backend.exitCode !== null) { alive = false; break; }
    await new Promise((r) => setTimeout(r, 200));
  }
  if (alive) {
    log('backend did not exit in time, killing tree', pid);
    killTree(pid);
  }
  await new Promise((r) => setTimeout(r, 400));
  backend = null;
}

/* -------------------------------------------------------------------- 窗口 */
function createSplash() {
  splash = new BrowserWindow({
    width: 460,
    height: 260,
    frame: false,
    resizable: false,
    show: true,
    backgroundColor: '#f2f2f5',
    webPreferences: { contextIsolation: true, nodeIntegration: false },
  });
  splash.loadFile(path.join(__dirname, 'splash.html'));
}

function createWindow(targetUrl) {
  win = new BrowserWindow({
    width: 1440,
    height: 920,
    minWidth: 1024,
    minHeight: 680,
    show: false,
    // 无边框：标题栏交给页面自己画，关闭方式改成"长按电源键"
    frame: false,
    roundedCorners: true,
    minimizable: true,
    maximizable: true,
    resizable: true,
    fullscreenable: true,
    skipTaskbar: false,
    backgroundColor: '#f2f2f5',
    title: APP_TITLE,
    icon: fs.existsSync(path.join(__dirname, 'assets', 'icon.ico'))
      ? path.join(__dirname, 'assets', 'icon.ico')
      : undefined,
    webPreferences: {
      preload: path.join(__dirname, 'preload.js'),
      contextIsolation: true,
      nodeIntegration: false,
      spellcheck: false,
    },
  });

  // 只允许在窗口内看本机控制台；外链（AstrBot / SnowLuma 面板）交给系统浏览器
  win.webContents.setWindowOpenHandler(({ url }) => {
    if (/^https?:\/\/(127\.0\.0\.1|localhost)(:|\/)/.test(url)) {
      return { action: 'allow' };
    }
    shell.openExternal(url);
    return { action: 'deny' };
  });

  const hash = process.env.SLAB_HASH ? `#${process.env.SLAB_HASH.replace(/^#/, "")}` : "";
  win.loadURL(targetUrl || `http://127.0.0.1:${backendPort}/?desktop=1${hash}`);
  win.once('ready-to-show', () => {
    if (splash) { splash.destroy(); splash = null; }
    win.show();
  });
  // 关窗（Alt+F4 / 系统关闭 / 电源键的"彻底退出"）统一走：停服务 → 杀后端树 → 关窗口
  win.on('close', (event) => {
    if (quitting) return;
    event.preventDefault();
    quitApp('window-close');
  });

  win.on('closed', () => { win = null; });

  // 自检：SLAB_WIN_SELFTEST=1 时依次验证 最小化 / 最大化 / 还原 / 置顶，并打印结果
  if (process.env.SLAB_WIN_SELFTEST) {
    win.webContents.once('did-finish-load', async () => {
      const wait = (ms) => new Promise((r) => setTimeout(r, ms));
      try {
        const bridge = await win.webContents.executeJavaScript('typeof window.slabDesktop?.winAction');
        log('[selftest] renderer winAction:', bridge);
        // 等窗口真正可见再测（隐藏状态下 minimize() 是空操作，会得出错误结论）
        for (let i = 0; i < 20 && !win.isVisible(); i += 1) { await wait(200); }
        log('[selftest] visible =', win.isVisible());
        win.minimize();
        await wait(900);
        log('[selftest] minimized =', win.isMinimized());
        if (!win.isMinimized()) { win.hide(); }
        win.restore();
        await wait(400);
        win.maximize();
        await wait(700);
        log('[selftest] maximized =', win.isMaximized());
        win.unmaximize();
        await wait(400);
        win.setAlwaysOnTop(true, 'floating');
        log('[selftest] alwaysOnTop =', win.isAlwaysOnTop());
        win.setAlwaysOnTop(false);
        log('[selftest] done');
        if (process.env.SLAB_WIN_SELFTEST === 'quit') {
          log('[selftest] asking renderer to quit…');
          await win.webContents.executeJavaScript('window.slabDesktop.quit()');
        }
      } catch (err) {
        log('[selftest] error', err && err.message);
      }
    });
  }

  // 强制主题（SLAB_THEME=light|dark），截图/演示用
  if (process.env.SLAB_THEME) {
    const theme = /^dark/i.test(process.env.SLAB_THEME) ? 'dark' : 'light';
    win.webContents.once('did-finish-load', async () => {
      try {
        await win.webContents.executeJavaScript(`try{localStorage.setItem('slab-theme','${theme}')}catch(e){}`);
        win.webContents.reload();
      } catch (err) { log('set theme failed', err && err.message); }
    });
  }

  // 用哪种界面语言：SLAB_LANG=en 可强制英文（等价于用户点顶栏「EN」）
  if (process.env.SLAB_LANG) {
    const lang = /^en/i.test(process.env.SLAB_LANG) ? 'en-US' : 'zh-CN';
    win.webContents.once('did-finish-load', async () => {
      try {
        await win.webContents.executeJavaScript(
          `try{localStorage.setItem('slab-lang','${lang}')}catch(e){}`,
        );
        if (win.webContents.getURL()) win.webContents.reload();  // 语言变更后重载一次，立刻生效
      } catch (err) {
        log('set lang failed', err && err.message);
      }
    });
  }

  // SLAB_DEBUG=1：把渲染进程的 console 转发到主进程日志（排错用）
  if (process.env.SLAB_DEBUG) {
    win.webContents.on('console-message', (_e, level, message, line, sourceId) => {
      log(`[renderer:${level}] ${message} (${sourceId || ''}:${line || 0})`);
    });
  }
  // 调试用：SLAB_SCROLL=像素 时先滚动到指定位置（截长页面的下半部分）
  if (process.env.SLAB_SCROLL) {
    win.webContents.once('did-finish-load', () => {
      setTimeout(() => {
        win.webContents.executeJavaScript(`window.scrollTo(0, ${Number(process.env.SLAB_SCROLL) || 0})`)
          .catch(() => {});
      }, 1200);
    });
  }
  // 调试用：SLAB_KEYS="ctrl+q" 时在截图前派发一次按键（验证长按电源键/对话框）
  if (process.env.SLAB_KEYS) {
    win.webContents.once('did-finish-load', () => {
      setTimeout(() => {
        const seq = String(process.env.SLAB_KEYS).toLowerCase().split('+');
        const key = seq.pop();
        const mods = seq;
        try {
          win.webContents.sendInputEvent({ type: 'keyDown', keyCode: key,
            modifiers: mods.includes('ctrl') ? ['control'] : undefined });
          win.webContents.sendInputEvent({ type: 'keyUp', keyCode: key,
            modifiers: mods.includes('ctrl') ? ['control'] : undefined });
        } catch (err) { log('SLAB_KEYS failed', err.message); }
      }, 2500);
    });
  }

  // 调试/验收用：SLAB_SHOT=路径 时，加载完成后截一张窗口图再退出
  if (process.env.SLAB_SHOT) {
    win.webContents.once('did-finish-load', () => {
      setTimeout(async () => {
        try {
          const image = await win.webContents.capturePage();
          fs.writeFileSync(process.env.SLAB_SHOT, image.toPNG());
          log('screenshot saved:', process.env.SLAB_SHOT);
        } catch (err) {
          log('screenshot failed', err && err.message);
        }
        app.quit();
      }, Number(process.env.SLAB_SHOT_DELAY || 2500));
    });
  }
}

function buildMenu() {
  const template = [
    {
      label: '控制台',
      submenu: [
        { label: '重新加载页面', accelerator: 'CmdOrCtrl+R', click: () => win && win.reload() },
        {
          label: '打开数据目录',
          click: () => shell.openPath(app.getPath('userData')),
        },
        { type: 'separator' },
        { label: '退出', accelerator: 'CmdOrCtrl+Q', click: () => app.quit() },
      ],
    },
    {
      label: '视图',
      submenu: [
        { role: 'togglefullscreen' },
        { role: 'zoomIn' },
        { role: 'zoomOut' },
        { role: 'resetZoom' },
        { label: '开发者工具', accelerator: 'F12', click: () => win && win.webContents.toggleDevTools() },
      ],
    },
    {
      label: '帮助',
      submenu: [
        { label: '版本信息', click: () => notify(APP_TITLE, `桌面外壳 1.0.0\nElectron ${process.versions.electron}\n后端端口 ${backendPort || '-'}`) },
      ],
    },
  ];
  Menu.setApplicationMenu(Menu.buildFromTemplate(template));
}

/* ------------------------------------------------------------------ 生命周期 */
const gotLock = app.requestSingleInstanceLock();
if (!gotLock) {
  app.quit();
} else {
  app.on('second-instance', () => {
    if (win) { if (win.isMinimized()) win.restore(); win.focus(); }
  });

  app.whenReady().then(async () => {
    buildMenu();

    // 开发/调样式用：SLAB_URL 直接连一个已经跑着的控制台，不再自己拉后端
    if (process.env.SLAB_URL) {
      backendPort = 0;
      createWindow(process.env.SLAB_URL);
      return;
    }

    createSplash();
    const ok = await startBackend();
    if (!ok) {
      if (splash) splash.destroy();
      app.quit();
      return;
    }
    createWindow();

    if (SMOKE) {
      // 自动化冒烟：开窗后延时自动关窗，用来验证「关窗 = 后端（含 SnowLuma/AstrBot）被杀掉」
      // 延迟可用 SLAB_SMOKE_MS 调大，便于在关窗前启动服务做端到端验证
      const delay = Number(process.env.SLAB_SMOKE_MS || 8000);
      log(`smoke: window will close in ${delay}ms`);
      setTimeout(() => {
        log('smoke: closing window');
        if (win) win.close();
      }, delay);
    }

    app.on('activate', () => {
      if (BrowserWindow.getAllWindows().length === 0 && backend) createWindow();
    });
  });

  app.on('window-all-closed', () => {
    app.quit();
  });

  let cleanupDone = false;
  /**
   * 统一退出流程：停服务 → 杀后端进程树 → 关窗口。
   * 加 9 秒兜底：无论卡在哪一步，最后一定 app.exit(0)，不会出现"服务停了窗口还在"。
   */
  let quitInFlight = false;
  function quitApp(reason) {
    if (quitInFlight) return;
    quitInFlight = true;
    quitting = true;
    log(`quit requested (${reason})`);
    const watchdog = setTimeout(() => {
      log('quit watchdog fired → app.exit(0)');
      try { app.exit(0); } catch (err) { process.exit(0); }
    }, 9000);
    stopBackend()
      .catch((err) => log('stopBackend error', err && err.message))
      .finally(() => {
        clearTimeout(watchdog);
        log('quit complete');
        app.exit(0);
      });
  }

  app.on('before-quit', (event) => {
    if (cleanupDone) return;
    event.preventDefault();
    cleanupDone = true;
    quitApp('before-quit');
  });
}

// 渲染进程主动要求关窗（页面里的「退出」按钮）
// 窗口控制：最小化 / 最大化 / 置顶 / 退出（关闭不再用系统标题栏的 ×）
ipcMain.handle('win:action', async (_evt, action) => {
  if (!win || win.isDestroyed()) return { ok: false };
  switch (action) {
    case 'minimize':
      // 坑：对"还没显示出来"的窗口调 minimize() 会被 Windows 直接忽略，
      // 所以先确保窗口可见并拿到焦点，再最小化；失败就退化为 hide()。
      if (!win.isVisible()) win.show();
      win.focus();
      win.minimize();
      await new Promise((r) => setTimeout(r, 120));
      if (!win.isMinimized()) {
        log('minimize() had no effect, fallback to hide()');
        win.hide();
        return { ok: true, minimized: false, hidden: true };
      }
      return { ok: true, minimized: true };
    case 'maximize':
      if (win.isMaximized()) win.unmaximize(); else win.maximize();
      return { ok: true, maximized: win.isMaximized() };
    case 'pin': {
      const next = !win.isAlwaysOnTop();
      win.setAlwaysOnTop(next, 'floating');
      return { ok: true, pinned: next };
    }
    case 'quit':
      quitApp('ipc-quit');
      return { ok: true };
    default:
      return { ok: false, error: `未知窗口操作：${action}` };
  }
});

ipcMain.handle('app:quit', async () => {
  quitApp('app:quit');
  return true;
});

// 渲染进程要求打开面板并自动登录（内嵌窗口 + 自动填表）
ipcMain.handle('panel:open', async (_evt, payload) => {
  const { url, creds } = payload || {};
  if (!url) throw new Error('缺少面板地址');
  const panel = new BrowserWindow({
    width: 1200,
    height: 860,
    title: (creds && creds.label) || '面板',
    autoHideMenuBar: true,
    backgroundColor: '#f2f2f5',
    webPreferences: { contextIsolation: true, nodeIntegration: false, spellcheck: false },
  });
  await panel.loadURL(url);
  const password = (creds && creds.password) || '';
  const username = (creds && creds.username) || '';
  if (password) {
    const script = `(() => {
      const USER = ${JSON.stringify(username)};
      const PASS = ${JSON.stringify(password)};
      // SPA（React/Vue）必须走原生 setter + input 事件，直接 .value= 会被框架忽略
      const nativeSet = (el, value) => {
        const proto = Object.getPrototypeOf(el);
        const desc = Object.getOwnPropertyDescriptor(proto, 'value')
          || Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, 'value');
        if (desc && desc.set) desc.set.call(el, value);
        else el.value = value;
        el.dispatchEvent(new Event('input', { bubbles: true }));
        el.dispatchEvent(new Event('change', { bubbles: true }));
      };
      const visible = (el) => !!(el && el.offsetParent !== null);
      const findInputs = (root, out) => {
        try {
          root.querySelectorAll('input').forEach((el) => out.push(el));
          root.querySelectorAll('iframe').forEach((fr) => {
            try { findInputs(fr.contentDocument, out); } catch (e) {}
          });
        } catch (e) {}
        return out;
      };
      let done = false;
      let tries = 0;
      const timer = setInterval(() => {
        tries += 1;
        if (done) { clearInterval(timer); return; }
        const inputs = findInputs(document, []);
        const pass = inputs.find((el) => el.type === 'password' && visible(el));
        const user = inputs.find((el) => visible(el)
          && (el.type === 'text' || el.type === 'email' || el.type === 'tel')
          && /user|account|name|mail|帐号|账号/i.test(el.name + el.id + el.placeholder + el.autocomplete));
        if (pass && (done = true)) {
          clearInterval(timer);
          if (user && USER) nativeSet(user, USER);
          nativeSet(pass, PASS);
          const scopes = [document];
          try { document.querySelectorAll('iframe').forEach((fr) => { if (fr.contentDocument) scopes.push(fr.contentDocument); }); } catch (e) {}
          const btn = scopes.map((s) => s.querySelector(
            'button[type="submit"], form button, .login-btn, .btn-primary, button.primary, button'
          )).find((b) => b && visible(b) && !b.disabled);
          setTimeout(() => {
            try {
              const form = pass.closest('form');
              if (form && form.requestSubmit) form.requestSubmit();
              else if (btn) btn.click();
              else {
                pass.dispatchEvent(new KeyboardEvent('keydown', { key: 'Enter', keyCode: 13, bubbles: true }));
              }
            } catch (e) {}
          }, 150);
          return;
        }
        if (tries > 66) clearInterval(timer);   // 约 20 秒后放弃
      }, 300);
      return true;
    })();`;
    try {
      await panel.webContents.executeJavaScript(script);
    } catch (err) {
      log('panel autofill failed:', err.message);
    }
  }
  return { ok: true };
});

// 渲染进程要求选目录（安装路径）：用系统原生对话框，比后端 PowerShell 弹窗更稳
ipcMain.handle('dialog:pick-zip', async () => {
  const res = await dialog.showOpenDialog(win || undefined, {
    title: '选择新版本的 zip 升级包',
    filters: [{ name: 'SnowLuma 控制台升级包', extensions: ['zip'] }],
    properties: ['openFile'],
  });
  if (res.canceled || !res.filePaths.length) return { ok: false, canceled: true };
  return { ok: true, path: res.filePaths[0] };
});

ipcMain.handle('dialog:pick-dir', async (_evt, payload) => {
  const { title, defaultPath } = payload || {};
  const result = await dialog.showOpenDialog(win || undefined, {
    title: title || '选择目录',
    defaultPath: defaultPath || undefined,
    properties: ['openDirectory', 'createDirectory'],
  });
  if (result.canceled || !result.filePaths || !result.filePaths.length) return '';
  return result.filePaths[0];
});
