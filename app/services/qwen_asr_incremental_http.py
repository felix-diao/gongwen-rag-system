"""Qwen 离线音频：HTTP chat/completions + audio_url 分段识别与重叠拼接。

实现与 test_asr/qwen_asr_smoketest_incremental_merge.py 一致：
- ffmpeg 切带重叠的 wav 段（QWEN_ASR_CHUNK_SEC / QWEN_ASR_OVERLAP_SEC 对应脚本 CHUNK_SEC / OVERLAP_SEC）；
- 每段 URL 调 chat/completions（content 含 audio_url）；
- 段间用本模块 merge_pair 做增量拼接（含边界修正时整段替换，与脚本 incremental_transcribe_and_merge 一致）。

实时录音在 LIVE_FORCE_HTTP_CHUNK 模式下按 QWEN_ASR_CHUNK_SEC / QWEN_ASR_OVERLAP_SEC 从 PCM 滑窗切段，
同样逐段请求 + merge_pair，不整文件末尾一次性识别。

注意：HTTP 转写只认 QWEN_ASR_HTTP_CHAT_URL（及 QWEN_ASR_HTTP_CHAT_MODEL / *_API_KEY），
绝不回退 LLM_API_URL / LLM_MODEL / LLM_API_KEY，避免与「文本生成纪要」的 LLM 混用。
"""

from __future__ import annotations

import re
import shutil
import subprocess
import tempfile
import threading
import wave
from contextlib import contextmanager
from difflib import SequenceMatcher
from http.server import HTTPServer, SimpleHTTPRequestHandler
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import urlparse
from functools import partial

import requests

from app.config import settings
from app.utils.logger import get_logger

logger = get_logger("qwen_asr_incremental_http")

# 全局互斥：本进程内仅一路 HTTP 分段服务占用 public_base 端口，避免并发冲突。
_incremental_http_lock = threading.Lock()


def _run_cmd(cmd: str) -> str:
    result = subprocess.run(cmd, shell=True, capture_output=True, text=True)
    if result.returncode != 0:
        raise RuntimeError(f"命令执行失败: {cmd}\n{result.stderr.strip()}")
    return result.stdout.strip()


def _clean_text(text: str) -> str:
    if "<asr_text>" in text:
        text = text.split("<asr_text>")[-1]
    return text.strip()


def _normalize_with_map(text: str) -> Tuple[str, List[int]]:
    norm_chars: List[str] = []
    index_map: List[int] = []
    for i, ch in enumerate(text):
        if ch.isalnum() or ("\u4e00" <= ch <= "\u9fff"):
            norm_chars.append(ch)
            index_map.append(i)
    return "".join(norm_chars), index_map


def _find_anchor_by_normalized_lcs(prev: str, curr: str, max_overlap: int):
    window = 2 * max_overlap
    tail = prev[-window:]
    head = curr[:window]
    tail_offset = len(prev) - len(tail)

    norm_tail, map_tail = _normalize_with_map(tail)
    norm_head, map_head = _normalize_with_map(head)
    if not norm_tail or not norm_head:
        return None

    matcher = SequenceMatcher(None, norm_tail, norm_head)
    best = None
    for block in matcher.get_matching_blocks():
        if block.size < 2:
            continue
        dist_prev_end = len(norm_tail) - (block.a + block.size)
        dist_curr_start = block.b
        score = block.size * 10 - dist_prev_end - dist_curr_start
        if best is None or score > best["score"]:
            best = {
                "a": block.a,
                "b": block.b,
                "size": block.size,
                "score": score,
                "dist_prev_end": dist_prev_end,
                "dist_curr_start": dist_curr_start,
            }
    if best is None:
        return None

    if best["dist_prev_end"] > max(12, best["size"] * 2):
        return None
    if best["dist_curr_start"] > max(12, best["size"] * 2):
        return None

    tail_anchor_start = map_tail[best["a"]]
    head_anchor_start = map_head[best["b"]]
    return tail_offset + tail_anchor_start, head_anchor_start, best["size"]


