# schemas.py
from pydantic import BaseModel
from typing import List, Optional
from datetime import datetime

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



# 会议纪要基础模型
class MeetingMinutesBase(BaseModel):
    # 关联会议ID
    meeting_id: int
    # 纪要标题
    title: str
    # 纪要内容
    content: str

# 创建会议纪要模型
class MeetingMinutesCreate(MeetingMinutesBase):
    pass

# 更新会议纪要模型
class MeetingMinutesUpdate(BaseModel):
    # 纪要标题（可选）
    title: Optional[str] = None
    # 纪要内容（可选）
    content: Optional[str] = None

# 数据库会议纪要模型
class MeetingMinutesInDB(MeetingMinutesBase):
    # 纪要ID
    id: int
    # 创建时间
    created_at: datetime
    # 更新时间
    updated_at: datetime
    
    class Config:
        orm_mode = True