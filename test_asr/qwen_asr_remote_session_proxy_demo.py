# coding=utf-8
"""
本地前端 + 本地代理 + 远程甲方会话式 ASR 服务。

用途：
- 甲方已部署好 demo_streaming（/api/start /api/chunk /api/finish）。
- 你在本地启动本脚本，浏览器访问本地页面进行麦克风实时转写。
- 本地服务仅做转发，不加载模型，不需要本地 GPU。
"""

import os
from typing import Dict

import requests
from flask import Flask, Response, jsonify, request


REMOTE_BASE_URL = "http://14.103.157.248:40001"
LOCAL_HOST = "0.0.0.0"
LOCAL_PORT = 8010
REQUEST_TIMEOUT = (8, 120)

# 可选：若甲方接口有鉴权，可在环境变量里配置 token
# export REMOTE_BEARER_TOKEN="xxx"
REMOTE_BEARER_TOKEN = os.getenv("REMOTE_BEARER_TOKEN", "").strip()

app = Flask(__name__)


def _headers(extra: Dict[str, str] | None = None) -> Dict[str, str]:
    h: Dict[str, str] = {}
    if REMOTE_BEARER_TOKEN:
        h["Authorization"] = f"Bearer {REMOTE_BEARER_TOKEN}"
    if extra:
        h.update(extra)
    return h


def _remote_url(path: str) -> str:
    return f"{REMOTE_BASE_URL.rstrip('/')}{path}"


INDEX_HTML = r"""<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width,initial-scale=1" />
  <title>Remote Qwen3-ASR Streaming</title>
  <style>
    body{font-family:system-ui,Arial,sans-serif;max-width:920px;margin:24px auto;padding:0 12px;}
    .row{display:flex;gap:10px;align-items:center;flex-wrap:wrap;margin-bottom:12px;}
    button{padding:8px 14px;border-radius:8px;border:1px solid #cbd5e1;background:#f8fafc;cursor:pointer;}
    button:disabled{opacity:.5;cursor:not-allowed;}
    #status{font-size:13px;color:#334155;}
    #text{white-space:pre-wrap;min-height:240px;border:1px solid #e2e8f0;border-radius:8px;padding:10px;line-height:1.6;background:#fcfcfd;}
    #lang{font-size:13px;color:#0f766e;margin-bottom:8px;}
  </style>
</head>
<body>
  <h3>Remote Qwen3-ASR Streaming</h3>
  <div class="row">
    <button id="btnStart">Start / 开始</button>
    <button id="btnStop" disabled>Stop / 停止</button>
    <button id="btnClear">Clear / 清空</button>
    <span id="status">Idle / 未开始</span>
  </div>
  <div id="lang">language: -</div>
  <div id="text"></div>

<script>
(() => {
  const $ = (id) => document.getElementById(id);
  const btnStart = $("btnStart");
  const btnStop = $("btnStop");
  const btnClear = $("btnClear");
  const statusEl = $("status");
  const langEl = $("lang");
  const textEl = $("text");

  const TARGET_SR = 16000;
  const CHUNK_MS = 500;

  let sessionId = null;
  let running = false;
  let audioCtx = null;
  let source = null;
  let processor = null;
  let mediaStream = null;
  let pushLock = false;
  let buf = new Float32Array(0);

  const setStatus = (t) => { statusEl.textContent = t; };
  const concat = (a,b) => { const o = new Float32Array(a.length+b.length); o.set(a); o.set(b,a.length); return o; };

  function resampleLinear(input, srcSr, dstSr){
    if (srcSr === dstSr) return input;
    const ratio = dstSr / srcSr;
    const outLen = Math.max(0, Math.round(input.length * ratio));
    const out = new Float32Array(outLen);
    for (let i = 0; i < outLen; i++){
      const x = i / ratio;
      const x0 = Math.floor(x);
      const x1 = Math.min(x0 + 1, input.length - 1);
      const t = x - x0;
      out[i] = input[x0] * (1 - t) + input[x1] * t;
    }
    return out;
  }

  async function apiStart(){
    const r = await fetch("/api/start", { method: "POST" });
    if (!r.ok) throw new Error(await r.text());
    return await r.json();
  }

  async function apiChunk(chunk16k){
    const r = await fetch("/api/chunk?session_id=" + encodeURIComponent(sessionId), {
      method: "POST",
      headers: { "Content-Type": "application/octet-stream" },
      body: chunk16k.buffer
    });
    if (!r.ok) throw new Error(await r.text());
    return await r.json();
  }

  async function apiFinish(){
    const r = await fetch("/api/finish?session_id=" + encodeURIComponent(sessionId), { method: "POST" });
    if (!r.ok) throw new Error(await r.text());
    return await r.json();
  }

  async function stopPipeline(){
    try {
      if (processor) { processor.disconnect(); processor.onaudioprocess = null; }
      if (source) source.disconnect();
      if (audioCtx) await audioCtx.close();
      if (mediaStream) mediaStream.getTracks().forEach(t => t.stop());
    } catch (e) {}
    processor = null; source = null; audioCtx = null; mediaStream = null;
  }

  async function pump(){
    if (pushLock) return;
    pushLock = true;
    const chunkSamples = Math.round(TARGET_SR * CHUNK_MS / 1000);
    try{
      while (running && buf.length >= chunkSamples){
        const chunk = buf.slice(0, chunkSamples);
        buf = buf.slice(chunkSamples);
        const j = await apiChunk(chunk);
        langEl.textContent = "language: " + (j.language || "-");
        textEl.textContent = j.text || "";
      }
    } finally {
      pushLock = false;
    }
  }

  btnStart.onclick = async () => {
    if (running) return;
    textEl.textContent = "";
    langEl.textContent = "language: -";
    buf = new Float32Array(0);
    try {
      setStatus("Starting...");
      const j = await apiStart();
      sessionId = j.session_id;

      mediaStream = await navigator.mediaDevices.getUserMedia({ audio: { channelCount: 1 }, video: false });
      audioCtx = new (window.AudioContext || window.webkitAudioContext)();
      source = audioCtx.createMediaStreamSource(mediaStream);
      processor = audioCtx.createScriptProcessor(4096, 1, 1);

      processor.onaudioprocess = (e) => {
        if (!running) return;
        const input = e.inputBuffer.getChannelData(0);
        const pcm16k = resampleLinear(input, audioCtx.sampleRate, TARGET_SR);
        buf = concat(buf, pcm16k);
        if (!pushLock) pump();
      };

      source.connect(processor);
      processor.connect(audioCtx.destination);
      running = true;
      btnStart.disabled = true;
      btnStop.disabled = false;
      setStatus("Listening...");
    } catch (e) {
      setStatus("Start failed: " + e.message);
      await stopPipeline();
      running = false;
      sessionId = null;
    }
  };

  btnStop.onclick = async () => {
    if (!running) return;
    running = false;
    btnStart.disabled = false;
    btnStop.disabled = true;
    setStatus("Finishing...");
    await stopPipeline();
    try {
      const j = await apiFinish();
      langEl.textContent = "language: " + (j.language || "-");
      textEl.textContent = j.text || "";
      setStatus("Stopped");
    } catch (e) {
      setStatus("Finish failed: " + e.message);
    } finally {
      sessionId = null;
      buf = new Float32Array(0);
      pushLock = false;
    }
  };

  btnClear.onclick = () => { textEl.textContent = ""; };
})();
</script>
</body>
</html>
"""


