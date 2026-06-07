# app/models/schemas.py
from pydantic import BaseModel, Field, ConfigDict, field_validator
from typing import Optional, List, Literal, Generic, TypeVar
from datetime import date, datetime
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
    is_public: bool
    user_id: str
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
    pdf_url: Optional[str] = None
    word_url: Optional[str] = None


class ConversationResponse(BaseModel):
    """会话响应模型"""
    model_config = ConfigDict(from_attributes=True)
    
    conv_id: str
    query: str
    answer: str
    weight: float
    liked: bool
    pdf_url: Optional[str] = None
    word_url: Optional[str] = None
    created_at: datetime


class ConversationFeedback(BaseModel):
    """会话反馈"""
    liked: Optional[bool] = None
    weight_delta: Optional[float] = None


class ConversationSearchRequest(BaseModel):
    """会话搜索请求"""
    query: str
    top_k: int = 3


class ConversationSearchResult(BaseModel):
    """会话搜索结果"""
    id: str
    query: str
    answer: str
    score: float
    weight: float
    created_at: int


class ConversationStatistics(BaseModel):
    """会话统计"""
    total_conversations: int
    liked_conversations: int
    like_rate: float

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
        
        if v.isdigit():
            raise ValueError('用户名不能为纯数字')
        
        return v
    
    @field_validator('password')
    @classmethod
    def validate_password_strength(cls, v):
        """验证密码强度"""
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
        """验证密码强度"""
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
    is_public: bool = Field(default=False, description="是否为公共模板（仅管理员可设置）")

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
            return [var.strip() for var in v.split(',') if var.strip()]
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
    is_public: Optional[bool] = None
    
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
    
    id: str
    name: str
    category: str
    description: Optional[str] = None
    content: str
    variables: List[str] = Field(default_factory=list)
    isActive: bool
    isPublic: bool
    userId: str
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


# ========== 会议纪要相关 Schema（统一放在文件最后） ==========

class MeetingBase(BaseModel):
    """会议基础字段。"""
    title: str
    date: datetime
    location: Optional[str] = None
    host: Optional[str] = None
    participants: Optional[str] = None
    content_text: Optional[str] = None
    meeting_url: Optional[str] = None
    status: Optional[str] = "created"
    provider: Optional[str] = "volc"


class MeetingCreate(MeetingBase):
    """创建会议请求体。"""
    pass


class MeetingUpdate(MeetingBase):
    """更新会议请求体。

    meeting_domain 支持部分更新，因此重写必填字段为可选。
    """
    title: Optional[str] = None
    date: Optional[datetime] = None


class MeetingInDB(MeetingBase):
    """会议响应模型。"""
    model_config = ConfigDict(from_attributes=True)

    id: int
    creator_id: Optional[str] = None
    created_at: datetime
    updated_at: datetime



class MeetingAudioBase(BaseModel):
    """会议音频公共字段。"""

    provider: Literal["local", "volc"]
    meeting_id: int
    creator_id: Optional[str] = None
    file_name: Optional[str] = None
    object_key: Optional[str] = None
    file_url: Optional[str] = None
    file_type: Optional[str] = None
    status: str


class MeetingAudioInDB(MeetingAudioBase):
    """持久化后的会议音频响应模型。"""
    model_config = ConfigDict(from_attributes=True)

    id: int
    created_at: datetime
    updated_at: datetime


class MeetingAudioUploadTask(BaseModel):
    """会议音频上传任务响应模型。"""

    task_id: str
    provider: Literal["local", "volc"]
    meeting_id: int
    creator_id: Optional[str] = None
    file_name: str
    file_type: Optional[str] = None
    status: Literal["pending", "running", "completed", "failed"]
    audio_id: Optional[int] = None
    error_msg: Optional[str] = None
    audio: Optional["MeetingAudioInDB"] = None
    created_at: datetime
    updated_at: datetime


class VolcMeetingSummaryBase(BaseModel):
    """火山纪要摘要公共字段。"""
    title: Optional[str] = None
    paragraph: str
    source_audio_id: Optional[int] = None


class VolcMeetingSummaryCreate(VolcMeetingSummaryBase):
    """创建或更新火山纪要摘要。"""
    pass


class VolcMeetingSummaryInDB(VolcMeetingSummaryBase):
    """火山纪要摘要响应模型。"""
    model_config = ConfigDict(from_attributes=True)

    id: int
    meeting_id: int
    created_at: datetime
    updated_at: datetime


class VolcMeetingTodoBase(BaseModel):
    """火山纪要待办公共字段。"""
    content: str
    executor: Optional[str] = None
    execution_time: Optional[str] = None
    source_audio_id: Optional[int] = None


