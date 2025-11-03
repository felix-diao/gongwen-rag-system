from fastapi import APIRouter, Depends, HTTPException
from app.models.schemas import EmbedRequest, EmbedResponse
from app.services.embedding_service import embedding_service
from app.utils.auth import get_current_user

router = APIRouter(prefix="/api/embed", tags=["向量化"])

@router.post("/", response_model=EmbedResponse)
async def embed_texts(
    request: EmbedRequest,
    current_user: dict = Depends(get_current_user)
):
    """批量文本向量化"""
    try:
        texts = [item.get("text", "") for item in request.inputs]
        embeddings = await embedding_service.embed_texts(texts)
        
        results = []
        for i, (item, embedding) in enumerate(zip(request.inputs, embeddings)):
            results.append({
                "id": item.get("id", f"embed_{i}"),
                "embedding": embedding
            })
        
        return EmbedResponse(embeddings=results)
        
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"向量化失败: {str(e)}")