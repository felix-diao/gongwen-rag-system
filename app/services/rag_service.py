# app/services/rag_service.py

from typing import List, Dict, Optional
from app.services.vector_service import vector_service
from app.services.embedding_service import embedding_service
from app.services.conversation_service import conversation_service
from app.services.llm_service import llm_service 
from app.config import settings
from app.utils.logger import get_logger
logger = get_logger("rag_service")


class RAGService:
    """RAG 检索增强生成服务（解耦版）"""
    
    def __init__(self):
        self.llm_service = llm_service
        self.public_weight = settings.PUBLIC_WEIGHT
        self.private_weight = settings.PRIVATE_WEIGHT
        self.conv_weight = settings.CONVERSATION_WEIGHT
    
    async def retrieve_and_generate(
        self,
        user_id: str,
        query: str,
        top_k: int = 6,
        rerank: bool = True,
        rerank_model: str = "cross-encoder-v0",
        generator: str = None,
        context_token_limit: int = 3000,
        include_conversations: bool = True
    ) -> Dict:
        """RAG 主流程"""
        
        # 1. 向量化查询
        query_vector = await embedding_service.embed_query(query)
        
        # 2. 多源检索
        candidates = await self._multi_source_retrieve(
            user_id=user_id,
            query=query,
            query_vector=query_vector,
            top_k=top_k * 2,
            include_conversations=include_conversations
        )
        
        if not candidates:
            return {
                "query": query,
                "answer": "抱歉，没有找到相关的公文资料。",
                "sources": [],
                "metadata": {"retrieval_count": 0}
            }
        
        # 3. 重排序（可选）
        if rerank and len(candidates) > top_k:
            candidates = await self._rerank(query, candidates, rerank_model, top_k)
        else:
            candidates = candidates[:top_k]
        
        # 4. 构建上下文
        context = self._build_context(candidates, context_token_limit)
        
        # 5. 使用 LLM Service 生成答案（解耦后的调用）
        answer = await self.llm_service.generate_with_context(
            query=query,
            context=context
        )
        
        # 6. 格式化返回结果
        sources = self._format_sources(candidates)
        
        return {
            "query": query,
            "answer": answer,
            "sources": sources,
            "metadata": {
                "retrieval_count": len(candidates),
                "reranked": rerank,
                "context_length": len(context)
            }
        }
    
    async def _multi_source_retrieve(
        self,
        user_id: str,
        query: str,
        query_vector: List[float],
        top_k: int,
        include_conversations: bool
    ) -> List[Dict]:
        """多源检索：公共库 + 私有库 + 历史会话"""
        all_candidates = []
        
        # 检索公共文档库
        try:
            public_candidates = vector_service.search(
                collection_name="public_documents",
                query_vector=query_vector,
                top_k=int(top_k * self.public_weight),
                expr="valid == true"
            )
            
            for candidate in public_candidates:
                candidate["source_type"] = "public"
                candidate["weighted_score"] = candidate["score"] * self.public_weight
            
            all_candidates.extend(public_candidates)
            logger.info(f"公共库检索到 {len(public_candidates)} 条结果")
            
        except Exception as e:
            logger.error(f"公共库检索失败: {e}")
        
        # 检索私有文档库
        try:
            partition_name = f"user_{user_id}"
            private_candidates = vector_service.search(
                collection_name="private_documents",
                query_vector=query_vector,
                top_k=int(top_k * self.private_weight),
                partition_names=[partition_name],
                expr="valid == true"
            )

            for candidate in private_candidates:
                candidate["source_type"] = "private"
                candidate["weighted_score"] = candidate["score"] * self.private_weight

            all_candidates.extend(private_candidates)

            if private_candidates:
                logger.info(f"私有文档库检索到 {len(private_candidates)} 条结果")
            else:
                logger.info("私有文档库存在，但当前用户暂无可用私有文档")

        except Exception as e:
            err_msg = str(e)

            # Milvus 分区不存在：正常情况，不算错误
            if "partition name" in err_msg and "not found" in err_msg:
                logger.info("私有文档库暂无数据（用户尚未上传私有文档）")
            else:
                logger.error(f"私有文档库检索异常: {e}")

        
        # 检索历史会话
        if include_conversations:
            try:
                conv_candidates = await conversation_service.search_conversations(
                    user_id=user_id,
                    query=query,
                    query_vector=query_vector,
                    top_k=int(top_k * self.conv_weight)
                )
                
                for candidate in conv_candidates:
                    candidate["source_type"] = "conversation"
                    candidate["weighted_score"] = candidate["score"] * self.conv_weight
                
                all_candidates.extend(conv_candidates)
                logger.info(f"历史会话检索到 {len(conv_candidates)} 条结果")
                
            except Exception as e:
                logger.warning("历史会话检索失败，已跳过该来源", exc_info=True)
        
        # 按加权分数排序
        all_candidates.sort(key=lambda x: x["weighted_score"], reverse=True)
        
        return all_candidates
    
    async def _rerank(
        self,
        query: str,
        candidates: List[Dict],
        model: str,
        top_k: int
    ) -> List[Dict]:
        """
        重排序（如果 DeepSeek 不支持 rerank，这里会失败并使用原始排序）
        注意：DeepSeek API 可能不支持 /rerank 端点
        """
        try:
            # 尝试使用重排序（如果不支持会捕获异常）
            import httpx
            
            async with httpx.AsyncClient(timeout=30.0) as client:
                pairs = []
                for candidate in candidates:
                    text = candidate.get("chunk_content") or candidate.get("answer", "")
                    pairs.append([query, text])
                
                rerank_url = settings.LLM_API_URL.replace('/chat/completions', '/rerank')
                
                response = await client.post(
                    rerank_url,
                    json={
                        "model": model,
                        "query": query,
                        "passages": [p[1] for p in pairs]
                    },
                    headers={"Authorization": f"Bearer {settings.LLM_API_KEY}"}
                )
                
                if response.status_code == 200:
                    result = response.json()
                    scores = result.get("scores", [])
                    
                    for i, candidate in enumerate(candidates):
                        if i < len(scores):
                            candidate["rerank_score"] = scores[i]
                    
                    candidates.sort(key=lambda x: x.get("rerank_score", 0), reverse=True)
                    logger.info("重排序完成")
                else:
                    logger.warning(f"重排序接口返回 {response.status_code}，使用原始排序")
            
        except Exception as e:
            logger.warning(f"重排序失败: {e}，使用原始排序（这是正常的，DeepSeek 不支持 rerank）")
        
        return candidates[:top_k]
    
    def _build_context(self, candidates: List[Dict], token_limit: int) -> str:
        """构建上下文"""
        context_parts = []
        total_tokens = 0
        
        for i, candidate in enumerate(candidates):
            # 区分历史会话和文档
            if candidate["source_type"] == "conversation":
                text = f"历史问答：\nQ: {candidate.get('query', '')}\nA: {candidate.get('answer', '')}"
            else:
                text = f"文档片段（{candidate.get('doc_type', '未知类型')} - {candidate.get('title', '无标题')}）：\n{candidate.get('chunk_content', '')}"
            
            # 估算 token 数量（简单估算：1个字符约1.5个token）
            estimated_tokens = len(text) * 1.5
            
            if total_tokens + estimated_tokens > token_limit:
                break
            
            context_parts.append(f"[参考资料 {i+1}]\n{text}\n")
            total_tokens += estimated_tokens
        
        return "\n".join(context_parts)
    
    
    def _format_sources(self, candidates: List[Dict]) -> List[Dict]:
        """格式化来源信息"""
        sources = []
        
        for candidate in candidates:
            source = {
                "type": candidate["source_type"],
                "score": candidate.get("weighted_score", candidate.get("score", 0))
            }
            
            if candidate["source_type"] == "conversation":
                source.update({
                    "conv_id": candidate.get("id"),
                    "query": candidate.get("query"),
                    "answer": candidate.get("answer")[:100] + "..."
                })
            else:
                source.update({
                    "doc_id": candidate.get("doc_id"),
                    "title": candidate.get("title"),
                    "doc_type": candidate.get("doc_type"),
                    "chunk_index": candidate.get("chunk_index"),
                    "content": candidate.get("chunk_content", "")[:200] + "..."
                })
            
            sources.append(source)
        
        return sources


rag_service = RAGService()