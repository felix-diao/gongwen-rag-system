# schemas.py
from pydantic import BaseModel, Field, ConfigDict, field_serializer
from typing import List, Optional
from datetime import datetime, date, timezone


def _ensure_utc_aware(dt: datetime) -> datetime:
    """Normalize DB datetime to timezone-aware UTC for JSON serialization."""
    if dt.tzinfo is None or dt.tzinfo.utcoffset(dt) is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)

# 会议基础信息模型
class MeetingBase(BaseModel):
    # 会议标题
    title: str
    # 会议时间
    date: datetime
    # 会议地点
    location: Optional[str] = None
    # 主持人
    host: Optional[str] = None
    # 参会人员
    participants: Optional[str] = None
    # 会议内容文本
    content_text: Optional[str] = None
    # 会议链接（新增字段）
    meeting_url: Optional[str] = None
    # 会议状态
    status: Optional[str] = "created"


# 创建会议请求模型
class MeetingCreate(MeetingBase):
    pass

# 更新会议请求模型
class MeetingUpdate(MeetingBase):
    pass

# 数据库会议模型
class MeetingInDB(MeetingBase):
    # 会议ID
    id: int
    # 创建者 ID
    creator_id: Optional[str] = None
    # 创建时间
    created_at: datetime
    # 更新时间
    updated_at: datetime
    
    class Config:
        # 允许ORM模式
        orm_mode = True

# 会议文件基础模型
class MeetingFileBase(BaseModel):
    # 关联会议ID
    meeting_id: int
    # 文件名
    filename: str
    # 文件类型
    file_type: str

# 创建会议文件模型
class MeetingFileCreate(MeetingFileBase):
    # 文件存储路径
    file_path: str

# 数据库会议文件模型
class MeetingFileInDB(MeetingFileBase):
    # 文件ID
    id: int
    # 上传时间
    uploaded_at: datetime
    
    class Config:
        orm_mode = True


# 会议音频模型
class MeetingAudioBase(BaseModel):
    meeting_id: int
    filename: str
    file_type: str

class MeetingAudioCreate(MeetingAudioBase):
    file_path: str

class MeetingAudioInDB(MeetingAudioBase):
    id: int
    uploaded_at: datetime
    transcript_text: Optional[str] = None
    language: Optional[str] = "zh"
    status: Optional[str] = "pending"
    error_msg: Optional[str] = None

    class Config:
        orm_mode = True



class MeetingSummaryBase(BaseModel):
    summary_text: str


class MeetingSummaryCreate(MeetingSummaryBase):
    meeting_id: int


class MeetingSummaryUpdate(BaseModel):
    summary_text: Optional[str] = None


class MeetingSummaryInDB(MeetingSummaryBase):
    model_config = ConfigDict(from_attributes=True)
    id: int
    meeting_id: int
    created_at: datetime
    updated_at: datetime


class MeetingActionItemBase(BaseModel):
    description: str
    owner: Optional[str] = None
    due_date: Optional[date] = None
    status: Optional[str] = "pending"


class MeetingActionItemCreate(MeetingActionItemBase):
    meeting_id: Optional[int] = None


class MeetingActionItemUpdate(BaseModel):
    description: Optional[str] = None
    owner: Optional[str] = None
    due_date: Optional[date] = None
    status: Optional[str] = None


class MeetingActionItemInDB(MeetingActionItemBase):
    model_config = ConfigDict(from_attributes=True)
    id: int
    meeting_id: int
    created_at: datetime
    updated_at: datetime


class MeetingDecisionItemBase(BaseModel):
    description: str


class MeetingDecisionItemCreate(MeetingDecisionItemBase):
    meeting_id: Optional[int] = None


class MeetingDecisionItemUpdate(BaseModel):
    description: Optional[str] = None


class MeetingDecisionItemInDB(MeetingDecisionItemBase):
    model_config = ConfigDict(from_attributes=True)
    id: int
    meeting_id: int
    created_at: datetime
    updated_at: datetime


