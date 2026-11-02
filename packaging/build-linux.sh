#!/usr/bin/env bash
# SnowLuma × AstrBot 联合控制台 —— Linux 打包脚本
#
# 产物：
#   dist/SnowLuma-AstrBot-Console-v<版本>-linux-<arch>.tar.gz   ← 分发/首次安装
#   dist/SnowLuma-AstrBot-Console-v<版本>-linux-<arch>.zip      ← 应用内「升级」只认 .zip
#       （内容相同：onedir 产物 + VERSION + start.sh + README + docs/LINUX.md（若有））
#
# 与 Windows 版 packaging/build-exe.ps1 一一对应，差异只在「平台产物格式」：
#   Windows → 使用说明.txt + zip；Linux → start.sh + tar.gz。
#
# 为什么 onedir 而不是 onefile：
#   1. onefile 每次启动都要把整包解到 /tmp/_MEIxxxx，冷启动多 2-3 秒；
#   2. 很多发行版/容器把 /tmp 挂成 noexec（或 systemd PrivateTmp），解出来的
#      bootloader 根本没有执行权限 → 启动即失败，且报错信息极其难懂；
#   3. onedir 直接跑目录里的可执行文件，日志/替换库/排查都直观。
#
# 用法：
#   bash packaging/build-linux.sh                 # 装依赖 → 打包 → 冒烟
#   bash packaging/build-linux.sh --skip-install  # 不装 PyInstaller（离线或已装）
#   bash packaging/build-linux.sh --skip-smoke    # 只打包，不跑冒烟
#
# 冒烟做什么：真的把打包产物跑起来 → 请求 /api/health → SIGTERM 优雅退出 →
# 检查「进程真的没了、端口真的放开了」。这一步能挡住 90% 的"打出来能下载但跑不起来"。
set -euo pipefail

HERE=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
ROOT=$(cd -- "$HERE/.." && pwd)
APP_NAME="SnowLuma-AstrBot-Console"
VERSION=$(tr -d '[:space:]' < "$ROOT/VERSION")
DIST="$ROOT/dist"
BUILD="$ROOT/build"
STAGE="$DIST/$APP_NAME"
BIN="$STAGE/$APP_NAME"

SKIP_INSTALL=0
SKIP_SMOKE=0
for arg in "$@"; do
  case "$arg" in
    --skip-install) SKIP_INSTALL=1 ;;
    --skip-smoke) SKIP_SMOKE=1 ;;
    -h|--help) sed -n '1,40p' "${BASH_SOURCE[0]}"; exit 0 ;;
    *) echo "未知参数：$arg（可用：--skip-install / --skip-smoke）" >&2; exit 2 ;;
  esac
done

log() { printf '== %s\n' "$*"; }
die() { printf '\n[错误] %s\n' "$*" >&2; exit 1; }

# --------------------------------------------------------------------------- #
# 0) 解释器
# --------------------------------------------------------------------------- #
if [ -x "$ROOT/.venv/bin/python" ]; then
  PY="$ROOT/.venv/bin/python"
else
  PY=$(command -v python3 || true)
fi
[ -n "${PY:-}" ] || die "找不到 python3。请先安装 Python 3.10+（Ubuntu：sudo apt install -y python3 python3-venv）"

log "项目目录: $ROOT"
log "Python: $PY ($("$PY" -V 2>&1))"
"$PY" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)' \
  || die "Python 版本太低（需要 3.10+），当前：$("$PY" -V 2>&1)"

case "$(uname -m)" in
  x86_64|amd64) ARCH=x64 ;;
  aarch64|arm64) ARCH=arm64 ;;
  *) ARCH=$(uname -m) ;;
esac
TARBALL="$DIST/${APP_NAME}-v${VERSION}-linux-${ARCH}.tar.gz"

