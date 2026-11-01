# 构建 Windows exe（onedir 绿色版 + 可选 onefile 单文件）
# 用法： powershell -ExecutionPolicy Bypass -File packaging\build-exe.ps1
#         加 -OneFile 打单文件 exe；加 -Console 保留黑框便于排查
param(
    [switch]$OneFile,
    [switch]$Console,
    [switch]$SkipInstall
)

$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $PSScriptRoot
$VenvPy = Join-Path $Root ".venv\Scripts\python.exe"
$Dist = Join-Path $Root "dist"
$Work = Join-Path $Root "build"
$AppName = "SnowLuma-AstrBot-Console"
$Version = (Get-Content (Join-Path $Root "VERSION") -Raw).Trim()

# Windows PowerShell 5.1 在 $ErrorActionPreference=Stop 下会把原生命令的 stderr
# 当成终止性错误（uv/pip/PyInstaller 都会往 stderr 写正常进度），所以统一走这层包装。
function Invoke-Native {
    param([scriptblock]$Command, [string]$What = "命令", [int]$Tail = 10)
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

Write-Host "== 项目目录: $Root" -ForegroundColor Cyan
if (-not (Test-Path $VenvPy)) {
    throw "找不到 .venv\Scripts\python.exe，请先双击 一键启动.bat 初始化（或 uv venv --python 3.12 .venv）"
}

if (-not $SkipInstall) {
    Write-Host "== 安装打包依赖（pyinstaller / pillow）" -ForegroundColor Cyan
    $uv = Get-Command uv -ErrorAction SilentlyContinue
    if ($uv) {
        Invoke-Native { & uv pip install --python $VenvPy pyinstaller pillow } "安装 pyinstaller/pillow" 4
    } else {
        Invoke-Native { & $VenvPy -m pip install -q pyinstaller pillow } "安装 pyinstaller/pillow" 4
    }
}

Write-Host "== 生成版本资源（$Version）" -ForegroundColor Cyan
$parts = ($Version -split '\.') + @('0','0','0','0')
$vf = @"
# UTF-8
VSVersionInfo(
  ffi=FixedFileInfo(filevers=($($parts[0]), $($parts[1]), $($parts[2]), $($parts[3])), prodvers=($($parts[0]), $($parts[1]), $($parts[2]), $($parts[3])), mask=0x3f, flags=0x0, OS=0x40004, fileType=0x1, subtype=0x0, date=(0, 0)),
  kids=[StringFileInfo([StringTable(u'080404b0', [StringStruct(u'CompanyName', u'SnowLuma x AstrBot Console'), StringStruct(u'FileDescription', u'SnowLuma x AstrBot 联合控制台'), StringStruct(u'FileVersion', u'$Version'), StringStruct(u'InternalName', u'SnowLuma-AstrBot-Console'), StringStruct(u'OriginalFilename', u'SnowLuma-AstrBot-Console.exe'), StringStruct(u'ProductName', u'SnowLuma x AstrBot 联合控制台'), StringStruct(u'ProductVersion', u'$Version')])]), VarFileInfo([VarStruct(u'Translation', [2052, 1200])])]
)
"@
Set-Content -Path (Join-Path $PSScriptRoot "version_info.txt") -Value $vf -Encoding UTF8

Write-Host "== 生成图标" -ForegroundColor Cyan
Invoke-Native { & $VenvPy (Join-Path $PSScriptRoot "make_icon.py") } "生成图标" 3

if ($Console) { $env:SLAB_EXE_CONSOLE = "1" } else { Remove-Item Env:\SLAB_EXE_CONSOLE -ErrorAction SilentlyContinue }
if ($OneFile) { $env:SLAB_EXE_ONEFILE = "1" } else { Remove-Item Env:\SLAB_EXE_ONEFILE -ErrorAction SilentlyContinue }

Write-Host "== PyInstaller 打包（首次约 1-3 分钟）" -ForegroundColor Cyan
$specPath = Join-Path $PSScriptRoot "console.spec"
Push-Location $Root
try {
    Invoke-Native { & $VenvPy -m PyInstaller --clean --noconfirm --workpath $Work --distpath $Dist $specPath } "PyInstaller" 14
} finally {
    Pop-Location
}

if ($OneFile) {
    $exe = Join-Path $Dist ($AppName + ".exe")
    if (-not (Test-Path $exe)) { throw "打包产物缺失: $exe" }
    $mb = [math]::Round((Get-Item $exe).Length / 1MB, 1)
    Write-Host "== 产物: $exe ($mb MB)" -ForegroundColor Green
    exit 0
}

$ExeDir = Join-Path $Dist $AppName
$exe = Join-Path $ExeDir ($AppName + ".exe")
if (-not (Test-Path $exe)) { throw "打包产物缺失: $exe" }

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
Set-Content -Path (Join-Path $ExeDir "使用说明.txt") -Value $lines -Encoding UTF8

$totalMb = [math]::Round(((Get-ChildItem $ExeDir -Recurse -File | Measure-Object Length -Sum).Sum) / 1MB, 1)
Write-Host "== 产物: $ExeDir（共 $totalMb MB）" -ForegroundColor Green

$zip = Join-Path $Dist ($AppName + "-v$Version-win-x64.zip")
if (Test-Path $zip) { Remove-Item $zip -Force }
Compress-Archive -Path $ExeDir -DestinationPath $zip -CompressionLevel Optimal
$zipMb = [math]::Round((Get-Item $zip).Length / 1MB, 1)
Write-Host "== 压缩包: $zip ($zipMb MB)" -ForegroundColor Green
Write-Host "== 完成：$exe" -ForegroundColor Green
