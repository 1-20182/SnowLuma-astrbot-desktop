/* =========================================================================
 * SnowLuma × AstrBot 联合控制台 —— 运行期国际化（中文 ⇄ 英文）
 *
 * 设计要点：
 *   1) 现有 HTML / app.js / term.js 里全是硬编码中文字面量，所以走「中文 → 英文」
 *      词典 + 遍历文本节点替换的路子，不改任何原有文件。
 *   2) 只动文本节点（整串命中才翻，保留前后空白）与 title / placeholder /
 *      aria-label 三个属性；绝不使用 innerHTML，不碰事件、不碰 DOM 结构。
 *   3) 幂等：每个被替换过的文本节点在 WeakMap 里记 { src: 中文原文, out: 英文结果 }，
 *      切回 zh-CN 时按 src 原样还原；节点被外部（app.js 每秒刷新）改写时，
 *      自动把新内容当成新的中文原文。
 *   4) 动态文本（运行 12 分 3 秒 / 已下载 18.2 MB / 41.0 MB（44%）/ 3/4 条正常 …）
 *      走模式规则，不依赖词典穷举。
 *   5) 对 document.body 挂 MutationObserver（childList + characterData，
 *      节流 120ms），app.js / term.js 动态渲染出来的新节点也会被翻译。
 *
 * 对外 API（挂在 window.slabI18n）：
 *   LANG_KEY            localStorage key，固定 'slab-lang'
 *   current()           'zh-CN' | 'en-US'
 *   setLang(lang)       立即生效：刷新所有已翻译节点 + 写 localStorage
 *   toggle()            中 ⇄ 英
 *   t(zhText)           查词典 / 模式；查不到返回原文
 *   apply(root)         翻译 root（默认 document.body）下所有文本 / 属性
 *   onChange(fn)        注册语言变化回调，返回取消函数（注册时立即回调一次当前语言）
 *
 * 接线方式见文件末尾注释。
 * ========================================================================= */
