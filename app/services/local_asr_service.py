"""
基于本地部署 Qwen3-ASR-1.7B 的流式语音识别服务。

支持两种模式：
- live ：前端实时发送 PCM 音频帧 → 透传到本地 Qwen3-ASR 服务 → 实时返回识别结果，结束后合成 WAV 保存
- file ：从 TOS 下载音频 → 转 WAV → 读取 PCM 帧分块发送到本地服务 → SSE 推流识别结果

本地 Qwen3-ASR Realtime WebSocket 协议（兼容 OpenAI Realtime 风格）：
  URL:  ws://<host>:<port>/api-ws/v1/realtime?model=<model>
  Auth: Authorization: bearer <api_key>
  客户端事件: session.update / input_audio_buffer.append / session.finish
  服务端事件: session.created / session.updated /
             input_audio_buffer.speech_started / input_audio_buffer.speech_stopped /
             conversation.item.input_audio_transcription.text (中间结果) /
             conversation.item.input_audio_transcription.completed (最终结果) /
             session.finished
"""
from __future__ import annotations

import asyncio
import base64
import json
import os
import re
import socket
import subprocess
import tempfile
import threading
import time
import uuid
import wave
from difflib import SequenceMatcher
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, AsyncGenerator, Dict, List, Optional, Tuple
from urllib.parse import urlparse

import aiohttp
from sqlalchemy.orm import Session

from app.config import settings
from app.models.database import LocalAsrSession, LocalMeetingAudio, SessionLocal
from app.services.local_asr_events import (
    _extract_transcription_text,
    _is_final_transcription_event,
    _is_partial_transcription_event,
)
from app.services.websocket_manager import meeting_ws_manager
from app.utils.logger import get_logger

logger = get_logger("local_asr_service")

# ─── 音频保存目录 ────────────────────────────────────────────────────────────

def _get_audio_save_dir() -> Path:
    base = settings.QWEN_ASR_AUDIO_SAVE_DIR or os.path.join(settings.UPLOAD_DIR, "local_asr_recordings")
    p = Path(base)
    p.mkdir(parents=True, exist_ok=True)
    return p


def _session_audio_path(meeting_id: int, session_id: int) -> Path:
    d = _get_audio_save_dir() / f"meeting_{meeting_id}"
    d.mkdir(parents=True, exist_ok=True)
    return d / f"local_session_{session_id}.wav"


# ─── WAV 工具 ────────────────────────────────────────────────────────────────

def _save_pcm_as_wav(
    pcm_chunks: List[bytes], dest: Path,
    sample_rate: int = 16000, channels: int = 1, sample_width: int = 2,
) -> float:
    pcm_data = b"".join(pcm_chunks)
    with wave.open(str(dest), "wb") as wf:
        wf.setnchannels(channels)
        wf.setsampwidth(sample_width)
        wf.setframerate(sample_rate)
        wf.writeframes(pcm_data)
    n_frames = len(pcm_data) // (channels * sample_width)
    return n_frames / sample_rate


def _ensure_wav_on_disk(file_path: str) -> str:
    src = Path(file_path)
    try:
        with open(src, "rb") as f:
            header = f.read(12)
        is_wav = header[:4] == b"RIFF" and header[8:12] == b"WAVE"
    except Exception:
        is_wav = False
    if is_wav:
        return file_path
    wav_path = src.with_suffix(".wav")
    cmd = [
        "ffmpeg", "-v", "quiet", "-y", "-i", str(src),
        "-acodec", "pcm_s16le", "-ac", "1", "-ar", "16000",
        str(wav_path),
    ]
    try:
        subprocess.run(cmd, check=True, stderr=subprocess.PIPE)
        try:
            src.unlink()
        except OSError:
            pass
        logger.info("Converted %s → %s", src.name, wav_path.name)
        return str(wav_path)
    except Exception as exc:
        logger.warning("Conversion failed, using original: %s", exc)
        return file_path


# ─── 增量拼接工具（参考 qwen_asr_smoketest_incremental_merge）──────────────────

def _clean_asr_text(text: str) -> str:
    if not text:
        return ""
    if "<asr_text>" in text:
        text = text.split("<asr_text>")[-1]
    return text.strip()


def _normalize_with_map(text: str):
    norm_chars = []
    index_map = []
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


def _merge_pair(prev: str, curr: str, min_overlap: int = 2, max_overlap: int = 140):
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


def _build_incremental_fields(old_text: str, new_text: str) -> Dict[str, Any]:
    """
    计算前端增量渲染所需字段：
    - delta: 仅新增的尾部文本（可直接 append）
    - replace: True 表示出现边界修正，前端应使用 accumulated 全量替换
    """
    if new_text.startswith(old_text):
        return {"delta": new_text[len(old_text):], "replace": False}
    return {"delta": new_text, "replace": True}


# ─── DB 工具 ─────────────────────────────────────────────────────────────────

def _create_session(db: Session, meeting_id: int, session_type: str) -> LocalAsrSession:
    session = LocalAsrSession(meeting_id=meeting_id, session_type=session_type, status="pending")
    db.add(session)
    db.commit()
    db.refresh(session)
    return session


def _finalize_session(
    db: Session, session_id: int, transcript_text: str,
    audio_local_path: Optional[str] = None, duration_seconds: Optional[float] = None,
    error_msg: Optional[str] = None,
) -> LocalAsrSession:
    session = db.query(LocalAsrSession).filter(LocalAsrSession.id == session_id).first()
    if not session:
        raise ValueError(f"LocalAsrSession {session_id} not found")
    if error_msg:
        session.status = "failed"
        session.error_msg = error_msg
    else:
        session.status = "completed"
        session.transcript_text = transcript_text
        session.audio_local_path = audio_local_path
        session.duration_seconds = duration_seconds
    db.commit()
    db.refresh(session)
    return session


def _upload_local_live_audio_and_minutes(
    meeting_id: int,
    session_id: int,
    audio_path: str,
    transcript: str,
) -> Tuple[Optional[int], bool, Optional[str]]:
    """
    在线录音停止后的 TOS 上传与纪要生成在线程池执行，避免阻塞 asyncio 事件循环
    （与火山 volc 版 asyncio.to_thread 对齐，防止上传阶段卡死整站 WS/HTTP）。
    """
    from app.services.local_minutes_service import local_minutes_service

    db = SessionLocal()
    audio_id: Optional[int] = None
    minutes_generated = False
    minutes_error: Optional[str] = None
    try:
        audio_record = local_minutes_service.upload_from_local(
            db=db,
            meeting_id=meeting_id,
            local_path=Path(audio_path),
            original_name=f"live_{session_id}.wav",
            content_type="audio/wav",
            source_asr_session_id=session_id,
        )
        if transcript:
            audio_record.transcript_text = transcript
            db.commit()
        audio_id = audio_record.id
        logger.info("Local live audio uploaded to TOS audio_id=%s (thread worker)", audio_id)
        if transcript:
            try:
                local_minutes_service.generate_minutes_from_transcript(db, meeting_id)
                minutes_generated = True
                logger.info(
                    "LocalLiveASR auto minutes generated meeting_id=%s (thread worker)",
                    meeting_id,
                )
            except Exception as exc:  # noqa: BLE001
                minutes_error = str(exc)
                logger.warning(
                    "LocalLiveASR auto minutes generation failed meeting_id=%s: %s",
                    meeting_id,
                    exc,
                )
    except Exception as exc:  # noqa: BLE001
        logger.warning("Failed to upload local live audio / minutes (thread worker): %s", exc)
    finally:
        try:
            db.close()
        except Exception:  # noqa: BLE001
            pass
    return audio_id, minutes_generated, minutes_error


