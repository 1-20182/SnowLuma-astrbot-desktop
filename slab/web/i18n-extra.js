/* 后续功能模块（向导 / 维护 / 安装路径）的补充词条。
   在 i18n.js 之后加载，靠 slabI18n.extend 合并进主词典，不污染主文件。 */
(function () {
  'use strict';
  var api = window.slabI18n;
  if (!api || typeof api.extend !== 'function') return;

  api.extend({
    /* ---- 首次启动向导 ---- */
    '首次使用引导': 'First-run setup',
    '四步把机器人跑起来': 'Get your bot running in four steps',
    '环境检查': 'Environment',
    '安装项目': 'Install',
    '填写配置': 'Configure',
    '启动': 'Launch',
    '跳过': 'Skip',
    '上一步': 'Back',
    '下一步': 'Next',
    '开始使用': 'Get started',
    '开始安装': 'Start install',
    '已安装': 'Installed',
    '未安装': 'Not installed',
    '正在安装…': 'Installing…',
    '等待输出…': 'Waiting for output…',
    '正在检查…': 'Checking…',
    '检查失败': 'Check failed',
    '提醒': 'Notice',
    '磁盘空间': 'Disk space',
    '端口': 'Ports',
    '先确认这台机器具备运行条件；有黄色项也不一定挡路，向导会给出替代方案。':
      'Let us check whether this machine is ready. Yellow items are not always blockers — the wizard suggests workarounds.',
    'QQ 协议端': 'QQ protocol end',
    '大模型框架': 'LLM framework',
    '大约 3–10 分钟；过程中不会弹命令行黑框': 'Takes about 3–10 minutes; no console windows will pop up',
    '已开始安装 SnowLuma 与 AstrBot…': 'Installing SnowLuma and AstrBot…',
    '两个项目都装好了，点「下一步」填写配置': 'Both projects are installed — click Next to fill in the settings',
    '机器人 QQ 号': 'Bot QQ number',
    '管理员 QQ（可留空 = 同上）': 'Admin QQ (leave empty to reuse the bot QQ)',
    '填自己的 QQ 号才能用 /help 等指令': 'Fill in your own QQ to use /help and other commands',
    '大模型 API Key': 'LLM API key',
    '模型名': 'Model',
    '接口地址 Base URL': 'API base URL',
    '检查并自动换端口': 'Check and fix ports',
    '端口：': 'Ports: ',
    '正在把配置写入 SnowLuma / AstrBot…': 'Writing config to SnowLuma / AstrBot…',
    '正在启动服务…': 'Starting services…',
    '✔ 已启动。回到 QQ 私聊机器人，或群里 @它试试。':
      '✔ Started. Go back to QQ and DM the bot, or @ it in a group.',
    '完成后这个页面会自动刷新': 'This page will refresh when finished',
    '点「开始使用」后会：把配置写进两个项目 → 启动 SnowLuma 与 AstrBot → 刷新页面。':
      '“Get started” will write the config into both projects, start SnowLuma and AstrBot, then refresh this page.',
    '填了你的 QQ 号即可使用 /help、/reset 等指令': 'Fill in your QQ to enable /help, /reset and other commands',
    '大模型 Key 太短，请检查是否完整': 'The API key looks too short — please check it',
    'QQ 号看起来不对（应该是 5 位以上数字）': 'That QQ number looks wrong (5+ digits expected)',

    /* ---- 维护卡片 ---- */
    '维护': 'Maintenance',
    '长期挂着跑时用得着': 'Useful when running for a long time',
    '日志保留天数': 'Log retention (days)',
    '安装包保留天数': 'Installer retention (days)',
    '立即清理': 'Clean up now',
    '检查并自动换端口': 'Check & fix ports',
    '控制台每 6 小时自动清理一次；填 0 表示不自动删。单个日志超过 20 MB 会先截断再保留。':
      'Cleanup runs every 6 hours; set 0 to disable. A single log over 20 MB is truncated first.',
    '已清理': 'Cleaned',
    '个文件，释放': 'files, freed',
    '没有需要清理的文件': 'Nothing to clean up',
    '端口都空闲，无需调整': 'All ports are free — nothing to change',
    '端口已自动避让': 'Ports were moved automatically',
    '控制台端口变了，请手动重启控制台': 'The console port changed — please restart the console manually',
    '检查端口失败': 'Port check failed',
    '清理失败': 'Cleanup failed',

    /* ---- 安装位置 / 备份 ---- */
    '安装位置与备份': 'Install location & backup',
    '想装到 D 盘就改这里；留空用默认位置': 'Pick another drive here; leave empty for the default',
    'SnowLuma 安装目录': 'SnowLuma install dir',
    'AstrBot 安装目录': 'AstrBot install dir',
    '数据目录': 'Data directory',
    '（配置、日志、下载缓存）': ' (config, logs, download cache)',
    '选择…': 'Browse…',
    '打开': 'Open',
    '导出配置': 'Export config',
    '导入配置': 'Import config',
    '导出的文件里含 API Key 与联动 Token，别乱发；导入会整份覆盖当前配置。':
      'The exported file contains API keys and tokens — keep it private. Import replaces the whole config.',
    '数据目录固定在程序旁边（桌面版在': 'The data directory lives next to the app (desktop build: ',
    '）：想换盘就把整个程序文件夹搬过去。': '). To move it, move the whole program folder.',
    '配合键': 'Combined key',
    '留空 = 程序目录\\runtime\\snowluma': 'Empty = <app>\\runtime\\snowluma',
    '留空 = 程序目录\\runtime\\astrbot': 'Empty = <app>\\runtime\\astrbot',
    '导出配置': 'Export config',
    '导入配置': 'Import config',
    '导出失败': 'Export failed',
    '导入失败': 'Import failed',
    '配置已导出': 'Config exported',
    '配置已导入': 'Config imported',
    '文件里含密钥，注意保管': 'The file contains secrets — keep it safe',
    '文件不是合法的 JSON': 'That file is not valid JSON',

    /* ---- 平台差异：Linux 文案（前端按后端 env 字段切换，Windows 那份仍走 i18n.js） ---- */
    '可用': 'Available',
    '不可用': 'Unavailable',
    '修复：': 'Fix:',
    '路径已复制': 'Path copied',
    '路径已复制，可粘贴到文件管理器地址栏': 'Path copied — paste it into the file manager address bar',

    /* 环境自检：Linux 专属项 */
    'Python（≥ 3.12，控制台与 AstrBot 都要）': 'Python (≥ 3.12 — the console and AstrBot both need it)',
    '可用磁盘空间（建议 ≥ 3 GB）': 'Free disk space (≥ 3 GB recommended)',
    'python3（AstrBot 需要 ≥ 3.12）': 'python3 (AstrBot needs ≥ 3.12)',
    'python3-venv（创建虚拟环境需要）': 'python3-venv (needed to create a virtualenv)',
    'Yama ptrace_scope（= 3 时禁止注入）': 'Yama ptrace_scope (3 forbids injection)',
    'node 的 cap_sys_ptrace（注入权限）': 'cap_sys_ptrace on node (injection permission)',
    '图形会话（DISPLAY，扫码登录需要）': 'Graphical session (DISPLAY, needed for the QR login)',
    '与 QQ 同一个用户（uid）': 'Same user as QQ (uid)',
    '未知（缺 libcap2-bin 时无法确认）': 'Unknown (cannot confirm without libcap2-bin)',
    '未知（未装 getcap）': 'Unknown (getcap not installed)',
    '未启用 Yama': 'Yama not enabled',
    '无 DISPLAY / WAYLAND_DISPLAY': 'No DISPLAY / WAYLAND_DISPLAY',
    '缺少 venv/ensurepip': 'venv/ensurepip missing',
    '有': 'Yes',
    '没有': 'No',

    /* 部署 / 配置页：路径说法 */
    '不会额外弹出终端窗口': 'no extra terminal window pops up',
    '清空备份（data/backups）': 'Clear backups (data/backups)',
    '「卸载」会把目录备份到 data/backups（可回滚）；「彻底删除」直接删掉安装目录和它的历史备份，不可恢复。':
      'Uninstall backs the directory up to data/backups (rollback-able); permanent delete removes the install directory and its history for good.',
    '默认：程序目录/runtime/snowluma': 'Default: <program dir>/runtime/snowluma',
    '默认：程序目录/runtime/astrbot': 'Default: <program dir>/runtime/astrbot',
    '留空 = 程序目录/runtime/snowluma': 'Blank = <program dir>/runtime/snowluma',
    '留空 = 程序目录/runtime/astrbot': 'Blank = <program dir>/runtime/astrbot',
    '在文件管理器里打开': 'Open in the file manager',
    '想放到别的挂载点就改这里；留空用默认位置': 'Change this to use another mount point; blank means the default',
    '程序文件：被新版本覆盖，覆盖前自动备份到 data/backups/program-v…':
      'Program files: overwritten by the new version, backed up to data/backups/program-v… first',

    /* QQ 页：Linux 没有 DLL 注入（深度分身），多开靠独立 HOME */
    '独立 HOME 多开': 'Multi-instance (separate HOME)',
    '用独立 HOME 启动一个新的 QQ 实例（Linux 没有 DLL 注入这回事）':
      'Start an extra QQ instance with its own HOME (there is no DLL injection on Linux)',
    '选择要多开的 QQ 安装': 'Pick the QQ installation to launch',
    '用机器人账号登录 QQ 客户端：先用官方 Linux QQ（默认装在 /opt/QQ）扫码登录；登录后别退出、别切号，让 QQ 留在后台运行。没有图形会话（DISPLAY 为空）时扫码做不了（见 docs/LINUX.md 第 7 节）。':
      'Sign the bot account in on the QQ client: scan the QR code in the official Linux QQ (installed at /opt/QQ by default), then keep it running in the background without switching accounts. Without a graphical session (empty DISPLAY) you cannot scan the code — see docs/LINUX.md §7.',
    '权限不一致：控制台与 QQ 必须是同一个 Linux 用户（同一个 uid），两边都别用 sudo。':
      'Permission mismatch: the console and QQ must run as the same Linux user (same uid) — do not use sudo for either.',
    '注入被拒：给 node 打上 cap_sys_ptrace，并确认 ptrace_scope 不是 3（见 docs/LINUX.md 第 4 节）。':
      'Injection refused: grant cap_sys_ptrace to node and make sure ptrace_scope is not 3 (see docs/LINUX.md §4).',
    'Linux 上没有「深度分身（DLL 注入）」：多开用独立 HOME，注入前需要给 node 打上 cap_sys_ptrace，并保证 ptrace_scope ≤ 2。':
      'Linux has no "deep clone" (DLL injection): run extra accounts with a separate HOME, and grant cap_sys_ptrace to node with ptrace_scope ≤ 2 before injecting.',
    '未检测到官方 Linux QQ：先装官方 QQNT deb（apt install -y ./QQ_*.deb，x64 用 amd64 包、arm64 用 arm64 包，默认装到 /opt/QQ），再在图形会话里扫码登录。':
      'Official Linux QQ (QQNT) not found: install the official .deb first (apt install -y ./QQ_*.deb — amd64 package on x64, arm64 package on arm64; it lands in /opt/QQ), then scan the QR code in a graphical session.',
    '已用独立 HOME 启动多开 QQ': 'Extra QQ instance started with its own HOME',
    '在窗口里登录小号，再回来点「刷新进程 → 用这个」':
      'Sign the spare account in on that window, then come back and click "Refresh processes → Use this one"',
    '独立 HOME 多开失败': 'Failed to start the extra instance (separate HOME)',
    '隔离启动 QQ': 'Start an isolated QQ',
    '选择要隔离启动的 QQ 安装': 'Pick the QQ installation to launch in isolation',
    '已启动深度分身 QQ': 'Deep-clone QQ started',
    '已启动轻量隔离 QQ': 'Light-isolation QQ started',

    /* 帮助弹窗 / 向导 */
    '登录 QQ：用机器人小号在官方 Linux QQ（默认 /opt/QQ）里扫码登录，登录后保持 QQ 在后台运行。SnowLuma 不会弹二维码——它通过 ptrace 注入已登录的 QQ 进程来接管会话，所以扫码动作在 QQ 客户端里完成。':
      'Log in to QQ: scan the QR code in the official Linux QQ (at /opt/QQ by default) with the spare bot account and keep it running. SnowLuma never shows a QR code — it attaches to the already logged-in QQ process via ptrace, so the scan happens inside QQ itself.',
    '注入失败：多半是缺 cap_sys_ptrace 或 ptrace_scope=3，见 docs/LINUX.md 第 4 节。':
      'Injection failed: usually a missing cap_sys_ptrace or ptrace_scope=3 — see docs/LINUX.md §4.',
    '大约 3–10 分钟；输出都显示在下方日志里': 'Takes about 3–10 minutes; the output shows up in the log below',

    /* 后端连不上时的说法（平台未知时用中性那份） */
    '连不上控制台后端，请确认终端里控制台进程还在运行（可重新运行「一键启动.sh」）':
      'Cannot reach the backend — check that the console is still running in your terminal (you can re-run "./一键启动.sh").',
    '连不上控制台后端，请确认控制台进程没有退出（Windows 双击「一键启动.bat」，Linux 运行「一键启动.sh」）':
      'Cannot reach the backend — make sure the console process has not exited (Windows: re-run "一键启动.bat"; Linux: re-run "./一键启动.sh").',

    /* 部署页：Node 门槛 / 版本说法（Linux） */
    'lite 版体积更小，但要求本机 Node 满足 ^24.22.1 || >=26.12.1（发行版自带的 node 基本都不满足），否则启动会失败。':
      'The lite edition is smaller but needs Node ^24.22.1 || >=26.12.1 on this machine (distro-packaged node almost never matches) — otherwise it will not start.',
    'full 版自带 node，不用自己装 Node，小白首选（发行版仓库里的 node 版本普遍不够）；lite 版体积更小，但要求本机 Node 满足 ^24.22.1 || >=26.12.1。':
      'The full edition bundles node, so you do not install Node yourself — best for beginners (distro repo node versions are usually too old). The lite edition is smaller but needs Node ^24.22.1 || >=26.12.1.',

    /* ---- 语言按钮 ---- */
    '切换到中文': 'Switch to Chinese',
    'Switch to English': 'Switch to English',
  });

  /* 动态文本：向导/维护里带数字的句子 */
  api.extend(null, [
    [/^已清理 (\d+) 个过期文件$/, function (m) { return 'Cleaned ' + m[1] + ' expired files'; }],
    [/^已清理 (\d+) 个文件，释放 ([\d.]+) MB$/, function (m) { return 'Cleaned ' + m[1] + ' files, freed ' + m[2] + ' MB'; }],
    [/^释放 ([\d.]+) MB$/, function (m) { return 'Freed ' + m[1] + ' MB'; }],
    [/^([\d.]+) GB 可用（建议 ≥ 3 GB）$/, function (m) { return m[1] + ' GB free (3 GB+ recommended)'; }],
    [/^被占用：(.+?)（下一步可以一键换端口）$/, function (m) { return 'In use: ' + m[1] + ' (fix in the next step)'; }],
    [/^(\d+) \/ (\d+) \/ (\d+) \/ (\d+) 都空闲$/, function (m) {
      return m[1] + ' / ' + m[2] + ' / ' + m[3] + ' / ' + m[4] + ' are free';
    }],
    [/^端口 (.+?)：(\d+) → (\d+)（(.+?)）$/, function (m) {
      return 'Port ' + m[1] + ': ' + m[2] + ' → ' + m[3] + ' (' + m[4] + ')';
    }],
  ]);
}());
