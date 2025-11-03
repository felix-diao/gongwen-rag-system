from fastapi import APIRouter, Depends, HTTPException, UploadFile, File, Form
from sqlalchemy.orm import Session
from typing import List, Optional
import os
import shutil
from app.models.database import get_db
from app.models.schemas import DocumentCreate, DocumentResponse, DocumentUpdate
from app.services.document_service import document_service
from app.utils.auth import get_current_user
from app.config import settings

router = APIRouter(prefix="/api/documents", tags=["文档管理"])

@router.post("/upload", response_model=DocumentResponse)
async def upload_document(
    file: UploadFile = File(...),
    title: str = Form(...),
    doc_type: str = Form(...),
    tags: str = Form(""),
    weight: float = Form(1.0),
    is_public: bool = Form(False),
    db: Session = Depends(get_db),
    current_user: dict = Depends(get_current_user)
):
    """上传并索引文档"""
    
    file.file.seek(0, 2)
    file_size = file.file.tell()
    file.file.seek(0)
    
    if file_size > settings.MAX_UPLOAD_SIZE:
        raise HTTPException(status_code=400, detail="文件过大")
    
    os.makedirs(settings.UPLOAD_DIR, exist_ok=True)
    file_path = os.path.join(settings.UPLOAD_DIR, file.filename)
    
    with open(file_path, "wb") as buffer:
        shutil.copyfileobj(file.file, buffer)
    
    owner_id = "public" if is_public else current_user["user_id"]
    tags_list = [tag.strip() for tag in tags.split(",") if tag.strip()]
    
    doc_data = DocumentCreate(
        owner_id=owner_id,
        title=title,
        doc_type=doc_type,
        tags=tags_list,
        weight=weight
    )
    
    document = await document_service.create_document(db, doc_data, file_path)
    
    return DocumentResponse(
        doc_id=document.doc_id,
        owner_id=document.owner_id,
        title=document.title,
        doc_type=document.doc_type,
        filename=document.filename,
        tags=document.tags,
        weight=document.weight,
        valid=document.valid,
        created_at=document.created_at
    )

@router.get("/", response_model=List[DocumentResponse])
def list_documents(
    doc_type: Optional[str] = None,
    tags: Optional[str] = None,
    limit: int = 20,
    offset: int = 0,
    db: Session = Depends(get_db),
    current_user: dict = Depends(get_current_user)
):
    """列出文档"""
    tags_list = [tag.strip() for tag in tags.split(",") if tag.strip()] if tags else None
    
    documents = document_service.list_documents(
        db=db,
        owner_id=current_user["user_id"],
        doc_type=doc_type,
        tags=tags_list,
        limit=limit,
        offset=offset
    )
    
    public_docs = document_service.list_documents(
        db=db,
        owner_id="public",
        doc_type=doc_type,
        tags=tags_list,
        limit=limit,
        offset=offset
    )
    
    all_docs = documents + public_docs
    
    return [
        DocumentResponse(
            doc_id=doc.doc_id,
            owner_id=doc.owner_id,
            title=doc.title,
            doc_type=doc.doc_type,
            filename=doc.filename,
            tags=doc.tags,
            weight=doc.weight,
            valid=doc.valid,
            created_at=doc.created_at
        )
        for doc in all_docs
    ]

@router.get("/{doc_id}", response_model=DocumentResponse)
def get_document(
    doc_id: str,
    db: Session = Depends(get_db),
    current_user: dict = Depends(get_current_user)
):
    """获取文档详情"""
    document = document_service.get_document(db, doc_id)
    
    if not document:
        raise HTTPException(status_code=404, detail="文档不存在")
    
    if document.owner_id != "public" and document.owner_id != current_user["user_id"]:
        raise HTTPException(status_code=403, detail="无权访问")
    
    return DocumentResponse(
        doc_id=document.doc_id,
        owner_id=document.owner_id,
        title=document.title,
        doc_type=document.doc_type,
        filename=document.filename,
        tags=document.tags,
        weight=document.weight,
        valid=document.valid,
        created_at=document.created_at
    )

@router.patch("/{doc_id}", response_model=DocumentResponse)
def update_document(
    doc_id: str,
    updates: DocumentUpdate,
    db: Session = Depends(get_db),
    current_user: dict = Depends(get_current_user)
):
    """更新文档"""
    document = document_service.get_document(db, doc_id)
    
    if not document:
        raise HTTPException(status_code=404, detail="文档不存在")
    
    if document.owner_id != current_user["user_id"] and current_user["role"] != "admin":
        raise HTTPException(status_code=403, detail="无权操作")
    
    updated_doc = document_service.update_document(db, doc_id, updates)
    
    return DocumentResponse(
        doc_id=updated_doc.doc_id,
        owner_id=updated_doc.owner_id,
        title=updated_doc.title,
        doc_type=updated_doc.doc_type,
        filename=updated_doc.filename,
        tags=updated_doc.tags,
        weight=updated_doc.weight,
        valid=updated_doc.valid,
        created_at=updated_doc.created_at
    )

@router.delete("/{doc_id}")
def delete_document(
    doc_id: str,
    db: Session = Depends(get_db),
    current_user: dict = Depends(get_current_user)
):
    """删除文档"""
    document = document_service.get_document(db, doc_id)
    
    if not document:
        raise HTTPException(status_code=404, detail="文档不存在")
    
    if document.owner_id != current_user["user_id"] and current_user["role"] != "admin":
        raise HTTPException(status_code=403, detail="无权操作")
    
    success = document_service.delete_document(db, doc_id)
    
    if not success:
        raise HTTPException(status_code=500, detail="删除失败")
    
    return {"message": "删除成功"}