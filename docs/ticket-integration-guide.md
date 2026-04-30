# 一次性登录 Ticket 集成文档

## 概述

本文档描述如何从第三方平台集成一次性登录功能，实现用户无感知登录。

**核心流程：**
```
第三方平台 → 获取 Ticket → 拼接 URL → 用户点击 → 自动登录
```

---

## 一、完整逻辑流程

### 1.1 Ticket 登录流程

```
用户打开链接（带 ticket 参数）
        │
        ▼
┌───────────────────────────────┐
│  前端检查：是否有现有 token？  │
└───────────────────────────────┘
        │
   ┌────┴────┐
   │         │
   ▼         ▼
 有 token   无 token
   │         │
   │         ▼
   │    ┌─────────────────────────────┐
   │    │  调用 redeem-ticket 兑换 ticket │
   │    └─────────────────────────────┘
   │         │
   │    ┌────┴────┐
   │    │         │
   │    ▼         ▼
   │  兑换成功   兑换失败
   │    │         │
   │    │         ▼
   │    │    显示"ticket已使用/过期"
   │    │    跳转到登录页
   │    │
   │    ▼
   │  设置 token 到 localStorage
   │    │
   └────┴────┐
              │
              ▼
    ┌──────────────────────────────┐
    │  检查：needs_password_setup？  │
    └──────────────────────────────┘
              │
         ┌────┴────┐
         │         │
         ▼         ▼
     true        false
         │         │
         ▼         │
   跳转到设置密码页  │
   设置完成后跳转    │
   到目标页面       │
                    │
              ┌─────┘
              ▼
        进入目标页面
```

### 1.2 Token 处理逻辑

**关键原则：已登录用户不受 ticket 影响**

| 场景 | 行为 |
|------|------|
| 有 token | 不兑换 ticket，静默跳过，保持当前登录 |
| 无 token + 兑换成功 | 设置 token，正常登录 |
| 无 token + 兑换失败 | 显示"ticket已使用/过期"提示，跳转登录页 |

**这样设计的好处：**
- 避免用户 A 的 token 被用户 B 的 ticket 覆盖
- 已登录用户点击同一链接不会看到错误提示

### 1.3 新用户设置密码流程

```
新用户通过 ticket 登录
        │
        ▼
检查 needs_password_setup = true
        │
        ▼
跳转到 /user/set-password 页面
        │
        ▼
用户设置密码（不需要旧密码）
        │
        ▼
调用 /api/auth/set-password
        │
        ▼
needs_password_setup 设为 false
        │
        ▼
跳转到目标页面
```

---

## 二、后端部署步骤

### 2.1 数据库表结构

**新增 login_tickets 表：**
```sql
CREATE TABLE login_tickets (
    id SERIAL PRIMARY KEY,
    ticket VARCHAR(64) UNIQUE NOT NULL,
    username VARCHAR(256) NOT NULL,
    is_used BOOLEAN DEFAULT FALSE,
    expires_at TIMESTAMP NOT NULL,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX idx_login_tickets_ticket ON login_tickets(ticket);
CREATE INDEX idx_login_tickets_username ON login_tickets(username);
CREATE INDEX idx_login_tickets_is_used ON login_tickets(is_used);
```

**修改 users 表，添加 needs_password_setup 字段：**
```sql
ALTER TABLE users ADD COLUMN needs_password_setup BOOLEAN DEFAULT FALSE;
CREATE INDEX idx_users_needs_password_setup ON users(needs_password_setup);
```

### 2.2 后端文件改动清单

| 文件 | 改动说明 |
|------|----------|
| `app/models/database.py` | 新增 `LoginTicket` 表，`User` 表添加 `needs_password_setup` 字段 |
| `app/models/schemas.py` | 新增 `CreateTicketRequest`、`RedeemTicketRequest`、`RedeemTicketResponse`、`SetPasswordRequest`、`SetPasswordResponse` |
| `app/api/admin.py` | 新增 `/create-ticket`、`/redeem-ticket`、`/set-password` 接口，修改 `/me` 返回 `needs_password_setup` |
| `app/utils/auth.py` | 新增 `generate_random_password()` 函数 |

### 2.3 部署命令

