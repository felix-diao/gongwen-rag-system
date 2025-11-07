# app/main.py
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from contextlib import asynccontextmanager
from app.config import settings
from app.api import documents, rag, conversations, admin, embed
from app.utils.logger import logger

# app/main.py 中的 lifespan 函数需要更新
@asynccontextmanager
async def lifespan(app: FastAPI):
    """应用生命周期管理"""
    logger.info(f"{settings.APP_NAME} 启动中...")
    
    # 初始化服务
    from app.services.vector_service import vector_service
    from app.services.embedding_service import embedding_service
    from app.services.rag_service import rag_service
    
    # 初始化 embedding_service
    await embedding_service.initialize()
    
    vector_service.create_collection_if_not_exists("public_documents", is_private=False)
    vector_service.create_collection_if_not_exists("private_documents", is_private=True)
    vector_service.create_collection_if_not_exists("conversations", is_private=True)
    
    logger.info(f"{settings.APP_NAME} 启动完成")
    
    yield
    
    await embedding_service.close()
    await rag_service.close()
    logger.info(f"{settings.APP_NAME} 已关闭")

app = FastAPI(
    title=settings.APP_NAME,
    version=settings.APP_VERSION,
    description="公文大模型 RAG 系统",
    lifespan=lifespan
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(admin.router)
app.include_router(documents.router)
app.include_router(embed.router)
app.include_router(rag.router)
app.include_router(conversations.router)

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
        port=8000,
        reload=settings.DEBUG,
        log_level="info"
    )