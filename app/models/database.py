from sqlalchemy import create_engine, Column, String, Float, Boolean, TIMESTAMP, Integer, Text, ARRAY, ForeignKey, DateTime, inspect, text
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy.orm import sessionmaker, relationship
from datetime import datetime
from sqlalchemy import Column, Date
from app.config import settings

Base = declarative_base()
engine = create_engine(settings.DATABASE_URL, echo=settings.DEBUG)
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)

class Document(Base):
    """文档表"""
    __tablename__ = "documents"
    
    doc_id = Column(String(64), primary_key=True)
    owner_id = Column(String(64), nullable=False, index=True)
    title = Column(String(256), nullable=False)
    doc_type = Column(String(64), nullable=False)
    filename = Column(Text, nullable=False)
    file_path = Column(Text, nullable=False)
    tags = Column(ARRAY(String), default=[])
    weight = Column(Float, default=1.0)
    valid = Column(Boolean, default=True)
    created_at = Column(TIMESTAMP, default=datetime.utcnow)
    updated_at = Column(TIMESTAMP, default=datetime.utcnow, onupdate=datetime.utcnow)

class Conversation(Base):
    """历史会话表"""
    __tablename__ = "conversations"
    
    conv_id = Column(String(64), primary_key=True)
    user_id = Column(String(64), nullable=False, index=True)
    query = Column(Text, nullable=False)
    answer = Column(Text, nullable=False)
    weight = Column(Float, default=0.8)
    liked = Column(Boolean, default=False)
    valid = Column(Boolean, default=True)
    created_at = Column(TIMESTAMP, default=datetime.utcnow)

class User(Base):
    """用户表"""
    __tablename__ = "users"
    
    user_id = Column(String(64), primary_key=True)
    username = Column(String(256), unique=True, nullable=False)
    hashed_password = Column(String(256), nullable=False)
    department = Column(String(128))
    role = Column(String(64), default="user")
    created_at = Column(TIMESTAMP, default=datetime.utcnow)

# ========== 新增：知识库表 ==========
class KnowledgeBase(Base):
    """知识库表"""
    __tablename__ = "knowledge_bases"
    
    id = Column(Integer, primary_key=True, autoincrement=True)
    name = Column(String(100), nullable=False, index=True)
    key = Column(String(50), index=True)  # 唯一标识符（可选）
    description = Column(Text)
    user_id = Column(String(64), ForeignKey("users.user_id", ondelete="CASCADE"), nullable=False, index=True)
    
    is_public = Column(Boolean, default=False) 

    # 统计信息
    item_count = Column(Integer, default=0)
    total_size = Column(Integer, default=0)  # 总大小（字节）
    
    created_at = Column(TIMESTAMP, default=datetime.utcnow)
    updated_at = Column(TIMESTAMP, default=datetime.utcnow, onupdate=datetime.utcnow)
    
    # 关联关系
    items = relationship("KnowledgeItem", back_populates="base", cascade="all, delete-orphan")

class KnowledgeItem(Base):
    """知识项表（知识库中的文件）"""
    __tablename__ = "knowledge_items"
    
    id = Column(Integer, primary_key=True, autoincrement=True)
    
    # 文件基本信息
    original_name = Column(String(255), nullable=False)
    url = Column(Text, nullable=False)  # 文件存储路径
    mime_type = Column(String(100), nullable=False)
    size = Column(Integer, nullable=False)
    
    # 关联关系
    base_id = Column(Integer, ForeignKey("knowledge_bases.id", ondelete="CASCADE"), nullable=True, index=True)
    user_id = Column(String(64), ForeignKey("users.user_id", ondelete="CASCADE"), nullable=False, index=True)
    
    # 关联 documents 表
    doc_id = Column(String(64), ForeignKey("documents.doc_id", ondelete="CASCADE"), unique=True, index=True)
    
    # 标签（使用 ARRAY 保持与你现有风格一致）
    tags = Column(ARRAY(String), default=[])
    
    # 处理状态
    status = Column(String(20), default="pending")  # pending/processing/completed/failed
    error_msg = Column(Text)
    chunk_count = Column(Integer, default=0)
    
    created_at = Column(TIMESTAMP, default=datetime.utcnow)
    updated_at = Column(TIMESTAMP, default=datetime.utcnow, onupdate=datetime.utcnow)
    
    # 关联关系
    base = relationship("KnowledgeBase", back_populates="items")
    document = relationship("Document", foreign_keys=[doc_id])


    # 会议信息表