class VolcMeetingTodoCreate(VolcMeetingTodoBase):
    """创建或更新火山纪要待办。"""
    pass


class VolcMeetingTodoInDB(VolcMeetingTodoBase):
    """火山纪要待办响应模型。"""
    model_config = ConfigDict(from_attributes=True)

    id: int
    meeting_id: int
    created_at: datetime
    updated_at: datetime


class VolcTranscriptUpdate(BaseModel):
    """修改火山纪要转写文本。"""
    transcript_text: str


class VolcMinutesJobInDB(BaseModel):
    """火山妙记离线任务响应模型。"""
    model_config = ConfigDict(from_attributes=True)

    id: int
    meeting_id: int
    source_audio_id: Optional[int] = None
    input_file_url: str
    input_file_type: Optional[str] = None
    volc_task_id: Optional[str] = None
    status: str
    error_msg: Optional[str] = None
    created_at: datetime
    updated_at: datetime


class VolcMinutesCancelRequest(BaseModel):
    """取消火山妙记任务请求。"""
    job_id: Optional[int] = None
    reason: Optional[str] = None


class VolcMinutesCancelResponse(BaseModel):
    """取消火山妙记任务响应。"""
    meeting_id: int
    job_id: int
    source_audio_id: Optional[int] = None
    task_id: Optional[str] = None
    status: Literal["cancelled"] = "cancelled"


class VolcSpeakerSegmentInDB(BaseModel):
    """火山说话人分段响应模型（由 volc_accurate_transcriptions.speaker_segments_json 解析展开）。"""
    model_config = ConfigDict(from_attributes=True)

    id: int
    meeting_id: int
    source_audio_id: int
    segment_index: int
    speaker: str
    text: str
    start_ms: Optional[float] = None
    end_ms: Optional[float] = None
    created_at: datetime
    updated_at: datetime


class SpeakerSegment(BaseModel):
    """宽松版说话人分段模型。

    旧火山接口只关心说话人和文本，新链路还会带分段主键与审计字段。
    """
    id: Optional[int] = None
    meeting_id: Optional[int] = None
    source_audio_id: Optional[int] = None
    segment_index: Optional[int] = None
    speaker: str
    text: str
    start_ms: Optional[float] = None
    end_ms: Optional[float] = None
    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None


class VolcMeetingMinutesResponse(BaseModel):
    """火山会议纪要聚合响应。"""
    stream_transcript_text: Optional[str] = None
    transcript_text: Optional[str] = None
    minutes_job_id: Optional[int] = None
    minutes_job_status: Optional[str] = None
    audio_status: Optional[str] = None
    speaker_segments: List[SpeakerSegment] = Field(default_factory=list)
    summary: Optional[VolcMeetingSummaryInDB] = None
    todos: List[VolcMeetingTodoInDB] = Field(default_factory=list)


class VolcSessionTodoItem(BaseModel):
    """火山历史会话里的待办快照。"""
    content: str
    executor: Optional[str] = None
    execution_time: Optional[str] = None
    source_audio_id: Optional[int] = None


class VolcSessionSpeakerSegment(BaseModel):
    """火山历史会话里的说话人分段快照。"""
    speaker: str
    text: str
    start_ms: Optional[float] = None
    end_ms: Optional[float] = None


class VolcMeetingMinutesSessionInDB(BaseModel):
    """火山纪要历史快照响应模型。"""
    model_config = ConfigDict(from_attributes=True)

    id: int
    session_no: Optional[str] = None
    meeting_id: int
    source_audio_id: Optional[int] = None
    status: str
    stream_transcript_text: Optional[str] = None
    transcript_text: Optional[str] = None
    speaker_segments: List[VolcSessionSpeakerSegment] = Field(default_factory=list)
    summary_title: Optional[str] = None
    summary_paragraph: Optional[str] = None
    todos: List[VolcSessionTodoItem] = Field(default_factory=list)
    created_at: datetime
    updated_at: datetime


class VolcMeetingMinutesSessionUpdate(BaseModel):
    """修改火山纪要历史快照。"""
    stream_transcript_text: Optional[str] = None
    transcript_text: Optional[str] = None
    speaker_segments: Optional[List[VolcSessionSpeakerSegment]] = None
    summary_title: Optional[str] = None
    summary_paragraph: Optional[str] = None
    todos: Optional[List[VolcSessionTodoItem]] = None


class LocalMeetingSummaryBase(BaseModel):
    """本地纪要摘要公共字段。"""
    title: Optional[str] = None
    paragraph: str
    source_audio_id: Optional[int] = None