# ─── Qwen3-ASR WebSocket 协议工具 ────────────────────────────────────────────

def _build_ws_url() -> str:
    base = (settings.QWEN_ASR_WS_URL or "ws://172.17.32.228:40001/api-ws/v1/realtime").rstrip("/")
    model = settings.QWEN_ASR_MODEL or "qwen3-asr-flash-realtime"
    return f"{base}?model={model}"


def _build_ws_headers() -> dict:
    """
    构建 WebSocket 连接头。
    - 本地部署（QWEN_ASR_API_KEY 留空）：不发送认证头，避免本地服务器返回 403
    - DashScope 云端：填写 QWEN_ASR_API_KEY 后自动带上认证头
    """
    headers: dict = {}
    api_key = (settings.QWEN_ASR_API_KEY or "").strip()
    if api_key and api_key not in ("your-local-api-key", "sk-your-dashscope-api-key"):
        headers["Authorization"] = f"bearer {api_key}"
        headers["OpenAI-Beta"] = "realtime=v1"
    return headers


def _mask_header_value(value: str) -> str:
    if not value:
        return value
    if len(value) <= 10:
        return "*" * len(value)
    return f"{value[:6]}***{value[-4:]}"


def _masked_ws_headers(headers: Dict[str, str]) -> Dict[str, str]:
    masked: Dict[str, str] = {}
    for key, value in headers.items():
        if key.lower() in ("authorization", "x-api-key"):
            masked[key] = _mask_header_value(value)
        else:
            masked[key] = value
    return masked


def _derive_http_base_from_ws(ws_url: str) -> Optional[str]:
    """Convert ws(s)://host:port/path -> http(s)://host:port."""
    try:
        parsed = urlparse(ws_url)
    except Exception:
        return None
    if not parsed.scheme or not parsed.netloc:
        return None
    if parsed.scheme == "ws":
        http_scheme = "http"
    elif parsed.scheme == "wss":
        http_scheme = "https"
    else:
        return None
    return f"{http_scheme}://{parsed.netloc}"


async def _probe_http_endpoint(http: aiohttp.ClientSession, url: str, timeout_seconds: int) -> Dict[str, Any]:
    try:
        async with http.get(url, timeout=timeout_seconds) as resp:
            text = await resp.text()
            snippet = text[:400] if text else ""
            return {
                "ok": 200 <= resp.status < 300,
                "status": resp.status,
                "body_snippet": snippet,
            }
    except Exception as exc:
        return {
            "ok": False,
            "status": None,
            "error": f"{type(exc).__name__}: {exc}",
        }


async def diagnose_qwen_asr_connectivity(
    timeout_seconds: int = 8,
    check_protocol: bool = True,
) -> Dict[str, Any]:
    ws_url = _build_ws_url()
    ws_headers = _build_ws_headers()
    result: Dict[str, Any] = {
        "ok": False,
        "ws_url": ws_url,
        "model": settings.QWEN_ASR_MODEL,
        "headers_masked": _masked_ws_headers(ws_headers),
        "has_api_key": bool((settings.QWEN_ASR_API_KEY or "").strip()),
        "timeout_seconds": timeout_seconds,
        "handshake_status": None,
        "error_type": None,
        "error_message": None,
        "first_event_type": None,
        "protocol_check": check_protocol,
        "elapsed_ms": None,
        "http_base": None,
        "http_probes": {},
        "server_hint": None,
    }

    start = asyncio.get_running_loop().time()
    try:
        async with aiohttp.ClientSession() as http:
            http_base = _derive_http_base_from_ws(ws_url)
            result["http_base"] = http_base
            if http_base:
                health = await _probe_http_endpoint(http, f"{http_base}/health", timeout_seconds)
                models = await _probe_http_endpoint(http, f"{http_base}/v1/models", timeout_seconds)
                result["http_probes"] = {
                    "health": health,
                    "v1_models": models,
                }

            async with http.ws_connect(ws_url, headers=ws_headers, receive_timeout=timeout_seconds) as ds_ws:
                result["ok"] = True
                result["handshake_status"] = 101
                if check_protocol:
                    await ds_ws.send_str(_session_update_event())
                    try:
                        msg = await asyncio.wait_for(ds_ws.receive(), timeout=timeout_seconds)
                        if msg.type == aiohttp.WSMsgType.TEXT:
                            payload = json.loads(msg.data)
                            result["first_event_type"] = payload.get("type")
                            result["first_event"] = payload
                        else:
                            result["first_event_type"] = str(msg.type)
                    except asyncio.TimeoutError:
                        result["first_event_type"] = "timeout"
    except aiohttp.WSServerHandshakeError as exc:
        result["error_type"] = "handshake_error"
        result["handshake_status"] = exc.status
        result["error_message"] = str(exc)
    except aiohttp.ClientConnectorError as exc:
        result["error_type"] = "connect_error"
        result["error_message"] = str(exc)
    except asyncio.TimeoutError as exc:
        result["error_type"] = "timeout"
        result["error_message"] = str(exc)
    except Exception as exc:
        result["error_type"] = type(exc).__name__
        result["error_message"] = str(exc)
    finally:
        elapsed = (asyncio.get_running_loop().time() - start) * 1000
        result["elapsed_ms"] = int(elapsed)

    # Provide a human-friendly hint to speed up troubleshooting.
    if result.get("ok"):
        result["server_hint"] = "realtime_ws_ok"
    else:
        status = result.get("handshake_status")
        probes = result.get("http_probes") or {}
        health = probes.get("health") or {}
        models = probes.get("v1_models") or {}
        if status == 403 and health.get("ok") and models.get("ok"):
            result["server_hint"] = "http_openai_api_detected_but_realtime_ws_forbidden"
        elif status == 404:
            result["server_hint"] = "realtime_path_not_found"
        elif result.get("error_type") == "connect_error":
            result["server_hint"] = "network_unreachable_or_port_closed"
        elif result.get("error_type") == "timeout":
            result["server_hint"] = "network_or_server_timeout"
        else:
            result["server_hint"] = "unknown"
    return result


def _session_update_event() -> str:
    evt = {
        "event_id": f"evt_{uuid.uuid4().hex[:12]}",
        "type": "session.update",
        "session": {
            "modalities": ["text"],
            "input_audio_format": "pcm",
            "sample_rate": 16000,
            "input_audio_transcription": {
                "language": settings.QWEN_ASR_LANGUAGE or "zh",
            },
            "turn_detection": {
                "type": "server_vad",
                "threshold": float(settings.QWEN_ASR_VAD_THRESHOLD or 0.65),
                "silence_duration_ms": settings.QWEN_ASR_SILENCE_DURATION_MS or 400,
            },
        },
    }
    return json.dumps(evt, ensure_ascii=False)


