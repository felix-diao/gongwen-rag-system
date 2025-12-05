from pydantic import BaseModel, Field, ConfigDict, field_validator
from typing import Optional, List, Literal, Generic, TypeVar
from datetime import datetime
import re

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
    is_public: bool = Field(default=False, description="是否为公有知识库")


class KnowledgeBaseCreate(KnowledgeBaseBase):
    """创建知识库"""
    pass


class KnowledgeBaseUpdate(BaseModel):
    """更新知识库"""
    name: Optional[str] = Field(default=None, max_length=100)
    key: Optional[str] = Field(default=None, max_length=50)
    description: Optional[str] = None
    is_public: Optional[bool] = None


class KnowledgeBaseResponse(BaseModel):
    """知识库响应"""
    model_config = ConfigDict(from_attributes=True)

    id: int
    name: str
    key: Optional[str] = None
    description: Optional[str] = None
    is_public: bool  # ⭐ 新增
    user_id: str  # ⭐ 添加创建者ID
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
        
        if not re.search(r'[!@#$%^&*(),.?":{}|<>_\-+=\[\]\\\/~`]', v):
            raise ValueError('密码必须包含至少一个特殊符号(!@#$%^&*等)')

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
        
        if not re.search(r'[!@#$%^&*(),.?":{}|<>_\-+=\[\]\\\/~`]', v):
            raise ValueError('密码必须包含至少一个特殊符号(!@#$%^&*等)')

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


# ========== Prompt 模板相关 Schema ==========

class PromptTemplateBase(BaseModel):
    """Prompt 模板基础字段"""
    name: str = Field(..., min_length=1, max_length=100, description="模板名称")
    category: str = Field(..., description="分类")
    description: Optional[str] = Field(None, max_length=500, description="描述")
    content: str = Field(..., min_length=1, max_length=5000, description="Prompt内容")
    variables: List[str] = Field(default_factory=list, description="变量列表")
    
    @field_validator('category')
    @classmethod
    def validate_category(cls, v):
        """验证分类"""
        allowed_categories = ['notice', 'bulletin', 'request', 'report', 'letter', 'meeting']
        if v not in allowed_categories:
            raise ValueError(f'分类必须是以下之一: {", ".join(allowed_categories)}')
        return v
    
    @field_validator('variables', mode='before')
    @classmethod
    def ensure_variables(cls, v):
        """确保变量列表格式正确"""
        if v is None:
            return []
        if isinstance(v, str):
            # 如果是字符串，按逗号分割
            return [var.strip() for var in v.split(',') if var.strip()]
        # 去除空字符串和重复项
        return list(set(filter(None, [var.strip() for var in v])))


class PromptTemplateCreate(PromptTemplateBase):
    """创建 Prompt 模板"""
    is_active: bool = Field(default=True, description="是否启用")


class PromptTemplateUpdate(BaseModel):
    """更新 Prompt 模板"""
    name: Optional[str] = Field(None, min_length=1, max_length=100)
    category: Optional[str] = None
    description: Optional[str] = Field(None, max_length=500)
    content: Optional[str] = Field(None, min_length=1, max_length=5000)
    variables: Optional[List[str]] = None
    is_active: Optional[bool] = None
    
    @field_validator('category')
    @classmethod
    def validate_category(cls, v):
        if v is not None:
            allowed_categories = ['notice', 'bulletin', 'request', 'report', 'letter', 'meeting']
            if v not in allowed_categories:
                raise ValueError(f'分类必须是以下之一: {", ".join(allowed_categories)}')
        return v
    
    @field_validator('variables', mode='before')
    @classmethod
    def ensure_variables(cls, v):
        if v is None:
            return None
        if isinstance(v, str):
            return [var.strip() for var in v.split(',') if var.strip()]
        return list(set(filter(None, [var.strip() for var in v])))


class PromptTemplateResponse(BaseModel):
    """Prompt 模板响应"""
    model_config = ConfigDict(from_attributes=True)
    
    id: str  # 前端需要字符串类型的 ID
    name: str
    category: str
    description: Optional[str] = None
    content: str
    variables: List[str] = Field(default_factory=list)
    isActive: bool
    createdAt: str
    updatedAt: str
    
    @field_validator("id", mode="before")
    @classmethod
    def convert_id(cls, value):
        """将整数 ID 转为字符串"""
        return str(value)
    
    @field_validator("variables", mode="before")
    @classmethod
    def ensure_variables(cls, value):
        """确保变量列表"""
        if value is None:
            return []
        if isinstance(value, str):
            return [value]
        return value
    
    @field_validator("createdAt", mode="before")
    @classmethod
    def format_created_at(cls, value):
        """格式化创建时间"""
        if isinstance(value, datetime):
            return value.isoformat()
        return value
    
    @field_validator("updatedAt", mode="before")
    @classmethod
    def format_updated_at(cls, value):
        """格式化更新时间"""
        if isinstance(value, datetime):
            return value.isoformat()
        return value


class PromptTemplateToggle(BaseModel):
    """切换启用状态"""
    isActive: bool = Field(..., description="是否启用")


class PromptTemplateBatchDelete(BaseModel):
    """批量删除"""
    ids: List[str] = Field(..., min_length=1, description="要删除的ID列表")
    
    @field_validator("ids", mode="before")
    @classmethod
    def convert_ids(cls, value):
        """确保 ID 列表是字符串"""
        if not isinstance(value, list):
            raise ValueError("ids 必须是列表")
        return [str(v) for v in value]


# ========== 管理员相关 Schema ==========

class UserCreateByAdmin(BaseModel):
    """管理员创建用户"""
    username: str = Field(..., min_length=3, max_length=50)
    password: str = Field(..., min_length=8, max_length=72)
    department: Optional[str] = Field(None, max_length=128)
    role: Literal["user", "admin"] = "user"
    
    @field_validator('username')
    @classmethod
    def validate_username(cls, v):
        v = v.strip()
        if not v:
            raise ValueError('用户名不能为空')
        if v.isdigit():
            raise ValueError('用户名不能为纯数字')
        return v
    
    @field_validator('password')
    @classmethod
    def validate_password_strength(cls, v):
        if len(v) < 8:
            raise ValueError('密码长度至少8位')
        if not any(char.isdigit() for char in v):
            raise ValueError('密码必须包含数字')
        if not any(char.isupper() for char in v):
            raise ValueError('密码必须包含大写字母')
        if not any(char.islower() for char in v):
            raise ValueError('密码必须包含小写字母')
        if not re.search(r'[!@#$%^&*(),.?":{}|<>_\-+=\[\]\\\/~`]', v):
            raise ValueError('密码必须包含特殊符号')
        return v


class UserListResponse(BaseModel):
    """用户列表响应"""
    model_config = ConfigDict(from_attributes=True)
    
    user_id: str
    username: str
    department: Optional[str]
    role: str
    created_at: datetime


class UserUpdateByAdmin(BaseModel):
    """管理员更新用户"""
    username: Optional[str] = Field(None, min_length=3, max_length=50)
    department: Optional[str] = Field(None, max_length=128)
    role: Optional[Literal["user", "admin"]] = None


class AdminStatsResponse(BaseData):
    """系统统计"""
    total_users: int
    total_admins: int
    total_documents: int
    total_knowledge_bases: int
    total_conversations: int

# ========== AI-rate Schema ==========
class AIRateRequest(BaseModel):
    """请求体：传入公文/文本内容以评估 AI 生成概率"""
    content: str


class AIRateResponse(BaseData):
    """响应体：仅返回 AI 生成概率（0-100）"""
    ai_rate: float




