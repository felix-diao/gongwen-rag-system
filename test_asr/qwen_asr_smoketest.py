import requests
import json
import os
import time
import threading
from http.server import HTTPServer, SimpleHTTPRequestHandler
from difflib import SequenceMatcher

# ================= 配置 =================
API_URL = "http://14.103.157.248:40001/v1/chat/completions"
MODEL = "/app/model"

AUDIO_URL = "https://meeting-record-temp2.tos-cn-beijing.volces.com/meetings/19/%E6%B6%88%E9%98%B2%E6%BC%94%E7%BB%83%E4%BC%9A%E8%AE%AE1.mp3"

CHUNK_SEC = 6        # ⭐推荐 5~8 秒
OVERLAP_SEC = 1      # ⭐关键：1秒 overlap
PORT = 8001

SERVER_IP = "8.152.214.78"   # ⭐改成你服务器IP

# ================= 关闭代理 =================
session = requests.Session()
session.trust_env = False


# ================= 下载音频 =================
def download_audio():
    print("📥 下载音频...")
    r = session.get(AUDIO_URL)
    with open("audio.mp3", "wb") as f:
        f.write(r.content)
    print("✅ 下载完成")


# ================= 启动HTTP服务 =================
def start_http_server():
    handler = SimpleHTTPRequestHandler
    httpd = HTTPServer(("0.0.0.0", PORT), handler)
    print(f"🌐 HTTP服务启动: http://{SERVER_IP}:{PORT}")
    httpd.serve_forever()


# ================= 切分音频 =================
def split_audio():
    print("✂️ 切分音频（带重叠）...")

    os.system("ffmpeg -y -i audio.mp3 -ar 16000 -ac 1 audio.wav -loglevel quiet")

    duration = float(os.popen(
        "ffprobe -i audio.wav -show_entries format=duration -v quiet -of csv='p=0'"
    ).read())

    chunks = []
    start = 0.0
    idx = 0

    while start < duration:
        end = start + CHUNK_SEC
        filename = f"chunk_{idx}.wav"

        os.system(
            f"ffmpeg -y -i audio.wav -ss {start} -to {end} {filename} -loglevel quiet"
        )

        chunks.append(filename)

        # ⭐滑动窗口（关键）
        start += (CHUNK_SEC - OVERLAP_SEC)
        idx += 1

    print(f"✅ 共切分 {len(chunks)} 段")
    return chunks


# ================= 清洗模型输出 =================
def clean_text(text):
    """
    去掉垃圾前缀
    """
    if "<asr_text>" in text:
        text = text.split("<asr_text>")[-1]
    return text.strip()


# ================= 强力拼接（核心） =================
def merge_text(prev, curr):
    """
    使用 SequenceMatcher 做模糊去重
    """
    if not prev:
        return curr

    matcher = SequenceMatcher(None, prev, curr)
    match = matcher.find_longest_match(0, len(prev), 0, len(curr))

    # ⭐关键：过滤小重叠（避免误删）
    if match.size < 6:
        return prev + curr

    return prev + curr[match.b + match.size:]


# ================= 伪流式 =================
def stream_asr(chunks):
    print("\n🚀 开始伪流式输出：\n")

    final_text = ""

    for chunk in chunks:
        url = f"http://{SERVER_IP}:{PORT}/{chunk}"

        payload = {
            "model": MODEL,
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "audio_url",
                            "audio_url": {"url": url}
                        }
                    ]
                }
            ],
            "max_tokens": 256
        }

        try:
            response = session.post(API_URL, json=payload)
            result = response.json()

            raw = result["choices"][0]["message"]["content"]
            curr_text = clean_text(raw)

            # ⭐拼接
            merged = merge_text(final_text, curr_text)

            # ⭐只输出新增
            diff = merged[len(final_text):]

            print(diff, end="", flush=True)

            final_text = merged

            time.sleep(0.1)

        except Exception as e:
            print("\n❌ 出错:", e)
            print(response.text)

    print("\n\n✅ 完成")


# ================= 主函数 =================
def main():
    download_audio()
    chunks = split_audio()

    threading.Thread(target=start_http_server, daemon=True).start()
    time.sleep(2)

    stream_asr(chunks)


if __name__ == "__main__":
    main()