class Meeting(Base):
    __tablename__ = "meetings"
    
    # 主键ID
    id = Column(Integer, primary_key=True, index=True)
    # 会议标题
    title = Column(String, index=True)
    # 会议日期时间
    date = Column(DateTime)
    # 会议地点
    location = Column(String)
    # 主持人
    host = Column(String)
    # 参会人员（JSON字符串或逗号分隔的姓名）
    participants = Column(Text)
    # 会议内容文本
    content_text = Column(Text)
    # 会议链接
    meeting_url = Column(String)
    # 会议状态（pending、created、minutes_generated）
    status = Column(String, default="created")
    # 创建者ID
    creator_id = Column(String(64), index=True)
    # 创建时间
    created_at = Column(DateTime, default=datetime.now)
    # 更新时间
    updated_at = Column(DateTime, default=datetime.now, onupdate=datetime.now)
    
    # 关联会议文件和纪要（逻辑关联，无外键约束）
    # files = relationship("MeetingFile", back_populates="meeting")
    # minutes = relationship("MeetingMinutes", back_populates="meeting")

# 会议文件表
class MeetingFile(Base):
    __tablename__ = "meeting_files"
    
    # 主键ID
    id = Column(Integer, primary_key=True, index=True)
    # 关联的会议ID（逻辑关联，无外键约束）
    meeting_id = Column(Integer)
    # 文件名
    filename = Column(String)
    # 文件存储路径
    file_path = Column(String)
    # 文件类型（pdf、docx等）
    file_type = Column(String)
    # 上传时间
    uploaded_at = Column(DateTime, default=datetime.utcnow)
    
    # 建立与会议的关联关系（逻辑关联，无外键约束）
    # meeting = relationship("Meeting", back_populates="files")

class MeetingSummary(Base):
    __tablename__ = "meeting_summaries"

    id = Column(Integer, primary_key=True, index=True)
    meeting_id = Column(Integer, unique=True, index=True, nullable=False)
    summary_text = Column(Text, nullable=False)
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


class MeetingActionItem(Base):
    __tablename__ = "meeting_action_items"

    id = Column(Integer, primary_key=True, index=True)
    meeting_id = Column(Integer, index=True, nullable=False)
    description = Column(Text, nullable=False)
    owner = Column(String)
    due_date = Column(Date)
    status = Column(String, default="pending")
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


class MeetingDecisionItem(Base):
    __tablename__ = "meeting_decision_items"

    id = Column(Integer, primary_key=True, index=True)
    meeting_id = Column(Integer, index=True, nullable=False)
    description = Column(Text, nullable=False)
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


# 会议音频表
class MeetingAudio(Base):
    __tablename__ = "meeting_audios"

    id = Column(Integer, primary_key=True, index=True)
    meeting_id = Column(Integer, index=True)
    filename = Column(String)
    file_path = Column(String)
    file_type = Column(String) 
    uploaded_at = Column(DateTime, default=datetime.utcnow)
    transcript_text = Column(Text)
    language = Column(String(20), default="zh")
    status = Column(String(20), default="pending")  # pending/processing/completed/failed
    error_msg = Column(Text)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


class PromptTemplate(Base):
    """Prompt 模板表"""
    __tablename__ = "prompt_templates"
    
    id = Column(Integer, primary_key=True, autoincrement=True)
    user_id = Column(String(64), ForeignKey("users.user_id", ondelete="CASCADE"), nullable=False, index=True)
    name = Column(String(100), nullable=False, index=True)
    category = Column(String(50), nullable=False, index=True)
    description = Column(Text)
    content = Column(Text, nullable=False)
    variables = Column(ARRAY(String), default=[])  # 保持与你的风格一致，使用 ARRAY
    is_active = Column(Boolean, default=True, index=True)

    is_public = Column(Boolean, default=False, index=True, comment="是否为公共模板（管理员专用）")

    created_at = Column(TIMESTAMP, default=datetime.utcnow)
    updated_at = Column(TIMESTAMP, default=datetime.utcnow, onupdate=datetime.utcnow)
    
    # 关联关系
    user = relationship("User", foreign_keys=[user_id])


