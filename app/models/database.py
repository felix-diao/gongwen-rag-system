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

# ========== 会议主表 ==========


class Meeting(Base):
    """会议主表（与 `MeetingService` / `meetings` DDL 一致）。"""

    __tablename__ = "meetings"

    id = Column(Integer, primary_key=True, autoincrement=True)
    title = Column(String(256), nullable=False)
    date = Column(DateTime, nullable=True)
    location = Column(String(128), nullable=True)
    host = Column(String(128), nullable=True)
    participants = Column(Text, nullable=True)
    content_text = Column(Text, nullable=True)
    meeting_url = Column(String(512), nullable=True)
    status = Column(String(64), nullable=True, default="created")
    creator_id = Column(String(64), nullable=True, index=True)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    updated_at = Column(
        DateTime, default=datetime.utcnow, onupdate=datetime.utcnow, nullable=False
    )


# ==========会议与会议音频表==========

class MeetingAudio(Base):
    """统一会议音频表。local / volc 共用一张表，用 provider 区分链路。"""
    __tablename__ = "meeting_audios"

    id = Column(Integer, primary_key=True, index=True)
    # 关联 meetings.id
    meeting_id = Column(Integer, index=True, nullable=False)
    # local=本地AI纪要，volc=火山（与 API provider 一致）
    provider = Column(String(16), nullable=False, index=True)
    # 上传人 user_id
    creator_id = Column(String(64), index=True, nullable=True)
    # 原始文件名
    file_name = Column(String(255))
    # 对象存储 object key
    object_key = Column(String(512))
    # 音频访问 URL
    file_url = Column(Text)
    # MIME 类型
    file_type = Column(String(64))
    # 处理状态：uploaded / submitted / completed / failed 等
    status = Column(String(32), default="uploaded", nullable=False)
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

# ==========火山会议纪要表==========

class VolcMeetingAudio(MeetingAudio):
    """火山会议纪要音频（单表继承，物理表 meeting_audios）。"""
    __mapper_args__ = {"polymorphic_identity": "volc"}


