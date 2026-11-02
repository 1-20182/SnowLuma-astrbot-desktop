"""联动路由（T2 维护）：``/api/link/*``。

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET  | ``/api/link/status``    | 四块状态（snowluma / astrbot / onebot / bridge）+ 最近消息 + 建议 |
| POST | ``/api/link/apply``     | 按 ``link.mode`` 写两侧配置并按需重启，返回逐步结果 |
| POST | ``/api/link/test-send`` | 发一条真实消息（OneBot 直发 → 中继 → AstrBot API 逐级回退） |
| GET  | ``/api/link/whoami``    | 当前 QQ 号 / 好友数 / 群数（OneBot 优先，AstrBot API 兜底） |
| POST | ``/api/link/probe``     | 只探测不写入（部署/端口/握手/账号全览） |

统一返回：成功 ``{"ok": true, ...}``；失败 HTTP 400/500 且 body 含 ``ok/error/detail``。
"""

from __future__ import annotations

import asyncio
import time
import logging
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Body, HTTPException, Query
from fastapi.responses import JSONResponse

from .. import astrbot_admin, bridge, snowluma_admin
from ..config import get_config
from ..events import bus

log = logging.getLogger("slab.link")

router = APIRouter(prefix="/api/link", tags=["link"])


def _fail(status_code: int, error: str, detail: Any = "") -> JSONResponse:
    """按契约返回中文错误（不用 HTTPException，避免丢失 detail）。"""

    return JSONResponse(
        status_code=status_code,
        content={"ok": False, "error": error, "detail": "" if detail is None else str(detail)},
    )


@router.get("/status")
async def status(force: bool = Query(default=False)) -> Any:
    """四块联动状态（默认 3 秒内存缓存；``?force=1`` 强制刷新）。"""

    try:
        return await bridge.link_status(get_config(), force=force)
    except Exception as exc:  # noqa: BLE001
        log.exception("读取联动状态失败")
        return _fail(500, f"读取联动状态失败：{exc}")


@router.post("/apply")
async def apply(body: Optional[Dict[str, Any]] = Body(default=None)) -> Any:
    """把配置写入 SnowLuma / AstrBot 两侧。

    body（可省略）::

        {"restart": true,          # 是否重启受影响的服务（默认 true）
         "require_provider": true} # 缺少大模型 API Key 时是否报错（默认 true）
    """

    payload = body or {}
    restart = bool(payload.get("restart", True))
    require_provider = bool(payload.get("require_provider", True))
    try:
        result = await bridge.apply_link(get_config(), restart=restart, require_provider=require_provider)
    except astrbot_admin.AstrBotConfigError as exc:
        return _fail(400, str(exc))
    except snowluma_admin.SnowLumaConfigError as exc:
        return _fail(400, str(exc))
    except Exception as exc:  # noqa: BLE001
        log.exception("写入联动配置失败")
        return _fail(500, f"写入联动配置失败：{exc}")
    if not result.get("ok"):
        failed_steps = [step for step in result.get("steps", []) if not step.get("ok")]
        only_verify = bool(failed_steps) and all(
            step.get("step") == "verify" for step in failed_steps
        )
        if only_verify:
            # 配置已经全写进去了，只是"链路还没就绪"（服务没启动 / QQ 还没登录）。
            # 这是正常中间态，不该当成错误弹红条；返回 200 + ready=false 让前端给黄色提示。
            verification = result.get("verification") or {}
            return JSONResponse(
                status_code=200,
                content={
                    **result,
                    "ok": True,
                    "ready": False,
                    "warning": failed_steps[0].get("detail") or "配置已写入，等待服务启动",
                    "suggestions": verification.get("suggestions") or [],
                },
            )
        failed = failed_steps[0] if failed_steps else {}
        return JSONResponse(
            status_code=400,
            content={
                "ok": False,
                "error": f"部分步骤失败：{failed.get('detail') or '未知原因'}",
                "detail": result.get("verification", {}),
                **result,
            },
        )
    return result


