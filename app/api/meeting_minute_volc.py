"""
meeting_domain - 火山会议纪要 API。

核心能力：
1) WebSocket 实时录音转译（流式返回转写）
2) 提交音频到语音妙记生成纪要（精确转写/摘要/待办）
3) 四个核心部分（流式转写、精确转写、摘要、待办）的增删改查

接口约定：
- “流式转写” 与 “精确转写” 是两个独立字段；
- 音频上传与上传任务查询统一收敛到 meeting_audio 域接口。

阅读提示：
1. 这个文件只做 HTTP / WebSocket 协议转换，不直接处理妙记轮询。
2. 火山链路天然分两段：实时 ASR 和离线妙记；对应 service 内部也分成两套状态更新逻辑。
3. 若排查问题，优先区分“实时录音阶段”还是“离线妙记阶段”出错。
"""

from __future__ import annotations

from fastapi import APIRouter, Body, Depends, HTTPException, Query, WebSocket
from sqlalchemy.orm import Session

from app.models.meeting_domain import database, schemas
from app.models.schemas import StandardResponse
from app.services.meeting_domain.meeting_minute_volc_service import (
    LiveVolcAsrHandler,
    volc_meeting_minute_service,
)
from app.utils.auth import decode_access_token, get_current_user

router = APIRouter(prefix="/api/meetings/minutes/volc", tags=["meeting_domain_volc_minutes"])


# 步骤说明（实时录音 WebSocket）：
# 1) 校验 token；
# 2) 校验会议存在；
# 3) 交给 LiveVolcAsrHandler 处理“接收音频 -> 流式转写 -> 上传音频”全链路。
@router.websocket("/{meeting_id}/live")
async def live_recording(websocket: WebSocket, meeting_id: int, token: str = Query(...)):
    try:
        decode_access_token(token)
    except HTTPException:
        await websocket.close(code=4001)
        return

    from app.models.meeting_domain.database import SessionLocal

    db = SessionLocal()
    try:
        exists = db.query(database.Meeting.id).filter(database.Meeting.id == meeting_id).first()
        if not exists:
            raise ValueError("会议不存在")
        handler = LiveVolcAsrHandler(websocket, db, meeting_id, volc_meeting_minute_service)
        await handler.run()
    except ValueError as exc:
        await websocket.close(code=4004, reason=str(exc))
    finally:
        db.close()


