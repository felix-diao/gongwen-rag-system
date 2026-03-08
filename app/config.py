from pydantic_settings import BaseSettings
from typing import List, Optional
import os

# 在导入配置之前就设置离线模式
os.environ['TRANSFORMERS_OFFLINE'] = '1'
os.environ['HF_HUB_OFFLINE'] = '1'

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
    LLM_PROVIDER: str = "deepseek"
    LLM_BASE_URL: str = "https://api.deepseek.com/v1"
    LLM_TIMEOUT: int = 180
    
    
    # Embedding 模型配置 - 使用本地缓存的模型
    EMBEDDING_MODEL: str = "BAAI/bge-large-zh-v1.5"
    
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

    # 腾讯会议配置
    TENCENT_MEETING_APP_ID: str = ""
    TENCENT_MEETING_SDK_ID: str = ""
    TENCENT_MEETING_SECRET_ID: str = ""
    TENCENT_MEETING_SECRET_KEY: str = ""
    TENCENT_MEETING_API_URL: str = "https://api.meeting.qq.com/v1"

    # 语音转文字配置
    WHISPER_MODEL_PATH: str
    BEAM_SIZE: int
    VAD_FILTER: bool
    LANGUAGE: str

    # AI率配置
    AI_RATE_MODEL_DIR: str
    USE_LM: bool

    #日志保存时间
    LOG_KEEP_DAYS: int = 30   # ← 加这一行

    # 火山引擎大模型流式语音识别 (Functionality 1)
    # 资源ID：小时版 volc.bigasr.sauc.duration；并发版 volc.bigasr.sauc.concurrent
    VOLC_ASR_RESOURCE_ID: str = "volc.bigasr.sauc.duration"
    VOLC_ASR_APP_KEY: str = ""
    VOLC_ASR_ACCESS_KEY: str = ""
    # 实时录音保存目录（留空则使用 UPLOAD_DIR/asr_recordings）
    VOLC_ASR_AUDIO_SAVE_DIR: str = ""

    # 火山引擎对象存储配置
    VOLC_TOS_ENDPOINT: str = "https://tos-cn-beijing.volces.com"
    VOLC_TOS_REGION: str = "cn-beijing"
    VOLC_TOS_BUCKET: str = ""
    VOLC_TOS_PUBLIC_BASE: str = ""
    VOLC_TOS_ACCESS_KEY_ID: str = ""
    VOLC_TOS_SECRET_ACCESS_KEY: str = ""

    # 豆包语音妙记 API 配置
    VOLC_MINUTES_API_BASE: str = "https://openspeech.bytedance.com"
    VOLC_MINUTES_SUBMIT_PATH: str = "/api/v3/auc/lark/submit"
    VOLC_MINUTES_QUERY_PATH: str = "/api/v3/auc/lark/query"
    VOLC_MINUTES_APP_KEY: str = "7348432775"
    VOLC_MINUTES_ACCESS_KEY: str = "AH0yQdtRt-FFj7Iq_hQT--GewVFLVYYj"
    VOLC_MINUTES_RESOURCE_ID: str = "volc.lark.minutes"
    VOLC_MINUTES_SOURCE_LANG: str = "zh_cn"
    VOLC_MINUTES_SPEAKER_IDENTIFICATION: bool = False
    VOLC_MINUTES_NUMBER_OF_SPEAKERS: int = 0
    VOLC_MINUTES_NEED_WORD_TS: bool = False
    VOLC_MINUTES_TIMEOUT: int = 10
    VOLC_MINUTES_TRANSLATION_ENABLE: bool = False
    VOLC_MINUTES_TRANSLATION_TARGET_LANG: str = "zh_cn"
    VOLC_MINUTES_INFORMATION_EXTRACTION_TYPES: List[str] = ["todo_list", "question_answer", "transition"]
    VOLC_MINUTES_SUMMARIZATION_TYPES: List[str] = ["summary"]
    VOLC_MINUTES_CHAPTER_ENABLED: bool = True
    
    class Config:
        env_file = ".env"

settings = Settings()