(function () {
  'use strict';

  if (typeof window === 'undefined' || typeof document === 'undefined') return;

  /* ====================================================================== *
   * 0. 常量
   * ====================================================================== */
  var LANG_KEY = 'slab-lang';
  var ZH = 'zh-CN';
  var EN = 'en-US';
  var OBSERVE_DELAY = 120;                 /* 节流：120ms 合批 */
  var ATTRS = ['title', 'placeholder', 'aria-label'];

  /* 这些容器内部一律不翻（日志、终端回显、代码片段…） */
  var BLOCK_TAGS = {
    pre: 1, code: 1, kbd: 1, samp: 1, script: 1, style: 1,
    textarea: 1, noscript: 1, iframe: 1, svg: 1,
  };
  var BLOCK_CLASSES = ['term-view', 'dock-view', 'log-view'];

  /* ====================================================================== *
   * 1. 词典（中文原文 → 英文）
   * ====================================================================== */
  var DICT = {

    /* ── 品牌 / 顶栏 / 导航 ─────────────────────────────────────────── */
    '跳到主要内容': 'Skip to main content',
    '联合控制台 · 无代码版': 'Joint console · No-code edition',
    '控制台版本': 'Console version',
    '实时通道（SSE）连接状态': 'Realtime channel (SSE) status',
    '控制台已运行时长': 'Console uptime',
    '切换浅色/深色（Alt+D）': 'Toggle light/dark (Alt+D)',
    '切换到浅色（Alt+D）': 'Switch to light (Alt+D)',
    '切换到深色（Alt+D）': 'Switch to dark (Alt+D)',
    '打开帮助': 'Open help',
    '帮助': 'Help',
    '打开/关闭浮动终端（Alt+T）': 'Toggle floating terminal (Alt+T)',
    '终端': 'Terminal',
    '重启全部': 'Restart all',
    '停止所有服务并关闭控制台进程': 'Stop all services and quit the console',
    '退出': 'Quit',
    '步骤导航': 'Step navigation',
    '总览': 'Overview',
    '状态灯 · 环境自检': 'Status lights · Environment check',
    '部署': 'Deploy',
    '下载并安装两个项目': 'Download and install both projects',
    '配置': 'Settings',
    'QQ 号 · 大模型 Key': 'QQ number · LLM key',
    '运行': 'Run',
    '启停服务 · 实时日志': 'Start/stop services · Live logs',
    '联调': 'Link test',
    '测试发消息 · 消息流水': 'Test message · Message stream',
    '实时输出 · 进程监视': 'Live output · Process monitor',
    '一键启动全部': 'Start all',
    '停止全部': 'Stop all',
    '三个服务都跑起来后，去 QQ 里': 'Once all three services are running, go to QQ and',
    '@机器人': '@bot',
    '，就能收到大模型的回复。': ', and you will get a reply from the LLM.',

    /* ── 通用状态 / 徽章 ──────────────────────────────────────────── */
    '未知': 'Unknown',
    '正常': 'OK',
    '异常': 'Error',
    '异常退出': 'Crashed',
    '运行中': 'Running',
    '运行中（未就绪）': 'Running (not ready)',
    '未运行': 'Not running',
    '已停止': 'Stopped',
    '启动中': 'Starting',
    '启动中…': 'Starting…',
    '停止中': 'Stopping',
    '停止中…': 'Stopping…',
    '启动失败': 'Start failed',
    '正在启动': 'Starting',
    '已就绪': 'Ready',
    '正在停止': 'Stopping',
    '就绪': 'Ready',
    '未就绪': 'Not ready',
    '空闲': 'Free',
    '被占用': 'In use',
    '本控制台': 'This console',
    '已连接': 'Connected',
    '未连接': 'Disconnected',
    '连接断开': 'Disconnected',
    '正在连接…': 'Connecting…',
    '已断开，重连中…': 'Disconnected, reconnecting…',
    '连接不稳定，正在重连…': 'Unstable connection, reconnecting…',
    '与后端断开': 'Disconnected from the backend',
    '连接异常': 'Connection error',
    '端口监听中': 'Port listening',
    '未启用': 'Disabled',
    '已安装': 'Installed',
    '未安装': 'Not installed',
    '已配置': 'Configured',
    '未配置': 'Not configured',
    '未设置': 'Not set',
    '已保存': 'Saved',
    '未保存': 'Unsaved',
    '是': 'Yes',
    '否': 'No',
    '读取中…': 'Loading…',
    '加载中…': 'Loading…',
    '正在获取状态…': 'Fetching status…',
    '正在检测环境…': 'Checking the environment…',
    '正在读取端口信息…': 'Reading port info…',
    '正在读取进程树……': 'Reading the process tree…',
    '成功': 'Success',
    '失败': 'Failed',
    '请稍候…': 'Please wait…',
    '准备中…': 'Preparing…',
    '进行中…': 'In progress…',
    '完成': 'Done',

    /* ── 通用按钮 ─────────────────────────────────────────────────── */
    '保存设置': 'Save settings',
    '恢复默认': 'Restore defaults',
    '刷新': 'Refresh',
    '刷新状态': 'Refresh status',
    '重新加载': 'Reload',
    '清屏': 'Clear',
    '清空': 'Clear',
    '下载日志': 'Download log',
    '暂停滚动': 'Pause scrolling',
    '继续滚动': 'Resume scrolling',
    '复制': 'Copy',
    '复制路径': 'Copy path',
    '复制安装目录': 'Copy install path',
    '卸载': 'Uninstall',
    '下载安装': 'Download & install',
    '一键安装两个项目': 'Install both projects',
    '克隆并安装依赖': 'Clone & install dependencies',
    '在终端中看进度': 'Watch progress in the terminal',
    '写入联动配置': 'Write link config',
    '发送测试消息': 'Send test message',
    '读取账号信息': 'Read account info',
    '关闭': 'Close',
    '展开': 'Expand',
    '打开': 'Open',
    '选择…': 'Choose…',
    '重新生成': 'Regenerate',
    '导出配置': 'Export config',
    '导入配置': 'Import config',
    '启动': 'Start',
    '停止': 'Stop',
    '重启': 'Restart',
    '去处理': 'Fix it',
    '去部署': 'Go to Deploy',
    '去填写': 'Fill it in',
    '去修改': 'Edit it',
    '去启动': 'Start services',
    '去联调': 'Open Link test',
    '换端口': 'Change port',
    '看日志': 'View logs',

    /* ── 表格 / 字段标签 ──────────────────────────────────────────── */
    '状态': 'Status',
    '用途': 'Purpose',
    '端口': 'Port',
    '占用者': 'Owner',
    '项目': 'Item',
    '路径': 'Path',
    '服务': 'Service',
    '进程': 'Processes',
    '内存': 'Memory',
    '运行时长': 'Uptime',
    '就绪端口': 'Ready port',
    '说明': 'Notes',
    '版本': 'Version',
    '安装类型': 'Install type',
    '版本 / 分支': 'Version / branch',
    '安装位置与运行环境': 'Install location & runtime',
    '安装位置与备份': 'Install locations & backup',
    '数据目录': 'Data directory',
    '运行时目录': 'Runtime directory',
    '发送到': 'Send to',
    '消息内容': 'Message',
    '内容': 'Content',
    '结果': 'Result',
    '错误': 'Error',
    '群号 / QQ 号': 'Group / QQ number',
    'QQ 号': 'QQ number',
    '管理员 QQ': 'Admin QQ',
    '昵称': 'Nickname',
    '头像': 'Avatar',
    '在线': 'Online',
    '登录': 'Login',
    '联动模式': 'Link mode',
    '触发方式': 'Trigger mode',
    '基础与端口': 'Basics & ports',
    '控制台端口': 'Console port',
    'AstrBot 面板端口': 'AstrBot panel port',
    'SnowLuma 面板端口': 'SnowLuma panel port',
    'OneBot 联动端口': 'OneBot link port',
    '控制台监听地址': 'Console bind address',
    '联动通道主机': 'Link channel host',
    'AstrBot OneBot 监听地址': 'AstrBot OneBot bind address',
    'SnowLuma 安装目录': 'SnowLuma install directory',
    'AstrBot 安装目录': 'AstrBot install directory',
    'SnowLuma 中继端口': 'SnowLuma relay port',
    '联动 Token（两侧必须一致）': 'Link token (must match on both sides)',
    '自定义前缀': 'Custom prefixes',
    '接口地址 Base URL': 'Base URL',
    '模型名': 'Model name',
    '机器人人设': 'Bot persona',
    'QQ 账号': 'QQ account',
    'AstrBot 面板用户名': 'AstrBot panel username',
    'AstrBot 面板密码': 'AstrBot panel password',
    '大模型': 'LLM',
    '高级：监听地址': 'Advanced: bind addresses',
    '重新生成 Token': 'Regenerate token',

    /* ── 复选框 / 说明文字 ───────────────────────────────────────── */
    '启动控制台后自动打开浏览器': 'Open the browser when the console starts',
    '随控制台自动启动 SnowLuma': 'Start SnowLuma with the console',
    '随控制台自动启动 AstrBot': 'Start AstrBot with the console',
    '允许私聊对话': 'Allow private chats',
    '允许群聊对话': 'Allow group chats',
    '忽略自己发的消息': 'Ignore the bot\u2019s own messages',
    '图片/语音等非文本也转给大模型': 'Forward images / voice to the LLM as well',
    '自动滚动': 'Auto-scroll',
    '全部服务': 'All services',
    '全部级别': 'All levels',
    '全部来源': 'All sources',
    '中继': 'Relay',
    '控制台': 'Console',

    /* ── 部署页 ──────────────────────────────────────────────────── */
    '第一次使用请先安装两个项目。安装包从官方仓库下载，全程可看日志；中途不要关闭本页面。':
      'On first use, install the two projects. Packages come from the official repository and the whole process is logged; do not close this page meanwhile.',
    '说明：所有下载/安装都在后台静默执行，':
      'Note: all downloads and installs run silently in the background \u2014',
    '；进度与输出都显示在下方日志里。': ' progress and output are shown in the log below.',
    '实时看 git/uv/pip 的输出': 'Live git/uv/pip output',
    'QQ 协议端': 'QQ protocol endpoint',
    '两个项目': 'both projects',
    '负责把 QQ 账号登录起来，并把消息转成标准的 OneBot v11 事件。':
      'Logs the QQ account in and turns messages into standard OneBot v11 events.',
    'latest（最新稳定版）': 'latest (newest stable)',
    'full —— 自带 Node，推荐': 'full — bundles Node, recommended',
    'lite —— 需系统 Node ≥ 24.22.1': 'lite — needs system Node ≥ 24.22.1',
    'full 版自带 node.exe，不用自己装 Node，小白首选；lite 版体积小，但要求本机已有 Node。':
      'The full edition bundles node.exe, so you do not install Node yourself — best for beginners. The lite edition is smaller but needs Node on this machine.',
    'lite 版体积更小，但要求本机已安装 Node.js ≥ 24.22.1，否则启动会失败。':
      'The lite edition is smaller but needs Node.js ≥ 24.22.1 installed, otherwise it will fail to start.',
    '大模型机器人框架': 'LLM bot framework',
    '负责接住 QQ 消息、调用大模型生成回复（DeepSeek / OpenAI 兼容接口均可）。':
      'Receives QQ messages and asks the LLM for a reply (DeepSeek / OpenAI-compatible endpoints both work).',
    'main（稳定主分支）': 'main (stable branch)',
    '安装过程会': 'The install runs',
    '源码并创建独立的 Python 虚拟环境、安装依赖，通常需要 3–10 分钟。':
      ' on the sources, then creates a dedicated Python virtualenv and installs dependencies — usually 3–10 minutes.',
    '路径可用环境变量 SLAB_RUNTIME 覆盖': 'Paths can be overridden with the SLAB_RUNTIME environment variable',
    '例如 123456789': 'e.g. 123456789',

    /* ── 配置页 ──────────────────────────────────────────────────── */
    '只有三个必填项：': 'Only three fields are required:',
    '。其余保持默认即可。': '. Leave the rest at their defaults.',
    '、': ',',
    '有未保存的修改': 'Unsaved changes',
    '只在本机使用，一般不用改': 'Local use only — usually no need to change',
    '想装到 D 盘就改这里；留空用默认位置': 'Change these to install on another drive; blank means the default',
    '留空 = 程序目录\\runtime\\snowluma': 'Blank = <program dir>\\runtime\\snowluma',
    '留空 = 程序目录\\runtime\\astrbot': 'Blank = <program dir>\\runtime\\astrbot',
    '打开系统目录选择框': 'Open the system folder picker',
    '在资源管理器里打开': 'Open in File Explorer',
    '数据目录跟随程序位置': 'The data directory follows the program location',
    '（配置、日志、下载缓存）': ' (config, logs, download cache)',
    '数据目录固定在程序旁边（桌面版在':
      'The data directory lives next to the program (on desktop builds it is in',
    '）：想换盘就把整个程序文件夹搬过去。': '): to use another drive, move the whole program folder.',
    '导出成 JSON 文件': 'Export as a JSON file',
    '从 JSON 文件恢复': 'Restore from a JSON file',
    '导出的文件里含 API Key 与联动 Token，别乱发；导入会整份覆盖当前配置。':
      'The exported file contains the API key and link token — do not share it. Importing overwrites the whole config.',
    '强烈建议使用小号，协议端有封号风险': 'A spare account is strongly recommended — the protocol side risks bans',
    '留空 = 自动使用上面的 QQ 号': 'Blank = reuse the QQ number above',
    '填了你自己的 QQ 号即可使用': 'Use your own QQ number here to enable',
    '等管理指令；多个号用逗号或空格分隔。': ' and other admin commands; separate multiple numbers with commas or spaces.',
    '已保存，留空表示不修改': 'Saved — leave blank to keep',
    '已配置（回显为 ***）': 'Configured (shown as ***)',
    '填 DeepSeek / OpenAI / 任何 OpenAI 兼容接口': 'DeepSeek / OpenAI / any OpenAI-compatible endpoint',
    '形如': 'Form:',
    '；填写后点右上角「保存设置」。留空不会清空已保存的值。':
      '; after filling it in, click "Save settings" at the top right. Leaving it blank never clears the saved value.',
    '你是一个友好、简洁的中文聊天助手。': 'You are a friendly, concise Chinese chat assistant.',
    '二选一，不确定就用「原生直连」': 'Pick one — if unsure, use "Native direct"',
    '原生直连（推荐）': 'Native direct (recommended)',
    'SnowLuma 直接连 AstrBot 的 OneBot 接口，少一个中间进程，最省资源、延迟最低。':
      'SnowLuma connects straight to AstrBot\u2019s OneBot endpoint: one less process, least overhead, lowest latency.',
    '适合：只接一个 QQ 号、只挂一个机器人。': 'Best for: one QQ account, one bot.',
    '内置中继': 'Built-in relay',
    '控制台自己当一个「中间人」：SnowLuma → 中继 → AstrBot，能看到每一句收发消息，方便排查。':
      'The console acts as the middleman: SnowLuma → relay → AstrBot, so you can see every message in and out — great for debugging.',
    '适合：消息不回复、要抓包看数据流、以后接多个机器人。':
      'Best for: silent bots, inspecting the data flow, or multiple bots later.',
    '相当于 OneBot 的口令，防止别人连到你的机器人。改完记得去「联调」页点「写入联动配置」。':
      'Works like an OneBot password that stops others from connecting to your bot. After changing it, write the link config on the Link test page.',
    '仅「内置中继」模式使用：SnowLuma 会在此端口开一个 OneBot 服务端给中继连接，需与其它端口不同。':
      'Only used in "Built-in relay" mode: SnowLuma opens an OneBot server on this port for the relay, so it must differ from the other ports.',
    '触发规则': 'Trigger rules',
    '决定机器人什么时候开口': 'Decides when the bot speaks up',
    '群里 @机器人 或以 / 开头': 'In groups: @bot, or a leading /',
    '只在被叫到时回复，最不打扰。群聊里 @机器人、或发「/你好」；私聊默认都会回复。':
      'Replies only when called, which is least intrusive. In groups @bot or send "/hello"; private chats always reply by default.',
    '所有消息都回复': 'Reply to every message',
    '群里每句话都接，容易刷屏、也更容易被风控，仅在测试时使用。':
      'Answers every group message — noisy and more likely to trip risk control; use it for testing only.',
    '多个前缀用逗号或空格分隔，例如': 'Separate prefixes with commas or spaces, e.g.',
    '改动会立即写入': 'Changes are written immediately to',
    '，不需要重启控制台。': ' — no console restart needed.',

    /* ── 总览页 ──────────────────────────────────────────────────── */
    '一眼看清 SnowLuma、AstrBot、中继与 OneBot 连接的状态；有红色条目就看下面的「下一步建议」。':
      'See the state of SnowLuma, AstrBot, the relay and the OneBot connection at a glance; if something is red, read "Next steps" below.',
    '端口占用': 'Port usage',
    '四个端口必须互不相同且未被其它程序占用': 'All four ports must be distinct and free of other programs',
    '环境自检': 'Environment check',
    '部署前先过一遍': 'Run through this before deploying',
    '下一步建议': 'Next steps',
    '按顺序做即可，不需要懂代码': 'Just follow the order — no coding knowledge needed',
    '大模型机器人框架：收到消息后调用大模型生成回复。': 'LLM bot framework: calls the LLM to answer each message.',
    'QQ 协议端：登录 QQ、收发消息，并转成 OneBot v11 事件。':
      'QQ protocol endpoint: logs in, sends/receives messages and emits OneBot v11 events.',
    '中继服务': 'Relay service',
    '控制台内置的 OneBot 中继，仅在「内置中继」联动模式下需要。':
      'The OneBot relay built into the console — only needed in "Built-in relay" mode.',
    'SnowLuma 协议端': 'SnowLuma endpoint',
    'AstrBot 框架': 'AstrBot framework',
    'OneBot 连接': 'OneBot connection',
    '内置中继模式': 'Built-in relay mode',
    '原生直连模式': 'Native direct mode',
    '消息会经过控制台转发，可在「联调」页看到流水':
      'Messages are forwarded by the console — watch the stream on the Link test page',
    '无需中继进程，SnowLuma 直连 AstrBot': 'No relay process; SnowLuma talks to AstrBot directly',
    '多开 QQ 时，每个已登录账号各有一条连接；任意一条正常即算连通':
      'With several QQ accounts, each logged-in account has its own connection; one healthy link is enough',
    '已建立连接但尚未握手成功，稍等或看「运行」页日志':
      'Connected but not yet handshaked — wait a moment, or check the logs on the Run page',
    '端口已被占用，但尚未确认握手成功': 'The port is taken but the handshake is not confirmed yet',
    '两端服务都启动后才会建立连接；多开 QQ 时每个账号各一条':
      'The link appears once both services are up; one per QQ account when running several',
    '在「运行」页启动；首次登录需扫码': 'Start it on the Run page; the first login needs a QR scan',
    '首次登录需扫码': 'the first login needs a QR scan',
    '在「运行」页启动': 'Start it on the Run page',
    '安装 SnowLuma': 'Install SnowLuma',
    '安装 AstrBot': 'Install AstrBot',
    '填写 QQ 号': 'Fill in the QQ number',
    '填写大模型 API Key': 'Fill in the LLM API key',
    '配置待修正': 'Config needs fixing',
    '端口被占用': 'Port in use',
    '启动服务': 'Start the services',
    '：去「部署」页点「下载安装」，建议选 full 版（自带 Node）。':
      ': on the Deploy page click "Download & install" — the full edition (bundled Node) is recommended.',
    '：点「克隆并安装依赖」，需要本机有 Git。':
      ': click "Clone & install dependencies"; Git must be available on this machine.',
    '：建议用一个小号，登录协议端有封号风险。':
      ': a spare account is recommended — logging in on the protocol side risks a ban.',
    '：没有 Key 机器人不会回复。可选 DeepSeek / OpenAI 兼容接口。':
      ': without a key the bot never replies. DeepSeek / OpenAI-compatible endpoints both work.',
    '：两个项目都已安装，去「运行」页点「一键启动全部」。':
      ': both projects are installed — click "Start all" on the Run page.',
    '链路看起来已经打通：去 QQ 里': 'The chain looks healthy: go to QQ and',
    '或发一条': ' or send',
    '，然后到「联调」页看消息流水。': ', then watch the message stream on the Link test page.',
    '一切正常。想改端口 / 换模型就在「配置」页调整，需要看细节就去「运行」页的实时日志。':
      'Everything looks fine. To change ports or models, use the Settings page; for details, check the live log on the Run page.',
    'Python（控制台自身）': 'Python (the console itself)',
    'Git（克隆 AstrBot 需要）': 'Git (needed to clone AstrBot)',
    'uv（更快的 Python 包管理器，可选）': 'uv (faster Python package manager, optional)',
    'Node.js（SnowLuma lite 版需要）': 'Node.js (needed by SnowLuma lite)',
    'pnpm（可选）': 'pnpm (optional)',
    '磁盘剩余空间（建议 ≥ 3 GB）': 'Free disk space (≥ 3 GB recommended)',
    '安装两个项目约占用 1–3 GB': 'Both projects take about 1–3 GB',
    'SnowLuma 已安装': 'SnowLuma installed',
    'AstrBot 已安装': 'AstrBot installed',
    '未检测到': 'Not detected',

    /* ── 运行页 ──────────────────────────────────────────────────── */
    '在这里启停服务、打开两个网页面板，并实时查看日志。启动顺序建议：AstrBot → SnowLuma → 中继。':
      'Start/stop services, open the two web panels and watch the logs live. Suggested order: AstrBot → SnowLuma → relay.',
    '实时日志': 'Live log',
    '选择服务': 'Choose a service',
    '级别过滤': 'Filter by level',
    '服务日志输出': 'Service log output',
    '暂无日志。启动服务后，这里会实时滚动输出。':
      'No log yet. Once a service starts, its output scrolls here in real time.',
    '服务尚未注册': 'No services registered yet',
    '控制台后端还没有注册任何服务，请先完成部署（或稍等后端初始化）。':
      'The backend has not registered any service yet — finish the deployment first (or wait for the backend to initialise).',
    '打开 AstrBot 面板': 'Open the AstrBot panel',
    '打开 SnowLuma 面板': 'Open the SnowLuma panel',

    /* ── 联调页 ──────────────────────────────────────────────────── */
    '把配置写进两个项目，再发一条测试消息验证链路：QQ ⇄ SnowLuma ⇄ OneBot ⇄ AstrBot ⇄ 大模型。':
      'Write the config into both projects, then send a test message to verify the chain: QQ ⇄ SnowLuma ⇄ OneBot ⇄ AstrBot ⇄ LLM.',
    '发一条测试消息': 'Send a test message',
    '让机器人主动发一条，最快验证链路': 'Let the bot send one — the fastest way to verify the chain',
    '群聊': 'Group',
    '私聊': 'Private',
    '你好，这是一条测试消息': 'Hi, this is a test message',
    '提示：私聊只能发给你自己或机器人好友；群聊需要机器人已经在群里。':
      'Tip: private messages can only go to yourself or a bot friend; for group messages the bot must already be in the group.',
    '我的 QQ 账号信息': 'My QQ account',
    '从 OneBot 读取当前登录账号': 'Read the logged-in account from OneBot',
    '尚未读取': 'Not loaded yet',
    '最近一次操作结果': 'Last operation result',
    '「写入联动配置」「测试消息」的后端返回详情':
      'Backend details for "write link config" / "test message"',
    '还没有操作记录。': 'No operations yet.',
    '消息流水': 'Message stream',
    '还没有消息。去 QQ 里给机器人发一句话，这里会实时出现「收到 / 回复」。':
      'No messages yet. Say something to the bot in QQ and "recv / reply" lines show up here live.',
    '收到': 'recv',
    '回复': 'reply',
    '（空）': '(empty)',
    '无数据': 'No data',
    '（无详细信息）': '(no details)',
    '尚无数据，点右上角「刷新」或先写入联动配置':
      'No data yet — click "Refresh" at the top right, or write the link config first',
    '最近一次「写入联动配置」结果': 'Last "write link config" result',
    '写入联动配置失败': 'Failed to write the link config',
    '最近一次测试消息': 'Last test message',
    '测试消息发送失败': 'Failed to send the test message',
    '接口返回为空': 'The API returned nothing',
    '读取失败': 'Read failed',

    /* ── 终端页 ──────────────────────────────────────────────────── */
    '实时输出': 'Live output',
    '按来源过滤': 'Filter by source',
    '等待输出……': 'Waiting for output…',
    '等待输出……（去「部署」页装东西，或到「运行」页启动服务）':
      'Waiting for output… (install something on the Deploy page, or start a service on the Run page)',
    '暂无输出（按当前过滤条件）': 'No output (with the current filter)',
    '已清屏，后续输出会继续出现。': 'Cleared — new output will keep appearing.',
    '查看完整命令行': 'Show the full command line',
    '没有子进程（服务都还没启动）': 'No child processes (no service started yet)',
    '读取进程失败': 'Failed to read the processes',
    '浮动终端': 'Floating terminal',
    '在「终端」页查看（含进程监视）': 'Open the Terminal page (with process monitor)',
    '关闭（Alt+T）': 'Close (Alt+T)',
    '正在观看每一个进程的输出：部署时的': 'Watch the output of every process: deploy-time',
    '实时观看每一个进程的输出：部署时的': 'Watch the output of every process live: deploy-time',

    /* ── 帮助弹窗 ────────────────────────────────────────────────── */
    '帮助 · 五分钟上手': 'Help · Get started in five minutes',
    '常见问题速查': 'Quick FAQ',
    '：在「部署」页点': ': on the Deploy page click',
    '（SnowLuma，建议 full 版）和': ' (SnowLuma — the full edition is recommended) and',
    '（AstrBot）。': ' (AstrBot).',
    '：填 QQ 号（建议小号）、大模型 API Key 与模型名，点「保存设置」。':
      ': fill in the QQ number (a spare account), the LLM API key and the model name, then click "Save settings".',
    '：「联调」页点「写入联动配置」，把端口与 Token 同步进两个项目。':
      ': on the Link test page click "Write link config" to push the ports and token into both projects.',
    '：「运行」页依序启动 AstrBot → SnowLuma → 中继，等状态灯变绿。':
      ': on the Run page start AstrBot → SnowLuma → relay in that order and wait for the lights to turn green.',
    '：在 QQ 里 @机器人 或发': ': in QQ, @bot or send',
    '，收到回复即打通。': '; once you get a reply the chain works.',
    '面板打不开': 'Panel will not open',
    'QQ 未登录': 'QQ not logged in',
    '消息不回复': 'No replies',
    '多开 QQ': 'Several QQ accounts',
    '杀软拦截 node.exe': 'Antivirus blocks node.exe',
    '：先确认「运行」页对应服务是绿灯，再看端口是否被占用。':
      ': first check that the matching service is green on the Run page, then whether the port is taken.',
    '：SnowLuma 面板需要扫码登录，扫码后重启一次中继。':
      ': the SnowLuma panel needs a QR login; restart the relay once after scanning.',
    '：检查 API Key、模型名，以及触发规则是否匹配（群里需要 @ 或前缀）。':
      ': check the API key, the model name and whether the trigger rule matches (groups need @ or a prefix).',
    '：SnowLuma 会给每个已登录账号各建一条 AstrBot 连接，只要有一条正常就算连通。':
      ': SnowLuma opens one AstrBot connection per logged-in account; one healthy link is enough.',
    '：在「配置」页换一个端口，然后重新「写入联动配置」。':
      ': pick another port on the Settings page, then write the link config again.',
    '：把安装目录加入白名单后重试部署。': ': add the install directory to the allowlist and deploy again.',
    '完整文档在项目目录：': 'Full docs live in the project folder:',
    '。接口调试可访问': '. The API playground is at',
    '。': '.',

    /* ── Toast / 提示（app.js） ───────────────────────────────────── */
    '连不上控制台后端，请确认黑色命令行窗口没有关闭（可重新双击「一键启动.bat」）':
      'Cannot reach the backend — make sure the black command window is still open (you can re-run "一键启动.bat").',
    '该功能的后端模块还没就绪': 'The backend module for this feature is not ready yet',
    '接口不存在': 'Endpoint not found',
    '操作失败': 'Operation failed',
    '保存配置失败': 'Failed to save the config',
    '保存设置失败': 'Failed to save the settings',
    '没有可复制的内容': 'Nothing to copy',
    '已复制到剪贴板': 'Copied to the clipboard',
    '复制失败，请手动选择文本复制': 'Copy failed — select the text and copy it manually',
    '状态已刷新': 'Status refreshed',
    '读取状态失败': 'Failed to read the status',
    '已卸载': 'Uninstalled',
    '安装完成': 'Install finished',
    '安装流程结束': 'Install flow finished',
    '正在连接部署模块…': 'Connecting to the deploy module…',
    '正在等待系统目录选择框…': 'Waiting for the system folder picker…',
    '如果没看到对话框，请看一下任务栏': 'If no dialog appears, check the taskbar',
    '已取消选择': 'Selection cancelled',
    '打开目录选择框失败': 'Failed to open the folder picker',
    '打开目录失败': 'Failed to open the directory',
    '配置已导出': 'Config exported',
    '文件里含密钥，注意保管': 'The file contains secrets — keep it safe',
    '导出配置失败': 'Failed to export the config',
    '文件不是合法的 JSON': 'The file is not valid JSON',
    '配置已导入': 'Config imported',
    '导入完成。如果改了端口或安装目录，建议重启一次控制台让服务按新配置启动。':
      'Import done. If you changed ports or install directories, restart the console once so the services pick up the new config.',
    '导入配置失败': 'Failed to import the config',
    '没有需要保存的修改': 'Nothing to save',
    '已保存，但还有需要处理的地方': 'Saved, but a few things still need attention',
    '设置已保存': 'Settings saved',
    '已恢复默认配置（敏感字段会重新生成）': 'Default config restored (secrets are regenerated)',
    '恢复默认失败': 'Failed to restore the defaults',
    '已生成新的联动 Token，记得去「联调」页重新写入联动配置':
      'New link token generated — remember to write the link config again on the Link test page',
    'Token 已复制': 'Token copied',
    '出于安全考虑，已保存的 Token 不会回显': 'For safety, a saved token is never shown again',
    '点「重新生成」会得到新 Token 并显示出来': 'Click "Regenerate" to get a new token and see it',
    '还没有安装目录信息，请先刷新状态': 'No install directory yet — refresh the status first',
    '路径已复制，可粘贴到资源管理器地址栏': 'Path copied — paste it into the Explorer address bar',
    '保存后生效；重新部署会装到新位置': 'Takes effect after saving; a re-deploy installs to the new place',
    '日志窗口已清空（服务端记录不受影响）': 'Log view cleared (server-side records are untouched)',
    '读取日志失败': 'Failed to read the log',
    '联动配置已写入两个项目': 'Link config written into both projects',
    '如服务正在运行，建议到「运行」页重启一次': 'If services are running, restart them once on the Run page',
    '请填写正确的群号或 QQ 号（5–12 位数字）': 'Enter a valid group or QQ number (5–12 digits)',
    '请先填写要发送的内容': 'Enter the message to send first',
    '测试消息已提交，去 QQ 里看看有没有收到': 'Test message submitted — check QQ for it',
    '已读取 QQ 账号信息': 'QQ account info loaded',
    '读取 QQ 账号信息失败': 'Failed to read the QQ account info',
    '读取联动状态失败': 'Failed to read the link status',
    '联动状态已刷新': 'Link status refreshed',
    '请看「运行」页日志': 'See the logs on the Run page',
    '请到「运行」页查看日志': 'Check the logs on the Run page',
    '还没有已注册的服务，请先完成部署': 'No services registered yet — finish the deployment first',
    '第一次使用？先去「部署」页安装两个项目': 'First time here? Install both projects on the Deploy page',
    'SnowLuma 安装类型已保存': 'SnowLuma install type saved',
    'SnowLuma 版本已保存': 'SnowLuma version saved',
    'AstrBot 版本已保存': 'AstrBot version saved',
    '控制台已退出，所有服务已停止，可以关闭此页面了。':
      'The console has quit and all services are stopped — you can close this page.',
    '将停止 SnowLuma / AstrBot 并关闭控制台，确定吗？':
      'This stops SnowLuma / AstrBot and quits the console. Continue?',
    '确定要把所有配置恢复成默认值吗？已填写的 QQ 号与大模型 Key 会被清空。':
      'Restore every setting to its default? The QQ number and LLM key you entered will be cleared.',
    '导入会整份覆盖当前配置（含端口、QQ 号、API Key、安装目录），确定吗？':
      'Importing overwrites the whole config (ports, QQ number, API key, install directories). Continue?',
    '确定卸载 SnowLuma 吗？安装目录会被删除（data 目录里的配置不受影响）。':
      'Uninstall SnowLuma? Its install directory is deleted (configs under data are untouched).',
    '确定卸载 AstrBot 吗？安装目录会被删除（data 目录里的配置不受影响）。':
      'Uninstall AstrBot? Its install directory is deleted (configs under data are untouched).',
    '开始安装 AstrBot…': 'Installing AstrBot…',

    /* ── 进度短语（配合 “xx%” 模式） ──────────────────────────────── */
    '正在安装…': 'Installing…',
    '正在下载…': 'Downloading…',
    '正在解压…': 'Extracting…',
    '正在克隆…': 'Cloning…',
    '正在校验…': 'Verifying…',
    '正在清理…': 'Cleaning up…',
    '正在配置…': 'Configuring…',
    '正在部署…': 'Deploying…',

    /* ── 补充零散字面量（HTML <b>/<title>、app.js 常量、term.js 文案） ── */
    'SnowLuma × AstrBot 联合控制台': 'SnowLuma × AstrBot Joint Console',
    '不会弹出任何命令行黑框': 'never opens a command-line window',
    '大模型 API Key': 'LLM API key',
    '进度、SnowLuma 与 AstrBot 的运行日志，都会像终端一样滚出来。':
      ' progress, plus the SnowLuma and AstrBot runtime logs, scroll out like a real terminal.',
    '下方是控制台进程树（含 PID / CPU / 内存），进程数随部署/服务启停实时变化。':
      'Below is the console process tree (PID / CPU / memory); the process count follows deploys and service start/stop.',
    '写入联动': 'Write link config',
    '聊天': 'Chat',
    'AstrBot 面板': 'AstrBot panel',
    'SnowLuma 面板': 'SnowLuma panel',
    'OneBot 联动': 'OneBot link',
    '选择目录': 'Choose directory',
    '（暂无输出）': '(no output)',
    '发送测试消息失败': 'Failed to send the test message',
  };

  /* ====================================================================== *
   * 2. 模式规则（动态文本；命中后同样可还原，见 §4）
   *    每一项：[正则, 函数(m) -> 英文 | null]；返回 null 表示不处理
   * ====================================================================== */
  function uptimeEn(raw) {
    var t = String(raw || '').trim();
    var m = /^(\d+)\s*时\s*(\d+)\s*分$/.exec(t);
    if (m) return m[1] + 'h ' + m[2] + 'm';
    m = /^(\d+)\s*(?:小时)\s*(\d+)\s*分$/.exec(t);
    if (m) return m[1] + 'h ' + m[2] + 'm';
    m = /^(\d+)\s*分\s*(\d+)\s*秒$/.exec(t);
    if (m) return m[1] + 'm ' + m[2] + 's';
    m = /^(\d+)\s*分$/.exec(t);
    if (m) return m[1] + 'm';
    m = /^(\d+)\s*秒$/.exec(t);
    if (m) return m[1] + 's';
    m = /^(\d+)\s*(?:小时|时)$/.exec(t);
    if (m) return m[1] + 'h';
    return null;
  }

  function plural(n, one, many) {
    return String(n) + ' ' + (String(n) === '1' ? one : many);
  }

  /** 全角括号 → 半角（英文排版里更自然） */
  function halfParens(text) {
    return String(text).replace(/（/g, '(').replace(/）/g, ')');
  }

  /* 动词：启动 / 停止 / 重启 */
  function verbPast(zh) {
    return zh === '启动' ? 'started' : zh === '停止' ? 'stopped' : 'restarted';
  }
  function verbNoun(zh) {
    return zh === '启动' ? 'start' : zh === '停止' ? 'stop' : 'restart';
  }

  var RULES = [

    /* —— 运行时长 —— */
    [/^运行\s*(?:—|-{1,2})$/, function () { return 'Uptime —'; }],
    [/^控制台已运行\s*(.+)$/, function (m) {
      var t = uptimeEn(m[1]);
      return t ? 'Console uptime ' + t : null;
    }],
    [/^运行\s*(\d+)\s*时\s*(\d+)\s*分$/, function (m) { return 'Uptime ' + m[1] + 'h ' + m[2] + 'm'; }],
    [/^运行\s*(\d+)\s*分\s*(\d+)\s*秒$/, function (m) { return 'Uptime ' + m[1] + 'm ' + m[2] + 's'; }],
    [/^运行\s*(\d+)\s*秒$/, function (m) { return 'Uptime ' + m[1] + 's'; }],
    [/^运行\s*(\d+)\s*(?:小时)\s*(\d+)\s*分$/, function (m) { return 'Uptime ' + m[1] + 'h ' + m[2] + 'm'; }],

    /* —— 裸运行时长（服务卡片 / 进程表里只有 “12 分 3 秒”） —— */
    [/^(\d+)\s*时\s*(\d+)\s*分$/, function (m) { return m[1] + 'h ' + m[2] + 'm'; }],
    [/^(\d+)\s*分\s*(\d+)\s*秒$/, function (m) { return m[1] + 'm ' + m[2] + 's'; }],
    [/^(\d+)\s*秒$/, function (m) { return m[1] + 's'; }],
    [/^(\d+)\s*分$/, function (m) { return m[1] + 'm'; }],

    /* —— 下载 / 安装进度 —— */
    [/^已下载\s*(.+?)\s*[（(]\s*(\d+(?:\.\d+)?)\s*%\s*[)）]$/, function (m) {
      return 'Downloaded ' + m[1] + ' (' + m[2] + '%)';
    }],
    [/^已上传\s*(.+?)\s*[（(]\s*(\d+(?:\.\d+)?)\s*%\s*[)）]$/, function (m) {
      return 'Uploaded ' + m[1] + ' (' + m[2] + '%)';
    }],
    [/^已下载\s*(.+?)\s*\/\s*(.+?)$/, function (m) {
      return 'Downloaded ' + m[1] + ' / ' + m[2];
    }],

    /* —— “正在安装… 45%” 这类：前缀查词典，数字原样保留 —— */
    [/^(.+?)\s*(\d{1,3})\s*%$/, function (m) {
      var head = String(m[1]).replace(/\s+$/, '');
      if (!/[\u4e00-\u9fa5]/.test(head)) return null;
      var key = head.replace(/[.．]{2,}$/, '…');
      var en = DICT[key];
      if (!en && key.slice(-1) === '…') en = DICT[key.slice(0, -1)];
      if (!en) en = DICT[head];
      if (!en) return null;
      return en + ' ' + m[2] + '%';
    }],

    /* —— 计数 —— */
    [/^(\d+)\s*条$/, function (m) { return plural(m[1], 'message', 'messages'); }],
    [/^(\d+)\s*行$/, function (m) { return plural(m[1], 'line', 'lines'); }],
    [/^(\d+)\s*个进程\s*[·・]\s*共\s*([\d.]+)\s*MB$/, function (m) {
      return plural(m[1], 'process', 'processes') + ' · ' + m[2] + ' MB total';
    }],
    [/^(\d+)\s*个进程$/, function (m) { return plural(m[1], 'process', 'processes'); }],
    [/^(\d+)\s*\/\s*(\d+)\s*条正常$/, function (m) { return m[1] + '/' + m[2] + ' online'; }],

    /* —— 端口 / 状态短语 —— */
    [/^端口\s*(.+)$/, function (m) { return 'Port ' + m[1]; }],
    [/^就绪端口\s*(\d+)\s*[·・]\s*(.+)$/, function (m) {
      var tail = translateCore(m[2]);
      return 'Ready port ' + m[1] + ' · ' + tail;
    }],
    [/^面板端口\s*(\d+)$/, function (m) { return 'Panel port ' + m[1]; }],

    /* —— 提示类动态句子 —— */
    [/^请求失败（HTTP\s*(\d+)）$/, function (m) { return 'Request failed (HTTP ' + m[1] + ')'; }],
    [/^HTTP\s*(\d+)$/, function (m) { return 'HTTP ' + m[1]; }],
    [/^开始安装\s*(.+?)…$/, function (m) { return 'Installing ' + m[1] + '…'; }],
    [/^安装失败：(.+)$/, function (m) { return 'Install failed: ' + m[1]; }],
    [/^部分服务启动失败：(.+)$/, function (m) { return 'Some services failed to start: ' + m[1]; }],
    [/^(启动|停止|重启)全部服务：成功$/, function (m) { return 'All services ' + verbPast(m[1]) + ': OK'; }],
    [/^(启动|停止|重启)全部服务成功$/, function (m) { return 'All services ' + verbPast(m[1]); }],
    [/^(启动|停止|重启)完成，但\s*(.+?)\s*失败$/, function (m) {
      return verbPast(m[1]) + ' most services, but ' + m[2] + ' failed';
    }],
    [/^(启动|停止|重启)全部服务失败$/, function (m) {
      return 'Failed to ' + verbNoun(m[1]) + ' all services';
    }],
    [/^(.+?)\s*(启动|停止|重启)指令已下发$/, function (m) {
      return m[1] + ': ' + verbNoun(m[2]) + ' command sent';
    }],
    [/^(.+?)\s*(启动|停止|重启)失败$/, function (m) {
      return 'Failed to ' + verbNoun(m[2]) + ' ' + m[1];
    }],
    [/^(.+?)\s*安装完成（耗时\s*(\d+)\s*秒）$/, function (m) {
      return m[1] + ' installed (' + m[2] + 's)';
    }],
    [/^(.+?)\s*已卸载$/, function (m) { return m[1] + ' uninstalled'; }],
    [/^(.+?)\s*安装目录已复制，可粘贴到资源管理器地址栏$/, function (m) {
      return m[1] + ' install path copied — paste it into the Explorer address bar';
    }],
    [/^：(.+?)\s*已被别的程序占用，换一个端口后重新「写入联动配置」。$/, function (m) {
      return ': ' + halfParens(m[1]) + ' is taken by another program — pick another port and write the link config again.';
    }],
    [/^等待 SnowLuma 就绪：首次启动要扫码登录 QQ（面板端口\s*(.+?)）。$/, function (m) {
      return 'Waiting for SnowLuma to be ready: the first start needs a QR login (panel port ' + m[1] + ').';
    }],
    [/^（(.+)）$/, function (m) {
      var inner = translateCore(m[1]);
      return inner === m[1] ? null : '(' + inner + ')';
    }],
  ];

  /* ====================================================================== *
   * 3. 翻译核心
   * ====================================================================== */
  function has(obj, key) {
    return Object.prototype.hasOwnProperty.call(obj, key);
  }

  function closestTag(el, tag) {
    var cur = el;
    while (cur && cur.nodeType === 1) {
      if (cur.tagName && cur.tagName.toLowerCase() === tag) return true;
      cur = cur.parentElement;
    }
    return false;
  }

  /** 查词典 + 模式；返回 null 表示“没有对应英文” */
  function lookup(text, el) {
    /* 上下文歧义：同一个中文词在不同位置意思不同 */
    if (el) {
      if (text === '运行' && closestTag(el, 'th')) return 'Uptime';
      if (text === '进程' && closestTag(el, 'th')) return 'Process';
    }
    if (has(DICT, text)) return DICT[text];
    for (var i = 0; i < RULES.length; i += 1) {
      var m = RULES[i][0].exec(text);
      if (!m) continue;
      var out = RULES[i][1](m);
      if (out !== null && out !== undefined && out !== '') return out;
    }
    return null;
  }

  /** 只翻译“整串等于某个词条”的字符串；不做首尾空白处理（外部负责） */
  function translateCore(text, el) {
    var out = lookup(text, el || null);
    return out === null || out === undefined ? text : out;
  }

  /** 保留首尾空白地翻译一段文本 */
  function translate(raw, el) {
    if (raw === null || raw === undefined) return raw;
    var text = String(raw);
    var trimmed = text.trim();
    if (!trimmed) return text;
    var out = lookup(trimmed, el || null);
    if (out === null || out === undefined || out === trimmed) return text;
    var lead = /^\s*/.exec(text)[0];
    var tail = /\s*$/.exec(text)[0];
    return lead + out + tail;
  }

  /* ====================================================================== *
   * 4. 原文记录（幂等 / 可还原）
   * ====================================================================== */
  var textRec = typeof WeakMap === 'function' ? new WeakMap() : null;
  var attrRec = typeof WeakMap === 'function' ? new WeakMap() : null;

  function getTextRec(node) {
    if (textRec) return textRec.get(node) || null;
    return node.__slabI18nSrc || null;
  }
  function setTextRec(node, rec) {
    if (textRec) textRec.set(node, rec);
    else node.__slabI18nSrc = rec;
  }
  function delTextRec(node) {
    if (textRec) { textRec.delete(node); return; }
    try { delete node.__slabI18nSrc; } catch (e) { node.__slabI18nSrc = null; }
  }

  function getAttrRec(el) {
    if (attrRec) return attrRec.get(el) || null;
    return el.__slabI18nAttr || null;
  }
  function setAttrRec(el, map) {
    if (attrRec) attrRec.set(el, map);
    else el.__slabI18nAttr = map;
  }

  /* ====================================================================== *
   * 5. 屏蔽区域
   * ====================================================================== */
  function blocked(el) {
    var cur = el;
    while (cur && cur.nodeType === 1) {
      var tag = cur.tagName;
      if (tag && BLOCK_TAGS[String(tag).toLowerCase()]) return true;
      if (cur.hasAttribute && cur.hasAttribute('data-no-i18n')) return true;
      var cls = cur.classList;
      if (cls && cls.length) {
        for (var i = 0; i < BLOCK_CLASSES.length; i += 1) {
          if (cls.contains(BLOCK_CLASSES[i])) return true;
        }
      }
      cur = cur.parentElement;
    }
    return false;
  }

  /* ====================================================================== *
   * 6. 应用到节点
   * ====================================================================== */
  function applyTextNode(node) {
    var parent = node.parentElement;
    if (!parent) return;
    if (blocked(parent)) return;

    var cur = node.nodeValue;
    if (cur === null || cur === undefined) return;
    var trimmed = cur.trim();
    if (!trimmed) return;

    var rec = getTextRec(node);
    /* 只有当前内容仍是我们写进去的英文时，记录里的 src 才算数；
       否则说明外部（app.js 每秒刷新）改写了它，当前值就是新的原文。 */
    var base = (rec && rec.out === cur) ? rec.src : cur;

    if (lang === EN) {
      var out = translate(base, parent);
      if (out !== cur) node.nodeValue = out;
      if (out !== base) setTextRec(node, { src: base, out: out });
      else if (rec) delTextRec(node);
      return;
    }

    /* zh-CN：还原成中文原文 */
    if (cur !== base) node.nodeValue = base;
    if (rec) delTextRec(node);
  }

  function applyAttrOf(el) {
    if (blocked(el)) return;
    var map = getAttrRec(el);
    for (var i = 0; i < ATTRS.length; i += 1) {
      var name = ATTRS[i];
      var cur = el.getAttribute(name);
      if (cur === null || cur === '') continue;
      var setting = map ? map[name] : null;
      var base = (setting && setting.out === cur) ? setting.src : cur;

      if (lang === EN) {
        var out = translate(base, el);
        if (out !== cur) el.setAttribute(name, out);
        if (out !== base) {
          map = map || {};
          map[name] = { src: base, out: out };
          setAttrRec(el, map);
        } else if (map && map[name]) {
          delete map[name];
        }
      } else if (cur !== base) {
        el.setAttribute(name, base);
        if (map && map[name]) delete map[name];
      }
    }
  }

  var SHOW_TEXT = 4;

  function apply(root) {
    var base = root || (document.body || null);
    if (!base) return;
    if (base.nodeType === 3) { applyTextNode(base); return; }
    if (base.nodeType !== 1 && base.nodeType !== 9 && base.nodeType !== 11) return;

    if (base.nodeType === 1) applyAttrOf(base);

    var walker;
    try {
      walker = document.createTreeWalker(base, SHOW_TEXT, null, false);
    } catch (e) {
      return;
    }
    var node = walker.nextNode();
    while (node) {
      applyTextNode(node);
      node = walker.nextNode();
    }

    if (base.querySelectorAll) {
      var els = base.querySelectorAll('[title],[placeholder],[aria-label]');
      for (var i = 0; i < els.length; i += 1) applyAttrOf(els[i]);
    }
  }

  /* ====================================================================== *
   * 7. 文档标题 / <html lang>
   * ====================================================================== */
  var titleSrc = null;
  var titleOut = null;

  function applyTitle() {
    if (typeof document.title !== 'string') return;
    var cur = document.title;
    if (titleSrc === null || (cur !== titleSrc && cur !== titleOut)) {
      titleSrc = cur;
      titleOut = null;
    }
    var out = lang === EN ? translate(titleSrc) : titleSrc;
    titleOut = out;
    if (document.title !== out) document.title = out;
  }

  function applyHtmlLang() {
    var root = document.documentElement;
    if (!root || !root.setAttribute) return;
    root.setAttribute('lang', lang === EN ? 'en' : 'zh-CN');
  }

  /* ====================================================================== *
   * 8. 语言状态
   * ====================================================================== */
  function normalize(value) {
    var v = String(value || '').toLowerCase();
    if (v.indexOf('en') === 0) return EN;
    return ZH;
  }

  function detect() {
    var saved = null;
    try { saved = localStorage.getItem(LANG_KEY); } catch (e) { saved = null; }
    if (saved && (saved === ZH || saved === EN)) return saved;
    var nav = '';
    try { nav = String(navigator.language || (navigator.languages && navigator.languages[0]) || ''); } catch (e) { nav = ''; }
    return /^en/i.test(nav) ? EN : ZH;
  }

  var lang = detect();
  var listeners = [];

  function emit(value) {
    var list = listeners.slice();
    for (var i = 0; i < list.length; i += 1) {
      try { list[i](value); } catch (e) { /* 回调里的异常不影响引擎 */ }
    }
  }

  /* ====================================================================== *
   * 9. MutationObserver（节流 120ms）
   * ====================================================================== */
  var queue = [];
  var flushTimer = 0;
  var observer = null;

  function dedupe(list) {
    var out = [];
    for (var i = 0; i < list.length; i += 1) {
      var node = list[i];
      if (!node) continue;
      var skip = false;
      for (var j = 0; j < out.length; j += 1) {
        var kept = out[j];
        if (kept === node) { skip = true; break; }
        if (kept.nodeType === 1 && kept.contains) {
          if (kept.contains(node)) { skip = true; break; }
          if (node.nodeType === 1 && node.contains(kept)) { out[j] = node; skip = true; break; }
        }
      }
      if (!skip) out.push(node);
    }
    return out;
  }

  function flushQueue() {
    flushTimer = 0;
    var list = queue;
    queue = [];
    if (!list.length) return;
    if (list.length > 400) { apply(document.body); return; }
    list = dedupe(list);
    for (var i = 0; i < list.length; i += 1) {
      var node = list[i];
      if (!node || !node.parentElement) continue;   /* 已从 DOM 移除 */
      apply(node);
    }
  }

  function schedule() {
    if (flushTimer) return;
    flushTimer = setTimeout(flushQueue, OBSERVE_DELAY);
  }

  function onMutations(records) {
    var i;
    var j;
    for (i = 0; i < records.length; i += 1) {
      var rec = records[i];
      if (rec.type === 'characterData') {
        var tn = rec.target;
        /* 自己刚写进去的英文会再触发一次 characterData：值等于记录里的 out 就跳过，避免回声 */
        var prev = (tn && tn.nodeType === 3) ? getTextRec(tn) : null;
        if (prev && prev.out === tn.nodeValue) continue;
        queue.push(tn);
      } else if (rec.type === 'childList') {
        for (j = 0; j < rec.addedNodes.length; j += 1) queue.push(rec.addedNodes[j]);
      }
    }
    if (queue.length) schedule();
  }

  function startObserver() {
    if (observer || typeof MutationObserver !== 'function' || !document.body) return;
    observer = new MutationObserver(onMutations);
    observer.observe(document.body, {
      childList: true,
      characterData: true,
      subtree: true,
    });
  }

  /* ====================================================================== *
   * 10. 对外 API
   * ====================================================================== */
  function current() { return lang; }

  function setLang(next) {
    var value = normalize(next);
    var changed = value !== lang;
    lang = value;
    try { localStorage.setItem(LANG_KEY, value); } catch (e) { /* 忽略隐私模式 */ }
    applyHtmlLang();
    apply(document.body);
    applyTitle();
    if (changed) emit(value);
    return value;
  }

  function toggle() {
    return setLang(lang === EN ? ZH : EN);
  }

  function t(zhText) {
    if (zhText === null || zhText === undefined) return zhText;
    var text = String(zhText);
    var trimmed = text.trim();
    if (!trimmed) return text;
    var out = lookup(trimmed, null);
    if (out === null || out === undefined) return text;
    return out;
  }

  function onChange(fn) {
    if (typeof fn !== 'function') return function () {};
    listeners.push(fn);
    try { fn(lang); } catch (e) { /* 忽略 */ }
    return function off() {
      var idx = listeners.indexOf(fn);
      if (idx >= 0) listeners.splice(idx, 1);
    };
  }

  /** 追加词条 / 模式（供后来的功能模块扩展，不用改本文件） */
  function extend(entries, rules) {
    if (entries && typeof entries === 'object') {
      Object.keys(entries).forEach(function (key) { DICT[key] = entries[key]; });
    }
    if (Object.prototype.toString.call(rules) === '[object Array]') {
      rules.forEach(function (rule) {
        if (rule && rule[0] && typeof rule[1] === 'function') RULES.push(rule);
      });
    }
    if (document.body) apply(document.body);
    return true;
  }

  window.slabI18n = {
    LANG_KEY: LANG_KEY,
    LANGS: { ZH: ZH, EN: EN },
    current: current,
    setLang: setLang,
    toggle: toggle,
    t: t,
    apply: apply,
    extend: extend,
    onChange: onChange,
  };

  /* ====================================================================== *
   * 11. 启动
   * ====================================================================== */
  var started = false;

  function boot() {
    if (started) return;
    started = true;
    applyHtmlLang();
    apply(document.body);
    applyTitle();
    startObserver();
    /* 首次渲染后 app.js 还会插入骨架 / 卡片，再补一轮（观察器之前已经漏掉的） */
    setTimeout(function () {
      if (document.body) apply(document.body);
      applyTitle();
    }, 400);
  }

  try {
    if (document.readyState === 'loading') {
      document.addEventListener('DOMContentLoaded', boot, false);
    } else {
      boot();
    }
  } catch (e) {
    /* 极端环境下静默失败，不影响页面其它脚本 */
  }

  /* 语言切换时同步 <html lang>（浏览器辅助功能 / 拼写检查会用到） */
  applyHtmlLang();
}());

