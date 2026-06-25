# 数据库迁移说明

## 为什么需要迁移

本次迁移为 `volc_asr_sessions` 表新增两个字段：

- `recording_session_id VARCHAR(64)`：标识一次连续录音。用户开始录音后，无论中间因为暂停、网络抖动、切后台、锁屏等原因导致 WebSocket 断连多少次，所有 ASR 会话都复用同一个 `recording_session_id`。
- `audio_part_path VARCHAR(512)`：每个 ASR 会话录下的 WAV 片段在本地的临时路径。用户最终点击“结束录音”时，后端会把同一次录音的所有片段合并成一个完整音频文件，再上传到对象存储并生成会议纪要。

如果不执行这次迁移，新版本后端启动后会因为读取/写入不存在的列而报错，`实时录音` 和 `自动生成纪要` 功能将无法正常工作。

## 适用场景

任何**已经存在旧数据库**的部署环境都需要执行迁移，包括但不限于：

- 甲方生产服务器
- 测试环境
- Gongwen 开发机（如果之前已经跑过后端并生成过 `volc_asr_sessions` 表）

如果是**全新安装**（数据库由 SQLAlchemy `create_all()` 首次创建），则不需要手动执行，因为新表结构已经包含这两个字段。

## 如何迁移

### 1. 确认数据库连接信息

后端使用的数据库连接通常在 `.env` 或环境变量中配置，例如：

```bash
DATABASE_URL=postgresql://user:password@host:5432/gongwen_db
```

### 2. 执行迁移 SQL

#### 方式 A：使用 psql 直连

```bash
cd /path/to/gongwen-rag-system
psql "$DATABASE_URL" -f migrations/001_add_recording_session_to_volc_asr_sessions.sql
```

#### 方式 B：Docker Compose 环境

如果你的 PostgreSQL 也跑在 Docker 里：

```bash
cd /path/to/gongwen-rag-system
# 进入数据库容器
docker compose exec postgres bash
# 在容器内执行
psql -U $POSTGRES_USER -d $POSTGRES_DB -f /docker-entrypoint-initdb.d/001_add_recording_session_to_volc_asr_sessions.sql
```

> 注意：你需要先把 SQL 文件挂载或复制到容器内可访问的位置。

#### 方式 C：手动执行

如果你只能通过数据库管理工具（如 pgAdmin、DBeaver、Navicat）操作，可以直接打开 `migrations/001_add_recording_session_to_volc_asr_sessions.sql`，把里面的 SQL 粘贴执行。

### 3. 验证迁移结果

执行完成后，确认两列已存在：

```sql
\d volc_asr_sessions
```

应该能看到：

```
recording_session_id | character varying(64)  |           |          |
audio_part_path      | character varying(512) |           |          |
```

## 迁移后需要做什么

1. 重新构建并启动后端容器：
   ```bash
   docker compose up -d --build
   ```
2. 重新构建并部署前端 `dist/`。
3. 测试：开始录音 → 暂停 10 秒以上（让 WebSocket 超时断连）→ 继续录音 → 结束录音，确认纪要生成正常且内容完整。

## 回滚方式

如果迁移后需要回滚，可以执行：

```sql
ALTER TABLE volc_asr_sessions
    DROP COLUMN IF EXISTS recording_session_id,
    DROP COLUMN IF EXISTS audio_part_path;
```

> 回滚前请确保已经把依赖这两个字段的后端代码也回退到旧版本。

## 兼容性说明

- 新增的列都是**可空**的，旧数据不会受影响。
- 旧版本前端如果不发送 `recording_session_id`，后端会自动走原来的单 session 逻辑，不会报错。
- 只有同时使用新版本前后端时，断连续传功能才会生效。
