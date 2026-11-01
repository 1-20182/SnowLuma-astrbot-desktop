"""配置迁移：让老版本的 data/config.json 能被新版本平滑接管。

规则
----
* 配置文件顶层有 ``config_version``；缺省视为 **1**（1.0.0 时代的文件）。
* 每次升级只做"加字段/改默认值/搬位置"，**不删用户填过的值**。
* 迁移前把原文件备份成 ``config.v<旧版本>.bak.json``，出问题能直接还原。
* 迁移是纯函数（输入 dict → 输出 dict），可以单测，不碰磁盘。
"""

from __future__ import annotations

import copy
import json
import time
from pathlib import Path
from typing import Any, Callable, Dict, List, Tuple

CURRENT_CONFIG_VERSION = 4


def _set_default(node: Dict[str, Any], key: str, value: Any) -> None:
    if key not in node or node[key] in (None, ""):
        node[key] = value


def _v1_to_v2(raw: Dict[str, Any]) -> Dict[str, Any]:
    """1.0.0 → 1.1.0：新增"注入模式"，老用户默认 manual（不再自动接管所有 QQ）。"""

    snow = raw.setdefault("snowluma", {})
    if not isinstance(snow, dict):
        snow = raw["snowluma"] = {}
    _set_default(snow, "hook_mode", "manual")
    _set_default(snow, "edition", "full")
    return raw


def _v2_to_v3(raw: Dict[str, Any]) -> Dict[str, Any]:
    """1.1.0 → 1.2.0：下载源、日志/安装包保留天数。"""

    app = raw.setdefault("app", {})
    if not isinstance(app, dict):
        app = raw["app"] = {}
    _set_default(app, "download_mirror", "auto")
    _set_default(app, "log_retain_days", 7)
    _set_default(app, "download_retain_days", 7)
    return raw


def _v3_to_v4(raw: Dict[str, Any]) -> Dict[str, Any]:
    """1.3.x → 1.4.0：升级时可自动备份；补上 QQ 空号占位。"""

    app = raw.setdefault("app", {})
    if not isinstance(app, dict):
        app = raw["app"] = {}
    _set_default(app, "backup_before_upgrade", True)
    snow = raw.setdefault("snowluma", {})
    if isinstance(snow, dict):
        _set_default(snow, "install_dir", "")
    astr = raw.setdefault("astrbot", {})
    if isinstance(astr, dict):
        _set_default(astr, "install_dir", "")
    return raw


#: 版本号 → 迁移函数（把 v-1 的文件升到 v）
MIGRATIONS: Dict[int, Callable[[Dict[str, Any]], Dict[str, Any]]] = {
    2: _v1_to_v2,
    3: _v2_to_v3,
    4: _v3_to_v4,
}


def read_version(raw: Dict[str, Any]) -> int:
    try:
        return max(1, int(raw.get("config_version") or 1))
    except (TypeError, ValueError):
        return 1


def migrate(raw: Dict[str, Any]) -> Tuple[Dict[str, Any], int, List[int]]:
    """把任意旧版配置升到当前版本，返回 (新配置, 起始版本, 已执行的步骤)。"""

    data = copy.deepcopy(raw) if isinstance(raw, dict) else {}
    start = read_version(data)
    applied: List[int] = []
    for target in range(start + 1, CURRENT_CONFIG_VERSION + 1):
        step = MIGRATIONS.get(target)
        if step is None:
            continue
        data = step(data) or data
        applied.append(target)
    data["config_version"] = CURRENT_CONFIG_VERSION
    return data, start, applied


def migrate_file(path: Path) -> Dict[str, Any]:
    """就地迁移一个配置文件（带备份）。返回 ``{"ok", "from", "to", "steps", "backup"}``。"""

    if not path.is_file():
        return {"ok": True, "from": CURRENT_CONFIG_VERSION, "to": CURRENT_CONFIG_VERSION,
                "steps": [], "backup": ""}
    raw = json.loads(path.read_text("utf-8"))
    data, start, applied = migrate(raw)
    if not applied and data.get("config_version") == raw.get("config_version"):
        return {"ok": True, "from": start, "to": CURRENT_CONFIG_VERSION, "steps": [], "backup": ""}

    backup = path.with_name(f"config.v{start}.bak.{time.strftime('%Y%m%d-%H%M%S')}.json")
    try:
        backup.write_text(path.read_text("utf-8"), "utf-8")
    except OSError:
        backup = Path("")
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), "utf-8")
    tmp.replace(path)
    return {"ok": True, "from": start, "to": CURRENT_CONFIG_VERSION,
            "steps": applied, "backup": str(backup)}
