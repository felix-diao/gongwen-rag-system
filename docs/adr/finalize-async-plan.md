# 会议纪要 finalize-and-generate 异步化方案（修正版）

## 1. 背景与问题

当前移动端录音流程：

1. 用户在 `Record.tsx` 点击结束录音。
2. 前端等待 WebSocket `saving_audio` 事件（最多 10 秒）。
3. 前端 fire-and-forget 调用 `POST /api/meetings/minutes/volc/{meeting_id}/finalize-and-generate`。
4. 前端立刻跳转到 `/mobile/meetings`。

问题出在后端 `finalize-and-generate` 内部：

```
finalize-and-generate
  → finalize_recording_async
    → meeting_audio_service.create_audio_from_path
      → _MeetingTosUploader.upload_file  （同步上传 115 MB 到火山 TOS，约 3 分 20 秒）
    → submit_minutes
```

该 HTTP 请求被同步上传阻塞 3 分钟以上，极易因浏览器切后台、刷新页面、网络抖动、代理超时而中断，导致：

- `MeetingAudio` 可能已创建，但 `volc_minutes_jobs` 未创建。
- 用户看到会议永远在“生成中”，实际没有妙记任务在跑。

## 2. 目标

1. `finalize-and-generate` 接口响应时间 < 5 秒，无论音频多大。
2. 音频上传和妙记提交在后台独立完成，不依赖前端请求保持连接。
3. 不破坏现有 `finalize-recording` 端点行为。
4. 最小化代码改动，符合 AGENTS.md 中“最小代码 / 手术式修改”的要求。

## 3. 根因

- `meeting_audio_service.create_audio_from_path` 是同步上传。
- `finalize_recording_async` 直接调用同步方法。
- 同步上传阻塞 asyncio 事件循环，Uvicorn 无法处理其他协程。
- 客户端/代理等待 3 分 20 秒后断开连接，Uvicorn 标记该请求 task 为 cancelled。
- 取消信号被正在执行的同步上传“压住”，直到上传完成后才生效。
- 上传完成后，cancelled 状态立刻生效，`finalize_and_generate_async` 中上传之后的 `submit_minutes` 被跳过。
- 结果：`MeetingAudio` 已创建，`volc_minutes_jobs` 没有。

## 4. 方案

### 4.1 后端：让 `finalize-and-generate` 真异步

#### 4.1.1 `app/services/meeting_audio_service.py`

**改动类型**：新增方法，不修改现有方法。

新增 `create_audio_from_path_async`：

- 校验参数、配额、content_type（复用现有逻辑）。
- 预生成 `object_key`。
- 先创建 `MeetingAudio` 记录：
  - `status = 'uploading'`（必须显式赋值，数据库默认是 `uploaded`）
  - `file_url = NULL`
  - `object_key` 预生成
- `commit` 后立即返回该记录。
- 启动后台线程 `_run_upload_from_path`：
  - 在新 `SessionLocal` 中重新加载记录。
  - 调用 `_get_uploader().upload_file` 上传文件。
  - 成功：更新 `status='uploaded'`，设置 `file_url`，执行 `on_upload_complete(record)`。
  - 失败：更新 `status='failed'`，设置 `error_msg`。
  - 最后清理本地源文件（例如合并后的 WAV）。

**新增代码量**：约 90 行。

#### 4.1.2 `app/services/meeting_minute_volc_service.py`

**改动类型**：修改两个方法。

修改 `finalize_recording_async`：

- 新增参数 `auto_submit_minutes: bool = False`（默认 False，避免调用方误用）。
- 合并 WAV 后，不再调用同步 `create_audio_from_path`。
- 改为调用 `create_audio_from_path_async`：
  - 若 `auto_submit_minutes=True`，传入 `on_upload_complete` 回调。回调内新建 `SessionLocal`，先检查是否已有未失败/未取消的 `VolcMinutesJob`，没有则调用 `self.submit_minutes(meeting_id, audio.id)`；回调内捕获所有异常并记录日志。
  - 若 `auto_submit_minutes=False`，不传回调。
