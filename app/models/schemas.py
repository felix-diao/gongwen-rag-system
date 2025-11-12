from pydantic import BaseModel, Field, ConfigDict, field_validator
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

# ========== 知识库与知识项 Schema ==========

class KnowledgeBaseBase(BaseModel):
    """知识库基础字段"""
    name: str = Field(..., max_length=100)
    key: Optional[str] = Field(default=None, max_length=50)
    description: Optional[str] = None


class KnowledgeBaseCreate(KnowledgeBaseBase):
    """创建知识库"""
    pass


class KnowledgeBaseUpdate(BaseModel):
    """更新知识库"""
    name: Optional[str] = Field(default=None, max_length=100)
    key: Optional[str] = Field(default=None, max_length=50)
    description: Optional[str] = None


class KnowledgeBaseResponse(BaseModel):
    """知识库响应"""
    model_config = ConfigDict(from_attributes=True)

    id: int
    name: str
    key: Optional[str] = None
    description: Optional[str] = None
    item_count: Optional[int] = 0
    total_size: Optional[int] = 0
    created_at: datetime
    updated_at: Optional[datetime] = None


class KnowledgeItemResponse(BaseModel):
    """知识项响应"""
    model_config = ConfigDict(from_attributes=True)

    id: int
    original_name: str
    url: str
    mime_type: str
    size: int
    tags: List[str] = Field(default_factory=list)
    base_id: Optional[int] = None
    user_id: Optional[str] = None
    status: Optional[str] = None
    error_msg: Optional[str] = None
    doc_id: Optional[str] = None
    chunk_count: Optional[int] = None
    created_at: datetime
    updated_at: Optional[datetime] = None

    @field_validator("tags", mode="before")
    @classmethod
    def ensure_tags(cls, value):
        if value is None:
            return []
        if isinstance(value, str):
            return [value]
        return value


class KnowledgeItemMove(BaseModel):
    """知识项移动"""
    target_base_id: int = Field(..., ge=1, description="目标知识库 ID")


class KnowledgeItemBatchMove(BaseModel):
    """知识项批量移动"""
    item_ids: List[int] = Field(..., min_length=1, description="知识项 ID 列表")
    target_base_id: int = Field(..., ge=1, description="目标知识库 ID")

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


from typing import Optional, Literal,Generic,TypeVar
from datetime import datetime

T = TypeVar("T")

class DocumentWriteRequest(BaseModel):
    """AI 公文写作接口请求体"""
    prompt: str
    documentType: str  # 'article' | 'report' | 'summary' | 'email'
    tone: str | None = None  # 'professional' | 'casual' | 'formal'
    language: str | None = None  # e.g. 'zh', 'en'
    title: str | None = None  # 文章标题
    requirement: str | None = None  # 提出的需求


class DocumentOptimizeRequest(BaseModel):
    content: str
    optimizationType: Literal['grammar', 'style', 'clarity', 'logic', 'format', 'tone', 'all'] = 'all'
    customInstruction: Optional[str] = None
    context: Optional[dict] = None


class BaseData(BaseModel):
    """所有 data 模型的基类"""
    pass

class StandardResponse(BaseModel, Generic[T]):
    success: bool
    data: Optional[T]
    message: str


# 具体接口的 data 结构
class DocumentData(BaseData):
    content: str
    wordCount: int
    generatedAt: datetime

# 文档优化接口的数据结构（只保留 content）
class DocumentDataOptimize(BaseData):
    content: str