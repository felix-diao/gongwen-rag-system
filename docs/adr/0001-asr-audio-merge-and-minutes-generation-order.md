# ADR-0001: 实时 ASR 音频合并与纪要生成顺序

## 状态

审议中（Draft）

## 背景

项目使用火山引擎实时语音转写（WebSocket ASR）进行会议录音。一次会议可能产生多个 ASR 会话：

- 用户主动暂停后继续录音
- 火山服务端超时（如 `code=45000081 Timeout waiting next packet`）导致前端重连
- 网络抖动导致断连重连

为支持断连续传，系统引入了 `recording_session_id`：同一会议、同一次连续录音的多个 ASR 会话共享同一个 `recording_session_id`。每个 ASR 会话结束时会把 PCM 片段保存为 WAV 文件（`audio_part_path`）。

当前问题（以会议 404 为例）：

| 音频 id | 文件名 | 类型 | 创建时间 |
|---|---|---|---|
| 337 | `...part_263.wav` | ASR 263 片段 | 12:39:05 |
| 338 | `recording_a2da....wav` | 合并音频 | 12:41:03 |
| 339 | `...part_264.wav` | ASR 264 片段 | 12:41:07 |

ASR 会话：

| ASR id | source_audio_id | 状态 | 更新时间 | 错误信息 |
|---|---|---|---|---|
| 263 | 338 | failed | 12:41:03 | 火山超时 |
| 264 | 339 | failed | 12:41:07 | `'VolcMeetingAudio' object has no attribute 'duration_seconds'` |

妙记任务基于音频 339（片段）生成，而不是基于合并后的完整音频 338。

## 问题根因

### 根因 1：合并时机过早

当前 `_finalize()` 只在**当前 ASR 会话显式 stop** 时合并一次。如果用户点击"结束录音"时还有其他 ASR 会话尚未结束（如正在重连的 session 264），这些后续产生的片段不会被包含在合并结果中。

时间线：

```
12:39:05  session 263 开始，片段保存为 337
12:41:03  用户点击"结束录音"，session 263 触发合并 → 生成 338
12:41:07  session 264 失败，片段保存为 339  ← 未被合并进 338
```

### 根因 2：`generate` 取"最新音频"

`generate_minutes()` 使用 `_latest_volc_audio()` 按 `updated_at` 取会议下最新的音频。由于 339 创建时间晚于 338，它被选中作为生成纪要的输入。

### 根因 3：`VolcMeetingAudio` 缺少 `duration_seconds`

`_finalize()` 合并完成后访问 `audio_record.duration_seconds`，但 `VolcMeetingAudio` 模型没有该字段，导致抛 `AttributeError`，session 被标记为 `failed`。

### 根因 4：调用顺序不确定

前端 `stopRecording()` 流程：

```tsx
wsRef.current.send(JSON.stringify({ action: 'stop' }));
wsRef.current.close();

void generateMinutesAfterRecording().catch(...);
history.push('/mobile/meetings');
```

发送 `stop` 后立即关闭 WebSocket 并调用 `/generate`。后端 `_finalize()` 是异步执行的，`/generate` 可能在合并完成之前就被调用，导致取到不完整的音频。

## 待决策事项

需要选择一种方案，确保：

1. 用户点击"结束录音"后，生成纪要基于完整的合并音频。
2. 调用顺序确定。
3. 实现复杂度可控。

## 候选方案

### 方案 2：在 `generate` 接口内部兜底合并

#### 思路

`/generate` 被调用时，先检查会议下是否存在未合并的 part 片段。如果有，先执行合并，再基于合并后的音频生成纪要。

#### 需要修改的代码

1. **`app/services/meeting_minute_volc_service.py`**
   - 新增方法 `_ensure_merged_audio(db, meeting_id, recording_session_id)`：
     - 查询该 `recording_session_id` 下所有带 `audio_part_path` 的 ASR 会话。
     - 如果有且没有对应的 merged 音频，则调用合并逻辑。
     - 返回 merged `MeetingAudio`。
   - 修改 `generate_minutes()`（或 `submit_minutes` 的调用方）：
     - 获取 `recording_session_id`（从最新 ASR 会话或请求参数）。
     - 调用 `_ensure_merged_audio()` 确保合并完成。
     - 使用 merged 音频的 `id` 调用 `submit_minutes()`。

2. **`app/api/meeting_minute_volc.py`**
   - `generate_minutes()` 接口增加可选参数 `recording_session_id` 或 `audio_id`。
   - 在调用 `submit_minutes` 前执行合并检查。

3. **前端**
   - 改动最小，保持当前 `stop → generate` 调用顺序。

#### 优点

- 前端改动最小。
- 向后兼容：直接调用 `/generate` 也能得到正确结果。

#### 缺点

