#!/usr/bin/env bash
# SnowLuma × AstrBot 联合控制台 —— Linux 桌面壳打包脚本
#
# 产物：
#   desktop/dist/SnowLuma-Desktop-v<版本>-linux-<arch>.tar.gz   ← 分发/首次安装
#   desktop/dist/SnowLuma-Desktop-v<版本>-linux-<arch>.zip      ← 应用内「升级」只认 .zip
#       （Electron 运行时 + resources/app（外壳代码）+ resources/backend（PyInstaller 后端）
#         + SnowLuma-Desktop.sh 启动器 + SnowLuma-Desktop.desktop 桌面项 + 使用说明.txt）
#
# 和 Windows 的 desktop/build.ps1 是同一套思路：**不用 electron-builder**，纯手工组装，
# 因为 electron-builder 要联网下载平台工具链、还会改写我们的目录结构。
# 这里只是把 Compress-Archive 换成 tar、把 .exe 换成无扩展名、再补一个 .desktop。
#
# 前置条件：
#   1. desktop/node_modules/electron/dist 已存在（一次性 `cd desktop && npm install`，
#      需要网络，只做一次；之后打包完全离线）
#   2. 已跑过 packaging/build-linux.sh，存在 dist/SnowLuma-AstrBot-Console/ 后端产物
#
# 用法：
#   bash desktop/build-linux.sh          # 组装 + 校验 + 打 tar.gz
#   bash desktop/build-linux.sh --check  # 只做静态校验（node --check / 依赖检查）
set -euo pipefail

HERE=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
ROOT=$(cd -- "$HERE/.." && pwd)
PROJECT_NAME="SnowLuma-Desktop"
VERSION=$(tr -d '[:space:]' < "$ROOT/VERSION")
# 打 zip 用（控制台的「升级」只认 .zip）：优先用项目 venv 里的 python，其次系统 python3
if [ -x "$ROOT/.venv/bin/python" ]; then PY="$ROOT/.venv/bin/python"; else PY=$(command -v python3 || true); fi
ELECTRON_DIST="$HERE/node_modules/electron/dist"
BACKEND_SRC="$ROOT/dist/SnowLuma-AstrBot-Console"
OUT="$HERE/dist"
APP_DIR="$OUT/$PROJECT_NAME"

CHECK_ONLY=0
for arg in "$@"; do
  case "$arg" in
    --check) CHECK_ONLY=1 ;;
    -h|--help) sed -n '1,30p' "${BASH_SOURCE[0]}"; exit 0 ;;
    *) echo "未知参数：$arg（可用：--check）" >&2; exit 2 ;;
  esac
done

case "$(uname -m)" in
  x86_64|amd64) ARCH=x64 ;;
  aarch64|arm64) ARCH=arm64 ;;
  *) ARCH=$(uname -m) ;;
esac
TARBALL="$OUT/${PROJECT_NAME}-v${VERSION}-linux-${ARCH}.tar.gz"

log() { printf '== %s\n' "$*"; }
die() { printf '\n[错误] %s\n' "$*" >&2; exit 1; }

# --------------------------------------------------------------------------- #
# 1) 环境检查
# --------------------------------------------------------------------------- #
log "检查环境"
if ! command -v node >/dev/null 2>&1; then
  die "找不到 node。请安装 Node.js 20+（Ubuntu：sudo apt install -y nodejs npm，或用 nvm）"
fi

# JS 语法校验放最前面：桌面壳是纯手写 JS，语法错在 Electron 里报得很难懂，这里先挡一道
# （也不需要 180 MB 的 Electron 就能跑，方便 --check 在任意机器上做自检）
for js in main.js preload.js; do
  node --check "$HERE/$js" || die "$js 语法错误"
  log "  node --check $js ✓"
done

if [ "$CHECK_ONLY" = "1" ]; then
  log "静态校验通过（--check 到此为止）"
  exit 0
fi

[ -x "$ELECTRON_DIST/electron" ] \
  || die "Electron 未安装完整：$ELECTRON_DIST
       先执行一次（需要联网，之后打包可离线）：
         cd desktop && npm install --no-audit --no-fund"
[ -d "$BACKEND_SRC" ] \
  || die "找不到后端产物：$BACKEND_SRC
       先执行：bash packaging/build-linux.sh"
[ -x "$BACKEND_SRC/SnowLuma-AstrBot-Console" ] \
  || die "后端目录里没有可执行文件 SnowLuma-AstrBot-Console：$BACKEND_SRC"

# --------------------------------------------------------------------------- #
# 2) 组装：Electron 运行时 + resources/app + resources/backend
# --------------------------------------------------------------------------- #
log "组装 $PROJECT_NAME（v$VERSION / linux-$ARCH）"
rm -rf "$APP_DIR"
mkdir -p "$APP_DIR"

