import json
import os
import re
import subprocess
import threading
import time
from datetime import datetime
from difflib import SequenceMatcher
from functools import partial
from http.server import HTTPServer, SimpleHTTPRequestHandler
from pathlib import Path

import requests

# ================= 配置 =================
API_URL = "http://14.103.157.248:40001/v1/chat/completions"
MODEL = "/app/model"

AUDIO_URL = "https://meeting-record-temp2.tos-cn-beijing.volces.com/meetings/19/%E6%B6%88%E9%98%B2%E6%BC%94%E7%BB%83%E4%BC%9A%E8%AE%AE1.mp3"

CHUNK_SEC = 6
OVERLAP_SEC = 1
PORT = 8001
SERVER_IP = "8.152.214.78"

# 每段识别后是否打印整段调试信息
PRINT_STEP_DEBUG = True


# ================= 关闭代理 =================
session = requests.Session()
session.trust_env = False


def run_cmd(cmd: str) -> str:
    result = subprocess.run(cmd, shell=True, capture_output=True, text=True)
    if result.returncode != 0:
        raise RuntimeError(f"命令执行失败: {cmd}\n{result.stderr.strip()}")
    return result.stdout.strip()


def prepare_output_dir():
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    base_dir = f"asr_incremental_{ts}"
    chunks_dir = os.path.join(base_dir, "chunks")
    texts_dir = os.path.join(base_dir, "texts")
    os.makedirs(chunks_dir, exist_ok=True)
    os.makedirs(texts_dir, exist_ok=True)
    return base_dir, chunks_dir, texts_dir


def download_audio(base_dir: str) -> str:
    print("📥 下载音频...")
    r = session.get(AUDIO_URL, timeout=60)
    r.raise_for_status()

    audio_mp3 = os.path.join(base_dir, "audio.mp3")
    with open(audio_mp3, "wb") as f:
        f.write(r.content)
    print(f"✅ 下载完成: {audio_mp3}")
    return audio_mp3


def start_http_server(serve_dir: str):
    handler = partial(SimpleHTTPRequestHandler, directory=serve_dir)
    httpd = HTTPServer(("0.0.0.0", PORT), handler)
    print(f"🌐 HTTP服务启动: http://{SERVER_IP}:{PORT}")
    httpd.serve_forever()


def split_audio(audio_mp3: str, base_dir: str, chunks_dir: str):
    print("✂️ 切分音频（带重叠）...")

    audio_wav = os.path.join(base_dir, "audio.wav")
    run_cmd(f'ffmpeg -y -i "{audio_mp3}" -ar 16000 -ac 1 "{audio_wav}" -loglevel quiet')

    duration = float(
        run_cmd(
            f'ffprobe -i "{audio_wav}" -show_entries format=duration -v quiet -of csv="p=0"'
        )
    )

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

        chunks.append(
            {
                "idx": idx,
                "start": round(start, 3),
                "end": round(end, 3),
                "file_name": filename,
                "file_path": file_path,
            }
        )
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


def clean_text(text: str) -> str:
    if "<asr_text>" in text:
        text = text.split("<asr_text>")[-1]
    return text.strip()


def _normalize_with_map(text: str):
    norm_chars = []
    index_map = []
    for i, ch in enumerate(text):
        if ch.isalnum() or ("\u4e00" <= ch <= "\u9fff"):
            norm_chars.append(ch)
            index_map.append(i)
    return "".join(norm_chars), index_map


def _find_anchor_by_normalized_lcs(prev: str, curr: str, max_overlap: int):
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

    if best["dist_prev_end"] > max(12, best["size"] * 2):
        return None
    if best["dist_curr_start"] > max(12, best["size"] * 2):
        return None

    tail_anchor_start = map_tail[best["a"]]
    head_anchor_start = map_head[best["b"]]
    return tail_offset + tail_anchor_start, head_anchor_start, best["size"]


def _cleanup_repeated_phrase(text: str) -> str:
    pattern = r"([\u4e00-\u9fffA-Za-z0-9]{1,4})[。！？，、；：]\1"
    prev = None
    out = text
    while out != prev:
        prev = out
        out = re.sub(pattern, r"\1", out)
    return out


