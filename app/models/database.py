from sqlalchemy import create_engine, Column, String, Float, Boolean, TIMESTAMP, Integer, Text, ARRAY, ForeignKey
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy.orm import sessionmaker, relationship
from datetime import datetime
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


# ========== 新增：腾讯会议表 ==========
class Meeting(Base):
    """会议表"""
    __tablename__ = "meetings"
    
    id = Column(Integer, primary_key=True, autoincrement=True)
    
    # 腾讯会议信息
    meeting_id = Column(String(64), unique=True, nullable=False, index=True)  # 腾讯会议ID
    meeting_code = Column(String(32), nullable=False)  # 会议号
    subject = Column(String(256), nullable=False)  # 会议主题
    join_url = Column(Text, nullable=False)  # 加入链接
    
    # 会议时间
    meeting_type = Column(Integer, default=0)  # 0:预约会议 1:快速会议
    start_time = Column(Integer)  # Unix 时间戳
    end_time = Column(Integer)
    
    # 会议设置
    settings = Column(Text)  # JSON 格式存储会议设置
    
    # 关联用户
    user_id = Column(String(64), ForeignKey("users.user_id", ondelete="CASCADE"), nullable=False, index=True)
    
    # 状态
    status = Column(String(32), default="active")  # active/cancelled/ended
    
    created_at = Column(TIMESTAMP, default=datetime.utcnow)
    updated_at = Column(TIMESTAMP, default=datetime.utcnow, onupdate=datetime.utcnow)

# 创建所有表
Base.metadata.create_all(bind=engine)

def get_db():
    """数据库依赖"""
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()