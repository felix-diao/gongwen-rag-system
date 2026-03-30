#!/usr/bin/env python3
"""
Qwen3-ASR 本地流式接口一键自检脚本（可直接点击 Run 运行）

检测项：
1) 登录拿 token: POST /api/auth/login
2) Qwen ASR 健康检查: GET /api/minutes/local/asr/health
3) 本地流式 WS 连通性: ws://.../api/minutes/local/{meeting_id}/live?token=...

说明：
- 默认按下方“配置区”自动运行，不需要命令行参数。
- 你只需要改配置区里的账号密码/地址即可。
"""

from __future__ import annotations

import asyncio
import json
import sys
from datetime import datetime
from typing import Any, Dict, Optional, Tuple
from urllib.error import HTTPError, URLError
from urllib.parse import quote
from urllib.request import Request, urlopen

# ================= 配置（参考 qwen_asr_smoketest_incremental_merge.py 风格） =================
# 后端地址候选：脚本会按顺序自动探测第一个可用地址
SERVER_CANDIDATES = [
    "http://127.0.0.1:8080",
    "http://localhost:8080",
    "http://0.0.0.0:8080",
]

# 登录账号
USERNAME = "admin"
PASSWORD = "Admin123!"

# 若为 None：自动使用已有会议或自动创建测试会议
MEETING_ID: Optional[int] = None

# 超时（秒）
HTTP_TIMEOUT = 10
WS_TIMEOUT = 10

# 是否启用 asr/health 检查
CHECK_ASR_HEALTH = True


def now() -> str:
    return datetime.now().strftime("%H:%M:%S")


def log(level: str, message: str) -> None:
    print(f"[{now()}] [{level}] {message}")


def http_json(
    method: str,
    url: str,
    timeout: int = 10,
    headers: Optional[Dict[str, str]] = None,
    body: Optional[Dict[str, Any]] = None,
) -> Tuple[int, Dict[str, Any]]:
    data = None
    final_headers = {"Content-Type": "application/json"}
    if headers:
        final_headers.update(headers)
    if body is not None:
        data = json.dumps(body).encode("utf-8")
    req = Request(url, data=data, headers=final_headers, method=method)
    try:
        with urlopen(req, timeout=timeout) as resp:
            payload = resp.read().decode("utf-8")
            return resp.status, json.loads(payload) if payload else {}
    except HTTPError as exc:
        raw = exc.read().decode("utf-8", errors="ignore")
        try:
            parsed = json.loads(raw) if raw else {}
        except json.JSONDecodeError:
            parsed = {"raw": raw}
        return exc.code, parsed
    except URLError as exc:
        raise RuntimeError(f"HTTP 请求失败: {exc}") from exc


def login(base_url: str, username: str, password: str, timeout: int) -> str:
    log("INFO", "开始登录获取 token")
    status, payload = http_json(
        "POST",
        f"{base_url}/api/auth/login",
        timeout=timeout,
        body={"username": username, "password": password},
    )
    if status != 200:
        raise RuntimeError(f"登录失败，HTTP {status}, 响应: {payload}")
    if not payload.get("success"):
        raise RuntimeError(f"登录接口返回失败: {payload}")
    token = (((payload.get("data") or {}).get("access_token")) or "").strip()
    if not token:
        raise RuntimeError(f"登录成功但未返回 token: {payload}")
    log("OK", "登录成功")
    return token


def pick_server(candidates: list[str], timeout: int) -> str:
    last_error = ""
    for base_url in candidates:
        base_url = base_url.rstrip("/")
        try:
            status, payload = http_json("GET", f"{base_url}/health", timeout=timeout)
            if status == 200 and isinstance(payload, dict) and payload.get("status") == "healthy":
                log("OK", f"已探测到可用服务地址: {base_url}")
                return base_url
            last_error = f"健康检查异常: HTTP {status}, payload={payload}"
        except Exception as exc:
            last_error = str(exc)
    raise RuntimeError(f"未找到可用服务地址，候选={candidates}，最后错误={last_error}")


def ensure_meeting(base_url: str, token: str, meeting_id: Optional[int], timeout: int) -> int:
    auth = {"Authorization": f"Bearer {token}"}
    if meeting_id is not None:
        status, payload = http_json("GET", f"{base_url}/api/meetings/{meeting_id}", timeout=timeout, headers=auth)
        if status == 200 and payload.get("success"):
            log("OK", f"使用指定会议 meeting_id={meeting_id}")
            return meeting_id
        raise RuntimeError(f"指定 meeting_id={meeting_id} 不可用，HTTP {status}, 响应: {payload}")

    status, payload = http_json("GET", f"{base_url}/api/meetings", timeout=timeout, headers=auth)
    if status != 200 or not payload.get("success"):
        raise RuntimeError(f"获取会议列表失败，HTTP {status}, 响应: {payload}")
    meetings = payload.get("data") or []
    if meetings:
        mid = int(meetings[0]["id"])
        log("OK", f"使用已有会议 meeting_id={mid}")
        return mid

    create_body = {
        "title": "Qwen ASR 自检会议",
        "date": datetime.utcnow().isoformat(),
        "location": "auto-check",
        "host": "auto-check",
        "participants": "auto-check",
        "content_text": "用于测试 Qwen ASR 流式接口",
        "status": "created",
    }
    status, payload = http_json("POST", f"{base_url}/api/meetings", timeout=timeout, headers=auth, body=create_body)
    if status != 200 or not payload.get("success"):
        raise RuntimeError(f"创建测试会议失败，HTTP {status}, 响应: {payload}")
    mid = int((payload.get("data") or {}).get("id"))
    log("OK", f"创建测试会议成功 meeting_id={mid}")
    return mid


