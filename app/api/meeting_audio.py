"""会议音频的上传任务、列表/详情/下载/删除及会议内广播 WebSocket。

HTTP 仅做鉴权、参数规范化与 StandardResponse/FileResponse；读写 meeting_audios 与 TOS 均在 meeting_audio_service。
provider=local|volc 区分业务线，物理共用 meeting_audios 表。
WebSocket `/audio/ws/{meeting_id}` 用于同会议多端推送，由 meeting_ws_manager 维护连接表。
排障：上传看 upload-task 与后台线程；下载/删除看 object_key 与 TOS 日志；WS 看连接与断开日志。
"""

from pathlib import Path
from typing import Iterator, List, Optional
from urllib.parse import quote

import httpx

from fastapi import APIRouter, Depends, File, HTTPException, Query, Request, UploadFile, WebSocket, WebSocketDisconnect, status
from fastapi.responses import FileResponse, StreamingResponse
from sqlalchemy.orm import Session
from starlette.background import BackgroundTask

from app.models import database, schemas
from app.models.schemas import StandardResponse
from app.services.meeting_audio_service import meeting_audio_service
from app.services.websocket_manager import meeting_ws_manager
from app.utils.auth import decode_access_token, get_current_user
from app.utils.logger import get_logger

logger = get_logger("meeting_audio_api")

router = APIRouter(prefix="/api/meetings", tags=["meetings"])


def _resolve_download_user(request: Request, token: Optional[str]) -> dict:
    bearer = (token or "").strip()
    if not bearer:
        auth_header = (request.headers.get("Authorization") or "").strip()
        if auth_header.lower().startswith("bearer "):
            bearer = auth_header[7:].strip()
    if not bearer:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="缺少访问令牌",
            headers={"WWW-Authenticate": "Bearer"},
        )
    payload = decode_access_token(bearer)
    user_id = payload.get("sub")
    if user_id is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="无效的令牌",
            headers={"WWW-Authenticate": "Bearer"},
        )
    return {"user_id": user_id, "username": payload.get("username"), "role": payload.get("role")}


def _attachment_disposition(file_name: str) -> str:
    safe_name = Path(file_name).name or "download"
    ascii_name = safe_name.encode("ascii", "ignore").decode("ascii") or "download"
    ascii_name = ascii_name.replace("\\", "_").replace('"', "_")
    return f'attachment; filename="{ascii_name}"; filename*=UTF-8\'\'{quote(safe_name)}'


# 创建异步上传任务，立即返回 task_id 供轮询。
# 1. normalize_provider 校验 local/volc。
# 2. 将上传流落临时文件，注册进程内任务，后台线程上传 TOS 并插入 meeting_audios。
# 3. 返回 MeetingAudioUploadTask（pending/running/…）。
@router.post("/audio/{meeting_id}/upload-task", response_model=StandardResponse[schemas.MeetingAudioUploadTask])
def create_upload_task(
    meeting_id: int,
    provider: str = Query(..., description="local 或 volc"),
    file: UploadFile = File(...),
    db: Session = Depends(database.get_db),
    current_user: dict = Depends(get_current_user),
):
    logger.info(
        "创建音频上传任务请求 meeting_id=%s provider=%s file_name=%s",
        meeting_id,
        provider,
        file.filename,
    )
    normalized_provider = meeting_audio_service.normalize_provider(provider)
    data = meeting_audio_service.create_upload_task(
        db=db,
        meeting_id=meeting_id,
        provider=normalized_provider,
        creator_id=current_user.get("user_id"),
        upload_file=file,
    )
    logger.info(
        "创建音频上传任务成功 meeting_id=%s provider=%s task_id=%s",
        meeting_id,
        normalized_provider,
        data.task_id,
    )
    return StandardResponse(success=True, data=data, message="音频上传任务已创建")


# 按 task_id 查询上传任务状态；完成后附带音频详情。
# 1. 从进程内 _upload_tasks 取快照。
# 2. 无记录则 404。
# 3. 若已完成则回查 DB 填充 audio 字段。
@router.get("/audio/upload-tasks/{task_id}", response_model=StandardResponse[schemas.MeetingAudioUploadTask])
def get_upload_task(
    task_id: str,
    db: Session = Depends(database.get_db),
    current_user: dict = Depends(get_current_user),
):
    logger.info("查询音频上传任务请求 task_id=%s", task_id)
    data = meeting_audio_service.get_upload_task(db, task_id)
    if not data:
        logger.warning("查询音频上传任务失败：任务不存在 task_id=%s", task_id)
        raise HTTPException(status_code=404, detail="上传任务不存在")
    logger.info("查询音频上传任务成功 task_id=%s status=%s", task_id, data.status)
    return StandardResponse(success=True, data=data, message="获取上传任务状态成功")