- 立即返回 `MeetingAudio` 记录（`status='uploading'`）。
- 保留原有回填 `VolcAsrSession.source_audio_id`、清理 part 文件等逻辑。
- **关键**：合并后的 WAV 文件不再在本方法内删除，由 `create_audio_from_path_async` 的后台上传线程在成功后删除。

修改 `finalize_and_generate_async`：

- 调用 `finalize_recording_async(..., auto_submit_minutes=True)`，确保上传完成后自动提交妙记。
- **删除**原有的同步 `submit_minutes` 调用。
- 若无可合并音频，同步返回 `failed_no_audio`。
- 检查是否已有未失败/未取消的 `VolcMinutesJob`：
  - 有：返回 `already_submitted`。
- 若音频状态为 `failed`（之前上传失败，源文件已不可用时）：
  - 返回 `failed_no_audio`，message 提示“录音上传失败，无法生成会议纪要”。
- 若音频已 `uploaded`（例如由之前的 `finalize-recording` 上传完成）：
  - 同步调用 `submit_minutes` 并返回 `submitted`。
- 若音频仍在 `uploading`：
  - 返回 `{status: 'accepted', audio_id: audio.id, job_id: None, job_status: None}`，由上传回调完成后续提交。

**修改代码量**：约 65 行（含回调函数）。

#### 4.1.3 `app/models/schemas.py`

**改动类型**：扩展枚举 + 加一个字段。

- `VolcFinalizeAndGenerateResponse.status` 的 `Literal` 增加 `"accepted"`。
- `VolcFinalizeRecordingResponse` 增加 `status: Optional[str] = None`。

**修改代码量**：约 5 行。

#### 4.1.4 `app/api/meeting_minute_volc.py`

**改动类型**：调整返回值与消息文案。

- `finalize_recording`：调用 `finalize_recording_async(..., auto_submit_minutes=False)`，返回 `{audio_id, file_url, status}`。
- `finalize_and_generate`：
  - 返回 `{status: 'accepted', ...}` 时，message 使用“录音正在上传，会议纪要将在上传完成后自动生成”。
  - 返回 `{status: 'failed_no_audio', ...}` 且音频状态为 `failed` 时，message 使用“录音上传失败，无法生成会议纪要”。

**修改代码量**：约 20 行。

### 4.2 前端

#### 4.2.1 `src/pages/Mobile/MeetingList.tsx`

本次先不改，保持现状。

#### 4.2.2 `src/pages/Mobile/Record.tsx`

**改动类型**：确认兼容，可能无需修改。

当前代码：

```ts
const resultStatus = finalRes?.data?.status;
if (resultStatus === 'failed_no_audio') {
  throw new Error('没有可用录音，无法生成会议纪要');
}
Toast.show({ icon: 'success', content: '会议纪要已开始生成' });
```

返回 `accepted` 时会走成功分支，无需修改。若希望更明确，可加上 `|| resultStatus === 'accepted'`。

**修改代码量**：0–2 行。

## 5. 兼容性说明

- `finalize-and-generate` 新增 `accepted` 状态，前端现有逻辑已兼容（只要不是 `failed_no_audio` 即显示成功）。
- `finalize-recording` 保持原有入口，响应变快并带上 `status='uploading'`，不影响调用方；调用方需按 `status` 判断是否可立即调用 `/generate`。
- `create_audio_from_path` 同步方法保留，其他模块调用不受影响。
- 旧客户端（无 `recording_session_id`）走 `LiveVolcAsrHandler._merge_and_upload_recording` 的同步链路，本次不做改造，行为不变。
- 数据库表结构不变。
- 后台线程使用新的 `SessionLocal`，不污染请求线程的 DB session。

## 6. 测试计划

### 6.1 回归测试脚本

编写一个可重复运行的测试脚本：

1. 创建测试会议。
2. 在本地生成一个模拟 WAV 片段，插入一条 `volc_asr_sessions` 记录（`status=completed`，带 `audio_part_path`）。
3. Mock `meeting_audio_service._MeetingTosUploader.upload_file` 为 sleep 10 秒（模拟慢上传）。
4. 调用 `POST /finalize-and-generate`：
   - 断言响应时间 < 5 秒。
   - 断言返回 `status='accepted'` 和 `audio_id`。
   - 断言 `meeting_audios.status='uploading'`。