class MeetingInsightsResponse(BaseModel):
    summary: Optional[MeetingSummaryInDB]
    action_items: List[MeetingActionItemInDB] = Field(default_factory=list)
    decision_items: List[MeetingDecisionItemInDB] = Field(default_factory=list)


# ── 火山引擎大模型流式 ASR 会话 ─────────────────────────────────────────────

class VolcAsrSessionBase(BaseModel):
    meeting_id: int
    session_type: str = "file"        # "file" | "live"
    status: str = "pending"           # pending / processing / completed / failed
    transcript_text: Optional[str] = None
    audio_local_path: Optional[str] = None
    audio_filename: Optional[str] = None
    duration_seconds: Optional[float] = None
    error_msg: Optional[str] = None


class VolcAsrSessionInDB(VolcAsrSessionBase):
    model_config = ConfigDict(from_attributes=True)
    id: int
    created_at: datetime
    updated_at: datetime


# ── 火山引擎 TOS 音频（用于语音妙记）────────────────────────────────────────

class VolcMeetingAudioBase(BaseModel):
    meeting_id: int
    file_name: str
    object_key: str
    file_url: str
    file_type: Optional[str] = None
    status: Optional[str] = None
    task_id: Optional[str] = None
    error_msg: Optional[str] = None
    transcript_text: Optional[str] = None
    source_asr_session_id: Optional[int] = None


class VolcMeetingAudioCreate(VolcMeetingAudioBase):
    pass


class VolcMeetingAudioInDB(VolcMeetingAudioBase):
    model_config = ConfigDict(from_attributes=True)
    id: int
    created_at: datetime
    updated_at: datetime


class VolcAudioUploadTask(BaseModel):
    """火山音频异步上传任务状态"""
    task_id: str
    meeting_id: int
    file_name: str
    status: str  # pending | running | completed | failed
    audio_id: Optional[int] = None
    error_msg: Optional[str] = None
    created_at: datetime
    updated_at: datetime


# ── 语音妙记结果（Todos / Summary）──────────────────────────────────────────

class VolcMeetingTodoBase(BaseModel):
    meeting_id: int
    content: str
    executor: Optional[str] = None
    execution_time: Optional[str] = None
    source_audio_id: Optional[int] = None


class VolcMeetingTodoCreate(VolcMeetingTodoBase):
    pass


class VolcMeetingTodoInDB(VolcMeetingTodoBase):
    model_config = ConfigDict(from_attributes=True)
    id: int
    created_at: datetime
    updated_at: datetime


class VolcMeetingSummaryBase(BaseModel):
    meeting_id: int
    title: Optional[str] = None
    paragraph: str
    source_audio_id: Optional[int] = None


class VolcMeetingSummaryCreate(VolcMeetingSummaryBase):
    pass


class VolcMeetingSummaryInDB(VolcMeetingSummaryBase):
    model_config = ConfigDict(from_attributes=True)
    id: int
    created_at: datetime
    updated_at: datetime


class SpeakerSegment(BaseModel):
    """单段说话人转写"""
    speaker: str
    text: str
    start_ms: Optional[int] = None
    end_ms: Optional[int] = None


class VolcMeetingMinutesResponse(BaseModel):
    """语音妙记完整结果：精准转写 + 说话人分段 + 摘要 + Todos"""
    transcript_text: Optional[str] = None
    stream_transcript_text: Optional[str] = None   # 粗 ASR 流式转写结果（用于退出重进后恢复流式文本框）
    audio_status: Optional[str] = None             # 最新音频的妙记处理状态，'completed' 表示妙记已跑完
    speaker_segments: List[SpeakerSegment] = Field(default_factory=list)
    summary: Optional[VolcMeetingSummaryInDB] = None
    todos: List[VolcMeetingTodoInDB] = Field(default_factory=list)


class VolcTranscriptUpdate(BaseModel):
    """修改会议转写文本"""
    transcript_text: str


# ── 火山会议纪要会话历史 ───────────────────────────────────────────────────────