def _audio_append_event(pcm_bytes: bytes) -> str:
    evt = {
        "event_id": f"evt_{uuid.uuid4().hex[:12]}",
        "type": "input_audio_buffer.append",
        "audio": base64.b64encode(pcm_bytes).decode("ascii"),
    }
    return json.dumps(evt, ensure_ascii=False)


def _session_finish_event() -> str:
    evt = {
        "event_id": f"evt_{uuid.uuid4().hex[:12]}",
        "type": "session.finish",
    }
    return json.dumps(evt, ensure_ascii=False)


def _audio_commit_event() -> str:
    evt = {
        "event_id": f"evt_{uuid.uuid4().hex[:12]}",
        "type": "input_audio_buffer.commit",
    }
    return json.dumps(evt, ensure_ascii=False)


def _split_wav_chunks_with_overlap(
    wav_path: str,
    chunk_sec: float = 6.0,
    overlap_sec: float = 1.0,
) -> List[str]:
    """
    将 wav 文件按固定时长 + 重叠切块，返回临时 chunk wav 路径列表。
    """
    if chunk_sec <= 0:
        raise ValueError("chunk_sec must be > 0")
    if overlap_sec < 0:
        raise ValueError("overlap_sec must be >= 0")
    step_sec = chunk_sec - overlap_sec
    if step_sec <= 0:
        raise ValueError("chunk_sec must be greater than overlap_sec")

    chunk_paths: List[str] = []
    with wave.open(wav_path, "rb") as wf:
        channels = wf.getnchannels()
        sample_width = wf.getsampwidth()
        framerate = wf.getframerate()
        total_frames = wf.getnframes()

        chunk_frames = max(1, int(framerate * chunk_sec))
        step_frames = max(1, int(framerate * step_sec))
        start = 0
        idx = 0

        while start < total_frames:
            wf.setpos(start)
            frames = wf.readframes(chunk_frames)
            if not frames:
                break
            tmp = tempfile.NamedTemporaryFile(delete=False, suffix=f".chunk{idx:04d}.wav")
            tmp_path = tmp.name
            tmp.close()
            with wave.open(tmp_path, "wb") as out:
                out.setnchannels(channels)
                out.setsampwidth(sample_width)
                out.setframerate(framerate)
                out.writeframes(frames)
            chunk_paths.append(tmp_path)
            start += step_frames
            idx += 1

    return chunk_paths


def _extract_http_asr_text(payload: Any) -> str:
    """
    兼容不同 ASR 部署返回结构，尽可能提取文本。
    """
    if isinstance(payload, str):
        return payload.strip()
    if not isinstance(payload, dict):
        return ""

    direct_fields = ("text", "transcript", "result")
    for key in direct_fields:
        value = payload.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()

    choices = payload.get("choices")
    if isinstance(choices, list) and choices:
        choice0 = choices[0] if isinstance(choices[0], dict) else {}
        message = choice0.get("message") if isinstance(choice0, dict) else {}
        if isinstance(message, dict):
            content = message.get("content")
            if isinstance(content, str) and content.strip():
                return content.strip()
        text = choice0.get("text") if isinstance(choice0, dict) else None
        if isinstance(text, str) and text.strip():
            return text.strip()

    return ""


async def _fetch_http_model_ids(http_base: str) -> List[str]:
    api = f"{http_base.rstrip('/')}/v1/models"
    try:
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=20)) as http:
            async with http.get(api) as resp:
                if resp.status >= 400:
                    return []
                payload = await resp.json()
    except Exception:
        return []

    data = payload.get("data") if isinstance(payload, dict) else None
    if not isinstance(data, list):
        return []
    ids: List[str] = []
    for item in data:
        if not isinstance(item, dict):
            continue
        model_id = item.get("id")
        if isinstance(model_id, str) and model_id.strip():
            ids.append(model_id.strip())
    return ids


async def _resolve_http_asr_models(http_base: str) -> List[str]:
    configured = (settings.QWEN_ASR_MODEL or "").strip()
    model_ids = await _fetch_http_model_ids(http_base)
    # 若服务明确暴露 /app/model，固定使用它，避免模型切换带来 404 覆盖问题
    if "/app/model" in model_ids or configured == "/app/model":
        return ["/app/model"]

    candidates: List[str] = []
    if configured:
        candidates.append(configured)
    candidates.extend(["/app/model", "Qwen3-ASR-1.7B", "qwen3-asr-flash-realtime"])
    candidates.extend(model_ids)

    dedup: List[str] = []
    seen = set()
    for model in candidates:
        if not model:
            continue
        if model in seen:
            continue
        seen.add(model)
        dedup.append(model)
    return dedup


def _is_model_not_found_response(status: int, body: str) -> bool:
    if status != 404:
        return False
    lowered = (body or "").lower()
    return "model" in lowered and ("does not exist" in lowered or "notfounderror" in lowered)


async def _transcribe_chunk_via_http_asr(
    http_base: str,
    chunk_path: str,
    model_candidates: List[str],
) -> str:
    """
    调用 /v1/audio/transcriptions 识别单个音频块。
    """
    api = f"{http_base.rstrip('/')}/v1/audio/transcriptions"
    last_error = ""
    saw_success_but_empty = False
    for model in model_candidates:
        form = aiohttp.FormData()
        form.add_field("model", model)
        form.add_field("language", settings.QWEN_ASR_LANGUAGE or "zh")
        form.add_field("response_format", "json")
        with open(chunk_path, "rb") as f:
            form.add_field("file", f, filename=Path(chunk_path).name, content_type="audio/wav")
            async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=120)) as http:
                async with http.post(api, data=form) as resp:
                    body = await resp.text()
                    if resp.status >= 400:
                        last_error = f"status={resp.status} model={model} body={body[:240]}"
                        if _is_model_not_found_response(resp.status, body):
                            continue
                        raise RuntimeError(f"HTTP ASR failed {last_error}")
                    try:
                        payload = json.loads(body)
                    except json.JSONDecodeError:
                        payload = body
                    text = _clean_asr_text(_extract_http_asr_text(payload))
                    if text:
                        return text
                    saw_success_but_empty = True
                    last_error = f"empty_text model={model} body={body[:240]}"
                    # 某些部署 /audio/transcriptions 可能返回空文本，尝试 data_url chat-completions 兜底
                    try:
                        chat_text = await _transcribe_chunk_via_http_chat_completions_dataurl(
                            http_base=http_base,
                            chunk_path=chunk_path,
                            model_candidates=model_candidates,
                        )
                        if chat_text:
                            logger.info(
                                "Chunk transcription fallback success via chat-completions data_url model=%s",
                                model,
                            )
                            return chat_text
                        # data_url 也返回空文本，视作静音段，不当作错误
                        return ""
                    except Exception as exc:
                        logger.warning("Chunk transcription data_url fallback failed: %s", exc)

    if saw_success_but_empty:
        # 至少有一次请求成功但文本为空（静音段），属于正常场景
        return ""

    raise RuntimeError(
        f"HTTP ASR transcriptions failed after models={model_candidates}. last_error={last_error}"
    )


