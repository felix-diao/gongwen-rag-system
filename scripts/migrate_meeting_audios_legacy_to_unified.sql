-- 将旧版 meeting_audios（filename / file_path / uploaded_at 等）补齐为 ORM 统一结构所需列。
-- 幂等：可重复执行（依赖 IF NOT EXISTS / 条件 UPDATE）。

ALTER TABLE meeting_audios ADD COLUMN IF NOT EXISTS provider VARCHAR(16);
UPDATE meeting_audios SET provider = 'volc' WHERE provider IS NULL OR provider = '';
ALTER TABLE meeting_audios ALTER COLUMN provider SET DEFAULT 'volc';
ALTER TABLE meeting_audios ALTER COLUMN provider SET NOT NULL;

ALTER TABLE meeting_audios ADD COLUMN IF NOT EXISTS creator_id VARCHAR(64);

ALTER TABLE meeting_audios ADD COLUMN IF NOT EXISTS file_name VARCHAR(255);
UPDATE meeting_audios SET file_name = filename WHERE file_name IS NULL AND filename IS NOT NULL;

ALTER TABLE meeting_audios ADD COLUMN IF NOT EXISTS object_key VARCHAR(512);
UPDATE meeting_audios
SET object_key = file_path
WHERE object_key IS NULL
  AND file_path IS NOT NULL
  AND file_path NOT LIKE 'http%'
  AND file_path NOT LIKE 'https%';

ALTER TABLE meeting_audios ADD COLUMN IF NOT EXISTS file_url TEXT;
UPDATE meeting_audios
SET file_url = file_path
WHERE file_url IS NULL
  AND file_path IS NOT NULL
  AND (file_path LIKE 'http%' OR file_path LIKE 'https%');

ALTER TABLE meeting_audios ADD COLUMN IF NOT EXISTS created_at TIMESTAMP WITHOUT TIME ZONE;
UPDATE meeting_audios
SET created_at = COALESCE(created_at, uploaded_at, updated_at, NOW())
WHERE created_at IS NULL;

UPDATE meeting_audios
SET updated_at = COALESCE(updated_at, created_at, NOW())
WHERE updated_at IS NULL;

CREATE INDEX IF NOT EXISTS ix_meeting_audios_provider ON meeting_audios (provider);
