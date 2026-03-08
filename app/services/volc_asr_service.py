"""
火山引擎大模型流式语音识别服务（功能1）。

支持两种模式：
- file  ：上传音频文件 → 后台任务调用 Volc ASR WebSocket → 结果写入 DB，并通过 meeting WebSocket 推送进度
- live  ：前端实时发送 PCM 音频帧 → 透传到 Volc ASR WebSocket → 实时返回识别结果，结束后合成 WAV 保存
"""
from __future__ import annotations

import asyncio
import gzip
import json
import os
import struct
import uuid
import wave
from datetime import datetime
from pathlib import Path
from typing import AsyncGenerator, List, Optional, Tuple

import aiohttp
from sqlalchemy.orm import Session

# 火山引擎域名绕过系统代理
_VOLC_NO_PROXY_DOMAINS = "volces.com,bytedance.com,openspeech.bytedance.com"
for _env_key in ("NO_PROXY", "no_proxy"):
    _existing = os.environ.get(_env_key, "")
    _missing = [d for d in _VOLC_NO_PROXY_DOMAINS.split(",") if d not in _existing]
    if _missing:
        os.environ[_env_key] = ",".join(filter(None, [_existing] + _missing))

from app.api.sauc_websocket_demo import (
    AsrWsClient,
    CommonUtils,
    MessageType,
    MessageTypeSpecificFlags,
    ProtocolVersion,
    SerializationType,
    CompressionType,
    RequestBuilder,
    ResponseParser,
    AsrResponse,
)
from app.config import settings
from app.models.database import SessionLocal, VolcAsrSession, VolcAudioTranscription, VolcMeetingAudio
from app.services.websocket_manager import meeting_ws_manager
from app.utils.logger import get_logger

logger = get_logger("volc_asr_service")

# ─── 音频保存目录 ────────────────────────────────────────────────────────────

def _get_audio_save_dir() -> Path:
    base = settings.VOLC_ASR_AUDIO_SAVE_DIR or os.path.join(settings.UPLOAD_DIR, "asr_recordings")
    p = Path(base)
    p.mkdir(parents=True, exist_ok=True)
    return p


def _session_audio_path(meeting_id: int, session_id: int) -> Path:
    d = _get_audio_save_dir() / f"meeting_{meeting_id}"
    d.mkdir(parents=True, exist_ok=True)
    return d / f"session_{session_id}.wav"


# ─── WAV 工具 ────────────────────────────────────────────────────────────────

def _save_pcm_as_wav(
    pcm_chunks: List[bytes],
    dest_path: Path,
    sample_rate: int = 16000,
    channels: int = 1,
    sample_width: int = 2,
) -> float:
    """把 PCM 片段列表合并写成 WAV 文件，返回时长（秒）。"""
    pcm_data = b"".join(pcm_chunks)
    with wave.open(str(dest_path), "wb") as wf:
        wf.setnchannels(channels)
        wf.setsampwidth(sample_width)
        wf.setframerate(sample_rate)
        wf.writeframes(pcm_data)
    n_frames = len(pcm_data) // (channels * sample_width)
    duration = n_frames / sample_rate
    return duration


# ─── 文本抽取 ─────────────────────────────────────────────────────────────────

def _extract_text(payload: Optional[dict]) -> Optional[str]:
    if not payload:
        return None
    r = payload.get("result") if isinstance(payload, dict) else None
    if isinstance(r, dict):
        if "text" in r:
            return r["text"] or None
        alt = r.get("alternatives")
        if isinstance(alt, list) and alt and isinstance(alt[0], dict):
            return alt[0].get("transcript") or alt[0].get("text")
    if isinstance(payload.get("text"), str):
        return payload["text"] or None
    return None


# ─── DB 工具 ─────────────────────────────────────────────────────────────────