**方式一：自动建表（开发环境）**
```bash
cd /path/to/gongwen-rag-system
source .venv/bin/activate
python -c "
from app.models.database import Base, engine
from sqlalchemy import text

# 创建新表
Base.metadata.create_all(bind=engine)

# 添加新字段（如果不存在）
with engine.connect() as conn:
    try:
        conn.execute(text('ALTER TABLE users ADD COLUMN needs_password_setup BOOLEAN DEFAULT FALSE'))
        conn.commit()
        print('Column needs_password_setup added')
    except Exception as e:
        if 'already exists' in str(e):
            print('Column already exists')
        else:
            print(f'Error: {e}')
"
```

**方式二：Alembic 迁移（生产环境推荐）**
```bash
# 生成迁移文件
alembic revision --autogenerate -m "add login_tickets and needs_password_setup"

# 执行迁移
alembic upgrade head
```

**方式三：手动 SQL（PostgreSQL 生产环境）**
```bash
# 连接数据库
psql -U your_user -d your_database

# 执行 SQL
\i /path/to/migration.sql
```

migration.sql 内容：
```sql
-- 创建 login_tickets 表
CREATE TABLE IF NOT EXISTS login_tickets (
    id SERIAL PRIMARY KEY,
    ticket VARCHAR(64) UNIQUE NOT NULL,
    username VARCHAR(256) NOT NULL,
    is_used BOOLEAN DEFAULT FALSE,
    expires_at TIMESTAMP NOT NULL,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_login_tickets_ticket ON login_tickets(ticket);
CREATE INDEX IF NOT EXISTS idx_login_tickets_username ON login_tickets(username);
CREATE INDEX IF NOT EXISTS idx_login_tickets_is_used ON login_tickets(is_used);

-- 添加 needs_password_setup 字段
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM information_schema.columns 
        WHERE table_name = 'users' AND column_name = 'needs_password_setup'
    ) THEN
        ALTER TABLE users ADD COLUMN needs_password_setup BOOLEAN DEFAULT FALSE;
    END IF;
END $$;

CREATE INDEX IF NOT EXISTS idx_users_needs_password_setup ON users(needs_password_setup);
```

---

## 三、前端部署步骤

### 3.1 前端文件改动清单

| 文件 | 改动说明 |
|------|----------|
| `src/pages/user/set-password/index.tsx` | **新增** 设置密码页面 |
| `src/services/ant-design-pro/api.ts` | 新增 `setPassword()` 函数 |
| `src/app.tsx` | 修改 `getInitialState()`，添加 ticket 处理和新用户检测逻辑 |
| `config/routes.ts` | 新增 `/user/set-password` 路由 |
| `types/index.d.ts` | 新增相关类型定义，`RedeemTicketResult` 字段改为可选 |
| `src/requestErrorConfig.ts` | 错误拦截器保持原有逻辑 |

### 3.2 前端构建部署

```bash
cd /path/to/DocumentWriter

# 安装依赖（如有新增）
npm install

# 构建
npm run build

# 部署 dist 目录到服务器
```

---

## 四、第三方平台集成步骤

### 4.1 获取 Ticket

```bash
POST http://公文系统地址/api/auth/create-ticket
Content-Type: application/json

{"username": "用户名"}
```

返回：
```json
{
    "success": true,
    "data": {
        "ticket": "ticket_xxx...",
        "expires_in": 300
    }
}
```

### 4.2 拼接 URL 跳转

```
http://公文系统前端地址/目标页面?ticket=ticket_xxx...
```

**目标页面示例：**
- `/welcome` - 欢迎页
- `/ai/document/write` - 文档写作
- `/knowledge` - 知识库
- `/meetings` - 会议管理

### 4.3 代码示例

**Python（FastAPI）：**
```python
import httpx
from fastapi.responses import RedirectResponse

@app.get("/jump-to-gongwen")
async def jump_to_gongwen():
    username = get_current_user()  # 从 session 获取
    resp = httpx.post("http://公文系统/api/auth/create-ticket", 
                      json={"username": username})
    ticket = resp.json()["data"]["ticket"]
    return RedirectResponse(f"http://公文系统/welcome?ticket={ticket}")
```