@router.post("/test-send")
async def test_send(body: Dict[str, Any] = Body(...)) -> Any:
    """发一条测试消息。

    body::

        {"target_type": "group" | "private", "target_id": "123456", "text": "你好"}
    """

    target_type = str(body.get("target_type") or "").strip().lower()
    target_id = str(body.get("target_id") or "").strip()
    text = str(body.get("text") or "").strip()
    if target_type not in ("group", "private"):
        return _fail(400, "target_type 只能是 group（群）或 private（私聊）")
    if not target_id:
        return _fail(400, "target_id 不能为空（群号或 QQ 号）")
    if not text:
        return _fail(400, "text 不能为空")
    try:
        result = await bridge.test_send(get_config(), target_type, target_id, text)
    except ValueError as exc:
        return _fail(400, str(exc))
    except Exception as exc:  # noqa: BLE001
        log.exception("测试发消息失败")
        return _fail(500, f"测试发消息失败：{exc}")
    if not result.get("ok"):
        return _fail(400, result.get("error") or "没有可用的发送通道")
    return {"ok": True, **result}


@router.post("/sync-account")
async def sync_account() -> Dict[str, Any]:
    """把 SnowLuma 实际登录的 QQ 号写回配置（不用手抄，避免主号/小号写错）。"""

    from ..config import store

    cfg = get_config()
    uins: List[str] = []

    # 1) SnowLuma 侧已连接的账号最权威
    try:
        snap = await status()
    except Exception:  # noqa: BLE001
        snap = {}
    for client in ((snap.get("onebot") or {}).get("clients") or []):
        uin = str(client.get("uin") or "").strip()
        if uin and uin not in uins:
            uins.append(uin)
    self_id = str(((snap.get("onebot") or {}).get("self_id")) or "").strip()
    if self_id and self_id not in uins:
        uins.insert(0, self_id)

    if not uins:
        raise HTTPException(
            409,
            "还没读到已登录的 QQ：先在 QQ 里登录机器人号，并让 SnowLuma 接上它（运行 → 接入 QQ）。",
        )
    if len(uins) > 1:
        # 多账号时优先选"不是管理员主号"的那个
        admin = str(cfg.astrbot.admins_id or "").strip()
        non_admin = [u for u in uins if u != admin]
        if non_admin:
            uins = non_admin

    uin = uins[0]
    updated = store.apply_patch({"snowluma": {"qq_uin": uin}})
    return {
        "ok": True,
        "qq_uin": updated.snowluma.qq_uin,
        "candidates": uins,
        "message": f"已把机器人 QQ 号同步为 {uin}",
    }


@router.post("/provider/models")
async def provider_models(payload: Optional[Dict[str, Any]] = Body(default=None)) -> Dict[str, Any]:
    """列出当前 Key 可用的模型（接口不支持时给内置建议），供前端做成下拉选择。"""

    from .. import astrbot_admin

    cfg = get_config()
    provider = cfg.astrbot.provider
    data = payload or {}

    def _pick(name: str) -> str:
        if data.get(name):
            return str(data.get(name)).strip()
        if isinstance(provider, dict):
            return str(provider.get(name) or "").strip()
        return str(getattr(provider, name, "") or "").strip()

    base = _pick("base_url")
    key = _pick("api_key")
    result = await asyncio.to_thread(astrbot_admin.list_provider_models, base, key)
    result["base_url"] = base
    result["current"] = _pick("model")
    return result


@router.post("/provider/import")
async def provider_import() -> Dict[str, Any]:
    """把 AstrBot 里**当前真正生效**的模型配置读回控制台（地址/模型/Key）。

    典型场景：用户直接改过 AstrBot 的 provider，或用的是本地网关（127.0.0.1:7864），
    控制台里却是另一套（DeepSeek 官方 + 空 Key）——界面写的和实际跑的不一致，谁都懵。
    """

    from .. import astrbot_admin
    from ..config import store

    cfg = get_config()
    try:
        info = astrbot_admin.inspect_provider(cfg)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(502, f"读取 AstrBot 配置失败：{exc}") from exc

    provider = (info or {}).get("provider") or {}
    base = str(provider.get("api_base") or "").strip()
    model = str(provider.get("model") or "").strip()
    key = str(provider.get("key") or "").strip()
    if not (base or model):
        raise HTTPException(
            409,
            "AstrBot 里还没配模型：先在 AstrBot 面板配好，或在控制台填好后点「写入联动配置」。",
        )
    patch: Dict[str, Any] = {"astrbot": {"provider": {}}}
    if base:
        patch["astrbot"]["provider"]["base_url"] = base
    if model:
        patch["astrbot"]["provider"]["model"] = model
    if key and key != "***":
        patch["astrbot"]["provider"]["api_key"] = key
    updated = store.apply_patch(patch)
    return {
        "ok": True,
        "base_url": updated.astrbot.provider.get("base_url") if isinstance(updated.astrbot.provider, dict) else base,
        "model": model,
        "has_key": bool(key and key != "***"),
        "message": f"已从 AstrBot 读回：{model} @ {base}",
    }


