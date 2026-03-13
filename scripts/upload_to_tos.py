#!/usr/bin/env python3
"""
命令行上传本地文件到 TOS，与项目内上传方式完全一致（同一套 uploader + object_key 规则），用于测速。
请使用项目虚拟环境运行，否则会缺依赖（如 sqlalchemy）：
  cd /root/workspace/rag/gongwen-rag-system
  .venv/bin/python scripts/upload_to_tos.py 长录音.m4a 3
  或: source .venv/bin/activate && python scripts/upload_to_tos.py 长录音.m4a 3
用法:
  python scripts/upload_to_tos.py <本地文件> [meeting_id]
  例: python scripts/upload_to_tos.py 长录音.m4a 3
      上传到 bucket 下 meetings/3/{uuid}.m4a（与项目内 3 号会议上传规则一致）
"""
import os
import sys
import time

# 确保项目根在 path 中并加载 env
_project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _project_root not in sys.path:
    sys.path.insert(0, _project_root)
os.chdir(_project_root)

from dotenv import load_dotenv
load_dotenv(os.path.join(_project_root, ".env"))

from pathlib import Path

from app.services.volc_minutes_service import volc_minutes_service


def main():
    if len(sys.argv) < 2:
        print("用法: python scripts/upload_to_tos.py <本地文件> [meeting_id，默认 3]")
        sys.exit(1)
    local_path = Path(os.path.abspath(sys.argv[1]))
    meeting_id = int(sys.argv[2]) if len(sys.argv) > 2 else 3

    if not local_path.is_file():
        print(f"文件不存在: {local_path}")
        sys.exit(2)

    size = local_path.stat().st_size
    original_name = local_path.name
    # 与项目内 _upload_to_tos 一致：同一 uploader、同一 _build_object_key
    object_key = volc_minutes_service._build_object_key(meeting_id, original_name)
    content_type = "audio/mp4" if original_name.lower().endswith(".m4a") else None

    print(f"上传: {local_path} -> s3://{volc_minutes_service._ensure_uploader()._bucket}/{object_key} ({size / (1024*1024):.2f} MB)")
    print("使用项目内同一套 TOS 上传逻辑（upload_file）...")
    t0 = time.perf_counter()
    uploader = volc_minutes_service._ensure_uploader()
    file_url = uploader.upload_file(local_path, object_key, content_type)
    elapsed = time.perf_counter() - t0

    print(f"耗时: {elapsed:.2f} s")
    if size and elapsed > 0:
        print(f"速率: {size / (1024*1024) / elapsed:.2f} MB/s")
    print(f"对象键: {object_key}")
    print(f"访问 URL: {file_url}")


if __name__ == "__main__":
    main()