def check_local_asr_health(base_url: str, token: str, timeout: int) -> Dict[str, Any]:
    log("INFO", "检查 /api/minutes/local/asr/health")
    auth = {"Authorization": f"Bearer {token}"}
    status, payload = http_json(
        "GET",
        f"{base_url}/api/minutes/local/asr/health?timeout_seconds={max(2, min(timeout, 30))}&check_protocol=true",
        timeout=timeout + 3,
        headers=auth,
    )
    if status != 200:
        raise RuntimeError(f"ASR 健康检查 HTTP {status}, 响应: {payload}")
    if not payload.get("success"):
        raise RuntimeError(f"ASR 健康检查返回失败: {payload}")
    data = payload.get("data") or {}
    ok = bool(data.get("ok"))
    if ok:
        log("OK", f"ASR 健康检查通过: {data}")
    else:
        log("WARN", f"ASR 健康检查未通过: {data}")
    return data


async def check_ws_stream(base_url: str, token: str, meeting_id: int, timeout: int) -> Dict[str, Any]:
    try:
        import websockets
    except Exception as exc:  # pragma: no cover
        raise RuntimeError(
            "缺少 websockets 依赖，请先安装: pip install websockets"
        ) from exc

    ws_url = (
        base_url.replace("http://", "ws://").replace("https://", "wss://")
        + f"/api/minutes/local/{meeting_id}/live?token={quote(token)}"
    )
    log("INFO", f"检查 WS 连通性: {ws_url}")

    received_types = []
    close_reason = ""
    try:
        async with websockets.connect(ws_url, open_timeout=timeout, close_timeout=3, max_size=4 * 1024 * 1024) as ws:
            await ws.send(json.dumps({"action": "config", "rate": 16000, "channels": 1, "sample_width": 2}))
            await ws.send(json.dumps({"action": "stop"}))

            loop = asyncio.get_running_loop()
            deadline = loop.time() + timeout
            while loop.time() < deadline:
                remain = max(0.1, deadline - loop.time())
                try:
                    raw = await asyncio.wait_for(ws.recv(), timeout=remain)
                except asyncio.TimeoutError:
                    break
                except Exception as exc:
                    close_reason = str(exc)
                    break

                if isinstance(raw, bytes):
                    continue
                try:
                    msg = json.loads(raw)
                except json.JSONDecodeError:
                    continue
                mtype = str(msg.get("type") or "")
                if mtype:
                    received_types.append(mtype)
                if mtype in {"error", "completed"}:
                    break

            ok = any(t in {"session_created", "partial", "final", "completed"} for t in received_types)
            if ok:
                log("OK", f"WS 流式接口可用，收到事件类型: {received_types}")
            else:
                log("WARN", f"WS 未收到预期业务事件，收到: {received_types or ['<none>']} close={close_reason or '-'}")
            return {
                "ok": ok,
                "events": received_types,
                "close_reason": close_reason,
                "ws_url": ws_url,
            }
    except Exception as exc:
        return {
            "ok": False,
            "events": received_types,
            "close_reason": str(exc),
            "ws_url": ws_url,
        }


async def main_async() -> int:
    base_url = ""
    summary: Dict[str, Any] = {
        "server": None,
        "http_ok": False,
        "asr_health_ok": False,
        "ws_ok": False,
        "meeting_id": None,
    }
    try:
        base_url = pick_server(SERVER_CANDIDATES, HTTP_TIMEOUT)
        summary["server"] = base_url
        token = login(base_url, USERNAME, PASSWORD, HTTP_TIMEOUT)
        summary["http_ok"] = True

        if CHECK_ASR_HEALTH:
            health_data = check_local_asr_health(base_url, token, HTTP_TIMEOUT)
            summary["asr_health_ok"] = bool(health_data.get("ok"))
            summary["asr_health"] = health_data
        else:
            summary["asr_health_ok"] = True
            summary["asr_health"] = {"skipped": True}

        meeting_id = ensure_meeting(base_url, token, MEETING_ID, HTTP_TIMEOUT)
        summary["meeting_id"] = meeting_id

        ws_result = await check_ws_stream(base_url, token, meeting_id, WS_TIMEOUT)
        summary["ws_ok"] = bool(ws_result.get("ok"))
        summary["ws_detail"] = ws_result
    except Exception as exc:
        summary["error"] = str(exc)
        log("ERROR", str(exc))

    print("\n========== Qwen3-ASR 流式接口测试结果 ==========")
    print(json.dumps(summary, ensure_ascii=False, indent=2))

    all_ok = bool(summary.get("http_ok") and summary.get("asr_health_ok") and summary.get("ws_ok"))
    if all_ok:
        log("PASS", "接口可用：登录/ASR健康/WS流式 全部通过")
        return 0
    log("FAIL", "接口不可用：请根据 summary 的 error / asr_health / ws_detail 排查")
    return 1


def main() -> int:
    return asyncio.run(main_async())


if __name__ == "__main__":
    sys.exit(main())