def _extract_chat_completion_text(payload: Any) -> str:
    if not isinstance(payload, dict):
        return ""
    choices = payload.get("choices")
    if not isinstance(choices, list) or not choices:
        return ""
    choice0 = choices[0] if isinstance(choices[0], dict) else {}
    message = choice0.get("message") if isinstance(choice0, dict) else {}
    if isinstance(message, dict):
        content = message.get("content")
        if isinstance(content, str):
            return content.strip()
        if isinstance(content, list):
            # 兼容 message.content 为分段结构的场景
            parts: List[str] = []
            for item in content:
                if isinstance(item, str):
                    parts.append(item)
                elif isinstance(item, dict):
                    txt = item.get("text") or item.get("content")
                    if isinstance(txt, str):
                        parts.append(txt)
            return "\n".join([p for p in parts if p]).strip()
    text = choice0.get("text") if isinstance(choice0, dict) else None
    if isinstance(text, str):
        return text.strip()
    return ""


async def _transcribe_chunk_via_http_chat_completions_url(
    http_base: str,
    chunk_url: str,
    model_candidates: List[str],
) -> str:
    api = f"{http_base.rstrip('/')}/v1/chat/completions"
    last_error = ""

    # 对齐 qwen_asr_smoketest_incremental_merge：优先固定 /app/model
    ordered_models = ["/app/model"] + [m for m in model_candidates if m != "/app/model"]
    for model in ordered_models:
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
            "max_tokens": 256,
        }
        try:
            async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=120)) as http:
                async with http.post(api, json=payload) as resp:
                    body = await resp.text()
                    if resp.status >= 400:
                        last_error = f"status={resp.status} model={model} body={body[:240]}"
                        if _is_model_not_found_response(resp.status, body):
                            continue
                        # 非 model-not-found 错误直接抛出，避免被后续模型 404 覆盖真实原因
                        raise RuntimeError(f"chat-completions(url) failed {last_error}")
                    try:
                        data = json.loads(body)
                    except json.JSONDecodeError:
                        data = body
                    text = _clean_asr_text(_extract_chat_completion_text(data))
                    if text:
                        return text
                    logger.info("chat-completions(url) empty_text model=%s chunk_url=%s", model, chunk_url)
                    # 与增量转写一致：空文本按静音段处理，不视为失败
                    return ""
        except Exception as exc:
            last_error = f"request_failed model={model} err={type(exc).__name__}: {exc}"

    raise RuntimeError(
        f"HTTP ASR chat-completions failed models={model_candidates}. "
        f"chunk_url={chunk_url} last_error={last_error}"
    )


async def _transcribe_chunk_via_http_chat_completions_dataurl(
    http_base: str,
    chunk_path: str,
    model_candidates: List[str],
) -> str:
    api = f"{http_base.rstrip('/')}/v1/chat/completions"
    last_error = ""

    try:
        with open(chunk_path, "rb") as f:
            audio_b64 = base64.b64encode(f.read()).decode("ascii")
    except Exception as exc:
        raise RuntimeError(f"read_chunk_failed: {type(exc).__name__}: {exc}") from exc

    data_url = f"data:audio/wav;base64,{audio_b64}"

    ordered_models = sorted(model_candidates, key=lambda m: 0 if m == "/app/model" else 1)
    for model in ordered_models:
        payload = {
            "model": model,
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "audio_url",
                            "audio_url": {"url": data_url},
                        }
                    ],
                }
            ],
            "max_tokens": 256,
        }
        try:
            async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=120)) as http:
                async with http.post(api, json=payload) as resp:
                    body = await resp.text()
                    if resp.status >= 400:
                        last_error = f"status={resp.status} model={model} body={body[:240]}"
                        if _is_model_not_found_response(resp.status, body):
                            continue
                        raise RuntimeError(f"chat-completions(data_url) failed {last_error}")
                    try:
                        data = json.loads(body)
                    except json.JSONDecodeError:
                        data = body
                    text = _clean_asr_text(_extract_chat_completion_text(data))
                    if text:
                        return text
                    logger.info("chat-completions(data_url) empty_text model=%s chunk_path=%s", model, chunk_path)
                    return ""
        except Exception as exc:
            last_error = f"request_failed model={model} err={type(exc).__name__}: {exc}"

    raise RuntimeError(
        f"HTTP ASR chat-completions(data_url) failed models={model_candidates}. "
        f"chunk_path={chunk_path} last_error={last_error}"
    )


class _QuietStaticHandler(SimpleHTTPRequestHandler):
    def log_message(self, format: str, *args) -> None:  # noqa: A003
        return


def _guess_local_ip() -> str:
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        return ip or "127.0.0.1"
    except Exception:
        return "127.0.0.1"
    finally:
        s.close()


def _start_chunk_http_server(chunk_dir: str) -> tuple[ThreadingHTTPServer, threading.Thread, str]:
    configured_ip = (settings.QWEN_ASR_HTTP_SERVER_IP or "").strip()
    host_ip = configured_ip or _guess_local_ip()
    configured_port = int(settings.QWEN_ASR_HTTP_SERVER_PORT or 0)

    handler = partial(_QuietStaticHandler, directory=chunk_dir)
    try:
        httpd = ThreadingHTTPServer(("0.0.0.0", configured_port), handler)
    except OSError as exc:
        if configured_port:
            logger.warning(
                "Configured chunk HTTP port %s unavailable (%s), fallback to random free port",
                configured_port,
                exc,
            )
            httpd = ThreadingHTTPServer(("0.0.0.0", 0), handler)
        else:
            raise
    actual_port = int(httpd.server_address[1])
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    base_url = f"http://{host_ip}:{actual_port}"
    logger.info("HTTP chunk server started dir=%s base_url=%s", chunk_dir, base_url)
    return httpd, thread, base_url


async def _transcribe_pcm_chunk_via_http_asr(
    http_base: str,
    pcm_chunk: bytes,
    sample_rate: int,
    channels: int,
    sample_width: int,
    model_candidates: List[str],
) -> str:
    """
    将内存里的 PCM 片段写为临时 WAV 后，调用 HTTP ASR。
    """
    tmp = tempfile.NamedTemporaryFile(delete=False, suffix=".live_chunk.wav")
    tmp_path = tmp.name
    tmp.close()
    try:
        with wave.open(tmp_path, "wb") as wf:
            wf.setnchannels(channels)
            wf.setsampwidth(sample_width)
            wf.setframerate(sample_rate)
            wf.writeframes(pcm_chunk)
        # 与文件分段模式对齐：优先 chat-completions(data_url)，失败再回退 transcriptions
        try:
            return await _transcribe_chunk_via_http_chat_completions_dataurl(
                http_base=http_base,
                chunk_path=tmp_path,
                model_candidates=model_candidates,
            )
        except Exception:
            return await _transcribe_chunk_via_http_asr(http_base, tmp_path, model_candidates)
    finally:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass


