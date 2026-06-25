-- 为实时 ASR 断连续传增加 recording_session_id 和音频片段路径
ALTER TABLE volc_asr_sessions
    ADD COLUMN IF NOT EXISTS recording_session_id VARCHAR(64),
    ADD COLUMN IF NOT EXISTS audio_part_path VARCHAR(512);

CREATE INDEX IF NOT EXISTS idx_volc_asr_sessions_recording_session_id
    ON volc_asr_sessions (recording_session_id);
