@echo off
chcp 65001 >nul
setlocal
echo ============================================================
echo  打包 SnowLuma 桌面版（Electron 外壳 + Python 后端）
echo ============================================================
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0build.ps1" %*
if errorlevel 1 (
  echo.
  echo 打包失败，请查看上面的错误信息。
  pause
  exit /b 1
)
echo.
echo 打包完成，产物在 desktop\dist\ 目录下。
pause
