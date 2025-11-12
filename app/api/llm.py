from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import StreamingResponse
from typing import List, Dict, Optional
from pydantic import BaseModel
from app.services.llm_service import llm_service
from app.utils.auth import get_current_user
from app.utils.logger import logger

router = APIRouter(prefix="/api/llm", tags=["大模型服务"])


class ChatRequest(BaseModel):
    """聊天请求"""
    messages: List[Dict[str, str]]
    model: Optional[str] = None
    temperature: float = 0.7
    max_tokens: int = 2000
    stream: bool = False


class ChatResponse(BaseModel):
    """聊天响应"""
    content: str
    model: str
    usage: Optional[Dict] = None


class SummarizeRequest(BaseModel):
    """摘要请求"""
    text: str
    max_length: int = 200
    style: str = "concise"  # concise/detailed/bullet_points


class SummarizeResponse(BaseModel):
    """摘要响应"""
    summary: str
    original_length: int
    summary_length: int


@router.post("/chat", response_model=ChatResponse)
async def chat(
    request: ChatRequest,
    current_user: dict = Depends(get_current_user)
):
    """
    通用聊天接口
    
    消息格式：
    [
        {"role": "system", "content": "系统提示"},
        {"role": "user", "content": "用户消息"},
        {"role": "assistant", "content": "助手回复"}
    ]
    """
    try:
        if request.stream:
            # 流式输出
            async def generate():
                async for chunk in llm_service.stream_chat(
                    messages=request.messages,
                    model=request.model,
                    temperature=request.temperature,
                    max_tokens=request.max_tokens
                ):
                    yield f"data: {chunk}\n\n"
                yield "data: [DONE]\n\n"
            
            return StreamingResponse(
                generate(),
                media_type="text/event-stream"
            )
        else:
            # 普通输出
            content = await llm_service.chat(
                messages=request.messages,
                model=request.model,
                temperature=request.temperature,
                max_tokens=request.max_tokens
            )
            
            return ChatResponse(
                content=content,
                model=request.model or "default"
            )
        
    except Exception as e:
        logger.error(f"聊天失败: {e}")
        raise HTTPException(status_code=500, detail=f"LLM 服务错误: {str(e)}")


@router.post("/summarize", response_model=SummarizeResponse)
async def summarize(
    request: SummarizeRequest,
    current_user: dict = Depends(get_current_user)
):
    """
    文本摘要
    
    摘要风格：
    - concise: 简洁（一句话）
    - detailed: 详细
    - bullet_points: 要点列表
    """
    try:
        summary = await llm_service.summarize(
            text=request.text,
            max_length=request.max_length,
            style=request.style
        )
        
        return SummarizeResponse(
            summary=summary,
            original_length=len(request.text),
            summary_length=len(summary)
        )
        
    except Exception as e:
        logger.error(f"摘要生成失败: {e}")
        raise HTTPException(status_code=500, detail=f"LLM 服务错误: {str(e)}")