#!/usr/bin/env python3
"""联调：向 Qwen ASR HTTP 发一条与 qwen_asr_incremental_http._post_one_chunk 相同结构的请求。

用法示例：
  python scripts/test_qwen_asr_chat_audio_url.py \\
    --chat-url http://14.103.157.248:40001 \\
    --bearer "$QWEN_ASR_API_KEY" \\
    --audio-url http://112.25.91.21:30008/a.wav \\
    --model qwen3-asr-flash-realtime

说明：--bearer 一般为 .env 中 QWEN_ASR_HTTP_CHAT_API_KEY / QWEN_ASR_API_KEY；
若甲方网关要求用户 JWT，再换为登录拿到的 token。"""
from __future__ import annotations

import argparse
import sys

import requests


def _normalize_chat_url(url: str) -> str:
    u = url.rstrip("/")
    if u.endswith("/chat/completions"):
        return u
    if u.endswith("/v1"):
        return f"{u}/chat/completions"
    return f"{u}/v1/chat/completions"


def main() -> int:
    p = argparse.ArgumentParser(description="POST chat/completions + audio_url（对齐 _post_one_chunk）")
    p.add_argument("--chat-url", required=True, help="如 http://host:40001 或已含 /v1/chat/completions")
    p.add_argument("--bearer", required=True, help="Authorization Bearer 内容")
    p.add_argument("--model", default="qwen3-asr-flash-realtime")
    p.add_argument("--audio-url", required=True, help="ASR 可 GET 的 wav 地址，如 http://ip:30008/a.wav")
    p.add_argument("--max-tokens", type=int, default=512)
    p.add_argument("--timeout", type=float, default=120.0)
    p.add_argument("--no-proxy", action="store_true", help="不设代理（等同部分环境下 trust_env=False）")
    args = p.parse_args()

    chat_url = _normalize_chat_url(args.chat_url)
    payload = {
        "model": args.model,
        "messages": [
            {
                "role": "user",
                "content": [
                    {
                        "type": "audio_url",
                        "audio_url": {"url": args.audio_url},
                    }
                ],
            }
        ],
        "max_tokens": args.max_tokens,
    }
    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {args.bearer}",
    }

    session = requests.Session()
    if args.no_proxy:
        session.trust_env = False

    r = session.post(chat_url, json=payload, headers=headers, timeout=args.timeout)
    print(f"HTTP {r.status_code}")
    print(r.text[:4000])
    if r.status_code >= 400:
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
