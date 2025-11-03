"""批量导入文档脚本"""
import sys
sys.path.append('.')

import asyncio
import os
from pathlib import Path
from app.models.database import SessionLocal
from app.models.schemas import DocumentCreate
from app.services.document_service import document_service
from app.utils.logger import logger

async def import_documents(directory: str, doc_type: str, owner_id: str = "public"):
    """批量导入文档"""
    db = SessionLocal()
    
    try:
        directory_path = Path(directory)
        if not directory_path.exists():
            logger.error(f"目录不存在: {directory}")
            return
        
        files = list(directory_path.glob("*.txt")) + \
                list(directory_path.glob("*.docx")) + \
                list(directory_path.glob("*.pdf"))
        
        logger.info(f"找到 {len(files)} 个文件")
        
        for file_path in files:
            try:
                logger.info(f"正在处理: {file_path.name}")
                
                doc_data = DocumentCreate(
                    owner_id=owner_id,
                    title=file_path.stem,
                    doc_type=doc_type,
                    tags=[],
                    weight=1.0
                )
                
                await document_service.create_document(db, doc_data, str(file_path))
                logger.info(f"成功导入: {file_path.name}")
                
            except Exception as e:
                logger.error(f"导入失败 {file_path.name}: {e}")
                continue
        
        logger.info("批量导入完成")
        
    finally:
        db.close()

if __name__ == "__main__":
    if len(sys.argv) < 3:
        print("使用方法: python scripts/import_documents.py <目录路径> <文档类型> [owner_id]")
        print("示例: python scripts/import_documents.py ./data/reports 报告 public")
        sys.exit(1)
    
    directory = sys.argv[1]
    doc_type = sys.argv[2]
    owner_id = sys.argv[3] if len(sys.argv) > 3 else "public"
    
    asyncio.run(import_documents(directory, doc_type, owner_id))