@router.post("/provider/test")
async def provider_test() -> Dict[str, Any]:
    """用当前配置真的请求一次大模型，直接告诉你是 key 错、地址错还是网络问题。"""

    from .. import astrbot_admin

    cfg = get_config()
    provider = cfg.astrbot.provider

    def _field(name: str) -> str:
        if isinstance(provider, dict):
            return str(provider.get(name) or "").strip()
        return str(getattr(provider, name, "") or "").strip()

    base = _field("base_url")
    model = _field("model")
    key = _field("api_key")

    if not key:
        return {
            "ok": False,
            "kind": "no-key",
            "base_url": base,
            "model": model,
            "message": "还没填大模型 API Key：AstrBot 会在收到消息时返回 401「API Key 无效」。"
                       "去 DeepSeek/OpenAI 后台建一个 Key 填进「配置 → 大模型」，保存后再点测试。",
        }
    if not base:
        return {"ok": False, "kind": "no-base", "model": model,
                "message": "还没填接口地址（Base URL），例如 https://api.deepseek.com/v1"}

    try:
        result = await asyncio.to_thread(astrbot_admin.probe_provider, base, model, key)
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "kind": "error", "base_url": base, "model": model,
                "message": f"请求异常：{exc}"}
    result.update({"base_url": base, "model": model})
    return result


def _looks_like_no_bot(message: str) -> bool:
    """判断发送失败是不是「AstrBot 侧没有可用的 bot」。

    aiocqhttp 的 ``ApiNotAvailable`` 是空消息异常，AstrBot 的 OpenAPI 把它包成
    ``"Failed to send message: "``（冒号后面什么都没有）——所以不能只匹配类名。
    """

    text = str(message or "").strip()
    return "ApiNotAvailable" in text or text.rstrip(": ").strip() == "Failed to send message"


