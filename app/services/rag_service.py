from typing import List, Dict, Optional
import httpx
from app.services.vector_service import vector_service
from app.services.embedding_service import embedding_service
from app.services.conversation_service import conversation_service
from app.config import settings
from app.utils.logger import logger

class RAGService:
    """RAG 检索增强生成服务"""
    
    def __init__(self):
        self.llm_client = httpx.AsyncClient(timeout=60.0)
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
        
        query_vector = await embedding_service.embed_query(query)
        
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
        
        if rerank and len(candidates) > top_k:
            candidates = await self._rerank(query, candidates, rerank_model, top_k)
        else:
            candidates = candidates[:top_k]
        
        context = self._build_context(candidates, context_token_limit)
        
        answer = await self._generate_answer(
            query=query,
            context=context,
            model=generator or settings.LLM_MODEL
        )
        
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
            logger.info(f"私有库检索到 {len(private_candidates)} 条结果")
            
        except Exception as e:
            logger.error(f"私有库检索失败: {e}")
        
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
                logger.error(f"历史会话检索失败: {e}")
        
        all_candidates.sort(key=lambda x: x["weighted_score"], reverse=True)
        
        return all_candidates
    
    async def _rerank(
        self,
        query: str,
        candidates: List[Dict],
        model: str,
        top_k: int
    ) -> List[Dict]:
        """重排序"""
        try:
            pairs = []
            for candidate in candidates:
                text = candidate.get("chunk_content") or candidate.get("answer", "")
                pairs.append([query, text])
            
            response = await self.llm_client.post(
                f"{settings.LLM_API_URL.replace('/chat/completions', '/rerank')}",
                json={
                    "model": model,
                    "query": query,
                    "passages": [p[1] for p in pairs]
                }
            )
            
            if response.status_code == 200:
                result = response.json()
                scores = result.get("scores", [])
                
                for i, candidate in enumerate(candidates):
                    if i < len(scores):
                        candidate["rerank_score"] = scores[i]
                
                candidates.sort(key=lambda x: x.get("rerank_score", 0), reverse=True)
                logger.info("重排序完成")
            
        except Exception as e:
            logger.error(f"重排序失败: {e}，使用原始排序")
        
        return candidates[:top_k]
    
    def _build_context(self, candidates: List[Dict], token_limit: int) -> str:
        """构建上下文"""
        context_parts = []
        total_tokens = 0
        
        for i, candidate in enumerate(candidates):
            if candidate["source_type"] == "conversation":
                text = f"历史问答：\nQ: {candidate.get('query', '')}\nA: {candidate.get('answer', '')}"
            else:
                text = f"文档片段（{candidate.get('doc_type', '未知类型')} - {candidate.get('title', '无标题')}）：\n{candidate.get('chunk_content', '')}"
            
            estimated_tokens = len(text) * 1.5
            
            if total_tokens + estimated_tokens > token_limit:
                break
            
            context_parts.append(f"[参考资料 {i+1}]\n{text}\n")
            total_tokens += estimated_tokens
        
        return "\n".join(context_parts)
    
    async def _generate_answer(self, query: str, context: str, model: str) -> str:
        """调用 LLM 生成答案"""
        
        system_prompt = """你是一位专业的公文助手，擅长分析和解答各类公文相关问题。

任务要求：
1. 基于提供的参考资料回答用户问题
2. 回答要准确、专业、规范，符合公文写作要求
3. 如果参考资料不足以回答问题，请如实说明
4. 引用参考资料时，请注明来源（如：根据参考资料1...）
5. 对于公文格式、规范等问题，给出明确的指导

回答风格：
- 语言正式、严谨
- 条理清晰、逻辑严密
- 重点突出、简洁明了"""

        user_prompt = f"""参考资料：
{context}

用户问题：{query}

请基于以上参考资料，回答用户问题。"""

        try:
            response = await self.llm_client.post(
                settings.LLM_API_URL,
                json={
                    "model": model,
                    "messages": [
                        {"role": "system", "content": system_prompt},
                        {"role": "user", "content": user_prompt}
                    ],
                    "temperature": 0.7,
                    "max_tokens": 2000
                },
                headers={"Authorization": f"Bearer {settings.LLM_API_KEY}"}
            )
            
            response.raise_for_status()
            result = response.json()
            answer = result["choices"][0]["message"]["content"]
            
            logger.info("答案生成成功")
            return answer
            
        except Exception as e:
            logger.error(f"答案生成失败: {e}")
            return "抱歉，生成答案时出现错误，请稍后重试。"
    
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
    
    async def close(self):
        """关闭客户端"""
        await self.llm_client.aclose()

rag_service = RAGService()