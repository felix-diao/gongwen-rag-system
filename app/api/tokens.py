"""Token 消耗查询 API。"""

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session
from typing import Optional

from app.models.database import get_db
from app.models.schemas import (
    StandardResponse,
    TokenDailyStat,
    TokenUsageListResponse,
    TokenUsageQuery,
    TokenUsageRecordInDB,
    TokenUsageSummary,
)
from app.services.token_tracker import token_tracker
from app.utils.auth import get_current_user
from app.utils.logger import get_logger

logger = get_logger("tokens_api")

router = APIRouter(prefix="/api/tokens", tags=["Token消耗追踪"])


@router.get("/usage", response_model=StandardResponse[TokenUsageListResponse])
def list_usage(
    start_date: Optional[str] = Query(None, description="开始日期 YYYY-MM-DD"),
    end_date: Optional[str] = Query(None, description="结束日期 YYYY-MM-DD"),
    user_id: Optional[str] = Query(None),
    api_category: Optional[str] = Query(None, description="llm / volc_miaoji / volc_asr / qwen_asr"),
    model: Optional[str] = Query(None),
    page: int = Query(1, ge=1),
    page_size: int = Query(50, ge=1, le=500),
    db: Session = Depends(get_db),
    current_user: dict = Depends(get_current_user),
):
    """分页查询 token 消耗明细。"""
    try:
        result = token_tracker.query_usage(
            db=db,
            start_date=start_date,
            end_date=end_date,
            user_id=user_id,
            api_category=api_category,
            model=model,
            page=page,
            page_size=page_size,
        )
        items = [TokenUsageRecordInDB.model_validate(r) for r in result["items"]]
        return StandardResponse(
            success=True,
            data=TokenUsageListResponse(
                items=items,
                total=result["total"],
                page=result["page"],
                page_size=result["page_size"],
            ),
            message="查询成功",
        )
    except Exception as e:
        logger.error(f"查询 token 消耗失败: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/summary", response_model=StandardResponse[TokenUsageSummary])
def get_summary(
    start_date: Optional[str] = Query(None, description="开始日期 YYYY-MM-DD"),
    end_date: Optional[str] = Query(None, description="结束日期 YYYY-MM-DD"),
    db: Session = Depends(get_db),
    current_user: dict = Depends(get_current_user),
):
    """Token 消耗聚合统计。"""
    try:
        data = token_tracker.get_summary(db=db, start_date=start_date, end_date=end_date)
        return StandardResponse(
            success=True,
            data=TokenUsageSummary(**data),
            message="查询成功",
        )
    except Exception as e:
        logger.error(f"查询 token 统计失败: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/stats/daily", response_model=StandardResponse[list])
def get_daily_stats(
    start_date: Optional[str] = Query(None, description="开始日期 YYYY-MM-DD"),
    end_date: Optional[str] = Query(None, description="结束日期 YYYY-MM-DD"),
    db: Session = Depends(get_db),
    current_user: dict = Depends(get_current_user),
):
    """按天统计 token 消耗趋势。"""
    try:
        data = token_tracker.get_daily_stats(
            db=db,
            start_date=start_date,
            end_date=end_date,
        )
        return StandardResponse(
            success=True,
            data=data,
            message="查询成功",
        )
    except Exception as e:
        logger.error(f"查询 token 趋势失败: {e}")
        raise HTTPException(status_code=500, detail=str(e))