@router.post("/repair")
async def repair(send_test: bool = Query(default=True)) -> Dict[str, Any]:
    """修「能收到消息但 AstrBot 发不出去」。

    根因：aiocqhttp 把「可发送的 bot」按 ``X-Self-ID`` 存在一张表里，任意一条同
    账号连接断开时它都直接删这条登记（不校验归属）。于是只要有一条**别的**同账号
    连接连进来又断开（旧版控制台的 WS 握手探针就是），真实那条连接就变成「连着的
    黑户」：事件照收，AstrBot 却认为没有 bot，所有回复都 ``ApiNotAvailable``。
    只有重新建一次连接才会重新登记 —— 所以修法是「重启真正接进 AstrBot 的那一侧」：

    * bridge 模式：接入方是内置中继 → 重启中继（SnowLuma 侧不用动）；
    * native 模式：接入方是 SnowLuma 自己 → 重启 SnowLuma。

    写完还会用 AstrBot 的 OpenAPI 真发一条验证；万一还是 ``ApiNotAvailable``，
    会再自愈一次后重试。
    """

    from .. import astrbot_admin

    cfg = get_config()
    steps: List[Dict[str, Any]] = []

    # native 模式下 AstrBot 直连 SnowLuma 的 WS 客户端，aiocqhttp 的 bot 登记很容易被
    # 新旧连接重叠误删（表现为"能收不能发"、群聊/插件全部失效）。中继模式由我们自己的
    # bridge 作为 Universal 客户端接入，双向都稳，所以修复时优先切过来。
    if str(cfg.link.mode) != "bridge":
        try:
            from ..config import store as _store

            _store.apply_patch({"link": {"mode": "bridge"}})
            cfg = get_config()
            applied = await bridge.apply_link(cfg, restart=False, require_provider=False)
            ok_apply = bool(applied.get("ok")) or any(
                s.get("step") == "verify" and not s.get("ok") for s in applied.get("steps", [])
            )
            steps.append({"name": "切换到中继模式", "ok": ok_apply,
                          "detail": "已把两边配置改成交给内置 bridge"})
        except Exception as exc:  # noqa: BLE001
            steps.append({"name": "切换到中继模式", "ok": False, "detail": str(exc)})

    mode = str(cfg.link.mode)

    # 中继模式下先把 native 时代残留的直连 wsClient 清掉：
    # 只要它在，SnowLuma 会把同一条 QQ 消息同时推给直连和中继，AstrBot 就回两遍。
    if mode == "bridge":
        try:
            fixed = await snowluma_admin.ensure_single_channel(cfg, hot=False)
            if fixed.get("changed"):
                steps.append({"name": "清理残留直连适配器", "ok": True,
                              "detail": fixed.get("detail") or ""})
        except Exception as exc:  # noqa: BLE001
            steps.append({"name": "清理残留直连适配器", "ok": False, "detail": str(exc)})

    if mode == "bridge":
        healed = await bridge.reregister(cfg)
        connected = bool(healed.get("ok"))
        steps.append({"name": "重启中继（重新登记可发送的 bot）", "ok": connected,
                      "detail": healed.get("detail") or ""})
    else:
        from ..supervisor import supervisor as _sup

        await _sup.stop("snowluma")
        await asyncio.sleep(3.0)
        await _sup.start("snowluma")
        steps.append({"name": "重启 SnowLuma", "ok": True, "detail": "已重启并等待重新连接"})
        connected = False
        for _ in range(30):
            await asyncio.sleep(0.5)
            try:
                snap = await status()
                if (snap.get("onebot") or {}).get("clients_connected"):
                    connected = True
                    break
            except Exception:  # noqa: BLE001 - 状态探测失败不影响后续
                pass
        steps.append({"name": "OneBot 重新连接", "ok": connected,
                      "detail": "clients=1" if connected else "30 秒内没连上，看 SnowLuma 日志"})

    if not send_test:
        return {"ok": connected, "steps": steps,
                "message": "已重启链路" if connected else "链路仍未恢复"}

    base = f"http://127.0.0.1:{int(cfg.astrbot.webui_port)}"
    raw_admins = cfg.astrbot.admins_id
    admins = ([str(a).strip() for a in raw_admins] if isinstance(raw_admins, (list, tuple))
              else [x.strip() for x in str(raw_admins or "").replace(" ", ",").split(",") if x.strip()])
    target = admins[0] if admins else str(cfg.snowluma.qq_uin or "")
    if not target:
        return {"ok": False, "steps": steps, "message": "没配管理员 QQ，无法验证发送"}

    await asyncio.sleep(1.0)

    sent: Dict[str, Any] = {"ok": False, "message": ""}
    for attempt in (1, 2):
        try:
            jwt = await asyncio.to_thread(astrbot_admin._dashboard_token, base,  # noqa: SLF001
                                          cfg.astrbot.webui_username or "astrbot",
                                          cfg.astrbot.webui_password or "")
            key = await asyncio.to_thread(astrbot_admin.ensure_api_key, base, jwt)
            suffix = "" if attempt == 1 else "（自愈重试）"
            sent = await asyncio.to_thread(
                astrbot_admin.send_im_message, base, key, f"snowluma:FriendMessage:{target}",
                f"【SnowLuma 控制台】链路修复验证 {time.strftime('%H:%M:%S')}{suffix}")
        except Exception as exc:  # noqa: BLE001
            sent = {"ok": False, "message": str(exc)}
        if sent.get("ok"):
            break
        # 第一枪就打空且是「没有可用 bot」→ 再重启一次接入方后重试
        if attempt == 1 and _looks_like_no_bot(sent.get("message", "")):
            if mode == "bridge":
                again = await bridge.reregister(cfg)
            else:
                from ..supervisor import supervisor as _sup

                await _sup.restart("snowluma", cfg)
                again = {"ok": True, "detail": "已重启 SnowLuma"}
            steps.append({"name": "自愈（重新登记 bot）", "ok": bool(again.get("ok")),
                          "detail": again.get("detail") or ""})
            await asyncio.sleep(2.5)

    steps.append({"name": "发送验证", "ok": bool(sent.get("ok")), "detail": str(sent.get("message"))})
    ok = bool(sent.get("ok"))
    if ok:
        message = "发送链路已恢复 ✅（QQ 应该收到一条验证消息）"
    elif _looks_like_no_bot(sent.get("message", "")):
        message = ("还是「AstrBot 没有可用 bot」。请关掉其它连到 AstrBot 反向 WS 的客户端"
                   "（第三方 OneBot 面板、旧版控制台页面）后重试；再不行点「重启全部」。")
    else:
        message = "还是发不出去，把这一步的 detail 发我"
    return {"ok": ok, "steps": steps, "message": message}

