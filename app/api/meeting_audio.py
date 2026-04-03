"""meeting_domain - 统一会议音频 API。

这份路由承担所有 provider 共用的音频资产能力：
1. 创建异步上传任务。
2. 查询上传任务状态。
3. 查询音频列表/详情。
4. 下载与删除音频。
5. 建立会议级音频状态广播 WebSocket。

设计原则：
1. local / volc 两种模式共用同一套路由，通过 `provider` 参数区分。
2. API 层不直接操作对象存储 SDK，只通过 `meeting_audio_service` 间接访问。
3. 上传任务、下载临时文件、WebSocket 广播等横切能力统一收敛在这里，避免散落在纪要路由中。
"""

from typing import List

from fastapi import APIRouter, Depends, File, HTTPException, Query, UploadFile, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse
from sqlalchemy.orm import Session
from starlette.background import BackgroundTask

from app.models.meeting_domain import database, schemas
from app.models.schemas import StandardResponse
from app.services.meeting_domain.meeting_audio_service import meeting_audio_service
from app.services.meeting_domain.websocket_manager import meeting_ws_manager
from app.utils.auth import get_current_user
from app.utils.logger import get_logger

logger = get_logger("meeting_audio_api")

router = APIRouter(prefix="/api/meetings", tags=["meetings"])


# 步骤说明（统一上传任务创建，local/volc 复用）：
# 1) 读取 provider 并做标准化校验；
# 2) 创建后台上传任务并立即返回任务快照；
# 3) 前端使用 task_id 轮询统一任务查询接口。
@router.post("/audio/{meeting_id}/upload-task", response_model=StandardResponse[schemas.MeetingAudioUnifiedInDB])
def create_upload_task(
    meeting_id: int,
    provider: str = Query(..., description="local 或 volc"),
    file: UploadFile = File(...),
    db: Session = Depends(database.get_db),
    current_user: dict = Depends(get_current_user),
):
    # 先做 provider 归一化，再由 service 统一处理上传任务生命周期。
    normalized_provider = meeting_audio_service.normalize_provider(provider)
    data = meeting_audio_service.create_upload_task(
        db=db,
        meeting_id=meeting_id,
        provider=normalized_provider,
        upload_file=file,
    )
    return StandardResponse(success=True, data=data, message="音频上传任务已创建")


# 步骤说明（统一上传任务查询，local/volc 复用）：
# 1) 先查询任务快照，不存在则返回 404；
# 2) 已完成且存在 audio_id 时，读取完整音频元数据并补齐 task 字段；
# 3) 未完成时返回任务态最小字段，前端可直接轮询展示进度。
@router.get("/audio/upload-tasks/{task_id}", response_model=StandardResponse[schemas.MeetingAudioUnifiedInDB])
def get_upload_task(
    task_id: str,
    db: Session = Depends(database.get_db),
    current_user: dict = Depends(get_current_user),
):
    # 任务查询必须返回统一音频模型，前端才能用同一张卡片展示“处理中”和“已完成”两种状态。
    data = meeting_audio_service.get_upload_task(db, task_id)
    if not data:
        raise HTTPException(status_code=404, detail="上传任务不存在")
    return StandardResponse(success=True, data=data, message="获取上传任务状态成功")


# 步骤说明（音频列表查询）：
# 1) 解析 provider；
# 2) 查询 meeting + provider 维度的音频记录；
# 3) 统一返回列表模型，便于前端按 provider 维度切换视图。
@router.get("/audio/{meeting_id}", response_model=StandardResponse[List[schemas.MeetingAudioUnifiedInDB]])
def list_meeting_audio(
    meeting_id: int,
    provider: str = Query(..., description="local 或 volc"),
    db: Session = Depends(database.get_db),
    current_user: dict = Depends(get_current_user),
):
    # provider 维度是显式设计：同一会议可以同时保留 local / volc 两套音频资产。
    normalized_provider = meeting_audio_service.normalize_provider(provider)
    data = meeting_audio_service.list_audio(db, meeting_id, normalized_provider)
    return StandardResponse(success=True, data=data, message="获取音频列表成功")


