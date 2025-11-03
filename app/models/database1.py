from sqlalchemy import create_engine, Column, String, Float, Boolean, TIMESTAMP, Integer, Text, ARRAY
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy.orm import sessionmaker
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

# 创建所有表
Base.metadata.create_all(bind=engine)

def get_db():
    """数据库依赖"""
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()