async def _iter_http_chunk_asr_events(
    wav_path: str,
    chunk_sec: float = 6.0,
    overlap_sec: float = 1.0,
) -> AsyncGenerator[Dict[str, Any], None]:
    """
    参考 qwen_asr_smoketest_incremental_merge 的分段增量识别：
    - 固定分段 + 重叠
    - 优先走 chat-completions(audio_url=data_url) 获取与脚本一致的文本风格
    - 每段通过 _merge_pair 做边界去重与自然拼接
    """
    ws_url = _build_ws_url()
    http_base = _derive_http_base_from_ws(ws_url)
    if not http_base:
        raise RuntimeError(f"无法从 QWEN_ASR_WS_URL 推导 HTTP 地址: {ws_url}")
    model_candidates = await _resolve_http_asr_models(http_base)
    if not model_candidates:
        raise RuntimeError("未找到可用 ASR 模型，请检查 /v1/models 与 QWEN_ASR_MODEL 配置")

    merged_text = ""
    chunk_paths: List[str] = []
    try:
        chunk_paths = _split_wav_chunks_with_overlap(wav_path, chunk_sec=chunk_sec, overlap_sec=overlap_sec)
        logger.info(
            "Chunk ASR mode=chat_completions_dataurl chunks=%d models=%s",
            len(chunk_paths),
            model_candidates,
        )

        for idx, chunk_path in enumerate(chunk_paths):
            try:
                # 对齐 smoketest：优先 chat-completions(data_url)；失败时降级 transcriptions
                try:
                    curr_text = await _transcribe_chunk_via_http_chat_completions_dataurl(
                        http_base=http_base,
                        chunk_path=chunk_path,
                        model_candidates=model_candidates,
                    )
                except Exception:
                    curr_text = await _transcribe_chunk_via_http_asr(
                        http_base=http_base,
                        chunk_path=chunk_path,
                        model_candidates=model_candidates,
                    )
            except Exception as exc:
                # 单段失败不应中断整场流式过程，继续后续分段
                logger.warning("Chunk %s transcribe failed, skip chunk: %s", idx, exc)
                continue

            if not curr_text:
                continue
            old_merged = merged_text
            merged, merge_info = _merge_pair(merged_text, curr_text)
            if merged == merged_text:
                continue
            merged_text = merged
            incr = _build_incremental_fields(old_merged, merged_text)
            yield {
                "type": "final",
                "text": curr_text,
                "accumulated": merged_text,
                "delta": incr["delta"],
                "replace": incr["replace"],
                "chunk_index": idx,
                "merge_method": merge_info.get("method"),
            }
            # 对齐 smoketest：短暂间隔，确保前端能感知增量刷新
            await asyncio.sleep(0.10)
    finally:
        for p in chunk_paths:
            try:
                os.unlink(p)
            except OSError:
                pass


# ─── 实时模式 ASR（WebSocket Handler）────────────────────────────────────────

