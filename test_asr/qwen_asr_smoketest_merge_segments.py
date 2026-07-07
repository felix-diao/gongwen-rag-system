import json
import os
import subprocess
import threading
import time
import re
from datetime import datetime
from difflib import SequenceMatcher
from functools import partial
from http.server import HTTPServer, SimpleHTTPRequestHandler

import requests

# ================= 配置 =================
API_URL = "http://14.103.157.248:40001/v1/chat/completions"
MODEL = "/app/model"

AUDIO_URL = "https://meeting-record-temp2.tos-cn-beijing.volces.com/meetings/19/%E6%B6%88%E9%98%B2%E6%BC%94%E7%BB%83%E4%BC%9A%E8%AE%AE1.mp3"

CHUNK_SEC = 6
OVERLAP_SEC = 1
PORT = 8001

SERVER_IP = "8.152.214.78"  # 改成你服务器IP


# ================= 关闭代理 =================
session = requests.Session()
session.trust_env = False


def run_cmd(cmd):
    """执行 shell 命令，失败时抛出异常。"""
    result = subprocess.run(cmd, shell=True, capture_output=True, text=True)
    if result.returncode != 0:
        raise RuntimeError(f"命令执行失败: {cmd}\n{result.stderr.strip()}")
    return result.stdout.strip()


def prepare_output_dir():
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    base_dir = f"asr_smoketest_{ts}"
    chunks_dir = os.path.join(base_dir, "chunks")
    texts_dir = os.path.join(base_dir, "texts")
    os.makedirs(chunks_dir, exist_ok=True)
    os.makedirs(texts_dir, exist_ok=True)
    return base_dir, chunks_dir, texts_dir


# ================= 下载音频 =================
def download_audio(base_dir):
    print("📥 下载音频...")
    r = session.get(AUDIO_URL, timeout=60)
    r.raise_for_status()

    audio_mp3 = os.path.join(base_dir, "audio.mp3")
    with open(audio_mp3, "wb") as f:
        f.write(r.content)
    print(f"✅ 下载完成: {audio_mp3}")
    return audio_mp3


# ================= 启动HTTP服务 =================
def start_http_server(serve_dir):
    handler = partial(SimpleHTTPRequestHandler, directory=serve_dir)
    httpd = HTTPServer(("0.0.0.0", PORT), handler)
    print(f"🌐 HTTP服务启动: http://{SERVER_IP}:{PORT}")
    httpd.serve_forever()


# ================= 切分音频（保存所有分段） =================
def split_audio(audio_mp3, base_dir, chunks_dir):
    print("✂️ 切分音频（带重叠，并保存每段）...")

    audio_wav = os.path.join(base_dir, "audio.wav")
    run_cmd(f'ffmpeg -y -i "{audio_mp3}" -ar 16000 -ac 1 "{audio_wav}" -loglevel quiet')

    duration = float(run_cmd(
        f'ffprobe -i "{audio_wav}" -show_entries format=duration -v quiet -of csv="p=0"'
    ))

    chunks = []
    start = 0.0
    idx = 0
    step = CHUNK_SEC - OVERLAP_SEC
    if step <= 0:
        raise ValueError("CHUNK_SEC 必须大于 OVERLAP_SEC")

    while start < duration:
        end = min(start + CHUNK_SEC, duration)
        filename = f"chunk_{idx:04d}.wav"
        file_path = os.path.join(chunks_dir, filename)

        run_cmd(
            f'ffmpeg -y -i "{audio_wav}" -ss {start:.3f} -to {end:.3f} "{file_path}" -loglevel quiet'
        )

        chunks.append({
            "idx": idx,
            "start": round(start, 3),
            "end": round(end, 3),
            "file_name": filename,
            "file_path": file_path,
        })

        start += step
        idx += 1

    manifest_path = os.path.join(base_dir, "segments_manifest.json")
    with open(manifest_path, "w", encoding="utf-8") as f:
        json.dump(
            {
                "chunk_sec": CHUNK_SEC,
                "overlap_sec": OVERLAP_SEC,
                "duration_sec": duration,
                "segments": chunks,
            },
            f,
            ensure_ascii=False,
            indent=2,
        )

    print(f"✅ 共切分 {len(chunks)} 段，清单已保存: {manifest_path}")
    return chunks


