from pydantic import BaseModel, Field, ConfigDict, field_validator
from typing import Optional, List, Literal, Generic, TypeVar
from datetime import datetime

# ========== 文档相关 Schema ==========

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

# ========== 向量化和检索 Schema ==========

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

# ========== 会话相关 Schema ==========

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

# ========== 用户认证相关 Schema ==========

class UserLogin(BaseModel):
    """用户登录"""
    username: str = Field(..., min_length=1, description="用户名")
    password: str = Field(..., min_length=1, description="密码")


class UserRegister(BaseModel):
    """用户注册"""
    username: str = Field(..., min_length=3, max_length=50, description="用户名，3-50个字符")
    password: str = Field(..., min_length=8, max_length=72, description="密码，至少8位")
    department: Optional[str] = Field(None, max_length=128, description="部门")

    @field_validator('username')
    @classmethod
    def validate_username(cls, v):
        """验证用户名"""
        v = v.strip()
        if not v:
            raise ValueError('用户名不能为空或只包含空格')
        
        # 可选：限制用户名格式（只允许字母、数字、下划线、中文）
        # import re
        # if not re.match(r'^[\w\u4e00-\u9fa5]+$', v):
        #     raise ValueError('用户名只能包含字母、数字、下划线和中文')
        
        # 不允许纯数字用户名
        if v.isdigit():
            raise ValueError('用户名不能为纯数字')
        
        return v
    
    @field_validator('password')
    @classmethod
    def validate_password_strength(cls, v):
        """
        验证密码强度
        要求：
        - 长度至少8位
        - 必须包含大写字母
        - 必须包含小写字母
        - 必须包含数字
        """
        if len(v) < 8:
            raise ValueError('密码长度至少8位')
        
        if not any(char.isdigit() for char in v):
            raise ValueError('密码必须包含至少一个数字')
        
        if not any(char.isalpha() for char in v):
            raise ValueError('密码必须包含至少一个字母')
        
        if not any(char.isupper() for char in v):
            raise ValueError('密码必须包含至少一个大写字母')
        
        if not any(char.islower() for char in v):
            raise ValueError('密码必须包含至少一个小写字母')
        
        # 可选：检查常见弱密码
        weak_passwords = [
            '12345678', 'Password1', 'Qwerty123', 'Abc12345',
            'Test1234', 'Admin123', 'User1234'
        ]
        if v in weak_passwords:
            raise ValueError('密码过于简单，请使用更复杂的密码')
        
        return v
    
    @field_validator('department')
    @classmethod
    def validate_department(cls, v):
        """验证部门"""
        if v is not None:
            v = v.strip()
            if not v:
                return None
        return v


class PasswordChange(BaseModel):
    """修改密码请求模型"""
    old_password: str = Field(..., min_length=1, description="旧密码")
    new_password: str = Field(..., min_length=8, max_length=72, description="新密码")
    confirm_password: str = Field(..., min_length=8, max_length=72, description="确认新密码")
    
    @field_validator('new_password')
    @classmethod
    def validate_password_strength(cls, v):
        """
        验证密码强度
        要求：
        - 长度至少8位
        - 必须包含大写字母
        - 必须包含小写字母
        - 必须包含数字
        """
        if len(v) < 8:
            raise ValueError('密码长度至少8位')
        
        if not any(char.isdigit() for char in v):
            raise ValueError('密码必须包含至少一个数字')
        
        if not any(char.isalpha() for char in v):
            raise ValueError('密码必须包含至少一个字母')
        
        if not any(char.isupper() for char in v):
            raise ValueError('密码必须包含至少一个大写字母')
        
        if not any(char.islower() for char in v):
            raise ValueError('密码必须包含至少一个小写字母')
        
        # 可选：检查常见弱密码
        weak_passwords = [
            '12345678', 'Password1', 'Qwerty123', 'Abc12345',
            'Test1234', 'Admin123', 'User1234'
        ]
        if v in weak_passwords:
            raise ValueError('密码过于简单，请使用更复杂的密码')
        
        return v
    
    @field_validator('confirm_password')
    @classmethod
    def passwords_match(cls, v, info):
        """验证两次密码是否一致"""
        if 'new_password' in info.data and v != info.data['new_password']:
            raise ValueError('两次输入的密码不一致')
        return v


class Token(BaseModel):
    """Token 响应"""
    access_token: str
    token_type: str = "bearer"


# ========== 通用响应 Schema ==========

T = TypeVar("T")

class BaseData(BaseModel):
    """所有 data 模型的基类"""
    pass


class StandardResponse(BaseModel, Generic[T]):
    """标准响应格式"""
    success: bool
    data: Optional[T]
    message: str


class PasswordChangeResponse(BaseData):
    """修改密码响应"""
    message: str
    user_id: str
    username: str
    changed_at: datetime


class LogoutResponse(BaseData):
    """退出登录响应"""
    message: str
    user_id: str
    username: str
    logout_at: datetime


class RegisterResponse(BaseData):
    """注册响应"""
    user_id: str
    username: str
    department: Optional[str] = None


# ========== AI 文档处理 Schema ==========

class DocumentWriteRequest(BaseModel):
    """AI 公文写作接口请求体"""
    prompt: str = Field(..., description="写作提示")
    documentType: str = Field(..., description="文档类型：article/report/summary/email")
    tone: Optional[str] = Field(None, description="语气：professional/casual/formal")
    language: Optional[str] = Field(None, description="语言：zh/en")
    title: Optional[str] = Field(None, description="文章标题")
    requirement: Optional[str] = Field(None, description="具体需求")


class DocumentOptimizeRequest(BaseModel):
    """文档优化请求"""
    content: str = Field(..., description="待优化的内容")
    optimizationType: Literal['grammar', 'style', 'clarity', 'logic', 'format', 'tone', 'all'] = Field(
        default='all', 
        description="优化类型"
    )
    customInstruction: Optional[str] = Field(None, description="自定义优化指令")
    context: Optional[dict] = Field(None, description="上下文信息")


class DocumentExportRequest(BaseModel):
    """文档导出请求"""
    content: str = Field(..., description="文档内容")
    title: str = Field(..., description="文档标题")
    format: Literal['pdf', 'docx', 'txt'] = Field(default='pdf', description="导出格式")
    options: Optional[dict] = Field(None, description="导出选项")


class DocumentData(BaseData):
    """文档生成响应数据"""
    content: str
    wordCount: int
    generatedAt: datetime
    docxPath: Optional[str] = None
    pdfPath: Optional[str] = None
    aiRate: Optional[float] = None


class DocumentDataOptimize(BaseData):
    """文档优化响应数据"""
    content: str
    docxPath: Optional[str] = None
    pdfPath: Optional[str] = None
    aiRate: Optional[float] = None


class DocumentExportData(BaseData):
    """文档导出响应数据"""
    url: str
    filename: str
    size: int
    expiresAt: datetime
    


# ========== AI-rate Schema ==========
class AIRateRequest(BaseModel):
    """请求体：传入公文/文本内容以评估 AI 生成概率"""
    content: str


class AIRateResponse(BaseData):
    """响应体：仅返回 AI 生成概率（0-100）"""
    ai_rate: float
