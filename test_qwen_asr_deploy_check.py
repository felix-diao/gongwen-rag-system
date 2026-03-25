#!/usr/bin/env python3
"""
Qwen ASR deployment checker.

Usage:
  python test_qwen_asr_deploy_check.py --base http://14.103.157.248:40001 --audio ./short.m4a
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import subprocess
import sys
import urllib.error
import urllib.request
from typing import Any, Dict, List, Optional, Tuple


def _build_opener() -> urllib.request.OpenerDirector:
    # Disable local HTTP proxy interference.
    return urllib.request.build_opener(urllib.request.ProxyHandler({}))


def http_get(base: str, path: str, timeout: int = 10) -> Tuple[Optional[int], str]:
    opener = _build_opener()
    url = base.rstrip("/") + path
    req = urllib.request.Request(url, method="GET")
    try:
        with opener.open(req, timeout=timeout) as resp:
            body = resp.read().decode("utf-8", "ignore")
            return resp.status, body
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", "ignore")
        return exc.code, body
    except Exception as exc:
        return None, f"{type(exc).__name__}: {exc}"


def http_post_json(base: str, path: str, payload: Dict[str, Any], timeout: int = 20) -> Tuple[Optional[int], str]:
    opener = _build_opener()
    url = base.rstrip("/") + path
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=data,
        method="POST",
        headers={"Content-Type": "application/json"},
    )
    try:
        with opener.open(req, timeout=timeout) as resp:
            body = resp.read().decode("utf-8", "ignore")
            return resp.status, body
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", "ignore")
        return exc.code, body
    except Exception as exc:
        return None, f"{type(exc).__name__}: {exc}"


def curl_audio_api(base: str, path: str, audio: str, model: str, timeout: int = 60) -> Tuple[int, str]:
    url = base.rstrip("/") + path
    cmd = [
        "curl",
        "-sS",
        "-m",
        str(timeout),
        "-X",
        "POST",
        url,
        "-F",
        f"file=@{audio}",
        "-F",
        f"model={model}",
        "-F",
        "language=zh",
        "-F",
        "response_format=json",
    ]
    proc = subprocess.run(cmd, capture_output=True, text=True)
    output = (proc.stdout or proc.stderr).strip()
    return proc.returncode, output


async def ws_handshake(url: str, timeout: int = 8) -> Tuple[bool, str]:
    try:
        import aiohttp  # type: ignore
    except Exception as exc:
        return False, f"aiohttp_import_error: {exc}"

    try:
        async with aiohttp.ClientSession() as session:
            async with session.ws_connect(url, receive_timeout=timeout):
                return True, "handshake_ok"
    except Exception as exc:
        return False, f"{type(exc).__name__}: {exc}"


def _trim(text: str, n: int = 260) -> str:
    t = text.replace("\n", " ")
    return t if len(t) <= n else t[:n] + " ..."


def main() -> int:
    parser = argparse.ArgumentParser(description="Check deployed Qwen-ASR service capability")
    parser.add_argument("--base", default="http://14.103.157.248:40001", help="Service base URL")
    parser.add_argument("--audio", default="", help="Audio file path for /v1/audio/transcriptions test")
    parser.add_argument("--model", default="Qwen3-ASR-1.7B", help="Expected ASR model id")
    args = parser.parse_args()

    base = args.base.rstrip("/")
    expected_model = args.model

    print(f"[1] base: {base}")
    st, body = http_get(base, "/health")
    print(f"    /health => status={st}, body='{_trim(body, 120)}'")

    print("\n[2] model list")
    st, body = http_get(base, "/v1/models")
    model_ids: List[str] = []
    if st == 200:
        try:
            payload = json.loads(body)
            model_ids = [x.get("id", "") for x in payload.get("data", []) if isinstance(x, dict)]
        except Exception:
            pass
    print(f"    /v1/models => status={st}, models={model_ids if model_ids else _trim(body)}")

    print("\n[3] openapi route check")
    st, body = http_get(base, "/openapi.json")
    has_audio_api = False
    has_realtime_ws = False
    if st == 200:
        try:
            payload = json.loads(body)
            paths = payload.get("paths", {})
            has_audio_api = "/v1/audio/transcriptions" in paths
            has_realtime_ws = "/api-ws/v1/realtime" in paths
        except Exception:
            pass
    print(f"    /openapi.json => status={st}, has_audio_api={has_audio_api}, has_realtime_ws={has_realtime_ws}")

    print("\n[4] chat sanity")
    chat_model = model_ids[0] if model_ids else "/app/model"
    st, body = http_post_json(
        base,
        "/v1/chat/completions",
        {
            "model": chat_model,
            "messages": [{"role": "user", "content": "reply ok only"}],
            "max_tokens": 8,
        },
    )
    print(f"    /v1/chat/completions => status={st}, resp={_trim(body)}")

    if args.audio:
        print("\n[5] audio transcription check")
        if not os.path.exists(args.audio):
            print(f"    audio file not found: {args.audio}")
        else:
            candidates: List[str] = []
            if expected_model:
                candidates.append(expected_model)
            for m in model_ids:
                if m and m not in candidates:
                    candidates.append(m)
            for m in candidates:
                code, output = curl_audio_api(base, "/v1/audio/transcriptions", args.audio, m)
                print(f"    model={m} => curl_exit={code}, resp={_trim(output)}")
    else:
        print("\n[5] audio transcription check")
        print("    skipped (pass --audio /path/to/file)")

    print("\n[6] realtime websocket handshake")
    ws_base = base.replace("https://", "wss://").replace("http://", "ws://")
    ws_url = f"{ws_base}/api-ws/v1/realtime?model={expected_model}"
    ok, ws_msg = asyncio.run(ws_handshake(ws_url))
    print(f"    {ws_url} => ok={ok}, detail={ws_msg}")

    print("\n=== final judgment ===")
    model_ok = expected_model in model_ids
    if ok and model_ok:
        print("PASS: realtime asr service looks available.")
    else:
        print("FAIL: endpoint is reachable but not a ready Qwen3-ASR realtime service.")
        print("      Typical causes: wrong service type, model not loaded, missing audio deps, or WS route not exposed.")

    return 0


if __name__ == "__main__":
    sys.exit(main())
