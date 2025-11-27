import logging
from typing import List, Optional
from sqlalchemy.orm import Session
from app.models import database, schemas2
from app.utils.text_processor import TextProcessor
from app.llm_client.generators import get_client
from pathlib import Path
import time
from docx import Document
from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.lib.pagesizes import A4
from reportlab.lib.units import mm
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.lib.enums import TA_LEFT
from pathlib import Path
from datetime import datetime
from docx import Document
from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer
from reportlab.lib.styles import getSampleStyleSheet
from reportlab.lib.pagesizes import A4
from app.services.meeting_service import file_service
import re
from reportlab.lib import colors

logger = logging.getLogger(__name__)


class MinutesService:
    # 生成会议纪要（基于会议内容文本）
    def generate_minutes(self, db: Session, meeting_id: int, selected_file_ids: Optional[List[int]] = None, create_new_version: bool = False):
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

        # 检查是否已有纪要，如果有则根据 create_new_version 决定是更新还是创建新记录
        existing_minutes = self.get_minutes_by_meeting(db, meeting_id)
        if existing_minutes and not create_new_version:
            # 更新现有纪要（覆盖）
            existing_minutes.title = f"会议纪要: {meeting.title}"
            existing_minutes.content = generated_content
            db.commit()
            db.refresh(existing_minutes)
            return existing_minutes
        else:
            # 创建新纪要（或原先不存在）
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
        # 返回该会议最新创建的纪要（按创建时间降序）
        return db.query(database.MeetingMinutes).filter(
            database.MeetingMinutes.meeting_id == meeting_id
        ).order_by(database.MeetingMinutes.created_at.desc()).first()
    
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

    # helper to convert simple color css/rgb spans to ReportLab-compatible <font color="..."> tags
    def _rgb_to_hex(self, rgb_str: str) -> str:
        nums = re.findall(r"\d+", rgb_str)
        try:
            r, g, b = [int(n) for n in nums[:3]]
            return "#%02x%02x%02x" % (r, g, b)
        except Exception:
            return rgb_str

    def _convert_color_spans(self, text: str) -> str:
        if not text:
            return text
        # rgb(...) -> hex
        text = re.sub(r'<span\s+style="color:\s*rgb\(([^)]+)\)"\s*>', lambda m: f'<font color="{self._rgb_to_hex(m.group(1))}">', text, flags=re.IGNORECASE)
        # hex or named colors
        text = re.sub(r'<span\s+style="color:\s*([^;\"]+)\s*;?"\s*>', r'<font color="\1">', text, flags=re.IGNORECASE)
        text = text.replace('</span>', '</font>')
        return text

    # 导出为 DOCX，并把生成文件保存到 meeting_files/{meeting_id}/exports/
    def export_minutes_docx(self, db: Session, meeting_id: int):
        meeting = db.query(database.Meeting).filter(database.Meeting.id == meeting_id).first()
        if not meeting:
            return None

        minutes = self.get_minutes_by_meeting(db, meeting_id)
        if not minutes:
            return None

        # 目录
        repo_root = Path(__file__).resolve().parents[2]
        export_dir = repo_root / 'meeting_files' / str(meeting_id) / 'exports'
        export_dir.mkdir(parents=True, exist_ok=True)

        timestamp = int(time.time())
        filename = f"{timestamp}_minutes_{meeting_id}.docx"
        file_path = export_dir / filename

        doc = Document()
        doc.add_heading(minutes.title or f"会议纪要: {meeting.title}", level=1)

        # 基本信息
        doc.add_paragraph(f"时间: {meeting.date}")
        if meeting.location:
            doc.add_paragraph(f"地点: {meeting.location}")
        if meeting.host:
            doc.add_paragraph(f"主持人: {meeting.host}")
        if meeting.participants:
            doc.add_paragraph(f"参会人员: {meeting.participants}")

        doc.add_paragraph("")
        doc.add_paragraph("纪要内容:")
        # 将纪要内容按段落写入
        content = minutes.content or ''
        for para in content.split('\n'):
            doc.add_paragraph(para)

        # 保存
        doc.save(str(file_path))

        # 写入数据库文件记录
        file_record = database.MeetingFile(
            meeting_id=meeting_id,
            filename=filename,
            file_path=str(file_path),
            file_type='application/vnd.openxmlformats-officedocument.wordprocessingml.document'
        )
        db.add(file_record)
        db.commit()
        db.refresh(file_record)
        return file_record

    # 导出为 PDF（使用 ReportLab），保存到 meeting_files/{meeting_id}/exports/
    def export_minutes_pdf(self, db: Session, meeting_id: int):
        meeting = db.query(database.Meeting).filter(database.Meeting.id == meeting_id).first()
        if not meeting:
            return None

        minutes = self.get_minutes_by_meeting(db, meeting_id)
        if not minutes:
            return None

        repo_root = Path(__file__).resolve().parents[2]
        export_dir = repo_root / 'meeting_files' / str(meeting_id) / 'exports'
        export_dir.mkdir(parents=True, exist_ok=True)

        timestamp = int(time.time())
        filename = f"{timestamp}_minutes_{meeting_id}.pdf"
        file_path = export_dir / filename

        # PDF 样式
        doc = SimpleDocTemplate(str(file_path), pagesize=A4,
                                leftMargin=20*mm, rightMargin=20*mm, topMargin=20*mm, bottomMargin=20*mm)
        styles = getSampleStyleSheet()
        # 尝试注册常见中文字体（如系统存在）
        try:
            pdfmetrics.registerFont(TTFont('SimSun', '/usr/share/fonts/truetype/arphic/uming.ttf'))
            base_style = ParagraphStyle('Base', parent=styles['Normal'], fontName='SimSun', fontSize=11, leading=14)
            heading_style = ParagraphStyle('Heading', parent=styles['Heading1'], fontName='SimSun', fontSize=16, leading=20, alignment=TA_LEFT)
        except Exception:
            base_style = styles['Normal']
            heading_style = styles['Heading1']

        story = []
        # helper: convert simple HTML color spans to ReportLab <font color="..."> tags
        def _rgb_to_hex(m: re.Match) -> str:
            # convert rgb(r,g,b) to #rrggbb
            nums = re.findall(r"\d+", m.group(1))
            try:
                r, g, b = [int(n) for n in nums[:3]]
                return "#%02x%02x%02x" % (r, g, b)
            except Exception:
                return m.group(1)

        def _convert_color_spans(text: str) -> str:
            if not text:
                return text
            # replace <span style="color: rgb(...)"> or <span style="color: #..."> with <font color="#...">
            # handle rgb(...) -> hex
            text = re.sub(r'<span\s+style="color:\s*rgb\(([^)]+)\)"\s*>', lambda m: f'<font color="{_rgb_to_hex(m)}">', text, flags=re.IGNORECASE)
            # handle hex colors or named colors
            text = re.sub(r'<span\s+style="color:\s*([^;\"]+)\s*;?"\s*>', r'<font color="\1">', text, flags=re.IGNORECASE)
            # close spans
            text = text.replace('</span>', '</font>')
            return text

        title_text = minutes.title or f"会议纪要: {meeting.title}"
        story.append(Paragraph(_convert_color_spans(title_text), heading_style))
        story.append(Spacer(1, 6))

        meeting_info = ''
        meeting_info += f"时间: {meeting.date}<br/>"
        if meeting.location:
            meeting_info += f"地点: {meeting.location}<br/>"
        if meeting.host:
            meeting_info += f"主持人: {meeting.host}<br/>"
        if meeting.participants:
            meeting_info += f"参会人员: {meeting.participants}<br/>"

        story.append(Paragraph(_convert_color_spans(meeting_info), base_style))
        story.append(Spacer(1, 8))

        content = minutes.content or ''
        for para in content.split('\n'):
            if para.strip():
                story.append(Paragraph(_convert_color_spans(para.strip()), base_style))
                story.append(Spacer(1, 4))

        try:
            doc.build(story)
        except Exception as e:
            logger.exception(f"生成 PDF 文件失败: {e}")
            return None

        # 写入数据库文件记录
        file_record = database.MeetingFile(
            meeting_id=meeting_id,
            filename=filename,
            file_path=str(file_path),
            file_type='application/pdf'
        )
        db.add(file_record)
        db.commit()
        db.refresh(file_record)
        return file_record

    def export_minutes(self, db: Session, meeting_id: int, formats: Optional[List[str]] = None):
        """Export minutes to DOCX and/or PDF and save files into meeting_files/{meeting_id}/exports.

        Returns list of created MeetingFile DB objects.
        """
        if formats is None:
            formats = ["docx", "pdf"]

        allowed = {"docx", "pdf"}
        formats = [f.lower() for f in formats if f and f.lower() in allowed]
        if not formats:
            raise ValueError("至少指定一种导出格式: docx 或 pdf")

        meeting = db.query(database.Meeting).filter(database.Meeting.id == meeting_id).first()
        if not meeting:
            return None

        minutes = self.get_minutes_by_meeting(db, meeting_id)
        if not minutes:
            return None

        # prepare text content
        content = minutes.content or ""
        title = minutes.title or f"会议纪要: {meeting.title}"

        # storage dir: repo_root/meeting_files/{meeting_id}/exports
        repo_root = Path(__file__).resolve().parents[2]
        export_dir = repo_root / "meeting_files" / str(meeting_id) / "exports"
        export_dir.mkdir(parents=True, exist_ok=True)

        created_files = []
        timestamp = datetime.utcnow().strftime("%Y%m%d%H%M%S")

        # create DOCX
        if "docx" in formats:
            docx_name = f"minutes_{timestamp}.docx"
            docx_path = export_dir / docx_name
            try:
                doc = Document()
                doc.add_heading(title, level=1)
                doc.add_paragraph(f"生成时间: {datetime.utcnow().isoformat()}")
                doc.add_paragraph("")
                for line in content.splitlines():
                    doc.add_paragraph(line)
                doc.save(str(docx_path))

                # create DB record
                file_record = schemas2.MeetingFileCreate(
                    meeting_id=meeting_id,
                    filename=docx_name,
                    file_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
                    file_path=str(docx_path),
                )
                created = file_service.create_file(db, file_record)
                created_files.append(created)
            except Exception as e:
                logger.exception(f"导出 DOCX 失败: {e}")

        # create PDF
        if "pdf" in formats:
            pdf_name = f"minutes_{timestamp}.pdf"
            pdf_path = export_dir / pdf_name
            try:
                styles = getSampleStyleSheet()
                story = []
                story.append(Paragraph(self._convert_color_spans(title), styles['Title']))
                story.append(Paragraph(self._convert_color_spans(f"生成时间: {datetime.utcnow().isoformat()}"), styles['Normal']))
                story.append(Spacer(1, 12))
                for line in content.splitlines():
                    if line.strip() == "":
                        story.append(Spacer(1, 6))
                    else:
                        story.append(Paragraph(self._convert_color_spans(line), styles['Normal']))

                doc = SimpleDocTemplate(str(pdf_path), pagesize=A4)
                doc.build(story)

                file_record = schemas2.MeetingFileCreate(
                    meeting_id=meeting_id,
                    filename=pdf_name,
                    file_type="application/pdf",
                    file_path=str(pdf_path),
                )
                created = file_service.create_file(db, file_record)
                created_files.append(created)
            except Exception as e:
                logger.exception(f"导出 PDF 失败: {e}")

        return created_files

# 创建服务实例
minutes_service = MinutesService()
