
# api/meetings.py

import logging
from typing import List
from pathlib import Path
import shutil
import time

from fastapi import APIRouter, Depends, HTTPException, UploadFile, File, Body, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse
from sqlalchemy.orm import Session

from app.models import schemas2
from app.services.meeting_service import MeetingService, FileService, AudioService
from app.models.database import get_db
from app.utils.auth import get_current_user
from app.services import transcription_service
from app.services.websocket_manager import meeting_ws_manager
from app.models.schemas import StandardResponse

# 创建路由实例，设置前缀和标签
router = APIRouter(prefix="/api/meetings", tags=["meetings"])

# 创建日志记录器
logger = logging.getLogger(__name__)

# -------- 服务实例（关键改动） --------
meeting_service = MeetingService()
file_service = FileService()
audio_service = AudioService()
# -----------------------------------


# 创建会议接口
@router.post("", response_model=StandardResponse[schemas2.MeetingInDB])
def create_meeting(
    meeting: schemas2.MeetingCreate,
    db: Session = Depends(get_db),
    current_user: dict = Depends(get_current_user),
):
    user_id = current_user.get("user_id")
    logger.info(f"创建新会议，标题: {meeting.title}，创建者: {user_id}")
    # 将创建者 user_id 传入服务层保存到 Meeting.creator_id
    result = meeting_service.create_meeting(db, meeting, creator_id=user_id)
    logger.info(f"成功创建会议，ID: {result.id}")
    return StandardResponse(success=True, data=result, message="会议创建成功")



# 获取所有会议
@router.get("", response_model=StandardResponse[List[schemas2.MeetingInDB]])
def get_all_meetings(
    db: Session = Depends(get_db),
    current_user: dict = Depends(get_current_user),
):
    logger.info("获取所有会议列表")
    meetings = meeting_service.get_all_meetings(db)
    return StandardResponse(success=True, data=meetings, message="获取会议列表成功")


# 获取当前用户的所有会议
@router.get("/mine", response_model=StandardResponse[List[schemas2.MeetingInDB]])
def get_my_meetings(db: Session = Depends(get_db), current_user: dict = Depends(get_current_user)):
    user_id = current_user.get("user_id")
    logger.info(f"获取用户 {user_id} 创建的会议列表")
    meetings = meeting_service.get_meetings_by_creator(db, user_id)
    return StandardResponse(success=True, data=meetings, message="获取会议列表成功")


# 根据 user_id 获取该用户所有会议
@router.get("/user/{user_id}", response_model=StandardResponse[List[schemas2.MeetingInDB]])
def get_user_meetings(
    user_id: str,
    db: Session = Depends(get_db),
    current_user: dict = Depends(get_current_user),
):
    logger.info(f"获取用户 {user_id} 创建的会议列表（管理员视角）")
    meetings = meeting_service.get_meetings_by_creator(db, user_id)
    return StandardResponse(success=True, data=meetings, message="获取会议列表成功")


# 获取会议详情接口
@router.get("/{meeting_id}", response_model=StandardResponse[schemas2.MeetingInDB])
def get_meeting(
    meeting_id: int,
    db: Session = Depends(get_db),
    current_user: dict = Depends(get_current_user),
):
    logger.info(f"获取会议详情，会议ID: {meeting_id}")
    db_meeting = meeting_service.get_meeting(db, meeting_id)
    if not db_meeting:
        logger.warning(f"未找到会议，会议ID: {meeting_id}")
        raise HTTPException(status_code=404, detail="会议未找到")
    logger.info(f"成功获取会议详情，会议ID: {meeting_id}")
    return StandardResponse(success=True, data=db_meeting, message="获取会议详情成功")


# 更新会议信息接口
@router.put("/{meeting_id}", response_model=StandardResponse[schemas2.MeetingInDB])
def update_meeting(
    meeting_id: int,
    meeting: schemas2.MeetingUpdate,
    db: Session = Depends(get_db),
    current_user: dict = Depends(get_current_user),
):
    logger.info(f"更新会议信息，会议ID: {meeting_id}")
    db_meeting = meeting_service.update_meeting(db, meeting_id, meeting)
    if not db_meeting:
        logger.warning(f"未找到会议进行更新，会议ID: {meeting_id}")
        raise HTTPException(status_code=404, detail="会议未找到")
    logger.info(f"成功更新会议信息，会议ID: {meeting_id}")
    return StandardResponse(success=True, data=db_meeting, message="会议更新成功")


