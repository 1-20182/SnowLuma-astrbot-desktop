#!/usr/bin/env bash
# 语义化版本号提升（Linux/macOS 版，等价于 scripts/bump.ps1）
#
#   bash scripts/bump.sh patch|minor|major|x.y.z "更新说明"
#
# 会同步三处：VERSION、desktop/package.json 的 version、CHANGELOG.md 顶部条目。
#
# 为什么用一段 python3 而不是 sed/awk：
#   * desktop/package.json 是 JSON，用 sed 正则替换容易误伤其它 "version" 字段；
#   * VERSION / CHANGELOG 都要求 UTF-8 且不带 BOM，python 的显式 encoding 最稳；
#   * python3 本来就是本项目的硬依赖（一键启动.sh 会建 venv），不算引入新工具。
set -euo pipefail

ROOT=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)
KIND="${1:-}"
NOTE="${2:-}"

usage() {
  cat >&2 <<'USAGE'
用法：bash scripts/bump.sh patch|minor|major|x.y.z ["更新说明"]
  例：bash scripts/bump.sh patch "修复 Linux 桌面壳关窗后后端残留"
USAGE
}

if [ -z "$KIND" ]; then
  usage
  exit 2
fi

command -v python3 >/dev/null 2>&1 || {
  echo "[错误] 找不到 python3（本脚本用它安全地改 VERSION / JSON / CHANGELOG）" >&2
  exit 1
}

python3 - "$ROOT" "$KIND" "$NOTE" <<'PY'
from __future__ import annotations

import datetime
import json
import re
import sys
from pathlib import Path

root = Path(sys.argv[1])
kind = sys.argv[2].strip()
note = sys.argv[3].strip()

version_file = root / "VERSION"
changelog = root / "CHANGELOG.md"
pkg_file = root / "desktop" / "package.json"

current = version_file.read_text("utf-8").strip()
m = re.fullmatch(r"(\d+)\.(\d+)\.(\d+)", current)
if not m:
    raise SystemExit(f"VERSION 不是 x.y.z：{current}")
major, minor, patch = (int(m.group(i)) for i in (1, 2, 3))

if re.fullmatch(r"\d+\.\d+\.\d+", kind):
    nxt = kind
elif kind == "major":
    nxt = f"{major + 1}.0.0"
elif kind == "minor":
    nxt = f"{major}.{minor + 1}.0"
elif kind == "patch":
    nxt = f"{major}.{minor}.{patch + 1}"
else:
    raise SystemExit("用法：bump.sh patch|minor|major|x.y.z [说明]")

version_file.write_text(nxt + "\n", encoding="utf-8")

# desktop/package.json：只改顶层 version（用 JSON 解析再写回太容易重排格式，
# 这里用定点正则 + 断言只命中一次，保留原文件排版）
text = pkg_file.read_text("utf-8")
new_text, count = re.subn(r'("version"\s*:\s*")[^"]*(")', rf"\g<1>{nxt}\g<2>", text, count=1)
if count != 1:
    raise SystemExit(f"没能在 {pkg_file} 里找到 version 字段")
json.loads(new_text)  # 改完必须仍是合法 JSON，否则宁可失败也不要写坏文件
pkg_file.write_text(new_text, encoding="utf-8")

if changelog.is_file():
    body = changelog.read_text("utf-8")
    entry = f"## [{nxt}] - {datetime.date.today().isoformat()}\n\n### 变更\n- {note}\n\n"
    # 插在"第一条版本标题"之前：CHANGELOG 是倒序（最新在最上面），
    # 旧实现用固定的 "## [1.1.0]" 当锚点，新版本一旦多起来就会把条目塞进历史版本中间。
    match = re.search(r"^## \[", body, re.MULTILINE)
    if match:
        body = body[:match.start()] + entry + body[match.start():]
    else:
        body = body.rstrip() + "\n\n" + entry
    changelog.write_text(body, encoding="utf-8")

print(f"版本：{current} -> {nxt}")
print("记得：重新打包（Linux: packaging/build-linux.sh → desktop/build-linux.sh；"
      "Windows: packaging/build-exe.ps1 → desktop/build.ps1），发布物会带新版本号。")
PY
