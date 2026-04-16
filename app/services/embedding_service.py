# app/services/embedding_service.py
import asyncio
import os
from typing import List
from app.config import settings
from FlagEmbedding import FlagModel
from transformers import AutoTokenizer
from app.utils.logger import get_logger

logger = get_logger("embedding_service")

# 确保离线模式（防止意外的网络请求）
os.environ['TRANSFORMERS_OFFLINE'] = '1'
os.environ['HF_HUB_OFFLINE'] = '1'

class TextChunker:
    def __init__(
        self,
        model_name: str,
        chunk_size: int = 512,
        overlap: int = 50
    ):
        self.tokenizer = AutoTokenizer.from_pretrained(
            model_name,
            use_fast=True
        )
        self.chunk_size = chunk_size
        self.overlap = overlap

    def chunk_text(self, text: str) -> List[str]:
        tokens = self.tokenizer.encode(
            text,
            add_special_tokens=False
        )

        chunks = []
        start = 0

        while start < len(tokens):
            end = start + self.chunk_size
            chunk_tokens = tokens[start:end]
            chunk_text = self.tokenizer.decode(chunk_tokens)
            chunks.append(chunk_text)
            start += self.chunk_size - self.overlap

        return chunks

class EmbeddingService:
    """向量化服务"""
    
    def __init__(self):
        self.model = None
        self.query_instruction = "Represent this sentence for searching relevant passages:"
        self.model_name = settings.EMBEDDING_MODEL
        self.chunker = TextChunker(
            self.model_name,
            chunk_size=512,
            overlap=50
        )
    
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
        return FlagModel(
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
                
            # 分块
            all_chunks = []
            for text in texts:
                chunks = self.chunker.chunk_text(text)
                all_chunks.extend(chunks)

            logger.info(
                f"原始文本 {len(texts)} 条，"
                f"分块后 {len(all_chunks)} 条"
            )
        
            # 清空原列表并用分块后的文本填充
            texts.clear()
            texts.extend(all_chunks)
            
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