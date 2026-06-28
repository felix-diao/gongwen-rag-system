# Token 消耗追踪 & 写作素材上传 — 功能说明与部署指南

## 目录

1. [功能概述](#1-功能概述)
2. [Token 消耗追踪](#2-token-消耗追踪)
3. [写作素材上传](#3-写作素材上传)
4. [文件格式支持](#4-文件格式支持)
5. [部署到新服务器](#5-部署到新服务器)
6. [变更文件清单](#6-变更文件清单)

---

## 1. 功能概述

本次更新包含两个新功能：

| 功能 | 说明 |
|------|------|
| **Token 消耗追踪** | 自动记录所有 LLM API 调用的 token 用量，提供查询/统计 API |
| **写作素材上传** | 公文生成界面支持上传 PDF/Word/TXT 作为参考资料，内容自动提取并拼入 prompt |

---

## 2. Token 消耗追踪

### 2.1 工作原理

所有 LLM 调用最终经过两个底层方法，token 在出口处自动记录：

```
同步调用：公文生成/优化/会议纪要 → LLMClient.chat()  ──→ 自动记录 token
异步调用：RAG/翻译/摘要/聊天    → LLMService.chat() ──→ 自动记录 token
流式 RAG：                      → api/rag.py        ──→ 估算 token
```

无需修改任何业务代码，底层自动拦截并落库。

### 2.2 记录的字段

| 字段 | 说明 |
|------|------|
| `user_id` | 调用用户 |
| `api_category` | 类别：`llm` |
| `api_endpoint` | API 地址 |
| `model` | 模型名称 |
| `prompt_tokens` | 输入 token 数 |
| `completion_tokens` | 输出 token 数 |
| `total_tokens` | 总 token 数 |
| `duration_ms` | 调用耗时（毫秒） |
| `status` | `success` / `error` |
| `error_msg` | 失败原因 |
| `created_at` | 调用时间 |

### 2.3 API 接口

| 端点 | 方法 | 说明 |
|------|------|------|
| `/api/tokens/usage` | GET | 分页查询明细 |
| `/api/tokens/summary` | GET | 聚合统计（总 token、按类别/模型/用户） |
| `/api/tokens/stats/daily` | GET | 按天趋势 |

#### 查询参数

```
GET /api/tokens/usage?start_date=2026-06-01&end_date=2026-06-30&user_id=xxx&api_category=llm&model=deepseek-chat&page=1&page_size=50
GET /api/tokens/summary?start_date=2026-06-01&end_date=2026-06-30
GET /api/tokens/stats/daily?start_date=2026-06-01&end_date=2026-06-30
```

#### 返回示例

**`/api/tokens/summary`**

```json
{
  "success": true,
  "data": {
    "total_tokens": 125000,
    "total_prompt_tokens": 80000,
    "total_completion_tokens": 45000,
    "total_calls": 230,
    "total_errors": 3,
    "by_category": [
      { "category": "llm", "tokens": 125000, "calls": 230 }
    ],
    "by_model": [
      { "model": "deepseek-chat", "tokens": 125000, "calls": 230 }
    ],
    "by_user": [
      { "user_id": "admin", "tokens": 50000, "calls": 100 },
      { "user_id": "user_001", "tokens": 30000, "calls": 60 }
    ]
  }
}
```

**`/api/tokens/usage`**

```json
{
  "success": true,
  "data": {
    "items": [
      {
        "id": 42,
        "user_id": "admin",
        "api_category": "llm",
        "api_endpoint": "https://api.deepseek.com/v1/chat/completions",
        "model": "deepseek-chat",
        "prompt_tokens": 320,
        "completion_tokens": 180,
        "total_tokens": 500,
        "duration_ms": 1200,
        "status": "success",
        "created_at": "2026-06-08T10:30:00"
      }
    ],
    "total": 230,
    "page": 1,
    "page_size": 50
  }
}
```

---

## 3. 写作素材上传

### 3.1 工作原理

```
用户在公文生成界面点"添加文件" → 选择 PDF/Word/TXT
    ↓
前端调用 POST /api/document/extract-materials
    ↓
后端 TextProcessor 解析文件内容，返回文本
    ↓
用户点"生成"时，素材内容拼接进 prompt：
    [参考资料: 需求文档.pdf]
    {完整文本内容}
    ↓
LLM 基于素材生成公文
```

### 3.2 新增 API

| 端点 | 方法 | 说明 |
|------|------|------|
| `/api/document/extract-materials` | POST | 上传素材文件（multipart），返回提取的文本 |

请求：`multipart/form-data`，字段 `files`（多文件）

返回：

```json
{
  "success": true,
  "data": [
    {
      "filename": "需求文档.pdf",
      "text": "关于加强安全生产管理的通知要点：一、...",
      "char_count": 1024
    }
  ],
  "message": "素材提取成功"
}
```

### 3.3 前端交互

- Upload 组件选择文件后，自动上传到后端提取文本
- 上传过程显示"正在提取文件内容..."和加载动画
- 提取完成后显示文件数和总字数
- 生成公文时，素材真实文本内容被拼入 prompt

### 3.4 与知识库的区别

| | 写作素材 | 知识库 |
|------|------|------|
| 是否入库 | 否，临时提取 | 是，向量化存储 |
| 是否可复用 | 否，当次有效 | 是，全局检索 |
| 适用场景 | 单次写作参考 | 长期知识积累 |

---

## 4. 文件格式支持

| 格式 | 知识库上传 | 写作素材 | 解析方式 |
|------|:---:|:---:|------|
| `.txt` | ✅ | ✅ | UTF-8 直接读取 |
| `.md` | ✅ | ✅ | UTF-8 直接读取 |
| `.docx` | ✅ | ✅ | `python-docx` 库解析 |
| `.doc` | ✅ | ✅ | LibreOffice headless 转 `.docx` 后解析 |
| `.pdf` | ✅ | ✅ | `PyPDF2` 库解析 |

### 依赖说明

- `.docx` 解析需要 `pip install python-docx`
- `.pdf` 解析需要 `pip install PyPDF2`
- `.doc` 解析需要系统安装 LibreOffice：`apt install libreoffice-writer`

---

## 5. 部署到新服务器

### 5.1 前提条件

- 后端代码已拉取并安装 Python 依赖
- PostgreSQL 数据库可连接
- `.env` 中配置了正确的 `DATABASE_URL`

### 5.2 建表

新增了 `token_usage_records` 表。有两种方式建表：

#### 方式一：自动建表（推荐）

启动前设置环境变量：

```bash
AUTO_CREATE_ALL_TABLES=1 python -m uvicorn app.main:app --host 0.0.0.0 --port 8081
```

程序启动时会自动检测并创建缺失的表。**仅需首次设置，后续正常启动即可。**

#### 方式二：手动建表（如果不想用自动建表）

在服务器上执行一次性脚本：

```bash
cd /path/to/gongwen-rag-system
python -c "
from app.models.database import engine, Base
from sqlalchemy import text

# PostgreSQL 需先 set search_path
with engine.begin() as conn:
    conn.execute(text('SET search_path TO public'))
    Base.metadata.create_all(bind=conn)
print('建表完成')
"
```

#### 方式三：仅建 token_usage_records 表（不想动其他表）

```sql
-- 直接在 PostgreSQL 中执行
CREATE TABLE IF NOT EXISTS public.token_usage_records (
    id SERIAL PRIMARY KEY,
    user_id VARCHAR(64),
    api_category VARCHAR(32) NOT NULL,
    api_endpoint VARCHAR(256) NOT NULL,
    model VARCHAR(128),
    prompt_tokens INTEGER DEFAULT 0,
    completion_tokens INTEGER DEFAULT 0,
    total_tokens INTEGER DEFAULT 0,
    request_chars INTEGER DEFAULT 0,
    duration_ms INTEGER,
    status VARCHAR(16) DEFAULT 'success',
    error_msg TEXT,
    metadata_json TEXT,
    created_at TIMESTAMP DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_token_usage_user_id ON public.token_usage_records(user_id);
CREATE INDEX IF NOT EXISTS idx_token_usage_category ON public.token_usage_records(api_category);
```

### 5.3 验证部署

```bash
# 1. 检查新 API 路由是否注册
curl http://localhost:8081/openapi.json | python3 -c "import sys,json; [print(p) for p in json.load(sys.stdin)['paths'] if 'tokens' in p or 'extract-materials' in p]"

# 预期输出：
# /api/tokens/usage
# /api/tokens/summary
# /api/tokens/stats/daily
# /api/document/extract-materials

# 2. 健康检查
curl http://localhost:8081/health
```

### 5.4 前端部署

前端改动在 `src/pages/AI/DocumentWriter/index.tsx` 和 `src/services/ai.ts`。如果部署前端：

```bash
cd /path/to/DocumentWriter
npm install
npm run build   # 生产构建到 dist/
# 或将 dist/ 部署到 Nginx / CDN
```

开发模式（热更新）：

```bash
npm run dev
```

### 5.5 生产环境注意事项

1. **Token 追踪对性能影响极小**：每次 LLM 调用额外写入一条数据库记录（~1ms），失败时静默跳过不影响主流程
2. **表大小预估**：假设每天 1000 次 LLM 调用，每条记录约 200 字节，一年约 73MB——无需特殊维护
3. **流式调用 token 估算**：流式 API 不返回 `usage` 字段，按 `字符数 / 1.5` 估算，存在一定误差（通常 ±20%）
4. **预览功能在 Chrome 中需 PDF 文件**：确保 `.pdf` 文件可通过静态路由访问

---

## 6. 变更文件清单

### 新增文件（2 个）

| 文件 | 说明 |
|------|------|
| `app/services/token_tracker.py` | Token 消耗追踪服务（写入 + 查询） |
| `app/api/tokens.py` | Token 查询 API 路由 |

### 修改文件（7 个）

| 文件 | 修改内容 |
|------|---------|
| `app/models/database.py` | 新增 `TokenUsageRecord` ORM 模型 |
| `app/models/schemas.py` | 新增 Token 查询/响应 Pydantic schema |
| `app/llm_client/client.py` | `LLMClient.chat()` 解析 `usage` 并自动记录 |
| `app/services/llm_service.py` | `LLMService.chat()` / `stream_chat()` 同上 |
| `app/api/rag.py` | 流式 RAG 端点添加 token 估算记录 |
| `app/api/document.py` | 新增 `POST /api/document/extract-materials` 端点 |
| `app/utils/text_processor.py` | 新增 `.doc` / `.md` 格式支持 + LibreOffice 转换 |
| `app/main.py` | 注册 `tokens` 路由 |
| `app/services/knowledge_service.py` | 支持类型添加 `.doc` |

### 前端修改文件（2 个）

| 文件 | 修改内容 |
|------|---------|
| `src/services/ai.ts` | 新增 `extractMaterials()` 函数 |
| `src/pages/AI/DocumentWriter/index.tsx` | Upload 组件改为真实上传 + 素材文本拼入 prompt |
