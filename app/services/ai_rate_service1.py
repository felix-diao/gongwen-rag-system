import os
import re
import math
import collections
import statistics
import traceback
from dotenv import load_dotenv

# =============================
# 加载 .env 配置
# =============================
load_dotenv()

MODEL_DIR = os.getenv("AI_RATE_MODEL_DIR", "/root/models/tests/gpt2")
USE_LM = os.getenv("USE_LM", "True").lower() == "true"

from app.utils.logger import get_logger

logger = get_logger("ai_rate_service1")

_lm = None
_tokenizer = None
_lm_error = None

def _ensure_model() -> bool:
    """加载本地语言模型，自动选择 GPU/CPU"""
    global _lm, _tokenizer, _lm_error
    if not USE_LM:
        return False
    if _lm is not None:
        return True
    if _lm_error is not None:
        return False


    try:
        from transformers import AutoTokenizer, AutoModelForCausalLM
        import torch

        logger.info(f"[AI率检测] 正在加载本地语言模型: {MODEL_DIR}")

        _tokenizer = AutoTokenizer.from_pretrained(MODEL_DIR)
        _lm = AutoModelForCausalLM.from_pretrained(MODEL_DIR)

        device = "cuda" if torch.cuda.is_available() else "cpu"
        _lm.to(device)
        _lm.eval()

        logger.info(f"[AI率检测] 模型加载成功，使用设备: {device}")
        return True
    except Exception as e:
        _lm_error = e
        logger.warning(f"[AI率检测] 模型加载失败: {e}")
        logger.debug("\n".join(traceback.format_exception_only(type(e), e)).strip())
        return False

def _tokenize(text: str):
    if not text:
        return []
    if re.search(r"[\u4e00-\u9fff]", text):
        return [ch for ch in text if not ch.isspace()]
    return re.findall(r"\w+", text)

def _shannon_entropy(tokens) -> float:
    if not tokens:
        return 0.0
    cnt = collections.Counter(tokens)
    total = len(tokens)
    ent = 0.0
    for v in cnt.values():
        p = v / total
        ent -= p * math.log2(p)
    return ent

def compute_ai_rate(text: str) -> float:
    """计算文本的 AI 率（0-100）"""
    logger.info("进入 AI 率计算")

    text = (text or "").strip()
    if not text:
        return 0.0

    sentences = [s.strip() for s in re.split(r"[。！？!?\n]+", text) if s.strip()]
    tokens = _tokenize(text)
    total_tokens = len(tokens)
    unique_tokens = len(set(tokens))
    lexical_richness = (unique_tokens / total_tokens) if total_tokens > 0 else 0.0
    char_entropy = _shannon_entropy(tokens)

    rep_counts = collections.Counter(sentences)
    repetition_rate = (sum(c for s, c in rep_counts.items() if c > 1) / len(sentences)) if sentences else 0.0

    sent_lens = [len(re.findall(r"\w+|[\u4e00-\u9fff]", s)) for s in sentences] if sentences else []
    avg_sent_len = statistics.mean(sent_lens) if sent_lens else 0.0
    sent_len_std = statistics.pstdev(sent_lens) if sent_lens else 0.0
    cv = (sent_len_std / avg_sent_len) if avg_sent_len > 0 else 0.0

    rep_score = min(1.0, repetition_rate * 2.0)
    richness_score = 1.0 - min(1.0, lexical_richness)
    entropy_score = 1.0 - min(1.0, char_entropy / 8.0)
    var_score = 1.0 - min(1.0, cv / 1.0)
    w_rep, w_rich, w_ent, w_var = 0.20, 0.30, 0.30, 0.20
    heuristic_score = (rep_score * w_rep) + (richness_score * w_rich) + (entropy_score * w_ent) + (var_score * w_var)

    perp_available = False
    perp_score = None
    if _ensure_model():
        try:
            import torch
            enc = _tokenizer(text, return_tensors="pt", truncation=True, max_length=1024)
            device = next(_lm.parameters()).device
            input_ids = enc["input_ids"].to(device)
            with torch.no_grad():
                outputs = _lm(input_ids, labels=input_ids)
            loss = outputs.loss.item()
            perp = math.exp(min(20.0, max(0.0, loss)))
            logp = math.log(max(1.0, perp))
            normalized = (logp - 1.0) / 6.0
            normalized = min(1.0, max(0.0, normalized))
            perp_score = round((1.0 - normalized), 4)
            perp_available = True
            logger.info(f"[AI率检测] 困惑度: {perp:.4f}, 得分: {perp_score:.4f}")
        except Exception as e:
            logger.warning(f"[AI率检测] 困惑度计算失败: {e.__class__.__name__}: {e}")

    if perp_available and perp_score is not None:
        w_model = 0.55
        w_heur = 0.45
        combined = (perp_score * w_model) + (heuristic_score * w_heur)
    else:
        combined = heuristic_score

    ai_rate = round(float(combined * 100.0), 2)
    return ai_rate

if __name__ == "__main__":
    text = "运载火箭可以将各种人造卫星、飞船、空间站等航天器送入太空。目前，运载火箭多为一次性运载工具。..."
    ai_rate = compute_ai_rate(text)
    logger.info(f"AI 率: {ai_rate}%")