def _cleanup_repeated_phrase(text: str) -> str:
    pattern = r"([\u4e00-\u9fffA-Za-z0-9]{1,4})[。！？，、；：]\1"
    prev = None
    out = text
    while out != prev:
        prev = out
        out = re.sub(pattern, r"\1", out)
    return out


def merge_pair(
    prev: str,
    curr: str,
    min_overlap: int = 2,
    max_overlap: int = 140,
) -> Tuple[str, Dict[str, Any]]:
    if not prev:
        return curr, {"method": "init"}
    if not curr:
        return prev, {"method": "skip_empty_curr"}
    if curr in prev:
        return prev, {"method": "curr_in_prev"}

    max_len = min(max_overlap, len(prev), len(curr))
    for n in range(max_len, min_overlap - 1, -1):
        if prev[-n:] == curr[:n]:
            return prev + curr[n:], {"method": "exact_suffix_prefix", "anchor_size": n}

    anchor = _find_anchor_by_normalized_lcs(prev, curr, max_overlap=max_overlap)
    if anchor is not None:
        prev_anchor_start, curr_anchor_start, anchor_size = anchor
        merged = prev[:prev_anchor_start] + curr[curr_anchor_start:]
        merged = _cleanup_repeated_phrase(merged)
        return merged, {
            "method": "anchor_A_plus_D",
            "anchor_size": anchor_size,
            "prev_anchor_start": prev_anchor_start,
            "curr_anchor_start": curr_anchor_start,
        }

    return prev + curr, {"method": "concat_fallback"}


def _normalize_chat_completions_url(url: str) -> str:
    u = url.rstrip("/")
    if u.endswith("/chat/completions"):
        return u
    if u.endswith("/v1"):
        return f"{u}/chat/completions"
    return f"{u}/v1/chat/completions"


def _resolve_chat_url() -> str:
    """仅使用语音转写专用地址，与 LLM_API_URL（纪要/摘要等文本生成）严格分离。"""
    url = (settings.QWEN_ASR_HTTP_CHAT_URL or "").strip()
    if not url:
        raise RuntimeError(
            "请配置 QWEN_ASR_HTTP_CHAT_URL（音频转写用 chat/completions + audio_url），"
            "勿与 LLM_API_URL 混用"
        )
    return _normalize_chat_completions_url(url)


def _resolve_chat_model() -> str:
    """ASR 模型链：专用 CHAT_MODEL → QWEN_ASR_MODEL；不回退 LLM_MODEL。"""
    m = (settings.QWEN_ASR_HTTP_CHAT_MODEL or "").strip()
    if m:
        return m
    m2 = (settings.QWEN_ASR_MODEL or "").strip()
    if m2:
        return m2
    return "qwen3-asr-flash-realtime"


def _resolve_chat_api_key() -> str:
    """只使用 ASR 侧密钥，不把 LLM_API_KEY 发给转写服务。"""
    for k in (settings.QWEN_ASR_HTTP_CHAT_API_KEY, settings.QWEN_ASR_API_KEY):
        if k and str(k).strip():
            return str(k).strip()
    return ""


def _parse_public_base() -> Tuple[str, int]:
    raw = (settings.QWEN_ASR_FILE_HTTP_PUBLIC_BASE or "").strip().rstrip("/")
    if not raw:
        raise RuntimeError(
            "请配置 QWEN_ASR_FILE_HTTP_PUBLIC_BASE（如 http://公网IP:8010），"
            "使 ASR 服务能访问分段音频 URL"
        )
    u = urlparse(raw)
    if not u.scheme or not u.hostname:
        raise RuntimeError(f"QWEN_ASR_FILE_HTTP_PUBLIC_BASE 无效: {raw!r}")
    port = u.port or (443 if u.scheme == "https" else 80)
    return raw, port


