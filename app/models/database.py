from sqlalchemy import (
    ARRAY,
    Boolean,
    Column,
    Date,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    MetaData,
    String,
    Text,
    TIMESTAMP,
    create_engine,
    event,
    text,
)
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy.orm import relationship, sessionmaker
from datetime import datetime
import os

from app.config import is_postgresql_url, settings, sqlalchemy_connect_args

_is_sqlite = settings.DATABASE_URL.startswith("sqlite")
_is_pg = is_postgresql_url(settings.DATABASE_URL)
_metadata_schema = "public" if _is_pg else None
_metadata = MetaData(schema=_metadata_schema) if _metadata_schema else MetaData()
Base = declarative_base(metadata=_metadata)

if _is_sqlite:
    engine = create_engine(
        settings.DATABASE_URL,
        connect_args={"check_same_thread": False},
        echo=settings.DEBUG,
    )
elif _is_pg:
    engine = create_engine(
        settings.DATABASE_URL,
        echo=settings.DEBUG,
        connect_args=sqlalchemy_connect_args(settings.DATABASE_URL),
    )
else:
    engine = create_engine(settings.DATABASE_URL, echo=settings.DEBUG)

SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)

if _is_pg:

    @event.listens_for(engine, "connect")
    def _pg_set_search_path(dbapi_conn, _connection_record):
        with dbapi_conn.cursor() as cur:
            cur.execute("SET search_path TO public")

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

# ========== 会议域模型（重构后的 meeting_domain 主定义） ==========

# 会议主表。这里只保存会议基础信息，不直接承载纪要内容。
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
    # 会议状态（如 created / processing / finished）
    status = Column(String, default="created")
    # 创建者ID
    creator_id = Column(String(64), index=True)
    # 创建时间
    created_at = Column(DateTime, default=datetime.now)
    # 更新时间
    updated_at = Column(DateTime, default=datetime.now, onupdate=datetime.now)
    
# 会议文件表。
# 这是旧会议体系仍在使用的附件表，不属于 meeting_domain 新纪要链路，因此保留不动。
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

# 统一会议音频表。local / volc 两种 provider 共用这一张表。
class MeetingAudio(Base):
    __tablename__ = "meeting_audios"

    id = Column(Integer, primary_key=True, index=True)
    meeting_id = Column(Integer, index=True, nullable=False)
    provider = Column(String(32), index=True, nullable=False, default="local")

    # 统一音频元信息
    file_name = Column(String(255))
    object_key = Column(String(512))
    file_url = Column(Text)
    file_type = Column(String(64))
    status = Column(String(32), default="uploaded", nullable=False)
    task_id = Column(String(128), index=True)
    error_msg = Column(Text)
    # 当前音频关联的最新转写全文，便于前端列表和排障直接读取。
    transcript_text = Column(Text)
    # 兼容旧火山纪要逻辑的说话人分段缓存；新 meeting_domain 主链路不依赖该字段。
    speaker_transcript = Column(Text)
    # 来源 ASR 会话 ID：实时录音完成后回填，便于串联日志和历史快照。
    source_asr_session_id = Column(Integer, index=True)
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    __mapper_args__ = {
        "polymorphic_on": provider,
        "polymorphic_identity": "base",
    }


class VolcMeetingAudio(MeetingAudio):
    """火山会议纪要音频（单表继承）。"""
    __mapper_args__ = {"polymorphic_identity": "volc"}


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
    """火山会议纪要历史快照（每次提交妙记生成一条）。"""
    __tablename__ = "volc_meeting_minutes_sessions"

    id = Column(Integer, primary_key=True, index=True)
    session_no = Column(String(64), index=True)
    meeting_id = Column(Integer, index=True, nullable=False)
    source_audio_id = Column(Integer, index=True)
    source_asr_session_id = Column(Integer, index=True)
    volc_task_id = Column(String(128), index=True)
    status = Column(String(32), default="completed", nullable=False)
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
    """火山流式 ASR 会话。"""
    __tablename__ = "volc_asr_sessions"

    id = Column(Integer, primary_key=True, index=True)
    meeting_id = Column(Integer, index=True, nullable=False)
    source_audio_id = Column(Integer, index=True)
    # 当前重构版主要创建 live 会话；保留类型字段是为了兼容文件转写与排障。
    session_type = Column(String(32), default="live", nullable=False)
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
    """火山语音识别/精准转写文本存储。"""
    __tablename__ = "volc_audio_transcriptions"

    id = Column(Integer, primary_key=True, index=True)
    meeting_id = Column(Integer, index=True, nullable=True)
    # 兼容旧链路：可关联 VolcAsrSession.id
    source_session_id = Column(Integer, index=True, nullable=True)
    # 新旧链路共用：关联 meeting_audios.id
    source_audio_id = Column(Integer, index=True, nullable=True)
    # 兼容旧链路：来源标记
    provider = Column(String(64), default="volc")
    # 每一段的识别文本；既可用于实时粗转写片段，也可用于最终整段精准转写。
    text = Column(Text, nullable=False)
    # 是否是当前 utterance 的最终确认段
    is_final = Column(Boolean, default=False)
    # 起止时间（毫秒）
    start_time = Column(Float)
    end_time = Column(Float)
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


