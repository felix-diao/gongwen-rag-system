"""Token 消耗追踪服务 —— 统一记录和查询所有 LLM API 的 token 用量。"""

from __future__ import annotations

from datetime import date, datetime, timedelta
from typing import Any, Dict, List, Optional

from sqlalchemy import case, func, text
from sqlalchemy.orm import Session

from app.models.database import SessionLocal, TokenUsageRecord
from app.utils.logger import get_logger

logger = get_logger("token_tracker")


class TokenTracker:
    """Token 消耗记录与查询服务。"""

    @staticmethod
    def record(
        *,
        user_id: Optional[str] = None,
        api_category: str = "llm",
        api_endpoint: str = "",
        model: Optional[str] = None,
        prompt_tokens: int = 0,
        completion_tokens: int = 0,
        total_tokens: int = 0,
        request_chars: int = 0,
        duration_ms: Optional[int] = None,
        status: str = "success",
        error_msg: Optional[str] = None,
        metadata_json: Optional[str] = None,
    ) -> None:
        """写一条消耗记录（在现有 db session 上操作，由调用方传 db）。"""
        db: Optional[Session] = None
        try:
            db = SessionLocal()
            record = TokenUsageRecord(
                user_id=user_id,
                api_category=api_category,
                api_endpoint=api_endpoint,
                model=model,
                prompt_tokens=prompt_tokens,
                completion_tokens=completion_tokens,
                total_tokens=total_tokens,
                request_chars=request_chars,
                duration_ms=duration_ms,
                status=status,
                error_msg=error_msg,
                metadata_json=metadata_json,
            )
            db.add(record)
            db.commit()
        except Exception:
            logger.exception("Token 记录写入失败")
        finally:
            if db is not None:
                db.close()

    @staticmethod
    def query_usage(
        db: Session,
        *,
        start_date: Optional[str] = None,
        end_date: Optional[str] = None,
        user_id: Optional[str] = None,
        api_category: Optional[str] = None,
        model: Optional[str] = None,
        page: int = 1,
        page_size: int = 50,
    ) -> Dict[str, Any]:
        """分页查询 token 消耗明细。"""
        q = db.query(TokenUsageRecord)
        if start_date:
            q = q.filter(TokenUsageRecord.created_at >= _parse_date_start(start_date))
        if end_date:
            q = q.filter(TokenUsageRecord.created_at <= _parse_date_end(end_date))
        if user_id:
            q = q.filter(TokenUsageRecord.user_id == user_id)
        if api_category:
            q = q.filter(TokenUsageRecord.api_category == api_category)
        if model:
            q = q.filter(TokenUsageRecord.model == model)

        total = q.count()
        items = (
            q.order_by(TokenUsageRecord.created_at.desc())
            .offset((page - 1) * page_size)
            .limit(page_size)
            .all()
        )
        return {"items": items, "total": total, "page": page, "page_size": page_size}

    @staticmethod
    def get_summary(
        db: Session,
        *,
        start_date: Optional[str] = None,
        end_date: Optional[str] = None,
    ) -> Dict[str, Any]:
        """聚合统计 token 消耗。"""
        q = db.query(TokenUsageRecord)
        if start_date:
            q = q.filter(TokenUsageRecord.created_at >= _parse_date_start(start_date))
        if end_date:
            q = q.filter(TokenUsageRecord.created_at <= _parse_date_end(end_date))

        agg = q.with_entities(
            func.coalesce(func.sum(TokenUsageRecord.total_tokens), 0).label("total_tokens"),
            func.coalesce(func.sum(TokenUsageRecord.prompt_tokens), 0).label("total_prompt"),
            func.coalesce(func.sum(TokenUsageRecord.completion_tokens), 0).label("total_completion"),
            func.count(TokenUsageRecord.id).label("total_calls"),
            func.coalesce(
                func.sum(case((TokenUsageRecord.status == "error", 1), else_=0)),
                0,
            ).label("total_errors"),
        ).first()

        # 按类别
        by_cat = (
            q.with_entities(
                TokenUsageRecord.api_category,
                func.coalesce(func.sum(TokenUsageRecord.total_tokens), 0).label("tokens"),
                func.count(TokenUsageRecord.id).label("calls"),
            )
            .group_by(TokenUsageRecord.api_category)
            .all()
        )
        by_model = (
            q.with_entities(
                TokenUsageRecord.model,
                func.coalesce(func.sum(TokenUsageRecord.total_tokens), 0).label("tokens"),
                func.count(TokenUsageRecord.id).label("calls"),
            )
            .filter(TokenUsageRecord.model.isnot(None))
            .group_by(TokenUsageRecord.model)
            .all()
        )
        by_user = (
            q.with_entities(
                TokenUsageRecord.user_id,
                func.coalesce(func.sum(TokenUsageRecord.total_tokens), 0).label("tokens"),
                func.count(TokenUsageRecord.id).label("calls"),
            )
            .filter(TokenUsageRecord.user_id.isnot(None))
            .group_by(TokenUsageRecord.user_id)
            .order_by(text("tokens DESC"))
            .limit(20)
            .all()
        )

        return {
            "total_tokens": agg.total_tokens,
            "total_prompt_tokens": agg.total_prompt,
            "total_completion_tokens": agg.total_completion,
            "total_calls": agg.total_calls,
            "total_errors": agg.total_errors,
            "by_category": [{"category": r.api_category, "tokens": r.tokens, "calls": r.calls} for r in by_cat],
            "by_model": [{"model": r.model, "tokens": r.tokens, "calls": r.calls} for r in by_model],
            "by_user": [{"user_id": r.user_id, "tokens": r.tokens, "calls": r.calls} for r in by_user],
        }

    @staticmethod
    def get_daily_stats(
        db: Session,
        *,
        start_date: Optional[str] = None,
        end_date: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        """按天统计 token 消耗趋势。"""
        if not end_date:
            end_dt = datetime.utcnow()
        else:
            end_dt = _parse_date_end(end_date)
        if not start_date:
            start_dt = end_dt - timedelta(days=30)
        else:
            start_dt = _parse_date_start(start_date)

        rows = (
            db.query(
                func.date(TokenUsageRecord.created_at).label("d"),
                func.coalesce(func.sum(TokenUsageRecord.total_tokens), 0).label("tokens"),
                func.count(TokenUsageRecord.id).label("calls"),
            )
            .filter(TokenUsageRecord.created_at >= start_dt, TokenUsageRecord.created_at <= end_dt)
            .group_by(text("d"))
            .order_by(text("d"))
            .all()
        )

        # 补全缺失的日期
        result: List[Dict[str, Any]] = []
        cursor = start_dt.date()
        row_map = {r.d: r for r in rows if isinstance(r.d, date)}
        while cursor <= end_dt.date():
            r = row_map.get(cursor)
            result.append({
                "date": cursor.isoformat(),
                "total_tokens": r.tokens if r else 0,
                "total_calls": r.calls if r else 0,
            })
            cursor += timedelta(days=1)
        return result


def _parse_date_start(val: str) -> datetime:
    return datetime.strptime(val.strip(), "%Y-%m-%d")


def _parse_date_end(val: str) -> datetime:
    return datetime.strptime(val.strip(), "%Y-%m-%d") + timedelta(days=1) - timedelta(seconds=1)


token_tracker = TokenTracker()