def _create_session(
    db: Session,
    meeting_id: int,
    session_type: str,
    audio_filename: Optional[str] = None,
) -> VolcAsrSession:
    session = VolcAsrSession(
        meeting_id=meeting_id,
        session_type=session_type,
        status="pending",
        audio_filename=audio_filename,
    )
    db.add(session)
    db.commit()
    db.refresh(session)
    return session


def _append_transcription(
    db: Session,
    meeting_id: int,
    session_id: int,
    text: str,
    is_final: bool,
    start_ms: Optional[float] = None,
    end_ms: Optional[float] = None,
) -> None:
    rec = VolcAudioTranscription(
        meeting_id=meeting_id,
        source_session_id=session_id,
        provider="volc",
        text=text,
        is_final=is_final,
        start_time=start_ms,
        end_time=end_ms,
    )
    db.add(rec)
    db.commit()


def _finalize_session(
    db: Session,
    session_id: int,
    transcript_text: str,
    audio_local_path: Optional[str] = None,
    duration_seconds: Optional[float] = None,
    error_msg: Optional[str] = None,
) -> VolcAsrSession:
    session = db.query(VolcAsrSession).filter(VolcAsrSession.id == session_id).first()
    if not session:
        raise ValueError(f"VolcAsrSession {session_id} not found")
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


# ─── 文件模式 ASR（后台任务） ──────────────────────────────────────────────────

def _ensure_wav_on_disk(file_path: str) -> str:
    """
    确保本地有一个 WAV 文件可供后续使用。
    - 若原文件已是 WAV，直接返回原路径。
    - 若是其他格式，用 ffmpeg 转换后保存为同名 .wav，删除原文件，返回新路径。
    转换后的文件会持久保存到磁盘，供后续上传 TOS 使用。
    """
    import subprocess
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
        import subprocess as _sp
        _sp.run(cmd, check=True, stderr=_sp.PIPE)
        try:
            src.unlink()
        except OSError:
            pass
        logger.info("Pre-converted %s → %s", src.name, wav_path.name)
        return str(wav_path)
    except Exception as exc:
        logger.warning("Pre-conversion failed, will let AsrWsClient handle it: %s", exc)
        return file_path


def run_file_asr(
    meeting_id: int,
    session_id: int,
    file_path: str,
) -> None:
    """
    在后台（BackgroundTasks）中运行文件模式 ASR。
    识别进度通过 meeting WebSocket（meeting_ws_manager）推送到前端。
    """
    db = SessionLocal()
    try:
        # 标记为处理中
        session = db.query(VolcAsrSession).filter(VolcAsrSession.id == session_id).first()
        if session:
            session.status = "processing"
            db.commit()

        # 提前转换为 WAV 并保存到磁盘，确保 ASR 完成后本地文件仍然存在（供后续上传 TOS）
        actual_path = _ensure_wav_on_disk(file_path)
        if actual_path != file_path and session:
            session.audio_local_path = actual_path
            db.commit()

        accumulated: List[str] = []

        async def _run():
            url = "wss://openspeech.bytedance.com/api/v3/sauc/bigmodel"
            async with AsrWsClient(url, segment_duration=200, realtime=False) as client:
                async for response in client.execute(actual_path):
                    payload = response.payload_msg
                    text = _extract_text(payload) if payload else None
                    is_last = bool(response.is_last_package)
                    # definite=true 表示该句话已被 ASR 确认，不再变化；只累积确认结果避免重复
                    is_definite = bool((payload.get("result") or {}).get("definite", False)) if payload else False
                    should_accumulate = is_definite or is_last

                    if text:
                        if should_accumulate:
                            accumulated.append(text)

                        # 从 utterances 中提取时间信息
                        start_ms = end_ms = None
                        try:
                            utts = (payload.get("result") or {}).get("utterances") or []
                            if utts:
                                start_ms = utts[0].get("start_time")
                                end_ms = utts[-1].get("end_time")
                        except Exception:
                            pass

                        if should_accumulate:
                            inner_db = SessionLocal()
                            try:
                                _append_transcription(
                                    inner_db, meeting_id, session_id, text, is_last,
                                    start_ms, end_ms,
                                )
                            finally:
                                inner_db.close()

                        # 推送到前端 WebSocket（partial 也推，用于实时展示）
                        meeting_ws_manager.notify_from_thread(
                            meeting_id,
                            {
                                "type": "volc_asr_partial",
                                "session_id": session_id,
                                "text": text,
                                "is_final": is_definite,
                                "accumulated": "".join(accumulated),
                            },
                        )

        asyncio.run(_run())

        transcript = "".join(accumulated)
        _finalize_session(db, session_id, transcript, audio_local_path=actual_path)

        meeting_ws_manager.notify_from_thread(
            meeting_id,
            {
                "type": "volc_asr_completed",
                "session_id": session_id,
                "transcript": transcript,
            },
        )
        logger.info("File ASR completed session_id=%s meeting_id=%s", session_id, meeting_id)

    except Exception as exc:
        logger.exception("File ASR failed session_id=%s: %s", session_id, exc)
        try:
            _finalize_session(db, session_id, "", error_msg=str(exc))
        except Exception:
            pass
        meeting_ws_manager.notify_from_thread(
            meeting_id,
            {
                "type": "volc_asr_failed",
                "session_id": session_id,
                "error": str(exc),
            },
        )
    finally:
        db.close()