# 删除会议接口
@router.delete("/{meeting_id}", response_model=StandardResponse[None])
def delete_meeting(
    meeting_id: int,
    db: Session = Depends(get_db),
    current_user: dict = Depends(get_current_user),
):
    logger.info(f"删除会议，会议ID: {meeting_id}")
    # 先查找所有相关文件
    files = file_service.get_files_by_meeting(db, meeting_id)
    # 删除相关音频记录与文件
    if not audio_service.delete_audios_by_meeting(db, meeting_id):
        raise HTTPException(status_code=500, detail="删除会议音频失败")

    # 删除数据库中的会议及文件记录
    success = meeting_service.delete_meeting(db, meeting_id)
    if not success:
        logger.warning(f"未找到会议进行删除，会议ID: {meeting_id}")
        raise HTTPException(status_code=404, detail="会议未找到")
    # 删除磁盘上的文件夹
    try:
        repo_root = Path(__file__).resolve().parents[2]
        storage_dir = repo_root / "meeting_files" / str(meeting_id)
        if storage_dir.exists():
            shutil.rmtree(storage_dir)
            logger.info(f"已删除会议文件目录: {storage_dir}")
    except Exception as e:
        logger.warning(f"删除会议文件目录失败: {e}")
    logger.info(f"成功删除会议，会议ID: {meeting_id}")
    return StandardResponse(success=True, data=None, message="会议及相关文件与音频已删除")


# 上传会议文件接口（支持一次上传多个文件）
@router.post("/files/{meeting_id}", response_model=StandardResponse[List[schemas2.MeetingFileInDB]])
def upload_meeting_file(
    meeting_id: int,
    uploaded_files: List[UploadFile] = File(...),
    db: Session = Depends(get_db),
    current_user: dict = Depends(get_current_user),
):
    """接收浏览器上传的多个文件，保存到仓库根目录下的 `meeting_files/{meeting_id}/uploads` 下，并记录数据库。

    返回值为已保存的文件记录列表。
    """
    logger.info(f"上传会议文件，会议ID: {meeting_id}，文件数量: {len(uploaded_files)}")

    # 限制上传文件数量
    if len(uploaded_files) > 5:
        logger.warning(f"上传文件超过限制: {len(uploaded_files)} > 5")
        raise HTTPException(status_code=400, detail="最多上传5个文件")

    # 允许的后缀
    allowed_exts = {'.pdf', '.docx', '.doc', '.pptx', '.ppt', '.txt'}

    # 验证会议是否存在
    db_meeting = meeting_service.get_meeting(db, meeting_id)
    if not db_meeting:
        logger.warning(f"上传文件时未找到会议，会议ID: {meeting_id}")
        raise HTTPException(status_code=404, detail="会议未找到")

    # 计算存储目录：仓库根目录下的 meeting_files/{meeting_id}/uploads
    repo_root = Path(__file__).resolve().parents[2]
    storage_dir = repo_root / "meeting_files" / str(meeting_id) / "uploads"
    storage_dir.mkdir(parents=True, exist_ok=True)

    saved_records = []
    timestamp_base = int(time.time())
    for idx, uploaded_file in enumerate(uploaded_files):
        safe_name = Path(uploaded_file.filename).name
        ext = Path(safe_name).suffix.lower()
        if ext not in allowed_exts:
            logger.warning(f"不允许的文件类型: {safe_name}")
            raise HTTPException(status_code=400, detail=f"不支持的文件格式: {ext}")
        timestamp = f"{timestamp_base}_{idx}"
        stored_name = f"{timestamp}_{safe_name}"
        dest_path = storage_dir / stored_name

        try:
            with dest_path.open("wb") as out_file:
                shutil.copyfileobj(uploaded_file.file, out_file)
        finally:
            try:
                uploaded_file.file.close()
            except Exception:
                pass

        # 构造数据库记录并保存
        file_record = schemas2.MeetingFileCreate(
            meeting_id=meeting_id,
            filename=safe_name,
            file_type=uploaded_file.content_type or "",
            file_path=str(dest_path),
        )
        result = file_service.create_file(db, file_record)
        logger.info(f"成功上传文件，文件ID: {result.id}，会议ID: {meeting_id}，保存路径: {dest_path}")
        saved_records.append(result)

    return StandardResponse(success=True, data=saved_records, message="文件上传成功")


# 列出某会议下的所有文件（与上传端点同一路径，不同方法）
@router.get("/files/{meeting_id}", response_model=StandardResponse[List[schemas2.MeetingFileInDB]])
def list_meeting_files(
    meeting_id: int,
    db: Session = Depends(get_db),
    current_user: dict = Depends(get_current_user),
):
    # 验证会议是否存在
    db_meeting = meeting_service.get_meeting(db, meeting_id)
    if not db_meeting:
        raise HTTPException(status_code=404, detail="会议未找到")
    files = file_service.get_files_by_meeting(db, meeting_id)
    return StandardResponse(success=True, data=files, message="获取会议文件成功")