/* =========================================================================
 * 接线示例（父级自行接线，本文件不改任何其它文件）
 *
 * 1) index.html 里，放在 app.js / term.js 之前或之后均可（defer 顺序执行）：
 *      <script src="/assets/i18n.js?v={{V}}" defer></script>
 *
 * 2) 顶栏按钮（放在 #btn-help 旁边）：
 *      <button class="btn ghost sm" id="btn-lang" type="button" title="Switch to English">EN</button>
 *
 * 3) 点击与文案同步（放在 i18n.js 之后的任意内联脚本里）：
 *      (function () {
 *        var b = document.getElementById('btn-lang');
 *        if (!b || !window.slabI18n) return;
 *        b.addEventListener('click', function () { window.slabI18n.toggle(); });
 *        window.slabI18n.onChange(function (lang) {          // 注册时会立即回调一次
 *          var en = lang === 'en-US';
 *          b.textContent = en ? '中' : 'EN';                 // 按钮自身不翻译，靠这里切换
 *          b.title = en ? '切换到中文' : 'Switch to English';
 *          b.setAttribute('aria-label', en ? 'Switch to Chinese' : 'Switch to English');
 *        });
 *      }());
 *
 * 4) 不想被翻译的元素（例如后端返回的原始路径、用户手写的文案）：
 *      <div data-no-i18n>…</div>
 * ========================================================================= */