class LocalMeetingSummaryCreate(LocalMeetingSummaryBase):
    """创建或更新本地纪要摘要。"""
    pass


class LocalMeetingSummaryInDB(LocalMeetingSummaryBase):
    """本地纪要摘要响应模型。"""
    model_config = ConfigDict(from_attributes=True)

    id: int
    meeting_id: int
    created_at: datetime
    updated_at: datetime


class LocalMeetingTodoBase(BaseModel):
    """本地纪要待办公共字段。"""
    content: str
    executor: Optional[str] = None
    execution_time: Optional[str] = None
    source_audio_id: Optional[int] = None


class LocalMeetingTodoCreate(LocalMeetingTodoBase):
    """创建或更新本地纪要待办。"""
    pass


class LocalMeetingTodoInDB(LocalMeetingTodoBase):
    """本地纪要待办响应模型。"""
    model_config = ConfigDict(from_attributes=True)

    id: int
    meeting_id: int
    created_at: datetime
    updated_at: datetime


class LocalStreamTranscriptUpdate(BaseModel):
    """修改本地实时流式转写文本。"""
    stream_transcript_text: str


class LocalTranscriptUpdate(BaseModel):
    """修改本地会议音频转写文本。"""
    transcript_text: str


class LocalMeetingMinutesResponse(BaseModel):
    """本地会议纪要聚合响应。

    transcript_text 与 stream_transcript_text 同源。asr_session_id 始终为「最新一条」local_asr_sessions；
    若该行尚无正文（如异步转写中），转写字段回退为最近一条有稿的会话，避免轮询时空白。
    """
    transcript_text: Optional[str] = None
    stream_transcript_text: Optional[str] = None
    asr_session_id: Optional[int] = None
    asr_status: Optional[str] = None
    processing_asr_session_id: Optional[int] = None
    processing_stage: Optional[Literal["transcribe", "minutes"]] = None
    processing_status: Optional[str] = None
    source_audio_id: Optional[int] = None
    audio_status: Optional[str] = None
    summary: Optional[LocalMeetingSummaryInDB] = None
    todos: List[LocalMeetingTodoInDB] = Field(default_factory=list)


class LocalAsrTranscribeFromAudioResponse(BaseModel):
    """对已上传的本地会议音频提交分段 HTTP 转写；异步完成后写入 local_asr_sessions。"""

    asr_session_id: int
    meeting_id: int
    source_audio_id: int
    status: Literal["processing"] = "processing"


class LocalProcessingCancelRequest(BaseModel):
    """取消本地转写/纪要处理请求。"""
    asr_session_id: Optional[int] = None
    reason: Optional[str] = None


class LocalProcessingCancelResponse(BaseModel):
    """取消本地转写/纪要处理响应。"""
    meeting_id: int
    asr_session_id: int
    source_audio_id: Optional[int] = None
    stage: Literal["transcribe", "minutes"]
    status: Literal["cancel_requested"] = "cancel_requested"


class LocalSessionTodoItem(BaseModel):
    """本地历史会话里的待办快照。"""
    content: str
    executor: Optional[str] = None
    execution_time: Optional[str] = None
    source_audio_id: Optional[int] = None


class LocalMeetingMinutesSessionInDB(BaseModel):
    """本地纪要历史快照响应模型。"""
    model_config = ConfigDict(from_attributes=True)

    id: int
    session_no: Optional[str] = None
    meeting_id: int
    source_audio_id: Optional[int] = None
    stream_transcript_text: Optional[str] = None
    transcript_text: Optional[str] = None
    summary_title: Optional[str] = None
    summary_paragraph: Optional[str] = None
    todos: List[LocalSessionTodoItem] = Field(default_factory=list)
    created_at: datetime
    updated_at: datetime


class LocalMeetingMinutesSessionUpdate(BaseModel):
    """修改本地纪要历史快照。"""
    stream_transcript_text: Optional[str] = None
    transcript_text: Optional[str] = None
    summary_title: Optional[str] = None
    summary_paragraph: Optional[str] = None
    todos: Optional[List[LocalSessionTodoItem]] = None


# ========== 一次性 Ticket 相关 Schema ==========

class CreateTicketRequest(BaseModel):
    """创建 ticket 请求"""
    username: str = Field(..., min_length=3, max_length=50, description="用户名")

    @field_validator('username')
    @classmethod
    def validate_username(cls, v):
        """验证用户名"""
        v = v.strip()
        if not v:
            raise ValueError('用户名不能为空或只包含空格')
        if v.isdigit():
            raise ValueError('用户名不能为纯数字')
        return v


