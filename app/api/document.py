# app/routers/document.py
from fastapi import APIRouter, Depends, HTTPException, status
from app.services.document_service import DocumentService
from app.models.schemas import DocumentWriteRequest, DocumentOptimizeRequest,StandardResponse, DocumentData, DocumentDataOptimize
from app.llm_client.generators import generate_document_by_prompt, optimize_document
from app.services.rag_service import rag_service
from app.services.embedding_service import embedding_service
from app.models.schemas import RAGRequest
import json
from datetime import datetime, timezone
from app.models.database import get_db
from app.utils.auth import get_current_user
from sqlalchemy.orm import Session

router = APIRouter(prefix="/api/document", tags=["生成公文"])


def get_document_service() -> DocumentService:
    # 如需注入更多依赖（配置、DB、缓存），在这里构造
    return DocumentService()


@router.post("/write", response_model=StandardResponse[DocumentData])
async def document_write(
    req: DocumentWriteRequest,
    svc: DocumentService = Depends(get_document_service),
    db: Session = Depends(get_db),
    current_user: dict = Depends(get_current_user)
):
    """
    POST /document/write
    {
        "prompt": "关于开展校园安全检查的通知，要求包含三条措施…",
        "documentType": "article",
        "tone": "formal",
        "language": "zh"
    }
    """
    try:
        """
        负责拼装 prompt、调用 LLM 生成正文，返回生成后的 content 字符串
        """
        enhanced_prompt = req.prompt or ""

        if getattr(req, "title", None):
            enhanced_prompt = f"以 {req.title} 为题进行公文撰写\n\n{enhanced_prompt}"

        # 如果 requirement 不在 prompt 中则附加
        if getattr(req, "requirement", None) and req.requirement not in enhanced_prompt:
            enhanced_prompt = f"{enhanced_prompt}\n\n用户需求：{req.requirement}"

        current_user_id = current_user["user_id"]
        request = RAGRequest(
            user_id=current_user_id,
            query=enhanced_prompt
        )

        query_vector = await embedding_service.embed_query(request.query)
            
        candidates = await rag_service._multi_source_retrieve(
                user_id=current_user_id,
                query=request.query,
                query_vector=query_vector,
                top_k=request.top_k * 2,
                include_conversations=request.include_conversations
            )    
            
        ## yield f"data: {json.dumps({'type': 'retrieval', 'count': len(candidates)}, ensure_ascii=False)}\n\n"
            
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

        enhanced_prompt = f"{enhanced_prompt}\n\n参考资料如下：\n{context}\n。"
        print(f"enhanced_prompt: {enhanced_prompt}")
        content = generate_document_by_prompt(
            prompt=enhanced_prompt,
            document_type=req.documentType,
            tone=req.tone or "formal",
            language=req.language or "zh",
        )

        return StandardResponse(
            success=True,
            data=DocumentData(
                content=content,
                wordCount=len(content),
                generatedAt=datetime.now(timezone.utc)
            ),
            message="文档生成成功",
        )
    except Exception as e:
        # 也可按需细化成不同 HTTP 状态码
        return StandardResponse(
            success=False,
            data=DocumentData(
                content="",
                wordCount=0,
                generatedAt=datetime.now(timezone.utc)
            ),
            message=f"生成失败：{e}",
        )

# 小郭小郭看见了能不能回个消息

@router.post("/optimize", response_model=StandardResponse[DocumentDataOptimize])
async def document_optimize(
    req: DocumentOptimizeRequest,
    svc: DocumentService = Depends(get_document_service),
):
    """
    POST /document/optimize
    {
        "content": "我们要做好这项工作，效果很好。",
        "optimizationType": "all",
        "customInstruction": "使语气更正式"
    }
    """
    try:
        optimized_text = optimize_document(
            content=req.content,
            optimization_type=req.optimizationType,
            custom_instruction=req.customInstruction
        )
        return StandardResponse(
            success=True,
            data=DocumentDataOptimize(content=optimized_text),
            message="OK",
        )
    except Exception as e:
        return StandardResponse(
            success=False,
            data=DocumentDataOptimize(content=""),
            message=f"优化失败：{e}",
        )