# ================= 清洗模型输出 =================
def clean_text(text):
    if "<asr_text>" in text:
        text = text.split("<asr_text>")[-1]
    return text.strip()


def _normalize_with_map(text):
    """
    归一化文本用于匹配，并保留“归一化字符 -> 原文索引”的映射。
    仅保留中文、英文字母、数字，忽略标点与空白。
    """
    norm_chars = []
    index_map = []
    for i, ch in enumerate(text):
        if ch.isalnum() or ("\u4e00" <= ch <= "\u9fff"):
            norm_chars.append(ch)
            index_map.append(i)
    return "".join(norm_chars), index_map


def _find_anchor_by_normalized_lcs(prev, curr, max_overlap):
    """
    在 prev 尾部和 curr 头部上做归一化 LCS，返回锚点位置：
    prev_anchor_start, curr_anchor_start, anchor_size_norm
    """
    window = 2 * max_overlap
    tail = prev[-window:]
    head = curr[:window]
    tail_offset = len(prev) - len(tail)

    norm_tail, map_tail = _normalize_with_map(tail)
    norm_head, map_head = _normalize_with_map(head)

    if not norm_tail or not norm_head:
        return None

    matcher = SequenceMatcher(None, norm_tail, norm_head)
    best = None
    for block in matcher.get_matching_blocks():
        if block.size < 2:
            continue
        dist_prev_end = len(norm_tail) - (block.a + block.size)
        dist_curr_start = block.b
        score = block.size * 10 - dist_prev_end - dist_curr_start
        if best is None or score > best["score"]:
            best = {
                "a": block.a,
                "b": block.b,
                "size": block.size,
                "score": score,
                "dist_prev_end": dist_prev_end,
                "dist_curr_start": dist_curr_start,
            }

    if best is None:
        return None

    # 锚点必须足够靠近 prev 的尾部和 curr 的头部
    if best["dist_prev_end"] > max(12, best["size"] * 2):
        return None
    if best["dist_curr_start"] > max(12, best["size"] * 2):
        return None

    tail_anchor_start = map_tail[best["a"]]
    head_anchor_start = map_head[best["b"]]

    prev_anchor_start = tail_offset + tail_anchor_start
    curr_anchor_start = head_anchor_start
    return prev_anchor_start, curr_anchor_start, best["size"]


def _cleanup_repeated_phrase(text):
    """
    清理“短词 + 标点 + 同短词”的重复片段：
    例如：咱们不。咱们部门 -> 咱们不。部门
    """
    pattern = r"([\u4e00-\u9fffA-Za-z0-9]{1,4})[。！？，、；：]\1"
    prev = None
    out = text
    while out != prev:
        prev = out
        out = re.sub(pattern, r"\1", out)
    return out


def merge_pair(prev, curr, min_overlap=2, max_overlap=140):
    """
    将 curr 拼接到 prev：
    1) 先做“后缀-前缀”精确去重；
    2) 再做“公共锚点拼接”：若 prev=A+B, curr=C+A+D，则合并为 A+D；
    3) 最后才兜底直接拼接。
    """
    if not prev:
        return curr, {"method": "init"}
    if not curr:
        return prev, {"method": "skip_empty_curr"}

    # 0) curr 完全已包含在 prev 里，直接跳过
    if curr in prev:
        return prev, {"method": "curr_in_prev"}

    # 1) 精确后缀-前缀去重
    max_len = min(max_overlap, len(prev), len(curr))
    for n in range(max_len, min_overlap - 1, -1):
        if prev[-n:] == curr[:n]:
            return prev + curr[n:], {"method": "exact_suffix_prefix", "anchor_size": n}

    # 2) 公共锚点拼接：prev=A+B, curr=C+A+D => A+D
    anchor = _find_anchor_by_normalized_lcs(prev, curr, max_overlap=max_overlap)
    if anchor is not None:
        prev_anchor_start, curr_anchor_start, anchor_size = anchor
        merged = prev[:prev_anchor_start] + curr[curr_anchor_start:]
        merged = _cleanup_repeated_phrase(merged)
        return merged, {
            "method": "anchor_A_plus_D",
            "anchor_size": anchor_size,
            "prev_anchor_start": prev_anchor_start,
            "curr_anchor_start": curr_anchor_start,
        }

    # 3) 兜底
    return prev + curr, {"method": "concat_fallback"}