def merge_pair(prev: str, curr: str, min_overlap: int = 2, max_overlap: int = 140):
    if not prev:
        return curr, {"method": "init"}
    if not curr:
        return prev, {"method": "skip_empty_curr"}
    if curr in prev:
        return prev, {"method": "curr_in_prev"}

    max_len = min(max_overlap, len(prev), len(curr))
    for n in range(max_len, min_overlap - 1, -1):
        if prev[-n:] == curr[:n]:
            return prev + curr[n:], {"method": "exact_suffix_prefix", "anchor_size": n}

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

    return prev + curr, {"method": "concat_fallback"}


def incremental_transcribe_and_merge(chunks, base_dir: str, texts_dir: str):
    print("\n🚀 开始增量识别并增量拼接输出...\n")

    merged_text = ""
    results = []

    chunk_jsonl_path = os.path.join(base_dir, "chunk_texts.jsonl")
    stream_jsonl_path = os.path.join(base_dir, "incremental_merged.jsonl")
    merged_txt_path = os.path.join(base_dir, "merged_transcript_incremental.txt")

    with open(chunk_jsonl_path, "w", encoding="utf-8") as chunk_fw, open(
        stream_jsonl_path, "w", encoding="utf-8"
    ) as stream_fw:
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
                curr_text = clean_text(raw)
            except Exception as e:
                raw = ""
                curr_text = ""
                print(f"\n❌ 第 {seg['idx']:04d} 段识别失败: {e}")

            old_merged = merged_text
            merged_text, merge_info = merge_pair(merged_text, curr_text)

            # 增量输出：若存在边界修正（非纯 append），则输出“全量快照”以便前端替换渲染。
            if merged_text.startswith(old_merged):
                delta = merged_text[len(old_merged) :]
                if delta:
                    print(delta, end="", flush=True)
            else:
                print("\n\n[边界修正，当前完整文本快照]")
                print(merged_text, end="", flush=True)

            step_item = {
                "idx": seg["idx"],
                "start": seg["start"],
                "end": seg["end"],
                "file_name": seg["file_name"],
                "url": chunk_url,
                "raw": raw,
                "text": curr_text,
                "merge_method": merge_info.get("method"),
                "anchor_size": merge_info.get("anchor_size", 0),
                "merged_text": merged_text,
                "merged_len": len(merged_text),
            }
            results.append(step_item)

            txt_file = os.path.join(texts_dir, f"chunk_{seg['idx']:04d}.txt")
            with open(txt_file, "w", encoding="utf-8") as tf:
                tf.write(curr_text)

            chunk_fw.write(json.dumps(step_item, ensure_ascii=False) + "\n")
            chunk_fw.flush()
            stream_fw.write(json.dumps(step_item, ensure_ascii=False) + "\n")
            stream_fw.flush()

            with open(merged_txt_path, "w", encoding="utf-8") as mf:
                mf.write(merged_text)

            if PRINT_STEP_DEBUG:
                print(
                    f"\n\n[step={seg['idx']:04d} method={merge_info.get('method')} "
                    f"chunk_len={len(curr_text)} merged_len={len(merged_text)}]"
                )
            time.sleep(0.1)

    print("\n\n✅ 增量识别完成")
    print(f"📄 分段结果: {chunk_jsonl_path}")
    print(f"📄 增量拼接轨迹: {stream_jsonl_path}")
    print(f"📄 最终文本: {merged_txt_path}")
    return merged_text, results


def main():
    base_dir, chunks_dir, texts_dir = prepare_output_dir()
    audio_mp3 = download_audio(base_dir)
    chunks = split_audio(audio_mp3, base_dir, chunks_dir)

    threading.Thread(target=start_http_server, args=(chunks_dir,), daemon=True).start()
    time.sleep(2)

    final_text, _ = incremental_transcribe_and_merge(chunks, base_dir, texts_dir)

    print("\n===== 最终拼接文本 =====\n")
    print(final_text)
    print("\n✅ 完成")


if __name__ == "__main__":
    main()