class VolcAsrSession(Base):
    """火山流式 ASR 会话表。"""
    __tablename__ = "volc_asr_sessions"

    id = Column(Integer, primary_key=True, index=True)
    # 关联 meetings.id
    meeting_id = Column(Integer, index=True, nullable=False)
    # 关联 meeting_audios.id，录音上传后回填
    source_audio_id = Column(Integer, index=True)
    # pending / completed / failed 等
    status = Column(String(32), default="pending", nullable=False)
    # 实时流式 ASR 累积全文
    stream_transcript_text = Column(Text)
    # 录音时长（秒）
    duration_seconds = Column(Float)
    # 会话失败或异常时的说明（成功一般为空）
    error_msg = Column(Text, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

class VolcMinutesJob(Base):
    """火山语音妙记离线任务。

    表名 volc_minutes_jobs。承载与火山妙记 HTTP 提交/查询对接所需的 TaskID、轮询状态及错误信息；
    提交时固化 input_file_url、input_file_type，与 meeting_audios 解耦。source_audio_id 可选，仅表示
    「本次任务选用的音频」以便追溯，同一音频可对应多条任务记录。
    """

    __tablename__ = "volc_minutes_jobs"

    id = Column(Integer, primary_key=True, index=True)
    # 所属会议 meetings.id
    meeting_id = Column(Integer, index=True, nullable=False)
    # 可选；发起任务时选用的 meeting_audios.id，无外键强制，仅追溯
    source_audio_id = Column(Integer, index=True, nullable=True)
    # 调用妙记提交接口时使用的文件 URL 快照
    input_file_url = Column(Text, nullable=False)
    # 上述 URL 对应的 MIME 类型快照（如 audio/wav）
    input_file_type = Column(String(64), nullable=True)
    # 妙记接口返回的 TaskID，轮询 query 时使用
    volc_task_id = Column(String(128), nullable=True, index=True)
    # 本地任务状态：pending / submitted / 与远端一致的 running 等 / completed / failed
    status = Column(String(32), default="pending", nullable=False)
    # 失败时的说明（远端 ErrMessage 或本地异常文本）
    error_msg = Column(Text, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

class VolcAccurateTranscription(Base):
    """火山离线精准转写：每条会议音频一行（表 volc_accurate_transcriptions）。

    accurate_transcript_text 为精准全文；speaker_segments_json 为说话人分段列表的 JSON
   （[{speaker, text, start_ms?, end_ms?}, ...]）。实时 ASR 仅存 volc_asr_sessions。
    """
    __tablename__ = "volc_accurate_transcriptions"

    id = Column(Integer, primary_key=True, index=True)
    # 关联 meetings.id
    meeting_id = Column(Integer, index=True, nullable=False)
    # 关联 meeting_audios.id，每音频至多一条
    source_audio_id = Column(Integer, index=True, nullable=False, unique=True)
    # 离线精准转写全文
    accurate_transcript_text = Column(Text, nullable=False)
    # 说话人分段 JSON 数组
    speaker_segments_json = Column(Text, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

class VolcMeetingSummary(Base):
    """火山纪要摘要表。"""
    __tablename__ = "volc_meeting_summaries"

    id = Column(Integer, primary_key=True, index=True)
    # 每会议一条当前摘要，关联 meetings.id
    meeting_id = Column(Integer, unique=True, index=True, nullable=False)
    # 摘要所依据的 meeting_audios.id
    source_audio_id = Column(Integer, index=True)
    # 摘要标题
    title = Column(String(255))
    # 摘要正文
    paragraph = Column(Text, nullable=False)
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

class VolcMeetingTodo(Base):
    """火山纪要待办表。"""
    __tablename__ = "volc_meeting_todos"

    id = Column(Integer, primary_key=True, index=True)
    # 关联 meetings.id
    meeting_id = Column(Integer, index=True, nullable=False)
    # 来源 meeting_audios.id
    source_audio_id = Column(Integer, index=True)
    # 待办内容
    content = Column(Text, nullable=False)
    # 执行人
    executor = Column(String(128))
    # 计划完成时间等
    execution_time = Column(String(64))
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


class VolcMeetingMinutesSession(Base):
    """火山会议纪要历史快照表。"""
    __tablename__ = "volc_meeting_minutes_sessions"

    id = Column(Integer, primary_key=True, index=True)
    # 快照业务编号
    session_no = Column(String(64), index=True)
    # 关联 meetings.id
    meeting_id = Column(Integer, index=True, nullable=False)
    # 当时绑定的 meeting_audios.id
    source_audio_id = Column(Integer, index=True)
    # 实时 ASR 稿快照
    stream_transcript_text = Column(Text)
    # 精准转写全文快照
    accurate_transcript_text = Column(Text)
    # 说话人分段 JSON 快照（与 volc_accurate_transcriptions.speaker_segments_json 同形）
    speaker_segments_json = Column(Text, nullable=True)
    # 摘要标题快照
    summary_title = Column(String(255))
    # 摘要正文快照
    summary_paragraph = Column(Text)
    # 待办列表 JSON 快照
    todos_json = Column(Text)
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

# ==========本地AI会议纪要表==========

class LocalMeetingAudio(MeetingAudio):
    """本地会议纪要音频（单表继承）。"""
    __mapper_args__ = {"polymorphic_identity": "local"}

class LocalAsrSession(Base):
    """本地流式 ASR 会话表。"""
    __tablename__ = "local_asr_sessions"

    id = Column(Integer, primary_key=True, index=True)
    # 关联 meetings.id
    meeting_id = Column(Integer, index=True, nullable=False)
    # 关联 meeting_audios.id，录音结束后回填
    source_audio_id = Column(Integer, index=True)
    # pending / completed / failed 等
    status = Column(String(32), default="pending", nullable=False)
    # 本地流式 ASR 累积全文
    stream_transcript_text = Column(Text)
    # 录音时长（秒）
    duration_seconds = Column(Float)
    # 会话失败或异常时的说明（成功一般为空）
    error_msg = Column(Text, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
    
class LocalMeetingSummary(Base):
    """本地纪要摘要表。"""
    __tablename__ = "local_meeting_summaries"

    id = Column(Integer, primary_key=True, index=True)
    # 每会议一条当前摘要，关联 meetings.id
    meeting_id = Column(Integer, unique=True, index=True, nullable=False)
    # 摘要所依据的 meeting_audios.id
    source_audio_id = Column(Integer, index=True)
    # 摘要标题
    title = Column(String(255))
    # 摘要正文
    paragraph = Column(Text, nullable=False)
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


class LocalMeetingTodo(Base):
    """本地纪要待办表。"""
    __tablename__ = "local_meeting_todos"

    id = Column(Integer, primary_key=True, index=True)
    # 关联 meetings.id
    meeting_id = Column(Integer, index=True, nullable=False)
    # 来源 meeting_audios.id
    source_audio_id = Column(Integer, index=True)
    # 待办内容
    content = Column(Text, nullable=False)
    # 执行人
    executor = Column(String(128))
    # 计划完成时间等
    execution_time = Column(String(64))
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


class LocalMeetingMinutesSession(Base):
    """本地会议纪要历史快照表。"""
    __tablename__ = "local_meeting_minutes_sessions"

    id = Column(Integer, primary_key=True, index=True)
    # 快照业务编号
    session_no = Column(String(64), index=True)
    # 关联 meetings.id
    meeting_id = Column(Integer, index=True, nullable=False)
    # 当时绑定的 meeting_audios.id
    source_audio_id = Column(Integer, index=True)
    # 实时 ASR 稿快照
    stream_transcript_text = Column(Text)
    # 摘要标题快照
    summary_title = Column(String(255))
    # 摘要正文快照
    summary_paragraph = Column(Text)
    # 待办列表 JSON 快照
    todos_json = Column(Text)
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


# meeting 域表：创建顺序（外键未在库中强制时仍保持主表在前）。`recreate_meeting_domain_tables.py` 使用。
MEETING_DOMAIN_TABLES = (
    Meeting.__table__,
    MeetingAudio.__table__,
    VolcAsrSession.__table__,
    VolcMinutesJob.__table__,
    VolcAccurateTranscription.__table__,
    VolcMeetingSummary.__table__,
    VolcMeetingTodo.__table__,
    VolcMeetingMinutesSession.__table__,
    LocalAsrSession.__table__,
    LocalMeetingSummary.__table__,
    LocalMeetingTodo.__table__,
    LocalMeetingMinutesSession.__table__,
)


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