def _split_to_chunks(
    audio_source: Path,
    work_dir: Path,
    chunk_sec: float,
    overlap_sec: float,
) -> Tuple[List[Dict[str, Any]], Path, float]:
    work_dir.mkdir(parents=True, exist_ok=True)
    chunks_dir = work_dir / "chunks"
    chunks_dir.mkdir(parents=True, exist_ok=True)

    audio_wav = work_dir / "audio_normalized.wav"
    _run_cmd(
        f'ffmpeg -y -i "{audio_source}" -ar 16000 -ac 1 "{audio_wav}" -loglevel quiet'
    )

    duration = float(
        _run_cmd(
            f'ffprobe -i "{audio_wav}" -show_entries format=duration -v quiet -of csv="p=0"'
        )
    )

    step = chunk_sec - overlap_sec
    if step <= 0:
        raise ValueError("QWEN_ASR_CHUNK_SEC 必须大于 QWEN_ASR_OVERLAP_SEC")

    chunks: List[Dict[str, Any]] = []
    start = 0.0
    idx = 0
    while start < duration:
        end = min(start + chunk_sec, duration)
        filename = f"chunk_{idx:04d}.wav"
        file_path = chunks_dir / filename
        _run_cmd(
            f'ffmpeg -y -i "{audio_wav}" -ss {start:.3f} -to {end:.3f} "{file_path}" -loglevel quiet'
        )
        chunks.append(
            {
                "idx": idx,
                "start": round(start, 3),
                "end": round(end, 3),
                "file_name": filename,
                "file_path": str(file_path),
            }
        )
        start += step
        idx += 1

    return chunks, audio_wav, duration


def build_asr_requests_session() -> requests.Session:
    """与 smoketest 中 session.trust_env=False 一致，并按 LLM_* 代理配置 requests。"""
    session = requests.Session()
    if not settings.LLM_USE_ENV_PROXY:
        session.trust_env = False
    if settings.LLM_PROXY_URL and str(settings.LLM_PROXY_URL).strip():
        p = str(settings.LLM_PROXY_URL).strip()
        session.proxies = {"http": p, "https": p}
    return session


def write_pcm_as_wav_file(
    dest_path: Path,
    pcm: bytes,
    *,
    sample_rate: int,
    channels: int,
    sample_width: int,
) -> None:
    """把一段裸 PCM 写成 WAV（实时滑窗切段时无整文件 ffmpeg，直接落盘供 HTTP 暴露）。"""
    dest_path = Path(dest_path)
    dest_path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(dest_path), "wb") as wf:
        wf.setnchannels(int(channels))
        wf.setsampwidth(int(sample_width))
        wf.setframerate(int(sample_rate))
        wf.writeframes(pcm)


def start_chunk_http_server(chunks_dir: Path, bind_port: int) -> Tuple[HTTPServer, threading.Thread]:
    """启动脚本中与 HTTPServer+SimpleHTTPRequestHandler 等价的静态目录服务。"""
    handler = partial(SimpleHTTPRequestHandler, directory=str(chunks_dir))
    httpd = HTTPServer(("0.0.0.0", int(bind_port)), handler)
    th = threading.Thread(target=httpd.serve_forever, daemon=True)
    th.start()
    return httpd, th


def stop_chunk_http_server(httpd: HTTPServer, th: threading.Thread) -> None:
    try:
        httpd.shutdown()
    except Exception as exc:  # noqa: BLE001
        logger.warning("HTTP 分段服务 shutdown 异常: %s", exc)
    th.join(timeout=5.0)


def post_served_wav_chunk(
    session: requests.Session,
    chat_url: str,
    model: str,
    headers: Dict[str, str],
    public_base: str,
    wav_filename: str,
    timeout: float,
    max_tokens: int,
) -> str:
    """对已通过静态服务暴露的 wav 文件名发起一次 ASR（与脚本里 chunk_url 拼接方式一致）。"""
    base = public_base.rstrip("/")
    chunk_url = f"{base}/{wav_filename}"
    return _post_one_chunk(session, chat_url, model, headers, chunk_url, timeout, max_tokens)