class LocalLiveAsrHandler:
    """
    管理一次实时 WebSocket ASR 会话（前端 ↔ 服务端 ↔ 本地 Qwen3-ASR 服务）。

    协议同火山 volc 版：
    - 客户端发送二进制 PCM 帧 / JSON 控制消息 {"action":"stop"} / {"action":"config",...}
    - 服务端推送 {"type":"session_created",...} / {"type":"partial",...} / {"type":"final",...} / {"type":"completed",...}
    """

    def __init__(self, websocket, meeting_id: int, db: Session):
        self._ws = websocket
        self._meeting_id = meeting_id
        self._db = db
        self._session_id: Optional[int] = None
        self._audio_chunks: List[bytes] = []
        self._merged_final_text = ""
        self._sample_rate = 16000
        self._channels = 1
        self._sample_width = 2
        self._ws_alive = True
        self._speech_active = False
        self._last_speech_ts = 0.0
        self._discard_requested = False

    def _in_speech_window(self) -> bool:
        # 允许 speech_stopped 后短时间内继续接收尾部转写，避免误丢真实结尾
        return self._speech_active or (time.monotonic() - self._last_speech_ts) <= 2.0

    async def run(self) -> None:
        from fastapi.websockets import WebSocketDisconnect

        await self._ws.accept()
        logger.info("LocalLiveASR: WS accepted meeting_id=%s", self._meeting_id)

        try:
            from app.services.local_minutes_service import local_minutes_service
            local_minutes_service.discard_workspace(
                self._db,
                self._meeting_id,
                reason="开始新一轮在线录音，丢弃当前工作区",
            )
        except Exception as exc:
            logger.warning("Failed to clear before live ASR meeting_id=%s: %s", self._meeting_id, exc)

        session = _create_session(self._db, self._meeting_id, "live")
        self._session_id = session.id

        await self._ws.send_json({
            "type": "session_created",
            "session_id": self._session_id,
            "message": "实时ASR会话已创建，请开始发送音频数据",
        })

        try:
            await self._process()
        except Exception as exc:
            logger.exception("LocalLiveASR error session_id=%s: %s", self._session_id, exc)
            if self._ws_alive:
                try:
                    await self._ws.send_json({"type": "error", "message": str(exc)})
                except Exception:
                    self._ws_alive = False
            self._fail(str(exc))
        finally:
            try:
                await self._ws.close()
            except Exception:
                pass

    async def _process(self) -> None:
        stop_event = asyncio.Event()
        prefetch_chunks: List[bytes] = []
        prefetch_ctrl: List[dict] = []
        prefetch_stop = asyncio.Event()
        fallback_http_mode = False

        async def _prefetch_client() -> None:
            try:
                while not prefetch_stop.is_set():
                    try:
                        raw = await asyncio.wait_for(self._ws.receive(), timeout=120)
                    except asyncio.TimeoutError:
                        # 客户端短暂无音频/浏览器调度抖动时，不要立刻结束整场会话
                        continue
                    msg_type = raw.get("type", "")
                    if msg_type in ("websocket.disconnect", "websocket.close"):
                        self._ws_alive = False
                        prefetch_stop.set()
                        stop_event.set()
                        break
                    if "text" in raw and raw["text"]:
                        try:
                            ctrl = json.loads(raw["text"])
                            prefetch_ctrl.append(ctrl)
                            if ctrl.get("action") == "discard":
                                self._discard_requested = True
                                prefetch_stop.set()
                                stop_event.set()
                            elif ctrl.get("action") == "stop":
                                prefetch_stop.set()
                                stop_event.set()
                        except (json.JSONDecodeError, KeyError):
                            pass
                    elif "bytes" in raw and raw["bytes"]:
                        prefetch_chunks.append(raw["bytes"])
            except Exception:
                prefetch_stop.set()
                stop_event.set()

        prefetch_task = asyncio.create_task(_prefetch_client())
        force_live_chunk_mode = bool(settings.QWEN_ASR_LIVE_FORCE_HTTP_CHUNK)

        try:
            if force_live_chunk_mode:
                logger.info(
                    "LocalLiveASR: force fixed-time chunk mode meeting_id=%s chunk_sec=%.2f overlap_sec=%.2f",
                    self._meeting_id,
                    max(1.0, float(settings.QWEN_ASR_LIVE_CHUNK_SEC or 6.0)),
                    max(0.0, float(settings.QWEN_ASR_LIVE_OVERLAP_SEC or 1.0)),
                )
                prefetch_stop.set()
                try:
                    await asyncio.wait_for(asyncio.shield(prefetch_task), timeout=1.0)
                except (asyncio.TimeoutError, asyncio.CancelledError):
                    pass
                await self._collect_audio_without_realtime(stop_event, prefetch_chunks, prefetch_ctrl)
            else:
                ws_url = _build_ws_url()
                ws_headers = _build_ws_headers()
                logger.info("LocalLiveASR: connecting to Qwen3-ASR %s meeting_id=%s", ws_url, self._meeting_id)

                async with aiohttp.ClientSession() as http:
                    async with http.ws_connect(ws_url, headers=ws_headers, receive_timeout=30) as ds_ws:
                        logger.info("LocalLiveASR: Qwen3-ASR WS connected meeting_id=%s", self._meeting_id)

                        # 发送 session.update
                        await ds_ws.send_str(_session_update_event())

                        # 等待 session.created 或 session.updated 确认
                        try:
                            init_msg = await asyncio.wait_for(ds_ws.receive(), timeout=10)
                            if init_msg.type == aiohttp.WSMsgType.TEXT:
                                init_data = json.loads(init_msg.data)
                                logger.info("LocalLiveASR: Qwen3-ASR init event=%s", init_data.get("type"))
                        except asyncio.TimeoutError:
                            logger.warning("LocalLiveASR: Qwen3-ASR init timeout")

                        # 等待 session.updated
                        try:
                            upd_msg = await asyncio.wait_for(ds_ws.receive(), timeout=5)
                            if upd_msg.type == aiohttp.WSMsgType.TEXT:
                                upd_data = json.loads(upd_msg.data)
                                logger.info("LocalLiveASR: Qwen3-ASR update event=%s", upd_data.get("type"))
                        except asyncio.TimeoutError:
                            pass

                        prefetch_stop.set()
                        try:
                            await asyncio.wait_for(asyncio.shield(prefetch_task), timeout=1.0)
                        except (asyncio.TimeoutError, asyncio.CancelledError):
                            pass

                        if stop_event.is_set():
                            logger.info("LocalLiveASR: client gone during init meeting_id=%s", self._meeting_id)
                        else:
                            send_task = asyncio.create_task(
                                self._forward_audio(ds_ws, stop_event, prefetch_chunks, prefetch_ctrl)
                            )
                            recv_task = asyncio.create_task(self._receive_from_dashscope(ds_ws))

                            await asyncio.wait([send_task, recv_task], return_when=asyncio.ALL_COMPLETED)
                            for t in [send_task, recv_task]:
                                if not t.done():
                                    t.cancel()
                                    try:
                                        await t
                                    except asyncio.CancelledError:
                                        pass
        except aiohttp.WSServerHandshakeError as exc:
            if exc.status != 403:
                raise
            fallback_http_mode = True
            logger.warning(
                "LocalLiveASR realtime ws handshake failed status=%s, fallback to buffered HTTP chunks",
                exc.status,
            )
            prefetch_stop.set()
            try:
                await asyncio.wait_for(asyncio.shield(prefetch_task), timeout=1.0)
            except (asyncio.TimeoutError, asyncio.CancelledError):
                pass
            await self._collect_audio_without_realtime(stop_event, prefetch_chunks, prefetch_ctrl)
        finally:
            prefetch_stop.set()
            if not prefetch_task.done():
                prefetch_task.cancel()
            try:
                await prefetch_task
            except asyncio.CancelledError:
                pass

        if self._discard_requested:
            _finalize_session(
                self._db,
                int(self._session_id),
                "",
                error_msg="用户主动丢弃，未保存音频与纪要",
            )
            self._audio_chunks.clear()
            self._merged_final_text = ""
            if self._ws_alive:
                try:
                    await self._ws.send_json({"type": "discarded", "session_id": self._session_id})
                except Exception:
                    self._ws_alive = False
            logger.info(
                "LocalLiveASR: discarded session meeting_id=%s session_id=%s",
                self._meeting_id,
                self._session_id,
            )
            return

        # 保存音频
        if self._ws_alive:
            try:
                await self._ws.send_json({"type": "saving_audio", "session_id": self._session_id})
            except Exception:
                self._ws_alive = False

        audio_path: Optional[str] = None
        duration: Optional[float] = None
        if self._audio_chunks:
            dest = _session_audio_path(self._meeting_id, self._session_id)
            try:
                duration = _save_pcm_as_wav(self._audio_chunks, dest, self._sample_rate, self._channels, self._sample_width)
                audio_path = str(dest)
                logger.info("Live audio saved %s (%.2fs)", audio_path, duration)
            except Exception as exc:
                logger.warning("Failed to save live audio: %s", exc)

        if self._ws_alive and audio_path:
            try:
                await self._ws.send_json({"type": "uploading_audio", "session_id": self._session_id})
            except Exception:
                self._ws_alive = False

        if fallback_http_mode and audio_path and self._ws_alive and not self._merged_final_text:
            try:
                async for event in _iter_http_chunk_asr_events(audio_path):
                    if event.get("type") == "final":
                        self._merged_final_text = event.get("accumulated", self._merged_final_text)
                    await self._ws.send_json(event)
            except Exception as exc:
                logger.warning("LocalLiveASR fallback transcription failed: %s", exc)
                await self._ws.send_json({"type": "error", "message": f"转写失败: {exc}"})

        transcript = self._merged_final_text
        _finalize_session(self._db, self._session_id, transcript, audio_local_path=audio_path, duration_seconds=duration)

        # 上传到 TOS + 自动生成纪要（线程池，避免同步 IO 阻塞事件循环）
        audio_id: Optional[int] = None
        minutes_generated = False
        minutes_error: Optional[str] = None
        if audio_path:
            try:
                audio_id, minutes_generated, minutes_error = await asyncio.to_thread(
                    _upload_local_live_audio_and_minutes,
                    self._meeting_id,
                    int(self._session_id),
                    audio_path,
                    transcript,
                )
            except Exception as exc:  # noqa: BLE001
                logger.warning("Local live upload/minutes asyncio.to_thread failed: %s", exc)

        if self._ws_alive:
            try:
                await self._ws.send_json({
                    "type": "completed",
                    "session_id": self._session_id,
                    "audio_id": audio_id,
                    "transcript": transcript,
                    "audio_saved": audio_path is not None,
                    "audio_uploaded": audio_id is not None,
                    "duration_seconds": duration,
                    "minutes_generated": minutes_generated,
                    "minutes_error": minutes_error,
                })
            except Exception:
                self._ws_alive = False

    async def _collect_audio_without_realtime(
        self,
        stop_event: asyncio.Event,
        prefetch_chunks: List[bytes],
        prefetch_ctrl: List[dict],
    ) -> None:
        """
        按固定时长进行在线分段转写：
        - 以墙上时间推进分段窗口（而非按收到字节量推进）
        - 在静音/无包期间自动补零，保证“没声音也计时”
        - 每段返回后立刻做增量合并并推送给前端
        """
        for ctrl in prefetch_ctrl:
            if ctrl.get("action") == "config":
                self._sample_rate = ctrl.get("rate", self._sample_rate)
                self._channels = ctrl.get("channels", self._channels)

        ws_url = _build_ws_url()
        http_base = _derive_http_base_from_ws(ws_url)
        if not http_base:
            raise RuntimeError(f"无法从 QWEN_ASR_WS_URL 推导 HTTP 地址: {ws_url}")
        model_candidates = await _resolve_http_asr_models(http_base)
        if not model_candidates:
            raise RuntimeError("未找到可用 ASR 模型，请检查 /v1/models 与 QWEN_ASR_MODEL 配置")

        chunk_sec = max(1.0, float(settings.QWEN_ASR_LIVE_CHUNK_SEC or 6.0))
        overlap_sec = max(0.0, float(settings.QWEN_ASR_LIVE_OVERLAP_SEC or 1.0))
        if overlap_sec >= chunk_sec:
            overlap_sec = max(0.0, chunk_sec - 0.1)
        frames_per_chunk = max(1, int(self._sample_rate * chunk_sec))
        overlap_frames = max(0, int(self._sample_rate * overlap_sec))
        step_frames = max(1, frames_per_chunk - overlap_frames)
        bytes_per_frame = max(1, self._channels * self._sample_width)
        min_tail_frames = max(1, int(self._sample_rate * 0.6))

        pcm_all = bytearray()
        next_start_frame = 0
        chunk_index = 0
        seg_queue: asyncio.Queue = asyncio.Queue()

        async def _transcribe_worker() -> None:
            nonlocal chunk_index
            while True:
                item = await seg_queue.get()
                if item is None:
                    seg_queue.task_done()
                    break
                idx, pcm_seg = item
                try:
                    text = await _transcribe_pcm_chunk_via_http_asr(
                        http_base=http_base,
                        pcm_chunk=pcm_seg,
                        sample_rate=self._sample_rate,
                        channels=self._channels,
                        sample_width=self._sample_width,
                        model_candidates=model_candidates,
                    )
                    if text:
                        old_merged = self._merged_final_text
                        merged, merge_info = _merge_pair(self._merged_final_text, text)
                        if merged != self._merged_final_text:
                            self._merged_final_text = merged
                            incr = _build_incremental_fields(old_merged, self._merged_final_text)
                            if self._ws_alive:
                                await self._ws.send_json(
                                    {
                                        "type": "final",
                                        "text": text,
                                        "accumulated": self._merged_final_text,
                                        "delta": incr["delta"],
                                        "replace": incr["replace"],
                                        "chunk_index": idx,
                                        "merge_method": merge_info.get("method"),
                                        "mode": "http_chunk_live",
                                    }
                                )
                except Exception as exc:
                    logger.warning("LocalLiveASR HTTP chunk transcribe failed idx=%s: %s", idx, exc)
                    if self._ws_alive:
                        await self._ws.send_json({"type": "error", "message": f"分段转写失败: {exc}"})
                finally:
                    seg_queue.task_done()

        def _ensure_pcm_length(target_frames: int) -> None:
            target_bytes = max(0, target_frames * bytes_per_frame)
            miss = target_bytes - len(pcm_all)
            if miss > 0:
                pcm_all.extend(b"\x00" * miss)

        async def _emit_ready_segments(elapsed_frames: int) -> None:
            nonlocal next_start_frame, chunk_index
            while elapsed_frames >= next_start_frame + frames_per_chunk:
                end_frame = next_start_frame + frames_per_chunk
                _ensure_pcm_length(end_frame)
                start_b = next_start_frame * bytes_per_frame
                end_b = end_frame * bytes_per_frame
                pcm_seg = bytes(pcm_all[start_b:end_b])
                await seg_queue.put((chunk_index, pcm_seg))
                chunk_index += 1
                next_start_frame += step_frames

        worker_task = asyncio.create_task(_transcribe_worker())
        started_at = time.monotonic()

        for chunk in prefetch_chunks:
            self._audio_chunks.append(chunk)
            pcm_all.extend(chunk)
        await _emit_ready_segments(int((time.monotonic() - started_at) * self._sample_rate))

        while not stop_event.is_set():
            try:
                raw = await asyncio.wait_for(self._ws.receive(), timeout=0.25)
            except asyncio.TimeoutError:
                # 允许静默：无音频包时按时间继续推进切片
                await _emit_ready_segments(int((time.monotonic() - started_at) * self._sample_rate))
                continue
            msg_type = raw.get("type", "")
            if msg_type in ("websocket.disconnect", "websocket.close"):
                self._ws_alive = False
                stop_event.set()
                break
            if "text" in raw and raw["text"]:
                try:
                    ctrl = json.loads(raw["text"])
                    if ctrl.get("action") == "discard":
                        self._discard_requested = True
                        stop_event.set()
                    elif ctrl.get("action") == "stop":
                        stop_event.set()
                    elif ctrl.get("action") == "config":
                        # 会话开始后不再动态修改采样参数，避免切片时间轴漂移
                        pass
                except (json.JSONDecodeError, KeyError):
                    pass
                await _emit_ready_segments(int((time.monotonic() - started_at) * self._sample_rate))
                continue
            if "bytes" in raw and raw["bytes"]:
                chunk = raw["bytes"]
                self._audio_chunks.append(chunk)
                pcm_all.extend(chunk)
            await _emit_ready_segments(int((time.monotonic() - started_at) * self._sample_rate))

        # flush 最后一段尾巴
        elapsed_frames = int((time.monotonic() - started_at) * self._sample_rate)
        total_frames = len(pcm_all) // bytes_per_frame
        timeline_frames = max(elapsed_frames, total_frames)
        await _emit_ready_segments(timeline_frames)
        remain_frames = timeline_frames - next_start_frame
        if remain_frames >= min_tail_frames:
            _ensure_pcm_length(timeline_frames)
            start_b = next_start_frame * bytes_per_frame
            end_b = timeline_frames * bytes_per_frame
            await seg_queue.put((chunk_index, bytes(pcm_all[start_b:end_b])))

        await seg_queue.put(None)
        await seg_queue.join()
        await worker_task

        if self._discard_requested:
            return

    async def _forward_audio(
        self, ds_ws, stop_event: asyncio.Event,
        prefetch_chunks: List[bytes], prefetch_ctrl: List[dict],
    ) -> None:
        from fastapi.websockets import WebSocketDisconnect

        for ctrl in prefetch_ctrl:
            if ctrl.get("action") == "config":
                self._sample_rate = ctrl.get("rate", self._sample_rate)
                self._channels = ctrl.get("channels", self._channels)

        for chunk in prefetch_chunks:
            self._audio_chunks.append(chunk)
            await ds_ws.send_str(_audio_append_event(chunk))

        try:
            while not stop_event.is_set():
                try:
                    raw = await asyncio.wait_for(self._ws.receive(), timeout=120)
                except asyncio.TimeoutError:
                    # 临时无数据时保持连接，避免前端出现 code=1005 中断
                    continue
                msg_type = raw.get("type", "")
                if msg_type in ("websocket.disconnect", "websocket.close"):
                    self._ws_alive = False
                    stop_event.set()
                    break
                if "text" in raw and raw["text"]:
                    try:
                        ctrl = json.loads(raw["text"])
                        if ctrl.get("action") == "discard":
                            self._discard_requested = True
                            stop_event.set()
                        elif ctrl.get("action") == "stop":
                            stop_event.set()
                        elif ctrl.get("action") == "config":
                            self._sample_rate = ctrl.get("rate", self._sample_rate)
                            self._channels = ctrl.get("channels", self._channels)
                    except (json.JSONDecodeError, KeyError):
                        pass
                    continue
                if "bytes" in raw and raw["bytes"]:
                    chunk = raw["bytes"]
                    self._audio_chunks.append(chunk)
                    await ds_ws.send_str(_audio_append_event(chunk))
        except Exception as exc:
            logger.warning("LocalLiveASR forward error: %s", exc)
            stop_event.set()
        finally:
            try:
                await ds_ws.send_str(_audio_commit_event())
                await ds_ws.send_str(_session_finish_event())
                logger.info("LocalLiveASR: sent commit+session.finish meeting_id=%s", self._meeting_id)
            except Exception as e:
                logger.warning("LocalLiveASR: failed to send commit/session.finish: %s", e)

    async def _receive_from_dashscope(self, ds_ws) -> None:
        try:
            async for msg in ds_ws:
                if msg.type == aiohttp.WSMsgType.TEXT:
                    data = json.loads(msg.data)
                    evt_type = data.get("type", "")

                    if evt_type == "input_audio_buffer.speech_started":
                        self._speech_active = True
                        self._last_speech_ts = time.monotonic()
                        continue
                    if evt_type == "input_audio_buffer.speech_stopped":
                        self._speech_active = False
                        self._last_speech_ts = time.monotonic()
                        continue

                    if _is_final_transcription_event(evt_type):
                        text = _clean_asr_text(_extract_transcription_text(data))
                        if text and self._in_speech_window():
                            merged, _ = _merge_pair(self._merged_final_text, text)
                            if merged == self._merged_final_text:
                                continue
                            self._merged_final_text = merged
                            if self._ws_alive:
                                try:
                                    await self._ws.send_json({
                                        "type": "final",
                                        "text": text,
                                        "accumulated": self._merged_final_text,
                                    })
                                except Exception:
                                    self._ws_alive = False

                    elif _is_partial_transcription_event(evt_type):
                        text = _clean_asr_text(_extract_transcription_text(data))
                        if text and self._ws_alive and self._in_speech_window():
                            try:
                                base, _ = _merge_pair(self._merged_final_text, text)
                                await self._ws.send_json({
                                    "type": "partial",
                                    "text": text,
                                    "accumulated": base,
                                })
                            except Exception:
                                self._ws_alive = False

                    elif evt_type == "session.finished":
                        logger.info("LocalLiveASR: session.finished meeting_id=%s", self._meeting_id)
                        break

                elif msg.type in (aiohttp.WSMsgType.ERROR, aiohttp.WSMsgType.CLOSED):
                    logger.info("LocalLiveASR: Qwen3-ASR WS closed meeting_id=%s", self._meeting_id)
                    break
        except Exception as exc:
            logger.warning("LocalLiveASR receive error: %s", exc)

    def _fail(self, error_msg: str) -> None:
        try:
            _finalize_session(self._db, self._session_id, "", error_msg=error_msg)
        except Exception:
            pass


