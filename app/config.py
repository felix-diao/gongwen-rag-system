from pydantic_settings import BaseSettings
from typing import List, Optional
from pydantic import field_validator
import os

# 在导入配置之前就设置离线模式
os.environ['TRANSFORMERS_OFFLINE'] = '1'
os.environ['HF_HUB_OFFLINE'] = '1'

class Settings(BaseSettings):
    # 应用配置
    APP_NAME: str = "公文大模型RAG系统"
    APP_VERSION: str = "1.0.0"
    DEBUG: bool = False

    @field_validator("DEBUG", mode="before")
    @classmethod
    def parse_debug(cls, value):
        if isinstance(value, str):
            raw = value.strip().lower()
            if raw in {"release", "prod", "production", "0", "false", "off", "no"}:
                return False
            if raw in {"debug", "dev", "development", "1", "true", "on", "yes"}:
                return True
        return value
    
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
    # LLM 代理控制：默认不继承系统代理，避免失效代理导致调用失败
    LLM_USE_ENV_PROXY: bool = False
    LLM_PROXY_URL: str = ""
    
    
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
    # 企业微信配置
    WECHAT_CORP_ID: str = ""
    WECHAT_AGENT_ID: str = ""
    WECHAT_SECRET: str = ""

    # AI率配置
    AI_RATE_MODEL_DIR: str
    USE_LM: bool

    #日志保存时间
    LOG_KEEP_DAYS: int = 30   # ← 加这一行

    # 火山引擎大模型流式语音识别 (Functionality 1)
    # 资源ID：
    #   ASR 1.0 小时版 volc.bigasr.sauc.duration；并发版 volc.bigasr.sauc.concurrent
    #   ASR 2.0 小时版 volc.seedasr.sauc.duration；并发版 volc.seedasr.sauc.concurrent
    # 开启说话人分离时务必使用 ASR 2.0 资源
    VOLC_ASR_RESOURCE_ID: str = "volc.bigasr.sauc.duration"
    VOLC_ASR_APP_KEY: str = ""
    VOLC_ASR_ACCESS_KEY: str = ""
    # 实时录音保存目录（留空则使用 UPLOAD_DIR/asr_recordings）
    VOLC_ASR_AUDIO_SAVE_DIR: str = ""
    # 实时 ASR WebSocket 路径：
    #   /api/v3/sauc/bigmodel          标准双向流式（ASR 1.0，不支持说话人分离）
    #   /api/v3/sauc/bigmodel_async    双向流式优化版（ASR 2.0，支持说话人分离）
    VOLC_ASR_WS_PATH: str = "/api/v3/sauc/bigmodel_async"
    # 是否开启实时 ASR 说话人分离（需要 ASR 2.0 资源 + bigmodel_async）
    VOLC_ASR_SPEAKER_INFO: bool = True
    # 大模型 SSD 版本：开启说话人分离时必须为 "200"
    VOLC_ASR_SSD_VERSION: str = "200"
    # 是否开启二遍识别（流式+非流式），说话人分离依赖该能力
    VOLC_ASR_ENABLE_NONSTREAM: bool = True

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
    # 火山妙记说话人识别：开启后 volc 会议纪要会返回 speaker_segments
    VOLC_MINUTES_SPEAKER_IDENTIFICATION: bool = True
    VOLC_MINUTES_NUMBER_OF_SPEAKERS: int = 0
    VOLC_MINUTES_NEED_WORD_TS: bool = False
    VOLC_MINUTES_TIMEOUT: int = 10
    VOLC_MINUTES_TRANSLATION_ENABLE: bool = False
    VOLC_MINUTES_TRANSLATION_TARGET_LANG: str = "zh_cn"
    VOLC_MINUTES_INFORMATION_EXTRACTION_TYPES: List[str] = ["todo_list", "question_answer", "transition"]
    VOLC_MINUTES_SUMMARIZATION_TYPES: List[str] = ["summary"]
    VOLC_MINUTES_CHAPTER_ENABLED: bool = True

    # Qwen ASR：实时 WS + HTTP 分段转写（对齐 test_asr/qwen_asr_smoketest_incremental_merge.py）
    QWEN_ASR_API_KEY: str = ""
    QWEN_ASR_MODEL: str = "qwen3-asr-flash-realtime"
    QWEN_ASR_WS_URL: str = "ws://192.168.1.100:8888/api-ws/v1/realtime"
    QWEN_ASR_LANGUAGE: str = "zh"
    QWEN_ASR_VAD_THRESHOLD: float = 0.65
    QWEN_ASR_SILENCE_DURATION_MS: int = 400
    QWEN_ASR_AUDIO_SAVE_DIR: str = ""
    # true：实时只收 PCM，按 CHUNK/OVERLAP 滑窗走 HTTP ASR；false：走 QWEN_ASR_WS_URL
    QWEN_ASR_LIVE_FORCE_HTTP_CHUNK: bool = True
    # 实时滑窗与整文件 ffmpeg 切片共用（秒）；步长 = CHUNK_SEC - OVERLAP_SEC
    QWEN_ASR_CHUNK_SEC: float = 6.0
    QWEN_ASR_OVERLAP_SEC: float = 1.0
    # 语音转写 chat/completions（audio_url），与 LLM_API_URL 分离
    QWEN_ASR_HTTP_CHAT_URL: str = ""
    QWEN_ASR_HTTP_CHAT_MODEL: str = ""
    QWEN_ASR_HTTP_CHAT_API_KEY: str = ""
    QWEN_ASR_HTTP_CHAT_TIMEOUT_SEC: float = 120.0
    QWEN_ASR_HTTP_CHAT_MAX_TOKENS: int = 512
    # 已有音频转写：每批先切多少段再开始识别，避免长音频首段长时间无输出
    QWEN_ASR_FILE_PREPARE_BATCH_SIZE: int = 12
    # 分段 wav 静态服务根目录；必须显式配置，避免落到容器私有 /tmp 随机目录
    QWEN_ASR_HTTP_ROOT_DIR: str = ""
    # ASR 服务可访问的分段 wav URL 前缀（无尾斜杠），端口=本进程静态服务 bind
    QWEN_ASR_FILE_HTTP_PUBLIC_BASE: str = ""

    # 本地会议纪要 TOS 配置（复用 VOLC_TOS 的 endpoint/region/key，仅 bucket 不同）
    LOCAL_TOS_BUCKET: str = "meeting-record-local-temp"

    class Config:
        env_file = ".env"


def is_postgresql_url(url: str) -> bool:
    if not url:
        return False
    u = url.split(":", 1)[0].lower()
    return u.startswith("postgresql")


def sqlalchemy_connect_args(url: str) -> dict:
    """PostgreSQL 连接时设置 search_path，避免空 search_path 下建表失败。"""
    if not is_postgresql_url(url):
        return {}
    return {"options": "-c search_path=public"}


settings = Settings()
