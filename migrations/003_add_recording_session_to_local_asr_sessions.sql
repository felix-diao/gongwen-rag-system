-- 为机密会议本地实时 ASR 增加断连续传分组和音频片段路径
ALTER TABLE local_asr_sessions
    ADD COLUMN IF NOT EXISTS recording_session_id VARCHAR(64),
    ADD COLUMN IF NOT EXISTS audio_part_path VARCHAR(512);

CREATE INDEX IF NOT EXISTS idx_local_asr_sessions_recording_session_id
    ON local_asr_sessions (recording_session_id);