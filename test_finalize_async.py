"""finalize-and-generate 异步化回归测试。

运行方式（在 Gongwen 后端目录）：
    source .venv/bin/activate
    python test_finalize_async.py

注意：会在真实数据库中创建测试会议并在结束后清理。
"""

import os
import sys
import time
import wave
import threading
from pathlib import Path

sys.path.insert(0, "/root/workspace/rag/gongwen-rag-system")

os.environ.setdefault("ENV", "development")

import asyncio
from app.models import database
from app.services import meeting_audio_service as meeting_audio_module
from app.services.meeting_minute_volc_service import volc_meeting_minute_service


# 模拟慢上传：sleep 3 秒后返回假 URL
_orig_upload_file = None
_orig_minutes_submit = None
_orig_start_poller = None
_upload_lock = threading.Event()


def _mock_upload_file(self, source: Path, object_key: str, content_type):
    print(f"[mock] 开始上传 {source} -> {object_key}")
    time.sleep(3)
    print(f"[mock] 上传完成 {object_key}")
    _upload_lock.set()
    return f"https://mock-tos.example.com/{object_key}"


def _mock_minutes_submit(file_url: str, file_type: str) -> str:
    print(f"[mock] 提交妙记任务 file_url={file_url}")
    return f"mock-task-{int(time.time() * 1000)}"


def _mock_start_poller(job_id: int) -> None:
    print(f"[mock] 跳过启动轮询器 job_id={job_id}")


def _make_test_wav(path: Path, duration_seconds: int = 2) -> None:
    """生成测试 WAV 文件。"""
    sample_rate = 16000
    num_frames = sample_rate * duration_seconds
    path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(sample_rate)
        wf.writeframes(b"\x00\x00" * num_frames)


def main():
    global _orig_upload_file, _orig_minutes_submit, _orig_start_poller

    db = database.SessionLocal()
    meeting_id = None
    try:
        # 1. 创建测试会议（使用 raw SQL 避免本地模型与部署库不一致）
        result = db.execute(
            database.text(
                "INSERT INTO meetings (title, creator_id, status, created_at, updated_at) "
                "VALUES (:title, :creator_id, 'created', NOW(), NOW()) RETURNING id"
            ),
            {"title": "异步化回归测试会议", "creator_id": "test_user"},
        )
        meeting_id = result.scalar_one()
        db.commit()
        print(f"[test] 创建测试会议 meeting_id={meeting_id}")

        # 2. 创建测试 WAV 片段
        part_path = Path(f"/tmp/test_finalize_async_part_{meeting_id}.wav")
        _make_test_wav(part_path, duration_seconds=1)

        session = database.VolcAsrSession(
            meeting_id=meeting_id,
            recording_session_id=f"test_session_{meeting_id}",
            status="completed",
            audio_part_path=str(part_path),
            duration_seconds=1.0,
        )
        db.add(session)
        db.commit()
        print(f"[test] 创建 ASR session session_id={session.id}")

        # 3. Mock 上传、妙记提交和轮询器
        uploader_cls = meeting_audio_module._MeetingTosUploader
        _orig_upload_file = uploader_cls.upload_file
        uploader_cls.upload_file = _mock_upload_file
        if meeting_audio_module.meeting_audio_service._uploader is not None:
            meeting_audio_module.meeting_audio_service._uploader.upload_file = _mock_upload_file

        _orig_minutes_submit = volc_meeting_minute_service._minutes_api.submit
        volc_meeting_minute_service._minutes_api.submit = _mock_minutes_submit

        _orig_start_poller = volc_meeting_minute_service._start_poller
        volc_meeting_minute_service._start_poller = _mock_start_poller

        _upload_lock.clear()

        # 4. 调用 finalize-and-generate
        start = time.time()
        result = asyncio.run(
            volc_meeting_minute_service.finalize_and_generate_async(
                db=db,
                meeting_id=meeting_id,
                recording_session_id=session.recording_session_id,
            )
        )
        elapsed = time.time() - start
        print(f"[test] 第一次调用结果: {result}, 耗时: {elapsed:.2f}s")

        assert result["status"] == "accepted", f"期望 accepted，实际 {result['status']}"
        assert result["audio_id"] is not None
        assert elapsed < 5.0, f"响应时间 {elapsed:.2f}s 超过 5 秒"

        audio_id = result["audio_id"]

        # 5. 断言音频状态为 uploading
        audio = (
            db.query(database.MeetingAudio)
            .filter(database.MeetingAudio.id == audio_id)
            .first()
        )
        assert audio is not None
        assert audio.status == "uploading", f"期望 uploading，实际 {audio.status}"
        print(f"[test] 音频状态: {audio.status}")

        # 6. 等待上传线程完成
        print("[test] 等待后台上传完成...")
        _upload_lock.wait(timeout=10)
        time.sleep(0.5)  # 给回调留点时间

        # 7. 断言音频 uploaded 且 job 已创建
        db.expire_all()
        audio = (
            db.query(database.MeetingAudio)
            .filter(database.MeetingAudio.id == audio_id)
            .first()
        )
        print(f"[test] 上传后音频状态: {audio.status}, file_url={audio.file_url}")
        assert audio.status == "uploaded", f"期望 uploaded，实际 {audio.status}"
        assert audio.file_url is not None

        job = (
            db.query(database.VolcMinutesJob)
            .filter(database.VolcMinutesJob.source_audio_id == audio_id)
            .first()
        )
        assert job is not None, "volc_minutes_jobs 未创建"
        print(f"[test] 妙记任务已创建 job_id={job.id}, status={job.status}")

        # 8. 第二次调用，应返回 already_submitted
        result2 = asyncio.run(
            volc_meeting_minute_service.finalize_and_generate_async(
                db=db,
                meeting_id=meeting_id,
                recording_session_id=session.recording_session_id,
            )
        )
        print(f"[test] 第二次调用结果: {result2}")
        assert result2["status"] == "already_submitted", f"期望 already_submitted，实际 {result2['status']}"

        print("\n[test] 所有断言通过 ✅")

    finally:
        # 9. 清理 mock
        if _orig_upload_file:
            uploader_cls = meeting_audio_module._MeetingTosUploader
            uploader_cls.upload_file = _orig_upload_file
            if meeting_audio_module.meeting_audio_service._uploader is not None:
                meeting_audio_module.meeting_audio_service._uploader.upload_file = _orig_upload_file
        if _orig_minutes_submit:
            volc_meeting_minute_service._minutes_api.submit = _orig_minutes_submit
        if _orig_start_poller:
            volc_meeting_minute_service._start_poller = _orig_start_poller

        # 10. 清理测试数据
        if meeting_id:
            try:
                db.execute(
                    database.text("DELETE FROM volc_minutes_jobs WHERE meeting_id = :mid"),
                    {"mid": meeting_id},
                )
                db.execute(
                    database.text("DELETE FROM meeting_audios WHERE meeting_id = :mid"),
                    {"mid": meeting_id},
                )
                db.execute(
                    database.text("DELETE FROM volc_asr_sessions WHERE meeting_id = :mid"),
                    {"mid": meeting_id},
                )
                db.execute(
                    database.text("DELETE FROM meetings WHERE id = :mid"),
                    {"mid": meeting_id},
                )
                db.commit()
                print(f"[test] 清理测试数据 meeting_id={meeting_id}")
            except Exception as exc:
                print(f"[test] 清理数据失败: {exc}")
        db.close()


if __name__ == "__main__":
    main()
