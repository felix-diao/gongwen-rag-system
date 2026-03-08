"""
自动测试脚本：用本地 WAV 文件模拟实时录音，测试 Volc Live ASR WebSocket 接口

用法：
    python3 test_live_asr.py [--wav <文件路径>] [--meeting <会议ID>] [--speed <倍速>]

示例：
    python3 test_live_asr.py
    python3 test_live_asr.py --wav /path/to/audio.wav --meeting 1 --speed 2
"""
import asyncio
import json
import struct
import sys
import time
import wave
import argparse
import urllib.request
import urllib.parse

import websockets


# ─── 配置 ──────────────────────────────────────────────────────────────────
SERVER      = "http://localhost:8080"
USERNAME    = "admin"
PASSWORD    = "Admin123!"
MEETING_ID  = 1
WAV_FILE    = "/root/workspace/rag/gongwen-rag-system/uploads/meetings/3/asr_uploads/e436575bde9f48e7ab86cf4c16ef02c3.wav"
CHUNK_MS    = 200    # 每次发送多少毫秒的音频（模拟实时）
SPEED       = 5.0    # 播放倍速（越大测试越快，但要 <= 实际速度）

CYAN   = "\033[96m"
GREEN  = "\033[92m"
YELLOW = "\033[93m"
RED    = "\033[91m"
GRAY   = "\033[90m"
RESET  = "\033[0m"
BOLD   = "\033[1m"


def log(color, tag, msg):
    t = time.strftime("%H:%M:%S")
    print(f"{GRAY}[{t}]{RESET} {color}{BOLD}[{tag}]{RESET} {msg}")


# ─── 第 1 步：登录拿 Token ─────────────────────────────────────────────────
def get_token(server: str, username: str, password: str) -> str:
    log(CYAN, "AUTH", f"登录 {server}/api/auth/login ...")
    body = json.dumps({"username": username, "password": password}).encode()
    req = urllib.request.Request(
        f"{server}/api/auth/login",
        data=body,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=10) as resp:
        data = json.loads(resp.read())
    token = data["data"]["access_token"]
    log(GREEN, "AUTH", f"登录成功，token={token[:30]}...")
    return token


# ─── 第 2 步：读 WAV → PCM 块列表 ─────────────────────────────────────────
def load_wav_chunks(wav_path: str, chunk_ms: int):
    """把 WAV 切成每块 chunk_ms 毫秒的 PCM bytes 列表，返回 (chunks, sample_rate)。"""
    with wave.open(wav_path, "rb") as wf:
        sr        = wf.getframerate()
        channels  = wf.getnchannels()
        sw        = wf.getsampwidth()
        n_frames  = wf.getnframes()
        raw_pcm   = wf.readframes(n_frames)

    total_sec = n_frames / sr
    log(CYAN, "WAV", f"已读取 {wav_path}")
    log(CYAN, "WAV", f"  采样率={sr}Hz  声道={channels}  位宽={sw*8}bit  时长={total_sec:.1f}s")

    # 如果是立体声，转为单声道（取左声道）
    if channels == 2:
        import array
        samples = array.array("h", raw_pcm)
        mono = array.array("h", [samples[i] for i in range(0, len(samples), 2)])
        raw_pcm = mono.tobytes()
        log(YELLOW, "WAV", "立体声 → 已转为单声道")

    frames_per_chunk = int(sr * chunk_ms / 1000)
    bytes_per_chunk  = frames_per_chunk * sw  # 单声道

    chunks = []
    offset = 0
    while offset < len(raw_pcm):
        chunk = raw_pcm[offset:offset + bytes_per_chunk]
        if chunk:
            chunks.append(chunk)
        offset += bytes_per_chunk

    log(CYAN, "WAV", f"切分为 {len(chunks)} 块，每块 {chunk_ms}ms")
    return chunks, sr


