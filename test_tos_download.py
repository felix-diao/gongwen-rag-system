
import os
from pathlib import Path

# Mock required env vars for Settings
os.environ.setdefault("WHISPER_MODEL_PATH", "mock")
os.environ.setdefault("BEAM_SIZE", "5")
os.environ.setdefault("VAD_FILTER", "true")
os.environ.setdefault("LANGUAGE", "zh")
os.environ.setdefault("AI_RATE_MODEL_DIR", "mock")
os.environ.setdefault("USE_LM", "false")
os.environ.setdefault("VOLC_TOS_BUCKET", "mock-bucket") # Ensure this is set if not in .env

import time
import tempfile
from app.services.volc_minutes_service import VolcTosUploaderSDK
from app.config import settings

def test_upload_download():
    # Ensure env vars are loaded (assuming they are in current env)
    if not settings.VOLC_TOS_BUCKET:
        print("Skipping test: VOLC_TOS_BUCKET not set")
        return

    print(f"Initializing SDK with bucket: {settings.VOLC_TOS_BUCKET}")
    try:
        uploader = VolcTosUploaderSDK()
    except Exception as e:
        print(f"Failed to init uploader: {e}")
        return

    # Create dummy file
    content = b"Hello TOS Download Test " * 1024 * 1024 # 24MB file
    with tempfile.NamedTemporaryFile(delete=False, suffix=".txt") as source_file:
         source_file.write(content)
         source_path = Path(source_file.name)
    
    object_key = f"test_downloads/test_{int(time.time())}.txt"
    print(f"Uploading {source_path} to {object_key}...")
    
    try:
        url = uploader.upload_file(source_path, object_key, "text/plain")
        print(f"Uploaded to {url}")
    except Exception as e:
        print(f"Upload failed: {e}")
        source_path.unlink()
        return

    # Now download
    with tempfile.NamedTemporaryFile(delete=False, suffix=".downloaded") as dest_file:
        dest_path = Path(dest_file.name)
    # Close it so SDK can open it? or does it matter?
    # mkstemp creates it, NamedTemporaryFile creates it.
    
    print(f"Downloading {object_key} to {dest_path}...")
    try:
        uploader.download_file(object_key, dest_path)
        print("Download finished.")
    except Exception as e:
        print(f"Download failed: {e}")
        import traceback
        traceback.print_exc()

    # Verify content
    if dest_path.exists():
        size = dest_path.stat().st_size
        print(f"Downloaded size: {size}")
        if size == len(content):
            print("Size matches!")
        else:
            print("Size MISMATCH!")
    else:
        print("Destination file does not exist!")

    # Cleanup
    source_path.unlink(missing_ok=True)
    dest_path.unlink(missing_ok=True)

if __name__ == "__main__":
    test_upload_download()