# ─── 实时模式 ASR（WebSocket Handler） ─────────────────────────────────────────

class LiveAsrHandler:
    """
    管理一次实时 WebSocket ASR 会话的全部生命周期。

    使用方法：
        handler = LiveAsrHandler(websocket, meeting_id, db)
        await handler.run()
    """

    def __init__(self, websocket, meeting_id: int, db: Session):
        self._ws = websocket        # FastAPI WebSocket
        self._meeting_id = meeting_id
        self._db = db
        self._session_id: Optional[int] = None
        self._audio_chunks: List[bytes] = []
        self._transcript_parts: List[str] = []
        self._sample_rate = 16000
        self._channels = 1
        self._sample_width = 2  # 16-bit
        self._ws_alive = True   # 前端连接是否仍然有效

    async def run(self) -> None:
        from fastapi.websockets import WebSocketDisconnect

        logger.info("LiveAsrHandler: accepting WS meeting_id=%s", self._meeting_id)
        await self._ws.accept()
        logger.info("LiveAsrHandler: WS accepted meeting_id=%s", self._meeting_id)

        # 新任务开始前清空旧的摘要/Todos/转写文本，保证本次录音与后续妙记一致
        try:
            from app.services.volc_minutes_service import volc_minutes_service
            volc_minutes_service.clear_minutes(self._db, self._meeting_id)
        except Exception as exc:
            logger.warning("Failed to clear minutes before live ASR meeting_id=%s: %s", self._meeting_id, exc)

        # 创建 ASR 会话记录
        session = _create_session(self._db, self._meeting_id, "live")
        self._session_id = session.id

        await self._ws.send_json({
            "type": "session_created",
            "session_id": self._session_id,
            "message": "实时ASR会话已创建，请开始发送音频数据",
        })
        logger.info("LiveAsrHandler: session_created sent meeting_id=%s session_id=%s", self._meeting_id, self._session_id)

        try:
            await self._process()
        except Exception as exc:
            logger.exception("LiveAsrHandler error session_id=%s: %s", self._session_id, exc)
            if self._ws_alive:
                try:
                    await self._ws.send_json({"type": "error", "message": str(exc)})
                except Exception:
                    self._ws_alive = False
            self._fail(str(exc))
        finally:
            try:
                await self._ws.close()
                logger.info("LiveAsrHandler: WS closed meeting_id=%s session_id=%s", self._meeting_id, self._session_id)
            except Exception as e:
                logger.debug("LiveAsrHandler: WS close exception: %s", e)

    async def _process(self) -> None:
        url = "wss://openspeech.bytedance.com/api/v3/sauc/bigmodel"
        stop_event = asyncio.Event()

        # 在连接 Volc ASR（可能需要数百毫秒）之前，先启动一个 Task 持续读取前端
        # 发来的消息并缓存，避免后端长时间不调用 receive() 导致代理/ASGI 把连接
        # 判定为空闲而关闭（"连上即断"问题的根因）。
        prefetch_chunks: List[bytes] = []
        prefetch_stop = asyncio.Event()
        prefetch_ctrl: List[dict] = []

        async def _prefetch_client() -> None:
            """在 Volc ASR 连接建立之前持续读取前端消息，防止连接被 proxy 断开。"""
            try:
                while not prefetch_stop.is_set():
                    try:
                        raw = await asyncio.wait_for(self._ws.receive(), timeout=30)
                    except asyncio.TimeoutError:
                        logger.warning("LiveAsrHandler: prefetch idle >30s meeting_id=%s", self._meeting_id)
                        prefetch_stop.set()
                        stop_event.set()
                        break
                    msg_type = raw.get("type", "")
                    if msg_type in ("websocket.disconnect", "websocket.close"):
                        logger.info("LiveAsrHandler: prefetch client disconnect meeting_id=%s", self._meeting_id)
                        self._ws_alive = False
                        prefetch_stop.set()
                        stop_event.set()
                        break
                    if "text" in raw and raw["text"]:
                        try:
                            ctrl = json.loads(raw["text"])
                            prefetch_ctrl.append(ctrl)
                            if ctrl.get("action") == "stop":
                                prefetch_stop.set()
                                stop_event.set()
                        except (json.JSONDecodeError, KeyError):
                            pass
                    elif "bytes" in raw and raw["bytes"]:
                        prefetch_chunks.append(raw["bytes"])
            except Exception as exc:
                logger.warning("LiveAsrHandler: prefetch error meeting_id=%s: %s", self._meeting_id, exc)
                prefetch_stop.set()
                stop_event.set()

        prefetch_task = asyncio.create_task(_prefetch_client())

        logger.info("LiveAsrHandler: connecting to Volc ASR meeting_id=%s session_id=%s", self._meeting_id, self._session_id)
        try:
            async with aiohttp.ClientSession() as http_session:
                headers = self._build_auth_headers()
                async with http_session.ws_connect(url, headers=headers) as volc_ws:
                    logger.info("LiveAsrHandler: Volc ASR WS connected meeting_id=%s session_id=%s", self._meeting_id, self._session_id)
                    # 发送初始化请求
                    seq = 1
                    init_req = self._build_full_client_request(seq)
                    await volc_ws.send_bytes(init_req)
                    seq += 1

                    # 等待服务端 ack
                    init_msg = await asyncio.wait_for(volc_ws.receive(), timeout=10)
                    if init_msg.type == aiohttp.WSMsgType.BINARY:
                        init_resp = ResponseParser.parse_response(init_msg.data)
                        logger.info("LiveAsrHandler: Volc ASR init ack received code=%s meeting_id=%s session_id=%s", init_resp.code, self._meeting_id, self._session_id)

                    # Volc ASR 就绪后停止 prefetch，切换为正式转发
                    prefetch_stop.set()
                    try:
                        await asyncio.wait_for(asyncio.shield(prefetch_task), timeout=1.0)
                    except (asyncio.TimeoutError, asyncio.CancelledError):
                        pass
                    prefetch_task.cancel()
                    try:
                        await prefetch_task
                    except asyncio.CancelledError:
                        pass

                    if stop_event.is_set():
                        # 前端在 Volc ASR 初始化期间已断开或发送了 stop
                        logger.info("LiveAsrHandler: client disconnected during Volc init meeting_id=%s session_id=%s", self._meeting_id, self._session_id)
                    else:
                        # 把 prefetch 缓存的 chunk 和控制消息交给正式的转发 task
                        send_task = asyncio.create_task(
                            self._forward_audio_to_volc(volc_ws, stop_event, seq,
                                                        prefetch_chunks=prefetch_chunks,
                                                        prefetch_ctrl=prefetch_ctrl)
                        )
                        recv_task = asyncio.create_task(
                            self._receive_from_volc(volc_ws)
                        )

                        done, pending = await asyncio.wait(
                            [send_task, recv_task],
                            return_when=asyncio.ALL_COMPLETED,
                        )
                        for t in pending:
                            t.cancel()
                            try:
                                await t
                            except asyncio.CancelledError:
                                pass
        finally:
            prefetch_task.cancel()
            try:
                await prefetch_task
            except asyncio.CancelledError:
                pass

        logger.info("LiveAsrHandler: Volc ASR done, saving audio meeting_id=%s session_id=%s chunks=%d",
                    self._meeting_id, self._session_id, len(self._audio_chunks))

        # 保存音频
        audio_path: Optional[str] = None
        duration: Optional[float] = None
        if self._audio_chunks:
            dest = _session_audio_path(self._meeting_id, self._session_id)
            try:
                duration = _save_pcm_as_wav(
                    self._audio_chunks, dest,
                    self._sample_rate, self._channels, self._sample_width,
                )
                audio_path = str(dest)
                logger.info("Live audio saved to %s (%.2fs)", audio_path, duration)
            except Exception as exc:
                logger.warning("Failed to save live audio: %s", exc)

        transcript = "".join(self._transcript_parts)
        _finalize_session(
            self._db, self._session_id, transcript,
            audio_local_path=audio_path,
            duration_seconds=duration,
        )

        # 自动上传到 TOS（供后续语音妙记使用），失败不阻断
        audio_id: Optional[int] = None
        if audio_path:
            try:
                from app.services.volc_minutes_service import volc_minutes_service
                audio_record = volc_minutes_service.upload_from_local(
                    db=self._db,
                    meeting_id=self._meeting_id,
                    local_path=Path(audio_path),
                    original_name=f"live_{self._session_id}.wav",
                    content_type="audio/wav",
                    source_asr_session_id=self._session_id,
                )
                # 同步粗转写文本到 audio 记录
                if transcript:
                    audio_record.transcript_text = transcript
                    self._db.commit()
                audio_id = audio_record.id
                logger.info("Live audio uploaded to TOS audio_id=%s", audio_id)
            except Exception as exc:
                logger.warning("Failed to upload live audio to TOS: %s", exc)

        if self._ws_alive:
            try:
                await self._ws.send_json({
                    "type": "completed",
                    "session_id": self._session_id,
                    "audio_id": audio_id,           # 供前端调用"提交妙记"按钮
                    "transcript": transcript,
                    "audio_saved": audio_path is not None,
                    "audio_uploaded": audio_id is not None,
                    "duration_seconds": duration,
                })
            except Exception:
                self._ws_alive = False
        logger.info("Live ASR completed session_id=%s audio_id=%s transcript_len=%d",
                    self._session_id, audio_id, len(transcript))

    async def _forward_audio_to_volc(
        self,
        volc_ws,
        stop_event: asyncio.Event,
        start_seq: int,
        prefetch_chunks: Optional[List[bytes]] = None,
        prefetch_ctrl: Optional[List[dict]] = None,
    ) -> None:
        """从前端 WebSocket 读取音频块，转发给 Volc ASR。"""
        from fastapi.websockets import WebSocketDisconnect
        seq = start_seq
        chunk_count = 0

        # 先处理 prefetch 阶段缓存的控制消息
        for ctrl in (prefetch_ctrl or []):
            if ctrl.get("action") == "config":
                self._sample_rate = ctrl.get("rate", self._sample_rate)
                self._channels = ctrl.get("channels", self._channels)
                self._sample_width = ctrl.get("sample_width", self._sample_width)
                logger.info("LiveAsrHandler: prefetch config applied rate=%s channels=%s", self._sample_rate, self._channels)

        # 先把 prefetch 阶段缓存的音频帧发出去
        for chunk in (prefetch_chunks or []):
            self._audio_chunks.append(chunk)
            chunk_count += 1
            if chunk_count == 1:
                logger.info("LiveAsrHandler: first audio chunk (from prefetch) meeting_id=%s session_id=%s len=%d",
                            self._meeting_id, self._session_id, len(chunk))
            req = self._build_audio_request(seq, chunk, is_last=False)
            await volc_ws.send_bytes(req)
            seq += 1

        if chunk_count:
            logger.info("LiveAsrHandler: flushed %d prefetch chunks meeting_id=%s session_id=%s",
                        chunk_count, self._meeting_id, self._session_id)

        try:
            while not stop_event.is_set():
                try:
                    raw = await asyncio.wait_for(self._ws.receive(), timeout=30)
                except asyncio.TimeoutError:
                    logger.warning("LiveAsrHandler: client idle >30s stopping meeting_id=%s session_id=%s chunks=%d",
                                   self._meeting_id, self._session_id, chunk_count)
                    stop_event.set()
                    break

                msg_type = raw.get("type", "")
                if msg_type in ("websocket.disconnect", "websocket.close"):
                    logger.info("LiveAsrHandler: client disconnect/close meeting_id=%s session_id=%s chunks=%d",
                               self._meeting_id, self._session_id, chunk_count)
                    self._ws_alive = False
                    stop_event.set()
                    break

                # 控制消息（JSON 文本）
                if "text" in raw and raw["text"]:
                    try:
                        ctrl = json.loads(raw["text"])
                        if ctrl.get("action") == "stop":
                            logger.info("LiveAsrHandler: client sent stop meeting_id=%s session_id=%s chunks=%d",
                                        self._meeting_id, self._session_id, chunk_count)
                            stop_event.set()
                        elif ctrl.get("action") == "config":
                            self._sample_rate = ctrl.get("rate", self._sample_rate)
                            self._channels = ctrl.get("channels", self._channels)
                            self._sample_width = ctrl.get("sample_width", self._sample_width)
                            logger.info("LiveAsrHandler: client config rate=%s channels=%s", self._sample_rate, self._channels)
                    except (json.JSONDecodeError, KeyError):
                        pass
                    continue

                # 二进制音频帧
                if "bytes" in raw and raw["bytes"]:
                    chunk: bytes = raw["bytes"]
                    self._audio_chunks.append(chunk)
                    chunk_count += 1
                    if chunk_count == 1 and not (prefetch_chunks):
                        logger.info("LiveAsrHandler: first audio chunk received meeting_id=%s session_id=%s len=%d",
                                    self._meeting_id, self._session_id, len(chunk))
                    req = self._build_audio_request(seq, chunk, is_last=False)
                    await volc_ws.send_bytes(req)
                    seq += 1

        except Exception as exc:
            logger.warning("LiveAsrHandler: forward_audio_to_volc error meeting_id=%s session_id=%s chunks=%d: %s",
                           self._meeting_id, self._session_id, chunk_count, exc)
            stop_event.set()
        finally:
            # 发送结束标志包
            try:
                end_req = self._build_audio_request(-seq, b"\x00" * 160, is_last=True)
                await volc_ws.send_bytes(end_req)
                logger.info("LiveAsrHandler: sent end packet to Volc meeting_id=%s session_id=%s total_chunks=%d",
                            self._meeting_id, self._session_id, chunk_count)
            except Exception as e:
                logger.warning("LiveAsrHandler: failed to send end packet: %s", e)

    async def _receive_from_volc(self, volc_ws) -> None:
        """从 Volc ASR 接收识别结果，推送给前端。"""
        recv_count = 0
        try:
            async for msg in volc_ws:
                if msg.type == aiohttp.WSMsgType.BINARY:
                    response = ResponseParser.parse_response(msg.data)
                    if response.code != 0:
                        logger.warning("LiveAsrHandler: Volc ASR error code=%s meeting_id=%s session_id=%s",
                                       response.code, self._meeting_id, self._session_id)
                        break

                    payload = response.payload_msg
                    text = _extract_text(payload) if payload else None
                    is_last = response.is_last_package
                    recv_count += 1
                    if recv_count == 1 and text:
                        logger.info("LiveAsrHandler: first ASR result received meeting_id=%s session_id=%s text_len=%d",
                                    self._meeting_id, self._session_id, len(text or ""))
                    # definite=true 表示该句话已确认，只累积确认结果避免重复
                    is_definite = bool((payload.get("result") or {}).get("definite", False)) if payload else False
                    should_accumulate = is_definite or is_last

                    if text:
                        if should_accumulate:
                            self._transcript_parts.append(text)
                            inner_db = SessionLocal()
                            try:
                                _append_transcription(
                                    inner_db,
                                    self._meeting_id,
                                    self._session_id,
                                    text,
                                    is_final=is_last,
                                )
                            finally:
                                inner_db.close()

                        if self._ws_alive:
                            try:
                                await self._ws.send_json({
                                    "type": "final" if is_definite else "partial",
                                    "text": text,
                                    "accumulated": "".join(self._transcript_parts),
                                })
                            except Exception:
                                self._ws_alive = False

                    if is_last:
                        logger.info("LiveAsrHandler: Volc ASR last package received meeting_id=%s session_id=%s recv_count=%d",
                                    self._meeting_id, self._session_id, recv_count)
                        break
                elif msg.type in (aiohttp.WSMsgType.ERROR, aiohttp.WSMsgType.CLOSED):
                    logger.info("LiveAsrHandler: Volc ASR WS ERROR/CLOSED meeting_id=%s session_id=%s msg_type=%s",
                                self._meeting_id, self._session_id, msg.type)
                    break
        except Exception as exc:
            logger.warning("LiveAsrHandler: receive_from_volc error meeting_id=%s session_id=%s: %s",
                           self._meeting_id, self._session_id, exc)

    def _fail(self, error_msg: str) -> None:
        try:
            _finalize_session(self._db, self._session_id, "", error_msg=error_msg)
        except Exception:
            pass

    # ── Volc 协议构建 ──────────────────────────────────────────────────────────

    @staticmethod
    def _build_auth_headers() -> dict:
        resource_id = getattr(settings, "VOLC_ASR_RESOURCE_ID", "volc.bigasr.sauc.duration")
        # 优先使用 VOLC_ASR_APP_KEY/VOLC_ASR_ACCESS_KEY，回退到妙记凭证
        app_key = getattr(settings, "VOLC_ASR_APP_KEY", "") or getattr(settings, "VOLC_MINUTES_APP_KEY", "")
        access_key = getattr(settings, "VOLC_ASR_ACCESS_KEY", "") or getattr(settings, "VOLC_MINUTES_ACCESS_KEY", "")
        return {
            "X-Api-Resource-Id": resource_id,
            "X-Api-Request-Id": str(uuid.uuid4()),
            "X-Api-Access-Key": access_key,
            "X-Api-App-Key": app_key,
        }

    @staticmethod
    def _build_full_client_request(seq: int) -> bytes:
        header = bytearray()
        header.append((ProtocolVersion.V1 << 4) | 1)
        header.append((MessageType.CLIENT_FULL_REQUEST << 4) | MessageTypeSpecificFlags.POS_SEQUENCE)
        header.append((SerializationType.JSON << 4) | CompressionType.GZIP)
        header.append(0x00)

        payload = {
            "user": {"uid": "live_user"},
            "audio": {
                "format": "pcm",
                "codec": "raw",
                "rate": 16000,
                "bits": 16,
                "channel": 1,
            },
            "request": {
                "model_name": "bigmodel",
                "enable_itn": True,
                "enable_punc": True,
                "enable_ddc": True,
                "show_utterances": True,
            },
        }
        payload_bytes = gzip.compress(json.dumps(payload).encode())
        req = bytes(header) + struct.pack(">i", seq) + struct.pack(">I", len(payload_bytes)) + payload_bytes
        return req

    @staticmethod
    def _build_audio_request(seq: int, chunk: bytes, is_last: bool) -> bytes:
        header = bytearray()
        header.append((ProtocolVersion.V1 << 4) | 1)
        flags = MessageTypeSpecificFlags.NEG_WITH_SEQUENCE if is_last else MessageTypeSpecificFlags.POS_SEQUENCE
        header.append((MessageType.CLIENT_AUDIO_ONLY_REQUEST << 4) | flags)
        header.append((SerializationType.NO_SERIALIZATION << 4) | CompressionType.GZIP)
        header.append(0x00)

        compressed = gzip.compress(chunk) if chunk else gzip.compress(b"")
        req = bytes(header) + struct.pack(">i", seq) + struct.pack(">I", len(compressed)) + compressed
        return req


