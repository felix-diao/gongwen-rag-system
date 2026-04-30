# 会议纪要相关后端 HTTP/WebSocket 接口说明

> 基准代码：`gongwen-rag-system`（FastAPI）  
> 统一前缀：业务接口在 **`/api/meetings`** 下；响应多为 `{ "success": true, "data": ..., "message": "..." }`（`StandardResponse`）。  
> 鉴权：除特别说明外，HTTP 需 **Bearer Token**；WebSocket 多通过 **Query `token=`** 传 JWT。

下文 **优先级** 含义（便于排障与压测排序）：

| 级别 | 含义 |
|------|------|
| **P0** | 核心路径：选会、看纪要、拉会话列表、音频主链路；挂了主流程不可用 |
| **P1** | 高频或强交互：实时录音 WS、提交生成/妙记、取消、会话详情与编辑 |
| **P2** | 辅助：上传任务轮询、直链下载、会议广播 WS 等 |

---

## 1. 会议主资源（`meeting.py`）

前缀：**`/api/meetings`**

| 方法 | 路径 | 说明 | 优先级 |
|------|------|------|--------|
| `GET` | `/api/meetings` | 当前用户创建的会议列表 | **P0** |
| `POST` | `/api/meetings` | 创建会议 | **P0** |
| `GET` | `/api/meetings/{meeting_id}` | 会议详情 | **P0** |
| `PUT` | `/api/meetings/{meeting_id}` | 更新会议 | **P1** |
| `DELETE` | `/api/meetings/{meeting_id}` | 删除会议（级联清理本地/火山纪要、音频等） | **P1** |

---

## 2. 会议音频（`meeting_audio.py`）

前缀：**`/api/meetings`**（与会议主资源共用前缀，注意路径以 `/audio/` 区分）

| 方法 | 路径 | 说明 | 优先级 |
|------|------|------|--------|
| `POST` | `/api/meetings/audio/{meeting_id}/upload-task` | 创建异步上传任务；Query：`provider=local|volc`，Body：multipart `file` | **P0** |
| `GET` | `/api/meetings/audio/upload-tasks/{task_id}` | 查询上传任务状态（轮询） | **P2** |
| `GET` | `/api/meetings/audio/{meeting_id}` | 某会议下音频列表；Query：`provider=local|volc` | **P0** |
| `GET` | `/api/meetings/audio/{meeting_id}/{audio_id}` | 单条音频元数据；Query：`provider` | **P1** |
| `GET` | `/api/meetings/audio/download/{meeting_id}/{audio_id}` | 下载音频（附件流）；Query：`provider` | **P1** |
| `GET` | `/api/meetings/audio/direct-download/{meeting_id}/{audio_id}` | 机密本地直链/流式下载；Query：`provider`（当前仅 `local`）、`token` 可选 | **P2** |
| `DELETE` | `/api/meetings/audio/{meeting_id}/{audio_id}` | 删除音频；Query：`provider` | **P1** |
| **WebSocket** | `/api/meetings/audio/ws/{meeting_id}` | 会议维度事件广播（纪要状态等推送）；**无 token 在部分部署需经反向代理** | **P1** |

---

## 3. 机密会议（本地）纪要 — `local`（`meeting_minute_local.py`）

前缀：**`/api/meetings/minutes/local`**

| 方法 | 路径 | 说明 | 优先级 |
|------|------|------|--------|
| **WebSocket** | `/api/meetings/minutes/local/{meeting_id}/live` | 在线录音 + 流式转写；Query：`token`（JWT） | **P0** |
| `GET` | `/api/meetings/minutes/local/{meeting_id}` | 当前聚合视图：流式稿、摘要、待办、ASR 状态等 | **P0** |
| `POST` | `/api/meetings/minutes/local/{meeting_id}/generate` | 根据当前转写生成摘要/待办并落历史快照；Query 可选：`asr_session_id` | **P0** |
| `POST` | `/api/meetings/minutes/local/{meeting_id}/transcribe-audio` | 对已上传本地音频提交分段转写；Query：`audio_id` | **P0** |
| `POST` | `/api/meetings/minutes/local/{meeting_id}/cancel` | 取消本地处理（转写/生成）；Body 可选：`asr_session_id`、`reason` 等 | **P1** |
| `GET` | `/api/meetings/minutes/local/{meeting_id}/sessions` | **本地会话历史**列表 | **P0** |
| `GET` | `/api/meetings/minutes/local/{meeting_id}/sessions/{session_id}` | 本地会话历史详情 | **P1** |
| `PUT` | `/api/meetings/minutes/local/{meeting_id}/sessions/{session_id}` | 更新会话快照（会话历史内编辑保存） | **P1** |
| `DELETE` | `/api/meetings/minutes/local/{meeting_id}/sessions/{session_id}` | 删除一条会话历史 | **P2** |

---

## 4. 普通会议（火山）纪要 — `volc`（`meeting_minute_volc.py`）

前缀：**`/api/meetings/minutes/volc`**

| 方法 | 路径 | 说明 | 优先级 |
|------|------|------|--------|
| **WebSocket** | `/api/meetings/minutes/volc/{meeting_id}/live` | 在线录音 + 火山实时 ASR；Query：`token` | **P0** |
| `GET` | `/api/meetings/minutes/volc/{meeting_id}` | 当前聚合视图：流式稿、精准转写、说话人分段、摘要、待办、妙记任务状态 | **P0** |
| `POST` | `/api/meetings/minutes/volc/{meeting_id}/submit` | 提交妙记离线任务；Query：`audio_id` | **P0** |
| `POST` | `/api/meetings/minutes/volc/{meeting_id}/cancel` | 取消当前妙记任务；Body 可选 `job_id`、`reason` | **P1** |
| `POST` | `/api/meetings/minutes/volc/{meeting_id}/jobs/{job_id}/cancel` | 按 `job_id` 取消指定任务 | **P1** |
| `GET` | `/api/meetings/minutes/volc/{meeting_id}/sessions` | **火山会话历史**列表 | **P0** |
| `GET` | `/api/meetings/minutes/volc/{meeting_id}/sessions/{session_id}` | 火山会话历史详情 | **P1** |
| `PUT` | `/api/meetings/minutes/volc/{meeting_id}/sessions/{session_id}` | 更新会话快照 | **P1** |
| `DELETE` | `/api/meetings/minutes/volc/{meeting_id}/sessions/{session_id}` | 删除一条会话历史 | **P2** |

---


