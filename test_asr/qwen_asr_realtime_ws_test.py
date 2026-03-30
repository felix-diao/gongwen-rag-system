#!/usr/bin/env python3
"""
Qwen-ASR-1.7B Realtime WebSocket smoke test.

验证目标：
1) 是否能建立 WS 握手
2) 是否能完成 session.update 协议交互
3) 是否能发送音频 append 并持续收到服务端事件（真流式链路）

用法示例：
  python3 qwen_asr_realtime_ws_test.py
  python3 qwen_asr_realtime_ws_test.py --ws-url ws://IP:PORT/api-ws/v1/realtime --model /app/model
  python3 qwen_asr_realtime_ws_test.py --wav ./sample_16k_mono.wav --require-transcription
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import json
import math
import os
import struct
import sys
import uuid
import wave
from typing import Dict, List, Tuple
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse

import aiohttp


DEFAULT_WS_URL = os.getenv("QWEN_ASR_WS_URL", "ws://192.168.1.100:8888/api-ws/v1/realtime")
DEFAULT_MODEL = os.getenv("QWEN_ASR_MODEL", "qwen3-asr-flash-realtime")
DEFAULT_API_KEY = os.getenv("QWEN_ASR_API_KEY", "").strip()


def _append_model_query(ws_url: str, model: str) -> str:
    parsed = urlparse(ws_url)
    query = dict(parse_qsl(parsed.query, keep_blank_values=True))
    query.setdefault("model", model)
    return urlunparse(parsed._replace(query=urlencode(query)))


def _event(event_type: str, payload: dict) -> str:
    return json.dumps(
        {
            "event_id": f"evt_{uuid.uuid4().hex[:12]}",
            "type": event_type,
            **payload,
        },
        ensure_ascii=False,
    )


def _session_update_event(language: str) -> str:
    return _event(
        "session.update",
        {
            "session": {
                "modalities": ["text"],
                "input_audio_format": "pcm",
                "sample_rate": 16000,
                "input_audio_transcription": {"language": language},
                "turn_detection": {
                    "type": "server_vad",
                    "threshold": 0.65,
                    "silence_duration_ms": 400,
                },
            }
        },
    )


def _audio_append_event(pcm_bytes: bytes) -> str:
    return _event(
        "input_audio_buffer.append",
        {"audio": base64.b64encode(pcm_bytes).decode("ascii")},
    )


def _audio_commit_event() -> str:
    return _event("input_audio_buffer.commit", {})


def _session_finish_event() -> str:
    return _event("session.finish", {})


def _read_wav_pcm_16k_mono_s16le(path: str) -> bytes:
    with wave.open(path, "rb") as wf:
        channels = wf.getnchannels()
        sample_width = wf.getsampwidth()
        sample_rate = wf.getframerate()
        if channels != 1 or sample_width != 2 or sample_rate != 16000:
            raise ValueError(
                f"WAV 格式不匹配: channels={channels}, sample_width={sample_width}, sample_rate={sample_rate}; "
                "需要 16kHz/mono/16-bit PCM"
            )
        return wf.readframes(wf.getnframes())


def _generate_tone_pcm(duration_sec: float = 2.0, freq_hz: float = 440.0, sample_rate: int = 16000) -> bytes:
    n = int(duration_sec * sample_rate)
    amp = 0.2
    buf = bytearray()
    for i in range(n):
        v = amp * math.sin(2 * math.pi * freq_hz * (i / sample_rate))
        s = int(max(-1.0, min(1.0, v)) * 32767)
        buf.extend(struct.pack("<h", s))
    return bytes(buf)


def _chunk_bytes(data: bytes, chunk_size: int) -> List[bytes]:
    return [data[i : i + chunk_size] for i in range(0, len(data), chunk_size)]


async def run_test(args: argparse.Namespace) -> int:
    ws_url = _append_model_query(args.ws_url, args.model)
    headers: Dict[str, str] = {}
    if args.api_key:
        headers["Authorization"] = f"bearer {args.api_key}"
        headers["OpenAI-Beta"] = "realtime=v1"

    if args.wav:
        pcm = _read_wav_pcm_16k_mono_s16le(args.wav)
        audio_desc = f"WAV:{args.wav}"
    else:
        pcm = _generate_tone_pcm(duration_sec=args.tone_seconds)
        audio_desc = f"tone:{args.tone_seconds:.1f}s"

    # 0.2s / chunk => 16000 * 2 * 0.2 = 6400 bytes
    chunk_size = int(16000 * 2 * args.chunk_seconds)
    chunks = _chunk_bytes(pcm, max(1, chunk_size))

    print("=== Qwen-ASR Realtime WS Test ===")
    print(f"WS URL: {ws_url}")
    print(f"Model: {args.model}")
    print(f"Auth header: {'yes' if 'Authorization' in headers else 'no'}")
    print(f"Audio source: {audio_desc}")
    print(f"Audio chunks: {len(chunks)} (chunk_seconds={args.chunk_seconds})")
    print("")

    received_types: List[str] = []
    transcription_events: List[str] = []

    timeout = aiohttp.ClientTimeout(total=None, sock_connect=args.connect_timeout, sock_read=args.read_timeout)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        try:
            async with session.ws_connect(ws_url, headers=headers, receive_timeout=args.read_timeout) as ws:
                print("[OK] WS 握手成功")

                await ws.send_str(_session_update_event(args.language))
                print("[SEND] session.update")

                async def _receiver() -> Tuple[bool, str]:
                    while True:
                        msg = await ws.receive()
                        if msg.type == aiohttp.WSMsgType.TEXT:
                            raw = msg.data
                            try:
                                data = json.loads(raw)
                            except json.JSONDecodeError:
                                print(f"[RECV] non-json text: {raw[:200]}")
                                continue

                            evt = str(data.get("type", ""))
                            received_types.append(evt)
                            print(f"[RECV] type={evt}")
                            if "transcription" in evt:
                                transcription_events.append(evt)
                            if evt in ("session.finished", "session.closed"):
                                return True, "session_finished"
                        elif msg.type in (aiohttp.WSMsgType.CLOSED, aiohttp.WSMsgType.CLOSE):
                            return False, "ws_closed"
                        elif msg.type == aiohttp.WSMsgType.ERROR:
                            return False, f"ws_error:{ws.exception()}"

                recv_task = asyncio.create_task(_receiver())

                for idx, chunk in enumerate(chunks):
                    await ws.send_str(_audio_append_event(chunk))
                    if idx == 0:
                        print("[SEND] input_audio_buffer.append (first chunk)")
                    await asyncio.sleep(args.send_interval)

                await ws.send_str(_audio_commit_event())
                print("[SEND] input_audio_buffer.commit")
                await ws.send_str(_session_finish_event())
                print("[SEND] session.finish")

                done, reason = await asyncio.wait_for(recv_task, timeout=args.finish_timeout)
                print("")
                print(f"[DONE] receiver_done={done}, reason={reason}")
        except asyncio.TimeoutError:
            print("[FAIL] 超时：未在预期时间内完成握手/事件接收")
            return 2
        except aiohttp.WSServerHandshakeError as exc:
            print(f"[FAIL] 握手失败: status={exc.status}, message={exc.message}")
            return 2
        except Exception as exc:
            print(f"[FAIL] 异常: {type(exc).__name__}: {exc}")
            return 2

    print(f"Received event count: {len(received_types)}")
    print(f"Received event types: {received_types}")
    print(f"Transcription-like events: {transcription_events}")

    # 判定标准：
    # - 至少握手成功并收到任意事件 => 接口可调用
    # - 若要求严格转写，必须收到 transcription 事件
    if not received_types:
        print("[FAIL] 未收到任何事件，接口可能不可用或协议不匹配")
        return 2
    if args.require_transcription and not transcription_events:
        print("[FAIL] 已连通但未收到转写事件（可换真实语音 WAV 再测）")
        return 3

    print("[PASS] Realtime WS 接口可调用")
    return 0


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Qwen-ASR Realtime WebSocket smoke test")
    p.add_argument("--ws-url", default=DEFAULT_WS_URL, help="Realtime WS base URL")
    p.add_argument("--model", default=DEFAULT_MODEL, help="ASR model name")
    p.add_argument("--api-key", default=DEFAULT_API_KEY, help="API key (optional)")
    p.add_argument("--language", default="zh", help="ASR language")
    p.add_argument("--wav", default="", help="16k mono 16-bit PCM WAV file path")
    p.add_argument("--tone-seconds", type=float, default=2.0, help="tone duration if --wav not provided")
    p.add_argument("--chunk-seconds", type=float, default=0.2, help="audio append chunk duration")
    p.add_argument("--send-interval", type=float, default=0.05, help="interval between append sends")
    p.add_argument("--connect-timeout", type=float, default=8.0, help="ws connect timeout")
    p.add_argument("--read-timeout", type=float, default=30.0, help="ws read timeout")
    p.add_argument("--finish-timeout", type=float, default=20.0, help="wait for finish event timeout")
    p.add_argument("--require-transcription", action="store_true", help="require transcription events to pass")
    return p.parse_args()


def main() -> int:
    args = parse_args()
    return asyncio.run(run_test(args))


if __name__ == "__main__":
    sys.exit(main())