5. 轮询数据库直到 `meeting_audios.status='uploaded'` 且 `volc_minutes_jobs` 存在对应记录。
6. 再次调用 `POST /finalize-and-generate`：
   - 断言返回 `status='already_submitted'`，不重复创建 job。
7. 额外断言：上传期间断开客户端连接，后台线程仍能完成上传并创建 `volc_minutes_jobs`。

### 6.2 手动验证

1. 实际手机录音 1 分钟以上，点结束。
2. 确认 5 秒内回到 Meetings 列表。
3. 确认后端日志中出现异步上传和妙记提交记录。
4. 确认 `volc_minutes_jobs` 有记录。

## 7. 风险与兜底

| 风险 | 影响 | 缓解措施 |
|---|---|---|
| 后台线程上传异常 | 音频状态停留在 uploading | 线程内 try/except，失败时更新 `status='failed'` 并记录 `error_msg` |
| 上传成功但 submit_minutes 失败 | 音频 uploaded，但无妙记任务 | 回调内 try/except 记录日志；下次 `finalize-and-generate` 若音频已 uploaded 会重新尝试提交 |
| 重复提交妙记（极小竞态窗口） | 同一音频产生两个 `volc_minutes_jobs` | 回调内先查询未失败/未取消的 job，存在则跳过；`finalize_and_generate_async` 同样先查询 |
| 后端重启导致后台任务中断 | 上传中断 | 重启后音频记录保留 uploading/failed 状态，前端可展示并允许重试 |
| `finalize_and_generate_async` 锁竞争 | 同一 recording_session 被并发收尾 | 保留现有 `asyncio.Lock` 机制 |
| 合并后 WAV 被提前删除 | 后台线程上传失败 | `finalize_recording_async` 不再删除合并 WAV，改由上传线程成功后删除 |
| 上传失败音频占用 quota | 后续同会议上传可能超限 | 与现有失败逻辑一致；如频繁出现可后续增加失败记录清理 |

## 8. 回滚方案

回滚时还原以下文件即可：

- `/root/workspace/rag/gongwen-rag-system/app/services/meeting_audio_service.py`
- `/root/workspace/rag/gongwen-rag-system/app/services/meeting_minute_volc_service.py`
- `/root/workspace/rag/gongwen-rag-system/app/models/schemas.py`
- `/root/workspace/rag/gongwen-rag-system/app/api/meeting_minute_volc.py`

## 9. 工作量估算

| 模块 | 文件 | 预估改动行数 |
|---|---|---|
| 后端 | `app/services/meeting_audio_service.py` | 新增 ~90 行 |
| 后端 | `app/services/meeting_minute_volc_service.py` | 修改 ~65 行 |
| 后端 | `app/models/schemas.py` | 修改 ~5 行 |
| 后端 | `app/api/meeting_minute_volc.py` | 修改 ~20 行 |
| 前端 | `src/pages/Mobile/Record.tsx` | 0–2 行 |
| 测试 | 新增回归测试脚本 | ~80 行 |
| **合计** | | **约 260 行** |

## 10. 验收标准

- [ ] `finalize-and-generate` 接口对 115 MB 音频的响应时间 < 5 秒。
- [ ] 前端点结束录音后 5 秒内回到 Meetings 列表。
- [ ] 后台上传完成后，`volc_minutes_jobs` 自动创建记录。
- [ ] 同一 `recording_session_id` 重复调用 `finalize-and-generate` 不会重复创建 `volc_minutes_jobs`。
- [ ] 现有 `finalize-recording` 端点行为不变（不自动提交妙记，返回 `status='uploading'`）。
- [ ] 未上传完成时，聚合接口 `GET /api/meetings/minutes/volc/{id}` 正确返回 `audio_status='uploading'`。
- [ ] 客户端断开连接后，后台上传和妙记提交仍能完成。
