

## 1. 会议管理流程 (Meeting Management)

**核心目标**：创建会议实体，并挂载相关的素材（文档、音频）。

### 流程步骤：

1.  **创建会议 (Create)**
    *   **场景**：用户点击“新建会议”。
    *   **前端动作**：调用 `POST /api/meetings/`。
    *   **数据**：提交会议标题、时间、地点、参会人等基本信息。
    *   **结果**：获得一个 `meeting_id`，后续所有操作都基于这个 ID。

2.  **上传会议材料 (Upload Files)**
    *   **场景**：用户上传会议相关的 PDF、Word 等参考文件。
    *   **前端动作**：调用 `POST /api/meetings/{meeting_id}/files`。
    *   **注意**：支持多文件上传，上传后文件会与该会议绑定。

3.  **录入会议音频 (Audio Input)**
    *   **场景 A（事后上传）**：会议结束后，用户上传录音笔或手机录制的音频文件。
        *   **前端动作**：调用 `POST /api/meetings/{meeting_id}/audio`。
    *   **场景 B（实时录音）**：会议进行中，通过网页实时录音。
        *   **前端动作**：连接 WebSocket `/ws/meetings/{meeting_id}/audio/stream` 推送音频流。

4.  **查看会议详情 (View)**
    *   **场景**：进入会议详情页。
    *   **前端动作**：调用 `GET /api/meetings/{meeting_id}`。
    *   **展示**：显示基本信息、已上传的文件列表、已上传的音频列表。

---

## 2. 会议纪要流程 (Meeting Minutes)

**核心目标**：利用 AI 基于会议素材生成结构化内容，并支持人工精修和导出。

### 流程步骤：

1.  **选择素材与生成 (Select & Generate)**
    *   **场景**：用户在会议详情页，点击“生成智能纪要”。
    *   **前端交互**：
        1.  弹窗或侧边栏让用户**勾选**参与生成的素材（哪些文件、哪些音频）。
        2.  用户确认后，前端调用 `POST /api/minutes/insights/generate/{meeting_id}`，带上 `file_ids` 和 `audio_ids`。
    *   **状态**：这是一个耗时操作，前端可能需要显示 Loading 状态。

2.  **展示结构化结果 (Display)**
    *   **场景**：生成完成后，或者用户再次进入纪要页面。
    *   **前端动作**：调用 `GET /api/minutes/insights/{meeting_id}`。
    *   **展示**：页面分为三个区域：
        *   **会议摘要 (Summary)**：一段纯文本总结。
        *   **行动项 (Action Items)**：一个任务列表（谁，什么时候，做什么）。
        *   **决策项 (Decision Items)**：一个结论列表。

3.  **人工精修 (Human-in-the-loop Editing)**
    *   **场景**：AI 生成的内容可能不完美，用户需要修改。
    *   **摘要修改**：
        *   用户编辑文本框 -> 点击保存 -> 调用 `PUT .../summary`。
    *   **行动项/决策项管理**：
        *   **新增**：点击“添加行动项” -> 填写表单 -> 调用 `POST .../actions`。
        *   **修改**：点击某行编辑 -> 修改状态或内容 -> 调用 `PUT .../actions/{id}`。
        *   **删除**：点击删除图标 -> 调用 `DELETE .../actions/{id}`。

4.  **导出文档 (Export)**
    *   **场景**：纪要整理完毕，需要发邮件或归档。
    *   **前端动作**：用户点击“导出 Word”，调用 `GET /api/minutes/insights/export/docx/{meeting_id}`。
    *   **结果**：浏览器触发下载 `.docx` 文件。

---

