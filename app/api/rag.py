from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import StreamingResponse
from sqlalchemy.orm import Session
import json
from app.models.database import get_db
from app.models.schemas import RAGRequest, ConversationCreate
from app.services.rag_service import rag_service
from app.services.conversation_service import conversation_service
from app.utils.auth import get_current_user
from app.utils.logger import logger

router = APIRouter(prefix="/api/rag", tags=["RAG检索生成"])

@router.post("/query")
async def rag_query(
    request: RAGRequest,
    db: Session = Depends(get_db),
    current_user: dict = Depends(get_current_user)
):
    """RAG 问答接口"""
    
    if request.user_id != current_user["user_id"]:
        raise HTTPException(status_code=403, detail="无权操作")
    
    try:
        result = await rag_service.retrieve_and_generate(
            user_id=request.user_id,
            query=request.query,
            top_k=request.top_k,
            rerank=request.rerank,
            rerank_model=request.rerank_model,
            generator=request.generator,
            context_token_limit=request.context_token_limit,
            include_conversations=request.include_conversations
        )
        
        conv_data = ConversationCreate(
            user_id=request.user_id,
            query=request.query,
            answer=result["answer"],
            weight=0.8,
            liked=False
        )
        
        await conversation_service.create_conversation(db, conv_data)
        
        return result
        
    except Exception as e:
        logger.error(f"RAG 查询失败: {e}")
        raise HTTPException(status_code=500, detail=f"查询失败: {str(e)}")

@router.post("/stream")
async def rag_query_stream(
    request: RAGRequest,
    db: Session = Depends(get_db),
    current_user: dict = Depends(get_current_user)
):
    """RAG 流式问答接口"""
    
    if request.user_id != current_user["user_id"]:
        raise HTTPException(status_code=403, detail="无权操作")
    
    async def generate():
        try:
            from app.services.embedding_service import embedding_service
            import httpx
            from app.config import settings
            
            query_vector = await embedding_service.embed_query(request.query)
            
            candidates = await rag_service._multi_source_retrieve(
                user_id=request.user_id,
                query=request.query,
                query_vector=query_vector,
                top_k=request.top_k * 2,
                include_conversations=request.include_conversations
            )
            
            yield f"data: {json.dumps({'type': 'retrieval', 'count': len(candidates)}, ensure_ascii=False)}\n\n"
            
            if request.rerank and len(candidates) > request.top_k:
                candidates = await rag_service._rerank(
                    request.query, 
                    candidates, 
                    request.rerank_model, 
                    request.top_k
                )
            else:
                candidates = candidates[:request.top_k]
            
            context = rag_service._build_context(candidates, request.context_token_limit)
            
            system_prompt = """你是一位专业的公文助手，擅长分析和解答各类公文相关问题。

任务要求：
1. 基于提供的参考资料回答用户问题
2. 回答要准确、专业、规范，符合公文写作要求
3. 如果参考资料不足以回答问题，请如实说明
4. 引用参考资料时，请注明来源（如：根据参考资料1...）

回答风格：
- 语言正式、严谨
- 条理清晰、逻辑严密
- 重点突出、简洁明了"""

            user_prompt = f"""参考资料：
{context}

用户问题：{request.query}

请基于以上参考资料，回答用户问题。"""

            async with httpx.AsyncClient(timeout=60.0) as client:
                async with client.stream(
                    "POST",
                    settings.LLM_API_URL,
                    json={
                        "model": request.generator or settings.LLM_MODEL,
                        "messages": [
                            {"role": "system", "content": system_prompt},
                            {"role": "user", "content": user_prompt}
                        ],
                        "temperature": 0.7,
                        "stream": True
                    },
                    headers={"Authorization": f"Bearer {settings.LLM_API_KEY}"}
                ) as response:
                    full_answer = ""
                    
                    async for line in response.aiter_lines():
                        if line.startswith("data: "):
                            data = line[6:]
                            if data == "[DONE]":
                                break
                            
                            try:
                                chunk = json.loads(data)
                                if "choices" in chunk:
                                    delta = chunk["choices"][0].get("delta", {})
                                    content = delta.get("content", "")
                                    if content:
                                        full_answer += content
                                        yield f"data: {json.dumps({'type': 'content', 'content': content}, ensure_ascii=False)}\n\n"
                            except json.JSONDecodeError:
                                continue
            
            sources = rag_service._format_sources(candidates)
            yield f"data: {json.dumps({'type': 'sources', 'sources': sources}, ensure_ascii=False)}\n\n"
            yield f"data: {json.dumps({'type': 'done'}, ensure_ascii=False)}\n\n"
            
            conv_data = ConversationCreate(
                user_id=request.user_id,
                query=request.query,
                answer=full_answer,
                weight=0.8,
                liked=False
            )
            await conversation_service.create_conversation(db, conv_data)
            
        except Exception as e:
            logger.error(f"流式查询失败: {e}")
            yield f"data: {json.dumps({'type': 'error', 'message': str(e)}, ensure_ascii=False)}\n\n"
    
    return StreamingResponse(generate(), media_type="text/event-stream")