# ================= 逐段识别并保存文本 =================
def transcribe_chunks(chunks, texts_dir):
    print("\n🧠 开始逐段识别并保存文本...\n")

    results = []
    jsonl_path = os.path.join(os.path.dirname(texts_dir), "chunk_texts.jsonl")

    with open(jsonl_path, "w", encoding="utf-8") as fw:
        for seg in chunks:
            chunk_url = f"http://{SERVER_IP}:{PORT}/{seg['file_name']}"
            payload = {
                "model": MODEL,
                "messages": [
                    {
                        "role": "user",
                        "content": [
                            {
                                "type": "audio_url",
                                "audio_url": {"url": chunk_url},
                            }
                        ],
                    }
                ],
                "max_tokens": 256,
            }

            try:
                response = session.post(API_URL, json=payload, timeout=120)
                response.raise_for_status()
                data = response.json()
                raw = data["choices"][0]["message"]["content"]
                text = clean_text(raw)
            except Exception as e:
                raw = ""
                text = ""
                print(f"❌ 第 {seg['idx']} 段识别失败: {e}")

            item = {
                "idx": seg["idx"],
                "start": seg["start"],
                "end": seg["end"],
                "file_name": seg["file_name"],
                "url": chunk_url,
                "raw": raw,
                "text": text,
            }
            results.append(item)

            txt_file = os.path.join(texts_dir, f"chunk_{seg['idx']:04d}.txt")
            with open(txt_file, "w", encoding="utf-8") as tf:
                tf.write(text)

            fw.write(json.dumps(item, ensure_ascii=False) + "\n")
            fw.flush()

            print(f"✅ 第 {seg['idx']:04d} 段完成: {text}")
            time.sleep(0.1)

    print(f"\n📄 分段识别结果已保存: {jsonl_path}")
    return results


# ================= 拼接所有段文本 =================
def merge_all(results, base_dir):
    print("\n🧩 开始拼接所有段文本...")
    final_text = ""
    merged_steps = []

    for item in results:
        before = final_text
        final_text, merge_info = merge_pair(final_text, item["text"])
        merged_steps.append({
            "idx": item["idx"],
            "before_len": len(before),
            "chunk_len": len(item["text"]),
            "after_len": len(final_text),
            "merge_method": merge_info.get("method"),
            "anchor_size": merge_info.get("anchor_size", 0),
        })

    merged_txt = os.path.join(base_dir, "merged_transcript.txt")
    with open(merged_txt, "w", encoding="utf-8") as f:
        f.write(final_text)

    merged_meta = os.path.join(base_dir, "merged_meta.json")
    with open(merged_meta, "w", encoding="utf-8") as f:
        json.dump(
            {
                "chunk_sec": CHUNK_SEC,
                "overlap_sec": OVERLAP_SEC,
                "total_segments": len(results),
                "final_text_len": len(final_text),
                "steps": merged_steps,
            },
            f,
            ensure_ascii=False,
            indent=2,
        )

    print(f"✅ 拼接完成: {merged_txt}")
    return final_text


def main():
    base_dir, chunks_dir, texts_dir = prepare_output_dir()
    audio_mp3 = download_audio(base_dir)
    chunks = split_audio(audio_mp3, base_dir, chunks_dir)

    threading.Thread(target=start_http_server, args=(chunks_dir,), daemon=True).start()
    time.sleep(2)

    results = transcribe_chunks(chunks, texts_dir)
    final_text = merge_all(results, base_dir)

    print("\n===== 最终拼接文本 =====\n")
    print(final_text)
    print("\n✅ 完成")


if __name__ == "__main__":
    main()