# --------------------------------------------------------------------------- #
# 1) PyInstaller
# --------------------------------------------------------------------------- #
if ! "$PY" -m PyInstaller --version >/dev/null 2>&1; then
  if [ "$SKIP_INSTALL" = "1" ]; then
    die "没有 PyInstaller（--skip-install 已指定，不会自动安装）。请先：$PY -m pip install pyinstaller"
  fi
  log "安装 PyInstaller（首次约 20-60 秒）"
  # 系统 Python 在 Debian/Ubuntu 24.04 上是 PEP 668 externally-managed，直接 pip install 会被拒。
  # 所以优先用项目 .venv；没有 venv 就带上 --user 再退一步，最后才提示建 venv。
  if [ -n "${VIRTUAL_ENV:-}" ] || [ -x "$ROOT/.venv/bin/python" ]; then
    "$PY" -m pip install -q --upgrade pyinstaller \
      || die "PyInstaller 安装失败（离线环境可先手动装好再加 --skip-install）"
  elif "$PY" -m pip install -q --upgrade --user pyinstaller 2>/dev/null; then
    : # --user 装成功（脚本内可能仍需 PYTHONPATH，下面统一校验）
  else
    die "系统 Python 不允许 pip 安装（PEP 668）。请先建虚拟环境：
       $PY -m venv .venv && .venv/bin/pip install -r requirements.txt pyinstaller"
  fi
fi
"$PY" -m PyInstaller --version >/dev/null 2>&1 || die "PyInstaller 仍不可用，请检查环境"

# --------------------------------------------------------------------------- #
# 2) PyInstaller 打包（onedir）
# --------------------------------------------------------------------------- #
log "PyInstaller 打包 v$VERSION（首次 1-3 分钟）"
# 强制 onedir：onefile 在 Linux 上会踩 noexec /tmp（见文件头注释）
unset SLAB_EXE_ONEFILE || true
cd "$ROOT"
"$PY" -m PyInstaller --clean --noconfirm \
  --workpath "$BUILD" --distpath "$DIST" "$HERE/console.spec"

[ -f "$BIN" ] || die "打包产物缺失: $BIN（请检查上面的 PyInstaller 输出）"
chmod +x "$BIN"

# --------------------------------------------------------------------------- #
# 3) 补齐"给人看"和"给人用"的东西
# --------------------------------------------------------------------------- #
log "整理发布目录"

# VERSION 由 console.spec 的 datas 带进来了，这里再兜一次（缺失时补）
[ -f "$STAGE/VERSION" ] || cp "$ROOT/VERSION" "$STAGE/VERSION"

# 启动器：用户解压后只需要 ./start.sh
cat > "$STAGE/start.sh" <<'LAUNCHER'
#!/usr/bin/env bash
# 控制台启动器（打包版）：不需要 Python，直接跑同目录的 onedir 产物。
# 数据目录默认走 XDG（~/.local/share/SnowLumaAstrBot），要改就设 SLAB_STORAGE_ROOT。
set -euo pipefail
HERE=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
exec "$HERE/SnowLuma-AstrBot-Console" serve "$@"
LAUNCHER
chmod +x "$STAGE/start.sh"

cp "$ROOT/README.md" "$STAGE/README.md"
# docs/LINUX.md 由文档同事产出，没有就不带（不能让"缺文件"把打包搞挂）
if [ -f "$ROOT/docs/LINUX.md" ]; then
  mkdir -p "$STAGE/docs"
  cp "$ROOT/docs/LINUX.md" "$STAGE/docs/LINUX.md"
fi

cat > "$STAGE/使用说明.txt" <<TXT
SnowLuma x AstrBot 联合控制台 v$VERSION（Linux ${ARCH} 版）
==========================================================
1. 两个压缩包内容完全一样，只是用途不同（都是本目录这一份）：
     *.tar.gz → 分发 / 首次部署 / 手动整目录替换（推荐，能保留可执行位）
     *.zip    → 控制台「升级」页选它（应用内升级只认 .zip）
2. 先解压，再进目录运行（不要直接在压缩包里点）：
     tar -xzf ${APP_NAME}-v${VERSION}-linux-${ARCH}.tar.gz
     cd ${APP_NAME}
3. 启动：
     ./start.sh
   （等价于 ./${APP_NAME} serve；浏览器打开 http://127.0.0.1:8790）
   若提示 Permission denied（部分解压工具会丢可执行位）：
     chmod +x start.sh ${APP_NAME}
