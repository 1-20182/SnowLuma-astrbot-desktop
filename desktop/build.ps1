# 打包 Electron 桌面版：外壳 + 内置 Python 后端（无需 electron-builder，纯手工组装，可离线）
# 用法： powershell -ExecutionPolicy Bypass -File desktop\build.ps1
param(
    [switch]$SkipInstall,
    [switch]$SkipSmoke
)

$ErrorActionPreference = "Stop"
$Desktop = $PSScriptRoot
$Root = Split-Path -Parent $Desktop
$ProjectName = "SnowLuma-Desktop"
$Version = (Get-Content (Join-Path $Root "VERSION") -Raw).Trim()
$OutDir = Join-Path $Desktop "dist"
$AppDir = Join-Path $OutDir $ProjectName
$ElectronDist = Join-Path $Desktop "node_modules\electron\dist"
$BackendSrc = Join-Path $Root "dist\SnowLuma-AstrBot-Console"

function Invoke-Native {
    param([scriptblock]$Command, [string]$What = "命令", [int]$Tail = 8)
    $old = $ErrorActionPreference
    $ErrorActionPreference = "Continue"
    $text = ""
    try {
        $text = (& $Command 2>&1 | Out-String)
        $code = $LASTEXITCODE
    } finally {
        $ErrorActionPreference = $old
    }
    if ($Tail -gt 0) {
        ($text -split "`r?`n" | Where-Object { $_.Trim() -ne "" } | Select-Object -Last $Tail) |
            ForEach-Object { Write-Host "   $_" }
    }
    if ($code -ne 0) { throw "$What 失败，退出码 $code" }
}

Write-Host "== 检查环境" -ForegroundColor Cyan
if (-not (Get-Command node -ErrorAction SilentlyContinue)) { throw "找不到 node，请先安装 Node.js 20+" }
if (-not (Test-Path $BackendSrc)) {
    throw "找不到后端：$BackendSrc`n请先运行 packaging\build-exe.ps1 生成控制台 exe"
}
if (-not (Test-Path (Join-Path $BackendSrc "SnowLuma-AstrBot-Console.exe"))) {
    throw "后端目录里没有 SnowLuma-AstrBot-Console.exe：$BackendSrc"
}

if (-not $SkipInstall) {
    Write-Host "== 安装 Electron（首次约 180 MB）" -ForegroundColor Cyan
    Push-Location $Desktop
    try {
        $env:ELECTRON_MIRROR = "https://npmmirror.com/mirrors/electron/"
        Invoke-Native { & npm install --no-audit --no-fund } "npm install" 6
    } finally {
        Pop-Location
    }
}

if (-not (Test-Path (Join-Path $ElectronDist "electron.exe"))) { throw "Electron 未安装完整：$ElectronDist" }

if (-not $SkipSmoke) {
    Write-Host "== 冒烟测试：启动 → 关窗 → 检查后端是否被回收" -ForegroundColor Cyan
    $env:ELECTRON_RUN_AS_NODE = $null
    Remove-Item Env:\ELECTRON_RUN_AS_NODE -ErrorAction SilentlyContinue
    # Electron 是 GUI 子系统程序：PowerShell 的 & 不会等待它，必须 Start-Process -Wait
    Push-Location $Desktop
    $smokeLog = Join-Path $OutDir "smoke.log"
    New-Item -ItemType Directory -Force $OutDir | Out-Null
    $beforePids = @(Get-Process "SnowLuma-AstrBot-Console" -ErrorAction SilentlyContinue | ForEach-Object { $_.Id })
    try {
        $proc = Start-Process -FilePath (Join-Path $ElectronDist "electron.exe") `
            -ArgumentList ".", "--smoke-test" -PassThru -Wait -NoNewWindow `
            -RedirectStandardOutput $smokeLog -RedirectStandardError "$smokeLog.err"
        Get-Content $smokeLog -Tail 6 -ErrorAction SilentlyContinue | ForEach-Object { Write-Host "   $_" }
        if ($proc.ExitCode -ne 0) { throw "冒烟测试失败，退出码 $($proc.ExitCode)" }
    } finally {
        Pop-Location
    }
    Start-Sleep -Seconds 2
    # 只看"本次冒烟新起的"后端进程：用户自己正开着的控制台不该算残留
    $left = @(Get-Process "SnowLuma-AstrBot-Console" -ErrorAction SilentlyContinue |
        Where-Object { $_.Id -notin $beforePids })
    if ($left.Count -gt 0) { throw "冒烟测试失败：关窗后仍残留 $($left.Count) 个后端进程（$($left.Id -join ',')）" }
    Write-Host "   关窗后无残留进程 ✓" -ForegroundColor Green
}