class VolcMeetingAudio(Base):
    __tablename__ = "volc_meeting_audios"

    id = Column(Integer, primary_key=True, index=True)
    meeting_id = Column(Integer, index=True, nullable=False)
    file_name = Column(String(255), nullable=False)
    object_key = Column(String(512), nullable=False)
    file_url = Column(Text, nullable=False)
    file_type = Column(String(64))
    status = Column(String(32), default="uploaded", nullable=False)
    task_id = Column(String(128), index=True)
    error_msg = Column(Text)
    # 语音妙记 返回的更精准转写文本（覆盖流式 ASR 结果）
    transcript_text = Column(Text)
    # 说话人分段转写（JSON 字符串）：[{"speaker":"说话人1","text":"...","start_ms":0,"end_ms":1500}, ...]
    speaker_transcript = Column(Text)
    # 关联的流式 ASR 会话（来源于 Functionality 1）
    source_asr_session_id = Column(Integer, index=True)
    created_at = Column(DateTime, default=datetime.now)
    updated_at = Column(DateTime, default=datetime.now, onupdate=datetime.now)


class VolcMeetingTodo(Base):
    __tablename__ = "volc_meeting_todos"

    id = Column(Integer, primary_key=True, index=True)
    meeting_id = Column(Integer, index=True, nullable=False)
    source_audio_id = Column(Integer, index=True)
    content = Column(Text, nullable=False)
    executor = Column(String(128))
    execution_time = Column(String(64))
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


class VolcMeetingSummary(Base):
    __tablename__ = "volc_meeting_summaries"

    id = Column(Integer, primary_key=True, index=True)
    meeting_id = Column(Integer, unique=True, index=True, nullable=False)
    source_audio_id = Column(Integer, index=True)
    title = Column(String(255))
    paragraph = Column(Text, nullable=False)
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


class VolcMeetingMinutesSession(Base):
    """火山会议纪要会话历史快照（每次提交妙记生成一条）。"""
    __tablename__ = "volc_meeting_minutes_sessions"

    id = Column(Integer, primary_key=True, index=True)
    session_no = Column(String(64), index=True)
    meeting_id = Column(Integer, index=True, nullable=False)
    source_audio_id = Column(Integer, index=True)
    source_asr_session_id = Column(Integer, index=True)
    volc_task_id = Column(String(128), index=True)
    status = Column(String(32), default="submitted", nullable=False)
    error_msg = Column(Text)

    # 流式 ASR 结果（粗转写）
    stream_transcript_text = Column(Text)
    # 妙记精准转写结果
    transcript_text = Column(Text)
    # 说话人分段 JSON 字符串
    speaker_segments_json = Column(Text)

    # 会议摘要快照
    summary_title = Column(String(255))
    summary_paragraph = Column(Text)
    # 待办快照 JSON 字符串
    todos_json = Column(Text)

    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

class VolcAsrSession(Base):
    """火山引擎大模型流式语音识别会话"""
    __tablename__ = "volc_asr_sessions"

    id = Column(Integer, primary_key=True, index=True)
    meeting_id = Column(Integer, index=True, nullable=False)
    # "file"：上传音频文件后台转写；"live"：实时 WebSocket 录音转写
    session_type = Column(String(32), default="file", nullable=False)
    # pending / processing / completed / failed
    status = Column(String(32), default="pending", nullable=False)
    # 累积的最终转写文本（流式 ASR 输出）
    transcript_text = Column(Text)
    # 本地保存的音频文件路径（实时录音时由服务器合成 WAV 保存）
    audio_local_path = Column(String(512))
    # 原始文件名
    audio_filename = Column(String(255))
    # 音频时长（秒）
    duration_seconds = Column(Float)
    error_msg = Column(Text)
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