@router.post("/selftest")
async def selftest() -> Dict[str, Any]:
    """双向自检：SnowLuma 收得到 + AstrBot 发得出（真发一条消息给管理员）。"""

    from .. import astrbot_admin

    cfg = get_config()
    steps = []

    # 1) SnowLuma 侧：OneBot 客户端在线
    try:
        snap = await status()
    except Exception as exc:  # noqa: BLE001
        snap = {}
        steps.append({"name": "读取联动状态", "ok": False, "detail": str(exc)})
    onebot = (snap.get("onebot") or {})
    clients = onebot.get("clients") or []
    steps.append({
        "name": "SnowLuma → AstrBot 连接",
        "ok": bool(onebot.get("ready") or clients),
        "detail": f"self_id={onebot.get('self_id') or '-'} clients={onebot.get('clients_connected') or 0}",
    })

    # 2) AstrBot 侧：面板可登录 + 能建/复用 API Key
    raw_admins = cfg.astrbot.admins_id
    if isinstance(raw_admins, (list, tuple)):
        admins = [str(a).strip() for a in raw_admins if str(a).strip()]
    else:
        admins = [a.strip() for a in str(raw_admins or "").replace(" ", ",").split(",") if a.strip()]
    target = admins[0] if admins else str(cfg.snowluma.qq_uin or "")
    platform_id = "snowluma"
    try:
        from .. import astrbot_admin as _aa

        live_cfg = _aa.read_config(_aa.config_path(cfg)) or {}
        platforms = live_cfg.get("platform") or []
        if isinstance(platforms, list) and platforms:
            platform_id = str((platforms[0] or {}).get("id") or platform_id)
        elif isinstance(platforms, dict):
            platform_id = str(platforms.get("id") or platform_id)
    except Exception:  # noqa: BLE001
        pass
    if not target:
        return {"ok": False, "steps": steps, "message": "没配置管理员 QQ 号，无法自检发送"}

    base = f"http://127.0.0.1:{int(cfg.astrbot.webui_port)}"
    try:
        jwt = await asyncio.to_thread(astrbot_admin._dashboard_token, base,  # noqa: SLF001
                                      cfg.astrbot.webui_username or "astrbot",
                                      cfg.astrbot.webui_password or "")
        steps.append({"name": "AstrBot 面板登录", "ok": True, "detail": "已拿到 token"})
    except Exception as exc:  # noqa: BLE001
        steps.append({"name": "AstrBot 面板登录", "ok": False, "detail": str(exc)})
        return {"ok": False, "steps": steps,
                "message": "登录 AstrBot 面板失败：去「运行 → 面板登录」点「重置密码」后再试"}

    try:
        key = await asyncio.to_thread(astrbot_admin.ensure_api_key, base, jwt)
        steps.append({"name": "AstrBot API Key（im 权限）", "ok": True, "detail": key[:10] + "…"})
    except Exception as exc:  # noqa: BLE001
        steps.append({"name": "AstrBot API Key（im 权限）", "ok": False, "detail": str(exc)})
        return {"ok": False, "steps": steps, "message": "拿不到 API Key，无法测试发送"}

    # 3) 真发一条：AstrBot → SnowLuma → QQ
    umo = f"{platform_id}:FriendMessage:{target}"
    text = f"【SnowLuma 控制台】双向自检 {time.strftime('%H:%M:%S')}"
    try:
        sent = await asyncio.to_thread(astrbot_admin.send_im_message, base, key, umo, text)
    except Exception as exc:  # noqa: BLE001
        sent = {"ok": False, "message": str(exc)}
    # 发不出去时先自愈一次：让「真正接进 AstrBot 的那一侧」重新登记成可发送的 bot
    #（aiocqhttp 的 _wsr_api_clients 会因别的同账号连接断开而丢掉登记 → 能收不能发）
    recovered = False
    if not sent.get("ok"):
        cfg_now = get_config()
        try:
            if str(cfg_now.link.mode) == "bridge":
                healed = await bridge.reregister(cfg_now)
                recovered = bool(healed.get("ok"))
            else:
                from .. import supervisor as _sup_mod

                await _sup_mod.supervisor.stop("snowluma")
                # 等 AstrBot 侧把旧连接注销（它的 finally 里按 self_id 删登记，
                # 新旧连接重叠时会误删新连接 → 表现为"能收不能发"）
                await asyncio.sleep(3.0)
                await _sup_mod.supervisor.start("snowluma")
                recovered = True
            await asyncio.sleep(1.5)
            retry = await asyncio.to_thread(astrbot_admin.send_im_message, base, key, umo,
                                            text + "（自愈重试）")
            if retry.get("ok"):
                sent = retry
            else:
                recovered = False
        except Exception as exc:  # noqa: BLE001
            bus.log("console", f"自检自愈失败（忽略）：{exc}", level="warn")

    steps.append({
        "name": "AstrBot → SnowLuma → QQ 发送",
        "ok": bool(sent.get("ok")),
        "detail": (f"umo={umo} · {sent.get('message')}"
                   + ("（重新登记 bot 后恢复正常）" if recovered else "")),
    })
    ok = all(s["ok"] for s in steps)
    hint = ""
    if not sent.get("ok") and _looks_like_no_bot(sent.get("message", "")):
        hint = ("AstrBot 侧没有可用 bot（事件能进、但登记表里没有接入方）：中继模式下"
                "点「修复发送链路」会重启中继重新登记；别让第二个客户端用同一个 QQ 号"
                "去连 AstrBot 的反向 WS。")
    elif not ok:
        hint = "看上面哪一步红了就修那一步；面板密码问题用「重置密码」。"
    return {"ok": ok, "steps": steps, "target": target, "umo": umo,
            "message": "双向链路正常，机器人能收也能发 ✅" if ok else (hint or "自检未通过")}