- `/generate` 接口变重：同步执行 WAV 合并 + TOS 上传，可能超时。
- 职责不清晰：`generate` 接口做了"合并音频"的事情。
- 如果合并失败，生成纪要的接口也会失败，错误信息混淆。
- 如果多个 ASR 会话同时处于 `processing` 状态，`generate` 无法确定是否要等。

#### 深入考察

**1. 并发竞争条件**

前端当前代码发送 `stop` 后立即关闭 WebSocket 并调用 `/generate`。后端 `_finalize()` 与 `/generate` 是并行执行的：

```
前端 send stop ──┬──→ 后端 _finalize() 保存片段、合并
                 │
                 └──→ 前端调用 /generate ──→ 后端 _ensure_merged_audio() 查询
```

如果 `/generate` 执行时 `_finalize()` 还没保存完片段，`_ensure_merged_audio()` 可能拿到旧的 merged 音频或更旧的 part 片段。

解决方案：
- 在 `_ensure_merged_audio()` 中轮询等待最新 ASR session 进入 `completed`/`failed` 状态。
- 或者给合并过程加分布式锁/数据库行锁。

两者都会增加复杂度。

**2. 超时风险**

`/generate` 当前是同步 HTTP 接口。如果在接口内部执行 WAV 合并 + TOS 上传，对于长录音（几十分钟）可能耗时数秒到十几秒，容易触发 Nginx/Gateway 超时（默认 60s）。

**3. `creator_id` 推断**

合并时需要创建 `MeetingAudio`，需要 `creator_id`。`generate_minutes` 接口只知道 `meeting_id` 和当前用户，不一定知道原始录音的 `creator_id`。如果会议有多个参与者，可能取错 creator。

当前 `_merge_and_upload_recording()` 使用 `self._creator_id`（即创建 ASR session 的用户）。方案 2 中需要从 ASR session 或 meeting 推断。

**4. 重复合并**

每次 `/generate` 被调用都要检查是否需要重新合并。如果用户多次点击"生成纪要"，会重复读取 WAV 文件、重复上传 TOS。

可以通过"最新 part 更新时间 > merged 更新时间"来避免不必要的合并，但这增加了状态判断的复杂度。

**5. 是否真的解决会议 404 的问题？**

会议 404 的核心问题是：session 264 的片段 339 在 merged 338 之后产生。方案 2 中，如果 `/generate` 调用时 session 264 还没进入 `completed`/`failed`，`_ensure_merged_audio()` 仍然拿不到 339。

所以方案 2 必须加入"等待 processing session 结束"的逻辑。一旦加入等待，就和方案 3 的复杂度接近，但接口职责更不清晰。

### 方案 3：新增独立的 `finalize-recording` 接口

#### 思路

把"结束录音"、"合并音频"、"生成纪要"拆成三个明确步骤：

1. `stop`：结束当前 ASR WebSocket 会话，保存最后片段。
2. `finalize-recording`：等待同 `recording_session_id` 下所有 ASR 会话结束，合并所有片段，生成/更新 merged 音频。
3. `generate`：基于 merged 音频生成纪要。

前端在用户点击"结束录音"后自动串行调用：

```
stop → finalize-recording → generate
```

用户感知上仍是一键结束。

#### 需要修改的代码

1. **`app/services/meeting_minute_volc_service.py`**
   - 新增方法 `finalize_recording(db, meeting_id, recording_session_id)`：
     - 查询同 `recording_session_id` 下所有 ASR 会话。
     - 等待 `processing` 状态的会话结束（设置超时，如 10 秒）。
     - 收集所有带 `audio_part_path` 的会话。
     - 调用 `_merge_and_upload_recording()` 合并片段。
     - 清理旧 merged 音频记录（可选）。
     - 返回 merged `MeetingAudio`。
   - 修改 `_merge_and_upload_recording()`：
     - 合并条件包含 `failed` 状态的会话（已完成）。
     - 设置 `audio_record.duration_seconds`（已完成）。
   - 修改 `_finalize()`：
     - 非显式 stop 时只保存片段，不合并（已完成）。
     - 显式 stop 时也可以选择不合并，只保存片段，由 `finalize-recording` 统一合并。

2. **`app/api/meeting_minute_volc.py`**
   - 新增接口 `POST /api/meetings/{meeting_id}/volc/finalize-recording`：
     - 请求体：`{"recording_session_id": "..."}` 或从最新 ASR 会话自动推断。
     - 调用 `volc_meeting_minute_service.finalize_recording(...)`。
     - 返回 merged 音频信息。

3. **`app/models/database.py` + migration**
   - 给 `MeetingAudio` 增加 `duration_seconds` 字段（已完成，migration 002）。