class VolcAudioTranscription(Base):
    """Volc 流式语音识别逐段结果存储"""
    __tablename__ = "volc_audio_transcriptions"

    id = Column(Integer, primary_key=True, index=True)
    meeting_id = Column(Integer, index=True, nullable=True)
    # 关联 VolcAsrSession.id
    source_session_id = Column(Integer, index=True, nullable=True)
    # 兼容旧字段（可关联 meeting_audios.id）
    source_audio_id = Column(Integer, index=True, nullable=True)
    # 来源标记
    provider = Column(String(64), default="volc")
    # 每一段的识别文本
    text = Column(Text, nullable=False)
    # 是否是当前 utterance 的最终确认段
    is_final = Column(Boolean, default=False)
    # 起止时间（毫秒）
    start_time = Column(Float)
    end_time = Column(Float)
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

# ── 本地会议纪要（Qwen3-ASR + LLM）────────────────────────────────────────

class LocalMeetingAudio(Base):
    """本地会议纪要 - TOS 音频记录"""
    __tablename__ = "local_meeting_audios"

    id = Column(Integer, primary_key=True, index=True)
    meeting_id = Column(Integer, index=True, nullable=False)
    file_name = Column(String(255), nullable=False)
    object_key = Column(String(512), nullable=False)
    file_url = Column(Text, nullable=False)
    file_type = Column(String(64))
    status = Column(String(32), default="uploaded", nullable=False)
    transcript_text = Column(Text)
    source_asr_session_id = Column(Integer, index=True)
    error_msg = Column(Text)
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


class LocalMeetingSummary(Base):
    """本地会议纪要 - 会议摘要（LLM 生成）"""
    __tablename__ = "local_meeting_summaries"

    id = Column(Integer, primary_key=True, index=True)
    meeting_id = Column(Integer, unique=True, index=True, nullable=False)
    source_audio_id = Column(Integer, index=True)
    title = Column(String(255))
    paragraph = Column(Text, nullable=False)
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


class LocalMeetingTodo(Base):
    """本地会议纪要 - 待办事项（LLM 生成）"""
    __tablename__ = "local_meeting_todos"

    id = Column(Integer, primary_key=True, index=True)
    meeting_id = Column(Integer, index=True, nullable=False)
    source_audio_id = Column(Integer, index=True)
    content = Column(Text, nullable=False)
    executor = Column(String(128))
    execution_time = Column(String(64))
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


class LocalMeetingMinutesSession(Base):
    """本地会议纪要会话历史快照（每次生成纪要一条）。"""
    __tablename__ = "local_meeting_minutes_sessions"

    id = Column(Integer, primary_key=True, index=True)
    session_no = Column(String(64), index=True)
    meeting_id = Column(Integer, index=True, nullable=False)
    source_audio_id = Column(Integer, index=True)
    source_asr_session_id = Column(Integer, index=True)
    status = Column(String(32), default="submitted", nullable=False)
    error_msg = Column(Text)
    stream_transcript_text = Column(Text)
    transcript_text = Column(Text)
    summary_title = Column(String(255))
    summary_paragraph = Column(Text)
    todos_json = Column(Text)
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


class LocalAsrSession(Base):
    """本地 Qwen3-ASR 流式语音识别会话"""
    __tablename__ = "local_asr_sessions"

    id = Column(Integer, primary_key=True, index=True)
    meeting_id = Column(Integer, index=True, nullable=False)
    session_type = Column(String(32), default="file", nullable=False)
    status = Column(String(32), default="pending", nullable=False)
    transcript_text = Column(Text)
    audio_local_path = Column(String(512))
    audio_filename = Column(String(255))
    duration_seconds = Column(Float)
    error_msg = Column(Text)
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


# 创建所有表
Base.metadata.create_all(bind=engine)


def _ensure_schema_compatibility() -> None:
    """
    轻量兼容迁移：为历史库补齐新增字段。
    说明：项目目前未接入 Alembic，这里在启动时做幂等补丁。
    """
    inspector = inspect(engine)
    table_name = "volc_meeting_minutes_sessions"
    existing_tables = set(inspector.get_table_names())
    if table_name not in existing_tables:
        return

    existing_columns = {col["name"] for col in inspector.get_columns(table_name)}
    if "session_no" in existing_columns:
        return

    with engine.begin() as conn:
        conn.execute(text(f"ALTER TABLE {table_name} ADD COLUMN session_no VARCHAR(64)"))


_ensure_schema_compatibility()

def get_db():
    """数据库依赖"""
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()