@router.get("/whoami")
async def whoami() -> Any:
    """当前登录的 QQ 号 / 好友数 / 群数。"""

    try:
        result = await bridge.whoami(get_config())
    except Exception as exc:  # noqa: BLE001
        log.exception("读取 QQ 账号信息失败")
        return _fail(500, f"读取 QQ 账号信息失败：{exc}")
    if not result.get("ok"):
        return _fail(400, result.get("error") or "没有可用的查询通道")
    return {"ok": True, **result}


@router.post("/probe")
async def probe() -> Any:
    """只探测不写入：部署状态、端口、配置一致性、反向 WS 握手、账号发现。

    与 ``GET /status`` 的区别：这里是**强制刷新**（绕过 3 秒内存缓存），
    对应前端"立即探测"按钮。
    """

    cfg = get_config()
    try:
        status = await bridge.link_status(cfg, force=True)
        snowluma = status.get("snowluma", {})
        astrbot = status.get("astrbot", {})
    except Exception as exc:  # noqa: BLE001
        log.exception("联动探测失败")
        return _fail(500, f"联动探测失败：{exc}")
    ready = bool(
        snowluma.get("connected") if cfg.link.mode == "native"
        else (snowluma.get("connected") and astrbot.get("onebot_ws", {}).get("ok"))
    )
    return {
        "ok": True,
        "mode": cfg.link.mode,
        "ready": ready,
        "snowluma": snowluma,
        "astrbot": astrbot,
        "onebot": {
            "server": astrbot.get("onebot_ws", {}),
            "relay_url": snowluma_admin.relay_ws_url(cfg),
        },
        "suggestions": list(snowluma.get("suggestions") or []) + list(astrbot.get("suggestions") or []),
    }


@router.get("/accounts")
async def accounts() -> Any:
    """发现到的 QQ 账号（配置文件 + data 目录 + 日志），供前端填充账号下拉框。"""

    cfg = get_config()
    try:
        found = snowluma_admin.list_accounts(cfg)
        sources = snowluma_admin.account_sources(cfg)
    except Exception as exc:  # noqa: BLE001
        return _fail(500, f"读取账号列表失败：{exc}")
    return {"ok": True, "accounts": found, "sources": sources, "configured": cfg.snowluma.qq_uin}
