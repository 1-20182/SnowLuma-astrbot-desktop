#!/usr/bin/env bash
# =============================================================================
#  SnowLuma × AstrBot 联合控制台 —— Linux / macOS 一键启动脚本
#
#  做的事（与 Windows 的 一键启动.bat 完全等价）：
#    1. 检查项目内的 .venv 虚拟环境，没有就用 uv（优先）或 python3 -m venv 创建
#    2. 安装 requirements.txt 里的依赖（已装则跳过）
#    3. 几秒后自动打开浏览器 http://127.0.0.1:8790（无图形会话时自动跳过）
#    4. 前台运行 python -m slab serve（Ctrl+C = 停止控制台及其启动的 SnowLuma / AstrBot）
#
#  用法：
#    bash 一键启动.sh          # 或者 chmod +x 一键启动.sh && ./一键启动.sh
#    SLAB_NO_BROWSER=1 bash 一键启动.sh    # 不自动开浏览器（无头/远程排错时用）
#
#  为什么不用 `set -e` 之外的容错：这个脚本是给普通用户双击/粘贴运行的第一入口，
#  一旦静默失败用户只会看到窗口一闪而过，所以每一步失败都停下来并打印中文原因 + 怎么办。
# =============================================================================
set -euo pipefail

HERE=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
cd "$HERE"

PY_BIN="$HERE/.venv/bin/python"
CONSOLE_URL="http://127.0.0.1:8790"
BROWSER_DELAY="${SLAB_BROWSER_DELAY:-5}"

info()  { printf '[%s] %s\n' "$1" "$2"; }
fail()  { printf '\n[错误] %s\n\n' "$1" >&2; exit 1; }

printf '============================================================\n'
printf '  SnowLuma x AstrBot 联合控制台 - 一键启动（Linux/macOS）\n'
printf '============================================================\n\n'

# ---------------------------------------------------------------------------
# 1) 虚拟环境
# ---------------------------------------------------------------------------
if [ -x "$PY_BIN" ]; then
  info "1/3" "虚拟环境就绪：$PY_BIN"
else
  info "1/3" "正在创建虚拟环境 .venv（首次运行需要 1-2 分钟）…"

  if ! command -v python3 >/dev/null 2>&1; then
    fail "没有检测到 python3。
       请先安装 Python 3.12 或更高版本：
         Ubuntu/Debian : sudo apt install -y python3 python3-venv
         Fedora        : sudo dnf install -y python3
         Arch          : sudo pacman -S python
       （Debian/Ubuntu 必须装 python3-venv，否则 python3 -m venv 会失败）"
  fi

  PY_VER=$(python3 -c 'import sys; print("%d.%d" % sys.version_info[:2])')
  python3 -c 'import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)' \
    || fail "Python 版本太低（检测到 $PY_VER，需要 3.10+）。请升级后重试。"

  if command -v uv >/dev/null 2>&1; then
    info " " "使用 uv 创建（更快）…"
    uv venv --python 3.12 .venv || fail "uv venv 创建虚拟环境失败。可改用：python3 -m venv .venv"
  else
    info " " "没有检测到 uv，改用 Python 自带的 venv…"
    python3 -m venv .venv \
      || fail "python3 -m venv 失败。
       最常见原因：没装 python3-venv（Ubuntu/Debian）：
         sudo apt install -y python3-venv
       或者虚拟环境目录损坏 —— 删掉 .venv 后重试。"
  fi

  [ -x "$PY_BIN" ] || fail "虚拟环境创建完成但找不到 $PY_BIN。请删除 .venv 后重试。"
  info "1/3" "虚拟环境就绪：$PY_BIN"
fi

# ---------------------------------------------------------------------------
# 2) 依赖
# ---------------------------------------------------------------------------
info "2/3" "检查控制台依赖（fastapi / uvicorn / websockets …）…"
if "$PY_BIN" -c 'import fastapi, uvicorn, httpx, websockets' >/dev/null 2>&1; then
  info "2/3" "依赖就绪。"
else
  info " " "正在安装 requirements.txt 中的依赖（首次约 1-2 分钟）…"
  if command -v uv >/dev/null 2>&1; then
    uv pip install --python "$PY_BIN" -r requirements.txt \
      || fail "依赖安装失败。常见原因：
       1) 网络无法访问 PyPI —— 可先执行：export https_proxy=http://127.0.0.1:7890 再重跑
       2) 虚拟环境损坏 —— 删除 .venv 文件夹后重新运行本脚本"
  else
    "$PY_BIN" -m pip --version >/dev/null 2>&1 || "$PY_BIN" -m ensurepip --upgrade >/dev/null 2>&1 || true
    "$PY_BIN" -m pip install -r requirements.txt \
      || fail "依赖安装失败。常见原因：
       1) 网络无法访问 PyPI —— 可先执行：export https_proxy=http://127.0.0.1:7890 再重跑
       2) 虚拟环境损坏 —— 删除 .venv 文件夹后重新运行本脚本"
  fi
  info "2/3" "依赖就绪。"
fi

# ---------------------------------------------------------------------------
# 3) 启动控制台（+ 延迟开浏览器）
# ---------------------------------------------------------------------------
info "3/3" "正在启动控制台…"
echo "      浏览器会在 $BROWSER_DELAY 秒后自动打开；若没有打开，请手动访问 $CONSOLE_URL"
echo "      按 Ctrl+C = 停止控制台及其启动的 SnowLuma / AstrBot。"
echo

open_browser() {
  # 无图形会话（纯 SSH / 容器）时开了也没用，直接说清楚
  if [ -z "${DISPLAY:-}" ] && [ -z "${WAYLAND_DISPLAY:-}" ] && [ "$(uname -s)" != "Darwin" ]; then
    echo "[提示] 当前没有图形会话（DISPLAY/WAYLAND_DISPLAY 都为空），跳过自动开浏览器。"
    echo "       远程使用请做端口转发：ssh -L 8790:127.0.0.1:8790 用户@主机"
    return 0
  fi
  if command -v xdg-open >/dev/null 2>&1; then
    xdg-open "$CONSOLE_URL" >/dev/null 2>&1 || true
  elif command -v open >/dev/null 2>&1; then
    open "$CONSOLE_URL" >/dev/null 2>&1 || true   # macOS
  else
    echo "[提示] 没找到 xdg-open，请手动打开 $CONSOLE_URL"
  fi
}

# 后台等几秒再开浏览器：服务还没起来就打开只会看到 502/连接被拒绝。
# 用子 shell 而不是 setsid：脚本随后 exec 成 python，子 shell 会被 init 收养并正常执行完。
if [ "${SLAB_NO_BROWSER:-0}" = "1" ]; then
  echo "      （SLAB_NO_BROWSER=1，不自动打开浏览器）"
else
  ( sleep "$BROWSER_DELAY"; open_browser ) &
fi

# 前台运行：Ctrl+C 直接给到 python（uvicorn 自己会优雅停掉子进程），
# 用 exec 而不是子 shell，信号不会被 bash 吞掉——这是"关掉终端还残留进程"的常见成因。
exec "$PY_BIN" -m slab serve "$@"
