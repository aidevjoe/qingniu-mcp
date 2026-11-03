#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
轻牛健康 (QingNiu / Yolanda Health) MCP server —— 只读桥接。

协议逆向自 Android 客户端 v4.21.1（与 qingniu-data.html 导出器同源）：
  - 登录：手机号 + 密码（密码仅在本地做 MD5，与官方 App 行为一致）
  - 请求签名：HMAC-SHA1，密钥 HMAC_KEY，签名串 "x-timestamp:..||x-resource:.."
  - 接口域名：gw.qnniu.com

本服务纯只读，只能拉取你自己的云端体测记录，不能触发称重、不能写入。
凭证（手机号 / 密码）只在运行本服务的机器上使用，对外仅向 gw.qnniu.com 发登录请求。

两种运行方式：
  - streamable-http（默认，给 NAS / 远程用）：MCP_TRANSPORT=streamable-http
  - stdio（给本机 MCP 客户端直接 command 调用）：MCP_TRANSPORT=stdio
"""

import os
import time
import json
import hashlib
import hmac
import base64
import asyncio
from urllib.parse import urlencode

import httpx
from mcp.server.fastmcp import FastMCP

# ----------------------------------------------------------------------------
# 协议常量（逆向自 Android 4.21.1，只读用途）
# ----------------------------------------------------------------------------
BASE = "https://gw.qnniu.com/"
APP_ID = "Qing_Niu_Plus"
APP_VERSION = "4.21.1"
X_VERSION = "1.0"
RESOURCE_SUFFIX = "services"
HMAC_KEY = "cHvpgDR1Ndzf7q3leHUhZZpee2q58RBB"
HMAC_USER = "lZAYaHRXz2ThNe2Uy9as2MqfavWjjJSd"

# 指标中文名 + 单位（用于摘要可读性）
METRICS = {
    "weight": ("体重", "kg"),
    "bodyfat": ("体脂率", "%"),
    "subfat": ("皮下脂肪率", "%"),
    "visfat": ("内脏脂肪等级", ""),
    "water": ("体水分率", "%"),
    "muscle": ("肌肉量", "kg"),
    "sinew": ("骨骼肌量", "kg"),
    "bone": ("骨量", "kg"),
    "protein": ("蛋白质率", "%"),
    "bmr": ("基础代谢", "kcal"),
    "bodyage": ("身体年龄", "岁"),
    "bmi": ("BMI", ""),
    "fat_free_weight": ("去脂体重", "kg"),
    "score": ("身体得分", ""),
    "heart_rate": ("心率", "bpm"),
    "waist": ("腰围", "cm"),
    "hip": ("臀围", "cm"),
}
SUMMARY_KEYS = ["weight", "bodyfat", "muscle", "bmi", "bmr", "visfat", "bodyage", "water"]

HOST = os.environ.get("MCP_HOST", "0.0.0.0")
PORT = int(os.environ.get("MCP_PORT", "8801"))
mcp = FastMCP("qingniu-health", host=HOST, port=PORT, streamable_http_path="/mcp")

# ----------------------------------------------------------------------------
# 凭证与 token 缓存
# ----------------------------------------------------------------------------
PHONE = os.environ.get("QN_PHONE", "").strip()
PASSWORD = os.environ.get("QN_PASSWORD", "").strip()  # 明文仅用于本地 MD5，绝不落盘明文
TOKEN_FILE = os.environ.get("QN_TOKEN_FILE", "").strip()  # 可选：持久化 token 的文件路径
TOKEN_TTL = int(os.environ.get("QN_TOKEN_TTL_DAYS", "7")) * 86400

_token: str | None = None
_token_expiry: float = 0.0


# ----------------------------------------------------------------------------
# 协议层
# ----------------------------------------------------------------------------
def resource_of(endpoint: str) -> str:
    if "endpoint=" in endpoint:
        path = endpoint.split("endpoint=")[1].split("&")[0].lstrip("/")
    else:
        path = endpoint.split("?")[0].lstrip("/")
    rules = [
        ("api/accounts/", "user"),
        ("users/", "user"),
        ("measurements/", "measurement"),
        ("height_records/", "measurement"),
        ("device_binds/", "device"),
        ("api/commons/", "common"),
    ]
    for prefix, res in rules:
        if path.startswith(prefix):
            return res
    parts = [p for p in path.split("/") if p]
    return parts[-2] if len(parts) >= 2 else "user"


def _sign(ts: str, resource: str) -> str:
    payload = f"x-timestamp: {ts}||x-resource: {resource}"
    digest = hmac.new(HMAC_KEY.encode("utf-8"), payload.encode("utf-8"), hashlib.sha1).digest()
    return base64.b64encode(digest).decode("ascii")


async def qn_request(
    endpoint: str,
    method: str = "GET",
    params: dict | None = None,
    form: dict | None = None,
    token: str | None = None,
) -> dict:
    ts = str(int(time.time()))
    resource = resource_of(endpoint) + "." + RESOURCE_SUFFIX
    sig = _sign(ts, resource)
    auth = (
        f'hmac username="{HMAC_USER}",algorithm="hmac-sha1",'
        f'headers="x-timestamp x-resource",signature="{sig}"'
    )
    headers = {
        "x-timestamp": ts,
        "x-resource": resource,
        "x-method": method.upper(),
        "x-version": X_VERSION,
        "Authorization": auth,
        "User-Agent": "okhttp/3.12.0",
        "Accept": "application/json",
        "x-token": token or "",
    }
    qp = {
        "app_revision": APP_VERSION,
        "app_id": APP_ID,
        "platform": "android",
        "system_type": "Android",
        "cellphone_type": "HomeAssistant",
        "address": "",
        "cellphone_brand": "home_assistant",
    }
    if params:
        for k, v in params.items():
            if v is not None:
                qp[str(k)] = str(v)
    url = BASE + endpoint + "?" + urlencode(qp)

    async with httpx.AsyncClient(timeout=20.0) as client:
        if form is not None:
            resp = await client.request(method.upper(), url, headers=headers,
                                       content=urlencode(form).encode("utf-8"))
        else:
            resp = await client.request(method.upper(), url, headers=headers)
        text = resp.text

    try:
        j = json.loads(text)
    except Exception:
        raise RuntimeError(f"响应不是合法 JSON（HTTP {resp.status_code}）：{text[:200]}")
    code = j.get("code")
    if str(code) != "200":
        msg = j.get("msg") or j.get("error_description") or j.get("error") or f"HTTP {resp.status_code}"
        raise RuntimeError(f"[{code}] {msg}")
    return j.get("data") or {}


async def do_login(phone: str, password: str) -> dict:
    md5pw = hashlib.md5(password.encode("utf-8")).hexdigest()
    data = await qn_request(
        "api/accounts/sign_in",
        method="POST",
        form={"phone": phone, "password": md5pw, "sign_type": "phone"},
    )
    tok = data.get("token") or {}
    access = tok.get("accessToken") or tok.get("access_token")
    refresh = tok.get("refreshToken") or tok.get("refresh_token")
    ttype = tok.get("tokenType") or tok.get("token_type") or "Bearer"
    if not access:
        raise RuntimeError("登录成功但未返回 access_token")
    return {
        "token": f"{ttype} {access}",
        "access": access,
        "refresh": refresh,
        "type": ttype,
        "user": data.get("user"),
    }


async def ensure_token() -> str:
    global _token, _token_expiry
    if _token and time.time() < _token_expiry:
        return _token
    # 尝试从持久化文件恢复
    if TOKEN_FILE and os.path.exists(TOKEN_FILE):
        try:
            with open(TOKEN_FILE, "r", encoding="utf-8") as f:
                d = json.load(f)
            if d.get("token") and d.get("expiry", 0) > time.time():
                _token, _token_expiry = d["token"], d["expiry"]
                return _token
        except Exception:
            pass
    if not PHONE or not PASSWORD:
        raise RuntimeError("未配置 QN_PHONE / QN_PASSWORD，无法登录轻牛云端")
    info = await do_login(PHONE, PASSWORD)
    _token = info["token"]
    _token_expiry = time.time() + TOKEN_TTL
    if TOKEN_FILE:
        try:
            with open(TOKEN_FILE, "w", encoding="utf-8") as f:
                json.dump({"token": _token, "expiry": _token_expiry}, f)
        except Exception:
            pass
    return _token


# ----------------------------------------------------------------------------
# 业务数据解析
# ----------------------------------------------------------------------------
def _pick(obj, *keys):
    if not isinstance(obj, dict):
        return None
    for k in keys:
        v = obj.get(k)
        if v not in (None, ""):
            return v
    return None


def user_id_of(u: dict) -> str:
    return str(_pick(u, "user_id", "userId", "id") or "")


def user_name_of(u: dict) -> str:
    return _pick(u, "nick_name", "nickName") or user_id_of(u) or "(未知)"


def as_user_list(data) -> list:
    users = []
    if isinstance(data, list):
        users = [x for x in data if isinstance(x, dict)]
    elif isinstance(data, dict):
        cur = _pick(data, "current_user", "currentUser")
        if isinstance(cur, dict):
            users.append(cur)
        for key in ["member_users", "memberUsers", "users", "user_list", "userList", "list"]:
            if isinstance(data.get(key), list):
                users += [x for x in data[key] if isinstance(x, dict)]
                break
    seen, uniq = set(), []
    for u in users:
        uid = user_id_of(u)
        if uid and uid in seen:
            continue
        if uid:
            seen.add(uid)
            uniq.append(u)
    return uniq


async def fetch_measurements(token: str, user_id: str, max_pages: int = 500) -> list:
    allm, page = [], 0
    cursor = {"last_updated_at": None, "last_measurement_id": None}
    while page < max_pages:
        page += 1
        params = {"user_id": user_id}
        if cursor["last_updated_at"]:
            params["last_updated_at"] = cursor["last_updated_at"]
        if cursor["last_measurement_id"]:
            params["last_measurement_id"] = cursor["last_measurement_id"]
        data = await qn_request(
            "api/servlets?endpoint=measurements/list_measurement",
            params=params, token=token,
        )
        items = data if isinstance(data, list) else (data.get("measurements") or data.get("measurement") or [])
        for it in (items or []):
            if isinstance(it, dict):
                allm.append(it)
        finish = _pick(data, "finishFlag", "finish_flag")
        if str(finish) == "1":
            break
        if not items:
            break
        nu = _pick(data, "lastUpdatedAt", "last_updated_at")
        ni = _pick(data, "lastMeasurementId", "last_measurement_id")
        if not nu and not ni:
            break
        cursor = {"last_updated_at": nu, "last_measurement_id": ni}
    return allm


async def fetch_all(token: str) -> list:
    resp = await qn_request("api/servlets?endpoint=users/list_member_user", token=token)
    users = as_user_list(resp)
    result = []
    for u in users:
        uid = user_id_of(u)
        try:
            ms = await fetch_measurements(token, uid)
        except Exception as e:  # 单个成员失败不影响其它
            ms = {"error": str(e)}
        result.append({"user_id": uid, "name": user_name_of(u), "user": u, "measurements": ms})
    return result


def _resolve_member(all_data: list, user_id: str) -> dict | None:
    if user_id:
        for d in all_data:
            if d["user_id"] == user_id:
                return d
        return None
    return all_data[0] if all_data else None


def _fmt_ts(ts) -> str:
    try:
        return time.strftime("%Y-%m-%d %H:%M", time.localtime(int(ts)))
    except Exception:
        return str(ts) if ts is not None else "—"


def _build_summary(member: dict) -> dict:
    ms = member.get("measurements")
    if isinstance(ms, dict) and ms.get("error"):
        return {"user_id": member["user_id"], "name": member["name"],
                "error": ms["error"], "record_count": 0}
    ms = [m for m in (ms or []) if isinstance(m, dict)]
    if not ms:
        return {"user_id": member["user_id"], "name": member["name"],
                "record_count": 0, "note": "无体测数据"}
    ms_sorted = sorted(ms, key=lambda m: int(m.get("timestamp") or 0))
    last = ms_sorted[-1]
    first = ms_sorted[0]
    out = {
        "user_id": member["user_id"],
        "name": member["name"],
        "record_count": len(ms_sorted),
        "first_record": _fmt_ts(first.get("timestamp")),
        "last_record": _fmt_ts(last.get("timestamp")),
        "latest": {},
        "window_delta": {},
    }
    for k in SUMMARY_KEYS:
        lv = last.get(k)
        if lv is not None:
            label, unit = METRICS.get(k, (k, ""))
            out["latest"][k] = {"label": label, "value": lv, "unit": unit}
        fv = first.get(k)
        if lv is not None and fv is not None:
            try:
                out["window_delta"][k] = round(float(lv) - float(fv), 2)
            except Exception:
                pass
    return out


# ----------------------------------------------------------------------------
# MCP 工具
# ----------------------------------------------------------------------------
@mcp.tool()
async def qn_list_members() -> list:
    """列出当前账号下的所有成员（家庭成员 / 多用户）。返回 user_id 与 name。"""
    token = await ensure_token()
    data = await fetch_all(token)
    return [{"user_id": d["user_id"], "name": d["name"],
             "record_count": len(d["measurements"]) if isinstance(d["measurements"], list) else 0}
            for d in data]


@mcp.tool()
async def qn_get_summary(user_id: str = "") -> list:
    """生成每个成员（或指定 user_id）的体成分摘要：最新一次测量值 + 统计窗口内的变化量。
    适合直接交给 Agent 做趋势分析，无需下载全部原始记录。"""
    token = await ensure_token()
    data = await fetch_all(token)
    if user_id:
        m = _resolve_member(data, user_id)
        return [_build_summary(m)] if m else []
    return [_build_summary(d) for d in data]


@mcp.tool()
async def qn_get_measurements(user_id: str = "", days: int = 120, limit: int = 1000) -> list:
    """获取原始体测记录（时间升序）。user_id 留空则用第一个成员。
    days 控制只返回最近 N 天的记录（默认 120）；limit 控制最大条数（默认 1000）。
    返回字段含 weight/bodyfat/muscle/bmi/bmr/visfat/bodyage 等。"""
    token = await ensure_token()
    data = await fetch_all(token)
    m = _resolve_member(data, user_id)
    if not m:
        return []
    ms = m.get("measurements")
    if isinstance(ms, dict) and ms.get("error"):
        return [{"error": ms["error"]}]
    ms = [x for x in (ms or []) if isinstance(x, dict)]
    if days and days > 0:
        cutoff = int(time.time()) - days * 86400
        ms = [x for x in ms if int(x.get("timestamp") or 0) >= cutoff]
    ms.sort(key=lambda x: int(x.get("timestamp") or 0))
    return ms[-limit:] if limit else ms


@mcp.tool()
async def qn_get_latest(user_id: str = "") -> list:
    """获取每个成员（或指定 user_id）最近一次的完整体测记录。"""
    token = await ensure_token()
    data = await fetch_all(token)
    targets = [data[0]] if (user_id and not _resolve_member(data, user_id)) else (
        [_resolve_member(data, user_id)] if user_id else data)
    out = []
    for m in targets:
        if not m:
            continue
        ms = m.get("measurements")
        if isinstance(ms, dict) and ms.get("error"):
            out.append({"user_id": m["user_id"], "name": m["name"], "error": ms["error"]})
            continue
        ms = [x for x in (ms or []) if isinstance(x, dict)]
        if not ms:
            out.append({"user_id": m["user_id"], "name": m["name"], "latest": None})
            continue
        latest = sorted(ms, key=lambda x: int(x.get("timestamp") or 0))[-1]
        latest["_measured_at"] = _fmt_ts(latest.get("timestamp"))
        out.append({"user_id": m["user_id"], "name": m["name"], "latest": latest})
    return out


# ----------------------------------------------------------------------------
# 入口
# ----------------------------------------------------------------------------
def main():
    transport = os.environ.get("MCP_TRANSPORT", "streamable-http").lower()
    if transport == "stdio":
        mcp.run(transport="stdio")
    else:
        mcp.run(transport="streamable-http")


if __name__ == "__main__":
    main()