4. **前端 `Record.tsx`**
   - 修改 `stopRecording()`：
     - 发送 `stop` 后等待 WebSocket 返回 `completed` 或 `session_saved`。
     - 调用 `POST /api/meetings/{meeting_id}/volc/finalize-recording`。
     - 等待 finalize 返回 merged audio_id。
     - 调用 `POST /generate?audio_id={merged_audio_id}`。
     - 完成后跳转会议列表。

5. **前端 `MeetingDetail.tsx`**
   - 如果用户在会议详情页手动点击"生成纪要"，也需要先调用 `finalize-recording`（或后端 `generate` 接口内部兼容兜底）。

#### 优点

- 职责清晰：stop 只结束会话，finalize 只合并音频，generate 只生成纪要。
- 顺序确定：前端串行调用，确保 generate 基于完整音频。
- 能解决 session 264 在 338 之后才结束的问题：finalize 会等待所有 processing 会话。
- part 片段不创建独立 `MeetingAudio`，不会被 `_latest_volc_audio` 误选。
- 合并失败和生成失败可以分开处理，错误信息更清晰。

#### 缺点

- 前端改动较大：需要新增 finalize 调用和等待逻辑。
- 用户点击"结束"后需要多等一个 finalize 耗时（通常秒级）。
- 如果 ASR 会话长时间处于 `processing`（如 WebSocket 未正常关闭），finalize 可能超时。
- 旧的 part 片段和 merged 音频（如 337、338、339）需要单独清理。

#### 深入考察

**1. WebSocket 关闭时机**

当前前端发送 `stop` 后立即 `wsRef.current.close()`。后端 `_finalize()` 完成后会发送 `completed` 消息，但 WebSocket 可能已经关闭，前端收不到。

方案 3 需要前端改为：

```tsx
wsRef.current.send(JSON.stringify({ action: 'stop' }));
// 等待后端返回 completed/session_saved 后再 close
await waitForWsMessage(['completed', 'session_saved', 'error']);
wsRef.current.close();
```

然后调用 `finalize-recording`。

**2. 单段录音处理**

如果只有一次 ASR 会话且成功结束，`finalize-recording` 会找到 1 个 part 片段。此时没有真正的"合并"，只需要把单个 WAV 文件上传为 merged 音频。

实现方式：
- 如果 `len(part_paths) == 1`，直接以该文件作为 merged 音频上传，避免无意义的合并操作。
- 或者复用 `_merge_wav_files`，单个文件合并后结果相同。

**3. 多 `recording_session_id` 情况**

一次会议可以有多次独立录音（用户删除后重新录）。`finalize-recording` 必须接收 `recording_session_id` 参数，只合并对应的那一次录音。

如果没有 `recording_session_id`（旧数据），退回到 `_latest_volc_audio` 老逻辑。

**4. 等待 processing session 的实现**

`finalize-recording` 需要等待同 `recording_session_id` 下所有 `processing` 的 session 结束。实现方式：

```python
async def finalize_recording(...):
    for _ in range(max_wait_seconds):
        processing = query(...status == "processing"...).all()
        if not processing:
            break
        await asyncio.sleep(1)
    
    # 超时后仍然继续合并已完成的片段
    sessions = query(...status in [completed, failed]..., audio_part_path != None).all()
    ...
```

超时策略：
- 建议最多等 10 秒。
- 超时后提示用户"部分录音可能未保存"，但仍合并已完成的片段。

**5. 幂等性**

同一个 `recording_session_id` 多次调用 `finalize-recording` 应该返回同一个 merged 音频（如果片段没有变化）。

实现方式：
- 检查是否存在已生成的 merged 音频且更新时间晚于所有 part 片段。
- 如果是，直接返回，不再合并。

**6. 对现有数据的影响**

方案 3 不修改旧数据。会议 404 中已存在的 337/338/339 会保留。新录音走新流程，不会再产生类似问题。

是否需要数据修复脚本：可选。如果不清理，会议列表里可能同时存在旧 merged 和旧 part 音频，但新流程只使用新 merged 音频。

## 推荐

推荐 **方案 3**。

理由：

1. 方案 3 从根本上解决了"调用顺序不确定"和"合并时机过早"两个问题。
2. 职责拆分后，每个接口只做一件事，便于后续维护。
3. 虽然前端改动较大，但用户交互仍是一键式，只是前端内部多了一步自动调用。
4. 方案 2 把合并逻辑塞进 `generate` 接口，会导致接口变重、职责混乱，且无法优雅处理"还有 processing 中的 session"的情况。

## 遗留问题

1. **旧数据清理**：会议 404 这种已存在的 part 片段（337、339）和旧 merged（338）不会自动消失。需要写一个 one-off 脚本或数据修复任务来清理/重新合并。
2. **超时策略**：`finalize-recording` 等待 processing session 的超时时间需要根据实际情况调整（建议 10 秒）。
3. **取消录音**：是否需要支持用户取消当前录音而不合并？如果需要，应新增 `cancel-recording` 接口。