@app.get("/")
def index():
    return Response(INDEX_HTML, mimetype="text/html; charset=utf-8")


@app.post("/api/start")
def api_start():
    s = requests.Session()
    s.trust_env = False
    r = s.post(_remote_url("/api/start"), headers=_headers(), timeout=REQUEST_TIMEOUT)
    return Response(r.content, status=r.status_code, mimetype="application/json")


@app.post("/api/chunk")
def api_chunk():
    session_id = request.args.get("session_id", "").strip()
    if not session_id:
        return jsonify({"error": "missing session_id"}), 400
    raw = request.get_data(cache=False)
    s = requests.Session()
    s.trust_env = False
    r = s.post(
        _remote_url(f"/api/chunk?session_id={session_id}"),
        data=raw,
        headers=_headers({"Content-Type": "application/octet-stream"}),
        timeout=REQUEST_TIMEOUT,
    )
    return Response(r.content, status=r.status_code, mimetype="application/json")


@app.post("/api/finish")
def api_finish():
    session_id = request.args.get("session_id", "").strip()
    if not session_id:
        return jsonify({"error": "missing session_id"}), 400
    s = requests.Session()
    s.trust_env = False
    r = s.post(_remote_url(f"/api/finish?session_id={session_id}"), headers=_headers(), timeout=REQUEST_TIMEOUT)
    return Response(r.content, status=r.status_code, mimetype="application/json")


if __name__ == "__main__":
    print(f"[info] Remote base: {REMOTE_BASE_URL}")
    print(f"[info] Local web:   http://127.0.0.1:{LOCAL_PORT}")
    app.run(host=LOCAL_HOST, port=LOCAL_PORT, debug=False, use_reloader=False, threaded=True)