@router.post(
    "/{meeting_id}/submit",
    response_model=StandardResponse[schemas.MeetingAudioUnifiedInDB],
)
# 步骤说明（提交语音妙记）：
# 1) 校验会议存在；
# 2) 校验 audio_id 属于该会议；
# 3) 调用 service 提交妙记任务，异步生成精确转写/摘要/待办。
def submit_minutes(
    meeting_id: int,
    audio_id: int = Query(...),
    db: Session = Depends(database.get_db),
    current_user: dict = Depends(get_current_user),
):
    exists = db.query(database.Meeting.id).filter(database.Meeting.id == meeting_id).first()
    if not exists:
        raise HTTPException(status_code=404, detail="会议不存在")

    audio = (
        db.query(database.MeetingAudio)
        .filter(
            database.MeetingAudio.id == audio_id,
            database.MeetingAudio.meeting_id == meeting_id,
            database.MeetingAudio.provider == "volc",
        )
        .first()
    )
    if not audio:
        raise HTTPException(status_code=404, detail="音频记录不存在")

    try:
        record = volc_meeting_minute_service.submit_minutes(
            db=db,
            meeting_id=meeting_id,
            audio_id=audio.id,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    return StandardResponse(
        success=True,
        data=schemas.MeetingAudioUnifiedInDB.model_validate(record),
        message="已提交语音妙记，后台处理中",
    )


@router.get(
    "/{meeting_id}",
    response_model=StandardResponse[schemas.VolcMeetingMinutesResponse],
)
# 步骤说明（纪要视图查询）：
# 1) 查询会议最新纪要快照；
# 2) 拼装四块内容：流式转写、精确转写、摘要、待办；
# 3) 返回前端统一渲染模型。
def get_minutes(
    meeting_id: int,
    db: Session = Depends(database.get_db),
    current_user: dict = Depends(get_current_user),
):
    try:
        data = volc_meeting_minute_service.get_minutes(db, meeting_id)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return StandardResponse(success=True, data=data, message="获取会议纪要成功")


@router.put(
    "/{meeting_id}/stream-transcript",
    response_model=StandardResponse[None],
)
# 步骤说明（流式转写更新）：
# 1) 定位当前会议对应的流式会话；
# 2) 覆盖文本内容；
# 3) 持久化后返回成功。
def update_stream_transcript(
    meeting_id: int,
    payload: schemas.VolcTranscriptUpdate = Body(...),
    db: Session = Depends(database.get_db),
    current_user: dict = Depends(get_current_user),
):
    try:
        volc_meeting_minute_service.update_stream_transcript(db, meeting_id, payload.transcript_text)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return StandardResponse(success=True, data=None, message="流式转写已更新")



@router.put(
    "/{meeting_id}/transcript",
    response_model=StandardResponse[schemas.MeetingAudioUnifiedInDB],
)
# 步骤说明（精确转写更新）：
# 1) 定位会议最新音频记录；
# 2) 更新精确转写字段；
# 3) 返回更新后的音频记录。
def update_transcript(
    meeting_id: int,
    payload: schemas.VolcTranscriptUpdate = Body(...),
    db: Session = Depends(database.get_db),
    current_user: dict = Depends(get_current_user),
):
    try:
        audio = volc_meeting_minute_service.update_precise_transcript(db, meeting_id, payload.transcript_text)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return StandardResponse(
        success=True,
        data=schemas.MeetingAudioUnifiedInDB.model_validate(audio),
        message="精确转写已更新",
    )



@router.put(
    "/{meeting_id}/summary",
    response_model=StandardResponse[schemas.VolcMeetingSummaryInDB],
)
# 步骤说明（摘要 upsert）：
# 1) 若摘要存在则更新；
# 2) 若摘要不存在则创建；
# 3) 返回当前摘要实体。
def upsert_summary(
    meeting_id: int,
    payload: schemas.VolcMeetingSummaryCreate = Body(...),
    db: Session = Depends(database.get_db),
    current_user: dict = Depends(get_current_user),
):
    try:
        summary = volc_meeting_minute_service.upsert_summary(db, meeting_id, payload)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return StandardResponse(
        success=True,
        data=schemas.VolcMeetingSummaryInDB.model_validate(summary),
        message="会议摘要已更新",
    )



@router.post(
    "/{meeting_id}/todos",
    response_model=StandardResponse[schemas.VolcMeetingTodoInDB],
)
# 步骤说明（待办新增）：
# 1) 校验会议存在；
# 2) 写入待办内容/执行人/执行时间；
# 3) 返回新增后的待办记录。
def create_todo(
    meeting_id: int,
    payload: schemas.VolcMeetingTodoCreate = Body(...),
    db: Session = Depends(database.get_db),
    current_user: dict = Depends(get_current_user),
):
    try:
        todo = volc_meeting_minute_service.create_todo(db, meeting_id, payload)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return StandardResponse(
        success=True,
        data=schemas.VolcMeetingTodoInDB.model_validate(todo),
        message="待办事项已新增",
    )


@router.put(
    "/{meeting_id}/todos/{todo_id}",
    response_model=StandardResponse[schemas.VolcMeetingTodoInDB],
)
# 步骤说明（待办更新）：
# 1) 校验 todo_id 属于该会议；
# 2) 覆盖待办内容；
# 3) 返回更新后的待办记录。
def update_todo(
    meeting_id: int,
    todo_id: int,
    payload: schemas.VolcMeetingTodoCreate = Body(...),
    db: Session = Depends(database.get_db),
    current_user: dict = Depends(get_current_user),
):
    todo = volc_meeting_minute_service.update_todo(db, meeting_id, todo_id, payload)
    if not todo:
        raise HTTPException(status_code=404, detail="待办事项不存在")
    return StandardResponse(
        success=True,
        data=schemas.VolcMeetingTodoInDB.model_validate(todo),
        message="待办事项已更新",
    )


@router.delete(
    "/{meeting_id}/todos/{todo_id}",
    response_model=StandardResponse[None],
)
# 步骤说明（待办删除）：
# 1) 校验 todo_id 属于该会议；
# 2) 删除记录；
# 3) 返回空数据表示删除成功。
def delete_todo(
    meeting_id: int,
    todo_id: int,
    db: Session = Depends(database.get_db),
    current_user: dict = Depends(get_current_user),
):
    deleted = volc_meeting_minute_service.delete_todo(db, meeting_id, todo_id)
    if not deleted:
        raise HTTPException(status_code=404, detail="待办事项不存在")
    return StandardResponse(success=True, data=None, message="待办事项已删除")
