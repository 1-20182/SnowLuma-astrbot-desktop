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