# 会议文件详情接口
@router.get("/files/{meeting_id}/{file_id}", response_model=StandardResponse[schemas2.MeetingFileInDB])
def get_meeting_file_detail(
    meeting_id: int,
    file_id: int,
    db: Session = Depends(get_db),
    current_user: dict = Depends(get_current_user),
):
    """返回单个会议文件的元信息。"""
    db_meeting = meeting_service.get_meeting(db, meeting_id)
    if not db_meeting:
        raise HTTPException(status_code=404, detail="会议未找到")
    db_file = file_service.get_file_by_id(db, file_id)
    if not db_file or db_file.meeting_id != meeting_id:
        raise HTTPException(status_code=404, detail="文件未找到")
    return StandardResponse(success=True, data=db_file, message="获取文件详情成功")



# 下载单个会议文件
@router.get("/files/download/{meeting_id}/{file_id}")
def download_meeting_file(
    meeting_id: int,
    file_id: int,
    db: Session = Depends(get_db),
    current_user: dict = Depends(get_current_user),
):
    # 验证会议存在
    db_meeting = meeting_service.get_meeting(db, meeting_id)
    if not db_meeting:
        raise HTTPException(status_code=404, detail="会议未找到")

    db_file = file_service.get_file_by_id(db, file_id)
    if not db_file or db_file.meeting_id != meeting_id:
        raise HTTPException(status_code=404, detail="文件未找到")

    file_path = db_file.file_path
    if not file_path:
        raise HTTPException(status_code=404, detail="文件路径未记录")

    path_obj = Path(file_path)
    if not path_obj.exists():
        raise HTTPException(status_code=404, detail="服务器上未找到文件")

    return FileResponse(path=str(path_obj), filename=db_file.filename, media_type=db_file.file_type or "application/octet-stream")


# 删除单个会议文件（删除数据库记录并删除磁盘文件）
@router.delete("/files/{meeting_id}/{file_id}", response_model=StandardResponse[None])
def delete_meeting_file(
    meeting_id: int,
    file_id: int,
    db: Session = Depends(get_db),
    current_user: dict = Depends(get_current_user),
):
    # 验证会议存在
    db_meeting = meeting_service.get_meeting(db, meeting_id)
    if not db_meeting:
        raise HTTPException(status_code=404, detail="会议未找到")

    db_file = file_service.get_file_by_id(db, file_id)
    if not db_file or db_file.meeting_id != meeting_id:
        raise HTTPException(status_code=404, detail="文件未找到")

    # 删除磁盘文件（如果存在）
    file_path = db_file.file_path
    try:
        if file_path:
            p = Path(file_path)
            if p.exists():
                p.unlink()
    except Exception as e:
        logger.warning(f"删除磁盘文件时出错: {e}")

    # 删除数据库记录
    success = file_service.delete_file_record(db, file_id)
    if not success:
        raise HTTPException(status_code=500, detail="删除数据库记录失败")

    return StandardResponse(success=True, data=None, message="文件删除成功")


# 上传会议音频接口（一次可上传一个或多个）
@router.post("/audio/{meeting_id}", response_model=StandardResponse[List[schemas2.MeetingAudioInDB]])
def upload_meeting_audio(
    meeting_id: int,
    uploaded_files: List[UploadFile] = File(...),
    db: Session = Depends(get_db),
    current_user: dict = Depends(get_current_user),
):
    """上传一个或多个会议音频，保存到 `meeting_files/{meeting_id}/audio`，并在后台转写为文字保存到转写表。"""
    logger.info(f"上传会议音频，会议ID: {meeting_id}，文件数量: {len(uploaded_files)}")

    # 验证会议是否存在
    db_meeting = meeting_service.get_meeting(db, meeting_id)
    if not db_meeting:
        logger.warning(f"上传音频时未找到会议，会议ID: {meeting_id}")
        raise HTTPException(status_code=404, detail="会议未找到")

    # 允许的音频后缀
    allowed_exts = {'.mp3', '.wav', '.m4a', '.flac', '.aac', '.ogg'}

    # 存储目录
    repo_root = Path(__file__).resolve().parents[2]
    storage_dir = repo_root / "meeting_files" / str(meeting_id) / "audio"
    storage_dir.mkdir(parents=True, exist_ok=True)

    saved_records = []
    for idx, uploaded_file in enumerate(uploaded_files):
        safe_name = Path(uploaded_file.filename).name
        ext = Path(safe_name).suffix.lower()
        if ext not in allowed_exts:
            logger.warning(f"不允许的音频类型: {safe_name}")
            raise HTTPException(status_code=400, detail=f"不支持的音频格式: {ext}")

        timestamp = int(time.time())
        stored_name = f"{timestamp}_{idx}_{safe_name}"
        dest_path = storage_dir / stored_name

        try:
            with dest_path.open("wb") as out_file:
                shutil.copyfileobj(uploaded_file.file, out_file)
        finally:
            try:
                uploaded_file.file.close()
            except Exception:
                pass

        # 写入数据库会议音频记录
        audio_record = audio_service.create_audio_record(
            db=db,
            meeting_id=meeting_id,
            filename=safe_name,
            file_path=str(dest_path),
            file_type=uploaded_file.content_type,
        )

        # 后台转写
        try:
            transcription_service.transcribe_audio_background(audio_record.id, meeting_id, str(dest_path))
        except Exception:
            logger.exception("启动后台转写失败")

        saved_records.append(audio_record)

    return StandardResponse(success=True, data=saved_records, message="音频上传成功，后台转写已启动")

