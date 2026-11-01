# 语义化版本号提升：./scripts/bump.ps1 patch|minor|major|x.y.z "更新说明"
# 会同步 VERSION / desktop\package.json / CHANGELOG.md 顶部条目。
param(
    [Parameter(Mandatory = $true, Position = 0)][string]$Kind,
    [Parameter(Position = 1)][string]$Note = ""
)

$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $PSScriptRoot
$VersionFile = Join-Path $Root "VERSION"
$Changelog = Join-Path $Root "CHANGELOG.md"
$DesktopPkg = Join-Path $Root "desktop\package.json"

$current = (Get-Content $VersionFile -Raw).Trim()
if ($current -notmatch '^(\d+)\.(\d+)\.(\d+)$') { throw "VERSION 不是 x.y.z：$current" }
$major = [int]$Matches[1]; $minor = [int]$Matches[2]; $patch = [int]$Matches[3]

if ($Kind -match '^\d+\.\d+\.\d+$') {
    $next = $Kind
} elseif ($Kind -eq 'major') {
    $next = "$($major + 1).0.0"
} elseif ($Kind -eq 'minor') {
    $next = "$major.$($minor + 1).0"
} elseif ($Kind -eq 'patch') {
    $next = "$major.$minor.$($patch + 1)"
} else {
    throw "用法：bump.ps1 patch|minor|major|x.y.z [说明]"
}

Set-Content -Path $VersionFile -Value "$next`n" -NoNewline -Encoding ascii

# desktop/package.json 同步版本（纯文本替换，避免 PowerShell 处理中文乱码）
$pkgText = [System.IO.File]::ReadAllText($DesktopPkg, [System.Text.Encoding]::UTF8)
$pkgText = [regex]::Replace($pkgText, '"version"\s*:\s*"[^"]*"', ('"version": "' + $next + '"'))
[System.IO.File]::WriteAllText($DesktopPkg, $pkgText, (New-Object System.Text.UTF8Encoding($false)))

# CHANGELOG 顶部插入条目
if (Test-Path $Changelog) {
    $text = Get-Content $Changelog -Raw
    $date = Get-Date -Format 'yyyy-MM-dd'
    $entry = "## [$next] - $date`n`n### 变更`n- $Note`n`n"
    $marker = "## [1.1.0]"
    if ($text.Contains($marker)) {
        $text = $text.Replace($marker, $entry + $marker)
    } else {
        $text = $text.TrimEnd() + "`n`n" + $entry
    }
    Set-Content -Path $Changelog -Value $text -Encoding UTF8
}

Write-Host "版本：$current -> $next" -ForegroundColor Green
Write-Host "记得：重新打包（packaging\build-exe.bat → desktop\build.bat），发布物会带新版本号。" -ForegroundColor Cyan
