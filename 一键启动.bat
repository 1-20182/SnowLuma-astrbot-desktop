@echo off
chcp 65001 >nul
setlocal enabledelayedexpansion
title SnowLuma x AstrBot 联合控制台
cd /d "%~dp0"

rem ===========================================================================
rem  SnowLuma × AstrBot 联合控制台 —— Windows 一键启动脚本
rem
rem  做的事：
rem    1. 检查项目内的 .venv 虚拟环境，没有就用 uv（优先）或 python -m venv 创建
rem    2. 安装 requirements.txt 里的依赖（已装则跳过）
rem    3. 几秒后自动打开浏览器 http://127.0.0.1:8790
rem    4. 前台运行 python -m slab serve（关掉这个窗口就停止控制台）
rem
rem  本文件请保存为 UTF-8 编码；第一行 chcp 65001 已经切换到 UTF-8 代码页。
rem ===========================================================================

rem ---- 自调用分支：延迟 5 秒后打开浏览器，然后自己退出 ----
if /i "%~1"=="__open_browser" (
  timeout /t 5 /nobreak >nul
  start "" "http://127.0.0.1:8790"
  exit /b 0
)

echo ============================================================
echo   SnowLuma x AstrBot 联合控制台 - 一键启动
echo ============================================================
echo.

set "PY=%CD%\.venv\Scripts\python.exe"

rem ---------------------------------------------------------------------------
rem 1) 虚拟环境
rem ---------------------------------------------------------------------------
if exist "%PY%" goto :have_venv

echo [1/3] 正在创建虚拟环境 .venv（首次运行需要 1-2 分钟）...

where uv >nul 2>nul
if errorlevel 1 goto :venv_builtin

echo       使用 uv 创建（更快）...
uv venv --python 3.12 .venv
if not exist "%PY%" goto :venv_fail
goto :have_venv

:venv_builtin
echo       没有检测到 uv，改用 Python 自带的 venv...
where python >nul 2>nul
if errorlevel 1 goto :no_python
python -m venv .venv
if not exist "%PY%" goto :venv_fail
goto :have_venv

:no_python
echo.
echo [错误] 没有检测到 Python，也没有现成的虚拟环境。
echo        请先安装 Python 3.12 或更高版本（安装时勾选 "Add python.exe to PATH"）：
echo        https://www.python.org/downloads/windows/
echo.
pause
exit /b 1

:venv_fail
echo.
echo [错误] 虚拟环境创建失败。
echo        请确认 Python 版本不低于 3.12，可执行：python --version
echo        也可以手动执行：python -m venv .venv
echo.
pause
exit /b 1

:have_venv
echo [1/3] 虚拟环境就绪：%PY%

rem ---------------------------------------------------------------------------
rem 2) 依赖
rem ---------------------------------------------------------------------------
echo [2/3] 检查控制台依赖（fastapi / uvicorn / websockets ...）...
"%PY%" -c "import fastapi, uvicorn, httpx, websockets" >nul 2>nul
if not errorlevel 1 goto :deps_ok

echo       正在安装 requirements.txt 中的依赖...
where uv >nul 2>nul
if errorlevel 1 goto :deps_pip

uv pip install --python "%PY%" -r requirements.txt
if errorlevel 1 goto :deps_fail
goto :deps_ok

:deps_pip
echo       没有检测到 uv，改用 pip...
"%PY%" -m pip --version >nul 2>nul
if errorlevel 1 "%PY%" -m ensurepip --upgrade >nul 2>nul
"%PY%" -m pip install -r requirements.txt
if errorlevel 1 goto :deps_fail

:deps_ok
echo [2/3] 依赖就绪。

rem ---------------------------------------------------------------------------
rem 3) 启动控制台
rem ---------------------------------------------------------------------------
echo [3/3] 正在启动控制台...
echo       浏览器会在 5 秒后自动打开；若没有打开，请手动访问 http://127.0.0.1:8790
echo       关闭本窗口 = 停止控制台及其启动的 SnowLuma / AstrBot。
echo.
start "" /min cmd /c call "%~f0" __open_browser
"%PY%" -m slab serve

if errorlevel 1 goto :serve_fail
echo.
echo 控制台已退出。
pause
exit /b 0

:deps_fail
echo.
echo [错误] 依赖安装失败。常见原因：
echo        1) 网络无法访问 PyPI —— 可先执行：set HTTPS_PROXY=http://127.0.0.1:7890 再重跑本脚本
echo        2) 杀毒软件拦截了 python.exe
echo        3) 虚拟环境损坏 —— 删除 .venv 文件夹后重新运行本脚本
echo.
pause
exit /b 1

:serve_fail
echo.
echo [错误] 控制台启动失败。常见原因：
echo        1) 8790 端口被其它程序占用 —— 改 data\config.json 里的 app.port，或先关掉占用程序
echo        2) 依赖没有装全 —— 删除 .venv 文件夹后重新运行本脚本
echo        3) 杀毒软件拦截 python.exe —— 把本目录加入白名单
echo.
echo        详细排查见 docs\TROUBLESHOOTING.md
echo.
pause
exit /b 1
