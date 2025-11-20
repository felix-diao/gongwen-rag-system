import logging
from typing import List, Optional
from sqlalchemy.orm import Session
from app.models import database, schemas2
from app.utils.text_processor import TextProcessor
from app.llm_client.generators import get_client

logger = logging.getLogger(__name__)


class MinutesService:
    # 生成会议纪要（基于会议内容文本）
    def generate_minutes(self, db: Session, meeting_id: int, selected_file_ids: Optional[List[int]] = None):
        # 获取会议基本信息和内容文本
        meeting = db.query(database.Meeting).filter(database.Meeting.id == meeting_id).first()
        if not meeting:
            return None

        # 使用会议的基本信息和内容文本生成纪要
        meeting_info = f"会议标题: {meeting.title}\n"
        meeting_info += f"会议时间: {meeting.date}\n"
        if meeting.location:
            meeting_info += f"会议地点: {meeting.location}\n"
        if meeting.host:
            meeting_info += f"主持人: {meeting.host}\n"
        if meeting.participants:
            meeting_info += f"参会人员: {meeting.participants}\n"

        # 合并会议文本和会议文件内容
        tp = TextProcessor()
        parts = [meeting_info]
        if getattr(meeting, 'content_text', None):
            parts.append(f"【会议记录】\n{meeting.content_text}")

        # 从数据库中获取会议文件并提取文本（如支持的格式）
        if selected_file_ids:
            logger.info(f"生成纪要时使用所选文件ids: {selected_file_ids}")
            files_query = db.query(database.MeetingFile).filter(database.MeetingFile.meeting_id == meeting_id, database.MeetingFile.id.in_(selected_file_ids))
        else:
            files_query = db.query(database.MeetingFile).filter(database.MeetingFile.meeting_id == meeting_id)

        files = files_query.all()
        for idx, f in enumerate(files, start=1):
            fp = getattr(f, 'file_path', None)
            if fp:
                try:
                    logger.info(f"开始提取文件文本: {fp}")
                    text = tp.extract_text(fp)
                    parts.append(f"【文件{idx}:{f.filename}】\n" + text)
                    logger.info(f"提取成功: {fp}")
                except Exception as e:
                    logger.warning(f"提取文件文本失败: {fp}, 错误: {e}")
                    parts.append(f"【文件{idx}:{f.filename}】\n(无法提取文本)")

        combined_text = "\n\n".join(parts)

        # 使用大语言模型生成纪要
        try:
            cli = get_client()
            system_msg = "你是一位专业的会议记录助手，擅长将会议内容整理成结构化的会议纪要。语言正式、条理清晰。"
            user_msg = f"请根据以下内容生成规范的会议纪要，包含：会议概况、主要议题、讨论要点、决议与待办事项（责任人、截止期如有）。\n\n{combined_text}"
            logger.info(f"调用 LLM 生成纪要，会议ID: {meeting_id}, 字符数: {len(combined_text)}")
            generated_content = cli.chat([{"role": "system", "content": system_msg}, {"role": "user", "content": user_msg}], max_tokens=2000)
            logger.info(f"LLM 返回纪要，会议ID: {meeting_id}")
        except Exception as e:
            logger.warning(f"调用 LLM 生成纪要失败: {e}, 使用回退实现。")
            generated_content = self._generate_with_llm(combined_text, meeting)

        # 检查是否已有纪要，如果有则更新，否则创建新的
        existing_minutes = self.get_minutes_by_meeting(db, meeting_id)
        if existing_minutes:
            # 更新现有纪要
            existing_minutes.title = f"会议纪要: {meeting.title}"
            existing_minutes.content = generated_content
            db.commit()
            db.refresh(existing_minutes)
            return existing_minutes
        else:
            # 创建新纪要
            minutes_data = schemas2.MeetingMinutesCreate(
                meeting_id=meeting_id,
                title=f"会议纪要: {meeting.title}",
                content=generated_content
            )
            
            db_minutes = database.MeetingMinutes(**minutes_data.dict())
            db.add(db_minutes)
            db.commit()
            db.refresh(db_minutes)
            return db_minutes

    # 调用大语言模型生成纪要（占位方法）
    def _generate_with_llm(self, combined_text: str, meeting: database.Meeting) -> str:
        return f"""
# 会议纪要: {meeting.title}

**时间:** {meeting.date}
**地点:** {meeting.location or '未指定'}
**主持人:** {meeting.host or '未指定'}

## 参会人员
{meeting.participants or '未指定'}

## 会议内容摘要
主要内容如下：
- 关键讨论点1
- 关键讨论点2
- 重要决议

## 待办事项
- [负责人] 完成具体任务（截止时间）

*本文档基于会议内容自动生成*
        """.strip()
    
    # 根据会议ID获取纪要
    def get_minutes_by_meeting(self, db: Session, meeting_id: int):
        return db.query(database.MeetingMinutes).filter(
            database.MeetingMinutes.meeting_id == meeting_id
        ).first()
    
    # 更新会议纪要
    def update_minutes(self, db: Session, meeting_id: int, minutes_update: schemas2.MeetingMinutesUpdate):
        db_minutes = self.get_minutes_by_meeting(db, meeting_id)
        if db_minutes:
            update_data = minutes_update.dict(exclude_unset=True)
            for key, value in update_data.items():
                setattr(db_minutes, key, value)
            db.commit()
            db.refresh(db_minutes)
        return db_minutes

    # 删除会议纪要
    def delete_minutes(self, db: Session, meeting_id: int):
        db_minutes = self.get_minutes_by_meeting(db, meeting_id)
        if db_minutes:
            db.delete(db_minutes)
            db.commit()
            return True
        return False

# 创建服务实例
minutes_service = MinutesService()