4. 数据默认在 ~/.local/share/SnowLumaAstrBot/{data,runtime,downloads}，
   要换位置就设 SLAB_STORAGE_ROOT=/your/path 再启动。
5. 本包含完整 Python 运行时，**不需要**装 Python；也不需要 root。
6. 排查与手册：docs/LINUX.md（Linux 专属，若随包）、README.md；
   控制台内置的 MANUAL.md / UPGRADE.md 在 _internal/docs/ 下（PyInstaller 的资源目录）。
7. 扫码登录 QQ 需要图形会话（X11/Wayland）；无头服务器请用 Xvfb + VNC。
TXT

# --------------------------------------------------------------------------- #
# 4) 冒烟：真的起一次服务
# --------------------------------------------------------------------------- #
_health_ok() {
  # 用 python 而不是 curl：最小发行版/容器里经常没有 curl
  "$PY" - "$1" <<'PY'
import json, sys, urllib.request
port = int(sys.argv[1])
try:
    with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/health", timeout=1) as resp:
        payload = json.loads(resp.read().decode("utf-8"))
except Exception:
    sys.exit(1)
sys.exit(0 if payload.get("ok") else 1)
PY
}

_port_listening() {
  # 判断"还有没有人在这个端口上 LISTEN"，读 /proc/net/tcp{,6}（state 0A），
  # 而不是尝试 bind()：健康检查请求会在服务端留下 TIME_WAIT 连接，此时 bind()
  # 会 EADDRINUSE，但端口其实已经没人监听了——用 bind 判会误报失败。
  "$PY" - "$1" <<'PY'
import sys

port = int(sys.argv[1])
wanted = f"{port:04X}"
for name in ("/proc/net/tcp", "/proc/net/tcp6"):
    try:
        with open(name, "r", encoding="utf-8", errors="replace") as fh:
            next(fh, None)                      # 表头
            for line in fh:
                parts = line.split()
                if len(parts) < 4 or parts[3] != "0A":
                    continue
                if parts[1].rsplit(":", 1)[1].upper() == wanted:
                    sys.exit(0)                 # 还在监听
    except OSError:
        continue
sys.exit(1)                                     # 没人监听了
PY
}

smoke() {
  local tmp port pid alive
  tmp=$(mktemp -d "${TMPDIR:-/tmp}/slab-smoke.XXXXXX")
  port=$("$PY" -c 'import socket; s=socket.socket(); s.bind(("127.0.0.1",0)); print(s.getsockname()[1]); s.close()')
  log "冒烟：起服务（端口 $port，数据目录 $tmp）"

  # 数据/运行时/下载全部指到临时目录：冒烟不许污染仓库里的 data/，也不许动真用户的配置
  SLAB_DATA_DIR="$tmp/data" \
  SLAB_RUNTIME="$tmp/runtime" \
  SLAB_DOWNLOAD_DIR="$tmp/downloads" \
    "$BIN" serve --port "$port" --no-browser >"$tmp/serve.log" 2>&1 &
  pid=$!

  for _ in $(seq 1 160); do          # 最多等 40 秒（首启动要做自检/清理，慢机器够用）
    if _health_ok "$port"; then break; fi
    if ! kill -0 "$pid" 2>/dev/null; then
      tail -n 40 "$tmp/serve.log" || true
      die "冒烟失败：后端进程启动后就退出了（日志见上，完整日志：$tmp/serve.log）"
    fi
    sleep 0.25
  done
  if ! _health_ok "$port"; then
    tail -n 40 "$tmp/serve.log" || true
    kill -TERM "$pid" 2>/dev/null || true
    die "冒烟失败：40 秒内 /api/health 没就绪（完整日志：$tmp/serve.log）"
  fi
  log "  /api/health 就绪 ✓"

  # SIGTERM：后端要能优雅退出（关掉 uvicorn、落盘配置）
  kill -TERM "$pid" 2>/dev/null || true
  alive=1
  for _ in $(seq 1 40); do          # 最多 10 秒
    if ! kill -0 "$pid" 2>/dev/null; then alive=0; break; fi
    sleep 0.25
  done
  if [ "$alive" = "1" ]; then
    tail -n 20 "$tmp/serve.log" || true
    kill -KILL "$pid" 2>/dev/null || true
    die "冒烟失败：SIGTERM 后 10 秒仍未退出（后端没有正确处理信号，桌面壳会退不掉）"
  fi
  wait "$pid" 2>/dev/null || true     # 收尸，避免 <defunct>
  log "  SIGTERM 优雅退出 ✓"

  # 残留检查 1：不许有同名进程还在（模板里那句 "启动即孤儿" 的老毛病）
  if pgrep -f -- "$BIN serve" >/dev/null 2>&1; then
    pgrep -af -- "$BIN serve" || true
    die "冒烟失败：退出后仍有残留进程"
  fi
  # 残留检查 2：端口必须真的没人监听，否则用户下次启动会撞"端口被占用"
  for _ in $(seq 1 20); do
    if ! _port_listening "$port"; then break; fi
    sleep 0.25
  done
  if _port_listening "$port"; then
    die "冒烟失败：进程已经退出，但端口 $port 仍有进程在监听（有残留/守护进程）"
  fi
  log "  无残留进程 / 端口已释放 ✓"

  rm -rf "$tmp"
}