# 2.1) Electron 运行时（整份 dist 拷过来，再把可执行文件改名，
#      这样 Chromium 的进程名/任务栏分组就是我们自己的名字）
cp -a "$ELECTRON_DIST/." "$APP_DIR/"
mv "$APP_DIR/electron" "$APP_DIR/$PROJECT_NAME"
chmod +x "$APP_DIR/$PROJECT_NAME"

# 2.2) 外壳代码 → resources/app
res_app="$APP_DIR/resources/app"
mkdir -p "$res_app/assets"
for f in main.js preload.js splash.html package.json; do
  cp "$HERE/$f" "$res_app/$f"
done
cp "$HERE/assets/"* "$res_app/assets/"

# 2.3) Python 后端 → resources/backend（去掉运行时产生的 data/runtime，
#      否则会把开发机的配置与密钥打进发布包）
res_backend="$APP_DIR/resources/backend"
mkdir -p "$res_backend"
cp -a "$BACKEND_SRC/." "$res_backend/"
rm -rf "$res_backend/data" "$res_backend/runtime"
chmod +x "$res_backend/SnowLuma-AstrBot-Console"

# 2.4) 版本号文件：Electron 的 dist 自带一个叫 `version` 的文件（内容是 Electron 版本，
#      形如 33.4.11）。控制台的升级检查是按"压缩包里最浅的 VERSION"判新版本的
#      （slab/upgrade.py::read_zip_version，文件名大小写不敏感），这个文件会冒充成
#      "新版本 33.4.11"，界面上显示的版本号就是错的。Electron 运行时并不读它，
#      改名成 electron-version，并放一份我们自己的 VERSION。
if [ -f "$APP_DIR/version" ]; then
  mv "$APP_DIR/version" "$APP_DIR/electron-version"
fi
cp "$ROOT/VERSION" "$APP_DIR/VERSION"

# 2.5) 启动器
#   Electron 的 Chromium 沙箱要求 chrome-sandbox 是 root:root + setuid(4755)。
#   tar.gz 解压出来的文件属主是当前用户、也没有 setuid，于是在 Ubuntu 24.04 上直接
#   ./SnowLuma-Desktop 会报 "The SUID sandbox helper binary was found, but is not
#   configured correctly"。两条路：让用户 sudo chown/chmod（一次性），或者加
#   --no-sandbox（安全性略降但能跑）。这里自动判断：能跑沙箱就不加参数。
cat > "$APP_DIR/${PROJECT_NAME}.sh" <<'LAUNCHER'
#!/usr/bin/env bash
# 桌面壳启动器（打包版）
#   - 正常情况：直接启动，保留 Chromium 沙箱
#   - chrome-sandbox 不是 root:root 4755（tar.gz 解压后的常态）时退化为 --no-sandbox，
#     否则 Electron 会直接拒绝启动。想恢复沙箱：
#       sudo chown root:root chrome-sandbox && sudo chmod 4755 chrome-sandbox
set -euo pipefail
HERE=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
SANDBOX="$HERE/chrome-sandbox"
if [ -e "$SANDBOX" ]; then
  owner=$(stat -c '%u' "$SANDBOX" 2>/dev/null || echo "?")
  if [ "$owner" != "0" ] || [ ! -u "$SANDBOX" ]; then
    exec "$HERE/SnowLuma-Desktop" --no-sandbox "$@"
  fi
fi
exec "$HERE/SnowLuma-Desktop" "$@"
LAUNCHER
chmod +x "$APP_DIR/${PROJECT_NAME}.sh"

# 2.6) 使用说明
cat > "$APP_DIR/使用说明.txt" <<TXT
SnowLuma x AstrBot 联合控制台 v$VERSION（Linux 桌面版）
======================================================
1. 两个压缩包内容完全一样，用途不同：
     *.tar.gz → 分发 / 首次安装（推荐，能保留可执行位）
     *.zip    → 控制台「升级」页选它（应用内升级只认 .zip）
2. 解压后进目录运行（别在压缩包里直接双击）：
     ./${PROJECT_NAME}.sh
   若提示 Permission denied（部分解压工具会丢可执行位）：
     chmod +x ${PROJECT_NAME}.sh ${PROJECT_NAME}
3. 关闭窗口 = 停止控制台（长按右上角电源键 ⏻ 约 1 秒 → 彻底退出）
4. 数据默认在 ~/.local/share/SnowLumaAstrBot/{data,runtime,downloads}，
   要换位置：export SLAB_STORAGE_ROOT=/your/path 再启动。