class VolcSessionTodoItem(BaseModel):
    content: str
    executor: Optional[str] = None
    execution_time: Optional[str] = None
    source_audio_id: Optional[int] = None


class VolcMeetingMinutesSessionBase(BaseModel):
    session_no: Optional[str] = None
    meeting_id: int
    source_audio_id: Optional[int] = None
    source_asr_session_id: Optional[int] = None
    volc_task_id: Optional[str] = None
    status: str
    error_msg: Optional[str] = None
    stream_transcript_text: Optional[str] = None
    transcript_text: Optional[str] = None
    speaker_segments: List[SpeakerSegment] = Field(default_factory=list)
    summary_title: Optional[str] = None
    summary_paragraph: Optional[str] = None
    todos: List[VolcSessionTodoItem] = Field(default_factory=list)


class VolcMeetingMinutesSessionInDB(VolcMeetingMinutesSessionBase):
    model_config = ConfigDict(from_attributes=True)
    id: int
    created_at: datetime
    updated_at: datetime

    @field_serializer("created_at", "updated_at")
    def serialize_datetimes(self, value: datetime) -> datetime:
        return _ensure_utc_aware(value)


class VolcMeetingMinutesSessionUpdate(BaseModel):
    """
    会话历史可编辑字段。仅更新请求中显式传入的字段。
    """
    status: Optional[str] = None
    error_msg: Optional[str] = None
    stream_transcript_text: Optional[str] = None
    transcript_text: Optional[str] = None
    speaker_segments: Optional[List[SpeakerSegment]] = None
    summary_title: Optional[str] = None
    summary_paragraph: Optional[str] = None
    todos: Optional[List[VolcSessionTodoItem]] = None


# ── 流式 ASR 逐段文本 ─────────────────────────────────────────────────────────

class VolcAudioTranscriptionBase(BaseModel):
    meeting_id: Optional[int] = None
    source_session_id: Optional[int] = None
    source_audio_id: Optional[int] = None
    text: str
    is_final: Optional[bool] = False
    provider: Optional[str] = "volc"
    start_time: Optional[float] = None
    end_time: Optional[float] = None


class VolcAudioTranscriptionCreate(VolcAudioTranscriptionBase):
    pass


class VolcAudioTranscriptionInDB(VolcAudioTranscriptionBase):
    model_config = ConfigDict(from_attributes=True)
    id: int
    created_at: datetime
    updated_at: datetime


# ══════════════════════════════════════════════════════════════════════════════
# 本地会议纪要（Qwen3-ASR + LLM）
# ══════════════════════════════════════════════════════════════════════════════

class LocalMeetingAudioBase(BaseModel):
    meeting_id: int
    file_name: str
    object_key: str
    file_url: str
    file_type: Optional[str] = None
    status: Optional[str] = None
    transcript_text: Optional[str] = None
    source_asr_session_id: Optional[int] = None
    error_msg: Optional[str] = None


class LocalMeetingAudioInDB(LocalMeetingAudioBase):
    model_config = ConfigDict(from_attributes=True)
    id: int
    created_at: datetime
    updated_at: datetime

    @field_serializer("created_at", "updated_at")
    def serialize_datetimes(self, value: datetime) -> datetime:
        return _ensure_utc_aware(value)


class LocalMeetingSummaryBase(BaseModel):
    meeting_id: int
    title: Optional[str] = None
    paragraph: str
    source_audio_id: Optional[int] = None


class LocalMeetingSummaryCreate(LocalMeetingSummaryBase):
    pass


class LocalMeetingSummaryInDB(LocalMeetingSummaryBase):
    model_config = ConfigDict(from_attributes=True)
    id: int
    created_at: datetime
    updated_at: datetime


class LocalMeetingTodoBase(BaseModel):
    meeting_id: int
    content: str
    executor: Optional[str] = None
    execution_time: Optional[str] = None
    source_audio_id: Optional[int] = None


class LocalMeetingTodoCreate(LocalMeetingTodoBase):
    pass


