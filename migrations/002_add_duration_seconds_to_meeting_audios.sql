-- 为 meeting_audios 表增加 duration_seconds 字段，用于记录音频时长（秒）
-- 修复 VolcMeetingAudio 访问 duration_seconds 时 AttributeError 的问题

ALTER TABLE meeting_audios
    ADD COLUMN IF NOT EXISTS duration_seconds FLOAT NULL;
