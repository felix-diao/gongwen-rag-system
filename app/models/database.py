# app/models/database.py
from sqlalchemy import create_engine, Column, String, Float, Boolean, TIMESTAMP, Text
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy.orm import sessionmaker
from datetime import datetime
import json
from app.config import settings

Base = declarative_base()

# 检查是否为 SQLite
is_sqlite = settings.DATABASE_URL.startswith("sqlite")

# 创建引擎
if is_sqlite:
    engine = create_engine(
        settings.DATABASE_URL,
        connect_args={"check_same_thread": False},
        echo=settings.DEBUG
    )
else:
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
    tags = Column(Text, default='[]')  # JSON 字符串
    weight = Column(Float, default=1.0)
    valid = Column(Boolean, default=True)
    created_at = Column(TIMESTAMP, default=datetime.utcnow)
    updated_at = Column(TIMESTAMP, default=datetime.utcnow, onupdate=datetime.utcnow)
    
    def get_tags(self):
        """获取标签列表"""
        if isinstance(self.tags, str):
            try:
                return json.loads(self.tags)
            except:
                return []
        return self.tags if self.tags else []
    
    def set_tags(self, tags_list):
        """设置标签列表"""
        self.tags = json.dumps(tags_list if tags_list else [], ensure_ascii=False)

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