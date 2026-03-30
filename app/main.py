from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from contextlib import asynccontextmanager
import asyncio
import os

from app.config import settings
from app.api import documents, rag, conversations, admin, embed, knowledge, document, translate, llm, meeting, meeting_minute_local, meeting_minute_volc, meeting_minute_structured, prompt
from app.services.websocket_manager import meeting_ws_manager
from app.utils.logger import get_logger  # ← 修改：使用新 logger

# 创建应用专属 logger
logger = get_logger("app")  # ← 修改：创建 logger 实例

@asynccontextmanager
async def lifespan(app: FastAPI):
    """应用生命周期管理"""
    logger.info("=" * 60)
    logger.info(f"🚀 {settings.APP_NAME} 启动中...")
    logger.info(f"版本: {settings.APP_VERSION}")
    logger.info(f"调试模式: {settings.DEBUG}")
    logger.info("=" * 60)
    
    try:
        # 初始化服务
        from app.services.vector_service import vector_service
        from app.services.embedding_service import embedding_service
        from app.services.rag_service import rag_service
        from app.services.llm_service import llm_service
        
        logger.info("初始化 WebSocket 管理器...")
        meeting_ws_manager.set_event_loop(asyncio.get_running_loop())
        
        logger.info("初始化 Embedding 服务...")
        await embedding_service.initialize()
        
        logger.info("初始化向量数据库集合...")
        vector_service.create_collection_if_not_exists("public_documents", is_private=False)
        vector_service.create_collection_if_not_exists("private_documents", is_private=True)
        vector_service.create_collection_if_not_exists("conversations", is_private=True)
        
        logger.info("=" * 60)
        logger.info(f"✅ {settings.APP_NAME} 启动完成")
        logger.info(f"📚 API 文档: http://0.0.0.0:8080/docs")
        logger.info("=" * 60)
        
    except Exception as e:
        logger.critical(f"❌ 应用启动失败: {e}", exc_info=True)
        raise
    
    yield
    
    logger.info("=" * 60)
    logger.info(f"👋 {settings.APP_NAME} 正在关闭...")
    logger.info("=" * 60)
    
    try:
        await embedding_service.close()
        await llm_service.close()
        logger.info("✅ 服务关闭完成")
    except Exception as e:
        logger.error(f"关闭服务时出错: {e}", exc_info=True)

app = FastAPI(
    title=settings.APP_NAME,
    version=settings.APP_VERSION,
    description="公文大模型 RAG 系统",
    lifespan=lifespan,
    docs_url="/docs"
)

# 上传目录
UPLOAD_DIR = os.getenv("UPLOAD_DIR", "./uploads")
os.makedirs(UPLOAD_DIR, exist_ok=True)
logger.info(f"上传目录: {UPLOAD_DIR}")

# 挂载静态目录
app.mount("/uploads", StaticFiles(directory=UPLOAD_DIR), name="uploads") 

DOWNLOAD_DIR = os.getenv("DOWNLOAD_DIR", "./generated_documents")
os.makedirs(DOWNLOAD_DIR, exist_ok=True)
app.mount("/generated_documents", StaticFiles(directory=DOWNLOAD_DIR), name="generated_documents")

PDF_DIR = os.getenv("PDF_DIR", "./pdf")
os.makedirs(PDF_DIR, exist_ok=True)
app.mount("/pdf", StaticFiles(directory=PDF_DIR), name="pdf")

TXT_DIR = os.getenv("TXT_DIR", "./txt")
os.makedirs(TXT_DIR, exist_ok=True)
app.mount("/txt", StaticFiles(directory=TXT_DIR), name="txt")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# 注册路由
logger.info("注册 API 路由...")
app.include_router(admin.router)
app.include_router(documents.router)
app.include_router(document.router)
app.include_router(meeting.router)
app.include_router(meeting_minute_local.router)
app.include_router(meeting_minute_volc.router)
app.include_router(meeting_minute_structured.router)
app.include_router(embed.router)
app.include_router(rag.router)
app.include_router(conversations.router)
app.include_router(knowledge.router)
app.include_router(translate.router)
app.include_router(llm.router)
app.include_router(prompt.router)

@app.get("/health")
def health_check():
    """健康检查"""
    return {
        "status": "healthy",
        "app_name": settings.APP_NAME,
        "version": settings.APP_VERSION
    }

@app.get("/")
def root():
    """根路径"""
    return {
        "message": f"欢迎使用{settings.APP_NAME}",
        "version": settings.APP_VERSION,
        "docs": "/docs",
        "health": "/health"
    }

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(
        "app.main:app",
        host="0.0.0.0",
        port=8080,
        reload=settings.DEBUG,
        log_level="info"
    )