class LocalMeetingTodoInDB(LocalMeetingTodoBase):
    model_config = ConfigDict(from_attributes=True)
    id: int
    created_at: datetime
    updated_at: datetime


class LocalSessionTodoItem(BaseModel):
    content: str
    executor: Optional[str] = None
    execution_time: Optional[str] = None
    source_audio_id: Optional[int] = None


class LocalMeetingMinutesSessionBase(BaseModel):
    session_no: Optional[str] = None
    meeting_id: int
    source_audio_id: Optional[int] = None
    source_asr_session_id: Optional[int] = None
    status: str
    error_msg: Optional[str] = None
    stream_transcript_text: Optional[str] = None
    transcript_text: Optional[str] = None
    summary_title: Optional[str] = None
    summary_paragraph: Optional[str] = None
    todos: List[LocalSessionTodoItem] = Field(default_factory=list)


class LocalMeetingMinutesSessionInDB(LocalMeetingMinutesSessionBase):
    model_config = ConfigDict(from_attributes=True)
    id: int
    created_at: datetime
    updated_at: datetime

    @field_serializer("created_at", "updated_at")
    def serialize_datetimes(self, value: datetime) -> datetime:
        return _ensure_utc_aware(value)


class LocalMeetingMinutesSessionUpdate(BaseModel):
    status: Optional[str] = None
    error_msg: Optional[str] = None
    stream_transcript_text: Optional[str] = None
    transcript_text: Optional[str] = None
    summary_title: Optional[str] = None
    summary_paragraph: Optional[str] = None
    todos: Optional[List[LocalSessionTodoItem]] = None


class LocalAsrSessionBase(BaseModel):
    meeting_id: int
    session_type: str = "file"
    status: str = "pending"
    transcript_text: Optional[str] = None
    audio_local_path: Optional[str] = None
    audio_filename: Optional[str] = None
    duration_seconds: Optional[float] = None
    error_msg: Optional[str] = None


class LocalAsrSessionInDB(LocalAsrSessionBase):
    model_config = ConfigDict(from_attributes=True)
    id: int
    created_at: datetime
    updated_at: datetime


class LocalSessionTodoItem(BaseModel):
    content: str
    executor: Optional[str] = None
    execution_time: Optional[str] = None
    source_audio_id: Optional[int] = None


class LocalMeetingMinutesSessionBase(BaseModel):
    session_no: Optional[str] = None
    meeting_id: int
    source_audio_id: Optional[int] = None
    source_asr_session_id: Optional[int] = None
    status: str
    error_msg: Optional[str] = None
    stream_transcript_text: Optional[str] = None
    transcript_text: Optional[str] = None
    summary_title: Optional[str] = None
    summary_paragraph: Optional[str] = None
    todos: List[LocalSessionTodoItem] = Field(default_factory=list)


class LocalMeetingMinutesSessionInDB(LocalMeetingMinutesSessionBase):
    model_config = ConfigDict(from_attributes=True)
    id: int
    created_at: datetime
    updated_at: datetime

    @field_serializer("created_at", "updated_at")
    def serialize_datetimes(self, value: datetime) -> datetime:
        return _ensure_utc_aware(value)


class LocalMeetingMinutesSessionUpdate(BaseModel):
    status: Optional[str] = None
    error_msg: Optional[str] = None
    stream_transcript_text: Optional[str] = None
    transcript_text: Optional[str] = None
    summary_title: Optional[str] = None
    summary_paragraph: Optional[str] = None
    todos: Optional[List[LocalSessionTodoItem]] = None


class LocalMeetingMinutesResponse(BaseModel):
    """本地会议纪要完整结果：转写 + 摘要 + Todos"""
    transcript_text: Optional[str] = None
    stream_transcript_text: Optional[str] = None
    audio_status: Optional[str] = None
    summary: Optional[LocalMeetingSummaryInDB] = None
    todos: List[LocalMeetingTodoInDB] = Field(default_factory=list)


class LocalTranscriptUpdate(BaseModel):
    transcript_text: str