**Java（Spring Boot）：**
```java
@GetMapping("/jump-to-gongwen")
public String jumpToGongwen() {
    String username = getCurrentUser();
    RestTemplate restTemplate = new RestTemplate();
    Map<String, String> body = Map.of("username", username);
    ResponseEntity<Map> resp = restTemplate.postForEntity(
        "http://公文系统/api/auth/create-ticket", body, Map.class);
    String ticket = (String) ((Map) resp.getBody().get("data")).get("ticket");
    return "redirect:http://公文系统/welcome?ticket=" + ticket;
}
```

---

## 五、API 接口详情

### 5.1 创建 Ticket

**请求：**
```bash
POST /api/auth/create-ticket
Content-Type: application/json

{"username": "zhangsan"}
```

**响应：**
```json
{
    "success": true,
    "data": {
        "ticket": "ticket_xxx...",
        "expires_in": 300
    },
    "message": "ticket 创建成功"
}
```

**说明：**
- 用户不存在时自动注册（`needs_password_setup=True`）
- Ticket 有效期 5 分钟

### 5.2 兑换 Ticket

**请求：**
```bash
POST /api/auth/redeem-ticket
Content-Type: application/json

{"ticket": "ticket_xxx..."}
```

**成功响应：**
```json
{
    "success": true,
    "data": {
        "access_token": "eyJhbGciOiJIUzI1NiIs...",
        "token_type": "bearer",
        "user_id": "user_xxx",
        "username": "zhangsan"
    },
    "message": "登录成功"
}
```

**失败响应（ticket 已使用）：**
```json
{
    "success": false,
    "data": {
        "username": "zhangsan"
    },
    "message": "ticket 已被使用"
}
```

### 5.3 新用户设置密码

**请求：**
```bash
POST /api/auth/set-password
Authorization: Bearer <token>
Content-Type: application/json

{"new_password": "NewPass@123"}
```

**响应：**
```json
{
    "success": true,
    "data": {
        "message": "密码设置成功",
        "user_id": "user_xxx",
        "username": "zhangsan",
        "changed_at": "2026-05-01T00:00:00"
    },
    "message": "密码设置成功"
}
```

**说明：**
- 只有 `needs_password_setup=True` 的用户才能调用
- 设置成功后 `needs_password_setup` 自动设为 `False`

### 5.4 获取用户信息

**请求：**
```bash
GET /api/auth/me
Authorization: Bearer <token>
```

**响应：**
```json
{
    "user_id": "user_xxx",
    "username": "zhangsan",
    "role": "user",
    "department": null,
    "created_at": "2026-05-01T00:00:00",
    "is_admin": false,
    "needs_password_setup": true
}
```

---

## 六、前端核心代码逻辑

### 6.1 app.tsx 初始化逻辑

```typescript
export async function getInitialState() {
  // 1. 检查 URL 中的 ticket 参数
  const ticket = urlParams.get('ticket');
  
  // 2. 检查是否已有 token
  const existingToken = localStorage.getItem('access_token');
  
  if (ticket) {
    if (existingToken) {
      // 已登录：不兑换 ticket，静默跳过
      console.log('Already logged in, skipping ticket redemption');
    } else {
      // 未登录：兑换 ticket
      const response = await redeemTicket({ ticket });
      if (response.success && response.data?.access_token) {
        setToken(response.data.access_token);
      }
      // 失败则后续流程会跳转到登录页
    }
    // 清除 URL 中的 ticket 参数
    window.history.replaceState({}, '', cleanUrl);
  }
  
  // 3. 获取用户信息
  const currentUser = await fetchUserInfo();
  
  // 4. 检查新用户是否需要设置密码
  if (currentUser?.needs_password_setup) {
    history.push('/user/set-password?redirect=' + targetPath);
  }
  
  return { currentUser, ... };
}
```

### 6.2 页面切换拦截

```typescript
onPageChange: () => {
  // 新用户需要设置密码
  if (initialState?.currentUser?.needs_password_setup) {
    history.push('/user/set-password');
    return;
  }
  // 未登录跳转到登录页
  if (!initialState?.currentUser) {
    history.push('/user/login');
  }
}
```

---

## 七、关键特性

### 7.1 Ticket 是可选的

| 用户类型 | 登录方式 | 设置密码 |
|----------|----------|----------|
| 普通用户 | `/user/login` 输入用户名密码 | 使用修改密码功能（需旧密码） |
| Ticket 新用户 | 点击链接自动登录 | 首次登录强制设置密码 |

