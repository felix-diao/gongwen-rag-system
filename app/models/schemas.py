from pydantic import BaseModel, Field
from typing import Optional, List
from datetime import datetime

class DocumentCreate(BaseModel):
    """创建文档请求"""
    owner_id: str
    title: str
    doc_type: str = Field(..., description="公文类型：通知/报告/请示/批复/函/会议纪要等")
    tags: List[str] = Field(default=[], description="标签：部门/主题/紧急程度等")
    weight: float = 1.0
    content: Optional[str] = None
    chunks: Optional[List[dict]] = None

class DocumentResponse(BaseModel):
    """文档响应"""
    doc_id: str
    owner_id: str
    title: str
    doc_type: str
    filename: str
    tags: List[str]
    weight: float
    valid: bool
    created_at: datetime
    chunks_count: Optional[int] = None

class DocumentUpdate(BaseModel):
    """更新文档"""
    title: Optional[str] = None
    tags: Optional[List[str]] = None
    weight: Optional[float] = None
    valid: Optional[bool] = None

class EmbedRequest(BaseModel):
    """向量化请求"""
    model: str = "gongwen-embed-v1"
    inputs: List[dict] = Field(..., description="[{id, text}, ...]")

class EmbedResponse(BaseModel):
    """向量化响应"""
    embeddings: List[dict]

class RetrieveRequest(BaseModel):
    """检索请求"""
    user_id: str
    query: str
    top_k: int = 6
    collection: str = "public_documents"
    partition: Optional[str] = None
    filters: Optional[dict] = None
    score_threshold: float = 0.2
    rerank: bool = False

class RAGRequest(BaseModel):
    """RAG 请求"""
    user_id: str
    query: str
    top_k: int = 6
    rerank: bool = True
    rerank_model: str = "cross-encoder-v0"
    generator: str = "gongwen-llm-v1"
    context_token_limit: int = 3000
    include_conversations: bool = True

class ConversationCreate(BaseModel):
    """创建会话"""
    user_id: str
    query: str
    answer: str
    weight: float = 0.8
    liked: bool = False

class ConversationFeedback(BaseModel):
    """会话反馈"""
    liked: Optional[bool] = None
    weight_delta: Optional[float] = None

class UserLogin(BaseModel):
    """用户登录"""
    username: str
    password: str

class UserRegister(BaseModel):
    """用户注册"""
    username: str
    password: str
    department: Optional[str] = None

    
class Token(BaseModel):
    """Token 响应"""
    access_token: str
    token_type: str = "bearer"