Write-Host "== 组装 $ProjectName" -ForegroundColor Cyan
if (Test-Path $AppDir) { Remove-Item -Recurse -Force $AppDir }
New-Item -ItemType Directory -Force $AppDir | Out-Null

# 1) Electron 运行时
Copy-Item -Path (Join-Path $ElectronDist "*") -Destination $AppDir -Recurse -Force
$electronExe = Join-Path $AppDir "electron.exe"
$targetExe = Join-Path $AppDir "$ProjectName.exe"
if (Test-Path $electronExe) { Move-Item $electronExe $targetExe -Force }

# 1b) 版本号文件：Electron 的 dist 自带一个叫 version 的文件（内容是 Electron 版本，如 33.4.11）。
#     控制台的「升级」按"压缩包里最浅的 VERSION"判版本（slab/upgrade.py::read_zip_version，
#     文件名大小写不敏感），这个文件会被当成"新版本 33.4.11"显示给用户。
#     Electron 运行时不读它 → 改名，并放一份我们自己的 VERSION。
$electronVersionFile = Join-Path $AppDir "version"
if (Test-Path $electronVersionFile) {
    Move-Item $electronVersionFile (Join-Path $AppDir "electron-version") -Force
}
Copy-Item (Join-Path $Root "VERSION") (Join-Path $AppDir "VERSION") -Force

# 2) 外壳代码 → resources\app
$resApp = Join-Path $AppDir "resources\app"
New-Item -ItemType Directory -Force $resApp | Out-Null
foreach ($f in @("main.js", "preload.js", "splash.html", "package.json")) {
    Copy-Item (Join-Path $Desktop $f) $resApp -Force
}
New-Item -ItemType Directory -Force (Join-Path $resApp "assets") | Out-Null
Copy-Item (Join-Path $Desktop "assets\*") (Join-Path $resApp "assets") -Force -ErrorAction SilentlyContinue

# 3) Python 后端 → resources\backend（去掉运行时产生的 data/runtime）
$resBackend = Join-Path $AppDir "resources\backend"
New-Item -ItemType Directory -Force $resBackend | Out-Null
Copy-Item -Path (Join-Path $BackendSrc "*") -Destination $resBackend -Recurse -Force
foreach ($junk in @("data", "runtime")) {
    $p = Join-Path $resBackend $junk
    if (Test-Path $p) { Remove-Item -Recurse -Force $p }
}

# 4) 使用说明
$lines = @(
"SnowLuma x AstrBot 联合控制台 v$Version（桌面版）"
"=============================================="
"1. 双击 SnowLuma-Desktop.exe（别在压缩包里直接双击，先解压）"
"2. 右上角三个按钮：— 最小化 / ▢ 最大化 / ◎ 置顶；关闭是【长按电源键 ⏻ 约 1 秒】"
"   电源键弹窗里选：收起窗口（服务继续跑）或 彻底退出（停服务并结束进程）"
"3. 首次跟着四步向导走：环境检查 → 装两个项目 → 填机器人小号 QQ + 大模型 Key → 启动"
"4. 「运行 → 接入 QQ」点【用这个】把机器人小号注入（不需要二维码）"
"5. 机器人不回话/群聊不回/插件不触发：联调页点【双向自检】→【修复发送链路】"
"6. 完整操作手册：docs\\MANUAL.md（升级看 docs\\UPGRADE.md）"
""
"数据与安装在默认非 C 盘：<盘符>:\\SnowLumaAstrBot\\{data,runtime,downloads}"
"整个文件夹可拷到别的 Windows 10/11 x64 机器，无需安装 Python。"
)
Set-Content -Path (Join-Path $AppDir "使用说明.txt") -Value $lines -Encoding UTF8

$sizeMb = [math]::Round(((Get-ChildItem $AppDir -Recurse -File | Measure-Object Length -Sum).Sum) / 1MB, 1)
Write-Host "== 产物：$AppDir（$sizeMb MB）" -ForegroundColor Green

Write-Host "== 打压缩包（约 1 分钟）" -ForegroundColor Cyan
$zip = Join-Path $OutDir "$ProjectName-v$Version-win-x64.zip"
if (Test-Path $zip) { Remove-Item $zip -Force }
Compress-Archive -Path $AppDir -DestinationPath $zip -CompressionLevel Optimal
$zipMb = [math]::Round((Get-Item $zip).Length / 1MB, 1)
Write-Host "== 压缩包：$zip（$zipMb MB）" -ForegroundColor Green
Write-Host "== 完成：$(Join-Path $AppDir "$ProjectName.exe")" -ForegroundColor Green