两种方式并存，互不影响。

### 7.2 Token 安全处理

```
场景：用户 A 已登录，点击用户 B 的 ticket 链接

处理：保持用户 A 的登录状态，不兑换 ticket，静默跳过

原因：避免 token 被覆盖，保证安全性
```

### 7.3 错误提示策略

| 场景 | 提示 |
|------|------|
| 已登录用户点击已用 ticket | 无提示（静默跳过） |
| 未登录用户点击已用 ticket | "ticket已使用，请重新获取链接" |
| 未登录用户点击过期 ticket | "ticket已过期，请重新获取链接" |

---

## 八、测试验证

### 8.1 后端测试

```bash
# 运行测试
PYTHONPATH=. pytest tests/test_ticket_api.py -v

# 测试结果应全部通过（34个测试）
```

### 8.2 手动测试流程

```bash
# 1. 创建 ticket
curl -X POST http://localhost:8080/api/auth/create-ticket \
  -H "Content-Type: application/json" \
  -d '{"username": "testuser"}'

# 2. 浏览器打开链接
# http://localhost:8000/welcome?ticket=ticket_xxx...

# 3. 首次打开：自动登录 → 跳转设置密码页 → 设置密码 → 进入目标页面

# 4. 再次打开同一链接：已登录状态，静默跳过，不显示错误
```

### 8.3 测试场景清单

| 场景 | 预期结果 |
|------|----------|
| 新用户首次点击 ticket 链接 | 登录成功 → 跳转设置密码页 |
| 新用户设置密码后 | 跳转到目标页面 |
| 已登录用户点击已用 ticket | 静默跳过，不显示错误 |
| 未登录用户点击已用 ticket | 显示"ticket已使用"，跳转登录页 |
| 未登录用户点击过期 ticket | 显示"ticket已过期"，跳转登录页 |
| 普通用户注册 | 直接登录，不跳转设置密码页 |

---

## 九、安全建议

### 9.1 必须项

- **HTTPS**：生产环境必须使用 HTTPS
- **Ticket 有效期**：当前 5 分钟，可根据安全需求缩短

### 9.2 推荐项

- **API Key 认证**：为 `create-ticket` 接口添加认证
- **IP 白名单**：限制只有特定 IP 才能调用 `create-ticket`

```python
# API Key 认证示例
API_KEYS = {"your_secret_key"}

@router.post("/create-ticket")
def create_ticket(
    ticket_data: CreateTicketRequest,
    x_api_key: str = Header(None)
):
    if x_api_key not in API_KEYS:
        raise HTTPException(status_code=401, detail="Invalid API Key")
    ...
```

---

## 十、常见问题

### Q1: 第三方平台需要做什么？

只需要两步：
1. 调用接口获取 ticket
2. 拼接 URL 给用户

### Q2: 普通用户注册会跳转设置密码页吗？

不会。只有通过 ticket 登录的新用户才会跳转。

### Q3: 已登录用户点击 ticket 链接会发生什么？

保持当前登录状态，不会兑换 ticket，不会显示错误。

### Q4: 如何区分新用户和老用户？

- `needs_password_setup = true`：新用户（需要设置密码）
- `needs_password_setup = false`：老用户

### Q5: 用户设置密码后还能再次设置吗？

不能。`/api/auth/set-password` 只对 `needs_password_setup=true` 的用户有效。

---

## 十一、文件修改汇总

### 后端文件

```
app/
├── api/
│   └── admin.py          # 新增 3 个接口，修改 /me 接口
├── models/
│   ├── database.py       # 新增 LoginTicket 表，User 表添加字段
│   └── schemas.py        # 新增 5 个 Schema 类
└── utils/
    └── auth.py           # 新增 generate_random_password 函数

tests/
└── test_ticket_api.py    # 新增测试文件（34个测试用例）
```

### 前端文件

```
src/
├── app.tsx               # 修改初始化逻辑
├── pages/
│   └── user/
│       └── set-password/
│           └── index.tsx # 新增设置密码页面
├── services/
│   └── ant-design-pro/
│       └── api.ts        # 新增 setPassword 函数
└── requestErrorConfig.ts # 保持原有逻辑

config/
└── routes.ts             # 新增路由

types/
└── index.d.ts            # 新增类型定义
```