# ─── 第 3 步：WebSocket 测试 ───────────────────────────────────────────────
async def run_test(server: str, token: str, meeting_id: int, wav_path: str,
                   chunk_ms: int, speed: float):
    ws_url = server.replace("http", "ws") + f"/api/minutes/volc/{meeting_id}/live?token={token}"
    log(CYAN, "WS", f"连接 {ws_url}")

    chunks, sample_rate = load_wav_chunks(wav_path, chunk_ms)
    sleep_per_chunk = (chunk_ms / 1000) / speed  # 控制发送速度

    confirmed_text = ""
    start_time = None
    audio_id = None

    async with websockets.connect(ws_url, max_size=10 * 1024 * 1024) as ws:
        log(GREEN, "WS", "已连接")

        # 发送音频配置
        await ws.send(json.dumps({"action": "config", "rate": sample_rate, "channels": 1, "sample_width": 2}))

        # 并发：发送音频 + 接收结果
        send_done = asyncio.Event()

        async def send_audio():
            nonlocal start_time
            start_time = time.time()
            log(CYAN, "SEND", f"开始发送 {len(chunks)} 块音频（{speed}x 倍速）...")
            for i, chunk in enumerate(chunks):
                await ws.send(chunk)
                await asyncio.sleep(sleep_per_chunk)
                if (i + 1) % 50 == 0:
                    elapsed = time.time() - start_time
                    audio_sent = (i + 1) * chunk_ms / 1000
                    log(GRAY, "SEND", f"  已发 {audio_sent:.0f}s 音频 / 耗时 {elapsed:.1f}s")
            log(GREEN, "SEND", "音频发送完毕，发送 stop 指令...")
            await ws.send(json.dumps({"action": "stop"}))
            send_done.set()

        async def recv_messages():
            nonlocal confirmed_text, audio_id
            async for raw in ws:
                try:
                    msg = json.loads(raw)
                except Exception:
                    continue
                mtype = msg.get("type", "")

                if mtype == "session_created":
                    log(GREEN, "RECV", f"session_created  session_id={msg.get('session_id')}")

                elif mtype == "partial":
                    sys.stdout.write(f"\r{YELLOW}[PARTIAL]{RESET} {msg.get('accumulated','')[:80]}")
                    sys.stdout.flush()

                elif mtype == "final":
                    sys.stdout.write("\r" + " " * 90 + "\r")
                    confirmed_text = msg.get("accumulated", confirmed_text + msg.get("text", ""))
                    log(GREEN, "FINAL", f"「{msg.get('text', '')}」")

                elif mtype == "completed":
                    sys.stdout.write("\r" + " " * 90 + "\r")
                    elapsed = time.time() - (start_time or time.time())
                    audio_id = msg.get("audio_id")
                    transcript = msg.get("transcript", confirmed_text)
                    duration   = msg.get("duration_seconds", 0)
                    log(GREEN, "DONE", "=" * 60)
                    log(GREEN, "DONE", f"✅ 转写完成！")
                    log(GREEN, "DONE", f"   session_id    = {msg.get('session_id')}")
                    log(GREEN, "DONE", f"   audio_id      = {audio_id}")
                    log(GREEN, "DONE", f"   音频时长       = {duration:.1f}s")
                    log(GREEN, "DONE", f"   测试耗时       = {elapsed:.1f}s")
                    log(GREEN, "DONE", f"   转写字数       = {len(transcript)} 字")
                    log(GREEN, "DONE", "─" * 60)
                    log(GREEN, "DONE", "【完整转写文本】")
                    # 每行 50 字换行打印
                    for i in range(0, len(transcript), 50):
                        print(f"  {transcript[i:i+50]}")
                    log(GREEN, "DONE", "=" * 60)
                    if audio_id:
                        log(CYAN, "NEXT", f"可提交妙记：POST {server}/api/minutes/volc/{meeting_id}/submit")
                    return

                elif mtype == "error":
                    log(RED, "ERROR", f"服务端错误：{msg.get('message')}")
                    return

        await asyncio.gather(send_audio(), recv_messages())

    return audio_id


# ─── 主入口 ───────────────────────────────────────────────────────────────
def main():
    parser = argparse.ArgumentParser(description="Volc Live ASR WebSocket 自动测试")
    parser.add_argument("--server",  default=SERVER,     help="服务地址")
    parser.add_argument("--user",    default=USERNAME,   help="用户名")
    parser.add_argument("--pass",    default=PASSWORD,   dest="password", help="密码")
    parser.add_argument("--meeting", default=MEETING_ID, type=int, help="会议 ID")
    parser.add_argument("--wav",     default=WAV_FILE,   help="WAV 文件路径")
    parser.add_argument("--speed",   default=SPEED,      type=float, help="发送倍速（默认5x）")
    parser.add_argument("--chunk",   default=CHUNK_MS,   type=int,   help="每块毫秒数（默认200ms）")
    args = parser.parse_args()

    print(f"\n{BOLD}{CYAN}{'='*60}")
    print(f"  Volc Live ASR 自动测试")
    print(f"  服务: {args.server}  会议ID: {args.meeting}")
    print(f"  音频: {args.wav}")
    print(f"  发送倍速: {args.speed}x")
    print(f"{'='*60}{RESET}\n")

    try:
        token = get_token(args.server, args.user, args.password)
    except Exception as e:
        log(RED, "ERROR", f"登录失败：{e}")
        sys.exit(1)

    try:
        asyncio.run(run_test(
            args.server, token, args.meeting,
            args.wav, args.chunk, args.speed,
        ))
    except KeyboardInterrupt:
        log(YELLOW, "ABORT", "用户中断测试")
    except Exception as e:
        log(RED, "ERROR", f"测试失败：{e}")
        import traceback; traceback.print_exc()
        sys.exit(1)


if __name__ == "__main__":
    main()