# 步骤说明（单音频详情查询）：
# 1) 解析 provider；
# 2) 校验会议与音频归属关系；
# 3) 返回单条统一音频模型。
@router.get("/audio/{meeting_id}/{audio_id}", response_model=StandardResponse[schemas.MeetingAudioUnifiedInDB])
def get_meeting_audio(
    meeting_id: int,
    audio_id: int,
    provider: str = Query(..., description="local 或 volc"),
    db: Session = Depends(database.get_db),
    current_user: dict = Depends(get_current_user),
):
    # 详情接口仍复用统一模型，保持与列表、任务查询的字段一致性。
    normalized_provider = meeting_audio_service.normalize_provider(provider)
    data = meeting_audio_service.get_audio(db, meeting_id, normalized_provider, audio_id)
    return StandardResponse(success=True, data=data, message="获取音频详情成功")


# 步骤说明（音频下载）：
# 1) 从对象存储下载到临时文件；
# 2) 通过 FileResponse 回传给客户端；
# 3) 响应完成后自动清理临时文件，避免磁盘残留。
@router.get("/audio/download/{meeting_id}/{audio_id}")
def download_meeting_audio(
    meeting_id: int,
    audio_id: int,
    provider: str = Query(..., description="local 或 volc"),
    db: Session = Depends(database.get_db),
    current_user: dict = Depends(get_current_user),
):
    normalized_provider = meeting_audio_service.normalize_provider(provider)
    tmp_file_path, file_name, file_type = meeting_audio_service.download_audio_to_temp(
        db, meeting_id, normalized_provider, audio_id
    )

    def cleanup() -> None:
        # 下载接口只负责生成临时文件，真正的清理由响应完成后的回调统一负责。
        try:
            if tmp_file_path.exists():
                tmp_file_path.unlink()
        except Exception as exc:
            logger.warning("清理临时音频文件失败，路径=%s，错误=%s", tmp_file_path, exc)

    return FileResponse(
        path=str(tmp_file_path),
        filename=file_name,
        media_type=file_type,
        background=BackgroundTask(cleanup),
    )


# 步骤说明（音频删除）：
# 1) 校验 meeting/provider/audio 三元归属；
# 2) 删除对象存储文件；
# 3) 删除数据库记录并返回删除前数据快照。
@router.delete("/audio/{meeting_id}/{audio_id}", response_model=StandardResponse[schemas.MeetingAudioUnifiedInDB])
def delete_meeting_audio(
    meeting_id: int,
    audio_id: int,
    provider: str = Query(..., description="local 或 volc"),
    db: Session = Depends(database.get_db),
    current_user: dict = Depends(get_current_user),
):
    normalized_provider = meeting_audio_service.normalize_provider(provider)
    data = meeting_audio_service.delete_audio(db, meeting_id, normalized_provider, audio_id)
    return StandardResponse(success=True, data=data, message="音频删除成功")


# 步骤说明（会议音频 WS 广播连接）：
# 1) 客户端建立连接后登记到 meeting 维度连接池；
# 2) 通过心跳/任意文本维持连接；
# 3) 断开或异常时做连接清理。
@router.websocket("/audio/ws/{meeting_id}")
async def meeting_audio_ws(meeting_id: int, websocket: WebSocket):
    # 该 WS 不承载音频二进制，只用作会议级状态广播通道。
    # 客户端只要保持连接并周期性发文本心跳，即可收到后台线程推送的任务状态。
    logger.info("会议音频WS连接尝试，会议ID=%s，客户端=%s", meeting_id, websocket.client)
    await meeting_ws_manager.connect(meeting_id, websocket)
    try:
        while True:
            await websocket.receive_text()
    except WebSocketDisconnect:
        logger.info("会议音频WS连接断开，会议ID=%s", meeting_id)
        await meeting_ws_manager.disconnect(meeting_id, websocket)
    except Exception:
        logger.exception("会议音频WS处理异常，会议ID=%s", meeting_id)
        await meeting_ws_manager.disconnect(meeting_id, websocket)