# ─── TOS 音频文件 SSE 流式 ASR ──────────────────────────────────────────────────

async def stream_file_asr(
    audio_id: int,
    meeting_id: int,
    file_path: str,
) -> AsyncGenerator[str, None]:
    """
    对本地音频文件运行 SAUC 流式 ASR，以 SSE 格式 yield 识别结果。

    SSE 事件类型：
      - session_created : ASR 会话已创建
      - partial         : 实时识别片段（未确认，仅展示）
      - final           : 已确认的句子
      - completed       : 全部识别完成（含完整文本）
      - error           : 识别失败

    调用方负责在生成器结束后清理临时文件。
    """
    db = SessionLocal()
    session_id: Optional[int] = None
    accumulated: List[str] = []

    def _sse(data: dict) -> str:
        return f"data: {json.dumps(data, ensure_ascii=False)}\n\n"

    try:
        # 新任务开始前清空旧的摘要/Todos/转写文本，保证本次转写与后续妙记一致
        try:
            from app.services.volc_minutes_service import volc_minutes_service
            volc_minutes_service.clear_minutes(db, meeting_id)
        except Exception as _clr_exc:
            logger.warning("Failed to clear minutes before SSE ASR meeting_id=%s: %s", meeting_id, _clr_exc)

        session = _create_session(db, meeting_id, "file_tos")
        session_id = session.id
        session.status = "processing"
        db.commit()

        yield _sse({"type": "session_created", "session_id": session_id, "audio_id": audio_id})

        url = "wss://openspeech.bytedance.com/api/v3/sauc/bigmodel"
        async with AsrWsClient(url, segment_duration=200, realtime=False) as client:
            async for response in client.execute(file_path):
                payload = response.payload_msg
                text = _extract_text(payload) if payload else None
                is_last = bool(response.is_last_package)
                is_definite = bool((payload.get("result") or {}).get("definite", False)) if payload else False
                should_accumulate = is_definite or is_last

                if text:
                    if should_accumulate:
                        accumulated.append(text)
                        inner_db = SessionLocal()
                        try:
                            _append_transcription(inner_db, meeting_id, session_id, text, is_final=is_last)
                        finally:
                            inner_db.close()

                    yield _sse({
                        "type": "final" if is_definite else "partial",
                        "text": text,
                        "accumulated": "".join(accumulated),
                    })

        transcript = "".join(accumulated)
        _finalize_session(db, session_id, transcript, audio_local_path=file_path)

        # 同步更新 VolcMeetingAudio 的粗转写文本
        audio_record = db.query(VolcMeetingAudio).filter(VolcMeetingAudio.id == audio_id).first()
        if audio_record and transcript:
            audio_record.transcript_text = transcript
            db.commit()

        logger.info("SSE ASR completed session_id=%s audio_id=%s len=%d", session_id, audio_id, len(transcript))
        yield _sse({"type": "completed", "session_id": session_id, "audio_id": audio_id, "transcript": transcript})

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
