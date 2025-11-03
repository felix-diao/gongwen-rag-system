from pydantic_settings import BaseSettings
from typing import Optional

class Settings(BaseSettings):
    # 应用配置
    APP_NAME: str = "公文大模型RAG系统"
    APP_VERSION: str = "1.0.0"
    DEBUG: bool = False
    
    # 数据库配置
    DATABASE_URL: str = "postgresql://gongwen_user:password123@localhost:5432/gongwen_rag"
    
    # Milvus 配置
    MILVUS_HOST: str = "localhost"
    MILVUS_PORT: int = 19530
    MILVUS_USER: str = ""
    MILVUS_PASSWORD: str = ""
    
    # 向量维度
    EMBEDDING_DIM: int = 1024
    
    # LLM 配置
    LLM_API_URL: str = "http://localhost:8000/v1/chat/completions"
    LLM_API_KEY: str = "your-api-key"
    LLM_MODEL: str = "gongwen-llm-v1"
    
    # Embedding 模型配置
    EMBEDDING_API_URL: str = "http://localhost:8001/v1/embeddings"
    EMBEDDING_MODEL: str = "gongwen-embed-v1"
    
    # JWT 配置
    SECRET_KEY: str = "09d25e094faa6ca2556c818166b7a9563b93f7099f6f0f4caa6cf63b88e8d3e7"
    ALGORITHM: str = "HS256"
    ACCESS_TOKEN_EXPIRE_MINUTES: int = 1440  # 24小时
    
    # 文件存储
    UPLOAD_DIR: str = "./uploads"
    MAX_UPLOAD_SIZE: int = 100 * 1024 * 1024  # 100MB
    
    # RAG 配置
    DEFAULT_TOP_K: int = 6
    PUBLIC_WEIGHT: float = 0.6
    PRIVATE_WEIGHT: float = 0.4
    CONVERSATION_WEIGHT: float = 0.3
    
    # 分块配置
    CHUNK_SIZE: int = 500
    CHUNK_OVERLAP: int = 50
    
    class Config:
        env_file = ".env"

settings = Settings()