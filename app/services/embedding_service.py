# app/services/embedding_service.py
import asyncio
import os
from typing import List
from app.config import settings
from app.utils.logger import logger
from FlagEmbedding import FlagAutoModel

# 确保离线模式（防止意外的网络请求）
os.environ['TRANSFORMERS_OFFLINE'] = '1'
os.environ['HF_HUB_OFFLINE'] = '1'

class EmbeddingService:
    """向量化服务"""
    
    def __init__(self):
        self.model = None
        self.query_instruction = "Represent this sentence for searching relevant passages:"
        self.model_name = settings.EMBEDDING_MODEL
    
    async def initialize(self):
        """异步初始化模型"""
        if self.model is None:
            logger.info(f"正在加载 BGE 大模型: {self.model_name}")
            try:
                loop = asyncio.get_event_loop()
                self.model = await loop.run_in_executor(
                    None, 
                    self._load_model
                )
                logger.info("BGE 大模型加载完成")
                
                # 验证模型维度
                test_embedding = self.model.encode(["test"]).shape
                logger.info(f"模型向量维度: {test_embedding}")
                
                if test_embedding[1] != settings.EMBEDDING_DIM:
                    logger.warning(
                        f"模型维度 {test_embedding[1]} 与配置维度 {settings.EMBEDDING_DIM} 不匹配！"
                    )
                    
            except Exception as e:
                logger.error(f"模型加载失败: {e}")
                logger.error("请检查模型是否已下载到本地缓存")
                raise
    
    def _load_model(self):
        """在线程池中执行的模型加载"""
        return FlagAutoModel.from_finetuned(
            self.model_name,
            query_instruction_for_retrieval=self.query_instruction,
            use_fp16=True  # 使用半精度加速
        )
    
    async def embed_texts(self, texts: List[str]) -> List[List[float]]:
        """批量文本向量化
        
        Args:
            texts: 要向量化的文本列表
            
        Returns:
            向量列表
        """
        try:
            await self.initialize()
            
            if not texts:
                return []
            
            loop = asyncio.get_event_loop()
            embeddings = await loop.run_in_executor(
                None, 
                lambda: self.model.encode(texts).tolist()
            )
            
            logger.info(f"成功向量化 {len(texts)} 段文本")
            return embeddings
            
        except Exception as e:
            logger.error(f"向量化失败: {e}")
            raise
    
    async def embed_query(self, query: str) -> List[float]:
        """查询向量化（使用专门的查询指令）
        
        Args:
            query: 查询文本
            
        Returns:
            查询向量
        """
        try:
            await self.initialize()
            
            loop = asyncio.get_event_loop()
            embedding = await loop.run_in_executor(
                None,
                lambda: self.model.encode_queries([query])[0].tolist()
            )
            
            logger.debug(f"查询向量化完成: {query[:50]}...")
            return embedding
            
        except Exception as e:
            logger.error(f"查询向量化失败: {e}")
            raise
    
    async def compute_similarity(self, texts1: List[str], texts2: List[str]) -> List[List[float]]:
        """计算两组文本的相似度矩阵
        
        Args:
            texts1: 第一组文本
            texts2: 第二组文本
            
        Returns:
            相似度矩阵
        """
        try:
            embeddings1 = await self.embed_texts(texts1)
            embeddings2 = await self.embed_texts(texts2)
            
            # 计算余弦相似度
            import numpy as np
            emb1 = np.array(embeddings1)
            emb2 = np.array(embeddings2)
            
            # 归一化
            emb1_norm = emb1 / np.linalg.norm(emb1, axis=1, keepdims=True)
            emb2_norm = emb2 / np.linalg.norm(emb2, axis=1, keepdims=True)
            
            # 计算相似度
            similarity = emb1_norm @ emb2_norm.T
            
            return similarity.tolist()
            
        except Exception as e:
            logger.error(f"相似度计算失败: {e}")
            raise
    
    async def close(self):
        """关闭服务"""
        self.model = None
        logger.info("Embedding service closed")

# 创建全局实例
embedding_service = EmbeddingService()