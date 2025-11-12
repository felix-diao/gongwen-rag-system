from typing import List, Optional
from sqlalchemy.orm import Session
from sqlalchemy import and_, any_
from fastapi import UploadFile, HTTPException
import os
import uuid
from datetime import datetime

from app.models.database import (
    KnowledgeBase as KnowledgeBaseModel,
    KnowledgeItem as KnowledgeItemModel,
)
from app.models.schemas import (
    DocumentCreate,
    KnowledgeBaseCreate,
    KnowledgeBaseUpdate,
)
from app.services.document_service import document_service
from app.config import settings
from app.utils.logger import logger

class KnowledgeService:
    """知识库管理服务"""
    
    def __init__(self):
        self.upload_dir = settings.UPLOAD_DIR
        os.makedirs(self.upload_dir, exist_ok=True)
    
    # ========== 知识库管理 ==========
    
    async def list_bases(self, db: Session, user_id: str) -> List[KnowledgeBaseModel]:
        """获取用户的知识库列表"""
        return db.query(KnowledgeBaseModel).filter(
            KnowledgeBaseModel.user_id == user_id
        ).order_by(KnowledgeBaseModel.created_at.desc()).all()
    
    async def create_base(
        self, 
        db: Session, 
        user_id: str, 
        data: KnowledgeBaseCreate
    ) -> KnowledgeBaseModel:
        """创建知识库"""
        
        # 检查 key 冲突
        if data.key:
            existing = db.query(KnowledgeBaseModel).filter(
                and_(
                    KnowledgeBaseModel.user_id == user_id,
                    KnowledgeBaseModel.key == data.key
                )
            ).first()
            
            if existing:
                raise HTTPException(status_code=400, detail="知识库标识符已存在")
        
        base = KnowledgeBaseModel(
            name=data.name,
            key=data.key,
            description=data.description,
            user_id=user_id
        )
        
        db.add(base)
        db.commit()
        db.refresh(base)
        
        logger.info(f"用户 {user_id} 创建知识库: {base.name} (ID: {base.id})")
        return base
    
    async def update_base(
        self, 
        db: Session, 
        user_id: str, 
        base_id: int, 
        data: KnowledgeBaseUpdate
    ) -> KnowledgeBaseModel:
        """更新知识库"""
        
        base = db.query(KnowledgeBaseModel).filter(
            and_(
                KnowledgeBaseModel.id == base_id,
                KnowledgeBaseModel.user_id == user_id
            )
        ).first()
        
        if not base:
            raise HTTPException(status_code=404, detail="知识库不存在")
        
        # 检查 key 冲突
        if data.key and data.key != base.key:
            existing = db.query(KnowledgeBaseModel).filter(
                and_(
                    KnowledgeBaseModel.user_id == user_id,
                    KnowledgeBaseModel.key == data.key,
                    KnowledgeBaseModel.id != base_id
                )
            ).first()
            
            if existing:
                raise HTTPException(status_code=400, detail="知识库标识符已存在")
        
        # 更新字段
        update_data = data.dict(exclude_unset=True)
        for key, value in update_data.items():
            setattr(base, key, value)
        
        base.updated_at = datetime.utcnow()
        db.commit()
        db.refresh(base)
        
        logger.info(f"更新知识库: {base.id}")
        return base
    
    async def delete_base(self, db: Session, user_id: str, base_id: int):
        """删除知识库（级联删除所有知识项和文档）"""
        
        base = db.query(KnowledgeBaseModel).filter(
            and_(
                KnowledgeBaseModel.id == base_id,
                KnowledgeBaseModel.user_id == user_id
            )
        ).first()
        
        if not base:
            raise HTTPException(status_code=404, detail="知识库不存在")
        
        # 获取所有知识项
        items = db.query(KnowledgeItemModel).filter(
            KnowledgeItemModel.base_id == base_id
        ).all()
        
        # 删除关联的文档和文件
        for item in items:
            # 删除文档和向量（如果已索引）
            if item.doc_id:
                try:
                    document_service.delete_document(db, item.doc_id)
                    logger.info(f"已删除文档: {item.doc_id}")
                except Exception as e:
                    logger.error(f"删除文档失败: {e}")
            
            # 删除物理文件
            try:
                if os.path.exists(item.url):
                    os.remove(item.url)
                    logger.info(f"已删除文件: {item.url}")
            except Exception as e:
                logger.error(f"删除文件失败: {e}")
        
        # 删除知识库（会级联删除 knowledge_items）
        db.delete(base)
        db.commit()
        
        logger.info(f"删除知识库: {base_id}, 共删除 {len(items)} 个知识项")
    
    # ========== 知识项管理 ==========
    
    async def upload_file(
        self,
        db: Session,
        user_id: str,
        file: UploadFile,
        tags: List[str],
        base_id: Optional[int] = None
    ) -> KnowledgeItemModel:
        """上传文件到知识库"""
        
        # 验证知识库
        if base_id:
            base = db.query(KnowledgeBaseModel).filter(
                and_(
                    KnowledgeBaseModel.id == base_id,
                    KnowledgeBaseModel.user_id == user_id
                )
            ).first()
            
            if not base:
                raise HTTPException(status_code=404, detail="知识库不存在")
        
        # 生成文件路径
        file_ext = os.path.splitext(file.filename)[1]
        unique_filename = f"{uuid.uuid4().hex}{file_ext}"
        file_path = os.path.join(self.upload_dir, unique_filename)
        
        # 保存文件
        try:
            content = await file.read()
            file_size = len(content)
            
            with open(file_path, "wb") as f:
                f.write(content)
            
            logger.info(f"文件保存成功: {file.filename}, 大小: {file_size} 字节")
            
        except Exception as e:
            logger.error(f"文件保存失败: {e}")
            raise HTTPException(status_code=500, detail="文件保存失败")
        
        # 创建知识项记录
        item = KnowledgeItemModel(
            original_name=file.filename,
            url=file_path,
            mime_type=file.content_type or "application/octet-stream",
            size=file_size,
            tags=tags,
            base_id=base_id,
            user_id=user_id,
            status="processing"
        )
        
        db.add(item)
        db.commit()
        db.refresh(item)
        
        # ⭐ 处理空文件
        if file_size == 0:
            item.status = "completed"
            item.error_msg = "文件为空，已保存但未索引"
            item.chunk_count = 0
            
            if base_id:
                self._update_base_stats(db, base_id, size_delta=0, count_delta=1)
            
            db.commit()
            db.refresh(item)
            
            logger.info(f"空文件已保存: {item.id}")
            return item
        
        # ⭐ 检查文件格式是否支持
        file_ext_lower = file_ext.lower()
        supported_types = ['.txt', '.md', '.docx', '.pdf']
        
        if file_ext_lower not in supported_types:
            item.status = "completed"
            item.error_msg = f"文件格式 {file_ext} 暂不支持自动解析（文件已保存）"
            item.chunk_count = 0
            
            if base_id:
                self._update_base_stats(db, base_id, size_delta=file_size, count_delta=1)
            
            db.commit()
            db.refresh(item)
            
            logger.warning(f"不支持的文件格式: {file_ext}")
            return item
        
        # 处理文档并索引
        try:
            doc_data = DocumentCreate(
                owner_id=user_id,
                title=file.filename,
                doc_type="knowledge_item",
                tags=tags,
                weight=1.0
            )
            
            document = await self._create_document_with_metadata(
                db, doc_data, file_path, base_id, item.id
            )
            
            # 更新知识项
            item.doc_id = document.doc_id
            item.status = "completed"
            item.error_msg = None
            
            # 计算 chunk 数量
            try:
                from app.services.vector_service import vector_service
                collection_name = "private_documents"
                results = vector_service.get_collection(collection_name).query(
                    expr=f'doc_id == "{document.doc_id}"',
                    output_fields=["chunk_index"]
                )
                item.chunk_count = len(results)
            except Exception as e:
                logger.error(f"查询 chunk 数量失败: {e}")
                item.chunk_count = 0
            
            # 更新知识库统计
            if base_id:
                self._update_base_stats(db, base_id, size_delta=file_size, count_delta=1)
            
            logger.info(f"文档处理完成: {item.id}, chunks: {item.chunk_count}")
            
        except Exception as e:
            logger.error(f"文档处理失败: {e}", exc_info=True)
            item.status = "failed"
            item.error_msg = str(e)[:500]
            
            # 即使处理失败，文件已保存，也要计数
            if base_id:
                self._update_base_stats(db, base_id, size_delta=file_size, count_delta=1)
        
        db.commit()
        db.refresh(item)
        
        logger.info(f"用户 {user_id} 上传文件: {file.filename} (ID: {item.id})")
        return item
    
    async def _create_document_with_metadata(
        self,
        db: Session,
        doc_data: DocumentCreate,
        file_path: str,
        base_id: Optional[int],
        item_id: int
    ):
        """创建文档并在向量中添加 base_id/item_id 元数据"""
        
        doc_id = f"doc_{uuid.uuid4().hex[:16]}"
        
        from app.models.database import Document
        db_doc = Document(
            doc_id=doc_id,
            owner_id=doc_data.owner_id,
            title=doc_data.title,
            doc_type=doc_data.doc_type,
            filename=file_path.split("/")[-1],
            file_path=file_path,
            tags=doc_data.tags,
            weight=doc_data.weight
        )
        db.add(db_doc)
        db.commit()
        db.refresh(db_doc)
        
        await self._ingest_with_metadata(db_doc, doc_data, base_id, item_id)
        
        return db_doc
    
    async def _ingest_with_metadata(
        self, 
        document, 
        doc_data: DocumentCreate,
        base_id: Optional[int],
        item_id: int
    ):
        """扩展版的文档索引（添加 base_id/item_id）"""
        
        from app.services.vector_service import vector_service
        from app.services.embedding_service import embedding_service
        from app.utils.text_processor import TextProcessor
        import time
        
        text_processor = TextProcessor()
        
        # 解析文档
        if doc_data.chunks:
            chunks = doc_data.chunks
        elif doc_data.content:
            chunks = text_processor.split_text(doc_data.content)
        else:
            content = text_processor.extract_text(document.file_path)
            chunks = text_processor.split_text(content)
        
        # 向量化
        texts = [chunk.get("chunk_content", chunk.get("text", "")) for chunk in chunks]
        embeddings = await embedding_service.embed_texts(texts)
        
        # 构建向量数据
        timestamp = int(time.time())
        vector_data = []
        
        for i, (chunk, embedding) in enumerate(zip(chunks, embeddings)):
            chunk_id = f"{document.doc_id}#{i}"
            
            item = {
                "id": chunk_id,
                "doc_id": document.doc_id,
                "base_id": base_id or 0,
                "item_id": item_id,
                "title": document.title,
                "doc_type": document.doc_type,
                "filename": document.filename,
                "tags": document.tags,
                "weight": document.weight,
                "valid": document.valid,
                "created_at": timestamp,
                "chunk_index": i,
                "chunk_content": chunk.get("chunk_content", chunk.get("text", "")),
                "embedding": embedding
            }
            
            if document.owner_id != "public":
                item["owner_id"] = document.owner_id
            
            vector_data.append(item)
        
        # 插入向量库
        collection_name = "public_documents" if document.owner_id == "public" else "private_documents"
        
        vector_service.create_collection_if_not_exists(
            collection_name,
            is_private=(document.owner_id != "public")
        )
        
        partition_name = None
        if document.owner_id != "public":
            partition_name = f"user_{document.owner_id}"
            vector_service.create_partition_if_not_exists(collection_name, partition_name)
        
        vector_service.insert_documents(collection_name, vector_data, partition_name)
        
        logger.info(f"文档 {document.doc_id} 索引完成（base_id={base_id}, item_id={item_id}）")
    
    async def list_items(
        self,
        db: Session,
        user_id: str,
        tag: Optional[str] = None,
        base_id: Optional[int] = None
    ) -> List[KnowledgeItemModel]:
        """获取知识项列表"""
        
        query = db.query(KnowledgeItemModel).filter(
            KnowledgeItemModel.user_id == user_id
        )
        
        if base_id is not None:
            query = query.filter(KnowledgeItemModel.base_id == base_id)
        
        if tag:
            from sqlalchemy import any_
            query = query.filter(tag == any_(KnowledgeItemModel.tags))
        
        return query.order_by(KnowledgeItemModel.created_at.desc()).all()
    
    async def remove_item(self, db: Session, user_id: str, item_id: int):
        """删除知识项"""
        
        item = db.query(KnowledgeItemModel).filter(
            and_(
                KnowledgeItemModel.id == item_id,
                KnowledgeItemModel.user_id == user_id
            )
        ).first()
        
        if not item:
            raise HTTPException(status_code=404, detail="知识项不存在")
        
        # 删除文档和向量（如果已索引）
        if item.doc_id:
            try:
                document_service.delete_document(db, item.doc_id)
                logger.info(f"已删除文档和向量: {item.doc_id}")
            except Exception as e:
                logger.error(f"删除文档失败: {e}")
        else:
            logger.info(f"知识项 {item_id} 未索引，跳过文档删除")
        
        # 删除物理文件
        try:
            if os.path.exists(item.url):
                os.remove(item.url)
                logger.info(f"已删除物理文件: {item.url}")
            else:
                logger.warning(f"物理文件不存在: {item.url}")
        except Exception as e:
            logger.error(f"删除物理文件失败: {e}")
        
        # 更新知识库统计
        if item.base_id:
            self._update_base_stats(db, item.base_id, size_delta=-item.size, count_delta=-1)
            logger.info(f"已更新知识库 {item.base_id} 统计")
        
        db.delete(item)
        db.commit()
        
        logger.info(f"知识项 {item_id} 删除完成")
    
    async def move_item(
        self,
        db: Session,
        user_id: str,
        item_id: int,
        target_base_id: int
    ):
        """移动知识项"""
        
        item = db.query(KnowledgeItemModel).filter(
            and_(
                KnowledgeItemModel.id == item_id,
                KnowledgeItemModel.user_id == user_id
            )
        ).first()
        
        if not item:
            raise HTTPException(status_code=404, detail="知识项不存在")
        
        # 验证目标知识库
        target_base = db.query(KnowledgeBaseModel).filter(
            and_(
                KnowledgeBaseModel.id == target_base_id,
                KnowledgeBaseModel.user_id == user_id
            )
        ).first()
        
        if not target_base:
            raise HTTPException(status_code=404, detail="目标知识库不存在")
        
        # 更新统计
        old_base_id = item.base_id
        if old_base_id:
            self._update_base_stats(db, old_base_id, size_delta=-item.size, count_delta=-1)
        
        item.base_id = target_base_id
        item.updated_at = datetime.utcnow()
        
        self._update_base_stats(db, target_base_id, size_delta=item.size, count_delta=1)
        
        # 更新 Milvus 中的 base_id（逻辑更新）
        if item.doc_id:
            self._update_vector_base_id(item.doc_id, target_base_id, user_id)
        
        db.commit()
        
        logger.info(f"移动知识项 {item_id} 到知识库 {target_base_id}")
    
    async def move_batch(
        self,
        db: Session,
        user_id: str,
        item_ids: List[int],
        target_base_id: int
    ) -> int:
        """批量移动知识项"""
        
        # 验证目标知识库
        target_base = db.query(KnowledgeBaseModel).filter(
            and_(
                KnowledgeBaseModel.id == target_base_id,
                KnowledgeBaseModel.user_id == user_id
            )
        ).first()
        
        if not target_base:
            raise HTTPException(status_code=404, detail="目标知识库不存在")
        
        items = db.query(KnowledgeItemModel).filter(
            and_(
                KnowledgeItemModel.id.in_(item_ids),
                KnowledgeItemModel.user_id == user_id
            )
        ).all()
        
        moved_count = 0
        for item in items:
            old_base_id = item.base_id
            
            # 更新统计
            if old_base_id:
                self._update_base_stats(db, old_base_id, size_delta=-item.size, count_delta=-1)
            
            item.base_id = target_base_id
            item.updated_at = datetime.utcnow()
            
            self._update_base_stats(db, target_base_id, size_delta=item.size, count_delta=1)
            
            # 更新向量
            if item.doc_id:
                self._update_vector_base_id(item.doc_id, target_base_id, user_id)
            
            moved_count += 1
        
        db.commit()
        
        logger.info(f"批量移动 {moved_count} 个知识项到知识库 {target_base_id}")
        return moved_count
    
    # ========== 辅助方法 ==========
    
    def _update_base_stats(self, db: Session, base_id: int, size_delta: int, count_delta: int):
        """更新知识库统计"""
        base = db.query(KnowledgeBaseModel).filter(
            KnowledgeBaseModel.id == base_id
        ).first()
        
        if base:
            base.total_size = max(0, base.total_size + size_delta)
            base.item_count = max(0, base.item_count + count_delta)
            base.updated_at = datetime.utcnow()
            db.commit()
    
    def _update_vector_base_id(self, doc_id: str, new_base_id: int, user_id: str):
        """更新 Milvus 中的 base_id（逻辑更新）"""
        try:
            # Milvus 不支持直接 UPDATE，这里只记录日志
            # 检索时通过 DB 的 item_id 来过滤
            logger.info(f"文档 {doc_id} 的 base_id 已逻辑更新为 {new_base_id}")
            
        except Exception as e:
            logger.error(f"更新向量 base_id 失败: {e}")

knowledge_service = KnowledgeService()