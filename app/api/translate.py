from fastapi import APIRouter, Depends, HTTPException
from typing import List
from pydantic import BaseModel
from app.services.llm_service import llm_service
from app.utils.auth import get_current_user
from app.utils.logger import logger

router = APIRouter(prefix="/api/translate", tags=["翻译服务"])


class TranslateRequest(BaseModel):
    """翻译请求"""
    text: str
    from_lang: str = "zh-CN"
    to_lang: str = "en-US"


class TranslateResponse(BaseModel):
    """翻译响应"""
    translated_text: str
    from_lang: str
    to_lang: str
    original_text: str


class BatchTranslateRequest(BaseModel):
    """批量翻译请求"""
    texts: List[str]
    from_lang: str = "zh-CN"
    to_lang: str = "en-US"


class BatchTranslateResponse(BaseModel):
    """批量翻译响应"""
    results: List[str]
    from_lang: str
    to_lang: str


@router.post("/", response_model=TranslateResponse)
async def translate(
    request: TranslateRequest,
    current_user: dict = Depends(get_current_user)
):
    """
    翻译文本
    
    支持的语言代码：
    - zh-CN: 中文
    - en-US: 英文
    - ja-JP: 日文
    - ko-KR: 韩文
    - fr-FR: 法文
    - de-DE: 德文
    - es-ES: 西班牙文
    - ru-RU: 俄文
    """
    try:
        translated = await llm_service.translate(
            text=request.text,
            source_lang=request.from_lang,
            target_lang=request.to_lang
        )
        
        return TranslateResponse(
            translated_text=translated,
            from_lang=request.from_lang,
            to_lang=request.to_lang,
            original_text=request.text
        )
        
    except Exception as e:
        logger.error(f"翻译失败: {e}")
        raise HTTPException(status_code=500, detail=f"翻译服务错误: {str(e)}")


@router.post("/batch", response_model=BatchTranslateResponse)
async def batch_translate(
    request: BatchTranslateRequest,
    current_user: dict = Depends(get_current_user)
):
    """
    批量翻译文本
    
    自动分批处理，避免超过 API 频率限制
    """
    try:
        results = await llm_service.batch_translate(
            texts=request.texts,
            source_lang=request.from_lang,
            target_lang=request.to_lang
        )
        
        return BatchTranslateResponse(
            results=results,
            from_lang=request.from_lang,
            to_lang=request.to_lang
        )
        
    except Exception as e:
        logger.error(f"批量翻译失败: {e}")
        raise HTTPException(status_code=500, detail=f"翻译服务错误: {str(e)}")