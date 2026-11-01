@echo off
chcp 65001 >nul
setlocal
echo ============================================================
echo  构建 SnowLuma x AstrBot 联合控制台 exe（绿色版）
echo ============================================================
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0build-exe.ps1" %*
if errorlevel 1 (
  echo.
  echo 构建失败，请查看上面的错误信息。
  pause
  exit /b 1
)
echo.
echo 构建完成，产物在 dist\ 目录下。
pause
