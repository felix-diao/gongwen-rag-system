# schemas.py
from pydantic import BaseModel, Field, ConfigDict
from typing import List, Optional
from datetime import datetime, date

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


class VolcMeetingAudioBase(BaseModel):
    meeting_id: int
    file_name: str
    object_key: str
    file_url: str
    file_type: Optional[str] = None
    status: Optional[str] = None
    task_id: Optional[str] = None
    error_msg: Optional[str] = None


class VolcMeetingAudioCreate(VolcMeetingAudioBase):
    pass


class VolcMeetingAudioInDB(VolcMeetingAudioBase):
    model_config = ConfigDict(from_attributes=True)
    id: int
    created_at: datetime
    updated_at: datetime


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


class VolcMeetingMinutesResponse(BaseModel):
    summary: Optional[VolcMeetingSummaryInDB] = None
    todos: List[VolcMeetingTodoInDB] = Field(default_factory=list)