class CreateTicketResponse(BaseData):
    """创建 ticket 响应"""
    ticket: str = Field(..., description="一次性票据")
    expires_in: int = Field(..., description="有效期（秒）")


class RedeemTicketRequest(BaseModel):
    ticket: str = Field(..., min_length=1, description="一次性票据")

class RedeemTicketResponse(BaseData):
    """兑换 ticket 响应"""
    access_token: Optional[str] = None
    token_type: str = "bearer"
    user_id: Optional[str] = None
    username: Optional[str] = None


class ResetPasswordByUsernameRequest(BaseModel):
    """根据用户名重置密码请求"""
    username: str = Field(..., min_length=3, max_length=50, description="用户名")
    new_password: str = Field(..., min_length=8, max_length=72, description="新密码")

    @field_validator('username')
    @classmethod
    def validate_username(cls, v):
        """验证用户名"""
        v = v.strip()
        if not v:
            raise ValueError('用户名不能为空或只包含空格')
        if v.isdigit():
            raise ValueError('用户名不能为纯数字')
        return v

    @field_validator('new_password')
    @classmethod
    def validate_password_strength(cls, v):
        """验证密码强度"""
        if len(v) < 8:
            raise ValueError('密码长度至少8位')
        if not any(char.isdigit() for char in v):
            raise ValueError('密码必须包含至少一个数字')
        if not any(char.isupper() for char in v):
            raise ValueError('密码必须包含至少一个大写字母')
        if not any(char.islower() for char in v):
            raise ValueError('密码必须包含至少一个小写字母')
        if not re.search(r'[!@#$%^&*(),.?":{}|<>_\-+=\[\]\\\/~`]', v):
            raise ValueError('密码必须包含至少一个特殊符号(!@#$%^&*等)')
        return v


class ResetPasswordByUsernameResponse(BaseData):
    """根据用户名重置密码响应"""
    message: str
    user_id: str
    username: str
    changed_at: datetime


class SetPasswordRequest(BaseModel):
    """新用户设置密码请求（不需要旧密码）"""
    new_password: str = Field(..., min_length=8, max_length=72, description="新密码")

    @field_validator('new_password')
    @classmethod
    def validate_password_strength(cls, v):
        """验证密码强度"""
        if len(v) < 8:
            raise ValueError('密码长度至少8位')
        if not any(char.isdigit() for char in v):
            raise ValueError('密码必须包含至少一个数字')
        if not any(char.isupper() for char in v):
            raise ValueError('密码必须包含至少一个大写字母')
        if not any(char.islower() for char in v):
            raise ValueError('密码必须包含至少一个小写字母')
        if not re.search(r'[!@#$%^&*(),.?":{}|<>_\-+=\[\]\\\/~`]', v):
            raise ValueError('密码必须包含至少一个特殊符号(!@#$%^&*等)')
        return v


class SetPasswordResponse(BaseData):
    """新用户设置密码响应"""
    message: str
    user_id: str
    username: str
    changed_at: datetime


# ========== Token 消耗追踪相关 Schema ==========


class TokenUsageRecordInDB(BaseModel):
    """Token 消耗记录响应。"""
    model_config = ConfigDict(from_attributes=True)

    id: int
    user_id: Optional[str] = None
    api_category: str
    api_endpoint: str
    model: Optional[str] = None
    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0
    request_chars: int = 0
    duration_ms: Optional[int] = None
    status: str = "success"
    error_msg: Optional[str] = None
    metadata_json: Optional[str] = None
    created_at: datetime


class TokenUsageQuery(BaseModel):
    """Token 消耗查询参数。"""
    start_date: Optional[str] = None
    end_date: Optional[str] = None
    user_id: Optional[str] = None
    api_category: Optional[str] = None
    model: Optional[str] = None
    page: int = Field(default=1, ge=1)
    page_size: int = Field(default=50, ge=1, le=500)


class TokenUsageSummary(BaseModel):
    """Token 消耗聚合统计。"""
    total_tokens: int = 0
    total_prompt_tokens: int = 0
    total_completion_tokens: int = 0
    total_calls: int = 0
    total_errors: int = 0
    by_category: List[dict] = Field(default_factory=list)
    by_model: List[dict] = Field(default_factory=list)
    by_user: List[dict] = Field(default_factory=list)


class TokenDailyStat(BaseModel):
    """按天统计。"""
    date: str
    total_tokens: int
    total_calls: int


class TokenUsageListResponse(BaseModel):
    """分页列表响应。"""
    items: List[TokenUsageRecordInDB]
    total: int
    page: int
    page_size: int


# 统一保留 schemas.py 作为唯一 schema 入口