# ─── 文件模式 SSE 流式 ASR ───────────────────────────────────────────────────

async def stream_local_file_asr(
    audio_id: int, meeting_id: int, file_path: str,
) -> AsyncGenerator[str, None]:
    """
    对本地音频文件通过本地 Qwen3-ASR-1.7B 服务进行流式 ASR，以 SSE 格式 yield 识别结果。

    事件类型：session_created / partial / final / completed / error
    """
    db = SessionLocal()
    session_id: Optional[int] = None
    merged_final_text = ""

    def _sse(data: dict) -> str:
        return f"data: {json.dumps(data, ensure_ascii=False)}\n\n"

    try:
        try:
            from app.services.local_minutes_service import local_minutes_service
            local_minutes_service.discard_workspace(
                db,
                meeting_id,
                reason="开始新一轮上传音频转写，丢弃当前工作区",
                current_audio_id=audio_id,
            )
        except Exception as exc:
            logger.warning("Failed to clear before SSE ASR meeting_id=%s: %s", meeting_id, exc)

        session = _create_session(db, meeting_id, "file_tos")
        session_id = session.id
        session.status = "processing"
        db.commit()

        yield _sse({"type": "session_created", "session_id": session_id, "audio_id": audio_id, "accumulated": ""})

        actual_path = _ensure_wav_on_disk(file_path)
        logger.info(
            "SSE ASR file mode=chunk_http_stream chunk_sec=%.2f overlap_sec=%.2f",
            max(1.0, float(settings.QWEN_ASR_FILE_CHUNK_SEC or 1.5)),
            max(0.0, float(settings.QWEN_ASR_FILE_OVERLAP_SEC or 0.2)),
        )
        async for event in _iter_http_chunk_asr_events(
            actual_path,
            chunk_sec=max(1.0, float(settings.QWEN_ASR_FILE_CHUNK_SEC or 1.5)),
            overlap_sec=max(0.0, float(settings.QWEN_ASR_FILE_OVERLAP_SEC or 0.2)),
        ):
            # 统一使用分段结果流：每段完成即推送，避免整段提交后一次性返回
            if event.get("type") == "final":
                merged_final_text = event.get("accumulated", merged_final_text)
            yield _sse(event)

        # 推送最终剩余的句子
        transcript = merged_final_text
        _finalize_session(db, session_id, transcript, audio_local_path=actual_path)

        audio_record = db.query(LocalMeetingAudio).filter(LocalMeetingAudio.id == audio_id).first()
        if audio_record:
            if transcript:
                audio_record.transcript_text = transcript
            audio_record.source_asr_session_id = session_id
            db.commit()

        logger.info("SSE ASR completed session_id=%s audio_id=%s len=%d", session_id, audio_id, len(transcript))
        yield _sse({
            "type": "completed",
            "session_id": session_id,
            "audio_id": audio_id,
            "transcript": transcript,
            "minutes_generated": False,
            "minutes_error": None,
        })

    except Exception as exc:
        logger.exception("SSE ASR error audio_id=%s session_id=%s: %s", audio_id, session_id, exc)
        if session_id:
            try:
                _finalize_session(db, session_id, "", error_msg=str(exc))
            except Exception:
                pass
        yield _sse({"type": "error", "message": str(exc)})
    finally:
        db.close()
