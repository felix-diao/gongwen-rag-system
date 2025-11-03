import httpx
from typing import List
from app.config import settings
from app.utils.logger import logger

class EmbeddingService:
    """向量化服务"""
    
    def __init__(self):
        self.api_url = settings.EMBEDDING_API_URL
        self.model = settings.EMBEDDING_MODEL
        self.client = httpx.AsyncClient(timeout=30.0)
    
    async def embed_texts(self, texts: List[str]) -> List[List[float]]:
        """批量文本向量化"""
        try:
            response = await self.client.post(
                self.api_url,
                json={
                    "model": self.model,
                    "input": texts
                }
            )
            response.raise_for_status()
            
            result = response.json()
            embeddings = [item["embedding"] for item in result["data"]]
            
            logger.info(f"成功向量化 {len(texts)} 段文本")
            return embeddings
            
        except Exception as e:
            logger.error(f"向量化失败: {e}")
            raise
    
    async def embed_query(self, query: str) -> List[float]:
        """查询向量化"""
        embeddings = await self.embed_texts([query])
        return embeddings[0]
    
    async def close(self):
        """关闭客户端"""
        await self.client.aclose()

embedding_service = EmbeddingService()