def _post_one_chunk(
    session: requests.Session,
    chat_url: str,
    model: str,
    headers: Dict[str, str],
    chunk_url: str,
    timeout: float,
    max_tokens: int,
) -> str:
    payload = {
        "model": model,
        "messages": [
            {
                "role": "user",
                "content": [
                    {
                        "type": "audio_url",
                        "audio_url": {"url": chunk_url},
                    }
                ],
            }
        ],
        "max_tokens": max_tokens,
    }
    r = session.post(chat_url, json=payload, timeout=timeout)
    r.raise_for_status()
    data = r.json()
    raw = data["choices"][0]["message"]["content"]
    return _clean_text(raw) if isinstance(raw, str) else ""


@contextmanager
def _serve_chunks_dir(chunks_dir: Path, bind_port: int):
    httpd, th = start_chunk_http_server(chunks_dir, bind_port)
    try:
        yield httpd
    finally:
        stop_chunk_http_server(httpd, th)


def transcribe_audio_file_incremental(source_audio: Path) -> Tuple[str, float]:
    """对本地音频文件做分段 HTTP ASR 并拼接全文。返回 (全文, 规范化后时长秒)。"""
    source_audio = Path(source_audio)
    if not source_audio.is_file():
        raise FileNotFoundError(str(source_audio))

    public_base, bind_port = _parse_public_base()
    chat_url = _resolve_chat_url()

    model = _resolve_chat_model()
    api_key = _resolve_chat_api_key()
    timeout = float(settings.QWEN_ASR_HTTP_CHAT_TIMEOUT_SEC or 120.0)
    max_tokens = int(settings.QWEN_ASR_HTTP_CHAT_MAX_TOKENS or 512)
    chunk_sec = float(settings.QWEN_ASR_CHUNK_SEC or 6.0)
    overlap_sec = float(settings.QWEN_ASR_OVERLAP_SEC or 1.0)

    work_root = Path(tempfile.mkdtemp(prefix="qwen_asr_inc_"))

    headers: Dict[str, str] = {}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"

    session = build_asr_requests_session()

    merged_text = ""
    duration_sec = 0.0

    try:
        with _incremental_http_lock:
            chunks, _norm_wav, duration_sec = _split_to_chunks(
                source_audio, work_root, chunk_sec, overlap_sec
            )
            if not chunks:
                return "", duration_sec

            with _serve_chunks_dir(work_root / "chunks", bind_port):
                for seg in chunks:
                    chunk_url = f"{public_base}/{seg['file_name']}"
                    try:
                        curr = _post_one_chunk(
                            session, chat_url, model, headers, chunk_url, timeout, max_tokens
                        )
                    except Exception as exc:  # noqa: BLE001
                        logger.warning(
                            "分段 ASR 失败 idx=%s url=%s error=%s",
                            seg["idx"],
                            chunk_url,
                            exc,
                        )
                        curr = ""
                    merged_text, _ = merge_pair(merged_text, curr)
    finally:
        shutil.rmtree(work_root, ignore_errors=True)

    return merged_text, duration_sec


def validate_incremental_http_config() -> None:
    """启动实时 HTTP 分段或文件转写前校验配置。"""
    _parse_public_base()
    _resolve_chat_url()
    _resolve_chat_model()


def get_incremental_http_public_base_and_port() -> Tuple[str, int]:
    """返回 (对外 URL 前缀, 本机 bind 端口)。须在 validate_incremental_http_config 之后调用。"""
    return _parse_public_base()


def asr_http_runtime_params() -> Tuple[str, str, Dict[str, str], float, int]:
    """chat 地址、模型、鉴权头、超时与 max_tokens，供实时滑窗与离线分段共用。"""
    chat_url = _resolve_chat_url()
    model = _resolve_chat_model()
    api_key = _resolve_chat_api_key()
    timeout = float(settings.QWEN_ASR_HTTP_CHAT_TIMEOUT_SEC or 120.0)
    max_tokens = int(settings.QWEN_ASR_HTTP_CHAT_MAX_TOKENS or 512)
    headers: Dict[str, str] = {}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    return chat_url, model, headers, timeout, max_tokens