if [ "$SKIP_SMOKE" = "1" ]; then
  log "跳过冒烟（--skip-smoke）"
else
  smoke
fi

# --------------------------------------------------------------------------- #
# 5) 打包：tar.gz（分发用）+ zip（应用内「升级」用）
# --------------------------------------------------------------------------- #
mkdir -p "$DIST"
log "打包 tar.gz（约 30 秒，压缩等级默认）"
rm -f "$TARBALL"
tar -czf "$TARBALL" -C "$DIST" "$APP_NAME"

# 为什么还要出一个 .zip：
#   控制台的「升级」功能（slab/upgrade.py）只认 .zip 升级包（inspect_zip 里写死了
#   suffix != ".zip" 就拒绝），而 Linux 的分发习惯是 tar.gz。只发 tar.gz 的话
#   Linux 用户在界面里选升级包会被判"下错包"。所以同一份目录再打一个 zip。
#   用 python 的 zipfile 而不是 zip 命令：构建机上不保证装了 zip，python 一定有；
#   顺便把 st_mode 写进 external_attr，解压后入口仍有可执行位（unzip 会照抄）。
ZIP="$DIST/${APP_NAME}-v${VERSION}-linux-${ARCH}.zip"
rm -f "$ZIP"
"$PY" - "$STAGE" "$ZIP" "$APP_NAME" <<'PY'
import os
import sys
import zipfile
from pathlib import Path

stage, out, root = Path(sys.argv[1]), Path(sys.argv[2]), sys.argv[3]
count = links = 0
with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as zf:
    for path in sorted(stage.rglob("*")):
        if path.is_dir() and not path.is_symlink():
            continue
        arc = str(Path(root) / path.relative_to(stage))
        info = zipfile.ZipInfo(arc)
        if path.is_symlink():
            # 软链按 zip 的 Unix 约定写（S_IFLNK + 链接目标），unzip 会原样还原
            info.create_system = 3
            info.external_attr = 0o120777 << 16
            zf.writestr(info, os.readlink(path))
            links += 1
            continue
        info.create_system = 3
        info.compress_type = zipfile.ZIP_DEFLATED
        # 用文件真实权限位（0o100000 = 普通文件），别用 os.access(X_OK)：root 下它恒为真，
        # 会把 VERSION / README 这类普通文件也标成 755
        mode = path.stat().st_mode & 0o777
        info.external_attr = ((mode or 0o644) | 0o100000) << 16
        with open(path, "rb") as fh:
            zf.writestr(info, fh.read())
        count += 1
print(f"   zip 条目数: {count}（软链 {links} 个，共 {out.stat().st_size / 1048576:.1f} MB）")
PY

total=$(du -sh "$STAGE" | cut -f1)
size=$(du -h "$TARBALL" | cut -f1)
zsize=$(du -h "$ZIP" | cut -f1)
log "产物目录: $STAGE（$total）"
log "分发压缩包: $TARBALL（$size）"
log "升级包    : $ZIP（$zsize，应用内「升级」选它）"
log "完成 ✓  解压后执行 ./start.sh 即可"