# 获取某一会议的所有音频
@router.get("/audio/{meeting_id}", response_model=StandardResponse[List[schemas2.MeetingAudioInDB]])
def list_meeting_audio(
    meeting_id: int,
    db: Session = Depends(get_db),
    current_user: dict = Depends(get_current_user),
):
    db_meeting = meeting_service.get_meeting(db, meeting_id)
    if not db_meeting:
        raise HTTPException(status_code=404, detail="会议未找到")
    audios = audio_service.get_audios_by_meeting(db, meeting_id)
    return StandardResponse(success=True, data=audios, message="获取会议音频成功")

# 获取某个会议的某个音频
@router.get("/audio/{meeting_id}/{audio_id}", response_model=StandardResponse[schemas2.MeetingAudioInDB])
def get_meeting_audio(
    meeting_id: int,
    audio_id: int,
    db: Session = Depends(get_db),
    current_user: dict = Depends(get_current_user),
):
    db_meeting = meeting_service.get_meeting(db, meeting_id)
    if not db_meeting:
        raise HTTPException(status_code=404, detail="会议未找到")

    audio = audio_service.get_audio_by_id(db, audio_id)
    if not audio or audio.meeting_id != meeting_id:
        raise HTTPException(status_code=404, detail="音频未找到")

    return StandardResponse(success=True, data=audio, message="获取会议音频详情成功")
                                    
# 下载某个会议的某个音频
@router.get("/audio/download/{meeting_id}/{audio_id}")
def download_meeting_audio(
    meeting_id: int,
    audio_id: int,
    db: Session = Depends(get_db),
    current_user: dict = Depends(get_current_user),
):
    db_meeting = meeting_service.get_meeting(db, meeting_id)
    if not db_meeting:
        raise HTTPException(status_code=404, detail="会议未找到")

    audio = audio_service.get_audio_by_id(db, audio_id)
    if not audio or audio.meeting_id != meeting_id:
        raise HTTPException(status_code=404, detail="音频未找到")

    file_path = audio.file_path
    if not file_path:
        raise HTTPException(status_code=404, detail="音频文件路径未记录")

    path_obj = Path(file_path)
    if not path_obj.exists():
        raise HTTPException(status_code=404, detail="服务器上未找到音频文件")

    media_type = audio.file_type or "audio/*"
    return FileResponse(path=str(path_obj), filename=audio.filename, media_type=media_type)

# 删除某个会议的某个音频
@router.delete("/audio/{meeting_id}/{audio_id}", response_model=StandardResponse[None])
def delete_meeting_audio(
    meeting_id: int,
    audio_id: int,
    db: Session = Depends(get_db),
    current_user: dict = Depends(get_current_user),
):
    db_meeting = meeting_service.get_meeting(db, meeting_id)
    if not db_meeting:
        raise HTTPException(status_code=404, detail="会议未找到")

    audio = audio_service.get_audio_by_id(db, audio_id)
    if not audio or audio.meeting_id != meeting_id:
        raise HTTPException(status_code=404, detail="音频未找到")

    # 删除文件
    try:
        p = Path(audio.file_path)
        if p.exists():
            p.unlink()
    except Exception as e:
        logger.warning(f"删除音频文件出错: {e}")

    # 删除数据库记录
    if not audio_service.delete_audio_record(db, audio):
        raise HTTPException(status_code=500, detail="删除失败")

    return StandardResponse(success=True, data=None, message="音频已删除")


@router.websocket("/audio/ws/{meeting_id}")
async def meeting_audio_ws(meeting_id: int, websocket: WebSocket):
    await meeting_ws_manager.connect(meeting_id, websocket)
    try:
        while True:
            await websocket.receive_text()
    except WebSocketDisconnect:
        await meeting_ws_manager.disconnect(meeting_id, websocket)
    except Exception:
        logger.exception("会议音频 WebSocket 异常")
        await meeting_ws_manager.disconnect(meeting_id, websocket)