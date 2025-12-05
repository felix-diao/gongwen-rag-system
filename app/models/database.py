from sqlalchemy import create_engine, Column, String, Float, Boolean, TIMESTAMP, Integer, Text, ARRAY, ForeignKey,DateTime
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
    created_at = Column(DateTime, default=datetime.utcnow)
    # 更新时间
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
    
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
    created_at = Column(TIMESTAMP, default=datetime.utcnow)
    updated_at = Column(TIMESTAMP, default=datetime.utcnow, onupdate=datetime.utcnow)
    
    # 关联关系
    user = relationship("User", foreign_keys=[user_id])


# 创建所有表
Base.metadata.create_all(bind=engine)

def get_db():
    """数据库依赖"""
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()