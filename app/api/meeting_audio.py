"""统一会议音频 API。

该控制器承接会议音频的统一入口，local 与 volc 两种模式共用同一套路由。
控制器层只负责 HTTP / WebSocket 协议转换，音频表读写与对象存储操作统一交给
`meeting_audio_service` 处理。

运维排障建议：
1. 上传问题优先看 upload-task 创建日志和后台任务日志。
2. 下载或删除失败优先看 object_key 和对象存储相关日志。
3. WebSocket 广播问题优先看连接建立和断开日志。
"""

from typing import List

from fastapi import APIRouter, Depends, File, HTTPException, Query, UploadFile, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse
from sqlalchemy.orm import Session
from starlette.background import BackgroundTask

from app.models import database, schemas
from app.models.schemas import StandardResponse
from app.services.meeting_audio_service import meeting_audio_service
from app.services.websocket_manager import meeting_ws_manager
from app.utils.auth import get_current_user
from app.utils.logger import get_logger

logger = get_logger("meeting_audio_api")

router = APIRouter(prefix="/api/meetings", tags=["meetings"])


# 处理流程（创建上传任务）：
# 1. 标准化 provider 参数。
# 2. 交给 service 创建后台上传任务。
# 3. 立即返回任务快照给前端轮询。
@router.post("/audio/{meeting_id}/upload-task", response_model=StandardResponse[schemas.MeetingAudioUnifiedInDB])
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
        upload_file=file,
    )
    logger.info(
        "创建音频上传任务成功 meeting_id=%s provider=%s task_id=%s",
        meeting_id,
        normalized_provider,
        data.task_id,
    )
    return StandardResponse(success=True, data=data, message="音频上传任务已创建")


# 处理流程（查询上传任务）：
# 1. 读取进程内任务快照。
# 2. 任务不存在时返回 404。
# 3. 返回任务态或已完成的音频详情。
@router.get("/audio/upload-tasks/{task_id}", response_model=StandardResponse[schemas.MeetingAudioUnifiedInDB])
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


# 处理流程（音频列表）：
# 1. 标准化 provider 参数。
# 2. 查询 meeting + provider 维度的音频记录。
# 3. 返回统一音频列表模型。
@router.get("/audio/{meeting_id}", response_model=StandardResponse[List[schemas.MeetingAudioUnifiedInDB]])
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


# 处理流程（音频详情）：
# 1. 标准化 provider 参数。
# 2. 校验音频归属关系。
# 3. 返回单条统一音频模型。
@router.get("/audio/{meeting_id}/{audio_id}", response_model=StandardResponse[schemas.MeetingAudioUnifiedInDB])
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


# 处理流程（音频下载）：
# 1. 标准化 provider 参数。
# 2. 将对象存储文件下载到临时路径。
# 3. 响应完成后清理临时文件。
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
        try:
            if tmp_file_path.exists():
                tmp_file_path.unlink()
                logger.info("下载临时音频清理成功 path=%s", tmp_file_path)
        except Exception as exc:
            logger.warning("下载临时音频清理失败 path=%s error=%s", tmp_file_path, exc)

    return FileResponse(
        path=str(tmp_file_path),
        filename=file_name,
        media_type=file_type,
        background=BackgroundTask(cleanup),
    )


# 处理流程（音频删除）：
# 1. 标准化 provider 参数。
# 2. 校验音频归属关系并删除对象存储文件。
# 3. 删除数据库记录并返回删除前快照。
@router.delete("/audio/{meeting_id}/{audio_id}", response_model=StandardResponse[schemas.MeetingAudioUnifiedInDB])
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


# 处理流程（会议音频广播 WebSocket）：
# 1. 建立 meeting 维度的广播连接。
# 2. 持续接收文本心跳保持连接活跃。
# 3. 断开或异常时主动清理连接池。
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