5. 本包含 Electron 与 Python 运行时，**不需要**装 Python / Node。
6. 想加进应用菜单（KDE/GNOME）：
     ./install-desktop-entry.sh
   也可以手动把 ${PROJECT_NAME}.desktop 拷到 ~/.local/share/applications/
   并改掉里面的 Exec/Icon 绝对路径。
7. 首次启动若提示 SUID sandbox 相关错误，按启动器注释处理（或直接用启动器，它会自动加
   --no-sandbox）。完整说明：docs/LINUX.md（若随包发布）、README.md。
TXT

# 2.7) .desktop 桌面项（Exec/Icon 默认按"装到 /opt"写，
#      install-desktop-entry.sh 会按实际解压路径改写后装到用户目录）
cat > "$APP_DIR/${PROJECT_NAME}.desktop" <<DESKTOP
[Desktop Entry]
Type=Application
Version=1.0
Name=SnowLuma × AstrBot 联合控制台
Name[en]=SnowLuma × AstrBot Console
GenericName=Chatbot 部署控制台
Comment=一键部署 SnowLuma + AstrBot 的可视化控制台
Exec=/opt/${PROJECT_NAME}/${PROJECT_NAME}.sh %U
Icon=/opt/${PROJECT_NAME}/resources/app/assets/icon.png
Terminal=false
Categories=Development;Network;
Keywords=SnowLuma;AstrBot;QQ;OneBot;chatbot;
StartupWMClass=${PROJECT_NAME}
DESKTOP

cat > "$APP_DIR/install-desktop-entry.sh" <<'INSTALLER'
#!/usr/bin/env bash
# 把 .desktop 里的 /opt 路径改成本目录的实际路径，安装到用户应用菜单。
# 好处：解压到哪都能用，不需要 root（装到 ~/.local/share/applications）。
set -euo pipefail
HERE=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
TARGET_DIR="${HOME}/.local/share/applications"
SRC="$HERE/SnowLuma-Desktop.desktop"
[ -f "$SRC" ] || { echo "缺少 $SRC" >&2; exit 1; }
mkdir -p "$TARGET_DIR"
sed -e "s#/opt/SnowLuma-Desktop#${HERE}#g" "$SRC" > "$TARGET_DIR/SnowLuma-Desktop.desktop"
chmod +x "$TARGET_DIR/SnowLuma-Desktop.desktop"
# 更新菜单缓存（没有 update-desktop-database 的机器忽略即可，多数桌面会自动发现）
update-desktop-database "$TARGET_DIR" 2>/dev/null || true
echo "已安装：$TARGET_DIR/SnowLuma-Desktop.desktop"
echo "在应用菜单里搜索「SnowLuma」即可（部分桌面需要注销/重登录）"
INSTALLER
chmod +x "$APP_DIR/install-desktop-entry.sh"

# --------------------------------------------------------------------------- #
# 3) 打包：tar.gz（分发用）+ zip（应用内「升级」用）
# --------------------------------------------------------------------------- #
mkdir -p "$OUT"
log "打包 tar.gz（约 1 分钟）"
rm -f "$TARBALL"
tar -czf "$TARBALL" -C "$OUT" "$PROJECT_NAME"

# 与控制台后端包同理：slab/upgrade.py 的 inspect_zip 只接受 .zip，
# 桌面版也要给用户一个能直接在界面里选的升级包。
# 桌面版升级包里会有两个 basename 命中 ENTRY_HINTS（壳 + resources/backend 里的后端），
# 控制台那边已按「路径最浅优先」取胜，壳在最外层，所以命中的就是壳。
[ -n "${PY:-}" ] || die "打 zip 需要 python3（找 RUNTIME 依赖时空了）"
ZIP_TARGET="$OUT/${PROJECT_NAME}-v${VERSION}-linux-${ARCH}.zip"
rm -f "$ZIP_TARGET"
"$PY" - "$APP_DIR" "$ZIP_TARGET" "$PROJECT_NAME" <<'PY'
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
            # 符号链接按 zip 的 Unix 约定写（S_IFLNK + 链接目标），unzip 会原样还原；
            # 若当成普通文件写，会把目标内容复制一份，包体积翻倍还容易写坏。
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

total=$(du -sh "$APP_DIR" | cut -f1)
size=$(du -h "$TARBALL" | cut -f1)
zsize=$(du -h "$ZIP_TARGET" | cut -f1)
log "产物目录    : $APP_DIR（$total）"
log "分发压缩包  : $TARBALL（$size）"
log "升级包(zip) : $ZIP_TARGET（$zsize）"
log "完成 ✓  解压后执行 ./${PROJECT_NAME}.sh"
log "提示：GUI 冒烟测试需要图形会话，手动跑：./${PROJECT_NAME}.sh --smoke-test"
