-- 撤销 migrate_meeting_audios_legacy_to_unified.sql 对 meeting_audios 的补充列与索引，
-- 恢复为仅含 filename / file_path / uploaded_at 等旧版列的结构。

DROP INDEX IF EXISTS ix_meeting_audios_provider;

ALTER TABLE meeting_audios DROP COLUMN IF EXISTS provider;
ALTER TABLE meeting_audios DROP COLUMN IF EXISTS creator_id;
ALTER TABLE meeting_audios DROP COLUMN IF EXISTS file_name;
ALTER TABLE meeting_audios DROP COLUMN IF EXISTS object_key;
ALTER TABLE meeting_audios DROP COLUMN IF EXISTS file_url;
ALTER TABLE meeting_audios DROP COLUMN IF EXISTS created_at;