# 列出某会议下指定 provider 的全部音频元数据。
# 1. normalize_provider。
# 2. 校验会议存在后按 meeting_id、provider 查询 meeting_audios。
# 3. 返回 MeetingAudioInDB 列表（时间倒序）。
@router.get("/audio/{meeting_id}", response_model=StandardResponse[List[schemas.MeetingAudioInDB]])
def list_meeting_audio(
    meeting_id: int,
    provider: str = Query(..., description="local 或 volc"),
    db: Session = Depends(database.get_db),
    current_user: dict = Depends(get_current_user),
):
    logger.info("获取音频列表请求 meeting_id=%s provider=%s", meeting_id, provider)
    normalized_provider = meeting_audio_service.normalize_provider(provider)
    data = meeting_audio_service.list_audio(db, meeting_id, normalized_provider)
    logger.info(
        "获取音频列表成功 meeting_id=%s provider=%s count=%s",
        meeting_id,
        normalized_provider,
        len(data),
    )
    return StandardResponse(success=True, data=data, message="获取音频列表成功")


# 查询单条音频元数据。
# 1. normalize_provider。
# 2. 校验 meeting_id、provider、audio_id 三元组命中一行。
# 3. 返回 MeetingAudioInDB；未找到则 service 抛 404。
@router.get("/audio/{meeting_id}/{audio_id}", response_model=StandardResponse[schemas.MeetingAudioInDB])
def get_meeting_audio(
    meeting_id: int,
    audio_id: int,
    provider: str = Query(..., description="local 或 volc"),
    db: Session = Depends(database.get_db),
    current_user: dict = Depends(get_current_user),
):
    logger.info("获取音频详情请求 meeting_id=%s provider=%s audio_id=%s", meeting_id, provider, audio_id)
    normalized_provider = meeting_audio_service.normalize_provider(provider)
    data = meeting_audio_service.get_audio(db, meeting_id, normalized_provider, audio_id)
    logger.info(
        "获取音频详情成功 meeting_id=%s provider=%s audio_id=%s status=%s",
        meeting_id,
        normalized_provider,
        audio_id,
        data.status,
    )
    return StandardResponse(success=True, data=data, message="获取音频详情成功")


# 从 TOS 拉取音频文件并以附件形式响应。
# 1. normalize_provider 并定位记录与 object_key。
# 2. download_audio_to_temp 下载到本地临时文件。
# 3. FileResponse 返回文件，BackgroundTask 删除临时文件。
@router.get("/audio/download/{meeting_id}/{audio_id}")
def download_meeting_audio(
    meeting_id: int,
    audio_id: int,
    provider: str = Query(..., description="local 或 volc"),
    db: Session = Depends(database.get_db),
    current_user: dict = Depends(get_current_user),
):
    logger.info("下载音频请求 meeting_id=%s provider=%s audio_id=%s", meeting_id, provider, audio_id)
    normalized_provider = meeting_audio_service.normalize_provider(provider)
    tmp_file_path, file_name, file_type = meeting_audio_service.download_audio_to_temp(
        db, meeting_id, normalized_provider, audio_id
    )
    logger.info(
        "下载音频准备完成 meeting_id=%s provider=%s audio_id=%s temp_path=%s",
        meeting_id,
        normalized_provider,
        audio_id,
        tmp_file_path,
    )

    def cleanup() -> None:
        if tmp_file_path.exists():
            tmp_file_path.unlink(missing_ok=True)
            logger.info("下载临时音频清理 path=%s", tmp_file_path)

    return FileResponse(
        path=str(tmp_file_path),
        filename=file_name,
        media_type=file_type,
        background=BackgroundTask(cleanup),
    )


