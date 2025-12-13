from sqlalchemy.orm import Session
from typing import List, Optional
import uuid
import time
from app.models.database import Document
from app.models.schemas import DocumentCreate, DocumentUpdate
from app.services.vector_service import vector_service
from app.services.embedding_service import embedding_service
from app.utils.text_processor import TextProcessor
from app.utils.logger import get_logger  # ← 修改导入


class DocumentService:
    """文档管理服务"""
    
    def __init__(self):
        self.logger = get_logger("document_service")  # ← 创建专属 logger
        self.text_processor = TextProcessor()
        self.logger.info("文档服务初始化完成")
    
    async def create_document(
        self,
        db: Session,
        doc_data: DocumentCreate,
        file_path: str
    ) -> Document:
        """创建文档并索引"""
        doc_id = f"doc_{uuid.uuid4().hex[:16]}"
        
        self.logger.info(
            f"创建文档: {doc_data.title}, owner={doc_data.owner_id}, "
            f"type={doc_data.doc_type}"
        )
        
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
        
        try:
            await self._ingest_document(db_doc, doc_data)
            self.logger.info(f"文档创建并索引完成: {doc_id}")
        except Exception as e:
            self.logger.error(f"文档索引失败: {doc_id} - {e}", exc_info=True)
        
        return db_doc
    
    async def _ingest_document(self, document: Document, doc_data: DocumentCreate):
        """文档分块、向量化和入库"""
        self.logger.info(f"开始处理文档: {document.doc_id} - {document.title}")
        
        # 解析文档
        if doc_data.chunks:
            chunks = doc_data.chunks
            self.logger.info(f"使用提供的分块: {len(chunks)} 个")
        elif doc_data.content:
            chunks = self.text_processor.split_text(doc_data.content)
            self.logger.info(f"从内容分块: {len(chunks)} 个")
        else:
            content = self.text_processor.extract_text(document.file_path)
            chunks = self.text_processor.split_text(content)
            self.logger.info(f"从文件提取并分块: {len(chunks)} 个")
        
        # 向量化
        texts = [chunk.get("chunk_content", chunk.get("text", "")) for chunk in chunks]
        
        self.logger.info(f"开始向量化 {len(texts)} 段文本...")
        embeddings = await embedding_service.embed_texts(texts)
        self.logger.info(f"向量化完成: {len(embeddings)} 个向量")
        
        # 构建向量数据
        timestamp = int(time.time())
        vector_data = []
        
        for i, (chunk, embedding) in enumerate(zip(chunks, embeddings)):
            chunk_id = f"{document.doc_id}#{i}"
            
            item = {
                "id": chunk_id,
                "doc_id": document.doc_id,
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
        
        self.logger.info(
            f"文档 {document.doc_id} 索引完成: {len(chunks)} 个分块 "
            f"-> 集合 {collection_name}, 分区 {partition_name or 'default'}"
        )
    
    def get_document(self, db: Session, doc_id: str) -> Optional[Document]:
        """获取文档"""
        doc = db.query(Document).filter(Document.doc_id == doc_id).first()
        if doc:
            self.logger.debug(f"查询到文档: {doc_id}")
        else:
            self.logger.warning(f"文档不存在: {doc_id}")
        return doc
    
    def list_documents(
        self,
        db: Session,
        owner_id: Optional[str] = None,
        doc_type: Optional[str] = None,
        tags: Optional[List[str]] = None,
        limit: int = 20,
        offset: int = 0
    ) -> List[Document]:
        """列出文档"""
        query = db.query(Document).filter(Document.valid == True)
        
        if owner_id:
            query = query.filter(Document.owner_id == owner_id)
        if doc_type:
            query = query.filter(Document.doc_type == doc_type)
        if tags:
            for tag in tags:
                query = query.filter(Document.tags.contains([tag]))
        
        docs = query.offset(offset).limit(limit).all()
        
        self.logger.info(
            f"查询文档列表: owner={owner_id}, type={doc_type}, "
            f"tags={tags}, 结果数={len(docs)}"
        )
        
        return docs
    
    def update_document(
        self,
        db: Session,
        doc_id: str,
        updates: DocumentUpdate
    ) -> Optional[Document]:
        """更新文档"""
        doc = self.get_document(db, doc_id)
        if not doc:
            return None
        
        self.logger.info(f"更新文档: {doc_id}, 更新字段: {list(updates.dict(exclude_unset=True).keys())}")
        
        update_data = updates.dict(exclude_unset=True)
        for key, value in update_data.items():
            setattr(doc, key, value)
        
        db.commit()
        db.refresh(doc)
        
        # 如果设置为无效，从向量库删除
        if "valid" in update_data:
            collection_name = "public_documents" if doc.owner_id == "public" else "private_documents"
            if not update_data["valid"]:
                partition_name = None if doc.owner_id == "public" else f"user_{doc.owner_id}"
                vector_service.delete_by_doc_id(collection_name, doc_id, partition_name)
                self.logger.info(f"文档 {doc_id} 已从向量库删除")
        
        self.logger.info(f"文档 {doc_id} 更新完成")
        return doc
    
    def delete_document(self, db: Session, doc_id: str) -> bool:
        """删除文档"""
        doc = self.get_document(db, doc_id)
        if not doc:
            return False
        
        self.logger.info(f"删除文档: {doc_id} - {doc.title}")
        
        # 从向量库删除
        try:
            collection_name = "public_documents" if doc.owner_id == "public" else "private_documents"
            partition_name = None if doc.owner_id == "public" else f"user_{doc.owner_id}"
            vector_service.delete_by_doc_id(collection_name, doc_id, partition_name)
            self.logger.info(f"文档 {doc_id} 已从向量库删除")
        except Exception as e:
            self.logger.error(f"从向量库删除文档失败 {doc_id}: {e}", exc_info=True)
        
        # 数据库软删除
        doc.valid = False
        db.commit()
        
        self.logger.info(f"文档 {doc_id} 删除完成（软删除）")
        return True


document_service = DocumentService()