class VolcSpeakerSegment(Base):
    """火山妙记精准转写的说话人分段。"""
    __tablename__ = "volc_speaker_segments"

    id = Column(Integer, primary_key=True, index=True)
    meeting_id = Column(Integer, index=True, nullable=False)
    source_audio_id = Column(Integer, index=True, nullable=False)
    segment_index = Column(Integer, nullable=False)
    speaker = Column(String(128), nullable=False)
    text = Column(Text, nullable=False)
    start_ms = Column(Float)
    end_ms = Column(Float)
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

# ── 本地会议纪要（Qwen3-ASR + LLM）────────────────────────────────────────

class LocalMeetingAudio(MeetingAudio):
    """本地会议纪要音频（单表继承）。"""
    __mapper_args__ = {"polymorphic_identity": "local"}


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
    """本地会议纪要历史快照（每次生成纪要一条）。"""
    __tablename__ = "local_meeting_minutes_sessions"

    id = Column(Integer, primary_key=True, index=True)
    session_no = Column(String(64), index=True)
    meeting_id = Column(Integer, index=True, nullable=False)
    source_audio_id = Column(Integer, index=True)
    source_asr_session_id = Column(Integer, index=True)
    status = Column(String(32), default="completed", nullable=False)
    error_msg = Column(Text)
    stream_transcript_text = Column(Text)
    # 兼容旧链路：本地纪要没有独立精准转写阶段，这里通常为空。
    transcript_text = Column(Text)
    summary_title = Column(String(255))
    summary_paragraph = Column(Text)
    todos_json = Column(Text)
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


class LocalAsrSession(Base):
    """本地流式 ASR 会话。"""
    __tablename__ = "local_asr_sessions"

    id = Column(Integer, primary_key=True, index=True)
    meeting_id = Column(Integer, index=True, nullable=False)
    source_audio_id = Column(Integer, index=True)
    session_type = Column(String(32), default="live", nullable=False)
    status = Column(String(32), default="pending", nullable=False)
    transcript_text = Column(Text)
    audio_local_path = Column(String(512))
    audio_filename = Column(String(255))
    duration_seconds = Column(Float)
    error_msg = Column(Text)
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


# 显式列出重构后 meeting_domain 关心的表。
# 这样脚本只重建会议纪要相关表时，不会误伤数据库中的其他业务表。
MEETING_DOMAIN_TABLES = [
    Meeting.__table__,
    MeetingAudio.__table__,
    VolcMeetingSummary.__table__,
    VolcMeetingTodo.__table__,
    VolcAsrSession.__table__,
    VolcAudioTranscription.__table__,
    VolcSpeakerSegment.__table__,
    LocalMeetingSummary.__table__,
    LocalMeetingTodo.__table__,
    LocalAsrSession.__table__,
    LocalMeetingMinutesSession.__table__,
    VolcMeetingMinutesSession.__table__,
]


def create_all_tables() -> None:
    """创建 ORM 表。PostgreSQL 在空 search_path 下须在同一条连接上先 SET 再 DDL。"""
    if _is_pg:
        with engine.begin() as conn:
            conn.execute(text("SET search_path TO public"))
            Base.metadata.create_all(bind=conn)
    else:
        Base.metadata.create_all(bind=engine)


# 仅在显式开启时自动建表，避免导入模块时把历史遗留表自动重建。
if os.getenv("AUTO_CREATE_ALL_TABLES", "0").strip().lower() in {"1", "true", "yes", "on"}:
    create_all_tables()


def get_db():
    """数据库依赖"""
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