@router.get("/audio/direct-download/{meeting_id}/{audio_id}")
def direct_download_meeting_audio(
    request: Request,
    meeting_id: int,
    audio_id: int,
    provider: str = Query(..., description="当前仅支持 local"),
    token: Optional[str] = Query(None, description="浏览器直链下载时使用的访问令牌"),
    db: Session = Depends(database.get_db),
):
    current_user = _resolve_download_user(request, token)
    normalized_provider = meeting_audio_service.normalize_provider(provider)
    if normalized_provider != "local":
        raise HTTPException(status_code=400, detail="该直链下载接口当前仅支持机密会议音频")
    logger.info(
        "直链下载音频请求 meeting_id=%s provider=%s audio_id=%s user_id=%s",
        meeting_id,
        normalized_provider,
        audio_id,
        current_user.get("user_id"),
    )
    file_url, file_name, file_type = meeting_audio_service.get_audio_download_source(
        db, meeting_id, normalized_provider, audio_id
    )
    if not file_url:
        logger.info(
            "直链下载缺少 file_url，回退旧链路 meeting_id=%s provider=%s audio_id=%s",
            meeting_id,
            normalized_provider,
            audio_id,
        )
        tmp_file_path, file_name, file_type = meeting_audio_service.download_audio_to_temp(
            db, meeting_id, normalized_provider, audio_id
        )

        def cleanup() -> None:
            if tmp_file_path.exists():
                tmp_file_path.unlink(missing_ok=True)
                logger.info("直链下载回退临时音频清理 path=%s", tmp_file_path)

        return FileResponse(
            path=str(tmp_file_path),
            filename=file_name,
            media_type=file_type,
            background=BackgroundTask(cleanup),
        )

    client = httpx.Client(follow_redirects=True, timeout=httpx.Timeout(connect=10.0, read=None, write=None, pool=None))
    try:
        upstream = client.send(client.build_request("GET", file_url), stream=True)
        upstream.raise_for_status()
    except Exception as exc:
        client.close()
        logger.warning(
            "直链下载快路径失败，回退旧链路，模式=%s，会议ID=%s，音频ID=%s，file_url=%s，错误=%s",
            normalized_provider,
            meeting_id,
            audio_id,
            file_url,
            exc,
        )
        tmp_file_path, file_name, file_type = meeting_audio_service.download_audio_to_temp(
            db, meeting_id, normalized_provider, audio_id
        )

        def cleanup() -> None:
            if tmp_file_path.exists():
                tmp_file_path.unlink(missing_ok=True)
                logger.info("直链下载回退临时音频清理 path=%s", tmp_file_path)

        return FileResponse(
            path=str(tmp_file_path),
            filename=file_name,
            media_type=file_type,
            background=BackgroundTask(cleanup),
        )

    def iter_remote() -> Iterator[bytes]:
        try:
            for chunk in upstream.iter_bytes():
                if chunk:
                    yield chunk
        finally:
            upstream.close()
            client.close()

    headers = {
        "Content-Disposition": _attachment_disposition(file_name),
        "Cache-Control": "no-store",
    }
    content_length = upstream.headers.get("content-length")
    if content_length:
        headers["Content-Length"] = content_length

    logger.info(
        "直链下载音频准备完成 meeting_id=%s provider=%s audio_id=%s file_url=%s",
        meeting_id,
        normalized_provider,
        audio_id,
        file_url,
    )
    return StreamingResponse(
        iter_remote(),
        media_type=file_type,
        headers=headers,
    )


# 删除 TOS 对象与 meeting_audios 行，返回删除前快照。
# 1. normalize_provider 并加载记录。
# 2. 有 object_key 则 delete_object。
# 3. DB delete 并 commit，响应 MeetingAudioInDB。
@router.delete("/audio/{meeting_id}/{audio_id}", response_model=StandardResponse[schemas.MeetingAudioInDB])
def delete_meeting_audio(
    meeting_id: int,
    audio_id: int,
    provider: str = Query(..., description="local 或 volc"),
    db: Session = Depends(database.get_db),
    current_user: dict = Depends(get_current_user),
):
    logger.info("删除音频请求 meeting_id=%s provider=%s audio_id=%s", meeting_id, provider, audio_id)
    normalized_provider = meeting_audio_service.normalize_provider(provider)
    data = meeting_audio_service.delete_audio(db, meeting_id, normalized_provider, audio_id)
    logger.info("删除音频成功 meeting_id=%s provider=%s audio_id=%s", meeting_id, normalized_provider, audio_id)
    return StandardResponse(success=True, data=data, message="音频删除成功")


# 会议维度音频相关事件的广播通道（文本心跳保活）。
# 1. accept 后加入 meeting_ws_manager 的连接池。
# 2. 循环 receive_text 维持连接。
# 3. 断开或异常时 disconnect 释放槽位。
@router.websocket("/audio/ws/{meeting_id}")
async def meeting_audio_ws(meeting_id: int, websocket: WebSocket):
    logger.info("会议音频 WS 连接尝试 meeting_id=%s client=%s", meeting_id, websocket.client)
    await meeting_ws_manager.connect(meeting_id, websocket)
    logger.info("会议音频 WS 连接建立 meeting_id=%s client=%s", meeting_id, websocket.client)
    try:
        while True:
            await websocket.receive_text()
    except WebSocketDisconnect:
        logger.info("会议音频 WS 主动断开 meeting_id=%s client=%s", meeting_id, websocket.client)
        await meeting_ws_manager.disconnect(meeting_id, websocket)
    except Exception:
        logger.exception("会议音频 WS 处理异常 meeting_id=%s client=%s", meeting_id, websocket.client)
        await meeting_ws_manager.disconnect(meeting_id, websocket)
